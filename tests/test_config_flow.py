"""The config flow (both modes, certificate pinning, reauth) and the v1 to v2 entry migration.

The network is faked: server_trust, fetch_server_cert and async_connect are patched, except in
test_pins_a_real_listener, which runs the certificate steps against a loopback TLS server."""
import asyncio
import ssl
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thingino_cam.api import (
    TRUST_NAME_MISMATCH, TRUST_NONE, TRUST_PUBLIC, AgentAuthError, AgentConnectionError, cert_fingerprint,
)
from custom_components.thingino_cam.const import (
    CONF_API_KEY, CONF_CLIENT_CERT, CONF_CLIENT_KEY, CONF_MODE, CONF_RTSP_PASSWORD, CONF_RTSP_PORT,
    CONF_RTSP_USERNAME, CONF_SERVER_CERT, CONF_TOKEN, DOMAIN, MODE_DIRECT, MODE_WEBUI,
)
from conftest import make_cert

FLOW = "custom_components.thingino_cam.config_flow"
DEVICE = {"id": "cam1", "name": "garage"}
DIRECT = {"host": "camera.example", "token": "tok"}
WEBUI = {"host": "camera.example", "api_key": "key"}
RTSP = {CONF_RTSP_USERNAME: "u", CONF_RTSP_PASSWORD: "p", CONF_RTSP_PORT: 322}


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def webui_enabled():
    """Setup offers web UI mode only while the flag is on (test_web_ui_mode_is_hidden turns it off)."""
    with patch(f"{FLOW}.WEBUI_MODE_ENABLED", True):
        yield


@pytest.fixture(autouse=True)
def entry_setup():
    """Creating or reloading an entry sets it up; the integration itself is tested elsewhere."""
    with patch("custom_components.thingino_cam.async_setup_entry", return_value=True) as mock:
        yield mock


@contextmanager
def network(trust=TRUST_PUBLIC, pem=None, device=DEVICE, connect_error=None, firmware=None, hello=True):
    """Patch the camera: how its certificate verifies, the certificate itself, and the agent's answers."""
    agent = MagicMock()
    agent.wait_for_hello = AsyncMock(return_value=hello)
    info = {**device, "software": {"firmware_version": firmware}} if firmware else device
    connect = AsyncMock(return_value=(agent, info), side_effect=connect_error)
    trust_mock = MagicMock(side_effect=trust if isinstance(trust, Exception) else None,
                           return_value=None if isinstance(trust, Exception) else trust)
    with patch(f"{FLOW}.server_trust", trust_mock) as trust_patch, \
            patch(f"{FLOW}.fetch_server_cert", return_value=pem) as fetch, \
            patch(f"{FLOW}.async_connect", connect):
        trust_patch.fetch = fetch
        yield trust_patch


async def start(hass, mode: str = MODE_DIRECT):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU and result["menu_options"] == [MODE_DIRECT, MODE_WEBUI]
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": mode})


async def submit(hass, result, user_input):
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_web_ui_mode_is_hidden_while_disabled(hass):
    with patch(f"{FLOW}.WEBUI_MODE_ENABLED", False):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == MODE_DIRECT


async def test_direct_public_cert(hass):
    with network():
        result = await start(hass)
        assert result["step_id"] == MODE_DIRECT
        result = await submit(hass, result, DIRECT)
    assert result["step_id"] == "rtsp"
    result = await submit(hass, result, RTSP)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "garage"                  # the camera's own hostname
    assert result["data"] == {**DIRECT, CONF_MODE: MODE_DIRECT, "port": 1998, **RTSP}
    assert result["result"].unique_id == "cam1"


async def test_name_overrides_hostname(hass):
    with network():
        result = await submit(hass, await start(hass), {**DIRECT, "name": " Porch "})
    result = await submit(hass, result, RTSP)
    assert result["title"] == "Porch"


async def test_title_falls_back_to_host(hass):
    with network(device={"id": "cam1"}):
        result = await submit(hass, await start(hass), DIRECT)
    result = await submit(hass, result, RTSP)
    assert result["title"] == "camera.example"


