"""Client for the Thingino camera agent API (`agentd`, /api/v1).

Two ways in (Connection, build_connection): directly to agentd's TLS listener (bearer token, plus an
optional mTLS client certificate), or through the web UI's agent proxy /x/agent.cgi (X-API-Key).
API shape: docs/thingino/next/api-rfc.md in thingino-firmware, and `agentctl schema` on the camera.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import socket
import ssl
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import aiohttp

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.util.ssl import create_client_context

from .const import (
    CONF_API_KEY,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_MODE,
    CONF_SERVER_CERT,
    CONF_TOKEN,
    DEFAULT_PORT,
    DEFAULT_WEBUI_PORT,
    MODE_DIRECT,
    MODE_WEBUI,
)
from .rtsp import url_host

_LOG = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
# agentctl spawns shell adapters on a slow MIPS CPU; snapshots can take a few seconds
SNAPSHOT_TIMEOUT = aiohttp.ClientTimeout(total=30)


class AgentError(Exception):
    """Any failure talking to the agent."""


class AgentAuthError(AgentError):
    """TLS client authentication or bearer token rejected."""


class AgentConnectionError(AgentError):
    """Camera unreachable, TLS failure or timeout."""


@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Connection:
    """Where and how to reach the agent API (see MODE_* in const.py)."""
    base_url: str
    headers: dict[str, str]
    ssl_context: ssl.SSLContext


def pinned_context(pem: str) -> ssl.SSLContext:
    """Trust exactly the camera's own certificate (self-signed, CN <hostname>.local), whatever name HA uses
    to reach it. Given cadata, create_default_context loads no system CAs, so no public cert passes
    without the name check. Blocking."""
    ctx = ssl.create_default_context(cadata=pem)
    ctx.check_hostname = False
    return ctx


def build_connection(data: Mapping[str, Any], config_path: Callable[[str], str]) -> Connection:
    """Connection for a config entry's data. Blocking (may read cert files): call from an executor.

    config_path resolves cert/key paths relative to the HA config dir (hass.config.path)."""
    host = url_host(data[CONF_HOST])
    if pem := data.get(CONF_SERVER_CERT):
        ctx = pinned_context(pem)
    else:
        ctx = create_client_context()
    if data.get(CONF_MODE, MODE_DIRECT) == MODE_WEBUI:
        port = data.get(CONF_PORT, DEFAULT_WEBUI_PORT)
        return Connection(f"https://{host}:{port}/x/agent.cgi/api/v1",
                          {"X-API-Key": data[CONF_API_KEY]}, ctx)
    if data.get(CONF_CLIENT_CERT):
        ctx.load_cert_chain(config_path(data[CONF_CLIENT_CERT]), config_path(data[CONF_CLIENT_KEY]))
    return Connection(f"https://{host}:{data.get(CONF_PORT, DEFAULT_PORT)}/api/v1",
                      {"Authorization": f"Bearer {data[CONF_TOKEN]}"}, ctx)


TRUST_PUBLIC = "public"            # verifies against public CAs for this name: nothing to pin
TRUST_NAME_MISMATCH = "mismatch"   # publicly trusted, but issued for another name: use that name instead
TRUST_NONE = "none"                # self-signed (Thingino's default) or unknown CA: offer to pin it

# OpenSSL X509_V_ERR_HOSTNAME_MISMATCH / X509_V_ERR_IP_ADDRESS_MISMATCH
_NAME_MISMATCH_CODES = {62, 64}


def server_trust(host: str, port: int, client_cert: str | None = None, client_key: str | None = None) -> str:
    """How the camera's certificate verifies for this host name (TRUST_*). Blocking.

    The client cert (absolute paths) is presented when given: an mTLS-only agent (mbedTLS) aborts the
    handshake without one. Raises ssl.SSLError for other TLS failures, OSError if unreachable."""
    ctx = create_client_context()
    if client_cert:
        ctx.load_cert_chain(client_cert, client_key)
    try:
        with socket.create_connection((host.strip("[]"), port), timeout=10) as sock, \
                ctx.wrap_socket(sock, server_hostname=host.strip("[]")):
            return TRUST_PUBLIC
    except ssl.SSLCertVerificationError as err:
        return TRUST_NAME_MISMATCH if err.verify_code in _NAME_MISMATCH_CODES else TRUST_NONE


def fetch_server_cert(host: str, port: int, client_cert: str | None = None, client_key: str | None = None) -> str:
    """The camera's certificate (PEM), unverified: to show its fingerprint before pinning it. Blocking."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if client_cert:
        ctx.load_cert_chain(client_cert, client_key)
    with socket.create_connection((host.strip("[]"), port), timeout=10) as sock, \
            ctx.wrap_socket(sock, server_hostname=host.strip("[]")) as tls:
        return ssl.DER_cert_to_PEM_cert(tls.getpeercert(binary_form=True))


