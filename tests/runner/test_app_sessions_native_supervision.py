"""Tests for subagent status supervision, delivery, and parent wakeups."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest

from omnigent import (
    claude_native_bridge,
)
from omnigent.claude_native_bridge import (
    bridge_dir_for_conversation_id,
)
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _BlockingHarnessClient,
    _build_interrupt_app,
    _drain_session_event_queue,
    _FakeProcessManager,
    _interrupt_markers,
    _runner_client,
    _ScriptedHarnessClient,
    _spec_resolver_returning,
    _sse,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_interrupt_inserts_cancellation_items_in_history() -> None:
    """Interrupting a turn with dangling function_calls inserts synthetic outputs.

    When the user interrupts mid-tool-chain, any ``function_call``
    items that were emitted by the harness but never received a
    ``function_call_output`` must get synthetic cancelled outputs.
    A cancellation marker message must also be appended so the LLM
    knows the prior turn was incomplete. This matches the DBOS
    path's ``_append_cancellation_item`` behavior.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "85b147537400967b1fb8542367423306"

        # Start the turn — it blocks after the first function_call
        # frame, before the second one and response.completed.
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "do something"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        # Let the background turn reach the gate.
        await _aio.sleep(0.1)

        # Send an interrupt while the turn is blocked.
        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )
        # The harness stub returns 200; the real scaffold returns
        # 204. Both are success — we care about the side-effect
        # (cancellation items), not the status code.
        assert int_resp.status_code in (200, 204), (
            f"Interrupt must succeed; got {int_resp.status_code}"
        )

        # Release the gate so the harness stream finishes and
        # _on_proxy_stream_end fires with the interrupt flag.
        gate.set()
        await _aio.sleep(0.2)

    # Access the runner's in-memory history for this session.
    histories = _session_histories_ref.get(conv_id, [])

    # The history should contain synthetic function_call_output
    # items for each dangling call_id. call_a was emitted before
    # the gate (always present). call_b may or may not have been
    # emitted depending on timing, but at minimum call_a should
    # have a synthetic output.
    synthetic_outputs = [
        h
        for h in histories
        if h.get("type") == "function_call_output"
        and h.get("output") == "[Cancelled — tool execution was interrupted.]"
    ]
    dangling_calls = [h for h in histories if h.get("type") == "function_call"]
    matched_real_outputs = [
        h
        for h in histories
        if h.get("type") == "function_call_output"
        and h.get("output") != "[Cancelled — tool execution was interrupted.]"
    ]
    dangling_call_ids = {c["call_id"] for c in dangling_calls}
    real_output_call_ids = {o["call_id"] for o in matched_real_outputs}
    orphan_ids = dangling_call_ids - real_output_call_ids
    synthetic_output_call_ids = {o["call_id"] for o in synthetic_outputs}

    # Every orphaned function_call must have a synthetic output.
    # If empty, _append_cancellation_items didn't fire (the interrupt
    # flag wasn't set or wasn't checked in _on_proxy_stream_end).
    assert orphan_ids == synthetic_output_call_ids, (
        f"Every dangling function_call must get a synthetic cancelled "
        f"output. Orphan call_ids={orphan_ids}, synthetic output "
        f"call_ids={synthetic_output_call_ids}. If synthetic is empty, "
        f"_append_cancellation_items was not called on interrupt."
    )

    # Cancellation marker message must be present so the LLM knows
    # the prior output was truncated. If missing, the next turn's
    # context is silently incomplete.
    markers = [
        h
        for h in histories
        if h.get("type") == "message"
        and h.get("role") == "user"
        and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
    ]
    assert len(markers) == 1, (
        f"Expected exactly 1 cancellation marker message, "
        f"got {len(markers)}. If 0, _append_cancellation_items "
        f"didn't insert the marker."
    )


@pytest.mark.asyncio
async def test_interrupt_cancel_floor_finalizes_stuck_turn() -> None:
    """The cancel floor: interrupt force-cancels a turn the harness never finishes.

    Sister to ``test_interrupt_inserts_cancellation_items_in_history``, but the
    gate is NEVER released — the harness stream stays blocked forever, and the
    forwarded interrupt (recorded by the stub but ignored) does not unblock it.
    The turn can therefore only end because the runner force-cancels its turn
    task (``_cancel_active_turn``). If that floor regresses to forward-only, the
    turn stays stuck and no cancellation marker is ever appended.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()  # never set — only the floor's task-cancel can end the turn
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "97d2b96d733e685433e2b3864eb97652"
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "do something"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        # Deterministic: the harness stream sets post_seen when it begins, so
        # the turn is in flight and blocked on the gate before we interrupt.
        await _aio.wait_for(_hc.post_seen.wait(), timeout=5.0)

        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )
        # The handler awaits the cancel, so the turn is finalized by the time
        # this returns — capture history before the client context tears down.
        assert int_resp.status_code == 204, int_resp.text
        markers = _interrupt_markers(list(_session_histories_ref.get(conv_id, [])))

    assert len(markers) == 1, (
        f"The cancel floor must finalize a stuck turn even though the gate was "
        f"never released; got {len(markers)} interrupted markers. 0 means the "
        f"turn is still blocked — interrupt only forwarded to the harness "
        f"without cancelling the runner turn task."
    )


@pytest.mark.asyncio
async def test_stop_session_cancels_inprocess_turn() -> None:
    """``stop_session`` cancels an in-process harness's in-flight turn.

    For non-native harnesses this used to be a 204 no-op — the sidebar Stop did
    nothing. It now routes through the same cancel floor as interrupt: with the
    gate never released, the blocked turn ends only because stop_session
    force-cancels the turn task. 0 markers means the no-op regressed.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()  # never set
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "422963919abf3c166633d99ea20f2b8e"
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "do something"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        # Deterministic: wait for the harness stream to begin (turn in flight,
        # blocked on the gate) before stopping.
        await _aio.wait_for(_hc.post_seen.wait(), timeout=5.0)

        stop_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "stop_session"},
        )
        assert stop_resp.status_code == 204, stop_resp.text
        markers = _interrupt_markers(list(_session_histories_ref.get(conv_id, [])))

    assert len(markers) == 1, (
        f"stop_session must cancel the in-flight in-process turn (was a 204 "
        f"no-op); got {len(markers)} interrupted markers. 0 means stop_session "
        f"still no-ops for non-native harnesses."
    )


@pytest.mark.asyncio
async def test_interrupt_during_setup_phase_recovers_stuck_turn() -> None:
    """Interrupt during the setup phase finalizes the turn — the session isn't stuck.

    A cancel that lands while the turn is still in setup (here: blocked in the
    background turn's spec resolution, before ``_drain_streaming_response`` is
    entered) raises ``CancelledError`` past ``_run_turn_bg``'s
    ``except Exception`` (it's a ``BaseException``), so neither the drain handler
    nor the setup handler cleans up. Without the floor's setup-phase recovery,
    ``_active_turns`` keeps the done task — every later message buffers behind a
    turn that never runs and the session hangs.

    Proof: after the setup-phase interrupt, a NEW message must start a fresh
    turn (its setup re-enters the resolver). With the bug it would be buffered
    behind the stale ``_active_turns`` entry and never run. Also asserts exactly
    one interrupted marker from the cancelled turn.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    resolver_gate = _aio.Event()  # released only in teardown → spec resolution blocks
    resolver_entered = _aio.Event()
    spec = AgentSpec(spec_version=1, name="t")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Signal entry, then block so the background turn stalls in setup."""
        del agent_id, session_id
        resolver_entered.set()
        await resolver_gate.wait()
        return spec

    # Frames let the eventual (post-teardown) turn drain cleanly.
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_s"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_s"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    conv_id = "9fb432c546c7dbf9f34c6acbc861b05f"
    # agent_id is required for the background turn's setup to invoke the resolver.
    msg = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
        "content": [{"type": "input_text", "text": "do something"}],
        "harness": "openai-agents",
    }
    fresh_turn_started = False
    async with _runner_client(app) as client:
        # First turn: the handler returns 202, then the background turn blocks in
        # spec resolution (the setup phase).
        r1 = await client.post(f"/v1/sessions/{conv_id}/events", json=msg)
        assert r1.status_code == 202
        await _aio.wait_for(resolver_entered.wait(), timeout=5.0)

        # Interrupt while the turn is still in setup.
        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )
        assert int_resp.status_code == 204, int_resp.text
        markers = _interrupt_markers(list(_session_histories_ref.get(conv_id, [])))

        # The session must accept a new turn. If _active_turns were left stale,
        # this message buffers behind the dead turn and never starts a new one,
        # so the resolver is never re-entered.
        resolver_entered.clear()
        r2 = await client.post(f"/v1/sessions/{conv_id}/events", json=msg)
        assert r2.status_code == 202
        try:
            await _aio.wait_for(resolver_entered.wait(), timeout=5.0)
            fresh_turn_started = True
        except _aio.TimeoutError:
            fresh_turn_started = False

        resolver_gate.set()  # release the blocked turn for clean teardown
        await _aio.sleep(0.1)

    assert fresh_turn_started, (
        "After a setup-phase interrupt the session must accept a new turn; the "
        "follow-up message never re-entered spec resolution, so _active_turns was "
        "left stale and the session is stuck (the bug this guards)."
    )
    # Exactly one marker = only the interrupted first turn was finalized.
    assert len(markers) == 1, (
        f"The interrupted setup-phase turn must append exactly one marker; got {len(markers)}."
    )


