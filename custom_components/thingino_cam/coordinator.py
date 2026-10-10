"""One coordinator per camera: event-driven, with a slow safety-net poll.

Load matters: every agent call runs shell adapters on the camera's single core, and six concurrent
calls a minute saturated it (snapshots and /state timed out). So: everything is read once at
startup, one call at a time; afterwards motion, day/night and scene brightness arrive over /events,
writes are mirrored into the cache from their responses, and only cheap /health (plus /config,
for edits made in the camera's web UI) is polled every SCAN_INTERVAL_SECONDS or on a state.changed
event that the stream does not already cover.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AgentAuthError, AgentError, AgentEvent, ThinginoAgent
from .const import DOMAIN, EVENT_RETRY_SECONDS, SCAN_INTERVAL_SECONDS

_LOG = logging.getLogger(__name__)

# Settings that GET /config does not include: (setting path, capability path) pairs, read one by one.
EXTRA_SETTINGS = (
    ("audio/mic-enabled", ("audio", "mic_enabled")),
    ("audio/spk-enabled", ("audio", "spk_enabled")),
    ("motion/sensitivity", ("motion", "sensitivity")),
    ("motion/cooldown-time", ("motion", "cooldown_time")),
)


# state.changed paths that need no re-read: the event stream already delivers them
EVENT_COVERED_PATHS = {"motion.active"}


@dataclass
class CameraData:
    state: dict[str, Any]          # read once at startup (stream endpoints, initial motion/day-night)
    config: dict[str, Any]
    health: dict[str, Any] = field(default_factory=dict)    # polled: system, network, streaming
    # setting path -> resource, for EXTRA_SETTINGS
    extra: dict[str, dict[str, Any]] = field(default_factory=dict)
    # from push events, not the poll
    motion: bool = False
    sample: dict[str, Any] = field(default_factory=dict)

    @property
    def system(self) -> dict[str, Any]:
        checks = self.health.get("checks") or {}
        return checks.get("system") or self.state.get("system") or {}

    @property
    def network(self) -> dict[str, Any]:
        checks = self.health.get("checks") or {}
        return checks.get("network") or self.state.get("network") or {}


class ThinginoCoordinator(DataUpdateCoordinator[CameraData]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, agent: ThinginoAgent,
                 device: dict[str, Any], capabilities: dict[str, Any]) -> None:
        super().__init__(
            hass, _LOG, config_entry=entry, name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
            # state.changed events can arrive in bursts; coalesce the refreshes they trigger
            request_refresh_debouncer=Debouncer(hass, _LOG, cooldown=2.0, immediate=False))
        self.agent = agent
        self.device = device
        self.capabilities = capabilities
        self.events_connected = False
        self._events_task: asyncio.Task | None = None
        self._extra_paths = [path for path, cap in EXTRA_SETTINGS if _has(capabilities, cap)]

    @property
    def device_id(self) -> str:
        return self.device["id"]

    async def _async_update_data(self) -> CameraData:
        # Sequential on purpose: concurrent agent calls compete for the camera's single core.
        old = self.data
        try:
            if old is None:
                state = await self.agent.state()
                config = await self.agent.config()
                extra = {path: await self.agent.get_setting(path) for path in self._extra_paths}
                health = await self.agent.health()
                return CameraData(state=state, config=config, health=health, extra=extra,
                                  motion=bool((state.get("motion") or {}).get("active")))
            old.health = await self.agent.health()
            old.config = await self.agent.config()
        except AgentAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AgentError as err:
            raise UpdateFailed(str(err)) from err
        return old

    async def _async_refresh_health(self) -> None:
        if self.data is None:
            return
        try:
            self.data.health = await self.agent.health()
        except AgentError as err:
            _LOG.debug("%s: health refresh failed: %s", self.config_entry.title, err)
            return
        self.async_update_listeners()

    # --- writes -------------------------------------------------------------------------------
    def setting(self, path: str, key: str) -> Any:
        """Current value of a setting, from /config or the individually read extras."""
        if self.data is None:
            return None
        if path in self.data.extra:
            return self.data.extra[path].get(key)
        section = self.data.config.get(path.split("/", 1)[0])
        return section.get(key) if isinstance(section, dict) else None

    async def async_set_setting(self, path: str, key: str, value: Any) -> None:
        """PATCH one setting and mirror it into the cache (no re-read: it would cost camera CPU)."""
        try:
            await self.agent.set_setting(path, key, value)
        except AgentError as err:
            raise UpdateFailed(f"setting {path}: {err}") from err
        if self.data is not None:
            if path in self.data.extra:
                self.data.extra[path][key] = value
            else:
                self.data.config.setdefault(path.split("/", 1)[0], {})[key] = value
            self.async_update_listeners()

    async def async_set_daynight(self, mode: str) -> None:
        try:
            await self.agent.action("daynight", {"mode": mode})
        except AgentError as err:
            raise UpdateFailed(f"day/night: {err}") from err
        if self.data is not None:
            self.data.config.setdefault("daynight", {})["force_mode"] = None if mode == "auto" else mode
            self.async_update_listeners()

    async def async_action(self, name: str, body: dict[str, Any] | None = None) -> None:
        """Fire-and-forget actions (reboot, streamer restart): the event stream reports the effects."""
        try:
            await self.agent.action(name, body)
        except AgentError as err:
            raise UpdateFailed(f"action {name}: {err}") from err

    # --- push events --------------------------------------------------------------------------
    def start_events(self) -> None:
        self._events_task = self.config_entry.async_create_background_task(
            self.hass, self._events_loop(), f"{DOMAIN} events {self.config_entry.entry_id}")

    async def _events_loop(self) -> None:
        while True:
            try:
                async for event in self.agent.events():
                    if not self.events_connected:
                        self.events_connected = True
                        _LOG.debug("%s: event stream connected", self.config_entry.title)
                    self._handle_event(event)
            except AgentAuthError as err:
                _LOG.warning("%s: event stream rejected: %s", self.config_entry.title, err)
            except AgentError as err:
                _LOG.debug("%s: event stream lost: %s", self.config_entry.title, err)
            if self.events_connected:
                self.events_connected = False
                self.async_update_listeners()
            await asyncio.sleep(EVENT_RETRY_SECONDS)

    def _handle_event(self, event: AgentEvent) -> None:
        if self.data is None:
            return
        if event.type in ("motion.started", "motion.stopped"):
            self.data.motion = event.type == "motion.started"
            self.async_update_listeners()
        elif event.type == "sensor.sample":
            self.data.sample = event.data
            self.async_update_listeners()
        elif event.type == "state.changed":
            paths = set(event.data.get("paths") or [])
            if paths - EVENT_COVERED_PATHS:
                self.hass.async_create_task(self._async_refresh_health())
        elif event.type in ("streamer.restarted", "health.warning"):
            self.hass.async_create_task(self._async_refresh_health())


def _has(data: dict[str, Any], path: tuple[str, ...]) -> bool:
    for part in path:
        if not isinstance(data, dict):
            return False
        data = data.get(part)
    return bool(data)
