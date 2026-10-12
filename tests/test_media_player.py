"""The speaker entity, set up through the integration with a fake agent, playing to the fake RTSP server."""
import asyncio
import io
import struct
import wave
from unittest.mock import patch

import pytest
from homeassistant.components import media_source
from homeassistant.components.media_player import ATTR_MEDIA_ANNOUNCE, MediaPlayerState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thingino_cam.const import (
    CONF_MODE, CONF_RTSP_PASSWORD, CONF_RTSP_PORT, CONF_RTSP_USERNAME, CONF_TOKEN, DOMAIN, MODE_DIRECT,
)
from fake_rtsp import PASSWORD, USER, FakeRtspServer

ENTITY = "media_player.cam_speaker"
MEDIA_URL = "http://media.example/clip.wav"


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations, socket_enabled):
    yield


def wav(seconds: float, rate: int = 22050) -> bytes:
    n = int(seconds * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{2 * n}h", *([500, 500, -500, -500] * n)[: 2 * n]))
    return buf.getvalue()


class FakeAgent:
    def __init__(self, port: int, speaker: bool = True, spk_enabled: bool = True):
        self.port, self.speaker, self.spk_enabled = port, speaker, spk_enabled

    async def capabilities(self):
        return {"audio": {"spk_enabled": self.speaker, "mic_enabled": True}}

    async def state(self):
        return {"streams": [{"rtsp_url": f"rtsp://127.0.0.1:{self.port}/ch0"}]}

    async def config(self):
        return {"audio": {}}

    async def get_setting(self, path):
        return {"audio/mic-enabled": {"mic_enabled": True},
                "audio/spk-enabled": {"spk_enabled": self.spk_enabled}}[path]

    async def health(self):
        return {}

    async def events(self):
        await asyncio.Event().wait()
        yield


@pytest.fixture
async def server():
    srv = await FakeRtspServer(mode="raptor").start()
    yield srv
    await srv.stop()


async def setup(hass: HomeAssistant, agent: FakeAgent, password: str = PASSWORD) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Cam", unique_id="cam1", version=2, data={
        "host": "127.0.0.1", CONF_MODE: MODE_DIRECT, CONF_TOKEN: "t", CONF_RTSP_USERNAME: USER,
        CONF_RTSP_PASSWORD: password, CONF_RTSP_PORT: agent.port})
    entry.add_to_hass(hass)
    with patch("custom_components.thingino_cam.async_connect", return_value=(agent, {"id": "cam1"})):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def play(hass: HomeAssistant, media_id: str = MEDIA_URL, **extra):
    await hass.services.async_call("media_player", "play_media", {
        "entity_id": ENTITY, "media_content_id": media_id, "media_content_type": "music", **extra},
        blocking=True)


async def test_no_entity_without_speaker(hass, server):
    await setup(hass, FakeAgent(server.port, speaker=False))
    assert hass.states.get(ENTITY) is None


async def test_play_media_source(hass, server, aioclient_mock):
    await setup(hass, FakeAgent(server.port))
    assert hass.states.get(ENTITY).state == MediaPlayerState.IDLE
    aioclient_mock.get(MEDIA_URL, content=wav(0.4))
    resolved = media_source.PlayMedia(url=MEDIA_URL, mime_type="audio/wav")
    with patch("custom_components.thingino_cam.media_player.media_source.async_resolve_media",
               return_value=resolved) as resolve:
        await play(hass, "media-source://tts/demo?message=hi", **{ATTR_MEDIA_ANNOUNCE: True})
    assert resolve.call_args.args[1] == "media-source://tts/demo?message=hi"
    assert server.torn_down.is_set()
    assert 19 <= len(server.rtp) <= 21 and {h[1] & 0x7F for _, h, _ in server.rtp} == {114}
    assert hass.states.get(ENTITY).state == MediaPlayerState.IDLE


async def test_state_playing(hass, server, aioclient_mock):
    await setup(hass, FakeAgent(server.port))
    aioclient_mock.get(MEDIA_URL, content=wav(0.6))
    task = asyncio.create_task(play(hass))
    await asyncio.sleep(0.4)
    assert hass.states.get(ENTITY).state == MediaPlayerState.PLAYING
    await task
    assert hass.states.get(ENTITY).state == MediaPlayerState.IDLE


async def test_new_clip_replaces_old(hass, server, aioclient_mock):
    await setup(hass, FakeAgent(server.port))
    aioclient_mock.get(MEDIA_URL, content=wav(5))
    aioclient_mock.get("http://media.example/short.wav", content=wav(0.2))
    first = asyncio.create_task(play(hass))
    await asyncio.sleep(0.5)
    await play(hass, "http://media.example/short.wav")
    await asyncio.wait_for(first, 1)                # returns without an error
    assert server.methods.count("TEARDOWN") == 2
    assert len(server.rtp) < 60
    assert hass.states.get(ENTITY).state == MediaPlayerState.IDLE


async def test_stop(hass, server, aioclient_mock):
    await setup(hass, FakeAgent(server.port))
    aioclient_mock.get(MEDIA_URL, content=wav(5))
    task = asyncio.create_task(play(hass))
    await asyncio.sleep(0.5)
    await hass.services.async_call("media_player", "media_stop", {"entity_id": ENTITY}, blocking=True)
    await asyncio.wait_for(task, 1)
    assert server.torn_down.is_set()
    assert hass.states.get(ENTITY).state == MediaPlayerState.IDLE


async def test_speaker_off(hass, server, aioclient_mock):
    await setup(hass, FakeAgent(server.port, spk_enabled=False))
    aioclient_mock.get(MEDIA_URL, content=wav(0.1))
    with pytest.raises(HomeAssistantError) as err:
        await play(hass)
    assert err.value.translation_key == "speaker_off"
    assert server.requests == []


async def test_wrong_rtsp_password(hass, server, aioclient_mock):
    await setup(hass, FakeAgent(server.port), password="wrong")
    aioclient_mock.get(MEDIA_URL, content=wav(0.1))
    with pytest.raises(HomeAssistantError) as err:
        await play(hass)
    assert err.value.translation_key == "rtsp_auth"
    assert hass.states.get(ENTITY).state == MediaPlayerState.IDLE


async def test_no_backchannel(hass, aioclient_mock):
    srv = await FakeRtspServer(backchannel=False).start()
    await setup(hass, FakeAgent(srv.port))
    aioclient_mock.get(MEDIA_URL, content=wav(0.1))
    with pytest.raises(HomeAssistantError) as err:
        await play(hass)
    assert err.value.translation_key == "backchannel_unavailable"
    await srv.stop()


@pytest.mark.parametrize(("content", "status", "key"), [
    (b"not audio at all" * 100, 200, "media_unreadable"),
    (b"", 404, "media_fetch_failed"),
])
async def test_bad_media(hass, server, aioclient_mock, content, status, key):
    await setup(hass, FakeAgent(server.port))
    aioclient_mock.get(MEDIA_URL, content=content, status=status)
    with pytest.raises(HomeAssistantError) as err:
        await play(hass)
    assert err.value.translation_key == key
    assert server.requests == []


async def test_too_long(hass, server, aioclient_mock):
    await setup(hass, FakeAgent(server.port))
    aioclient_mock.get(MEDIA_URL, content=wav(1.0, rate=8000))
    with patch("custom_components.thingino_cam.audio.MAX_SECONDS", 0.5), pytest.raises(HomeAssistantError) as err:
        await play(hass)
    assert err.value.translation_key == "media_too_long"
