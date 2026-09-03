"""Regression: a mid-stream dead-channel drop on the runner's
``proxy_stream`` relay must not silently fail the turn.

**User journey.** A user starts a claude-native turn routed through the
Databricks AI-Gateway Anthropic path. The model stream begins
(``response.created``, the turn is "requesting"). ~20s in, the upstream SSE
connection drops with ``httpx.ReadError`` -- the same gateway serves the
stream fine to a fresh client, so the drop is transient. The runner's
``proxy_stream`` relay reads the harness's streamed response via
``harness_resp.aiter_text()``; the ``ReadError`` falls into the broad
``except (httpx.HTTPError, RuntimeError)`` handler, which terminates the turn
with the generic ``response.failed`` ("Harness stream connection error.") and
makes **no retry**. The user sees the turn fail with zero assistant output and
no recovery.

``httpx.ReadError`` is already a member of ``_DEAD_HARNESS_CHANNEL_ERRORS``
and the runner already has a "retry once on dead channel" recovery path -- but
that recovery is scoped to the ASK-gate policy-verdict delivery, NOT the
primary model-stream relay. So the main-stream ``ReadError`` terminates the
turn as failed with no retry.

This test drives the runner's real HTTP stream endpoint (``POST
/v1/sessions/{id}/events?stream=true``) end-to-end, exactly as a browser/REPL
client would, backed by a harness whose stream drops mid-turn with
``httpx.ReadError`` on the first attempt and serves a clean stream on a retry.

- **Pre-fix (the bug):** ``proxy_stream`` makes exactly ONE stream attempt,
  relays a terminal ``response.failed`` with ``code=connection_error`` /
  ``message="Harness stream connection error."`` / ``type="ReadError"``, and
  the turn produces zero ``response.output_text.delta`` and never reaches
  ``response.completed``. The assertions below FAIL on this build.
- **Post-fix:** the relay retries the dead channel once (no output has been
  delivered yet, so a re-POST is safe), the retried stream completes, and the
  model output is relayed. The assertions PASS.

Kept self-contained (its own scripted harness, no dependency on
``tests/runner`` conftest helpers) so it stands on its own as the regression
guard for this bug.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec

_CONV_ID = "aa11bb22cc33dd44ee55ff6677889900"
_AGENT_ID = "965906f5d9fb596610dda599a80faaee"
_MODEL_TEXT = "hello from the model"


def _sse(event: dict[str, Any]) -> str:
    """Render one SSE ``data: {json}\\n\\n`` frame."""
    return f"data: {json.dumps(event)}\n\n"


class _NullServerClient:
    """Minimal Omnigent server-client stub: every call is a silent empty 200.

    The relay makes incidental server calls (session fetch, label patch,
    history load); none matter to this test, so all return an empty 200.
    """

    class _Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            """Return an empty JSON object."""
            return {}

        def raise_for_status(self) -> None:
            """No-op: stub always succeeds."""
            return

    async def get(self, url: str, **kwargs: Any) -> _NullServerClient._Response:
        """Return an empty 200 for any GET request."""
        del url, kwargs
        return self._Response()

    async def post(self, url: str, **kwargs: Any) -> _NullServerClient._Response:
        """Return an empty 200 for any POST request."""
        del url, kwargs
        return self._Response()

    async def patch(self, url: str, **kwargs: Any) -> _NullServerClient._Response:
        """Return an empty 200 for any PATCH request."""
        del url, kwargs
        return self._Response()


class _ReadErrorThenHealthyHarnessClient:
    """Harness whose stream drops mid-turn once, then serves a clean stream.

    Attempt #1 emits ``response.created`` then raises ``httpx.ReadError`` from
    ``aiter_text`` -- the exact ~20s-in Databricks AI-Gateway drop. A retry
    (attempt #2) finds the gateway healthy and streams a normal delta +
    ``response.completed``. ``attempts`` records how many ``stream()`` calls
    ``proxy_stream`` makes, so the test can prove whether the primary relay
    retried the dead channel.
    """

    def __init__(self) -> None:
        """Start with a zeroed attempt counter and empty POST capture."""
        self.posted_bodies: list[dict[str, Any]] = []
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

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        """PATCH-style result post back to the harness -- record + 200."""
        del url, json, timeout

        class _Response:
            status_code = 200
            headers: dict[str, str] = {}
            content = b""

            def raise_for_status(self) -> None:
                return None

        return _Response()

    class _Handle:
        status_code = 200

        def __init__(self, attempt: int) -> None:
            """Store which attempt this stream handle serves."""
            self._attempt = attempt

        async def aiter_text(self) -> AsyncIterator[str]:
            """Drop mid-stream on attempt #1; serve a clean stream after."""
            yield _sse({"type": "response.created", "response": {"id": f"resp_{self._attempt}"}})
            if self._attempt == 1:
                raise httpx.ReadError(
                    "harness stream dropped mid-turn (~20s Databricks AI-Gateway drop)"
                )
            yield _sse({"type": "response.output_text.delta", "delta": _MODEL_TEXT})
            yield _sse({"type": "response.completed", "response": {"id": f"resp_{self._attempt}"}})


class _FakeProcessManager:
    """ProcessManager stub returning a single scripted harness client.

    Implements the surface ``proxy_stream`` touches: ``get_client`` +
    in-flight / activity bookkeeping. All bookkeeping is recorded but inert.
    """

    handles_tool_dispatch = True

    def __init__(self, client: _ReadErrorThenHealthyHarnessClient) -> None:
        """Wrap *client* so :meth:`get_client` returns it."""
        self._client = client
        self._sessions: set[str] = set()
        self._active_turns: set[str] = set()

    async def get_client(
        self, conversation_id: str, harness: str, env: Any = None
    ) -> _ReadErrorThenHealthyHarnessClient:
        """Return the fixed scripted client and register the session."""
        del harness, env
        self._sessions.add(conversation_id)
        return self._client

    def has_session(self, conversation_id: str) -> bool:
        """Whether a session was registered via :meth:`get_client`."""
        return conversation_id in self._sessions

    def has_active_turn(self, conversation_id: str) -> bool:
        """Whether a turn is marked active for this conversation."""
        return conversation_id in self._active_turns

    def note_activity(self, conversation_id: str) -> None:
        """Record an activity lease refresh (inert)."""
        del conversation_id

    def mark_turn_active(self, conversation_id: str) -> None:
        """Mark a conversation as having an active turn."""
        self._active_turns.add(conversation_id)

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """Record a live turn, mirroring the real reaper guard."""
        del response_id
        self._active_turns.add(conversation_id)

    def clear_in_flight(self, conversation_id: str) -> None:
        """Clear the live-turn marker at stream end."""
        self._active_turns.discard(conversation_id)

    async def forward_cancel(self, conversation_id: str) -> bool:
        """Record a cancel and return ``True``."""
        del conversation_id
        return True

    async def release(self, conversation_id: str) -> None:
        """Record a release and drop the session."""
        self._sessions.discard(conversation_id)


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


async def _drive_turn(
    harness_client: _ReadErrorThenHealthyHarnessClient,
) -> list[dict[str, Any]]:
    """Drive one turn through the runner's real ``proxy_stream`` relay.

    Spins up the actual runner app around *harness_client*, POSTs a user
    message to the streaming events endpoint, and drains the SSE relay exactly
    as a browser/REPL client would. Returns the parsed frames the client saw.
    """
    pm = _FakeProcessManager(harness_client)
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_NullServerClient(),  # type: ignore[arg-type]
    )

    buffer = ""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
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
    (a ``_DEAD_HARNESS_CHANNEL_ERRORS`` member) before any output was
    delivered, and must retry once (as the policy-verdict delivery path
    does) instead of terminating the turn with the generic
    "Harness stream connection error." and zero output.
    """
    harness_client = _ReadErrorThenHealthyHarnessClient()
    frames = await _drive_turn(harness_client)

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
