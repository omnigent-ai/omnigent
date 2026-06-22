from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from omnigent_client._events import ResponseCompleted, ResponseCreated
from omnigent_client._session import Session
from omnigent_client._types import Response


class _ResponsesStub:
    def __init__(self) -> None:
        self.stream_kwargs: dict[str, Any] | None = None
        self.steer_kwargs: dict[str, Any] | None = None
        self.steer_response = Response(id="resp_new", status="completed", model="agent")

    async def stream(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.stream_kwargs = kwargs
        resp = Response(id="resp_stream", status="in_progress", model="agent")
        yield ResponseCreated(response=resp)
        yield ResponseCompleted(
            response=Response(id="resp_stream", status="completed", model="agent")
        )

    async def steer(self, response_id: str, input: str, **kwargs: Any) -> Response:
        self.steer_kwargs = {"response_id": response_id, "input": input, **kwargs}
        return self.steer_response


class _ClientStub:
    def __init__(self) -> None:
        self.responses = _ResponsesStub()


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_session_set_reasoning_effort_valid_values(effort: str) -> None:
    session = Session(_ClientStub(), "agent")  # type: ignore[arg-type]
    session.set_reasoning_effort(effort)
    assert session.reasoning_effort == effort
    assert session._reasoning_request() == {"effort": effort}


def test_session_set_reasoning_effort_string_none_is_provider_value() -> None:
    session = Session(_ClientStub(), "agent")  # type: ignore[arg-type]
    session.set_reasoning_effort("none")
    assert session.reasoning_effort == "none"
    assert session._reasoning_request() == {"effort": "none"}


def test_session_set_reasoning_effort_python_none_clears() -> None:
    session = Session(_ClientStub(), "agent")  # type: ignore[arg-type]
    session.set_reasoning_effort("high")
    session.set_reasoning_effort(None)
    assert session.reasoning_effort is None
    assert session._reasoning_request() is None


def test_session_set_reasoning_effort_rejects_invalid_without_mutating() -> None:
    session = Session(_ClientStub(), "agent")  # type: ignore[arg-type]
    session.set_reasoning_effort("medium")
    with pytest.raises(ValueError):
        session.set_reasoning_effort("extreme")
    assert session.reasoning_effort == "medium"


@pytest.mark.asyncio
async def test_session_stream_passes_reasoning_to_responses_stream() -> None:
    client = _ClientStub()
    session = Session(client, "agent")  # type: ignore[arg-type]
    session.set_reasoning_effort("high")
    events = [event async for event in session.send("hi")]
    assert events
    assert client.responses.stream_kwargs is not None
    assert client.responses.stream_kwargs["reasoning"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_session_stream_omits_reasoning_when_default() -> None:
    client = _ClientStub()
    session = Session(client, "agent")  # type: ignore[arg-type]
    events = [event async for event in session.send("hi")]
    assert events
    assert client.responses.stream_kwargs is not None
    assert client.responses.stream_kwargs["reasoning"] is None


@pytest.mark.asyncio
async def test_session_steer_passes_reasoning_for_race_new_response() -> None:
    client = _ClientStub()
    session = Session(client, "agent")  # type: ignore[arg-type]
    session.set_reasoning_effort("medium")
    session._is_terminal = False
    session._current_response_id = "resp_current"
    client.responses.steer_response = Response(id="resp_new", status="in_progress", model="agent")
    events = [event async for event in session.send("steer me")]
    assert events
    assert client.responses.steer_kwargs is not None
    assert client.responses.steer_kwargs["reasoning"] == {"effort": "medium"}
