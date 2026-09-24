"""Native sub-agent terminal delivery must report outcomes, not guesses.

``external_session_status: idle`` historically meant both "the turn finished"
and "the turn stopped early", and the runner mapped it to ``completed``
unconditionally; the interrupt path likewise delivered ``cancelled`` before
the agent was confirmed stopped. These tests pin the corrected contract:

- For a harness whose forwarder confirms turn outcomes (claude-native stamps
  ``turn_completed`` on its ``Stop``-hook edge), a bare quiescence ``idle``
  delivers nothing; only a confirmed edge completes the dispatch.
- Harnesses without that plumbing keep the legacy idle -> completed mapping.
- An interrupt defers the parent wake; the next unconfirmed idle settles the
  dispatch as ``cancelled`` while preserving the output it carried.
- A harness-confirmed completion corrects an already-delivered ``cancelled``.

The status tests drive the runner's real HTTP event route -- the same
``external_session_status`` POSTs the native forwarders emit -- so they
exercise the genuine edge-processing path, not internal helpers.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


def _native_app(harness: str) -> Any:
    """Build a runner app whose sessions resolve to *harness* specs."""
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    return create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


class _Rig:
    """One registered parent-inbox/child-work pair on a fresh runner app."""

    def __init__(self, harness: str) -> None:
        from omnigent.runner import app as runner_app

        self.runner_app = runner_app
        self.parent_id = uuid.uuid4().hex
        self.child_id = uuid.uuid4().hex
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.app = _native_app(harness)
        runner_app._session_inboxes_ref[self.parent_id] = self.inbox
        runner_app.register_subagent_work(
            parent_session_id=self.parent_id,
            child_session_id=self.child_id,
            agent="researcher",
            title="cite-check",
        )

    def close(self) -> None:
        self.runner_app.unregister_subagent_work(self.child_id)
        self.runner_app._session_inboxes_ref.pop(self.parent_id, None)
        self.runner_app._session_event_queues_ref.pop(self.parent_id, None)
        self.runner_app._session_event_queues_ref.pop(self.child_id, None)

    def drained(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while not self.inbox.empty():
            items.append(self.inbox.get_nowait())
        return items

    async def create_child_session(self, client: Any) -> None:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": self.child_id, "agent_id": uuid.uuid4().hex},
        )
        assert resp.status_code == 201, resp.text

    async def post_status(self, client: Any, data: dict[str, Any]) -> Any:
        return await client.post(
            f"/v1/sessions/{self.child_id}/events",
            json={"type": "external_session_status", "data": data},
        )


@pytest.mark.asyncio
async def test_quiescence_idle_for_outcome_confirming_harness_delivers_nothing() -> None:
    """A bare idle edge must not report an unfinished claude-native turn completed.

    The claude forwarder stamps ``turn_completed`` on its ``Stop``-hook edge, so
    an idle without it (a quiescence observation, a /clear supersession notice)
    proves nothing about the turn's outcome and must not wake the parent with a
    fabricated ``completed``.
    """
    rig = _Rig("claude-native")
    try:
        async with _runner_client(rig.app) as client:
            await rig.create_child_session(client)
            for data in ({"status": "running"}, {"status": "idle", "output": "partial text"}):
                resp = await rig.post_status(client, data)
                assert resp.status_code == 204, resp.text
        assert rig.drained() == [], "a bare quiescence idle must not deliver a terminal status"
        entry = rig.runner_app.get_subagent_work(rig.child_id)
        assert entry is not None and entry.status not in ("completed", "cancelled", "failed")
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_turn_completed_idle_delivers_completed_result() -> None:
    """The forwarder's confirmed turn-end edge still completes the dispatch."""
    rig = _Rig("claude-native")
    try:
        async with _runner_client(rig.app) as client:
            await rig.create_child_session(client)
            resp = await rig.post_status(
                client, {"status": "idle", "turn_completed": True, "output": "the verdict"}
            )
            assert resp.status_code == 204, resp.text
        delivered = rig.drained()
        assert [(item["status"], item["output"]) for item in delivered] == [
            ("completed", "the verdict")
        ]
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_quiescence_idle_for_legacy_native_harness_still_completes() -> None:
    """Harnesses without turn-outcome plumbing keep the idle -> completed mapping.

    Their quiescence edge is the only completion signal they have; requiring
    ``turn_completed`` from them would strand every dispatch as waiting.
    """
    rig = _Rig("cursor-native")
    try:
        async with _runner_client(rig.app) as client:
            await rig.create_child_session(client)
            resp = await rig.post_status(client, {"status": "idle", "output": "done"})
            assert resp.status_code == 204, resp.text
        delivered = rig.drained()
        assert [(item["status"], item["output"]) for item in delivered] == [("completed", "done")]
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_interrupt_then_quiescence_idle_delivers_cancelled_with_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interrupt defers the wake; the settling idle reports cancelled + output.

    The Escape alone proves nothing, so no terminal status may be delivered when
    the interrupt is handled. When the pane then settles without a confirmed
    completion, the dispatch is cancelled -- and the output the edge carried
    (a survivor's real result, or partial work) must reach the parent instead
    of being discarded.
    """
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client
        return session_id

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(claude_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)

    rig = _Rig("claude-native")
    try:
        async with _runner_client(rig.app) as client:
            await rig.create_child_session(client)
            resp = await client.post(
                f"/v1/sessions/{rig.child_id}/events", json={"type": "interrupt"}
            )
            assert resp.status_code == 204, resp.text
            assert rig.drained() == [], "an unconfirmed interrupt must not wake the parent"

            resp = await rig.post_status(client, {"status": "idle", "output": "the verdict"})
            assert resp.status_code == 204, resp.text
        delivered = rig.drained()
        assert [(item["status"], item["output"]) for item in delivered] == [
            ("cancelled", "the verdict")
        ]
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_settled_outcome_is_redelivered_on_retried_quiescence_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retried idle edge re-attempts delivery of an already-settled outcome.

    The forwarder retries a terminal edge the runner answered 503 (parent inbox
    missing). The retry is a bare quiescence idle, which settles nothing new --
    but it must still push the recorded outcome once the inbox exists, or the
    503-retry contract silently dies for outcome-confirming harnesses.
    """
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client
        return session_id

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(claude_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)

    rig = _Rig("claude-native")
    try:
        async with _runner_client(rig.app) as client:
            await rig.create_child_session(client)
            resp = await client.post(
                f"/v1/sessions/{rig.child_id}/events", json={"type": "interrupt"}
            )
            assert resp.status_code == 204, resp.text

            # First settling idle lands while the parent inbox is gone: the
            # outcome is recorded but delivery cannot be confirmed.
            rig.runner_app._session_inboxes_ref.pop(rig.parent_id, None)
            resp = await rig.post_status(client, {"status": "idle", "output": "partial work"})
            assert resp.status_code == 503, resp.text

            # The forwarder retries the same bare idle after the inbox exists.
            rig.runner_app._session_inboxes_ref[rig.parent_id] = rig.inbox
            resp = await rig.post_status(client, {"status": "idle", "output": "partial work"})
            assert resp.status_code == 204, resp.text
        delivered = rig.drained()
        assert [(item["status"], item["output"]) for item in delivered] == [
            ("cancelled", "partial work")
        ]
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_confirmed_completion_corrects_delivered_cancelled() -> None:
    """A harness-confirmed completion replaces a delivered ``cancelled`` guess.

    The grace-timer ``cancelled`` is a fallback, not a fact; when the agent
    survives and its forwarder later confirms the finished turn, the real
    result must be re-delivered rather than discarded as ALREADY_DELIVERED.
    An unconfirmed completion must keep losing to the delivered status.
    """
    from omnigent.runner import app as runner_app

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[parent_id] = inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="cite-check",
    )
    try:
        ack = runner_app.mark_subagent_work_terminal(child_id, status="cancelled", output=None)
        assert ack.delivered_now

        unconfirmed = runner_app.mark_subagent_work_terminal(
            child_id, status="completed", output="stale guess"
        )
        assert not unconfirmed.delivered_now

        confirmed = runner_app.mark_subagent_work_terminal(
            child_id, status="completed", output="the verdict", turn_confirmed=True
        )
        assert confirmed.delivered_now

        statuses = []
        while not inbox.empty():
            item = inbox.get_nowait()
            statuses.append((item["status"], item["output"]))
        assert statuses == [("cancelled", ""), ("completed", "the verdict")]
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)


def test_cancelled_inbox_line_surfaces_output_before_cancellation() -> None:
    """The parent-readable line keeps a cancelled dispatch's real output."""
    from omnigent.runner.tool_dispatch import _format_async_task_item

    payload = {
        "type": "sub_agent",
        "handle_id": "task1",
        "agent": "researcher",
        "title": "cite-check",
        "status": "cancelled",
        "output": "the verdict",
    }
    line = _format_async_task_item(payload)
    assert line == (
        "[System: sub-agent task task1 cancelled — researcher:cite-check; "
        "output before cancellation: the verdict]"
    )
    silent = _format_async_task_item({**payload, "output": ""})
    assert silent == "[System: sub-agent task task1 cancelled — researcher:cite-check]"
