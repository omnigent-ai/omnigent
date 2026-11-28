"""A launch-timeout failure is provisional; a later real result must reach the parent.

The launch-liveness reaper fails a dispatch that never acknowledged its start,
but that ``failed`` is only a guess: a steered claude-native child relays no
running edge while it works. These tests drive the runner's real HTTP routes
and the production ``sys_read_inbox`` drain, so the child's genuine completion
replaces the guess even after the parent has already read it, reaches the
inbox, wakes the parent, and only then finalizes the dispatch's cleanup.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.inner.executor import NativeTurnAcceptance
from omnigent.runner import create_runner_app, subagent_work
from omnigent.runner.tool_dispatch import execute_tool
from tests.runner.conftest import (
    _BlockingHarnessClient,
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient


class _WakeRecordingServerClient(NullServerClient):
    """Records parent wake notices and child label receipts; allows inbox policy."""

    class _AllowVerdict(NullServerClient._Response):
        def json(self) -> dict[str, Any]:
            return {"result": "POLICY_ACTION_UNSPECIFIED"}

    def __init__(self, parent_id: str) -> None:
        self._parent_events_path = f"/v1/sessions/{parent_id}/events"
        self.notices: list[str] = []
        self.label_patches: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> Any:
        if url.rstrip("/").endswith(self._parent_events_path):
            body = kwargs.get("json") or {}
            try:
                text = body["data"]["content"][0]["text"]
            except (KeyError, IndexError, TypeError):
                text = ""
            self.notices.append(text)
        if url.rstrip("/").endswith("/policies/evaluate"):
            return self._AllowVerdict()
        return await super().post(url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> Any:
        labels = (kwargs.get("json") or {}).get("labels")
        if isinstance(labels, dict):
            self.label_patches.append(labels)
        return await super().patch(url, **kwargs)

    def wakes(self) -> list[str]:
        return [notice for notice in self.notices if "finished (" in notice]


async def _wait_until(condition: Callable[[], bool], message: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.02)


def _turn_frames(
    response_id: str,
    native_acceptance: NativeTurnAcceptance | dict[str, Any] | str | None = None,
    *,
    include_null_acceptance: bool = False,
) -> list[str]:
    completed: dict[str, Any] = {
        "type": "response.completed",
        "response": {"id": response_id, "status": "completed"},
    }
    if native_acceptance is not None or include_null_acceptance:
        completed["native_acceptance"] = (
            asdict(native_acceptance)
            if isinstance(native_acceptance, NativeTurnAcceptance)
            else native_acceptance
        )
    return [
        _sse({"type": "response.created", "response": {"id": response_id}}),
        _sse(completed),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt_accepted", "delayed_previous_acceptance"),
    [(False, False), (True, False), (True, True)],
    ids=["unacknowledged-launch", "silent-codex", "silent-codex-after-stale-finalizer"],
)
async def test_reaped_native_launch_must_not_discard_childs_real_completion(
    monkeypatch: pytest.MonkeyPatch, prompt_accepted: bool, delayed_previous_acceptance: bool
) -> None:
    """A launch-reaped dispatch must still deliver the child's later completion.

    The reaper's ``failed`` is a guess with no terminal edge behind it (the
    child may still be running); a genuine ``completed`` edge that arrives
    afterwards must replace it, be delivered, and wake the parent, or the
    parent never learns the work actually finished.
    """
    from omnigent.runner import app as runner_app

    monkeypatch.delenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", raising=False)
    budget_s = subagent_work._DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S
    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    child_agent_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    turn_streamed = asyncio.Event()
    native_acceptance = NativeTurnAcceptance(child_id, "thread_review", "codex_turn_review")
    old_acceptance = NativeTurnAcceptance(child_id, "thread_review", "codex_turn_previous")
    delivery_gate = asyncio.Event()
    clock = [1000.0]
    harness = (
        _BlockingHarnessClient(
            _turn_frames(
                "resp_previous" if delayed_previous_acceptance else "resp_wake",
                old_acceptance if delayed_previous_acceptance else native_acceptance,
            ),
            delivery_gate,
        )
        if prompt_accepted
        else _ScriptedHarnessClient(
            _turn_frames("resp_wake", native_acceptance if prompt_accepted else None),
            stream_finished=turn_streamed,
        )
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    subagent_work._session_inboxes_ref[parent_id] = inbox
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="codex-native" if prompt_accepted else "claude-native",
        title="impl",
    )

    try:
        if delayed_previous_acceptance:
            async with _runner_client(app) as client:
                resp = await client.post(
                    f"/v1/sessions/{child_id}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": child_agent_id,
                        "model": "test-agent",
                        "harness_override": "codex-native",
                        "content": [{"type": "input_text", "text": "Previous review."}],
                    },
                )
                assert resp.status_code == 202, resp.text
                assert isinstance(harness, _BlockingHarnessClient)
                await asyncio.wait_for(harness.post_seen.wait(), timeout=5.0)
                assert child_id in subagent_work._pending_child_acceptances
                resp = await client.post(
                    f"/v1/sessions/{child_id}/events",
                    json={
                        "type": "external_session_status",
                        "data": {
                            "status": "idle",
                            "turn_outcome": "completed",
                            "response_id": old_acceptance.response_id,
                            "output": "Previous report.",
                        },
                    },
                )
                assert resp.status_code == 204, resp.text
                assert entry.status == "completed"
                delivery_gate.set()
                await _wait_until(
                    lambda: child_id not in app.state.active_turns,
                    "the previous delivery finalizer never ended",
                )
                assert child_id not in subagent_work._pending_child_acceptances
                assert child_id not in subagent_work._children_accepted_turns
                assert entry.awaiting_child_start_since is None
                await _wait_until(
                    lambda: any("finished (completed)" in n for n in server_client.wakes()),
                    "the previous result never woke its parent",
                )
                previous_report = await execute_tool(
                    tool_name="sys_read_inbox",
                    arguments="{}",
                    server_client=server_client,
                    conversation_id=parent_id,
                    session_inbox=inbox,
                )
                assert "Previous report." in previous_report
                assert subagent_work.get_subagent_work(child_id) is None
                assert server_client.label_patches == [
                    {subagent_work.SUBAGENT_DELIVERED_ID_LABEL_KEY: entry.work_id}
                ]
                resp = await client.post(
                    f"/v1/sessions/{parent_id}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": uuid.uuid4().hex,
                        "model": "test-agent",
                        "harness": "openai-agents",
                        "content": [
                            {"type": "input_text", "text": "sub-agent finished (completed)"}
                        ],
                    },
                )
                assert resp.status_code == 202, resp.text
                await _wait_until(
                    lambda: parent_id not in app.state.active_turns,
                    "the previous result's parent wake turn never ended",
                )
                harness._sse_frames = _turn_frames("resp_current", native_acceptance)
                harness.post_seen.clear()
        prior_receipts = server_client.label_patches[:]
        earliest_start = latest_start = entry.created_at
        if prompt_accepted:
            monkeypatch.setattr(subagent_work.time, "time", lambda: clock[0])
            delivery_gate.clear()
            async with _runner_client(app) as client:
                earliest_start = time.time()
                resp = await client.post(
                    f"/v1/sessions/{child_id}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": child_agent_id,
                        "model": "test-agent",
                        "harness_override": "codex-native",
                        "content": [{"type": "input_text", "text": "Double-check the change."}],
                    },
                )
                assert resp.status_code == 202, resp.text
                assert isinstance(harness, _BlockingHarnessClient)
                await asyncio.wait_for(harness.post_seen.wait(), timeout=5.0)
                assert child_id in subagent_work._pending_child_acceptances
                assert entry.awaiting_child_start_since is None
                # Receipt delivery is delayed; the runner has not finalized it yet.
                clock[0] = 1100.0
                delivery_gate.set()
                await _wait_until(
                    lambda: child_id not in app.state.active_turns,
                    "the codex-native delivery turn never ended",
                )
                latest_start = time.time()
                assert subagent_work._children_accepted_turns[child_id].accepted_at == latest_start
                assert child_id not in subagent_work._pending_child_acceptances
                if delayed_previous_acceptance:
                    # The current acceptance precedes the parent's fresh registration.
                    previous_work_id = entry.work_id
                    entry = subagent_work.register_subagent_work(
                        parent_session_id=parent_id,
                        child_session_id=child_id,
                        agent="codex-native",
                        title="impl",
                    )
                    entry.status = "running"
                    subagent_work.apply_child_start_evidence(entry)
                    assert entry.work_id != previous_work_id
                    assert entry.native_acceptance == native_acceptance
            assert harness.posted_bodies, "the brief never reached the codex-native harness"
            assert entry.status == "running"
            # Registration and native acceptance may predate runner finalization.
            entry.created_at -= 600.0
            assert entry.awaiting_child_start_since == latest_start

        assert (
            subagent_work.reap_stalled_subagent_launches(
                now=earliest_start + budget_s - 1.0,
                mark_terminal=app.state.mark_subagent_terminal_and_wake,
            )
            == []
        ), "the bound fired before its budget elapsed"
        if prompt_accepted:
            assert (
                subagent_work.reap_stalled_subagent_launches(
                    now=latest_start + budget_s - 1.0,
                    mark_terminal=app.state.mark_subagent_terminal_and_wake,
                )
                == []
            ), "the delivery delay consumed the finalization-based budget"
        reaped = subagent_work.reap_stalled_subagent_launches(
            now=latest_start + budget_s,
            mark_terminal=app.state.mark_subagent_terminal_and_wake,
        )
        assert reaped == [entry]
        assert entry.status == "failed"
        assert entry.launch_timed_out
        await _wait_until(
            lambda: any("finished (failed)" in notice for notice in server_client.wakes()),
            "the reaper's failure never woke the parent",
        )
        assert not inbox.empty()
        # The parent reads the notice through the production drain, exactly as
        # its wake turn would.
        notice = await execute_tool(
            tool_name="sys_read_inbox",
            arguments="{}",
            server_client=server_client,  # type: ignore[arg-type]
            conversation_id=parent_id,
            session_inbox=inbox,
        )
        failure = "reported no turn start" if prompt_accepted else "no start acknowledgment"
        assert failure in notice, notice
        assert inbox.empty()
        # That failure is only the reaper's guess, so draining it must not
        # finalize the dispatch: the entry stays registered (still flagged) and
        # no delivered receipt is written, or the child's real edge that follows
        # is acknowledged as "already delivered" and dropped.
        assert subagent_work.get_subagent_work(child_id) is entry, (
            "draining the reaper's guess unregistered the dispatch, so the child's "
            "real completion can no longer replace it"
        )
        assert entry.launch_timed_out
        assert server_client.label_patches == prior_receipts, (
            "draining the reaper's guess wrote a delivered receipt, so a runner "
            "restart would never recover the child's real result either"
        )

        async with _runner_client(app) as client:
            # The parent's wake turn: it reads the failure notice, sees the child
            # is still mid-turn, and goes idle again to keep waiting.
            resp = await client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": uuid.uuid4().hex,
                    "model": "test-agent",
                    "harness": "openai-agents",
                    "content": [{"type": "input_text", "text": "sub-agent finished (failed)"}],
                },
            )
            assert resp.status_code == 202, resp.text
            await _wait_until(
                lambda: parent_id not in app.state.active_turns,
                "the parent's wake turn never ended",
            )
            wakes_before_completion = len(server_client.wakes())

            if prompt_accepted:
                # The silent child was only slow to report its turn starting.
                resp = await client.post(
                    f"/v1/sessions/{child_id}/events",
                    json={
                        "type": "external_session_status",
                        "data": {
                            "status": "running",
                            "response_id": native_acceptance.response_id,
                        },
                    },
                )
                assert resp.status_code == 204, resp.text

            # The child genuinely finishes: its real Stop-hook completion edge
            # over the runner's HTTP event route.
            final_report = "FINAL REPORT: implemented the feature and wrote tests"
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {
                        "status": "idle",
                        "output": final_report,
                        **(
                            {"response_id": native_acceptance.response_id}
                            if prompt_accepted
                            else {}
                        ),
                    },
                },
            )
            assert resp.status_code == 204, resp.text

            entry = subagent_work.get_subagent_work(child_id)
            assert entry is not None
            assert entry.status == "completed", (
                f"work entry is {entry.status!r} after the child's real completion "
                f"edge: a launch-reaper 'failed' is a guess with no terminal edge "
                f"behind it, so a genuine 'completed' must replace it. Instead the "
                f"already-delivered early return in mark_subagent_work_terminal "
                f"discarded the completion."
            )
            assert entry.output == final_report, (
                f"work entry output is {entry.output!r}: the child's final report "
                f"was dropped and the stale reaper text kept."
            )

            await _wait_until(
                lambda: len(server_client.wakes()) > wakes_before_completion,
                "the parent was never woken for the completion: the wake POST is "
                "the sole signal that rouses an idle parent to drain its inbox.",
            )
            assert "finished (completed)" in server_client.wakes()[-1]

            report = await execute_tool(
                tool_name="sys_read_inbox",
                arguments="{}",
                server_client=server_client,  # type: ignore[arg-type]
                conversation_id=parent_id,
                session_inbox=inbox,
            )
            assert final_report in report, (
                "the parent inbox never received the child's completion: the "
                f"reaper's failed guess blocked delivery of the real result. Drain: {report!r}"
            )
            # The genuine result finalizes what the guess deferred: the dispatch
            # is unregistered and its delivered receipt written.
            assert subagent_work.get_subagent_work(child_id) is None
            assert server_client.label_patches == [
                *prior_receipts,
                {subagent_work.SUBAGENT_DELIVERED_ID_LABEL_KEY: entry.work_id},
            ]
    finally:
        delivery_gate.set()
        subagent_work.unregister_subagent_work_for_session(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness_name", "reported_status", "receipt_kind"),
    [
        ("claude-native", None, "missing"),
        ("codex-native", "running", "valid"),
        ("codex-native", "waiting", "valid"),
        ("codex-native", "running", "valid-stream"),
        ("codex-native", None, "missing"),
        ("codex-native", None, "explicit-null"),
        ("codex-native", None, "malformed"),
        ("codex-native", None, "other-session"),
        ("codex-native", None, "missing-thread"),
    ],
    ids=[
        "claude-native-steer",
        "codex-native-reported-start",
        "codex-native-reported-approval",
        "valid-receipt-stream",
        "legacy-codex",
        "null-receipt-stream",
        "malformed-receipt",
        "other-session-receipt",
        "missing-thread-receipt",
    ],
)
async def test_native_prompt_delivery_takes_dispatch_out_of_launching(
    monkeypatch: pytest.MonkeyPatch,
    harness_name: str,
    reported_status: str | None,
    receipt_kind: str,
) -> None:
    """The runner's own verified prompt delivery is the launch acknowledgment.

    A follow-up steered into a busy claude-native child is typed into the pane
    by a runner-driven turn, and claude-native relays no ``running`` edge for
    it, so that turn ending cleanly is the only proof the child took the
    message. The dispatch must leave ``launching`` there, or the launch-liveness
    reaper fails a working child. A codex-native child whose forwarder reports
    the turn start, or an approval wait, after Codex accepted the prompt is
    working too.
    """
    from omnigent.runner import app as runner_app

    # No real Claude Code bridge exists here, so the relay's tools/list
    # notification would spin forever waiting for one.
    monkeypatch.setattr(claude_native_bridge, "post_tools_changed", lambda *a, **k: None)

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    turn_streamed = asyncio.Event()
    receipt = NativeTurnAcceptance(child_id, "thread_steer", "codex_turn_steer")
    raw_receipt = (
        None
        if receipt_kind in ("missing", "explicit-null")
        else "not-an-acceptance"
        if receipt_kind == "malformed"
        else {**asdict(receipt), "session_id": "conv_other"}
        if receipt_kind == "other-session"
        else {"session_id": child_id, "response_id": receipt.response_id}
        if receipt_kind == "missing-thread"
        else receipt
    )
    harness = _ScriptedHarnessClient(
        _turn_frames(
            "resp_steer", raw_receipt, include_null_acceptance=receipt_kind == "explicit-null"
        ),
        stream_finished=turn_streamed,
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    subagent_work._session_inboxes_ref[parent_id] = inbox
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        async with _runner_client(app) as client:
            # The parent's follow-up: the runner injects it into the child's
            # native pane as a background turn that ends once the paste is
            # verifiably submitted.
            streaming = receipt_kind in ("explicit-null", "valid-stream")
            resp = await client.post(
                f"/v1/sessions/{child_id}/events" + ("?stream=true" if streaming else ""),
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": uuid.uuid4().hex,
                    "model": "test-agent",
                    "harness_override": harness_name,
                    "content": [{"type": "input_text", "text": "status check"}],
                },
            )
            assert resp.status_code == (200 if streaming else 202), resp.text
            if streaming:
                assert '"native_acceptance"' not in resp.text
            await asyncio.wait_for(turn_streamed.wait(), timeout=5.0)
            await _wait_until(
                lambda: child_id not in app.state.active_turns,
                "the steered native turn never ended",
            )
            if receipt_kind == "valid-stream":
                assert entry.native_acceptance == receipt
            if reported_status is not None:
                resp = await client.post(
                    f"/v1/sessions/{child_id}/events",
                    json={
                        "type": "external_session_status",
                        "data": {
                            "status": reported_status,
                            "response_id": receipt.response_id,
                        },
                    },
                )
                assert resp.status_code == 204, resp.text

        assert harness.posted_bodies, "the follow-up never reached the native harness"
        assert entry.status == "running", (
            f"work entry is {entry.status!r} after the runner delivered the "
            f"follow-up to the {harness_name} child: that verified delivery is "
            f"first-hand proof the child took its prompt, so the dispatch must "
            f"leave 'launching'."
        )
        assert (
            subagent_work.reap_stalled_subagent_launches(
                now=entry.created_at + 900.0, timeout_s=180.0
            )
            == []
        ), "the launch-liveness reaper failed a child that had already taken its prompt"
        assert entry.status == "running"
        assert inbox.empty()
        assert entry.awaiting_child_start_since is None
        assert child_id not in subagent_work._pending_child_acceptances
        events = _drain_session_event_queue(runner_app._session_event_queues_ref.get(child_id))
        completed = [event for event in events if event.get("type") == "response.completed"]
        assert len(completed) == 1
        assert completed[0]["response"]["id"] == "resp_steer"
        assert "native_acceptance" not in completed[0]
    finally:
        subagent_work.unregister_subagent_work_for_session(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("inbox_present_at_reap", [True, False], ids=["drained", "undelivered"])
async def test_bare_native_idle_does_not_respend_the_reapers_guess(
    monkeypatch: pytest.MonkeyPatch, inbox_present_at_reap: bool
) -> None:
    """A quiescence ``idle`` from a reaped claude-native child settles nothing.

    Claude's forwarder stamps ``turn_completed`` on a finished turn, so a bare
    ``idle`` only retries delivery of an outcome still awaiting it. Re-reporting
    the recorded launch-timeout ``failed`` as the child's own terminal edge
    would spend the provisional flag (and, once drained, re-deliver the guess),
    dropping the genuine completion that follows -- whether the parent already
    read the guess or its inbox was missing when the reaper fired.
    """
    from omnigent.runner import app as runner_app

    monkeypatch.setattr(claude_native_bridge, "post_tools_changed", lambda *a, **k: None)

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    turn_streamed = asyncio.Event()
    harness = _ScriptedHarnessClient(_turn_frames("resp_steer"), stream_finished=turn_streamed)
    server_client = _WakeRecordingServerClient(parent_id)
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    if inbox_present_at_reap:
        subagent_work._session_inboxes_ref[parent_id] = inbox
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        async with _runner_client(app) as client:
            assert subagent_work.reap_stalled_subagent_launches(
                now=entry.created_at + 200.0,
                timeout_s=180.0,
                mark_terminal=app.state.mark_subagent_terminal_and_wake,
            ) == [entry]
            assert entry.status == "failed" and entry.launch_timed_out
            if inbox_present_at_reap:
                # The parent is woken, reads the notice through the production
                # drain, and its wake turn goes idle again.
                await _wait_until(
                    lambda: any("finished (failed)" in n for n in server_client.wakes()),
                    "the reaper's failure never woke the parent",
                )
                notice = await execute_tool(
                    tool_name="sys_read_inbox",
                    arguments="{}",
                    server_client=server_client,  # type: ignore[arg-type]
                    conversation_id=parent_id,
                    session_inbox=inbox,
                )
                assert "no start acknowledgment" in notice, notice
                assert subagent_work.get_subagent_work(child_id) is entry
                resp = await client.post(
                    f"/v1/sessions/{parent_id}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": uuid.uuid4().hex,
                        "model": "test-agent",
                        "harness": "openai-agents",
                        "content": [{"type": "input_text", "text": "sub-agent finished (failed)"}],
                    },
                )
                assert resp.status_code == 202, resp.text
                await _wait_until(
                    lambda: parent_id not in app.state.active_turns,
                    "the parent's wake turn never ended",
                )
            else:
                assert not entry.delivered and not server_client.wakes()

            # The steered follow-up records the child's harness as claude-native;
            # its clean end acknowledges nothing for an already-settled entry.
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": uuid.uuid4().hex,
                    "model": "test-agent",
                    "harness_override": "claude-native",
                    "content": [{"type": "input_text", "text": "status check"}],
                },
            )
            assert resp.status_code == 202, resp.text
            await asyncio.wait_for(turn_streamed.wait(), timeout=5.0)
            await _wait_until(
                lambda: child_id not in app.state.active_turns,
                "the steered native turn never ended",
            )
            assert entry.status == "failed"
            wakes_before = len(server_client.wakes())

            # The pane goes quiet mid-turn: the PTY watcher's bare idle.
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={"type": "external_session_status", "data": {"status": "idle"}},
            )
            if inbox_present_at_reap:
                assert resp.status_code == 204, resp.text
                assert inbox.empty(), (
                    "a bare quiescence idle re-delivered the reaper's guess to the parent "
                    f"as if the child had reported it: {inbox.get_nowait()!r}"
                )
            else:
                # No parent inbox yet: the retry cannot confirm delivery, so the
                # forwarder is told to try again later.
                assert resp.status_code in (204, 503), resp.text
                assert not entry.delivered
            assert entry.status == "failed" and entry.launch_timed_out, (
                "a bare quiescence idle spent the provisional launch-timeout flag"
            )
            assert len(server_client.wakes()) == wakes_before
            if not inbox_present_at_reap:
                # The parent (re)initializes before the child finishes.
                subagent_work._session_inboxes_ref[parent_id] = inbox

            # The genuine completion: Claude's Stop hook stamps turn_completed.
            final_report = "FINAL REPORT: implemented the feature and wrote tests"
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": final_report, "turn_completed": True},
                },
            )
            assert resp.status_code == 204, resp.text
            assert entry.status == "completed" and entry.output == final_report, (
                f"work entry is {entry.status!r} with output {entry.output!r}: the "
                "child's real completion did not supersede the reaper's guess"
            )
            await _wait_until(
                lambda: len(server_client.wakes()) > wakes_before,
                "the parent was never woken for the completion",
            )
            report = await execute_tool(
                tool_name="sys_read_inbox",
                arguments="{}",
                server_client=server_client,  # type: ignore[arg-type]
                conversation_id=parent_id,
                session_inbox=inbox,
            )
            assert final_report in report, report
            assert subagent_work.get_subagent_work(child_id) is None
    finally:
        subagent_work.unregister_subagent_work(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)
