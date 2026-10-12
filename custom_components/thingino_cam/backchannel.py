"""Play audio on a camera's speaker over its RTSP audio backchannel (ONVIF Profile T).

No Home Assistant imports: unit-testable. One `play()` call runs one RTSP session over one TCP (or TLS)
connection, with RTP interleaved on the same connection:

- ONVIF flow (Raptor's rsd, and prudynt): DESCRIBE with the backchannel Require tag, SETUP a track the
  camera sends (Raptor refuses PLAY with 455 without one), SETUP the backchannel, PLAY, send RTP.
- ANNOUNCE flow (older prudynt): ANNOUNCE our SDP, SETUP the backchannel, RECORD, send RTP. Only tried
  after the ONVIF flow is refused, on a fresh connection.

Raptor stamps each packet on arrival and ignores the RTP timestamp, so packets are paced in real time.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import logging
import random
import re
import secrets
import ssl
import struct
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

_LOG = logging.getLogger(__name__)

ONVIF_TAG = "www.onvif.org/ver20/backchannel"
USER_AGENT = "thingino_cam"

REQUEST_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
TEARDOWN_TIMEOUT = 2.0
# the camera's session timeout is 60 s; refresh it well inside that
KEEPALIVE_SECONDS = 30.0
PACKET_SECONDS = 0.02
# time for the last packets to reach the speaker before TEARDOWN
DRAIN_SECONDS = 0.3

FLOW_ONVIF = "onvif"
FLOW_ANNOUNCE = "announce"

# channels for interleaved RTP/RTCP: the receive track, then the backchannel
RECEIVE_CHANNELS = (0, 1)
BACKCHANNEL_CHANNELS = (2, 3)

STATIC_PAYLOAD_TYPES = {0: ("PCMU", 8000), 8: ("PCMA", 8000)}


class BackchannelError(Exception):
    """Any failure playing audio on the camera."""


class BackchannelUnavailable(BackchannelError):
    """The camera offers no audio backchannel (speaker or backchannel off, or not in the firmware)."""


class BackchannelAuthError(BackchannelError):
    """RTSP login rejected."""


class BackchannelTLSError(BackchannelError):
    """The camera's RTSPS certificate isn't trusted."""


class BackchannelRefused(BackchannelError):
    """The camera answered a request with an error status."""

    def __init__(self, method: str, status: int, reason: str):
        super().__init__(f"{method} refused: {status} {reason}".rstrip())
        self.method, self.status = method, status


@dataclass(frozen=True)
class BackchannelTarget:
    host: str
    port: int
    path: str                           # starts with "/"
    username: str
    password: str
    tls: ssl.SSLContext | None = None   # None: plain rtsp
    # tried when `tls` fails to verify the certificate (the camera's own pinned cert, no name check)
    pinned_tls: ssl.SSLContext | None = None

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"rtsp{'s' if self.tls else ''}://{host}:{self.port}{self.path}"


# --- auth -------------------------------------------------------------------------------------
class Auth:
    """Basic, or Digest MD5 with or without qop=auth, from a WWW-Authenticate challenge."""

    def __init__(self, username: str, password: str, challenge: str):
        scheme, _, rest = challenge.strip().partition(" ")
        self.scheme = scheme.lower()
        if self.scheme not in ("basic", "digest"):
            raise BackchannelAuthError(f"unsupported RTSP auth scheme {scheme!r}")
        self.username, self.password = username, password
        params = {k.lower(): a or b for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', rest)}
        self.realm, self.nonce, self.opaque = params.get("realm", ""), params.get("nonce", ""), params.get("opaque")
        self.qop = "auth" if "auth" in (q.strip() for q in params.get("qop", "").split(",")) else None
        algorithm = params.get("algorithm", "MD5")
        if self.scheme == "digest" and algorithm.upper() != "MD5":
            raise BackchannelAuthError(f"unsupported RTSP digest algorithm {algorithm!r}")
        self.nc = 0

    def header(self, method: str, uri: str, cnonce: str | None = None) -> str:
        if self.scheme == "basic":
            return "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        ha1 = _md5(f"{self.username}:{self.realm}:{self.password}")
        ha2 = _md5(f"{method}:{uri}")
        parts = [f'username="{self.username}"', f'realm="{self.realm}"', f'nonce="{self.nonce}"', f'uri="{uri}"']
        if self.qop:
            self.nc += 1
            nc, cnonce = f"{self.nc:08x}", cnonce or secrets.token_hex(8)
            response = _md5(f"{ha1}:{self.nonce}:{nc}:{cnonce}:auth:{ha2}")
            parts += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
        else:
            response = _md5(f"{ha1}:{self.nonce}:{ha2}")
        parts.append(f'response="{response}"')
        if self.opaque:
            parts.append(f'opaque="{self.opaque}"')
        return "Digest " + ", ".join(parts)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# --- RTSP connection --------------------------------------------------------------------------
@dataclass
class Response:
    status: int
    reason: str
    headers: dict[str, str]     # lower-case names
    body: str


class RtspConnection:
    """RTSP requests and interleaved RTP on one connection. A reader task demultiplexes the stream:
    `$` frames (the camera's RTP/RTCP) are counted and dropped, the rest are responses matched by CSeq."""

    def __init__(self, username: str, password: str):
        self.username, self.password = username, password
        self.cseq = 0
        self.session: str | None = None
        self.auth: Auth | None = None
        self.received = 0           # interleaved frames from the camera
        self._pending: dict[int, asyncio.Future[Response]] = {}
        self._wlock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    async def connect(self, host: str, port: int, tls: ssl.SSLContext | None):
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host.strip("[]"), port, ssl=tls,
                                    server_hostname=host.strip("[]") if tls else None),
            CONNECT_TIMEOUT)
        self._task = asyncio.create_task(self._read_loop())

    async def close(self):
        if self._task:
            self._task.cancel()
        if self._writer:
            self._writer.close()
            try:
                await asyncio.wait_for(self._writer.wait_closed(), TEARDOWN_TIMEOUT)
            except (OSError, TimeoutError, ssl.SSLError):
                pass

    async def _read_loop(self):
        assert self._reader
        reader = self._reader
        try:
            while True:
                first = await reader.readexactly(1)
                if first == b"$":
                    _channel, size = struct.unpack("!BH", await reader.readexactly(3))
                    await reader.readexactly(size)
                    self.received += 1
                    continue
                head = (first + await reader.readuntil(b"\r\n\r\n")).decode(errors="replace")
                lines = head.split("\r\n")
                _version, status, reason = (lines[0].split(" ", 2) + ["", ""])[:3]
                headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
                body = ""
                if size := int(headers.get("content-length") or 0):
                    body = (await reader.readexactly(size)).decode(errors="replace")
                fut = self._pending.pop(int(headers.get("cseq") or -1), None)
                if fut and not fut.done():
                    fut.set_result(Response(int(status) if status.isdigit() else 0, reason, headers, body))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError,
                ssl.SSLError, ValueError) as err:
            self._fail(BackchannelError(f"connection lost: {err!r}"))

    def _fail(self, err: BackchannelError):
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    async def request(self, method: str, url: str, headers: dict[str, str] | None = None, body: str = "",
                      timeout: float = REQUEST_TIMEOUT) -> Response:
        """Send a request (authenticating after a 401) and return the response, whatever its status."""
        for _attempt in (1, 2):
            resp = await self._send(method, url, headers, body, timeout)
            if resp.status == 401 and self.auth is None and "www-authenticate" in resp.headers:
                self.auth = Auth(self.username, self.password, resp.headers["www-authenticate"])
                continue
            break
        if resp.status == 401:
            raise BackchannelAuthError("RTSP login rejected")
        if "session" in resp.headers:
            self.session = resp.headers["session"].split(";")[0].strip()
        return resp

    async def _send(self, method: str, url: str, headers: dict[str, str] | None, body: str,
                    timeout: float) -> Response:
        if self._writer is None or self._task is None or self._task.done():
            raise BackchannelError("not connected")
        self.cseq += 1
        h = {"CSeq": str(self.cseq), "User-Agent": USER_AGENT, **(headers or {})}
        if self.session:
            h["Session"] = self.session
        if self.auth:
            h["Authorization"] = self.auth.header(method, url)
        if body:
            h["Content-Length"] = str(len(body.encode()))
        msg = f"{method} {url} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n" + body
        fut: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[self.cseq] = fut
        try:
            async with self._wlock:
                self._writer.write(msg.encode())
                await self._writer.drain()
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError as err:
            raise BackchannelError(f"{method}: no answer from the camera in {timeout:.0f} s") from err
        except (ConnectionError, OSError) as err:
            raise BackchannelError(f"{method}: connection lost: {err!r}") from err
        finally:
            self._pending.pop(self.cseq, None)

    async def expect_ok(self, method: str, url: str, headers: dict[str, str] | None = None,
                        body: str = "") -> Response:
        resp = await self.request(method, url, headers, body)
        if not 200 <= resp.status < 300:
            raise BackchannelRefused(method, resp.status, resp.reason)
        return resp

    async def send_frame(self, channel: int, packet: bytes):
        assert self._writer
        try:
            async with self._wlock:
                self._writer.write(b"$" + struct.pack("!BH", channel, len(packet)) + packet)
                await self._writer.drain()
        except (ConnectionError, OSError) as err:
            raise BackchannelError(f"connection lost while sending audio: {err!r}") from err