@pytest.mark.asyncio
async def test_interrupt_marker_instructs_model_to_disregard_abandoned_request() -> None:
    """The cancellation marker tells the model to drop the canceled request.

    The bug this guards against: a marker that only says the assistant
    reply was cut off leaves the canceled user instruction in history,
    so the next turn replays it and the agent follows the abandoned
    request. The marker must explicitly instruct the model not to resume
    the interrupted request and to treat the next user message as current.
    A revert to the old "halted the agent response / may be incomplete"
    text (no disregard instruction) turns this test red.
    """
    import asyncio as _aio

    from omnigent.runner.app import _session_histories_ref

    gate = _aio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "86ba5bc9053ae8771fdb988e882f04b5"
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "delete all my files"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        await _aio.sleep(0.1)

        int_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "interrupt"},
        )
        assert int_resp.status_code in (200, 204)

        gate.set()
        await _aio.sleep(0.2)

    histories = _session_histories_ref.get(conv_id, [])
    marker_texts = [
        b.get("text") or ""
        for h in histories
        if h.get("type") == "message" and h.get("role") == "user"
        for b in h.get("content", [])
        if "interrupted" in (b.get("text") or "").lower()
    ]
    assert len(marker_texts) == 1, (
        f"Expected exactly 1 interrupted marker, got {len(marker_texts)}."
    )
    marker = marker_texts[0].lower()
    # "abandoned" framing + an explicit do-not-continue instruction are what
    # make the model drop the canceled request. The old marker had neither;
    # asserting both fails loud if the disregard semantics regress.
    assert "abandon" in marker, (
        f"Marker must frame the prior request as abandoned, got: {marker_texts[0]!r}"
    )
    assert "do not resume or act on" in marker, (
        f"Marker must instruct the model not to continue the interrupted "
        f"request, got: {marker_texts[0]!r}"
    )
    # The original assistant-incomplete disclaimer is still useful context
    # and must be retained alongside the new instruction.
    assert "may be" in marker and "incomplete" in marker, (
        f"Marker should still note the assistant message may be incomplete, "
        f"got: {marker_texts[0]!r}"
    )


@pytest.mark.asyncio
async def test_external_session_status_idle_delivers_forwarded_native_output_to_parent_inbox() -> (
    None
):
    """
    Native idle status completes sub-agent work with AP-forwarded output.

    Native harness transcript items are persisted by Omnigent server, so the
    runner's local history can be empty or stale. A forwarded
    ``data.output`` value must be used for the parent inbox instead of
    falling back to the runner-local history.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    parent_id = "d4cfd8ebd7ef0ae6f6f0c4310d2df7ce"
    child_id = "66a84f142181a489d004f744cc76c67b"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app._session_histories_ref[child_id] = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "LOCAL_SHOULD_NOT_WIN"}],
        }
    ]
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="native",
    )

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve the parent TOOL_RESULT policy check."""
        if (
            request.method == "POST"
            and request.url.path == f"/v1/sessions/{parent_id}/policies/evaluate"
        ):
            return httpx.Response(200, json={"result": "POLICY_ACTION_UNSPECIFIED"})
        return httpx.Response(404, json={"error": str(request.url)})

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "AP_NATIVE_DONE"},
                },
            )
        assert resp.status_code == 204, resp.text

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_server_handler),
            base_url="http://server",
        ) as server_client:
            inbox_output = await execute_tool(
                tool_name="sys_read_inbox",
                arguments="{}",
                server_client=server_client,
                conversation_id=parent_id,
                session_inbox=session_inbox,
            )
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)
        runner_app._session_histories_ref.pop(child_id, None)

    assert "sub-agent task 66a84f142181a489d004f744cc76c67b completed" in inbox_output
    assert "worker:native returned: AP_NATIVE_DONE" in inbox_output
    assert "LOCAL_SHOULD_NOT_WIN" not in inbox_output


@pytest.mark.asyncio
async def test_external_session_status_running_fans_out_child_busy_to_parent() -> None:
    """
    Native child ``running`` status updates the parent's child-session cache.

    Codex-native and claude-native workers report their real terminal
    lifecycle through ``external_session_status`` after the runner's prompt
    injection turn has already completed. The parent stream must still receive
    a ``session.child_session.updated`` delta with ``busy=True``; otherwise
    Nessie's Agents rail has no durable "Working" signal for native children.
    """
    from omnigent.runner import app as runner_app

    parent_id = "d72e6c2c5866b0f946739fa5a9f964f7"
    child_id = "8c9f35bc1cb0566b101c59941a576689"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_event_queues_ref.pop(parent_id, None)
    runner_app._session_event_queues_ref.pop(child_id, None)
    runner_app.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="codex:impl",
        tool="codex",
        session_name="impl",
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="codex",
        title="impl",
    )
    entry = runner_app.get_subagent_work(child_id)
    assert entry is not None
    assert entry.status == "launching"

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={"type": "external_session_status", "data": {"status": "running"}},
            )
        assert resp.status_code == 204, resp.text
        entry = runner_app.get_subagent_work(child_id)
        assert entry is not None
        assert entry.status == "running"

        events = _drain_session_event_queue(runner_app._session_event_queues_ref.get(parent_id))
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app.unregister_child_session(child_id)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)

    assert events == [
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": True,
                "current_task_status": "in_progress",
                "last_task_error": None,
            },
        }
    ]


@pytest.mark.asyncio
async def test_external_status_sequence_coalesces_duplicates_but_emits_task_status_change() -> (
    None
):
    """
    Native status fan-out coalesces duplicates, not task-status changes.

    The child rail should not churn on repeated ``running`` edges, but a rare
    ``idle`` → ``failed`` sequence must still update ``current_task_status``
    from ``"completed"`` to ``"failed"`` even though both edges are non-busy.
    """
    from omnigent.runner import app as runner_app

    parent_id = "75115d379fc444a3731a92c930150c8e"
    child_id = "2d88f6c2b566daadfb256052a0ee5abe"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_event_queues_ref.pop(parent_id, None)
    runner_app._session_event_queues_ref.pop(child_id, None)
    runner_app.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="codex:impl",
        tool="codex",
        session_name="impl",
    )

    try:
        async with _runner_client(app) as client:
            for status, output in [
                ("running", None),
                ("running", None),
                ("idle", "DONE"),
                ("failed", "BROKEN"),
            ]:
                data = {"status": status}
                if output is not None:
                    data["output"] = output
                resp = await client.post(
                    f"/v1/sessions/{child_id}/events",
                    json={"type": "external_session_status", "data": data},
                )
                assert resp.status_code == 204, resp.text

        events = _drain_session_event_queue(runner_app._session_event_queues_ref.get(parent_id))
    finally:
        runner_app.unregister_child_session(child_id)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)

    assert events == [
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": True,
                "current_task_status": "in_progress",
                "last_task_error": None,
            },
        },
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": False,
                "current_task_status": "completed",
                "last_message_preview": "DONE",
            },
        },
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": False,
                "current_task_status": "failed",
                "last_message_preview": "BROKEN",
            },
        },
    ]


@pytest.mark.asyncio
async def test_external_status_idle_fans_out_forwarded_output_preview_to_parent() -> None:
    """
    Native child ``idle`` status uses AP-forwarded output for rail preview.

    Native terminal transcripts are persisted by AP, not runner-local history.
    A terminal-observed idle edge must therefore fan out the ``data.output``
    value forwarded by AP; otherwise the Agents rail can replace the real
    native reply with stale runner-local text while clearing the spinner.
    """
    from omnigent.runner import app as runner_app

    parent_id = "83676fab214be9a9ba799712c450e385"
    child_id = "ffa0293c16d5a9e5e6c362846277c631"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_event_queues_ref.pop(parent_id, None)
    runner_app._session_event_queues_ref.pop(child_id, None)
    runner_app._session_histories_ref[child_id] = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "STALE_RUNNER_HISTORY"}],
        }
    ]
    runner_app.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="codex:impl",
        tool="codex",
        session_name="impl",
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "AP_NATIVE_DONE"},
                },
            )
        assert resp.status_code == 204, resp.text

        events = _drain_session_event_queue(runner_app._session_event_queues_ref.get(parent_id))
    finally:
        runner_app.unregister_child_session(child_id)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)
        runner_app._session_histories_ref.pop(child_id, None)

    assert events == [
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": False,
                "current_task_status": "completed",
                "last_message_preview": "AP_NATIVE_DONE",
            },
        }
    ]


