"""Fake RTSP server with an audio backchannel, shaped like Raptor's rsd or prudynt.

- raptor: ONVIF flow only; PLAY is refused with 455 unless a track it sends was set up; Digest without qop.
- prudynt: ONVIF flow with or without a receive track, and ANNOUNCE/RECORD; Digest with qop="auth".
- prudynt_announce: like prudynt, but PLAY of the backchannel is refused, so only ANNOUNCE/RECORD works.

Records every request and the RTP packets received on the backchannel channel.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import ssl
import struct
import time
from dataclasses import dataclass, field

USER, PASSWORD, REALM, NONCE = "thingino", "s3cret", "fake", "abc123"

# Raptor's DESCRIBE, built as rsd_session.c builds it (H.264 main stream, L16 mic, every backchannel codec)
SDP_RAPTOR = (
    "v=0\r\no=- 1760000000000000 1 IN IP4 192.0.2.10\r\ns=Thingino\r\nt=0 0\r\na=tool:Raptor RSS\r\n"
    "a=type:broadcast\r\na=control:*\r\na=range:npt=now-\r\n"
    "m=video 0 RTP/AVP 96\r\nc=IN IP4 0.0.0.0\r\nb=AS:1024\r\na=rtpmap:96 H264/90000\r\n"
    "a=fmtp:96 packetization-mode=1;profile-level-id=64001F;sprop-parameter-sets=Z2QAH6zZQFAFuhAAAAMAEAAAAwPI8YMZYA==,aOvjyyLA\r\n"
    "a=control:video\r\na=framerate:25\r\n"
    "m=audio 0 RTP/AVP 98\r\nc=IN IP4 0.0.0.0\r\nb=AS:256\r\na=rtpmap:98 L16/16000/1\r\na=control:audio\r\n"
)
SDP_RAPTOR_BACKCHANNEL = (
    "m=audio 0 RTP/AVP 0 8 112 113 114\r\na=control:backchannel\r\na=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\na=rtpmap:112 opus/48000/2\r\na=fmtp:112 maxplaybackrate=16000;stereo=0\r\n"
    "a=rtpmap:113 MPEG4-GENERIC/16000/1\r\n"
    "a=fmtp:113 streamtype=5; profile-level-id=1; mode=AAC-hbr; config=1408; sizeLength=13; "
    "indexLength=3; indexDeltaLength=3\r\na=rtpmap:114 L16/16000/1\r\na=sendonly\r\n"
)
# prudynt's shape (SdpGenerator.cpp): track ids, PCMU and PCMA only, no audio receive track here
SDP_PRUDYNT = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=prudynt\r\nt=0 0\r\na=control:*\r\n"
    "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:track1\r\n"
)
SDP_PRUDYNT_BACKCHANNEL = (
    "m=audio 0 RTP/AVP 0 8\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\na=control:track0\r\na=sendonly\r\n"
)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str]
    body: str


@dataclass
class FakeRtspServer:
    mode: str = "raptor"
    auth: str = "digest"            # digest | digest_qop | basic | none
    backchannel: bool = True        # offered in DESCRIBE
    mic: bool = True                # Raptor offers an audio track it sends
    password: str = PASSWORD
    describe_status: int = 200
    requests: list[Request] = field(default_factory=list)
    rtp: list[tuple[float, tuple[int, int, int, int, int], bytes]] = field(default_factory=list)
    torn_down: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    port: int = 0
    _server: asyncio.Server | None = None

    async def start(self, tls: ssl.SSLContext | None = None) -> FakeRtspServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=tls)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    @property
    def methods(self) -> list[str]:
        """Authenticated requests, in order."""
        return [r.method for r in self.requests if "authorization" in r.headers or self.auth == "none"]

    def _check_auth(self, method: str, url: str, header: str | None) -> bool:
        if self.auth == "none":
            return True
        if not header:
            return False
        if self.auth == "basic":
            return header == "Basic " + base64.b64encode(f"{USER}:{self.password}".encode()).decode()
        p = {k: a or b for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', header[7:])}
        ha1, ha2 = _md5(f"{USER}:{REALM}:{self.password}"), _md5(f"{method}:{p.get('uri')}")
        if self.auth == "digest_qop":
            expected = _md5(f"{ha1}:{NONCE}:{p.get('nc')}:{p.get('cnonce')}:auth:{ha2}")
        else:
            expected = _md5(f"{ha1}:{NONCE}:{ha2}")
        return p.get("response") == expected and p.get("uri") == url and p.get("username") == USER

    def _challenge(self) -> str:
        if self.auth == "basic":
            return f'Basic realm="{REALM}"'
        qop = ', qop="auth"' if self.auth == "digest_qop" else ""
        return f'Digest realm="{REALM}", nonce="{NONCE}"{qop}, algorithm=MD5'

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter):
        state = {"session": None, "receive": False, "backchannel": False, "playing": False}
        try:
            while True:
                first = await r.readexactly(1)
                if first == b"$":
                    ch, n = struct.unpack("!BH", await r.readexactly(3))
                    data = await r.readexactly(n)
                    if ch == 2 and state["playing"]:
                        self.rtp.append((time.monotonic(), struct.unpack("!BBHII", data[:12]), data[12:]))
                    continue
                head = (first + await r.readuntil(b"\r\n\r\n")).decode()
                lines = head.split("\r\n")
                method, url, _ = lines[0].split(" ")
                h = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
                body = (await r.readexactly(int(h["content-length"]))).decode() if "content-length" in h else ""
                self.requests.append(Request(method, url, h, body))

                def reply(code: str, extra: str = "", payload: str = ""):
                    msg = f"RTSP/1.0 {code}\r\nCSeq: {h['cseq']}\r\n{extra}"
                    if payload:
                        msg += f"Content-Type: application/sdp\r\nContent-Length: {len(payload)}\r\n"
                    w.write((msg + "\r\n" + payload).encode())

                if not self._check_auth(method, url, h.get("authorization")):
                    reply("401 Unauthorized", f"WWW-Authenticate: {self._challenge()}\r\n")
                elif method == "OPTIONS":
                    reply("200 OK", "Public: OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER\r\n")
                elif method == "DESCRIBE":
                    if self.describe_status != 200:
                        reply(f"{self.describe_status} Error")
                    else:
                        raptor = self.mode == "raptor"
                        sdp = SDP_RAPTOR if raptor else SDP_PRUDYNT
                        if not self.mic:
                            sdp = sdp.split("m=audio")[0]
                        if self.backchannel and "www.onvif.org/ver20/backchannel" in h.get("require", ""):
                            sdp += SDP_RAPTOR_BACKCHANNEL if raptor else SDP_PRUDYNT_BACKCHANNEL
                        reply("200 OK", f"Content-Base: {url}/\r\n", sdp)
                elif method == "ANNOUNCE" and self.mode != "raptor":
                    state["session"] = "ABCD1234"
                    reply("200 OK", "Session: ABCD1234\r\n")
                elif method == "SETUP":
                    transport = h["transport"]
                    state["session"] = state["session"] or "ABCD1234"
                    if url.endswith(("/backchannel", "/track0")):
                        state["backchannel"] = True
                    else:
                        state["receive"] = True
                    reply("200 OK", f"Transport: {transport}\r\nSession: {state['session']};timeout=60\r\n")
                elif method == "PLAY":
                    if (self.mode == "raptor" and not state["receive"]) or self.mode == "prudynt_announce":
                        reply("455 Method Not Valid In This State")
                    else:
                        state["playing"] = True
                        reply("200 OK", f"Session: {state['session']}\r\nRange: npt=0.000-\r\n")
                elif method == "RECORD" and self.mode != "raptor":
                    state["playing"] = True
                    reply("200 OK", f"Session: {state['session']}\r\n")
                elif method == "GET_PARAMETER":
                    reply("200 OK", f"Session: {state['session']}\r\n")
                elif method == "TEARDOWN":
                    reply("200 OK")
                    await w.drain()
                    self.torn_down.set()
                    break
                else:
                    reply("501 Not Implemented")
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()
            self.closed.set()