# --- SDP --------------------------------------------------------------------------------------
@dataclass
class Media:
    kind: str                   # "audio", "video", ...
    formats: list[int]
    control: str = ""
    direction: str = ""         # sendonly / recvonly / sendrecv / inactive, "" if not given
    rtpmap: dict[int, tuple[str, int]] = field(default_factory=dict)    # pt -> (upper-case name, rate)


def parse_sdp(sdp: str) -> list[Media]:
    """Media sections of an SDP body. Session-level attributes are ignored."""
    media: list[Media] = []
    for line in sdp.replace("\r", "").split("\n"):
        if line.startswith("m="):
            fields = line[2:].split()
            if len(fields) >= 3:
                media.append(Media(fields[0], [int(f) for f in fields[3:] if f.isdigit()]))
        elif media and line.startswith("a="):
            attr, _, value = line[2:].partition(":")
            m = media[-1]
            if attr == "control":
                m.control = value.strip()
            elif attr in ("sendonly", "recvonly", "sendrecv", "inactive"):
                m.direction = attr
            elif attr == "rtpmap":
                pt, _, encoding = value.strip().partition(" ")
                name, _, rest = encoding.partition("/")
                rate = rest.partition("/")[0]
                if pt.isdigit() and rate.isdigit():
                    m.rtpmap[int(pt)] = (name.upper(), int(rate))
    return media


