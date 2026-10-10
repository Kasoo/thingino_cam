"""Thingino camera via the on-camera agent API (agentd, /api/v1): through the web UI's agent proxy
(API key) or directly to the agent's TLS listener (token, optional mTLS); see config_flow.py.

One config entry = one camera = one HA device: RTSPS camera, motion (pushed over SSE), privacy,
audio, image tuning, day/night, diagnostics. Design: AGENTS.md at the repo root.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import AgentAuthError, AgentError
from .config_flow import async_connect
from .const import CONF_MODE, MODE_DIRECT
from .coordinator import ThinginoCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR, Platform.BUTTON, Platform.CAMERA, Platform.NUMBER, Platform.SELECT,
    Platform.SENSOR, Platform.SWITCH,
]

type ThinginoConfigEntry = ConfigEntry[ThinginoCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ThinginoConfigEntry) -> bool:
    try:
        agent, device = await async_connect(hass, entry.data)
        capabilities = await agent.capabilities()
    except AgentAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except AgentError as err:
        raise ConfigEntryNotReady(str(err)) from err
    except OSError as err:
        raise ConfigEntryNotReady(f"client certificate: {err}") from err

    coordinator = ThinginoCoordinator(hass, entry, agent, device, capabilities)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.start_events()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ThinginoConfigEntry) -> bool:
    if entry.version > 2:
        return False                # from a newer version of this integration
    if entry.version == 1:
        # v1 entries all talk to agentd directly (token + mTLS client cert)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MODE: MODE_DIRECT}, version=2)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ThinginoConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