async def test_host_is_stripped(hass):
    with network() as trust:
        result = await submit(hass, await start(hass), {**DIRECT, "host": "  camera.example "})
    assert trust.call_args.args[:2] == ("camera.example", 1998)
    result = await submit(hass, result, RTSP)
    assert result["data"]["host"] == "camera.example"


async def test_self_signed_cert_is_pinned_once_confirmed(hass, self_signed):
    pem = self_signed[1]
    with network(TRUST_NONE, pem):
        result = await submit(hass, await start(hass), DIRECT)
    assert result["step_id"] == "confirm_cert"
    placeholders = result["description_placeholders"]
    assert placeholders["fingerprint"] == cert_fingerprint(pem)
    assert placeholders["subject"] == "camera.local"
    assert placeholders["host"] == "camera.example"
    with network(TRUST_NONE, pem):
        result = await submit(hass, result, {})
    assert result["step_id"] == "rtsp"
    result = await submit(hass, result, RTSP)
    assert result["data"][CONF_SERVER_CERT] == pem


async def test_unconfirmed_cert_is_not_stored(hass, self_signed):
    with network(TRUST_NONE, self_signed[1]):
        result = await submit(hass, await start(hass), DIRECT)
        assert result["step_id"] == "confirm_cert"
    # abandoning the flow creates nothing
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_name_mismatch_is_refused_not_pinned(hass):
    with network(TRUST_NAME_MISMATCH) as trust:
        result = await submit(hass, await start(hass), DIRECT)
    assert result["step_id"] == MODE_DIRECT
    assert result["errors"] == {"base": "cert_name_mismatch"}
    trust.fetch.assert_not_called()


@pytest.mark.parametrize(("mode", "error", "key"), [
    (MODE_DIRECT, OSError("unreachable"), "cannot_connect"),
    (MODE_DIRECT, FileNotFoundError("no cert"), "bad_certificate"),
    (MODE_DIRECT, ssl.SSLError("tlsv13 alert certificate required"), "invalid_auth"),
    (MODE_DIRECT, ssl.SSLError("PEM lib"), "bad_certificate"),
    (MODE_DIRECT, ssl.SSLError("KEY_VALUES_MISMATCH"), "bad_certificate"),
    (MODE_WEBUI, ssl.SSLError("handshake failure"), "cannot_connect"),
    (MODE_WEBUI, OSError("unreachable"), "cannot_connect"),
])
async def test_trust_check_errors(hass, mode, error, key):
    user_input = DIRECT if mode == MODE_DIRECT else WEBUI
    with network(error):
        result = await submit(hass, await start(hass, mode), user_input)
    assert result["step_id"] == mode and result["errors"] == {"base": key}


async def test_error_then_retry_succeeds(hass):
    with network(OSError("unreachable")):
        result = await submit(hass, await start(hass), DIRECT)
    assert result["errors"] == {"base": "cannot_connect"}
    with network():
        result = await submit(hass, result, DIRECT)
    assert result["step_id"] == "rtsp"


async def test_client_cert_paths_are_relative_to_config_dir(hass):
    with network() as trust:
        await submit(hass, await start(hass), {**DIRECT, CONF_CLIENT_CERT: "c.pem", CONF_CLIENT_KEY: "k.pem"})
    assert trust.call_args.args[2:] == (hass.config.path("c.pem"), hass.config.path("k.pem"))


@pytest.mark.parametrize(("mode", "error", "key"), [
    (MODE_DIRECT, AgentAuthError("401"), "invalid_auth"),
    (MODE_WEBUI, AgentAuthError("401"), "invalid_api_key"),
    (MODE_DIRECT, AgentConnectionError("down"), "cannot_connect"),
    (MODE_DIRECT, OSError("cert"), "bad_certificate"),
    (MODE_DIRECT, ValueError("cert"), "bad_certificate"),
])
async def test_agent_errors(hass, mode, error, key):
    user_input = DIRECT if mode == MODE_DIRECT else WEBUI
    firmware = "master+abc, 2026-10-10 12:00:00 UTC"
    with network(connect_error=error, firmware=firmware):
        result = await submit(hass, await start(hass, mode), user_input)
    assert result["step_id"] == mode and result["errors"] == {"base": key}