def cert_fingerprint(pem: str) -> str:
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def parse_build_date(firmware_version: str | None) -> date | None:
    """Build date from a Thingino version, "<branch>+<commit>, 2026-10-10 12:48:46 UTC"; None if absent."""
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", firmware_version or "")
    try:
        return date(int(m[1]), int(m[2]), int(m[3])) if m else None
    except ValueError:
        return None


class ThinginoAgent:
    def __init__(self, session: aiohttp.ClientSession, connection: Connection) -> None:
        self._session = session
        self._base = connection.base_url
        self._headers = connection.headers
        self._ssl = connection.ssl_context

    async def _request(self, method: str, path: str, *, body: Any = None,
                       timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT) -> aiohttp.ClientResponse:
        try:
            resp = await self._session.request(
                method, self._base + path, headers=self._headers, ssl=self._ssl, timeout=timeout,
                data=json.dumps(body) if body is not None else None)
        except aiohttp.ClientSSLError as err:
            # a rejected client certificate surfaces as a handshake failure
            raise AgentAuthError(f"TLS handshake failed: {err}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            # TLS 1.3: a missing/refused client certificate arrives as an alert after the handshake
            if "alert" in str(err).lower() and "certificate" in str(err).lower():
                raise AgentAuthError(f"{method} {path}: client certificate refused: {err}") from err
            raise AgentConnectionError(f"{method} {path}: {err}") from err
        if resp.status in (401, 403):
            resp.release()
            raise AgentAuthError(f"{method} {path}: HTTP {resp.status}")
        if resp.status >= 400:
            text = await resp.text()
            raise AgentError(f"{method} {path}: HTTP {resp.status}: {text[:200]}")
        return resp

    async def _json(self, method: str, path: str, body: Any = None) -> Any:
        resp = await self._request(method, path, body=body)
        try:
            return await resp.json(content_type=None)
        except ValueError as err:
            raise AgentError(f"{method} {path}: bad JSON: {err}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            # the body can still fail after the headers arrived (truncated, timed out)
            raise AgentConnectionError(f"{method} {path}: {err}") from err

    async def device(self) -> dict[str, Any]:
        return await self._json("GET", "/device")

    async def capabilities(self) -> dict[str, Any]:
        return await self._json("GET", "/capabilities")

    async def state(self) -> dict[str, Any]:
        return await self._json("GET", "/state")

    async def health(self) -> dict[str, Any]:
        return await self._json("GET", "/health")

    async def config(self) -> dict[str, Any]:
        return await self._json("GET", "/config")

    async def get_setting(self, path: str) -> dict[str, Any]:
        return await self._json("GET", f"/settings/{path}")

    async def set_setting(self, path: str, key: str, value: Any) -> dict[str, Any]:
        result = await self._json("PATCH", f"/settings/{path}", {key: value})
        if isinstance(result, dict) and result.get("status") not in (None, "accepted"):
            raise AgentError(f"setting {path} not accepted: {result}")
        return result

    async def action(self, name: str, body: dict[str, Any] | None = None) -> Any:
        return await self._json("POST", f"/actions/{name}", body or {})

    async def snapshot(self, stream_id: int = 0) -> bytes:
        resp = await self._request("POST", f"/actions/snapshot?stream_id={stream_id}",
                                   body={}, timeout=SNAPSHOT_TIMEOUT)
        try:
            data = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AgentConnectionError(f"snapshot: {err}") from err
        if not data.startswith(b"\xff\xd8"):
            raise AgentError(f"snapshot is not a JPEG ({len(data)} bytes)")
        return data

    async def wait_for_hello(self, timeout: float) -> bool:
        """Whether the event stream opens with the agent's "hello" event within timeout seconds.

        Through an unfixed web UI proxy the stream "succeeds" (200) but carries a JSON error, no events."""
        stream = self.events()
        try:
            async with asyncio.timeout(timeout):
                async for event in stream:
                    return event.type == "hello"
        except (TimeoutError, AgentError):
            return False
        finally:
            await stream.aclose()
        return False

    async def events(self) -> AsyncIterator[AgentEvent]:
        """Server-sent events until the connection drops (raises AgentConnectionError then)."""
        resp = await self._request("GET", "/events",
                                   timeout=aiohttp.ClientTimeout(total=None, sock_read=180))
        event_type, data_lines = "message", []
        try:
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        payload = "\n".join(data_lines)
                        try:
                            data = json.loads(payload)
                        except ValueError:
                            data = {"raw": payload}
                        yield AgentEvent(event_type, data if isinstance(data, dict) else {"value": data})
                    event_type, data_lines = "message", []
                elif line.startswith(":"):
                    continue
                elif line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
        except (aiohttp.ClientError, TimeoutError, asyncio.IncompleteReadError) as err:
            raise AgentConnectionError(f"event stream: {err}") from err
        finally:
            resp.release()
        raise AgentConnectionError("event stream closed")
