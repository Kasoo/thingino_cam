"""The RTSP backchannel client: pure helpers, then whole sessions against the fake server."""
import asyncio
import ssl
import struct

import pytest

from custom_components.thingino_cam import backchannel as bc
from fake_rtsp import (
    PASSWORD, SDP_PRUDYNT, SDP_PRUDYNT_BACKCHANNEL, SDP_RAPTOR, SDP_RAPTOR_BACKCHANNEL, USER, FakeRtspServer,
)


@pytest.fixture(autouse=True)
def sockets_on(socket_enabled):
    """The fake server listens on a loopback port."""
    yield


# --- auth -------------------------------------------------------------------------------------
def test_digest_rfc2069_without_qop():
    auth = bc.Auth("Mufasa", "CircleOfLife",
                   'Digest realm="testrealm@host.com", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093"')
    assert auth.qop is None
    header = auth.header("GET", "/dir/index.html")
    assert 'response="1949323746fe6a43ef61f9606e7febea"' in header
    assert "qop" not in header and "nc=" not in header


def test_digest_rfc2617_with_qop():
    auth = bc.Auth("Mufasa", "Circle Of Life",
                   'Digest realm="testrealm@host.com", qop="auth,auth-int", '
                   'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    header = auth.header("GET", "/dir/index.html", cnonce="0a4f113b")
    assert 'response="6629fae49393a05397450978507c4ef1"' in header
    assert "qop=auth" in header and "nc=00000001" in header
    assert 'opaque="5ccc069c403ebaf9f0171e9517f40e41"' in header
    assert "nc=00000002" in auth.header("GET", "/dir/index.html")


def test_basic():
    auth = bc.Auth("Aladdin", "open sesame", 'Basic realm="x"')
    assert auth.header("DESCRIBE", "rtsp://h/p") == "Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ=="


@pytest.mark.parametrize("challenge", ['Digest realm="x", nonce="n", algorithm=SHA-256', 'Bearer realm="x"'])
def test_unsupported_auth(challenge):
    with pytest.raises(bc.BackchannelAuthError):
        bc.Auth("u", "p", challenge)


# --- SDP and codec ----------------------------------------------------------------------------
def test_parse_raptor_sdp():
    media = bc.parse_sdp(SDP_RAPTOR + SDP_RAPTOR_BACKCHANNEL)
    assert [(m.kind, m.control, m.direction) for m in media] == [
        ("video", "video", ""), ("audio", "audio", ""), ("audio", "backchannel", "sendonly")]
    back = bc.find_backchannel(media)
    assert back.formats == [0, 8, 112, 113, 114]
    assert back.rtpmap[114] == ("L16", 16000)
    assert back.rtpmap[112] == ("OPUS", 48000)
    assert bc.pick_codec(back) == bc.Codec(114, "L16", 16000)


def test_parse_prudynt_sdp():
    back = bc.find_backchannel(bc.parse_sdp(SDP_PRUDYNT + SDP_PRUDYNT_BACKCHANNEL))
    assert back.control == "track0"
    assert bc.pick_codec(back) == bc.Codec(0, "PCMU", 8000)


def test_pcmu_without_rtpmap():
    assert bc.pick_codec(bc.Media("audio", [8, 0], direction="sendonly")) == bc.Codec(0, "PCMU", 8000)


def test_no_supported_codec():
    media = bc.Media("audio", [8, 112], rtpmap={112: ("OPUS", 48000)}, direction="sendonly")
    with pytest.raises(bc.BackchannelUnavailable, match="OPUS/48000"):
        bc.pick_codec(media)


def test_no_backchannel():
    with pytest.raises(bc.BackchannelUnavailable):
        bc.find_backchannel(bc.parse_sdp(SDP_RAPTOR))


@pytest.mark.parametrize(("base", "control", "expected"), [
    ("rtsp://h:554/ch0/", "backchannel", "rtsp://h:554/ch0/backchannel"),
    ("rtsp://h:554/ch0", "track0", "rtsp://h:554/ch0/track0"),
    ("rtsp://h:554/ch0/", "/track0", "rtsp://h:554/ch0/track0"),
    ("rtsp://h:554/ch0/", "rtsp://other/x", "rtsp://other/x"),
    ("rtsps://[2001:db8::1]:322/ch0/", "*", "rtsps://[2001:db8::1]:322/ch0"),
])
def test_resolve_control(base, control, expected):
    assert bc.resolve_control(base, control) == expected


@pytest.mark.parametrize(("public", "first"), [
    ("OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER, SET_PARAMETER", bc.FLOW_ONVIF),
    (None, bc.FLOW_ONVIF),
    ("OPTIONS, DESCRIBE, SETUP, PLAY, RECORD, ANNOUNCE, TEARDOWN", bc.FLOW_ANNOUNCE),
])
def test_flows_to_try(public, first):
    flows = bc.flows_to_try(public)
    assert flows[0] == first and set(flows) == {bc.FLOW_ONVIF, bc.FLOW_ANNOUNCE}


def test_target_url():
    assert bc.BackchannelTarget("2001:db8::1", 322, "/ch0", "u", "p").url == "rtsp://[2001:db8::1]:322/ch0"


# --- packets ----------------------------------------------------------------------------------
@pytest.mark.parametrize(("sample", "byte"), [(0, 0xFF), (-1, 0x7F), (32767, 0x80), (-32768, 0x00), (100, 0xF2)])
def test_lin2ulaw(sample, byte):
    assert bc.lin2ulaw(sample) == byte
    assert bc.pcm_to_ulaw(struct.pack("=h", sample)) == bytes([byte])


def test_l16_packets():
    codec = bc.Codec(114, "L16", 16000)
    pcm = struct.pack("=500h", *range(500))          # 1.5625 packets
    packets = list(bc.rtp_packets(bc.payloads(pcm, codec), codec, seq=65535, ts=2**32 - 100, ssrc=7))
    assert len(packets) == 2
    headers = [struct.unpack("!BBHII", p[:12]) for p in packets]
    assert headers == [(0x80, 0x80 | 114, 65535, 2**32 - 100, 7), (0x80, 114, 0, 220, 7)]
    assert all(len(p) == 12 + 640 for p in packets)
    assert packets[0][12:16] == b"\x00\x00\x00\x01"   # big-endian
    assert packets[1][12 + 180 * 2:] == b"\0" * (140 * 2)    # padded with silence


def test_pcmu_packets():
    codec = bc.Codec(0, "PCMU", 8000)
    packets = list(bc.rtp_packets(bc.payloads(b"\0\0" * 320, codec), codec, seq=1, ts=0, ssrc=1))
    assert [struct.unpack("!BBHII", p[:12])[1:4] for p in packets] == [(0x80, 1, 0), (0, 2, 160)]
    assert all(p[12:] == b"\xff" * 160 for p in packets)


# --- whole sessions ---------------------------------------------------------------------------
def tone(seconds: float, rate: int) -> bytes:
    return struct.pack(f"={int(seconds * rate)}h", *([1000, -1000] * int(seconds * rate / 2)))


async def serve(**kw) -> FakeRtspServer:
    return await FakeRtspServer(**kw).start()


def target(server: FakeRtspServer, password: str = PASSWORD) -> bc.BackchannelTarget:
    return bc.BackchannelTarget("127.0.0.1", server.port, "/ch0", USER, password)


async def test_raptor_flow():
    server = await serve(mode="raptor")
    await bc.play(target(server), tone(0.5, 16000), tone(0.5, 8000))
    assert server.methods == ["OPTIONS", "DESCRIBE", "SETUP", "SETUP", "PLAY", "TEARDOWN"]
    describe, recv, back, play = server.requests[2], server.requests[3], server.requests[4], server.requests[5]
    assert describe.headers["require"] == bc.ONVIF_TAG and describe.headers["accept"] == "application/sdp"
    assert recv.url.endswith("/ch0/audio") and "interleaved=0-1" in recv.headers["transport"]
    assert back.url.endswith("/ch0/backchannel") and "interleaved=2-3" in back.headers["transport"]
    assert back.headers["require"] == bc.ONVIF_TAG and back.headers["session"] == "ABCD1234"
    assert play.headers["range"] == "npt=0.000-" and play.headers["session"] == "ABCD1234"
    rtp = server.rtp
    assert len(rtp) == 25
    assert {h[1] & 0x7F for _, h, _ in rtp} == {114}
    assert rtp[0][1][1] & 0x80 and not any(h[1] & 0x80 for _, h, _ in rtp[1:])
    seqs = [h[2] for _, h, _ in rtp]
    assert all((b - a) & 0xFFFF == 1 for a, b in zip(seqs, seqs[1:]))
    assert all(len(payload) == 640 for _, _, payload in rtp)
    gaps = [b[0] - a[0] for a, b in zip(rtp, rtp[1:])]
    assert 0.018 < sum(gaps) / len(gaps) < 0.025 and min(gaps) > 0.010
    await server.closed.wait()
    await server.stop()


async def test_prudynt_flow_pcmu_without_receive_track():
    server = await serve(mode="prudynt", auth="digest_qop")
    await bc.play(target(server), tone(0.2, 16000), tone(0.2, 8000))
    assert server.methods == ["OPTIONS", "DESCRIBE", "SETUP", "PLAY", "TEARDOWN"]
    assert server.requests[3].url.endswith("/ch0/track0")
    assert len(server.rtp) == 10 and {h[1] & 0x7F for _, h, _ in server.rtp} == {0}
    assert all(len(payload) == 160 for _, _, payload in server.rtp)
    await server.stop()


async def test_announce_fallback():
    server = await serve(mode="prudynt_announce", auth="basic")
    await bc.play(target(server), tone(0.1, 16000), tone(0.1, 8000))
    methods = server.methods
    assert methods[:5] == ["OPTIONS", "DESCRIBE", "SETUP", "PLAY", "TEARDOWN"]
    assert methods[5:] == ["DESCRIBE", "ANNOUNCE", "SETUP", "RECORD", "TEARDOWN"]
    announce = next(r for r in server.requests if r.method == "ANNOUNCE" and "authorization" in r.headers)
    assert "m=audio 0 RTP/AVP 0\r\n" in announce.body and "a=rtpmap:0 PCMU/8000" in announce.body
    assert "a=control:track0" in announce.body
    assert len(server.rtp) == 5
    await server.stop()


async def test_raptor_without_receive_track_explains():
    server = await serve(mode="raptor", mic=False)
    with pytest.raises(bc.BackchannelRefused, match="microphone"):
        await bc.play(target(server), tone(0.1, 16000), tone(0.1, 8000))
    assert "/ch0/video" not in [r.url for r in server.requests]      # no video pulled
    assert server.methods.count("TEARDOWN") == 1        # the ONVIF attempt; ANNOUNCE has no session
    await server.stop()


async def test_wrong_password():
    server = await serve()
    with pytest.raises(bc.BackchannelAuthError):
        await bc.play(target(server, "wrong"), tone(0.1, 16000), tone(0.1, 8000))
    await server.stop()


async def test_no_backchannel_offered():
    server = await serve(backchannel=False)
    with pytest.raises(bc.BackchannelUnavailable):
        await bc.play(target(server), tone(0.1, 16000), tone(0.1, 8000))
    assert "SETUP" not in server.methods
    await server.stop()


async def test_describe_refused():
    server = await serve(describe_status=404)
    with pytest.raises(bc.BackchannelRefused, match="DESCRIBE refused: 404"):
        await bc.play(target(server), tone(0.1, 16000), tone(0.1, 8000))
    assert server.methods.count("DESCRIBE") == 1
    await server.stop()


async def test_stop_mid_clip_tears_down():
    server = await serve()
    stop = asyncio.Event()
    task = asyncio.create_task(bc.play(target(server), tone(5, 16000), tone(5, 8000), stop=stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert server.torn_down.is_set()
    assert 5 < len(server.rtp) < 30
    await server.stop()


async def test_cancel_tears_down():
    server = await serve()
    task = asyncio.create_task(bc.play(target(server), tone(5, 16000), tone(5, 8000)))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(server.closed.wait(), 1)
    assert server.torn_down.is_set()
    await server.stop()


async def test_keepalive():
    server = await serve()
    await bc.play(target(server), tone(0.5, 16000), tone(0.5, 8000), keepalive=0.15)
    keepalives = [r for r in server.requests if r.method == "GET_PARAMETER"]
    assert len(keepalives) >= 2 and all(r.headers["session"] == "ABCD1234" for r in keepalives)
    await server.stop()


async def test_unreachable():
    server = await serve()
    port = server.port
    await server.stop()
    with pytest.raises(bc.BackchannelError, match="can't connect"):
        await bc.play(bc.BackchannelTarget("127.0.0.1", port, "/ch0", USER, PASSWORD), b"", b"")


@pytest.fixture
def self_signed(tmp_path):
    """(server context, PEM) for a self-signed cert named camera.local, as stock Thingino has."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "camera.local")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    (tmp_path / "cert.pem").write_text(pem)
    (tmp_path / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")
    return ctx, pem


async def test_rtsps_untrusted_cert(self_signed):
    server = await FakeRtspServer().start(self_signed[0])
    tls = bc.BackchannelTarget("127.0.0.1", server.port, "/ch0", USER, PASSWORD, tls=ssl.create_default_context())
    with pytest.raises(bc.BackchannelTLSError, match="isn't trusted"):
        await bc.play(tls, b"", b"")
    await server.stop()


async def test_rtsps_pinned_cert(self_signed):
    server = await FakeRtspServer().start(self_signed[0])
    pinned = ssl.create_default_context(cadata=self_signed[1])
    pinned.check_hostname = False
    target = bc.BackchannelTarget("127.0.0.1", server.port, "/ch0", USER, PASSWORD,
                                  tls=ssl.create_default_context(), pinned_tls=pinned)
    assert target.url.startswith("rtsps://")
    await bc.play(target, tone(0.1, 16000), tone(0.1, 8000))
    assert server.methods[-1] == "TEARDOWN" and len(server.rtp) == 5
    await server.stop()
