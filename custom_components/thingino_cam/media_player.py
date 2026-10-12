"""Speaker: plays media (TTS, media-source, URLs) on the camera over its RTSP audio backchannel.

The agent API has no playback action, so audio goes through the RTSP server (backchannel.py). The call
returns when the clip has played, so automations can sequence on it. A new clip replaces one in progress.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

import aiohttp

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.ssl import create_client_context

from . import audio, backchannel
from .api import AgentError
from .const import (
    CONF_RTSP_PASSWORD, CONF_RTSP_PORT, CONF_RTSP_USERNAME, CONF_SERVER_CERT, DEFAULT_RTSP_PORT, DOMAIN,
)
from .coordinator import ThinginoCoordinator
from .entity import ThinginoEntity, dig
from .rtsp import stream_target

_LOG = logging.getLogger(__name__)

FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback):
    coord: ThinginoCoordinator = entry.runtime_data
    if dig(coord.capabilities, "audio", "spk_enabled"):
        async_add_entities([ThinginoSpeaker(coord, entry)])


def _error(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=key,
                              translation_placeholders=placeholders or None)


def _pinned_context(pem: str) -> ssl.SSLContext:
    """Trust exactly the camera's own certificate, whatever name it carries. Blocking."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=pem)
    ctx.check_hostname = False
    return ctx


class ThinginoSpeaker(ThinginoEntity, MediaPlayerEntity):
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
                                | MediaPlayerEntityFeature.STOP)
    _attr_state = MediaPlayerState.IDLE

    def __init__(self, coordinator: ThinginoCoordinator, entry: ConfigEntry):
        super().__init__(coordinator, "speaker")
        self._entry = entry
        self._task: asyncio.Task | None = None      # the clip playing or about to
        self._lock = asyncio.Lock()                 # one RTSP session at a time
        self._pinned: ssl.SSLContext | None = None

    async def async_play_media(self, media_type: MediaType | str, media_id: str, **kwargs: Any):
        pcm16k, pcm8k = await self._load(media_id)
        await self._check_speaker_on()
        old, task = self._task, self.hass.async_create_task(self._play(pcm16k, pcm8k))
        self._task = task
        if old is not None:
            old.cancel()            # it tears its session down before releasing the lock
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and not (current and current.cancelling()):
                return              # replaced by another clip, or stopped
            raise

    async def async_media_stop(self):
        if self._task is not None:
            self._task.cancel()

    async def async_will_remove_from_hass(self):
        await self.async_media_stop()
        await super().async_will_remove_from_hass()

    # --- media ----------------------------------------------------------------------------------
    async def _load(self, media_id: str) -> tuple[bytes, bytes]:
        if media_source.is_media_source_id(media_id):
            media_id = (await media_source.async_resolve_media(self.hass, media_id, self.entity_id)).url
        url = async_process_play_media_url(self.hass, media_id)
        data = await self._fetch(url)
        try:
            return await self.hass.async_add_executor_job(audio.decode, data)
        except audio.AudioTooLong as err:
            raise _error("media_too_long", seconds=str(audio.MAX_SECONDS)) from err
        except audio.AudioError as err:
            raise _error("media_unreadable", error=str(err)) from err

    async def _fetch(self, url: str) -> bytes:
        session = async_get_clientsession(self.hass)
        limit = audio.MAX_BYTES
        try:
            async with session.get(url, timeout=FETCH_TIMEOUT) as resp:
                resp.raise_for_status()
                if int(resp.headers.get("Content-Length") or 0) > limit:
                    raise _error("media_too_large", megabytes=str(limit // 1024 // 1024))
                data = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    data += chunk
                    if len(data) > limit:
                        raise _error("media_too_large", megabytes=str(limit // 1024 // 1024))
                return bytes(data)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise _error("media_fetch_failed", error=str(err) or type(err).__name__) from err

    async def _check_speaker_on(self):
        """Read the speaker setting now (0.2-0.3 s on the camera): it isn't in /config or the poll."""
        try:
            setting = await self.coordinator.agent.get_setting("audio/spk-enabled")
        except AgentError as err:
            _LOG.debug("%s: can't read the speaker setting: %s", self._entry.title, err)
            return              # let the RTSP session say what's wrong
        if setting.get("spk_enabled") is False:
            raise _error("speaker_off")

    # --- playback -------------------------------------------------------------------------------
    async def _target(self) -> backchannel.BackchannelTarget:
        data = self._entry.data
        state = self.coordinator.data.state if self.coordinator.data else None
        target = stream_target(state, data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT))
        tls = pinned = None
        if target.scheme == "rtsps":
            tls = create_client_context()
            if (pem := data.get(CONF_SERVER_CERT)) and self._pinned is None:
                self._pinned = await self.hass.async_add_executor_job(_pinned_context, pem)
            pinned = self._pinned
        return backchannel.BackchannelTarget(
            host=data["host"], port=target.port, path=target.path, username=data[CONF_RTSP_USERNAME],
            password=data[CONF_RTSP_PASSWORD], tls=tls, pinned_tls=pinned)

    async def _play(self, pcm16k: bytes, pcm8k: bytes):
        async with self._lock:
            target = await self._target()
            self._attr_state = MediaPlayerState.PLAYING
            self.async_write_ha_state()
            try:
                await backchannel.play(target, pcm16k, pcm8k)
            except backchannel.BackchannelUnavailable as err:
                raise _error("backchannel_unavailable", error=str(err)) from err
            except backchannel.BackchannelAuthError as err:
                raise _error("rtsp_auth") from err
            except backchannel.BackchannelTLSError as err:
                raise _error("rtsp_cert", error=str(err)) from err
            except backchannel.BackchannelError as err:
                raise _error("playback_failed", error=str(err)) from err
            finally:
                if self._task is asyncio.current_task():     # not replaced by a newer clip
                    self._attr_state = MediaPlayerState.IDLE
                    self.async_write_ha_state()
