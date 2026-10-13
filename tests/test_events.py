"""The server-sent events parser (ThinginoAgent.events) and the hello probe, fed by a fake HTTP response."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.thingino_cam.api import (
    AgentAuthError, AgentConnectionError, AgentError, AgentEvent, Connection, ThinginoAgent,
)


class FakeResponse:
    def __init__(self, lines=(), status=200, error=None, hang=False):
        self.status = status
        self.released = False
        self._lines, self._error, self._hang = list(lines), error, hang
        self.content = self._stream()

    async def _stream(self):
        for line in self._lines:
            yield line if isinstance(line, bytes) else line.encode()
        if self._error:
            raise self._error
        if self._hang:
            await asyncio.Event().wait()

    async def text(self):
        return "error body"

    def release(self):
        self.released = True


def agent_for(response):
    session = MagicMock()
    session.request = AsyncMock(return_value=response)
    return ThinginoAgent(session, Connection("https://cam/api/v1", {}, None))


async def collect(agent):
    """Events up to the point where the stream ends (which raises)."""
    events = []
    with pytest.raises(AgentConnectionError):
        async for event in agent.events():
            events.append(event)
    return events


async def test_typed_event_with_json_data():
    resp = FakeResponse(["event: motion.started\n", 'data: {"x": 1}\n', "\n"])
    assert await collect(agent_for(resp)) == [AgentEvent("motion.started", {"x": 1})]


async def test_event_without_type_is_a_message():
    assert await collect(agent_for(FakeResponse(['data: {"a": 2}\n', "\n"]))) == [AgentEvent("message", {"a": 2})]


async def test_type_applies_to_one_event_only():
    resp = FakeResponse(["event: a\n", "data: {}\n", "\n", "data: {}\n", "\n"])
    assert [e.type for e in await collect(agent_for(resp))] == ["a", "message"]


async def test_multi_line_data_is_joined_with_newlines():
    resp = FakeResponse(["event: e\n", 'data: {"a":\n', "data: 1}\n", "\n"])
    assert await collect(agent_for(resp)) == [AgentEvent("e", {"a": 1})]


async def test_data_without_a_space_after_the_colon():
    assert await collect(agent_for(FakeResponse(['data:{"a": 1}\n', "\n"]))) == [AgentEvent("message", {"a": 1})]


async def test_crlf_line_endings():
    resp = FakeResponse(["event: hello\r\n", 'data: {"ok": true}\r\n', "\r\n"])
    assert await collect(agent_for(resp)) == [AgentEvent("hello", {"ok": True})]


async def test_comments_and_keepalives_are_skipped():
    resp = FakeResponse([": keepalive\n", "\n", "event: e\n", ": mid-event comment\n", "data: {}\n", "\n"])
    assert await collect(agent_for(resp)) == [AgentEvent("e", {})]


async def test_blank_lines_without_data_emit_nothing_and_reset_the_type():
    resp = FakeResponse(["event: dropped\n", "\n", "data: {}\n", "\n"])
    assert await collect(agent_for(resp)) == [AgentEvent("message", {})]


async def test_invalid_json_is_kept_raw():
    assert await collect(agent_for(FakeResponse(["data: not json\n", "\n"]))) == [
        AgentEvent("message", {"raw": "not json"})]


async def test_non_object_json_is_wrapped():
    assert await collect(agent_for(FakeResponse(["data: [1, 2]\n", "\n", "data: 7\n", "\n"]))) == [
        AgentEvent("message", {"value": [1, 2]}), AgentEvent("message", {"value": 7})]


async def test_undecodable_bytes_do_not_break_the_stream():
    events = await collect(agent_for(FakeResponse([b'data: {"a": "\xff"}\n', b"\n"])))
    assert events[0].data == {"a": "�"}


async def test_unterminated_event_is_dropped_when_the_stream_ends():
    assert await collect(agent_for(FakeResponse(["event: e\n", "data: {}\n"]))) == []


async def test_closed_stream_raises_and_releases_the_response():
    resp = FakeResponse()
    with pytest.raises(AgentConnectionError, match="closed"):
        async for _ in agent_for(resp).events():
            pass
    assert resp.released


async def test_connection_error_mid_stream():
    resp = FakeResponse(["data: {}\n", "\n"], error=aiohttp.ClientPayloadError("cut off"))
    events = []
    with pytest.raises(AgentConnectionError, match="cut off"):
        async for event in agent_for(resp).events():
            events.append(event)
    assert len(events) == 1 and resp.released


async def test_read_timeout_mid_stream():
    resp = FakeResponse(error=TimeoutError())
    with pytest.raises(AgentConnectionError):
        async for _ in agent_for(resp).events():
            pass


@pytest.mark.parametrize(("status", "error"), [(401, AgentAuthError), (403, AgentAuthError), (500, AgentError)])
async def test_http_errors(status, error):
    with pytest.raises(error):
        async for _ in agent_for(FakeResponse(status=status)).events():
            pass


async def test_event_stream_request_has_no_total_timeout():
    resp = FakeResponse()
    agent = agent_for(resp)
    await collect(agent)
    timeout = agent._session.request.call_args.kwargs["timeout"]
    assert timeout.total is None and timeout.sock_read == 180


# --- wait_for_hello -----------------------------------------------------------------------------------

async def test_hello_first():
    resp = FakeResponse(["event: hello\n", "data: {}\n", "\n"], hang=True)
    assert await agent_for(resp).wait_for_hello(1)
    assert resp.released


async def test_another_event_first_is_not_hello():
    resp = FakeResponse(["event: motion.started\n", "data: {}\n", "\n"], hang=True)
    assert not await agent_for(resp).wait_for_hello(1)


async def test_silent_stream_times_out():
    resp = FakeResponse(hang=True)
    assert not await agent_for(resp).wait_for_hello(0.05)
    assert resp.released


async def test_proxy_that_answers_with_a_json_error_instead_of_events():
    """An unfixed web UI proxy returns 200 and a JSON body, so no event ever parses and the stream ends."""
    resp = FakeResponse(['{"error": "not found"}\n'])
    assert not await agent_for(resp).wait_for_hello(1)


async def test_hello_probe_rejected():
    assert not await agent_for(FakeResponse(status=401)).wait_for_hello(1)