@pytest.mark.asyncio
async def test_external_status_idle_without_output_omits_stale_history_preview() -> None:
    """
    Native child ``idle`` without forwarded output omits stale local text.

    If Omnigent has no authoritative native transcript text to forward, the parent
    rail and parent inbox must not fall back to runner-local history: native
    runner history may be stale because the terminal forwarder owns
    persistence. The inbox receives an explicit empty result so the parent can
    still observe completion without fabricated output.
    """
    from omnigent.runner import app as runner_app

    parent_id = "1dc34bd0fea39227de4779abc14c4b74"
    child_id = "f7423fb08cf726ea957c2f907d85edbd"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_event_queues_ref.pop(parent_id, None)
    runner_app._session_event_queues_ref.pop(child_id, None)
    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app._session_histories_ref[child_id] = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "STALE_RUNNER_HISTORY"}],
        }
    ]
    runner_app.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="codex:impl",
        tool="codex",
        session_name="impl",
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="codex",
        title="impl",
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={"type": "external_session_status", "data": {"status": "idle"}},
            )
        assert resp.status_code == 204, resp.text
        await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)

        events = _drain_session_event_queue(runner_app._session_event_queues_ref.get(parent_id))
    finally:
        runner_app.unregister_child_session(child_id)
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)
        runner_app._session_inboxes_ref.pop(parent_id, None)
        runner_app._session_histories_ref.pop(child_id, None)

    assert events == [
        {
            "type": "session.child_session.updated",
            "conversation_id": parent_id,
            "child_session_id": child_id,
            "child": {
                "id": child_id,
                "title": "codex:impl",
                "tool": "codex",
                "session_name": "impl",
                "busy": False,
                "current_task_status": "completed",
            },
        }
    ]
    assert session_inbox.qsize() == 1, (
        f"Expected one empty completion in the parent inbox, got {session_inbox.qsize()}."
    )
    delivered = session_inbox.get_nowait()
    assert delivered["status"] == "completed"
    assert delivered["output"] == ""
    assert delivered["output"] != "STALE_RUNNER_HISTORY"
    assert len(server_client.wake_posts) == 1