def find_backchannel(media: Sequence[Media]) -> Media:
    """The audio section the camera receives on (a=sendonly from its point of view: we send)."""
    for m in media:
        if m.kind == "audio" and m.direction == "sendonly":
            return m
    raise BackchannelUnavailable("this camera offers no audio backchannel")


def resolve_control(base: str, control: str) -> str:
    """Absolute URL for a track's a=control: absolute ones as is, relative ones appended to the base."""
    if control.lower().startswith(("rtsp://", "rtsps://")):
        return control
    if control in ("", "*"):
        return base.rstrip("/")
    return base.rstrip("/") + "/" + control.lstrip("/")


@dataclass(frozen=True)
class Codec:
    pt: int
    name: str           # "L16" or "PCMU"
    rate: int

    @property
    def samples_per_packet(self) -> int:
        return int(self.rate * PACKET_SECONDS)


def pick_codec(backchannel: Media) -> Codec:
    """L16 at 16 kHz if offered (no companding), else PCMU at 8 kHz."""
    offered = {pt: backchannel.rtpmap.get(pt) or STATIC_PAYLOAD_TYPES.get(pt) for pt in backchannel.formats}
    for name, rate in (("L16", 16000), ("PCMU", 8000)):
        for pt, codec in offered.items():
            if codec == (name, rate):
                return Codec(pt, name, rate)
    names = ", ".join(sorted({f"{c[0]}/{c[1]}" for c in offered.values() if c})) or "nothing"
    raise BackchannelUnavailable(f"the camera's backchannel offers no supported codec (offers {names})")


