"""Stream URLs from the agent's /state (rtsp.py)."""
import pytest

from custom_components.thingino_cam.rtsp import StreamTarget, stream_target, stream_url


@pytest.mark.parametrize(("rtsp_url", "expected"), [
    ("rtsps://192.0.2.10:322/ch0", StreamTarget("rtsps", 322, "/ch0")),
    ("rtsps://[2001:db8::10]:322/ch0", StreamTarget("rtsps", 322, "/ch0")),
    ("rtsps://2001:db8::10:322/ch0", StreamTarget("rtsps", 322, "/ch0")),     # Raptor, unbracketed
    ("rtsp://user:pw@127.0.0.1:554/ch0", StreamTarget("rtsp", 554, "/ch0")),  # prudynt
    ("rtsp://127.0.0.1/ch1", StreamTarget("rtsp", 554, "/ch1")),
])
def test_from_rtsp_url(rtsp_url, expected):
    assert stream_target({"streams": [{"rtsp_url": rtsp_url}]}, 9999) == expected


@pytest.mark.parametrize("state", [None, {}, {"streams": []}, {"streams": [{"rtsp_url": "rtsp://h:554/"}]}])
def test_fallback(state):
    assert stream_target(state, 8322) == StreamTarget("rtsps", 8322, "/ch0")


def test_endpoint_fallback():
    assert stream_target({"streams": [{"rtsp_endpoint": "main"}]}, 322) == StreamTarget("rtsps", 322, "/main")


def test_stream_url_ipv6_and_quoting():
    url = stream_url(StreamTarget("rtsps", 322, "/ch0"), "2001:db8::10", "thingino", "p@ss:word")
    assert url == "rtsps://thingino:p%40ss%3Aword@[2001:db8::10]:322/ch0"
