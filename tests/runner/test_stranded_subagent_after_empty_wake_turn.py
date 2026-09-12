"""Regression test: failed sub-agent stranded when parent auto-wake turn returns empty.

Bug: a failed ACP sub-agent can be stranded when the parent auto-wake
turn completes with ``output: []``.

Root-cause path
---------------
1.  Child terminates with ``status="failed"`` → ``_mark_subagent_terminal_and_wake``
    puts an inbox item and adds the parent to ``_subagent_wake_pending``,
    then POSTs a ``[System: sub-agent …]`` wake notice to the parent session.
2.  The parent picks up the wake notice and starts a turn.
    ``_run_turn_bg`` **discards** the parent from ``_subagent_wake_pending``
    at turn start.
3.  The harness returns ``response.completed`` with ``output: []`` (empty).
    This is a known Codex-native behaviour; the framework cannot rely on every
    wake turn producing text.
4.  ``_on_proxy_stream_end`` → ``_check_and_start_next_turn``: the message
    buffer is empty so it calls ``_rewake_parent_if_inbox_stranded``.
5.  **Bug**: ``_rewake_parent_if_inbox_stranded`` returns immediately because
    the parent is no longer in ``_subagent_wake_pending`` (cleared in step 2).
    The inbox still holds the failed-child payload but no recovery wake fires.
6.  The parent returns to idle; the orchestrator appears unaware of the
    failure until the human sends another message.

The test drives the full sequence through the runner's in-process HTTP layer:

*  child fails while the parent is idle → first wake fires
*  the wake-notice turn is given a harness that returns an empty
   ``response.completed``
*  the test asserts that a **second** wake (the recovery wake) arrives
   within the timeout — without the fix this wait times out
*  the parent inbox is still non-empty (the payload was not drained by
   the empty turn), confirming the invariant the recovery wake guards
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from tests.runner.conftest import (
    _BlockingHarnessClient,
    _FakeProcessManager,
    _runner_client,
    _sse,
)
from tests.runner.test_app_sessions_native_supervision import _WakeRecordingServerClient


@pytest.mark.asyncio
async def test_failed_subagent_stranded_after_empty_wake_turn() -> None:
    """A failed child strands the parent when the auto-wake turn returns empty.

    Guards the gap: ``_rewake_parent_if_inbox_stranded`` is a no-op when
    ``_subagent_wake_pending`` was already cleared at turn start, so an empty
    wake turn leaves the inbox full with no follow-up recovery wake.  The fix
    must post a bounded recovery wake even when the initial auto-wake turn
    completed without draining the inbox.

    Sequence
    --------
    1.  Register parent + child sub-agent, seed parent inbox.
    2.  POST ``external_session_status: failed`` for the child while the
        parent is idle → first wake fires.
    3.  Deliver the wake notice to the parent ``/events``.  The harness for
        this turn is a :class:`_BlockingHarnessClient` whose scripted stream
        ends immediately with an **empty** ``response.completed`` (no output
        items).  Releasing the gate triggers the empty turn.
    4.  Assert that a **second** wake POST arrives (the recovery wake). On
        the buggy build this assert fails because
        ``_rewake_parent_if_inbox_stranded`` returns immediately.
    5.  Assert the parent inbox still holds the failed-child payload,
        confirming the inbox was genuinely undrained and the recovery is
        warranted.
    """
    from omnigent.runner import app as runner_app

    # Stable hex IDs so failures are searchable in logs.
    parent_id = "a1b2c3d4e5f601234567890abcdef012"
    child_id = "b2c3d4e5f6071234567890abcdef0123"

    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)

    # Gate controls when the empty wake turn ends.  It starts unset so the
    # harness blocks after the first SSE frame, giving us time to assert the
    # first wake has fired before the turn completes.
    gate = asyncio.Event()

    # The parent's harness returns a minimal empty response:
    # response.created → response.completed with no output items.
    harness_client = _BlockingHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_wake_empty"}}),
            _sse(
                {"type": "response.completed", "response": {"id": "resp_wake_empty", "output": []}}
            ),
        ],
        gate,
    )
    pm = _FakeProcessManager(harness_client)  # type: ignore[arg-type]
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="acp-worker",
        title="research",
    )

    try:
        async with _runner_client(app) as client:
            # Step 1: child terminates with a failure (ACP bridge collapsed a
            # provider 429 to "Internal error").
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {
                        "status": "failed",
                        "output": "inner executor error: Internal error",
                    },
                },
            )
            assert resp.status_code == 204, resp.text

            # Step 2: first wake arrives (child-failed → parent idled).
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            assert len(server_client.wake_posts) == 1, (
                f"Expected exactly one wake POST after child failed; "
                f"got {len(server_client.wake_posts)}"
            )
            first_wake_text = server_client.wake_posts[0]["data"]["content"][0]["text"]
            assert "finished (failed)" in first_wake_text, (
                f"First wake notice should name a failed child; got: {first_wake_text!r}"
            )
            server_client.wake_seen.clear()

            # Step 3: deliver the wake notice to the parent's /events so it
            # starts the wake turn.  The blocking harness holds the stream
            # open after response.created until the gate is released.
            parent_resp = await client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "c1d2e3f4a5b60718293a4b5c6d7e8f90",
                    "model": "test-agent",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": first_wake_text}],
                },
            )
            assert parent_resp.status_code == 202, parent_resp.text

            # Wait for the harness to receive the POST (turn has started and
            # _subagent_wake_pending has been cleared at this point).
            await asyncio.wait_for(harness_client.post_seen.wait(), timeout=5.0)

            # Step 4: release the gate → harness emits response.completed with
            # output:[] → turn ends → _check_and_start_next_turn fires.
            gate.set()

            # Step 5: assert a recovery wake fires.
            # Without the fix _rewake_parent_if_inbox_stranded returns
            # immediately (flag cleared at turn start) and this wait times out.
            try:
                await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            except TimeoutError:
                raise AssertionError(
                    "No recovery wake was posted after the parent's empty auto-wake "
                    "turn completed with the inbox still full. "
                    "_rewake_parent_if_inbox_stranded did not fire a "
                    "bounded recovery wake because _subagent_wake_pending was already "
                    f"cleared at turn start. Wake posts so far: "
                    f"{len(server_client.wake_posts)} "
                    f"(expected it to grow to 2)."
                ) from None

            assert len(server_client.wake_posts) == 2, (
                f"Expected exactly 2 wake POSTs (initial + recovery); "
                f"got {len(server_client.wake_posts)}"
            )

    finally:
        gate.set()  # ensure the harness is never permanently blocked on teardown
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # The parent inbox must still hold the undrained failed-child payload,
    # because the empty wake turn did not call sys_read_inbox.
    assert session_inbox.qsize() == 1, (
        f"Expected the failed-child payload to remain in the parent inbox "
        f"(the empty turn did not drain it); got {session_inbox.qsize()} item(s)"
    )
    delivered = session_inbox.get_nowait()
    assert delivered["status"] == "failed", (
        f"Expected inbox item status='failed'; got {delivered['status']!r}"
    )
    assert "Internal error" in delivered["output"], (
        f"Expected child error text in inbox payload; got {delivered['output']!r}"
    )