async def test_webui(hass):
    with network(firmware="master+abc, 2026-10-10 12:00:00 UTC") as trust:
        result = await submit(hass, await start(hass, MODE_WEBUI), WEBUI)
    assert trust.call_args.args[:2] == ("camera.example", 443)
    result = await submit(hass, result, RTSP)
    assert result["data"][CONF_MODE] == MODE_WEBUI and result["data"][CONF_API_KEY] == "key"
    assert CONF_TOKEN not in result["data"]


async def test_webui_firmware_too_old(hass):
    with network(firmware="master+abc, 2026-10-06 12:00:00 UTC"):
        result = await submit(hass, await start(hass, MODE_WEBUI), WEBUI)
    assert result["errors"] == {"base": "firmware_too_old"}


async def test_webui_without_event_stream(hass):
    with network(firmware="master+abc, 2026-10-10 12:00:00 UTC", hello=False):
        result = await submit(hass, await start(hass, MODE_WEBUI), WEBUI)
    assert result["errors"] == {"base": "no_events"}


@pytest.mark.parametrize("firmware", [None, "unreadable"])
async def test_webui_unknown_build_date_is_not_checked(hass, firmware):
    with network(firmware=firmware or "no date here"):
        result = await submit(hass, await start(hass, MODE_WEBUI), WEBUI)
    assert result["step_id"] == "rtsp"


async def test_direct_mode_skips_the_web_ui_probes(hass):
    with network(firmware="master+abc, 2000-01-01 00:00:00 UTC", hello=False):
        result = await submit(hass, await start(hass), DIRECT)
    assert result["step_id"] == "rtsp"


