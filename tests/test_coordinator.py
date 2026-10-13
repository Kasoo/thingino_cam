"""The coordinator: the startup read, the safety-net poll, push events, the reconnect loop and writes."""
import asyncio
from unittest.mock import patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thingino_cam import coordinator as coordinator_module
from custom_components.thingino_cam.api import AgentAuthError, AgentConnectionError, AgentError, AgentEvent
from custom_components.thingino_cam.const import DOMAIN
from custom_components.thingino_cam.coordinator import ThinginoCoordinator

CAPS = {"audio": {"mic_enabled": True}, "motion": {"sensitivity": True, "cooldown_time": True}}
STATE = {"motion": {"active": False}, "system": {"load": "state"}, "network": {"ip": "state"}}


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield


class FakeAgent:
    """Records the order of calls; a queue of event batches feeds the event stream."""

    def __init__(self):
        self.calls = []
        self.state_data = STATE
        self.config_data = {"image": {"brightness": 50}}
        self.health_data = {"checks": {"system": {"load": "health"}, "network": {"ip": "health"}}}
        self.error = None                     # raised by every read while set
        self.streams = asyncio.Queue()        # each item: a list of events, then an exception to raise

    async def _read(self, name, value):
        self.calls.append(name)
        await asyncio.sleep(0)                # yield, so overlapping calls would interleave
        if self.error:
            raise self.error
        return value

    async def state(self):
        return await self._read("state", self.state_data)

    async def config(self):
        return await self._read("config", dict(self.config_data))

    async def health(self):
        return await self._read("health", self.health_data)

    async def get_setting(self, path):
        return await self._read(f"get {path}", {path: 1})

    async def set_setting(self, path, key, value):
        self.calls.append(f"set {path} {key}={value}")
        if self.error:
            raise self.error
        return {"status": "accepted"}

    async def action(self, name, body=None):
        self.calls.append(f"action {name} {body}")
        if self.error:
            raise self.error

    async def events(self):
        self.calls.append("events")
        items = await self.streams.get()
        for item in items:
            if isinstance(item, Exception):
                raise item
            yield item
        await asyncio.Event().wait()          # like a live stream: only an error ends it


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
async def make(hass, agent):
    made = []

    async def factory(caps=CAPS, refresh=True):
        entry = MockConfigEntry(domain=DOMAIN, title="Cam", data={"host": "h"})
        entry.add_to_hass(hass)
        coord = ThinginoCoordinator(hass, entry, agent, {"id": "cam1"}, caps)
        made.append(coord)
        if refresh:
            await coord.async_refresh()
        return coord

    yield factory
    for coord in made:
        await coord.async_shutdown()


def count_updates(coord):
    updates = []
    coord.async_add_listener(lambda: updates.append(1))
    return updates


# --- startup and poll ---------------------------------------------------------------------------------

async def test_startup_reads_one_call_at_a_time(make, agent):
    coord = await make()
    assert agent.calls == ["state", "config", "get audio/mic-enabled", "get motion/sensitivity",
                           "get motion/cooldown-time"]
    assert set(coord.data.extra) == {"audio/mic-enabled", "motion/sensitivity", "motion/cooldown-time"}
    assert coord.data.config == {"image": {"brightness": 50}}


async def test_startup_skips_settings_the_camera_lacks(make, agent):
    coord = await make({"audio": {"mic_enabled": False}, "motion": {"sensitivity": True}})
    assert agent.calls == ["state", "config", "get motion/sensitivity"]
    assert list(coord.data.extra) == ["motion/sensitivity"]


@pytest.mark.parametrize(("active", "expected"), [(True, True), (False, False), (None, False)])
async def test_initial_motion_comes_from_state(make, agent, active, expected):
    agent.state_data = {"motion": {"active": active}} if active is not None else {}
    assert (await make()).data.motion is expected


async def test_poll_reads_config_only_without_health_users(make, agent):
    coord = await make({})
    agent.calls.clear()
    agent.config_data = {"image": {"brightness": 70}}
    await coord.async_refresh()
    assert agent.calls == ["config"]
    assert coord.data.config == {"image": {"brightness": 70}}


