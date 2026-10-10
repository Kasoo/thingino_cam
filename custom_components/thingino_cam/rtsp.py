"""Stream URLs from the agent's /state `streams[].rtsp_url` (no Home Assistant imports: unit-testable).

Only the scheme, port and path are taken from the agent; the host is always the one HA connects to.
Upstream adapters report hosts HA can't use: Raptor prints the camera's address without brackets
(`rtsps://2a02:…:cfd0:322/ch0`, invalid for IPv6) and prudynt always prints 127.0.0.1. Both always
print a port, so in an unbracketed IPv6 literal the last `:<digits>` group is taken as the port.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322}

_URL = re.compile(r"^(?P<scheme>rtsps?)://(?:[^@/]*@)?(?P<hostport>[^/?#]*)(?P<path>/[^?#]*)?", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StreamTarget:
    scheme: str
    port: int
    path: str           # starts with "/"


def parse_rtsp_url(url: str | None) -> StreamTarget | None:
    """Scheme, port and path of an agent-reported RTSP URL; None if it isn't one."""
    m = _URL.match((url or "").strip())
    if not m:
        return None
    scheme = m["scheme"].lower()
    hostport = m["hostport"]
    port: int | None = None
    if hostport.startswith("["):                    # [v6] or [v6]:port
        tail = hostport.partition("]")[2]
        if tail.startswith(":") and tail[1:].isdigit():
            port = int(tail[1:])
    else:                                            # host, host:port, or a bare v6 literal + port
        head, sep, last = hostport.rpartition(":")
        if sep and head and last.isdigit():
            port = int(last)
    if port is not None and not 0 < port < 65536:
        port = None
    path = m["path"] or ""
    if not path.strip("/"):
        return None                                  # no stream name: unusable
    return StreamTarget(scheme, port or DEFAULT_PORTS[scheme], path)


def url_host(host: str) -> str:
    """A host for use in a URL: bare IPv6 literals need brackets."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def stream_url(target: StreamTarget, host: str, username: str, password: str) -> str:
    cred = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username else ""
    return f"{target.scheme}://{cred}{url_host(host)}:{target.port}{target.path}"
