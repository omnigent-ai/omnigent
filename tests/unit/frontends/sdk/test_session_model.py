"""Unit tests for :class:`Session`'s per-request model_override plumbing.

Mirrors :mod:`tests.unit.frontends.sdk.test_session_reasoning` — the
``/model`` slash command's runtime contract is exactly the same shape
as ``/effort``: a session-local override that flows into
``responses.stream(...)`` (normal turns) and ``responses.steer(...)``
(steering race where the steer becomes a new response).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from omnigent_client._events import ResponseCompleted, ResponseCreated
from omnigent_client._session import Session
from omnigent_client._types import Response


class _ResponsesStub:
    """Minimal stub recording every kwarg passed to stream/steer.

    Concrete class (not :class:`MagicMock`) so an unexpected attribute
    access fails loudly instead of silently auto-creating mocks —
    matches the project's testing rules. See
    ``.claude/skills/omnigent-testing/SKILL.md`` rule 3.
    """

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
    """Stand-in for :class:`OmnigentClient` exposing only ``responses``."""

    def __init__(self) -> None:
        self.responses = _ResponsesStub()


def test_session_set_model_override_stores_value() -> None:
    """A non-empty model string is recorded as the session override.

    Proves the setter path that ``/model <name>`` relies on. If this
    fails, every subsequent test (and the REPL command itself) is
    operating on stale state.
    """
    session = Session(_ClientStub(), "agent")  # type: ignore[arg-type]
    session.set_model_override("openai/gpt-5.4-mini")
    assert session.model_override == "openai/gpt-5.4-mini"


def test_session_set_model_override_trims_whitespace() -> None:
    """Surrounding whitespace is trimmed before storage.

    ``/model  databricks-claude-sonnet-4-6  `` should land on the
    server as the bare model id. If this regresses, the trimmed-on-
    server side would surface the leading-space variant as an unknown
    model.
    """
    session = Session(_ClientStub(), "agent")  # type: ignore[arg-type]
    session.set_model_override("  databricks-claude-sonnet-4-6  ")
    assert session.model_override == "databricks-claude-sonnet-4-6"


def test_session_set_model_override_python_none_clears() -> None:
    """Passing ``None`` clears a previously-set override.

    Backs ``/model default | reset | off``.
    """
    session = Session(_ClientStub(), "agent")  # type: ignore[arg-type]
    session.set_model_override("openai/gpt-5.4-mini")
    session.set_model_override(None)
    assert session.model_override is None


@pytest.mark.parametrize("blank", ["", "   ", "\t\n "])
def test_session_set_model_override_rejects_empty_without_mutating(blank: str) -> None:
    """Empty/whitespace-only input raises and leaves prior state intact.

    ``/model`` (no args) is handled by the slash-command branch
    BEFORE calling the setter; this guards the setter contract itself
    so callers can't accidentally clear via empty input — they have
    to pass Python ``None`` explicitly.
    """
    session = Session(_ClientStub(), "agent")  # type: ignore[arg-type]
    session.set_model_override("openai/gpt-5.4-mini")
    with pytest.raises(ValueError):
        session.set_model_override(blank)
    assert session.model_override == "openai/gpt-5.4-mini"


@pytest.mark.asyncio
async def test_session_stream_passes_model_override_to_responses_stream() -> None:
    """An active override appears as ``model_override`` on the stream call.

    End-to-end SDK assertion: prove the value actually reaches the
    underlying responses namespace, not just the session attribute.
    """
    client = _ClientStub()
    session = Session(client, "agent")  # type: ignore[arg-type]
    session.set_model_override("openai/gpt-5.4-mini")
    events = [event async for event in session.send("hi")]
    # ResponseCreated + ResponseCompleted from the stub — proves the
    # mock pipeline yielded events at all.
    assert events
    assert client.responses.stream_kwargs is not None
    # The kwargs dict carries the override exactly as set.
    assert client.responses.stream_kwargs["model_override"] == "openai/gpt-5.4-mini"


@pytest.mark.asyncio
async def test_session_stream_passes_none_model_override_when_default() -> None:
    """No override → SDK passes ``model_override=None`` to ``responses.stream``.

    The session always passes the kwarg (not "omits" — the kwarg is
    always present in the call); the value is ``None`` when no
    override is set. The server-side ``_build_body`` then decides
    whether to add the field to the wire payload (it skips ``None``).

    Verifies the opposite direction of the "passes override" test:
    clearing or never-setting must not leak a stale value to the
    SDK transport layer.
    """
    client = _ClientStub()
    session = Session(client, "agent")  # type: ignore[arg-type]
    events = [event async for event in session.send("hi")]
    assert events
    assert client.responses.stream_kwargs is not None
    # Kwarg IS present in the call dict, but its value is None.
    # If the SDK started omitting the kwarg entirely (e.g. a future
    # refactor that does ``**({"model_override": x} if x else {})``)
    # the KeyError would surface here.
    assert "model_override" in client.responses.stream_kwargs
    assert client.responses.stream_kwargs["model_override"] is None


@pytest.mark.asyncio
async def test_session_steer_passes_model_override_for_race_new_response() -> None:
    """When a steer races and becomes a new response, the override is honored.

    Steering normally targets the in-flight response (which already
    locked in its model). But ``Session.send`` falls back to steer
    when ``is_streaming`` is true, and if the server returns a
    different response id the new response is a fresh turn — that
    turn must honor the active model override or the user's
    ``/model`` choice silently no-ops on the racy path.
    """
    client = _ClientStub()
    session = Session(client, "agent")  # type: ignore[arg-type]
    session.set_model_override("openai/gpt-5.4-mini")
    session._is_terminal = False
    session._current_response_id = "resp_current"
    client.responses.steer_response = Response(id="resp_new", status="in_progress", model="agent")
    events = [event async for event in session.send("steer me")]
    assert events
    assert client.responses.steer_kwargs is not None
    assert client.responses.steer_kwargs["model_override"] == "openai/gpt-5.4-mini"
