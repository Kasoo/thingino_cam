"""Constants for the Thingino camera integration."""
from __future__ import annotations

from datetime import date

DOMAIN = "thingino_cam"

CONF_MODE = "mode"
CONF_TOKEN = "token"
CONF_API_KEY = "api_key"
CONF_CLIENT_CERT = "client_cert"
CONF_CLIENT_KEY = "client_key"
# PEM of the camera's own (self-signed) certificate, pinned at setup; absent = publicly trusted cert
CONF_SERVER_CERT = "server_cert"
CONF_RTSP_USERNAME = "rtsp_username"
CONF_RTSP_PASSWORD = "rtsp_password"
CONF_RTSP_PORT = "rtsp_port"

# How HA reaches the agent API. Direct: agentd's own TLS listener (bearer token, optional mTLS client
# cert). Web UI: the web UI's agent proxy, /x/agent.cgi (the web UI's X-API-Key).
MODE_DIRECT = "direct"
MODE_WEBUI = "webui"

DEFAULT_PORT = 1998
DEFAULT_WEBUI_PORT = 443
DEFAULT_RTSP_PORT = 322
DEFAULT_RTSP_USERNAME = "thingino"
DEFAULT_RTSP_PASSWORD = "thingino"

# Web UI mode needs two Thingino fixes: uhttpd passing X-API-Key to CGI, and agent.cgi forwarding the
# event-stream path. Thingino only exposes a build date ("<branch>+<commit>, <date> <time> UTC"), so the
# minimum is a build date. This is upstream master e68b5e8, which does NOT have the fixes yet (TODO.md).
# Until they are released, the setup probes (key accepted in the header, a hello event) are the real gate.
MIN_WEBUI_BUILD_DATE = date(2026, 10, 7)
# how long setup waits for the agent's "hello" event through the web UI proxy
EVENT_PROBE_SECONDS = 15

# The agent runs shell adapters on a single-core MIPS SoC: /state costs 2-10 s of CPU, /config ~4 s,
# /health ~1 s. Push events (/events) carry motion, day/night and scene brightness, and announce
# other state changes, so the poll is only a safety net: /health + /config, one call at a time.
SCAN_INTERVAL_SECONDS = 15 * 60
EVENT_RETRY_SECONDS = 15