async def test_already_configured(hass):
    MockConfigEntry(domain=DOMAIN, unique_id="cam1", version=2, data={**DIRECT, CONF_MODE: MODE_DIRECT}
                    ).add_to_hass(hass)
    with network():
        result = await submit(hass, await start(hass), {**DIRECT, "host": "other.example"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_pins_a_real_listener(hass, self_signed, socket_enabled):
    """No patched network: a loopback TLS server with a self-signed cert is offered for pinning."""
    # the probe that rejects the unknown CA aborts its handshake, which asyncio logs on the server side
    handler = hass.loop.get_exception_handler()
    hass.loop.set_exception_handler(
        lambda loop, ctx: None if isinstance(ctx.get("exception"), ssl.SSLError) else loop.default_exception_handler(ctx))
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0, ssl=self_signed[0])
    port = server.sockets[0].getsockname()[1]
    try:
        with patch(f"{FLOW}.async_connect", AsyncMock(return_value=(MagicMock(), DEVICE))):
            result = await submit(hass, await start(hass), {"host": "127.0.0.1", "token": "t", "port": port})
            assert result["step_id"] == "confirm_cert"
            assert result["description_placeholders"]["fingerprint"] == cert_fingerprint(self_signed[1])
            result = await submit(hass, await submit(hass, result, {}), RTSP)
        assert result["data"][CONF_SERVER_CERT] == self_signed[1]
    finally:
        server.close()
        await server.wait_closed()
        hass.loop.set_exception_handler(handler)


# --- reauth -------------------------------------------------------------------------------------------

def reauth_entry(hass, pem=None, mode=MODE_DIRECT):
    secret = {CONF_TOKEN: "old"} if mode == MODE_DIRECT else {CONF_API_KEY: "old"}
    data = {"host": "camera.example", CONF_MODE: mode, **secret, **RTSP}
    if pem:
        data[CONF_SERVER_CERT] = pem
    entry = MockConfigEntry(domain=DOMAIN, unique_id="cam1", version=2, data=data)
    entry.add_to_hass(hass)
    return entry


async def test_reauth_direct_updates_the_token(hass):
    entry = reauth_entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm" and not result.get("errors")
    with network():
        result = await submit(hass, result, {CONF_TOKEN: "new"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "new" and entry.data["host"] == "camera.example"


async def test_reauth_webui_updates_the_api_key(hass):
    entry = reauth_entry(hass, mode=MODE_WEBUI)
    result = await entry.start_reauth_flow(hass)
    assert set(result["data_schema"].schema) == {CONF_API_KEY}
    with network(firmware="master+abc, 2026-10-10 12:00:00 UTC"):
        result = await submit(hass, result, {CONF_API_KEY: "new"})
    assert result["reason"] == "reauth_successful" and entry.data[CONF_API_KEY] == "new"


async def test_reauth_rejected_token_stays_on_the_form(hass):
    entry = reauth_entry(hass)
    result = await entry.start_reauth_flow(hass)
    with network(connect_error=AgentAuthError("401")):
        result = await submit(hass, result, {CONF_TOKEN: "bad"})
    assert result["step_id"] == "reauth_confirm" and result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_TOKEN] == "old"


async def test_reauth_cannot_reach_camera(hass):
    entry = reauth_entry(hass)
    result = await entry.start_reauth_flow(hass)
    with network(OSError("unreachable")):
        result = await submit(hass, result, {CONF_TOKEN: "new"})
    assert result["step_id"] == "reauth_confirm" and result["errors"] == {"base": "cannot_connect"}


async def test_reauth_with_unchanged_pinned_cert_asks_nothing(hass, self_signed):
    entry = reauth_entry(hass, pem=self_signed[1])
    result = await entry.start_reauth_flow(hass)
    with network(TRUST_NONE, self_signed[1]):
        result = await submit(hass, result, {CONF_TOKEN: "new"})
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_SERVER_CERT] == self_signed[1] and entry.data[CONF_TOKEN] == "new"


async def test_reauth_with_changed_cert_asks_to_pin_the_new_one(hass, self_signed, tmp_path):
    new_pem = make_cert(tmp_path / "new")[1]
    entry = reauth_entry(hass, pem=self_signed[1])
    result = await entry.start_reauth_flow(hass)
    with network(TRUST_NONE, new_pem):
        result = await submit(hass, result, {CONF_TOKEN: "new"})
    assert result["step_id"] == "confirm_cert"
    assert result["description_placeholders"]["fingerprint"] == cert_fingerprint(new_pem)
    assert entry.data[CONF_SERVER_CERT] == self_signed[1]       # not replaced until confirmed
    with network(TRUST_NONE, new_pem):
        result = await submit(hass, result, {})
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_SERVER_CERT] == new_pem


async def test_reauth_forgets_the_pin_once_the_cert_is_public(hass, self_signed):
    entry = reauth_entry(hass, pem=self_signed[1])
    result = await entry.start_reauth_flow(hass)
    with network(TRUST_PUBLIC):
        result = await submit(hass, result, {CONF_TOKEN: "new"})
    assert result["reason"] == "reauth_successful" and CONF_SERVER_CERT not in entry.data


async def test_reauth_refuses_a_different_camera(hass):
    entry = reauth_entry(hass)
    result = await entry.start_reauth_flow(hass)
    with network(device={"id": "other"}):
        result = await submit(hass, result, {CONF_TOKEN: "new"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_TOKEN] == "old"


# --- entry migration ----------------------------------------------------------------------------------

async def test_v1_entry_migrates_to_direct_mode(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="cam1", version=1, data={"host": "h", CONF_TOKEN: "t"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.version == 2 and entry.data == {"host": "h", CONF_TOKEN: "t", CONF_MODE: MODE_DIRECT}


async def test_entry_from_a_newer_version_is_not_loaded(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="cam1", version=3, data={"host": "h"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR and entry.version == 3
