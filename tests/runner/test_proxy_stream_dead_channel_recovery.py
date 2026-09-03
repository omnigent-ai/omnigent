"""Regression: proxy_stream must recover a mid-stream dead-channel drop.

The runner's ``proxy_stream`` relay reads the streamed model response via
``harness_resp.aiter_text()``. When the upstream SSE connection drops
mid-turn with ``httpx.ReadError`` (e.g. the Databricks AI-Gateway
Anthropic path drops ~20s into a turn), the error is caught by
``proxy_stream``'s generic ``except (httpx.HTTPError, RuntimeError)``
handler, which terminates the turn with the generic ``response.failed``
"Harness stream connection error." and makes no retry.

``httpx.ReadError`` is already listed in ``_DEAD_HARNESS_CHANNEL_ERRORS``
and the runner already has a "retry once on dead channel" recovery path
-- but that recovery is scoped to the ASK-gate policy-verdict delivery,
NOT the primary model-stream relay. So a mid-stream ``ReadError`` on the
main stream produces zero assistant output with no recovery, even though
the same gateway serves the stream fine to a fresh client (a retry would
complete).

The fix: ``proxy_stream`` must retry the harness stream once when the
error is a dead-channel error (in ``_DEAD_HARNESS_CHANNEL_ERRORS``),
mirroring the policy-verdict delivery path.

Pre-fix these tests FAIL because only one stream attempt is made and the
turn terminates with ``response.failed``; after the fix the relay retries
and the turn completes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

_CONV_ID = "aa11bb22cc33dd44ee55ff6677889900"
_AGENT_ID = "965906f5d9fb596610dda599a80faaee"
_MODEL_TEXT = "hello from the model"


class _ReadErrorThenHealthyHarnessClient(_ScriptedHarnessClient):
    """Harness stream that drops mid-turn once, then serves a clean stream.

    Attempt #1 emits ``response.created`` and then raises ``httpx.ReadError``
    from ``aiter_text`` -- the exact ~20s-in Databricks AI-Gateway drop.
    A retry (attempt #2) finds the gateway healthy and streams a normal
    delta + ``response.completed``. ``attempts`` records how many
    ``stream()`` calls ``proxy_stream`` makes so the test can prove
    whether the primary relay retried the dead channel.
    """

    def __init__(self) -> None:
        """Start with no scripted frames and a zeroed attempt counter."""
        super().__init__([])
        self.attempts = 0

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Record the POST body and return a per-attempt stream context."""
        del method, url, timeout
        self.posted_bodies.append(json)
        self.attempts += 1
        attempt = self.attempts

        class _Ctx:
            status_code = 200

            async def __aenter__(self_inner) -> _ReadErrorThenHealthyHarnessClient._Handle:
                return _ReadErrorThenHealthyHarnessClient._Handle(attempt)

            async def __aexit__(self_inner, *_: Any) -> None:
                return None

        return _Ctx()

    class _Handle:
        status_code = 200

        def __init__(self, attempt: int) -> None:
            """Store which attempt this stream handle serves."""
            self._attempt = attempt

        async def aiter_text(self) -> AsyncIterator[str]:
            """Drop mid-stream on attempt #1; serve a clean stream after."""
            yield _sse({"type": "response.created", "response": {"id": f"resp_{self._attempt}"}})
            if self._attempt == 1:
                raise httpx.ReadError("harness stream dropped mid-turn")
            yield _sse({"type": "response.output_text.delta", "delta": _MODEL_TEXT})
            yield _sse({"type": "response.completed", "response": {"id": f"resp_{self._attempt}"}})


class _RemoteDisconnectedThenHealthyHarnessClient(_ScriptedHarnessClient):
    """Harness that raises httpx.RemoteProtocolError on the first attempt.

    Covers the other prominent member of _DEAD_HARNESS_CHANNEL_ERRORS:
    a broken TCP connection where the remote side disconnects abruptly.
    Attempt #2 serves a clean stream.
    """

    def __init__(self) -> None:
        """Start with no scripted frames and a zeroed attempt counter."""
        super().__init__([])
        self.attempts = 0

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Return a stream context that raises on the first attempt."""
        del method, url, timeout
        self.posted_bodies.append(json)
        self.attempts += 1
        attempt = self.attempts

        class _Ctx:
            status_code = 200

            async def __aenter__(  # type: ignore[override]
                self_inner,
            ) -> _RemoteDisconnectedThenHealthyHarnessClient._Handle:
                return _RemoteDisconnectedThenHealthyHarnessClient._Handle(attempt)

            async def __aexit__(self_inner, *_: Any) -> None:
                return None

        return _Ctx()

    class _Handle:
        status_code = 200

        def __init__(self, attempt: int) -> None:
            """Store which attempt this stream handle serves."""
            self._attempt = attempt

        async def aiter_text(self) -> AsyncIterator[str]:
            """Raise RemoteProtocolError on attempt #1; complete on attempt #2."""
            yield _sse({"type": "response.created", "response": {"id": f"resp_{self._attempt}"}})
            if self._attempt == 1:
                raise httpx.RemoteProtocolError("peer disconnected without sending a response")
            yield _sse({"type": "response.output_text.delta", "delta": _MODEL_TEXT})
            yield _sse({"type": "response.completed", "response": {"id": f"resp_{self._attempt}"}})


def _parse_sse_frames(buffer: str) -> list[dict[str, Any]]:
    """Parse ``data: {json}`` SSE frames out of a raw relayed buffer."""
    events: list[dict[str, Any]] = []
    while "\n\n" in buffer:
        frame, _, buffer = buffer.partition("\n\n")
        data_line = next((line for line in frame.splitlines() if line.startswith("data:")), None)
        if data_line is None:
            continue
        try:
            events.append(json.loads(data_line[5:].strip()))
        except json.JSONDecodeError:
            continue
    return events


async def _run_turn(harness_client: _ScriptedHarnessClient) -> list[dict[str, Any]]:
    """Drive one turn through a runner app backed by *harness_client*.

    Returns the parsed SSE frames received by the caller.
    """
    pm = _FakeProcessManager(harness_client)
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    buffer = ""
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{_CONV_ID}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": _AGENT_ID,
                "model": "plain-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 200, resp.text
        # Drain the live SSE relay exactly as a browser/REPL client would.
        async for chunk in resp.aiter_text():
            buffer += chunk

    return _parse_sse_frames(buffer)


@pytest.mark.asyncio
async def test_proxy_stream_recovers_from_mid_stream_readerror() -> None:
    """A mid-stream ``httpx.ReadError`` must be retried, not surfaced as failure.

    The primary model-stream relay drops mid-stream with ``httpx.ReadError``
    and must retry once (as the policy-verdict delivery path does), not
    terminate the turn with the generic "Harness stream connection error."
    and zero output.
    """
    harness_client = _ReadErrorThenHealthyHarnessClient()
    frames = await _run_turn(harness_client)

    failed = [f for f in frames if f.get("type") == "response.failed"]
    completed = [f for f in frames if f.get("type") == "response.completed"]
    deltas = [f.get("delta") for f in frames if f.get("type") == "response.output_text.delta"]

    # The primary relay must retry the dead channel (as the policy path does),
    # not fail the turn on the first mid-stream ReadError.
    assert harness_client.attempts == 2, (
        f"proxy_stream must retry the dead harness channel once on a mid-stream "
        f"httpx.ReadError (httpx.ReadError is in _DEAD_HARNESS_CHANNEL_ERRORS); "
        f"got {harness_client.attempts} stream attempt(s) -- the primary "
        f"model-stream relay lacks the retry the policy-verdict path already has."
    )

    # No generic terminal failure should reach the client when a retry recovers.
    assert not failed, (
        f"turn surfaced a terminal response.failed instead of recovering from a "
        f"transient mid-stream drop: {failed!r}"
    )

    # The retried turn must relay the model output and complete -- not zero output.
    assert _MODEL_TEXT in deltas, (
        f"model output was never relayed after the mid-stream ReadError; "
        f"got deltas={deltas!r} (the turn produced zero assistant output)"
    )
    assert completed, (
        "turn never reached response.completed after the transient mid-stream "
        "drop -- it terminated with no recovery and no output"
    )


@pytest.mark.asyncio
async def test_proxy_stream_recovers_from_mid_stream_remote_protocol_error() -> None:
    """A mid-stream ``httpx.RemoteProtocolError`` must also be retried.

    Covers the other prominent dead-channel error besides ``httpx.ReadError``:
    an abrupt remote disconnect mid-turn should be recovered the same way.
    """
    harness_client = _RemoteDisconnectedThenHealthyHarnessClient()
    frames = await _run_turn(harness_client)

    failed = [f for f in frames if f.get("type") == "response.failed"]
    completed = [f for f in frames if f.get("type") == "response.completed"]
    deltas = [f.get("delta") for f in frames if f.get("type") == "response.output_text.delta"]

    assert harness_client.attempts == 2, (
        f"proxy_stream must retry once on httpx.RemoteProtocolError; "
        f"got {harness_client.attempts} attempt(s)"
    )
    assert not failed, (
        f"turn surfaced response.failed on a recoverable RemoteProtocolError: {failed!r}"
    )
    assert _MODEL_TEXT in deltas, (
        f"model output was never relayed after RemoteProtocolError; deltas={deltas!r}"
    )
    assert completed, "turn never reached response.completed after RemoteProtocolError"


@pytest.mark.asyncio
async def test_proxy_stream_does_not_retry_a_clean_failure() -> None:
    """A non-dead-channel error must NOT be retried -- it fails immediately.

    When the harness itself signals turn failure (``response.failed`` event)
    or raises a non-channel error, the relay must not retry -- that would
    duplicate the turn.  This test verifies the retry is scoped only to
    dead-channel errors.
    """

    class _ImmediateFailHarnessClient(_ScriptedHarnessClient):
        """Harness that raises a non-channel error on every attempt."""

        def __init__(self) -> None:
            super().__init__([])
            self.attempts = 0

        def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
            del method, url, timeout
            self.posted_bodies.append(json)
            self.attempts += 1

            class _Ctx:
                status_code = 200

                async def __aenter__(self_inner) -> Any:
                    return _ImmediateFailHarnessClient._Handle()

                async def __aexit__(self_inner, *_: Any) -> None:
                    return None

            return _Ctx()

        class _Handle:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                # A well-formed harness-level failure event, not a transport error.
                yield _sse({"type": "response.created", "response": {"id": "resp_fail"}})
                yield _sse(
                    {
                        "type": "response.failed",
                        "error": {"message": "model refused", "type": "ModelRefusal"},
                    }
                )

    harness_client = _ImmediateFailHarnessClient()
    frames = await _run_turn(harness_client)

    # The relay must not retry on a clean harness-level failure.
    assert harness_client.attempts == 1, (
        f"proxy_stream must NOT retry on a well-formed harness response.failed; "
        f"got {harness_client.attempts} attempt(s)"
    )
    failed = [f for f in frames if f.get("type") == "response.failed"]
    assert failed, "the harness-level response.failed must be relayed to the client"


@pytest.mark.asyncio
async def test_proxy_stream_does_not_retry_after_text_delta_delivered() -> None:
    """A mid-stream drop AFTER a text delta was delivered must NOT be retried.

    Once model text output has started streaming, a re-POST would duplicate
    the turn's output to the client. The retry gate must not fire; the
    turn surfaces the generic ``connection_error`` and stops after one attempt.
    """

    class _DropAfterDeltaHarnessClient(_ScriptedHarnessClient):
        def __init__(self) -> None:
            super().__init__([])
            self.attempts = 0

        def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
            del method, url, timeout
            self.posted_bodies.append(json)
            self.attempts += 1

            class _Ctx:
                status_code = 200

                async def __aenter__(self_inner) -> Any:
                    return _DropAfterDeltaHarnessClient._Handle()

                async def __aexit__(self_inner, *_: Any) -> None:
                    return None

            return _Ctx()

        class _Handle:
            status_code = 200

            async def aiter_text(self) -> AsyncIterator[str]:
                # Emit response.created AND a text delta before dropping.
                yield _sse({"type": "response.created", "response": {"id": "resp_d"}})
                yield _sse({"type": "response.output_text.delta", "delta": "partial"})
                raise httpx.ReadError("dropped after partial output")

    harness_client = _DropAfterDeltaHarnessClient()
    frames = await _run_turn(harness_client)

    # Once text output has started, the relay must NOT retry (would duplicate output).
    assert harness_client.attempts == 1, (
        f"proxy_stream must NOT retry after text deltas were delivered; "
        f"got {harness_client.attempts} attempt(s)"
    )
    failed = [f for f in frames if f.get("type") == "response.failed"]
    assert failed, "expected response.failed when drop occurs after text output started"
