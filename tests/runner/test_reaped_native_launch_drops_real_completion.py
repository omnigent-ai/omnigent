"""Launch-reaped native dispatch must still deliver a child's real completion.

A parent sends a follow-up to a busy claude-native child. The steered native
turn relays no running/waiting edge, so the runner-local work entry stays
``launching``; once the launch-liveness budget elapses the reaper fails it
("no start acknowledgment"), delivers that guess to the parent inbox and wakes
the parent, which reads the notice and goes idle again. The child then
genuinely finishes and its ``Stop`` hook posts the real
``external_session_status: idle`` edge. That completion must replace the
reaper's guess, reach the parent inbox, and wake the parent again.

On the buggy build ``mark_subagent_work_terminal`` sees an already-delivered
``failed`` entry and returns "already delivered": the real ``completed`` edge
is discarded, the parent inbox never receives the final report, and the parent
hangs forever with the result unread. Upstream of that, the runner's own
verified prompt delivery into the native pane never counted as the launch
acknowledgment, which is what let the reaper fail a working child.

Both tests drive the runner's real HTTP routes -- ``message`` turns for the
parent's wake turn and the steered child turn, and the same
``external_session_status`` POST the claude-native forwarder emits -- so they
exercise the genuine turn-end and edge-processing paths, not internal helpers.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.runner import create_runner_app
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient


class _WakeRecordingServerClient(NullServerClient):
    """Records the text of every wake notice POSTed to the parent session."""

    def __init__(self, parent_id: str) -> None:
        self._parent_events_path = f"/v1/sessions/{parent_id}/events"
        self.notices: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> Any:
        if url.rstrip("/").endswith(self._parent_events_path):
            body = kwargs.get("json") or {}
            try:
                text = body["data"]["content"][0]["text"]
            except (KeyError, IndexError, TypeError):
                text = ""
            self.notices.append(text)
        return await super().post(url, **kwargs)

    def wakes(self) -> list[str]:
        return [notice for notice in self.notices if "finished (" in notice]


async def _wait_until(condition: Callable[[], bool], message: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.02)


def _drain_inbox(queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    drained: list[dict[str, Any]] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    return drained


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
    runner_app._session_inboxes_ref[parent_id] = inbox
    entry = runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        # The steered native turn relays no running edge, so the entry is still
        # "launching" when the budget elapses: the reaper fails it, delivers
        # that guess to the parent inbox and wakes the parent.
        reaped = runner_app.reap_stalled_subagent_launches(
            now=entry.created_at + 200.0,
            timeout_s=180.0,
            mark_terminal=app.state.mark_subagent_terminal_and_wake,
        )
        assert reaped == [entry]
        assert entry.status == "failed"
        reaper_payload = inbox.get_nowait()
        assert reaper_payload["status"] == "failed"
        assert "no start acknowledgment" in str(reaper_payload["output"])
        assert inbox.empty()
        await _wait_until(
            lambda: any("finished (failed)" in notice for notice in server_client.wakes()),
            "the reaper's failure never woke the parent",
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

            entry = runner_app.get_subagent_work(child_id)
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

            delivered_completions = [
                item for item in _drain_inbox(inbox) if item.get("status") == "completed"
            ]
            assert delivered_completions, (
                "the parent inbox never received the child's completion: the "
                "reaper's failed guess blocked delivery of the real result, so the "
                "parent hangs forever with the report unread."
            )
            assert final_report in str(delivered_completions[-1]["output"])

            await _wait_until(
                lambda: len(server_client.wakes()) > wakes_before_completion,
                "the parent was never woken for the completion: the wake POST is "
                "the sole signal that rouses an idle parent to drain its inbox.",
            )
            assert "finished (completed)" in server_client.wakes()[-1]
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)
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
    runner_app._session_inboxes_ref[parent_id] = inbox
    entry = runner_app.register_subagent_work(
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
            runner_app.reap_stalled_subagent_launches(
                now=entry.created_at + 900.0, timeout_s=180.0
            )
            == []
        ), "the launch-liveness reaper failed a child that had already taken its prompt"
        assert entry.status == "running"
        assert inbox.empty()
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)