class _WakeRecordingServerClient(NullServerClient):
    """Records the runner→AP wake POSTs a parent session's ``/events`` receives.

    Subclasses :class:`NullServerClient` so every other runner→AP call still
    gets a benign empty 200; only POSTs to the watched parent's ``/events``
    path are captured. ``wake_seen`` lets a test await the (background) wake
    deterministically instead of sleeping.
    """

    def __init__(self, parent_id: str) -> None:
        """
        :param parent_id: Parent session whose ``/events`` POSTs to capture,
            e.g. ``"bf881b8f7e32add48bfcd6afc476452a"``.
        """
        self._parent_events_path = f"/v1/sessions/{parent_id}/events"
        self.wake_posts: list[dict[str, Any]] = []
        self.wake_seen = asyncio.Event()

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Capture a wake POST to the watched parent, else defer to the base.

        :param url: Request URL, e.g. ``"/v1/sessions/bf881b8f7e32add48bfcd6afc476452a/events"``.
        :param kwargs: Request kwargs; the wake notice is in ``json``.
        :returns: Stub 200 response from :class:`NullServerClient`.
        """
        if url == self._parent_events_path:
            body = kwargs.get("json")
            if isinstance(body, dict):
                self.wake_posts.append(body)
            self.wake_seen.set()
        return await super().post(url, **kwargs)


@pytest.mark.asyncio
async def test_native_subagent_completion_wakes_idle_parent() -> None:
    """
    A finished native sub-agent wakes its idle parent via a ``/events`` POST.

    nessie's workers are native harnesses whose completion arrives as an
    ``external_session_status: idle`` event. Delivering that completion to the
    parent inbox must ALSO post a ``[System: ...]`` wake notice to the
    *parent's* event stream, so an idle orchestrator takes a continuation turn
    instead of sleeping until the next user message. Without the wake wiring
    the inbox still fills but no parent ``/events`` POST is made — exactly the
    "nessie doesn't know its sub-agent finished" bug this fixes.
    """
    from omnigent.runner import app as runner_app

    parent_id = "bf881b8f7e32add48bfcd6afc476452a"
    child_id = "7ec2f4cd958a2c2a8c02bd3c03cbacc6"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude_code",
        title="auth",
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "WORKER_DONE"},
                },
            )
            assert resp.status_code == 204, resp.text
            # Wake is a background task; await the recorded POST (TimeoutError if none).
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # Delivery still happens: the child result is in the parent inbox. If 0,
    # external_session_status:idle did not deliver the completion at all.
    assert session_inbox.qsize() == 1, (
        f"Expected one completion in the parent inbox, got {session_inbox.qsize()}."
    )
    delivered = session_inbox.get_nowait()
    assert delivered["status"] == "completed"
    assert delivered["output"] == "WORKER_DONE"
    # Exactly one wake notice was POSTed to the PARENT's event stream. If 0,
    # the completion landed in the inbox but the idle parent was never woken
    # (the regression this test guards against).
    assert len(server_client.wake_posts) == 1, (
        f"Expected one wake POST to the parent, got {len(server_client.wake_posts)}."
    )
    wake_text = server_client.wake_posts[0]["data"]["content"][0]["text"]
    # Notice names the finished worker and steers the parent to drain the inbox.
    assert "sub-agent claude_code/auth finished (completed)" in wake_text
    assert "sys_read_inbox" in wake_text


@pytest.mark.asyncio
async def test_external_status_for_untracked_session_does_not_wake() -> None:
    """
    Completing a session that is not a tracked sub-agent wakes nobody.

    This is the loop-safety guarantee: the orchestrator's own turn ending
    routes through the same call site, but it is not registered as anyone's
    child, so ``mark_subagent_work_terminal`` returns an untracked ack and no
    wake is scheduled. A regression that dropped the ``entry is not None`` guard
    would either 500 (None.delivered) or post a spurious wake — both caught here.
    """
    orphan_id = "28e85f4c5fb460c5185e374605bc4364"
    server_client = _WakeRecordingServerClient(orphan_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    # No register_subagent_work for orphan_id — it is nobody's child.
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{orphan_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": "IGNORED"},
            },
        )
        assert resp.status_code == 204, resp.text
        # Let any erroneously-scheduled wake task run before asserting absence.
        for _ in range(5):
            await asyncio.sleep(0)

    # No wake was scheduled because the session is untracked. A non-empty list
    # would mean the orchestrator could wake (and loop on) its own turn-end.
    assert server_client.wake_posts == []
    assert not server_client.wake_seen.is_set()


@pytest.mark.asyncio
async def test_tracked_subagent_status_without_parent_inbox_returns_503() -> None:
    """
    A tracked sub-agent terminal status is not ACKed without parent delivery.

    The parent inbox is the durable handoff point for async sub-agent results.
    If the runner has a child work entry but the parent inbox is missing, a
    204 would tell AP/the forwarder the completion was delivered even though
    the parent can never drain it.
    """
    from omnigent.runner import app as runner_app

    parent_id = "41bd085f8d34ad9201cd59c372312a1a"
    child_id = "6d17e94dcad6a73de34441f490d140b4"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="lost-parent",
    )

    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "DONE_BUT_UNDELIVERED"},
                },
            )
        entry = runner_app.get_subagent_work(child_id)
    finally:
        runner_app.unregister_subagent_work(child_id)

    assert resp.status_code == 503, resp.text
    assert resp.json()["reason"] == "missing_parent_inbox"
    assert entry is not None
    # The child is terminal, but not delivered; if delivered were True here,
    # the runner would have ACKed a result the parent cannot read.
    assert entry.status == "completed"
    assert entry.delivered is False


def test_subagent_terminal_delivery_retry_uses_latest_undelivered_report() -> None:
    """
    Terminal retry delivers the latest report after the parent inbox reappears.

    A first terminal report can arrive while runner-local parent state is
    missing. The work entry must stay undelivered, but if a later terminal
    report carries newer status/output before the parent inbox returns, the
    parent should receive that latest report rather than stale cancellation
    text from the first failed delivery attempt.
    """
    from omnigent.runner import app as runner_app

    parent_id = "05f117c03074f5d4b0ebe450f79b0684"
    child_id = "01a9880c1386637a7d0a154ecb2c4a72"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="retry",
    )

    try:
        first_ack = runner_app.mark_subagent_work_terminal(
            child_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        runner_app._session_inboxes_ref[parent_id] = session_inbox
        second_ack = runner_app.mark_subagent_work_terminal(
            child_id,
            status="completed",
            output="DONE_AFTER_RETRY",
        )
        entry = runner_app.get_subagent_work(child_id)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert first_ack.reason == "missing_parent_inbox"
    assert first_ack.delivered is False
    assert first_ack.delivered_now is False
    assert second_ack.reason == "delivered"
    assert second_ack.delivered is True
    assert second_ack.delivered_now is True
    assert entry is not None
    assert entry.delivered is True
    assert session_inbox.qsize() == 1
    delivered = session_inbox.get_nowait()
    assert delivered["task_id"] == child_id
    assert delivered["status"] == "completed"
    assert delivered["output"] == "DONE_AFTER_RETRY"


def test_subagent_terminal_delivery_handles_missing_output() -> None:
    """
    Terminal work with no assistant text still delivers a marker payload.

    Native status reporters can emit ``idle`` without a final assistant
    message. That must not become an unstructured ``RuntimeError`` after the
    parent inbox is available.
    """
    from omnigent.runner import app as runner_app

    parent_id = "9c98fbe12742d712819dd26553a7a9ee"
    child_id = "c2a7357e26dc400e4aa6e5c25c611853"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="empty-output",
    )

    try:
        ack = runner_app.mark_subagent_work_terminal(
            child_id,
            status="completed",
            output=None,
        )
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert ack.reason == "delivered"
    assert ack.delivered is True
    assert session_inbox.qsize() == 1
    delivered = session_inbox.get_nowait()
    assert delivered["output"] == "[System: sub-agent completed with no output]"


@pytest.mark.asyncio
async def test_known_subagent_status_without_work_entry_returns_503() -> None:
    """
    A runner-known sub-agent session is not ACKed without a work entry.

    After runner state loss, the child session may still report terminal
    status while the child→parent work registry no longer has the handoff
    metadata. Returning 503 forces the AP/forwarder path to preserve the
    failed delivery instead of reporting success.
    """
    child_id = "38c56dfa64cb80c126c49429f7e482cd"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": child_id,
                "agent_id": "6eadea15d6e06f43d026b25b656a73ec",
                "sub_agent_name": "worker",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        try:
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "DONE_WITH_NO_WORK_ENTRY"},
                },
            )
        finally:
            await client.delete(f"/v1/sessions/{child_id}")

    assert resp.status_code == 503, resp.text
    assert resp.json()["reason"] == "missing_work_entry"


@pytest.mark.asyncio
async def test_repeated_idle_status_wakes_parent_only_once() -> None:
    """
    Re-posting a child's idle status wakes the parent only once.

    The wake gate fires on the not-delivered → delivered transition. A second
    ``external_session_status: idle`` for an already-terminal child must NOT
    re-deliver or re-wake — this is what keeps a parallel fan-out (or a
    forwarder that re-sends idle) from triggering a wake storm.
    """
    from omnigent.runner import app as runner_app

    parent_id = "f42f428f0217c078c09803aea44cd57b"
    child_id = "e63625ecd31e483b65e5333e6195cc13"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="phase-a",
    )

    idle_event = {
        "type": "external_session_status",
        "data": {"status": "idle", "output": "DONE"},
    }
    try:
        async with _runner_client(app) as client:
            resp1 = await client.post(f"/v1/sessions/{child_id}/events", json=idle_event)
            assert resp1.status_code == 204, resp1.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            server_client.wake_seen.clear()

            resp2 = await client.post(f"/v1/sessions/{child_id}/events", json=idle_event)
            assert resp2.status_code == 204, resp2.text
            # Give a (wrongly) re-scheduled wake a chance to land before asserting.
            for _ in range(5):
                await asyncio.sleep(0)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # One delivery, one wake — the second idle was a no-op. A count of 2 would
    # mean the already-delivered gate regressed and re-marking re-wakes.
    assert session_inbox.qsize() == 1, (
        f"Expected one inbox item after two idle posts, got {session_inbox.qsize()}."
    )
    assert len(server_client.wake_posts) == 1, (
        f"Expected one wake POST after two idle posts, got {len(server_client.wake_posts)}."
    )


@pytest.mark.asyncio
async def test_delete_session_clears_pending_subagent_wake() -> None:
    """
    Deleting a parent clears its outstanding sub-agent wake debounce.

    A wake POST remains pending until the parent starts a turn. If the parent
    session is deleted before consuming that wake, the debounce entry must go
    away too; otherwise a later session reusing the same id can receive a child
    result in its inbox but never get the wake notice that tells it to drain.
    """
    from omnigent.runner import app as runner_app

    parent_id = "514616cf803ec0ca696db4cdf75be6f6"
    first_child_id = "7cb2d00b228b559198a369204d1e1ffd"
    second_child_id = "06e5e6cd8edde087a16afcd0aa6a37f8"
    first_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    second_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    try:
        async with _runner_client(app) as client:
            create_resp = await client.post(
                "/v1/sessions",
                json={"session_id": parent_id, "agent_id": "727cdb9ef83160bc29658b70782734ac"},
            )
            assert create_resp.status_code == 201, create_resp.text

            runner_app._session_inboxes_ref[parent_id] = first_inbox
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=first_child_id,
                agent="worker",
                title="before-delete",
            )
            first_resp = await client.post(
                f"/v1/sessions/{first_child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "BEFORE_DELETE"},
                },
            )
            assert first_resp.status_code == 204, first_resp.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            assert len(server_client.wake_posts) == 1

            delete_resp = await client.delete(f"/v1/sessions/{parent_id}")
            assert delete_resp.status_code == 200, delete_resp.text
            server_client.wake_seen.clear()

            runner_app._session_inboxes_ref[parent_id] = second_inbox
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=second_child_id,
                agent="worker",
                title="after-delete",
            )
            second_resp = await client.post(
                f"/v1/sessions/{second_child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "AFTER_DELETE"},
                },
            )
            assert second_resp.status_code == 204, second_resp.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
    finally:
        runner_app.unregister_subagent_work(first_child_id)
        runner_app.unregister_subagent_work(second_child_id)
        runner_app.unregister_subagent_work_for_session(parent_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert first_inbox.qsize() == 1
    assert second_inbox.qsize() == 1
    assert len(server_client.wake_posts) == 2, (
        f"Expected a fresh wake after deleting and reusing the parent id, got "
        f"{len(server_client.wake_posts)} wake posts."
    )
    followup_text = server_client.wake_posts[1]["data"]["content"][0]["text"]
    assert "sub-agent worker/after-delete finished (completed)" in followup_text


@pytest.mark.asyncio
async def test_subagent_completion_during_parent_wake_turn_posts_followup_wake() -> None:
    """
    A child finishing during the parent's wake turn posts the next wake.

    The first child completion creates an outstanding wake for the parent. Once
    the parent starts processing that wake notice, the debounce must be
    considered consumed. A second child completion that lands while the parent
    turn is still active should therefore enqueue a follow-up wake rather than
    leaving the result stranded until a human sends another message.
    """
    from omnigent.runner import app as runner_app

    parent_id = "44026e683bcf8dd047e509d974196bf9"
    first_child_id = "1a9cf84a190d53d5e9b6ec4e9c534f31"
    second_child_id = "e3099fa95ced5bb8786b79a131429c78"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    gate = asyncio.Event()
    harness_client = _BlockingHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_wake_turn"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_wake_turn"}}),
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
        child_session_id=first_child_id,
        agent="codex",
        title="initial",
    )

    try:
        async with _runner_client(app) as client:
            first_resp = await client.post(
                f"/v1/sessions/{first_child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "FIRST_DONE"},
                },
            )
            assert first_resp.status_code == 204, first_resp.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            assert len(server_client.wake_posts) == 1
            server_client.wake_seen.clear()

            parent_resp = await client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "3006a2a399391aa72a65a507ff92dae3",
                    "model": "test-agent",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "wake notice"}],
                },
            )
            assert parent_resp.status_code == 202, parent_resp.text
            await asyncio.wait_for(harness_client.post_seen.wait(), timeout=5.0)
            assert harness_client.posted_bodies, "parent wake turn must reach the harness"

            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=second_child_id,
                agent="codex",
                title="followup",
            )
            second_resp = await client.post(
                f"/v1/sessions/{second_child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "SECOND_DONE"},
                },
            )
            assert second_resp.status_code == 204, second_resp.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            gate.set()
    finally:
        gate.set()
        runner_app.unregister_subagent_work(first_child_id)
        runner_app.unregister_subagent_work(second_child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert session_inbox.qsize() == 2, (
        f"Expected both completions in the parent inbox, got {session_inbox.qsize()}."
    )
    assert len(server_client.wake_posts) == 2, (
        f"Expected a follow-up wake after the parent turn started, got "
        f"{len(server_client.wake_posts)} wake posts."
    )
    followup_text = server_client.wake_posts[1]["data"]["content"][0]["text"]
    assert "sub-agent codex/followup finished (completed)" in followup_text
    assert "sys_read_inbox" in followup_text


@pytest.mark.asyncio
async def test_parent_idle_with_stuck_wake_flag_posts_recovery_wake() -> None:
    """
    A parent going idle while holding a stuck wake flag posts a recovery wake.

    Guards the multi-round fan-out stranding bug. ``_subagent_wake_pending`` is
    cleared only at turn start, so a child that completes mid-turn re-arms the
    flag with no later turn to clear it; the parent then idles with the flag
    stuck and results still in the inbox, and further completions are debounced
    and stranded. The fix (``_rewake_parent_if_inbox_stranded`` from
    ``_check_and_start_next_turn``) re-arms one wake on idle.

    Sequence (wake counts bracketed): (1) child A completes idle → wake [1],
    parent turn starts (clears flag); (2) child B completes mid-turn → wake [2],
    re-arms flag; (3) turn ends → recovery wake [3] WITH the fix, stays [2]
    without it (the discriminator); (4) child C completes → correctly
    *coalesced* against the re-armed flag (inbox grows, no 4th wake). Child C is
    kept only to pin that coalesce contract — the signal is the step-3 wake.
    """
    from omnigent.runner import app as runner_app

    parent_id = "22b91e208e5501fb8d2b502837391f04"
    child_a = "27cb54833afaf691aacb1bb7ec7ce66b"
    child_b = "06bc724cd3a9b5ee75c95a057a561cfb"
    child_c = "8cc1d911d72dee40ec6956d369f0b55a"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    gate = asyncio.Event()
    harness_client = _BlockingHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_rewake"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_rewake"}}),
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
        child_session_id=child_a,
        agent="claude",
        title="debate",
    )

    try:
        async with _runner_client(app) as client:
            # 1. Child A finishes while the parent is idle → first wake.
            resp_a = await client.post(
                f"/v1/sessions/{child_a}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "A_DONE"},
                },
            )
            assert resp_a.status_code == 204, resp_a.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            # Just the idle-parent wake; 0 = never fired, 2 = spurious POST.
            assert len(server_client.wake_posts) == 1, (
                f"Expected the single idle-parent wake, got {len(server_client.wake_posts)}."
            )
            server_client.wake_seen.clear()

            # Start the parent's wake turn (blocking harness holds it active).
            # post_seen resolves only after _run_turn_bg's turn-start clear, so
            # the flag is guaranteed clear for child B below.
            parent_resp = await client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "7c04e18ef7e3a769f7eecca21b049374",
                    "model": "test-agent",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "wake notice"}],
                },
            )
            assert parent_resp.status_code == 202, parent_resp.text
            await asyncio.wait_for(harness_client.post_seen.wait(), timeout=5.0)

            # 2. Child B finishes DURING the active parent turn: not debounced
            # (flag was just cleared), posts its own wake, and re-arms the flag.
            # No new turn starts on this wake, so nothing clears it again.
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=child_b,
                agent="gpt",
                title="debate",
            )
            resp_b = await client.post(
                f"/v1/sessions/{child_b}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "B_DONE"},
                },
            )
            assert resp_b.status_code == 204, resp_b.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            server_client.wake_seen.clear()
            # Baseline before the parent idles: A + B, recovery not yet fired.
            # Not 2 → child B posted no distinct wake, so the stuck-flag
            # precondition the fix recovers from is not reproduced.
            wakes_before_idle = len(server_client.wake_posts)
            assert wakes_before_idle == 2, (
                f"Expected 2 wakes (idle-parent A + mid-turn B) before the "
                f"parent goes idle, got {wakes_before_idle}."
            )

            # 3. End the parent turn → _check_and_start_next_turn runs the
            # re-arm. Await the recovery wake directly (not a fixed sleep);
            # without the fix it never posts and this wait_for times out.
            gate.set()
            try:
                await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            except TimeoutError:
                raise AssertionError(
                    "No recovery wake was posted after the parent went idle "
                    "holding a stuck wake flag with a non-empty inbox. The "
                    f"count stayed at {len(server_client.wake_posts)} "
                    "(expected it to grow to 3). This is the multi-round "
                    "fan-out stranding bug: _rewake_parent_if_inbox_stranded "
                    "did not re-arm the wake at turn end."
                ) from None
            # Recovery wake is the 3rd POST. 2 = re-arm never fired (stranding
            # bug); 4 = double-posted.
            assert len(server_client.wake_posts) == 3, (
                f"Expected the recovery wake to bring the count to 3, got "
                f"{len(server_client.wake_posts)}."
            )
            server_client.wake_seen.clear()

            # Let the turn fully settle so C is unambiguously a post-idle event.
            _deadline = asyncio.get_running_loop().time() + 5.0
            while app.state.has_active_work():
                if asyncio.get_running_loop().time() > _deadline:
                    raise AssertionError("parent wake turn did not end within 5s")
                await asyncio.sleep(0.01)
            for _ in range(5):
                await asyncio.sleep(0)

            # 4. Child C finishes post-idle. The recovery wake re-armed the
            # flag, so C must COALESCE: result lands in the inbox, no new wake.
            # Yield generously so a (wrongly) scheduled extra wake would land.
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=child_c,
                agent="claude",
                title="debate",
            )
            resp_c = await client.post(
                f"/v1/sessions/{child_c}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "C_DONE"},
                },
            )
            assert resp_c.status_code == 204, resp_c.text
            for _ in range(10):
                await asyncio.sleep(0)
    finally:
        gate.set()
        runner_app.unregister_subagent_work(child_a)
        runner_app.unregister_subagent_work(child_b)
        runner_app.unregister_subagent_work(child_c)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # All 3 completions reached the inbox regardless of coalescing; 2 = a
    # completion was lost.
    assert session_inbox.qsize() == 3, (
        f"Expected all 3 completions in the parent inbox, got {session_inbox.qsize()}."
    )
    # Exactly 3 wakes: A + B + recovery, with C coalesced. 4 = fix wrongly woke
    # per-completion; 2 = recovery wake never fired (stranding regression).
    assert len(server_client.wake_posts) == 3, (
        f"Expected exactly 3 wakes (A + B + recovery, with C coalesced), got "
        f"{len(server_client.wake_posts)}."
    )
    # Recovery wake (3rd) names child B — the latest completed at turn end, not
    # C — and reports the 2 results stranded then (A + B, before C).
    recovery_text = server_client.wake_posts[2]["data"]["content"][0]["text"]
    assert "sub-agent gpt/debate finished (completed)" in recovery_text, (
        f"Recovery wake should name child B (gpt/debate), the latest "
        f"completed child at turn end; got: {recovery_text!r}"
    )
    assert "2 results waiting in inbox" in recovery_text, (
        f"Recovery wake should report the 2 results stranded at turn end "
        f"(A's + B's, before C); got: {recovery_text!r}"
    )
    assert "sys_read_inbox" in recovery_text


@pytest.mark.asyncio
async def test_parent_idle_with_stuck_wake_flag_and_drained_inbox_clears_flag() -> None:
    """
    A parent idling with a stuck wake flag but an EMPTY inbox clears the flag.

    Companion to ``test_parent_idle_with_stuck_wake_flag_posts_recovery_wake``,
    which covers the stuck-flag + *non-empty* inbox case (re-arm a recovery
    wake). This guards the sibling variant the reviewer flagged:
    the parent consumes a mid-turn wake as an injection AND drains
    ``sys_read_inbox`` in that *same* live turn, so the turn ends with the
    debounce flag still set (turn start is the only place it clears) but the
    inbox already emptied. The buggy helper returned early on
    ``inbox.empty()`` WITHOUT discarding the flag, so the flag stayed stuck
    forever — and the NEXT child completion was debounced and stranded. The
    fix (``_rewake_parent_if_inbox_stranded``) discards the flag on idle
    *regardless* of inbox state, posting a recovery wake only when results
    remain.

    The closure-local ``_subagent_wake_pending`` set lives inside
    ``create_runner_app`` and has no module-level ref (unlike
    ``_session_inboxes_ref``), so the flag-clear is asserted *behaviorally* —
    which is also the stronger, user-facing claim: a subsequent child
    completion WAKES the parent (fresh POST) instead of being silently
    debounced. A stuck flag would suppress that wake, which is exactly the
    stranding the fix prevents. This is the discriminator: it goes red on the
    buggy ``inbox.empty()``-returns-without-discard ordering and green on the
    fix.

    Sequence (wake counts bracketed): (1) child A completes idle → wake [1],
    parent turn starts (clears flag); (2) child B completes mid-turn → wake
    [2], re-arms flag (A's + B's results now both queued); (3) the test drains
    the inbox to EMPTY in-turn (mirrors the parent draining via
    ``sys_read_inbox`` during its live turn) — flag stays set; (4) the turn
    ends idle → helper discards the stuck flag and, because the inbox is
    empty, posts NO recovery wake (count stays [2]); (5) child C completes
    post-idle → because the flag was cleared, C is NOT debounced and posts a
    fresh wake [3]. Under the bug, step 4 leaves the flag set, so step 5's C
    is debounced (count stays [2]) and C's result strands.
    """
    from omnigent.runner import app as runner_app

    parent_id = "8f0e87b24df3f773e7f8de693347dad9"
    child_a = "95dae25caa3d012baa1a1309cde1674b"
    child_b = "f92aa6bcffbfc5ed559a93d8c9404572"
    child_c = "41967d48eb327dd8f2f6e4442977c813"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    gate = asyncio.Event()
    harness_client = _BlockingHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_drained"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_drained"}}),
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
        child_session_id=child_a,
        agent="claude",
        title="debate",
    )

    try:
        async with _runner_client(app) as client:
            # 1. Child A finishes while the parent is idle → first wake.
            resp_a = await client.post(
                f"/v1/sessions/{child_a}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "A_DONE"},
                },
            )
            assert resp_a.status_code == 204, resp_a.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            # Just the idle-parent wake; 0 = never fired, 2 = spurious POST.
            assert len(server_client.wake_posts) == 1, (
                f"Expected the single idle-parent wake, got {len(server_client.wake_posts)}."
            )
            server_client.wake_seen.clear()

            # Start the parent's wake turn (blocking harness holds it active).
            # post_seen resolves only after _run_turn_bg's turn-start clear, so
            # the flag is guaranteed clear for child B below.
            parent_resp = await client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "d99814e82ffc3ad874ca24f44a86ffc7",
                    "model": "test-agent",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "wake notice"}],
                },
            )
            assert parent_resp.status_code == 202, parent_resp.text
            await asyncio.wait_for(harness_client.post_seen.wait(), timeout=5.0)

            # 2. Child B finishes DURING the active parent turn: not debounced
            # (flag was just cleared), posts its own wake, and re-arms the flag.
            # No new turn starts on this wake, so nothing clears it again.
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=child_b,
                agent="gpt",
                title="debate",
            )
            resp_b = await client.post(
                f"/v1/sessions/{child_b}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "B_DONE"},
                },
            )
            assert resp_b.status_code == 204, resp_b.text
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            server_client.wake_seen.clear()
            # Baseline before the drain: A + B wakes fired, recovery not yet.
            # Not 2 → child B posted no distinct wake, so the stuck-flag
            # precondition the fix recovers from is not reproduced.
            wakes_before_drain = len(server_client.wake_posts)
            assert wakes_before_drain == 2, (
                f"Expected 2 wakes (idle-parent A + mid-turn B) before the "
                f"inbox is drained, got {wakes_before_drain}."
            )

            # 3. Drain the inbox to EMPTY while the turn is still active — the
            # B wake_seen above guarantees both completions are already queued
            # (delivery put_nowait precedes the wake task). This stands in for
            # the parent draining sys_read_inbox during its live turn: the
            # debounce flag is still set (only turn start clears it), but the
            # inbox is now empty. This is the precondition the non-empty-inbox
            # sibling test deliberately never creates.
            drained = 0
            while not session_inbox.empty():
                session_inbox.get_nowait()
                drained += 1
            # A + B were delivered into the inbox before either wake task ran.
            # Not 2 → a completion never reached the inbox (delivery regression),
            # which would invalidate the empty-inbox precondition below.
            assert drained == 2, (
                f"Expected to drain A's + B's queued completions (2), got "
                f"{drained}; the empty-inbox-at-idle precondition is invalid."
            )
            assert session_inbox.empty(), "inbox must be empty before the turn ends"

            # 4. End the parent turn → _check_and_start_next_turn runs the
            # stuck-flag clear. The inbox is empty, so NO recovery wake posts;
            # the only observable here is that the flag was discarded, which
            # step 5 proves. Wait for the turn to fully end (no recovery wake
            # to await on, unlike the non-empty-inbox sibling).
            gate.set()
            _deadline = asyncio.get_running_loop().time() + 5.0
            while app.state.has_active_work():
                if asyncio.get_running_loop().time() > _deadline:
                    raise AssertionError("parent wake turn did not end within 5s")
                await asyncio.sleep(0.01)
            for _ in range(5):
                await asyncio.sleep(0)
            # No recovery wake fired on the empty inbox: still 2. A 3rd here
            # would mean the helper wrongly re-armed a wake with nothing
            # stranded (the non-empty path leaking into the empty case).
            assert len(server_client.wake_posts) == 2, (
                f"Expected no recovery wake on the drained (empty) inbox, "
                f"got {len(server_client.wake_posts)} wakes."
            )

            # 5. Child C finishes post-idle. If step 4 cleared the stuck flag
            # (the fix), C is NOT debounced and posts a FRESH wake. If the flag
            # stayed stuck (the bug: inbox.empty() returned before discard), C
            # is debounced and its result strands with no wake — the exact
            # regression this guards. Await the wake directly: under the bug it
            # never posts and this wait_for times out.
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=child_c,
                agent="claude",
                title="debate",
            )
            resp_c = await client.post(
                f"/v1/sessions/{child_c}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "C_DONE"},
                },
            )
            assert resp_c.status_code == 204, resp_c.text
            try:
                await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            except TimeoutError:
                raise AssertionError(
                    "No wake was posted for child C after the parent idled with "
                    "a drained (empty) inbox. The wake count stayed at "
                    f"{len(server_client.wake_posts)} (expected it to grow to "
                    "3). This is the drained-inbox stuck-flag bug: "
                    "_rewake_parent_if_inbox_stranded returned on inbox.empty() "
                    "WITHOUT discarding _subagent_wake_pending, so child C was "
                    "debounced and stranded."
                ) from None
    finally:
        gate.set()
        runner_app.unregister_subagent_work(child_a)
        runner_app.unregister_subagent_work(child_b)
        runner_app.unregister_subagent_work(child_c)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # Child C posted the 3rd wake: the stuck flag was cleared on idle. 2 = flag
    # stayed stuck and C was debounced (the regression); 4 = a spurious extra
    # wake (e.g. an erroneous empty-inbox recovery POST in step 4).
    assert len(server_client.wake_posts) == 3, (
        f"Expected exactly 3 wakes (A + B + C's post-clear wake), got "
        f"{len(server_client.wake_posts)}."
    )
    # The 3rd wake is C's: it names child C and steers the parent to drain.
    # A wrong name would mean a different completion posted this wake.
    c_wake_text = server_client.wake_posts[2]["data"]["content"][0]["text"]
    assert "sub-agent claude/debate finished (completed)" in c_wake_text, (
        f"Third wake should name child C (claude/debate); got: {c_wake_text!r}"
    )
    assert "sys_read_inbox" in c_wake_text
    # C's completion reached the inbox (1 item, queued after the step-3 drain).
    # Under the bug C still delivers, but with no wake the parent never learns
    # to drain it — 0 here would instead mean C's delivery itself regressed.
    assert session_inbox.qsize() == 1, (
        f"Expected child C's completion alone in the drained inbox, got {session_inbox.qsize()}."
    )
    delivered_c = session_inbox.get_nowait()
    assert delivered_c["status"] == "completed"
    assert delivered_c["output"] == "C_DONE"


@pytest.mark.asyncio
async def test_replayed_idle_status_after_inbox_drain_is_acknowledged() -> None:
    """
    Replayed terminal status after parent drain is a benign duplicate.

    ``sys_read_inbox`` removes delivered work after the parent collects it.
    The runner keeps a delivered tombstone so a later native forwarder replay
    sees an already-delivered ack instead of a false ``missing_work_entry``
    503 for a still-known child session.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    parent_id = "fde99284fcd969bcadb10a80290e6dc5"
    child_id = "6ce8004b6fc222e4a2794d177dc55042"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="drained",
    )

    idle_event = {
        "type": "external_session_status",
        "data": {"status": "idle", "output": "DONE_AND_DRAINED"},
    }

    async def _policy_handler(request: httpx.Request) -> httpx.Response:
        """
        Allow the parent TOOL_RESULT policy check during inbox drain.

        :param request: Policy-evaluation request from ``sys_read_inbox``.
        :returns: Allow verdict for the delayed sub-agent output.
        """
        if (
            request.method == "POST"
            and request.url.path == f"/v1/sessions/{parent_id}/policies/evaluate"
        ):
            return httpx.Response(200, json={"result": "POLICY_ACTION_UNSPECIFIED"})
        return httpx.Response(404, json={"error": str(request.url)})

    try:
        async with _runner_client(app) as client:
            create_resp = await client.post(
                "/v1/sessions",
                json={
                    "session_id": child_id,
                    "agent_id": "c14cab5182af3847da17636fdc7644e0",
                    "sub_agent_name": "worker",
                },
            )
            assert create_resp.status_code == 201, create_resp.text
            first_resp = await client.post(f"/v1/sessions/{child_id}/events", json=idle_event)
            assert first_resp.status_code == 204, first_resp.text
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(_policy_handler),
                base_url="http://server",
            ) as policy_client:
                drain_output = await execute_tool(
                    tool_name="sys_read_inbox",
                    arguments="{}",
                    server_client=policy_client,
                    conversation_id=parent_id,
                    session_inbox=session_inbox,
                )
            assert runner_app.get_subagent_work(child_id) is None
            replay_resp = await client.post(f"/v1/sessions/{child_id}/events", json=idle_event)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app.unregister_subagent_work_for_session(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert "DONE_AND_DRAINED" in drain_output
    assert replay_resp.status_code == 204, replay_resp.text
    assert session_inbox.qsize() == 0


@pytest.mark.asyncio
async def test_concurrent_subagent_completions_coalesce_into_one_wake() -> None:
    """
    A fan-out's completions debounce to a single wake POST.

    When a parent dispatches several workers and they finish close together,
    each completion is delivered to the parent inbox, but only the FIRST posts
    a wake notice — the rest are suppressed while that wake is outstanding.
    The one wake turn drains the whole inbox via sys_read_inbox. Without the
    debounce, N completions POST N synthetic /events messages, churning turns
    and tripping the executor's per-turn tool-context guard ("no active turn
    context") — the regression this guards against.
    """
    from omnigent.runner import app as runner_app

    parent_id = "0c51258a4c62e5c390402b8473ae8271"
    child_ids = [
        "cef7ed6e3019655ad11ad769184a43a7",
        "62e17066cdf6a5a732c054faefb7ef39",
        "a0c5b28f0854df92006a4ff0c857ac3a",
    ]
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    for idx, child_id in enumerate(child_ids):
        runner_app.register_subagent_work(
            parent_session_id=parent_id,
            child_session_id=child_id,
            agent="claude_code",
            title=f"worker-{idx}",
        )

    try:
        async with _runner_client(app) as client:
            for child_id in child_ids:
                resp = await client.post(
                    f"/v1/sessions/{child_id}/events",
                    json={
                        "type": "external_session_status",
                        "data": {"status": "idle", "output": f"DONE_{child_id}"},
                    },
                )
                assert resp.status_code == 204, resp.text
            # Let the (single, debounced) wake task and any suppressed ones run.
            await asyncio.wait_for(server_client.wake_seen.wait(), timeout=5.0)
            for _ in range(5):
                await asyncio.sleep(0)
    finally:
        for child_id in child_ids:
            runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # All three completions were delivered to the parent inbox...
    assert session_inbox.qsize() == 3, (
        f"Expected all 3 completions in the parent inbox, got {session_inbox.qsize()}."
    )
    # ...but only ONE wake notice was posted (the other two were debounced).
    # A count of 3 means the debounce regressed and the wake storm is back.
    assert len(server_client.wake_posts) == 1, (
        f"Expected exactly one (debounced) wake POST for the fan-out, got "
        f"{len(server_client.wake_posts)}."
    )


@pytest.mark.asyncio
async def test_events_interrupt_on_native_session_injects_escape_without_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` with ``{"type": "interrupt"}`` on a claude-native
    session sends Escape to the pane — and nothing else to the transcript.

    The interrupt handler's whole job is the Escape keystroke. This test
    pins the three properties of that handler:

    1. ``inject_interrupt`` is called with the bridge dir derived from
       the conversation id (the Escape keystroke that actually stops
       Claude).
    2. NO ``[System: interrupted]`` marker is appended to the runner's
       in-memory ``_session_histories``. That synthetic marker is for
       in-process LLM harnesses, where the runner's history is the
       model's next-turn context; Claude-native owns its own session and
       records the interrupt in its own (forwarder-mirrored) transcript,
       so persisting a forged ``role:"user"`` marker only diverged the
       AP-side mirror from Claude's real transcript.
    3. NO ``session.status: idle`` is enqueued: idle on interrupt now
       comes solely from the terminal's PTY-activity watcher (it sees
       the pane quiesce after the Escape), which also keeps the session
       ``running`` if the interrupt didn't take. Synthesizing idle here
       would bypass the watcher's running/idle dedupe and could strand
       the UI on idle.

    If side effect 1 is missing, the Escape never lands and Claude keeps
    generating; if a marker reappears in 2, the holdover that forged the
    user bubble is back; if a synthesized idle reappears in 3, the
    watcher desync bug is back.
    """
    from omnigent.runner.app import _session_event_queues_ref, _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    captured_inject: list[Any] = []

    def _fake_inject(bridge_dir: Any, *, timeout_s: float) -> None:
        """Record the call and return without touching tmux."""
        captured_inject.append((bridge_dir, timeout_s))

    monkeypatch.setattr(claude_native_bridge, "inject_interrupt", _fake_inject)

    # Native spec: executor.type="omnigent" + config.harness="claude-native"
    # is the canonical shape the runner reads at session start to
    # populate _session_spec_cache; _session_harness_name reads it
    # back at interrupt time to pick the right dispatch branch.
    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # POST /v1/sessions seeds _session_spec_cache so the
        # interrupt dispatch can detect "claude-native".
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "664449321754215750a1d43e89fca21e",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        # POST /events with type=interrupt. By the time this returns,
        # ``_handle_claude_native_interrupt`` has fully run — the sync
        # history mutation (``_append_cancellation_items``) and the
        # sub-agent wake finished before the response. So we can read
        # both ``_session_histories`` and ``_session_event_queues`` from
        # the test without any subscribe / sleep dance.
        int_resp = await client.post(
            "/v1/sessions/664449321754215750a1d43e89fca21e/events",
            json={"type": "interrupt"},
        )

        # Snapshot history + drain queue BEFORE deleting the session.
        # DELETE clears ``_session_histories`` and pops the queue from
        # ``_session_event_queues``, so reading after delete would
        # always see empty.
        captured_history = list(_session_histories_ref.get("664449321754215750a1d43e89fca21e", []))
        queue = _session_event_queues_ref.get("664449321754215750a1d43e89fca21e")
        assert queue is not None, (
            "Session creation should have initialized the event queue "
            "for ``664449321754215750a1d43e89fca21e``; ``_session_event_queues_ref`` is "
            "missing the entry, so we couldn't drain it to verify the "
            "interrupt handler did not enqueue a synthesized idle."
        )
        queued_events: list[dict[str, Any]] = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    # 1) tmux Escape was sent via inject_interrupt.
    # 0 = the dispatch fell through to the generic forward-to-harness
    # path (which 404s for native — silent regression).
    assert int_resp.status_code == 204, (
        f"Native interrupt must return 204 from /events; "
        f"got {int_resp.status_code}: {int_resp.text}"
    )
    assert len(captured_inject) == 1, (
        f"Expected one inject_interrupt call, got {len(captured_inject)}. "
        f"If 0, the dispatch in /events did not route to the native "
        f"handler — possibly _session_harness_name returned the wrong "
        f"canonical name."
    )
    bridge_dir, timeout_s = captured_inject[0]
    assert bridge_dir == bridge_dir_for_conversation_id("664449321754215750a1d43e89fca21e")
    # 1.0s short timeout: UI stop must feel snappy. If this becomes
    # the helper's 30s default, the user's click would hang on any
    # missing tmux.json.
    assert timeout_s == 1.0

    # 2) NO cancellation marker is appended to the runner's in-memory
    # history. The native interrupt must not forge a [System: interrupted]
    # user message into the AP-side mirror — Claude records the interrupt
    # in its own transcript. If a marker reappears, _append_cancellation_items
    # was wired back into the native handler.
    markers = [
        h
        for h in captured_history
        if h.get("type") == "message"
        and h.get("role") == "user"
        and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
    ]
    assert markers == [], (
        f"Expected no [System: interrupted] marker in history, got "
        f"{len(markers)}. If 1, _append_cancellation_items was re-invoked "
        f"by the native interrupt handler — the holdover that forged a "
        f"user bubble into the mirror is back. History: {captured_history!r}"
    )

    # 3) The interrupt handler must NOT synthesize session.status: idle.
    # Idle on interrupt now comes from the terminal's PTY activity watcher
    # (it sees the pane quiesce after the Escape) — the single source of
    # truth, which also keeps the session ``running`` if the interrupt
    # didn't take. Synthesizing idle here would bypass the watcher's
    # running/idle dedupe and could strand the UI on idle. This guards
    # against re-adding the pre-PTY synthesized idle.
    status_idle = [
        e for e in queued_events if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert status_idle == [], (
        f"The native interrupt handler must not enqueue session.status: idle "
        f"(the PTY watcher emits it on quiesce); got {status_idle!r} on the "
        f"queue: {queued_events!r}."
    )


@pytest.mark.parametrize(
    ("harness", "expected_statuses"),
    [
        # claude-native's working status is owned by the PTY-activity
        # watcher, so the runner injection task must not publish its
        # own running/idle edges.
        ("claude-native", []),
        # codex-native may use the runner's running edge so the thread
        # shows work as soon as Omnigent accepts the turn, but must not use the
        # runner's idle edge because the injection task completes before
        # the user-visible Codex turn.
        ("codex-native", ["running"]),
        # antigravity-native shares codex's shape: the executor's
        # SendUserCascadeMessage returns as soon as agy accepts the turn, so the
        # RPC read driver (not the runner injection task) owns idle. Publishing
        # the runner's idle here fires ~2s before agy's output streams and
        # prematurely completes the response (the live-e2e "double-idle").
        ("antigravity-native", ["running"]),
        # Non-terminal harnesses have no external lifecycle observer; the
        # runner turn remains their source of truth.
        ("openai-agents", ["running", "idle"]),
    ],
)
@pytest.mark.asyncio
async def test_message_turn_lifecycle_status_suppressed_for_terminal_backed_harnesses(
    harness: str,
    expected_statuses: list[str],
) -> None:
    """
    Runner lifecycle status is edge-specific for terminal-backed harnesses.

    First-principles invariant: the thread's "Working…" indicator should
    represent the user-visible model turn. For claude-native, the runner
    turn is only a pane-injection task, so its ``running`` and ``idle`` edges
    are both suppressed. For codex-native, the runner's ``running`` edge is a
    useful immediate signal that Omnigent accepted the turn, but its ``idle`` edge
    is invalid because the injection task finishes before Codex is done.

    Drives the real ``POST /events`` message path and waits for the
    background injection turn to finish, so both the synchronous ``running``
    edge and the invalid injection-task ``idle`` edge are observable if they
    regress.

    :param harness: Harness configured for the session, e.g.
        ``"codex-native"``.
    :param expected_statuses: Runner-published lifecycle statuses expected
        on the session stream, e.g. ``["running", "idle"]``.
    :returns: None.
    """
    from omnigent.runner.app import _session_event_queues_ref

    session_id = uuid.uuid4().hex
    spec = AgentSpec(
        spec_version=1,
        name="t",
        # executor.type="omnigent" + config.harness=<harness> is the
        # canonical shape the runner reads at session start to populate
        # _session_spec_cache; _session_harness_name reads it back to
        # decide whether a PTY watcher owns this session's status.
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )
    stream_finished = asyncio.Event()
    harness_client = _ScriptedHarnessClient([], stream_finished=stream_finished)
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=await _spec_resolver_returning(spec),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # POST /v1/sessions seeds _session_spec_cache so the turn path can
        # resolve the harness and decide whether to suppress turn status.
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        # Native session creation may try to auto-create a terminal. This
        # fixture intentionally has no real terminal registry / workspace, so
        # that setup path can enqueue ``session.status: failed`` before the
        # message turn under test. Drain creation-time events so the assertion
        # below isolates only the runner turn lifecycle around POST /events.
        queue = _session_event_queues_ref.get(session_id)
        assert queue is not None, (
            f"session creation must initialize the per-session event queue "
            f"for {session_id!r}; missing means the turn-status publish had "
            f"nowhere to land."
        )
        while not queue.empty():
            queue.get_nowait()

        msg_resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "test"}],
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert msg_resp.status_code in (200, 202), msg_resp.text

        if harness != "claude-native":
            # Wait until the background injection task drains its scripted
            # empty stream. If the runner still owns terminal-backed status,
            # this is where the invalid ``idle`` edge appears.
            await asyncio.wait_for(stream_finished.wait(), timeout=1.0)

            # Let the task resume after stream exhaustion and publish its
            # terminal lifecycle edge before the queue is drained below.
            await asyncio.sleep(0)

        # Drain BEFORE deleting: DELETE removes the queue. By this point both
        # the synchronous turn-start edge and the async turn-end edge have run.
        statuses: list[str] = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict) and item.get("type") == "session.status":
                statuses.append(item.get("status"))

        # DELETE cancels the background turn so it can't outlive the test.
        await client.delete(f"/v1/sessions/{session_id}")

    assert statuses == expected_statuses, (
        f"harness={harness}: expected runner turn lifecycle statuses "
        f"{expected_statuses!r}, got {statuses!r}. Claude-native must rely "
        f"fully on the PTY watcher, Codex-native must keep only the runner "
        f"running edge, and non-terminal harnesses must keep the full runner "
        f"lifecycle source."
    )


@pytest.mark.asyncio
async def test_events_interrupt_on_native_session_503_skips_cleanup_when_inject_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` interrupt returns 503 and skips cleanup when
    ``inject_interrupt`` can't reach tmux.

    Sister to the happy-path test. The contract is: if the runner
    can't actually deliver Escape (e.g. tmux pane gone, bridge dir
    not yet advertised), it must not (a) persist any
    ``[System: interrupted]`` marker (native never appends one — this
    also confirms the 503 early-return doesn't) and (b) publish
    ``session.status: idle`` — that would lie to the web UI ("we
    stopped it") while Claude keeps generating. The right signal is a
    503 so the caller can surface a failure (the spinner staying is
    correct).

    This was previously pinned by an AP-side test
    (``..._skips_idle_publish_on_runner_failure``); after the
    refactor the responsibility lives on the runner, so the
    invariant is pinned here.
    """
    from omnigent.runner.app import _session_event_queues_ref, _session_histories_ref
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(bridge_dir: Any, *, timeout_s: float) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "inject_interrupt", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "57ac398df7e3972b95ddba8d6109f396",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        int_resp = await client.post(
            "/v1/sessions/57ac398df7e3972b95ddba8d6109f396/events",
            json={"type": "interrupt"},
        )

        captured_history = list(_session_histories_ref.get("57ac398df7e3972b95ddba8d6109f396", []))
        queue = _session_event_queues_ref.get("57ac398df7e3972b95ddba8d6109f396")
        assert queue is not None, (
            "Session creation should have initialized the event queue; "
            "the failure path still needs the queue to exist so we can "
            "assert nothing was published into it."
        )
        queued_events: list[dict[str, Any]] = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    # 1) The route must surface the failure as 503 so the caller
    # treats the cancel as not delivered. 204 would let the caller
    # claim success and (e.g.) clear the spinner client-side.
    assert int_resp.status_code == 503, (
        f"Native interrupt with inject_interrupt failure must return "
        f"503; got {int_resp.status_code}: {int_resp.text}"
    )
    body = int_resp.json()
    assert body.get("error") == "claude_native_interrupt_failed", (
        f"503 body must carry the bridge-failure error code; got {body!r}"
    )

    # 2) No [System: interrupted] marker must be persisted. Native never
    # appends one; this additionally guards the failure path against a
    # reorder bug that appended the marker before the 503 early return.
    markers = [
        h
        for h in captured_history
        if h.get("type") == "message"
        and h.get("role") == "user"
        and any("interrupted" in (b.get("text") or "").lower() for b in h.get("content", []))
    ]
    assert markers == [], (
        f"No [System: interrupted] marker should be persisted on the "
        f"inject_interrupt failure path; got {markers!r}. "
        f"If non-empty, _append_cancellation_items fired before the "
        f"503 early return — likely a reordering bug in "
        f"_handle_claude_native_interrupt."
    )

    # 3) No session.status: idle on the failure path. Idle would
    # tell the web UI the cancel landed — the spinner clearing while
    # Claude keeps generating is exactly the misleading state we
    # need to avoid.
    status_idle = [
        e for e in queued_events if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert status_idle == [], (
        f"No session.status: idle should be enqueued when the Escape "
        f"injection failed; got {status_idle!r}. "
        f"If non-empty, _publish_event fired before the 503 early "
        f"return — same reordering concern as the marker."
    )