async def test_poll_reads_health_then_config_while_an_entity_uses_it(make, agent):
    coord = await make({})
    coord.health_users.add("sensor.cam_load")
    agent.calls.clear()
    await coord.async_refresh()
    assert agent.calls == ["health", "config"]
    assert coord.data.health == agent.health_data


async def test_poll_keeps_event_driven_values(make, agent):
    coord = await make({})
    coord.data.motion = True
    coord.data.sample = {"lux": 3}
    await coord.async_refresh()
    assert coord.data.motion is True and coord.data.sample == {"lux": 3}


async def test_system_and_network_fall_back_to_startup_state_until_health_is_read(make, agent):
    coord = await make({})
    assert coord.data.system == {"load": "state"} and coord.data.network == {"ip": "state"}
    coord.health_users.add("sensor.cam_load")
    await coord.async_refresh()
    assert coord.data.system == {"load": "health"} and coord.data.network == {"ip": "health"}


async def test_rejected_credentials_ask_for_reauth(make, agent):
    coord = await make(refresh=False)
    agent.error = AgentAuthError("401")
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_unreachable_camera_fails_the_update_and_keeps_old_data(make, agent):
    coord = await make({})
    agent.error = AgentConnectionError("down")
    await coord.async_refresh()
    assert not coord.last_update_success
    assert coord.data.config == {"image": {"brightness": 50}}


# --- push events --------------------------------------------------------------------------------------

async def test_motion_events(make):
    coord = await make()
    updates = count_updates(coord)
    coord._handle_event(AgentEvent("motion.started", {}))
    assert coord.data.motion is True
    coord._handle_event(AgentEvent("motion.stopped", {}))
    assert coord.data.motion is False
    assert len(updates) == 2


async def test_sensor_sample_replaces_the_sample(make):
    coord = await make()
    updates = count_updates(coord)
    coord._handle_event(AgentEvent("sensor.sample", {"lux": 12}))
    assert coord.data.sample == {"lux": 12} and len(updates) == 1


async def test_events_before_the_first_read_are_ignored(make):
    coord = await make(refresh=False)
    coord._handle_event(AgentEvent("motion.started", {}))
    assert coord.data is None


@pytest.mark.parametrize(("event", "reads"), [
    (AgentEvent("state.changed", {"paths": ["motion.active"]}), 0),
    (AgentEvent("state.changed", {"paths": ["motion.active", "privacy.enabled"]}), 1),
    (AgentEvent("state.changed", {"paths": ["daynight.running_mode"]}), 1),
    (AgentEvent("state.changed", {}), 0),
    (AgentEvent("streamer.restarted", {}), 1),
    (AgentEvent("health.warning", {}), 1),
    (AgentEvent("record.completed", {}), 0),
    (AgentEvent("hello", {}), 0),
])
async def test_which_events_reread_health(hass, make, agent, event, reads):
    coord = await make({})
    coord.health_users.add("sensor.cam_load")
    agent.calls.clear()
    coord._handle_event(event)
    await hass.async_block_till_done()
    assert agent.calls.count("health") == reads


async def test_health_is_not_reread_without_users(hass, make, agent):
    coord = await make({})
    agent.calls.clear()
    coord._handle_event(AgentEvent("streamer.restarted", {}))
    await hass.async_block_till_done()
    assert agent.calls == []


async def test_health_reread_updates_listeners(hass, make, agent):
    coord = await make({})
    coord.health_users.add("sensor.cam_load")
    updates = count_updates(coord)
    coord._handle_event(AgentEvent("health.warning", {}))
    await hass.async_block_till_done()
    assert coord.data.health == agent.health_data and len(updates) == 1


async def test_failed_health_reread_is_swallowed(hass, make, agent):
    coord = await make({})
    coord.health_users.add("sensor.cam_load")
    updates = count_updates(coord)
    agent.error = AgentConnectionError("down")
    coord._handle_event(AgentEvent("health.warning", {}))
    await hass.async_block_till_done()
    assert coord.data.health == {} and updates == []


# --- the event loop -----------------------------------------------------------------------------------

@pytest.fixture
def fast_retry():
    with patch.object(coordinator_module, "EVENT_RETRY_SECONDS", 0.01):
        yield