def flows_to_try(public: str | None) -> tuple[str, ...]:
    """Order of flows to try. ONVIF first: it works on Raptor and on prudynt, whose OPTIONS doesn't list
    ANNOUNCE although it accepts it. ANNOUNCE first only for a server that says it supports it."""
    methods = {m.strip().upper() for m in (public or "").split(",")}
    if {"ANNOUNCE", "RECORD"} <= methods:
        return (FLOW_ANNOUNCE, FLOW_ONVIF)
    return (FLOW_ONVIF, FLOW_ANNOUNCE)


# --- audio packets ----------------------------------------------------------------------------
def lin2ulaw(sample: int) -> int:
    """G.711 µ-law byte for a signed 16-bit sample."""
    sign = 0x80 if sample < 0 else 0
    magnitude = min(-sample if sample < 0 else sample, 32635) + 0x84
    exponent = (magnitude >> 7).bit_length() - 1
    return ~(sign | exponent << 4 | (magnitude >> (exponent + 3)) & 0x0F) & 0xFF


@functools.cache
def _ulaw_table() -> bytes:
    """µ-law byte for every 16-bit sample, indexed by its unsigned value."""
    return bytes(lin2ulaw(s - 65536 if s >= 32768 else s) for s in range(65536))


def pcm_to_ulaw(pcm: bytes) -> bytes:
    """µ-law bytes for native-endian signed 16-bit PCM."""
    table = _ulaw_table()
    return bytes(table[s] for s in memoryview(pcm).cast("H"))


