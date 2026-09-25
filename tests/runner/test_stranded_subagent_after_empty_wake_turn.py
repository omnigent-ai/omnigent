"""The runner recovers a failed sub-agent result stranded by an empty wake turn.

A failed child's result lands in the parent inbox and the framework posts an
auto-wake notice, but the parent's wake turn can complete with empty model
output (``response.completed`` with ``output: []``, a known intermittent
Codex-native behavior). ``_run_turn_bg`` discards ``_subagent_wake_pending``
at turn start, so without inbox-state recovery nothing re-wakes the parent and
the failure stays silently undrained until a human sends another message.

Both tests drive the full sequence through the runner's in-process HTTP layer
and assert the recovery contract on the recorded wake POSTs:

* an EMPTY wake turn with an undrained inbox triggers exactly one bounded
  recovery wake
* a wake turn that produced output is NOT re-woken — the parent answered the
  notice and chose not to drain, so re-waking would burn a model turn on
  every sub-agent completion
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
    """An empty wake turn with an undrained inbox gets one recovery wake.

    Guards the reported strand: the wake-pending flag is discarded at turn
    start, so after the empty turn only inbox-state recovery can re-wake the
    parent. Without the fix the second wake never arrives and this times out.
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


@pytest.mark.asyncio
async def test_wake_turn_with_output_does_not_rewake_undrained_inbox() -> None:
    """A wake turn that produced output gets no recovery wake.

    The parent visibly answered the notice and chose not to drain the inbox.
    Re-waking it anyway would spend a model turn after every sub-agent
    completion (and, chained, would fire later scripted actions early).
    """
    from omnigent.runner import app as runner_app

    parent_id = "c3d4e5f6a7b81234567890abcdef0134"
    child_id = "d4e5f6a7b8c91234567890abcdef0145"

    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)

    gate = asyncio.Event()
    # The wake turn streams a text reply but never calls sys_read_inbox.
    harness_client = _BlockingHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_wake_text"}}),
            _sse(
                {
                    "type": "response.output_text.delta",
                    "delta": "Noted: the researcher failed; standing by.",
                }
            ),
            _sse(
                {"type": "response.completed", "response": {"id": "resp_wake_text", "output": []}}
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

            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            assert len(server_client.wake_posts) == 1
            first_wake_text = server_client.wake_posts[0]["data"]["content"][0]["text"]
            server_client.wake_seen.clear()

            parent_resp = await client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "e5f6a7b8c9d01829304a5b6c7d8e9f01",
                    "model": "test-agent",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": first_wake_text}],
                },
            )
            assert parent_resp.status_code == 202, parent_resp.text

            await asyncio.wait_for(harness_client.post_seen.wait(), timeout=5.0)
            gate.set()

            # No recovery wake may fire for a wake turn that surfaced output.
            recovery_fired = True
            try:
                await asyncio.wait_for(server_client.wake_seen.wait(), timeout=2.0)
            except TimeoutError:
                recovery_fired = False
            assert not recovery_fired, (
                f"A recovery wake was posted even though the wake turn produced "
                f"output; wake posts: {len(server_client.wake_posts)}"
            )
            assert len(server_client.wake_posts) == 1
    finally:
        gate.set()
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # The undrained payload stays in the inbox for the parent's next turn.
    assert session_inbox.qsize() == 1
