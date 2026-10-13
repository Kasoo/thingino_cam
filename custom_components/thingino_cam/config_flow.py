"""Config flow: the agent's own TLS listener (token; the default), or the web UI's agent proxy (API key).

Direct mode is the designed path: Thingino's agent is the API for remote clients ("optional HTTPS listener
for remote hub access", docs/thingino/next/camera-agent.md; mTLS is security.md's preferred trust model).
It talks to agentd's listener (port 1998) with the agent token and an optional mTLS client certificate;
the agent has to be exposed on the camera first (instructions in the form).
Web UI mode (/x/agent.cgi, X-API-Key) is the no-shell shortcut: it rides on the optional web UI package and
needs two fixes (MIN_WEBUI_BUILD_DATE in const.py); setup checks the build date, then that the key is
accepted and the event stream says hello.
Either way, a certificate that isn't publicly trusted (Thingino generates its own) is shown by fingerprint
and pinned once the user confirms it.
"""
from __future__ import annotations

import logging
import ssl
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import (
    AgentAuthError,
    AgentError,
    ThinginoAgent,
    build_connection,
    cert_fingerprint,
    fetch_server_cert,
    TRUST_NAME_MISMATCH,
    TRUST_PUBLIC,
    parse_build_date,
    server_trust,
)
from .const import (
    CONF_API_KEY,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_MODE,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_USERNAME,
    CONF_SERVER_CERT,
    CONF_TOKEN,
    DEFAULT_PORT,
    DEFAULT_RTSP_PASSWORD,
    DEFAULT_RTSP_PORT,
    DEFAULT_RTSP_USERNAME,
    DEFAULT_WEBUI_PORT,
    DOMAIN,
    EVENT_PROBE_SECONDS,
    MIN_WEBUI_BUILD_DATE,
    MODE_DIRECT,
    MODE_WEBUI,
    WEBUI_MODE_ENABLED,
)

_LOG = logging.getLogger(__name__)
PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _suggest(d: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {"suggested_value": d.get(key)}


def _webui_schema(d: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_HOST, description=_suggest(d, CONF_HOST)): str,
        vol.Required(CONF_API_KEY): PASSWORD,
        vol.Optional(CONF_PORT, default=d.get(CONF_PORT, DEFAULT_WEBUI_PORT)): int,
        vol.Optional(CONF_NAME, description=_suggest(d, CONF_NAME)): str,
    })


def _direct_schema(d: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_HOST, description=_suggest(d, CONF_HOST)): str,
        vol.Required(CONF_TOKEN): PASSWORD,
        vol.Optional(CONF_PORT, default=d.get(CONF_PORT, DEFAULT_PORT)): int,
        vol.Optional(CONF_CLIENT_CERT, description=_suggest(d, CONF_CLIENT_CERT)): str,
        vol.Optional(CONF_CLIENT_KEY, description=_suggest(d, CONF_CLIENT_KEY)): str,
        vol.Optional(CONF_NAME, description=_suggest(d, CONF_NAME)): str,
    })


def _rtsp_schema(d: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_RTSP_USERNAME, default=d.get(CONF_RTSP_USERNAME, DEFAULT_RTSP_USERNAME)): str,
        vol.Required(CONF_RTSP_PASSWORD, default=d.get(CONF_RTSP_PASSWORD, DEFAULT_RTSP_PASSWORD)): PASSWORD,
        vol.Optional(CONF_RTSP_PORT, default=d.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)): int,
    })


async def async_connect(hass, data: Mapping[str, Any]) -> tuple[ThinginoAgent, dict[str, Any]]:
    """Build the client and fetch /device; raises AgentError, OSError (cert files) or ssl.SSLError."""
    connection = await hass.async_add_executor_job(build_connection, data, hass.config.path)
    agent = ThinginoAgent(async_get_clientsession(hass), connection)
    return agent, await agent.device()


