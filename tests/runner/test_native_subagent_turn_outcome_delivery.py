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
- A delayed cancel bound to an old dispatch never settles a newer send that
  reused the child session.

The status tests drive the runner's real HTTP event route -- the same
``external_session_status`` POSTs the native forwarders emit -- so they
exercise the genuine edge-processing path, not internal helpers.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import create_runner_app, subagent_work
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
        subagent_work._session_inboxes_ref[self.parent_id] = self.inbox
        subagent_work.register_subagent_work(
            parent_session_id=self.parent_id,
            child_session_id=self.child_id,
            agent="researcher",
            title="cite-check",
        )

    def close(self) -> None:
        subagent_work.unregister_subagent_work(self.child_id)
        subagent_work._session_inboxes_ref.pop(self.parent_id, None)
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
@pytest.mark.parametrize("harness", ["claude-native", "pi-native"])
async def test_quiescence_idle_for_outcome_confirming_harness_delivers_nothing(
    harness: str,
) -> None:
    """Quiescence or compaction clears runner activity without completing the dispatch."""
    rig = _Rig(harness)
    try:
        async with _runner_client(rig.app) as client:
            await rig.create_child_session(client)
            for data in ({"status": "running"}, {"status": "idle", "output": "partial text"}):
                resp = await rig.post_status(client, data)
                assert resp.status_code == 204, resp.text
                assert rig.app.state.native_pane_status[rig.child_id] == data["status"]
                assert rig.app.state.has_active_work() == (data["status"] == "running")
        assert rig.drained() == [], "a bare quiescence idle must not deliver a terminal status"
        entry = subagent_work.get_subagent_work(rig.child_id)
        assert entry is not None and entry.status not in ("completed", "cancelled", "failed")
    finally:
        rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-native", "pi-native"])
async def test_turn_completed_idle_delivers_completed_result(harness: str) -> None:
    """The forwarder's confirmed turn-end edge still completes the dispatch."""
    rig = _Rig(harness)
    try:
        async with _runner_client(rig.app) as client:
            await rig.create_child_session(client)
            for _ in range(2):  # A lost acknowledgment retries the same completed result.
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


def _accept_claude_interrupt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make the claude-native Escape inject succeed without a real pane."""
    del tmp_path
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client
        return session_id

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(claude_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)


def _accept_opencode_interrupt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make OpenCode's native abort report that it stopped active work."""
    import omnigent.harnesses.opencode_native.app_server as opencode_app_server
    import omnigent.harnesses.opencode_native.bridge as opencode_bridge

    class _AbortingClient:
        async def abort(self, session_id: str) -> bool:
            return True

        async def aclose(self) -> None:
            pass

    state = opencode_bridge.OpenCodeNativeBridgeState(
        session_id="unused", server_base_url="http://127.0.0.1:1", opencode_session_id="ses_oc"
    )

    def _no_cli(opencode_path: str | None = None) -> str:
        raise opencode_app_server.OpenCodeCliNotFoundError(opencode_path or "opencode")

    # Keep session creation off the real home and from launching an installed ``opencode``.
    monkeypatch.setattr(opencode_bridge, "_BRIDGE_ROOT", tmp_path / "opencode-native")
    monkeypatch.setattr(opencode_app_server, "find_opencode_cli", _no_cli)
    monkeypatch.setattr(opencode_bridge, "read_bridge_state", lambda bridge_dir: state)
    monkeypatch.setattr(opencode_app_server, "client_for_state", lambda **_: _AbortingClient())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "accept_interrupt"),
    [("claude-native", _accept_claude_interrupt), ("opencode-native", _accept_opencode_interrupt)],
)
async def test_interrupt_then_quiescence_idle_delivers_cancelled_with_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, harness: str, accept_interrupt: Any
) -> None:
    """The interrupt defers the wake; the settling idle reports cancelled + output.

    No terminal status may be delivered when the interrupt is handled. When the
    pane then settles without a confirmed completion, the dispatch is cancelled
    -- and the output the edge carried (a survivor's real result, or partial
    work) must reach the parent instead of being discarded.
    """
    accept_interrupt(monkeypatch, tmp_path)

    rig = _Rig(harness)
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
            subagent_work._session_inboxes_ref.pop(rig.parent_id, None)
            resp = await rig.post_status(client, {"status": "idle", "output": "partial work"})
            assert resp.status_code == 503, resp.text

            # The forwarder retries the same bare idle after the inbox exists.
            subagent_work._session_inboxes_ref[rig.parent_id] = rig.inbox
            resp = await rig.post_status(client, {"status": "idle", "output": "partial work"})
            assert resp.status_code == 204, resp.text
        delivered = rig.drained()
        assert [(item["status"], item["output"]) for item in delivered] == [
            ("cancelled", "partial work")
        ]
    finally:
        rig.close()


async def test_grace_timer_cancel_does_not_settle_reused_dispatch() -> None:
    """A delayed cancel bound to an old dispatch never settles a newer send.

    Interrupt A leaves a grace-timer cancel bound to A's ``work_id``. Before it
    fires, a new send registers turn B on the same child session. The cancel
    (``only_if_work_id`` = A) must be dropped as superseded, leaving B live so
    it can still complete.
    """

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    try:
        entry_a = subagent_work.register_subagent_work(
            parent_session_id=parent_id,
            child_session_id=child_id,
            agent="researcher",
            title="turn-a",
        )
        work_id_a = entry_a.work_id

        # The reused child session starts turn B (a fresh dispatch).
        entry_b = subagent_work.register_subagent_work(
            parent_session_id=parent_id,
            child_session_id=child_id,
            agent="researcher",
            title="turn-b",
        )
        assert entry_b.work_id != work_id_a

        # A's delayed grace-timer cancel fires, still bound to A's work_id.
        superseded = subagent_work.mark_subagent_work_terminal(
            child_id, status="cancelled", output=None, only_if_work_id=work_id_a
        )
        assert not superseded.delivered_now
        assert subagent_work.get_subagent_work(child_id).status == "launching"

        # B completes normally and reaches the parent.
        done = subagent_work.mark_subagent_work_terminal(
            child_id, status="completed", output="B result"
        )
        assert done.delivered_now

        statuses = []
        while not inbox.empty():
            statuses.append(inbox.get_nowait()["status"])
        assert statuses == ["completed"]
    finally:
        subagent_work.unregister_subagent_work(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


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