async def until(predicate, seconds=2):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_event_loop_connects_delivers_and_reconnects(hass, make, agent, fast_retry):
    coord = await make()
    updates = count_updates(coord)
    agent.streams.put_nowait([AgentEvent("hello", {}), AgentEvent("motion.started", {}),
                              AgentConnectionError("dropped")])
    coord.start_events()
    await until(lambda: coord.data.motion)
    # the drop is noticed, listeners hear that the stream is gone, and it reconnects
    await until(lambda: not coord.events_connected and agent.calls.count("events") == 2)
    assert len(updates) >= 2                        # motion, then the disconnect
    agent.streams.put_nowait([AgentEvent("motion.stopped", {}), AgentConnectionError("dropped")])
    await until(lambda: coord.data.motion is False)
    coord._events_task.cancel()


async def test_event_loop_reports_connected_on_the_first_event(hass, make, agent, fast_retry):
    coord = await make()
    agent.streams.put_nowait([AgentEvent("hello", {})])
    coord.start_events()
    await until(lambda: coord.events_connected)
    coord._events_task.cancel()


@pytest.mark.parametrize("error", [AgentAuthError("401"), AgentError("boom")])
async def test_event_loop_retries_after_any_agent_error(hass, make, agent, fast_retry, error):
    coord = await make()
    agent.streams.put_nowait([error])
    agent.streams.put_nowait([AgentEvent("motion.started", {})])
    coord.start_events()
    await until(lambda: coord.data.motion)
    assert agent.calls.count("events") >= 2
    coord._events_task.cancel()


async def test_event_loop_waits_between_attempts(hass, make, agent):
    coord = await make()
    agent.streams.put_nowait([AgentConnectionError("down")])
    coord.start_events()
    await until(lambda: agent.calls.count("events") == 1)
    await asyncio.sleep(0.1)
    assert agent.calls.count("events") == 1         # EVENT_RETRY_SECONDS (15 s) has not passed
    coord._events_task.cancel()


# --- reading and writing settings ---------------------------------------------------------------------

async def test_setting_lookup(make):
    coord = await make()
    assert coord.setting("image", "brightness") == 50
    assert coord.setting("audio/mic-enabled", "audio/mic-enabled") == 1
    assert coord.setting("image", "missing") is None
    assert coord.setting("nothing/here", "x") is None


async def test_setting_before_the_first_read(make):
    assert (await make(refresh=False)).setting("image", "brightness") is None


async def test_set_setting_mirrors_into_config_without_a_read(make, agent):
    coord = await make()
    updates = count_updates(coord)
    agent.calls.clear()
    await coord.async_set_setting("image/brightness", "brightness", 80)
    assert agent.calls == ["set image/brightness brightness=80"]
    assert coord.data.config["image"]["brightness"] == 80 and len(updates) == 1


async def test_set_setting_creates_a_missing_section(make):
    coord = await make()
    await coord.async_set_setting("privacy", "enabled", True)
    assert coord.data.config["privacy"] == {"enabled": True}


async def test_set_setting_mirrors_into_extras(make):
    coord = await make()
    await coord.async_set_setting("motion/sensitivity", "sensitivity", 4)
    assert coord.data.extra["motion/sensitivity"]["sensitivity"] == 4
    assert "motion" not in coord.data.config


async def test_failed_write_changes_nothing(make, agent):
    coord = await make()
    updates = count_updates(coord)
    agent.error = AgentError("no")
    with pytest.raises(UpdateFailed):
        await coord.async_set_setting("image/brightness", "brightness", 80)
    assert coord.data.config["image"]["brightness"] == 50 and updates == []


@pytest.mark.parametrize(("mode", "stored"), [("day", "day"), ("night", "night"), ("auto", None)])
async def test_set_daynight(make, agent, mode, stored):
    coord = await make()
    await coord.async_set_daynight(mode)
    assert f"action daynight {{'mode': '{mode}'}}" in agent.calls
    assert coord.data.config["daynight"]["force_mode"] == stored


async def test_failed_daynight_change(make, agent):
    coord = await make()
    agent.error = AgentError("no")
    with pytest.raises(UpdateFailed):
        await coord.async_set_daynight("night")
    assert "daynight" not in coord.data.config