def _cert_subject(pem: str) -> str:
    """Who the certificate is for: its DNS/IP names, else its subject (display only)."""
    try:
        from cryptography import x509  # noqa: PLC0415 (a Home Assistant core dependency)
        cert = x509.load_pem_x509_certificate(pem.encode())
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            names = [*san.get_values_for_type(x509.DNSName), *map(str, san.get_values_for_type(x509.IPAddress))]
        except x509.ExtensionNotFound:
            names = []
        return ", ".join(names) or cert.subject.rfc4514_string() or "?"
    except Exception:  # noqa: BLE001
        return "?"


class ThinginoCamConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._pending_cert: str | None = None
        self._device: dict[str, Any] = {}

    # --- entry points ---------------------------------------------------------------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not WEBUI_MODE_ENABLED:
            return await self.async_step_direct()
        return self.async_show_menu(step_id="user", menu_options=[MODE_DIRECT, MODE_WEBUI])

    async def async_step_webui(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_step_mode(MODE_WEBUI, user_input)

    async def async_step_direct(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_step_mode(MODE_DIRECT, user_input)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        if user_input is None:
            self._data = dict(entry.data)
            return self._show_reauth_form({})
        self._data = {**entry.data, **user_input}
        if errors := await self._async_check_cert():
            return self._show_reauth_form(errors)
        return await self._async_after_cert()

    # --- steps ----------------------------------------------------------------------------------
    async def _async_step_mode(self, mode: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = {**user_input, CONF_MODE: mode}
            if not (errors := await self._async_check_cert()):
                return await self._async_after_cert()
        return self._show_mode_form(mode, user_input or {}, errors)

    async def async_step_confirm_cert(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._pending_cert is not None
        if user_input is not None:
            self._data[CONF_SERVER_CERT] = self._pending_cert
            self._pending_cert = None
            return await self._async_validate_and_continue()
        return self.async_show_form(step_id="confirm_cert", data_schema=vol.Schema({}),
                                    description_placeholders={
                                        **self._placeholders(),
                                        "fingerprint": cert_fingerprint(self._pending_cert),
                                        "subject": _cert_subject(self._pending_cert),
                                    })

    async def async_step_rtsp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = {**self._data, **user_input}
            # blank name: the camera's hostname, as the agent reports it
            title = (data.get(CONF_NAME) or "").strip() or self._device.get("name") or data[CONF_HOST]
            return self.async_create_entry(title=title, data=data)
        return self.async_show_form(step_id="rtsp", data_schema=_rtsp_schema(self._data))

    # --- helpers --------------------------------------------------------------------------------
    def _placeholders(self) -> dict[str, str]:
        # Thingino names its releases by build date, e.g. "firmware-2026-10-07"
        return {"host": str(self._data.get(CONF_HOST, "")),
                "min_firmware": f"firmware-{MIN_WEBUI_BUILD_DATE.isoformat()}"}

    def _show_mode_form(self, mode: str, d: Mapping[str, Any], errors: dict[str, str]) -> ConfigFlowResult:
        schema = _webui_schema(d) if mode == MODE_WEBUI else _direct_schema(d)
        return self.async_show_form(step_id=mode, data_schema=schema, errors=errors,
                                    description_placeholders=self._placeholders())

    def _port(self) -> int:
        default = DEFAULT_WEBUI_PORT if self._data[CONF_MODE] == MODE_WEBUI else DEFAULT_PORT
        return int(self._data.get(CONF_PORT) or default)

    async def _async_check_cert(self) -> dict[str, str]:
        """Decide how to trust the camera: public CAs, the already pinned cert, or a new one to confirm."""
        host, port = self._data[CONF_HOST].strip(), self._port()
        self._data[CONF_HOST] = host
        client = ()
        if self._data[CONF_MODE] == MODE_DIRECT and self._data.get(CONF_CLIENT_CERT):
            client = (self.hass.config.path(self._data[CONF_CLIENT_CERT]),
                      self.hass.config.path(self._data.get(CONF_CLIENT_KEY) or ""))
        try:
            trust = await self.hass.async_add_executor_job(server_trust, host, port, *client)
            if trust == TRUST_PUBLIC:
                self._data.pop(CONF_SERVER_CERT, None)
                self._pending_cert = None
                return {}
            if trust == TRUST_NAME_MISMATCH:
                # a public (e.g. Let's Encrypt) cert for another name: pinning it would break on renewal
                return {"base": "cert_name_mismatch"}
            pem = await self.hass.async_add_executor_job(fetch_server_cert, host, port, *client)
        except FileNotFoundError as err:
            _LOG.warning("cannot load the client certificate: %s", err)
            return {"base": "bad_certificate"}
        except ssl.SSLError as err:
            # the TLS handshake itself failed: typically an mTLS-only agent and no (or a refused) client cert
            _LOG.warning("TLS handshake with %s:%s failed: %s", host, port, err)
            if self._data[CONF_MODE] == MODE_DIRECT:
                return {"base": "bad_certificate" if "PEM" in str(err) or "KEY" in str(err) else "invalid_auth"}
            return {"base": "cannot_connect"}
        except OSError as err:
            _LOG.warning("cannot reach %s:%s: %s", host, port, err)
            return {"base": "cannot_connect"}
        # unchanged pinned cert (reauth): nothing to confirm
        same = self._data.get(CONF_SERVER_CERT) and cert_fingerprint(self._data[CONF_SERVER_CERT]) == cert_fingerprint(pem)
        self._pending_cert = None if same else pem
        return {}

    async def _async_after_cert(self) -> ConfigFlowResult:
        if self._pending_cert is not None:
            return await self.async_step_confirm_cert()
        return await self._async_validate_and_continue()

    async def _async_validate_and_continue(self) -> ConfigFlowResult:
        mode = self._data[CONF_MODE]
        if errors := await self._async_validate():
            if self.source == SOURCE_REAUTH:
                return self._show_reauth_form(errors)
            return self._show_mode_form(mode, self._data, errors)
        await self.async_set_unique_id(self._device["id"])
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=self._data)
        # no updates=: a different host can need a different trust decision (a pinned cert, a name
        # mismatch), so an aborted flow never changes the existing entry; reconfigure via reauth
        self._abort_if_unique_id_configured()
        return await self.async_step_rtsp()

    def _show_reauth_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        if self._data.get(CONF_MODE, MODE_DIRECT) == MODE_WEBUI:
            schema = vol.Schema({vol.Required(CONF_API_KEY): PASSWORD})
        else:
            schema = vol.Schema({
                vol.Required(CONF_TOKEN): PASSWORD,
                vol.Optional(CONF_CLIENT_CERT, description=_suggest(self._data, CONF_CLIENT_CERT)): str,
                vol.Optional(CONF_CLIENT_KEY, description=_suggest(self._data, CONF_CLIENT_KEY)): str,
            })
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors,
                                    description_placeholders=self._placeholders())

    async def _async_validate(self) -> dict[str, str]:
        webui = self._data[CONF_MODE] == MODE_WEBUI
        try:
            agent, device = await async_connect(self.hass, self._data)
        except AgentAuthError as err:
            _LOG.warning("camera rejected the credentials: %s", err)
            return {"base": "invalid_api_key" if webui else "invalid_auth"}
        except AgentError as err:
            _LOG.warning("cannot reach the agent: %s", err)
            return {"base": "cannot_connect"}
        except (OSError, ValueError) as err:          # ssl.SSLError is an OSError
            _LOG.warning("cannot load the client certificate: %s", err)
            return {"base": "bad_certificate"}
        if webui:
            version = (device.get("software") or {}).get("firmware_version")
            built = parse_build_date(version)
            if built is not None and built < MIN_WEBUI_BUILD_DATE:
                _LOG.warning("Thingino %r is older than %s", version, MIN_WEBUI_BUILD_DATE)
                return {"base": "firmware_too_old"}
            if built is None:
                _LOG.warning("cannot read a build date from Thingino version %r; not checked", version)
            if not await agent.wait_for_hello(EVENT_PROBE_SECONDS):
                _LOG.warning("no hello event from %s through the web UI proxy", self._data[CONF_HOST])
                return {"base": "no_events"}
        self._device = device
        return {}