def payloads(pcm: bytes, codec: Codec) -> Iterator[bytes]:
    """20 ms payloads from native-endian signed 16-bit mono PCM at the codec's rate. The last packet
    is padded with silence."""
    step = codec.samples_per_packet * 2
    pcm = pcm[: len(pcm) // 2 * 2]
    for start in range(0, len(pcm), step):
        chunk = pcm[start:start + step].ljust(step, b"\0")
        if codec.name == "L16":
            samples = memoryview(chunk).cast("h")
            yield struct.pack(f"!{len(samples)}h", *samples)
        else:
            yield pcm_to_ulaw(chunk)


def rtp_packets(chunks: Iterator[bytes], codec: Codec, *, seq: int | None = None, ts: int | None = None,
                ssrc: int | None = None) -> Iterator[bytes]:
    """RTP packets: marker on the first, sequence +1 and timestamp +samples per packet."""
    seq = random.getrandbits(16) if seq is None else seq
    ts = random.getrandbits(32) if ts is None else ts
    ssrc = random.getrandbits(32) if ssrc is None else ssrc
    for i, payload in enumerate(chunks):
        marker = 0x80 if i == 0 else 0
        yield struct.pack("!BBHII", 0x80, marker | codec.pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc) + payload
        seq += 1
        ts += codec.samples_per_packet


# --- playback ---------------------------------------------------------------------------------
async def play(target: BackchannelTarget, pcm16k: bytes, pcm8k: bytes, *, stop: asyncio.Event | None = None,
               allow_video: bool = False, keepalive: float = KEEPALIVE_SECONDS):
    """Play native-endian signed 16-bit mono PCM at 16 kHz on the camera's speaker, to the end or until
    `stop` is set. `pcm8k` is the same audio at 8 kHz, for cameras that only take PCMU.

    Always tears the session down, also on error or cancellation."""
    tls = target.tls
    flows: tuple[str, ...] | None = None
    first_error: BackchannelRefused | None = None
    attempt = 0
    while True:
        conn = RtspConnection(target.username, target.password)
        try:
            try:
                await conn.connect(target.host, target.port, tls)
            except ssl.SSLCertVerificationError as err:
                if tls is not target.pinned_tls and target.pinned_tls is not None:
                    tls = target.pinned_tls
                    continue
                raise BackchannelTLSError(
                    f"the camera's RTSPS certificate isn't trusted ({err.verify_message})") from err
            except (OSError, TimeoutError) as err:
                raise BackchannelError(f"can't connect to {target.host} port {target.port}: {err!r}") from err
            if flows is None:
                # OPTIONS also gets the auth challenge out of the way; any answer will do
                resp = await conn.request("OPTIONS", target.url)
                flows = flows_to_try(resp.headers.get("public"))
            try:
                await _run_flow(conn, flows[attempt], target, pcm16k, pcm8k, stop, allow_video, keepalive)
                return
            except BackchannelRefused as err:
                first_error = first_error or err
                attempt += 1
                if attempt >= len(flows) or err.method == "DESCRIBE":
                    raise _explain(first_error) from err
                _LOG.debug("%s: %s; trying the %s flow", target.host, err, flows[attempt])
        finally:
            await _teardown(conn, target.url)


def _explain(err: BackchannelRefused) -> BackchannelError:
    if err.status == 455 and err.method == "PLAY":
        return BackchannelRefused(err.method, err.status,
                                  "the camera needs a stream to send back during playback; turn its microphone on")
    return err


async def _teardown(conn: RtspConnection, url: str):
    try:
        if conn.session:
            await conn.request("TEARDOWN", url, timeout=TEARDOWN_TIMEOUT)
    except (BackchannelError, asyncio.CancelledError):
        pass
    finally:
        await conn.close()


async def _run_flow(conn: RtspConnection, flow: str, target: BackchannelTarget, pcm16k: bytes,
                    pcm8k: bytes, stop: asyncio.Event | None, allow_video: bool, keepalive: float):
    url = target.url
    resp = await conn.expect_ok("DESCRIBE", url, {"Accept": "application/sdp", "Require": ONVIF_TAG})
    base = resp.headers.get("content-base") or url
    media = parse_sdp(resp.body)
    backchannel = find_backchannel(media)
    codec = pick_codec(backchannel)
    backchannel_url = resolve_control(base, backchannel.control)
    transport = "RTP/AVP/TCP;unicast;interleaved={}-{}"

    if flow == FLOW_ANNOUNCE:
        sdp = (f"v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=thingino_cam\r\nt=0 0\r\n"
               f"m=audio 0 RTP/AVP {codec.pt}\r\na=rtpmap:{codec.pt} {codec.name}/{codec.rate}\r\n"
               f"a=control:{backchannel.control}\r\n")
        await conn.expect_ok("ANNOUNCE", url, {"Content-Type": "application/sdp"}, sdp)
        await conn.expect_ok("SETUP", backchannel_url, {"Transport": transport.format(*BACKCHANNEL_CHANNELS)})
        await conn.expect_ok("RECORD", url, {"Range": "npt=0.000-"})
    else:
        # Raptor refuses PLAY unless a track it sends is set up too: audio is cheap, video is not
        others = [m for m in media if m is not backchannel]
        receive = next((m for m in others if m.kind == "audio"), None)
        if receive is None and allow_video:
            receive = next(iter(others), None)
        if receive is not None:
            await conn.expect_ok("SETUP", resolve_control(base, receive.control),
                                 {"Transport": transport.format(*RECEIVE_CHANNELS)})
        await conn.expect_ok("SETUP", backchannel_url,
                             {"Transport": transport.format(*BACKCHANNEL_CHANNELS), "Require": ONVIF_TAG})
        await conn.expect_ok("PLAY", url, {"Range": "npt=0.000-", "Require": ONVIF_TAG})

    pcm = pcm8k if codec.rate == 8000 else pcm16k
    await _send_paced(conn, url, rtp_packets(payloads(pcm, codec), codec), stop, keepalive)


async def _send_paced(conn: RtspConnection, url: str, packets: Iterator[bytes], stop: asyncio.Event | None,
                      keepalive: float):
    """Send packet i at t0 + (i + 1) * 20 ms on the monotonic clock: the camera plays them as they come."""
    t0 = last_keepalive = time.monotonic()
    pending: asyncio.Task | None = None
    sent = 0
    try:
        for i, packet in enumerate(packets):
            if stop is not None and stop.is_set():
                break
            await conn.send_frame(BACKCHANNEL_CHANNELS[0], packet)
            sent += 1
            now = time.monotonic()
            if now - last_keepalive >= keepalive and (pending is None or pending.done()):
                last_keepalive = now
                pending = asyncio.create_task(conn.request("GET_PARAMETER", url))
            await asyncio.sleep(max(0.0, t0 + (i + 1) * PACKET_SECONDS - time.monotonic()))
        if sent and not (stop is not None and stop.is_set()):
            await asyncio.sleep(DRAIN_SECONDS)
    finally:
        if pending is not None:
            if pending.done():
                if not pending.cancelled() and (err := pending.exception()):
                    _LOG.debug("keepalive failed: %s", err)
            else:
                pending.cancel()
