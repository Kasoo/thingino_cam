"""Camera: the RTSP(S) stream the agent reports in /state (rtsp.py), stills from the snapshot action.

Stills are cached for SNAPSHOT_CACHE_SECONDS: dashboard cards refresh their image
every few seconds per viewer, and each agent snapshot costs the camera's single core ~0.5-1 s.
Concurrent requests for the same stream share one fetch. Live view uses the stream, not this path.
"""
from __future__ import annotations

import asyncio
import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import AgentError
from .const import CONF_RTSP_PASSWORD, CONF_RTSP_PORT, CONF_RTSP_USERNAME, DEFAULT_RTSP_PORT
from .coordinator import ThinginoCoordinator
from .entity import ThinginoEntity, dig
from .rtsp import stream_target, stream_url

SNAPSHOT_CACHE_SECONDS = 60


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([ThinginoCamera(entry.runtime_data, entry)])


class ThinginoCamera(ThinginoEntity, Camera):
    _attr_name = None          # the device's main entity
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_brand = "Thingino"

    def __init__(self, coordinator: ThinginoCoordinator, entry: ConfigEntry) -> None:
        ThinginoEntity.__init__(self, coordinator, "camera")
        Camera.__init__(self)
        self._attr_translation_key = None
        data = entry.data
        self._host = data["host"]
        self._rtsp_user = data[CONF_RTSP_USERNAME]
        self._rtsp_password = data[CONF_RTSP_PASSWORD]
        self._rtsp_port = data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
        # stream id -> (monotonic time, JPEG); one lock per stream coalesces concurrent fetches
        self._snapshots: dict[int, tuple[float, bytes]] = {}
        self._snapshot_locks: dict[int, asyncio.Lock] = {0: asyncio.Lock(), 1: asyncio.Lock()}

    async def stream_source(self) -> str | None:
        # scheme/port/path from the agent's rtsp_url; its host is ignored (see rtsp.py)
        target = stream_target(self.coordinator.data.state if self.coordinator.data else None, self._rtsp_port)
        return stream_url(target, self._host, self._rtsp_user, self._rtsp_password)

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        # stream 1 is the small sub-stream; good enough for thumbnails
        stream_id = 1 if width is not None and width <= 640 else 0
        async with self._snapshot_locks[stream_id]:
            cached = self._snapshots.get(stream_id)
            if cached and time.monotonic() - cached[0] < SNAPSHOT_CACHE_SECONDS:
                return cached[1]
            # one retry: a snapshot can fail while the camera's single core is briefly saturated
            for _attempt in (1, 2):
                try:
                    image = await self.coordinator.agent.snapshot(stream_id)
                except AgentError:
                    continue
                self._snapshots[stream_id] = (time.monotonic(), image)
                return image
            # camera busy or unreachable: an older still beats a broken image
            return cached[1] if cached else None

    @property
    def is_on(self) -> bool:
        return not bool(dig(self.coordinator.data.config if self.coordinator.data else None,
                            "privacy", "enabled"))
