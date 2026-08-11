"""Tests for suppress_recovery_turn in the session-init protocol.

Regression coverage for the race where a server-persisted message appears in
the runner's history load during create_session, causes a recovery turn to
start, and the subsequent message-forward is then buffered (and optionally
double-processed).

The race is exercised via two paths:
1. SDK harness: the server calls session-init *after* persisting the message
   (the managed-sandbox-wake / relaunch path) and then forwards the message.
   Without suppress_recovery_turn the runner would start a recovery turn from
   history, see _active_turns occupied when the forward arrives, buffer it,
   and process it a second time once the recovery turn finishes.
2. The fix: suppress_recovery_turn=True in the session-init envelope causes
   the runner to skip the recovery-turn check, leaving _active_turns empty so
   the forward triggers the turn exactly once.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.runner.session_init_protocol import (
    build_runner_session_init_payload,
)
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)

# ── helpers ────────────────────────────────────────────────────────────


AGENT_ID = "ag_recover_test"
SESSION_ID = "conv_recover_test"

_PENDING_USER_MESSAGE = {
    "id": "msg_001",
    "type": "message",
    "role": "user",
    "content": [{"type": "input_text", "text": "hello from history"}],
}
_ITEMS_PAGE = {
    "object": "list",
    "data": [_PENDING_USER_MESSAGE],
    "has_more": False,
}


class _HistoryServerClient:
    """Returns one pre-persisted user message from GET /items.

    Simulates the server state after the message has been persisted to DB
    but before the forward reaches the runner — exactly the window that
    causes the recovery-turn race.
    """

    class _Resp:
        status_code = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            pass

    async def get(self, url: str, **kwargs: Any) -> _Resp:
        del kwargs
        if url.rstrip("/").endswith("/items"):
            return self._Resp(_ITEMS_PAGE)
        return self._Resp({})

    async def post(self, url: str, **kwargs: Any) -> _Resp:
        del url, kwargs
        return self._Resp({})

    async def patch(self, url: str, **kwargs: Any) -> _Resp:
        del url, kwargs
        return self._Resp({})


def _build_sdk_app(
    server_client: Any,
) -> tuple[FastAPI, _FakeProcessManager, _ScriptedHarnessClient]:
    spec = AgentSpec(spec_version=1, name="t")
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.output_text.delta", "delta": "hi"}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )
    return app, pm, harness_client


def _session_init_payload(
    *,
    suppress_recovery_turn: bool,
    server_version: str = "0.0.0-test",
) -> dict[str, Any]:
    from omnigent.entities import Conversation

    conv = Conversation(
        id=SESSION_ID,
        agent_id=AGENT_ID,
        runner_id="runner_test",
        created_at=0,
        updated_at=0,
        root_conversation_id=SESSION_ID,
    )
    return build_runner_session_init_payload(
        conv,
        server_version=server_version,
        suppress_recovery_turn=suppress_recovery_turn,
    )


# ── tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suppress_recovery_turn_prevents_recovery_turn_from_history() -> None:
    """suppress_recovery_turn=True: create_session must not start a recovery turn.

    When the runner loads history and finds a pending user message, it would
    normally start a crash-recovery turn.  With suppress_recovery_turn set the
    turn must be suppressed so the server's subsequent message-forward can
    trigger it cleanly.

    Asserts:
    - No turn is running after the session-init POST returns.
    - A subsequent message-forward triggers exactly one turn.
    """
    app, _pm, harness = _build_sdk_app(_HistoryServerClient())

    async with _runner_client(app) as client:
        init_resp = await client.post(
            "/v1/sessions",
            json=_session_init_payload(suppress_recovery_turn=True),
        )
        assert init_resp.status_code == 201, init_resp.text

        # Give the event loop a turn — a recovery turn (if started) would now
        # be scheduled and could have updated _active_turns.
        await asyncio.sleep(0)

        # Session must be idle: suppress_recovery_turn suppressed the turn.
        get_resp = await client.get(f"/v1/sessions/{SESSION_ID}")
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json().get("status") == "idle", (
            "Session must be idle after session-init with suppress_recovery_turn=True; "
            "a recovery turn was started from history instead."
        )

        # Now forward the message — this should trigger exactly one turn.
        forward_resp = await client.post(
            f"/v1/sessions/{SESSION_ID}/events",
            params={"stream": "true"},
            json={
                "type": "message",
                "role": "user",
                "agent_id": AGENT_ID,
                "content": [{"type": "input_text", "text": "hello"}],
                "persisted_item_id": "msg_001",
            },
        )
        assert forward_resp.status_code == 200, (
            f"Message forward returned {forward_resp.status_code}: {forward_resp.text}"
        )
        _ = forward_resp.text  # drain the streaming response

        assert len(harness.posted_bodies) == 1, (
            f"Expected exactly one harness call (one turn); got {len(harness.posted_bodies)}"
        )


@pytest.mark.asyncio
async def test_without_suppress_recovery_turn_starts_recovery_turn_from_history() -> None:
    """Without suppress_recovery_turn the runner starts a recovery turn from history.

    This documents the pre-fix behaviour: when the session-init envelope does
    NOT carry suppress_recovery_turn=True, the runner sees the persisted user
    message in history and starts a recovery turn immediately.  A subsequent
    forward then finds an active turn and buffers the message.  After the
    recovery turn finishes, _check_and_start_next_turn processes the buffered
    message as a second turn, so the harness is called twice.
    """
    app, _pm, harness = _build_sdk_app(_HistoryServerClient())

    async with _runner_client(app) as client:
        init_resp = await client.post(
            "/v1/sessions",
            json=_session_init_payload(suppress_recovery_turn=False),
        )
        assert init_resp.status_code == 201, init_resp.text

        # Let the recovery turn run and complete.
        await asyncio.sleep(0.1)

        # The recovery turn consumed the history message — harness was called once.
        assert len(harness.posted_bodies) == 1, (
            "Expected recovery turn to call the harness once after create_session "
            f"without suppress_recovery_turn; got {len(harness.posted_bodies)}"
        )

        # Now forward the message: since the recovery turn already ran and
        # _active_turns is now empty, the forward triggers a second turn.
        # (In the original bug, the forward would have been buffered _during_
        # the recovery turn and then replayed after it, resulting in two turns.)
        forward_resp = await client.post(
            f"/v1/sessions/{SESSION_ID}/events",
            params={"stream": "true"},
            json={
                "type": "message",
                "role": "user",
                "agent_id": AGENT_ID,
                "content": [{"type": "input_text", "text": "hello"}],
                "persisted_item_id": "msg_001",
            },
        )
        assert forward_resp.status_code == 200, (
            f"Message forward returned {forward_resp.status_code}: {forward_resp.text}"
        )
        _ = forward_resp.text  # drain

        # Second turn ran — harness called twice total.
        assert len(harness.posted_bodies) == 2, (
            "Expected two harness calls total (recovery turn + forward-triggered turn); "
            f"got {len(harness.posted_bodies)}"
        )
