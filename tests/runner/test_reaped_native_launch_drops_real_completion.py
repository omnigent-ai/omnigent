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
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.runner import create_runner_app, subagent_work
from omnigent.runner.tool_dispatch import execute_tool
from tests.runner.conftest import (
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


def _turn_frames(response_id: str) -> list[str]:
    return [
        _sse({"type": "response.created", "response": {"id": response_id}}),
        _sse({"type": "response.completed", "response": {"id": response_id}}),
    ]


@pytest.mark.asyncio
async def test_reaped_native_launch_must_not_discard_childs_real_completion() -> None:
    """A launch-reaped dispatch must still deliver the child's later completion.

    The reaper's ``failed`` is a guess with no terminal edge behind it (the
    child may still be running); a genuine ``completed`` edge that arrives
    afterwards must replace it, be delivered, and wake the parent, or the
    parent never learns the work actually finished.
    """
    from omnigent.runner import app as runner_app

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _WakeRecordingServerClient(parent_id)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient(_turn_frames("resp_wake"))),  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    subagent_work._session_inboxes_ref[parent_id] = inbox
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        # The steered native turn relays no running edge, so the entry is still
        # "launching" when the budget elapses: the reaper fails it, delivers
        # that guess to the parent inbox and wakes the parent.
        reaped = subagent_work.reap_stalled_subagent_launches(
            now=entry.created_at + 200.0,
            timeout_s=180.0,
            mark_terminal=app.state.mark_subagent_terminal_and_wake,
        )
        assert reaped == [entry]
        assert entry.status == "failed"
        await _wait_until(
            lambda: any("finished (failed)" in notice for notice in server_client.wakes()),
            "the reaper's failure never woke the parent",
        )
        # The parent reads the notice through the production drain, exactly as
        # its wake turn would.
        notice = await execute_tool(
            tool_name="sys_read_inbox",
            arguments="{}",
            server_client=server_client,  # type: ignore[arg-type]
            conversation_id=parent_id,
            session_inbox=inbox,
        )
        assert "no start acknowledgment" in notice, notice
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
        assert server_client.label_patches == [], (
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

            # The child genuinely finishes: its real Stop-hook completion edge
            # over the runner's HTTP event route.
            final_report = "FINAL REPORT: implemented the feature and wrote tests"
            resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": final_report},
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
                {subagent_work.SUBAGENT_DELIVERED_ID_LABEL_KEY: entry.work_id}
            ]
    finally:
        subagent_work.unregister_subagent_work(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)


@pytest.mark.asyncio
async def test_native_prompt_delivery_takes_dispatch_out_of_launching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner's own verified prompt delivery is the launch acknowledgment.

    A follow-up steered into a busy claude-native child is typed into the pane
    by a runner-driven turn, and claude-native relays no ``running`` edge for
    it, so that turn ending cleanly is the only proof the child took the
    message. The dispatch must leave ``launching`` there, or the launch-liveness
    reaper fails a working child.
    """
    from omnigent.runner import app as runner_app

    # No real Claude Code bridge exists here, so the relay's tools/list
    # notification would spin forever waiting for one.
    monkeypatch.setattr(claude_native_bridge, "post_tools_changed", lambda *a, **k: None)

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    turn_streamed = asyncio.Event()
    harness = _ScriptedHarnessClient(_turn_frames("resp_steer"), stream_finished=turn_streamed)
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

        assert harness.posted_bodies, "the follow-up never reached the native harness"
        assert entry.status == "running", (
            f"work entry is {entry.status!r} after the runner delivered the "
            f"follow-up into the child's pane: that verified delivery is first-hand "
            f"proof the child is working, so the dispatch must leave 'launching' "
            f"even though claude-native relays no running edge for a steered turn."
        )
        assert (
            subagent_work.reap_stalled_subagent_launches(
                now=entry.created_at + 900.0, timeout_s=180.0
            )
            == []
        ), "the launch-liveness reaper failed a child that had already taken its prompt"
        assert entry.status == "running"
        assert inbox.empty()
    finally:
        subagent_work.unregister_subagent_work(child_id)
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
