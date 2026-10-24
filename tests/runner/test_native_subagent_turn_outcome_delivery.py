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
        entry = subagent_work.get_subagent_work(rig.child_id)
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


def _patch_claude_interrupt(monkeypatch: pytest.MonkeyPatch, *, grace_s: float) -> None:
    """Stub the claude bridge injection and shorten the grace timer for a test."""
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client
        return session_id

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(claude_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", grace_s)


@pytest.mark.parametrize(
    "completion_edge",
    [
        pytest.param({"status": "idle", "turn_completed": True}, id="turn_completed-idle"),
        pytest.param({"status": "idle", "turn_outcome": "completed"}, id="turn_outcome-completed"),
    ],
)
@pytest.mark.asyncio
async def test_confirmed_completion_corrects_grace_cancelled_dispatch(
    completion_edge: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A survivor's confirmed turn-end overturns the grace timer's cancellation.

    The interrupt's grace timer fires first and delivers ``cancelled`` to the
    parent, but the child survived the Escape and finishes the turn. Both
    confirmed-completion edges -- a ``turn_completed`` idle and a
    ``turn_outcome: completed`` idle -- must correct the provisional cancellation
    and reach the parent with the real result, not be dropped as already delivered.
    """
    _patch_claude_interrupt(monkeypatch, grace_s=0.05)

    rig = _Rig("claude-native")
    try:
        async with _runner_client(rig.app) as client:
            await rig.create_child_session(client)
            resp = await client.post(
                f"/v1/sessions/{rig.child_id}/events", json={"type": "interrupt"}
            )
            assert resp.status_code == 204, resp.text

            # No confirming edge follows, so the grace timer fires and tells the
            # parent the dispatch was cancelled.
            await asyncio.sleep(0.2)
            assert [(item["status"], item["output"]) for item in rig.drained()] == [
                ("cancelled", "")
            ]

            # The child actually survived; its forwarder posts the confirmed
            # turn-end completion carrying the real result.
            resp = await rig.post_status(client, {**completion_edge, "output": "the verdict"})
            assert resp.status_code == 204, resp.text
        delivered = rig.drained()
        assert [(item["status"], item["output"]) for item in delivered] == [
            ("completed", "the verdict")
        ], "the survivor's confirmed completion must correct the provisional cancellation"
        assert delivered[0]["corrected_status"] == "cancelled", (
            "the corrected completion must carry the superseded cancellation status"
        )
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_confirmed_completion_corrects_drained_grace_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The correction must survive the parent draining the cancellation notice.

    After the grace timer's ``cancelled`` is delivered, the parent reads its
    inbox and drains it (recording a drained-delivery tombstone). The survivor's
    later confirmed completion must still correct the record and re-reach the
    parent, not be swallowed by the tombstone.
    """
    from omnigent.runner.tool_dispatch import _cleanup_drained_subagent_work

    _patch_claude_interrupt(monkeypatch, grace_s=0.05)

    rig = _Rig("claude-native")
    try:
        async with _runner_client(rig.app) as client:
            await rig.create_child_session(client)
            resp = await client.post(
                f"/v1/sessions/{rig.child_id}/events", json={"type": "interrupt"}
            )
            assert resp.status_code == 204, resp.text

            await asyncio.sleep(0.2)
            cancelled = rig.drained()
            assert [(item["status"], item["output"]) for item in cancelled] == [("cancelled", "")]

            # The parent drains the cancellation: the drain's cleanup records the
            # drained-delivery tombstone for this child.
            for payload in cancelled:
                await _cleanup_drained_subagent_work(payload, server_client=None)
            assert rig.child_id in subagent_work._drained_delivered_subagent_children

            resp = await rig.post_status(
                client, {"status": "idle", "turn_completed": True, "output": "the verdict"}
            )
            assert resp.status_code == 204, resp.text
        assert [(item["status"], item["output"]) for item in rig.drained()] == [
            ("completed", "the verdict")
        ], "a confirmed completion must correct even a drained provisional cancellation"
    finally:
        rig.close()


@pytest.mark.parametrize("drain_before_stop", [True, False], ids=["drained", "registered"])
@pytest.mark.asyncio
async def test_confirmed_stop_blocks_late_completion_after_drain(
    drain_before_stop: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed hard stop makes a cancellation final against a late idle.

    The grace timer delivers an optimistic ``cancelled``. Whether or not the
    parent has already drained it (recording the drained-delivery tombstone),
    the runner then confirms the kill via ``stop_session``. A late
    ``turn_completed`` idle -- a straggler edge from the torn-down pane -- must
    not resurrect the dispatch as a completion; the confirmed abort stands. The
    drained path finalizes the retained provisional record while the still-
    registered path confirms the live entry in place.
    """
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.tool_dispatch import _cleanup_drained_subagent_work

    _patch_claude_interrupt(monkeypatch, grace_s=0.05)
    monkeypatch.setattr(claude_bridge, "kill_session", lambda bridge_dir, *, timeout_s: None)

    rig = _Rig("claude-native")
    try:
        async with _runner_client(rig.app) as client:
            await rig.create_child_session(client)
            resp = await client.post(
                f"/v1/sessions/{rig.child_id}/events", json={"type": "interrupt"}
            )
            assert resp.status_code == 204, resp.text

            await asyncio.sleep(0.2)
            cancelled = rig.drained()
            assert [(item["status"], item["output"]) for item in cancelled] == [("cancelled", "")]

            if drain_before_stop:
                # The parent drains the cancellation, recording the drained tombstone.
                for payload in cancelled:
                    await _cleanup_drained_subagent_work(payload, server_client=None)
                assert rig.child_id in subagent_work._drained_delivered_subagent_children

            # The runner confirms the kill, making the cancellation final.
            resp = await client.post(
                f"/v1/sessions/{rig.child_id}/events", json={"type": "stop_session"}
            )
            assert resp.status_code == 204, resp.text
            # The confirmed kill never leaves a correctable optimistic record: a
            # drained record is dropped (the tombstone alone keeps the abort final)
            # and a still-registered entry is confirmed in place.
            assert rig.child_id not in subagent_work._provisional_cancelled_subagent_work
            if drain_before_stop:
                assert rig.child_id in subagent_work._drained_delivered_subagent_children
            else:
                entry = subagent_work.get_subagent_work(rig.child_id)
                assert entry is not None and entry.cancellation_confirmed

            # A late straggler completion edge must not overturn the confirmed abort.
            resp = await rig.post_status(
                client, {"status": "idle", "turn_completed": True, "output": "the verdict"}
            )
            assert resp.status_code == 204, resp.text
        assert rig.drained() == [], (
            "a confirmed cancellation must not be corrected by a late completion"
        )
        assert rig.child_id not in subagent_work._provisional_cancelled_subagent_work
    finally:
        rig.close()
        subagent_work._drained_delivered_subagent_children.discard(rig.child_id)
        subagent_work._provisional_cancelled_subagent_work.pop(rig.child_id, None)


@pytest.mark.asyncio
async def test_legacy_bare_idle_does_not_overturn_optimistic_cancellation() -> None:
    """A legacy harness's bare idle must not launder an optimistic cancellation.

    A legacy harness has no ``turn_completed`` edge, so its bare idle reaches the
    completion branch unconfirmed. When an optimistic grace-timer ``cancelled``
    was already delivered for the dispatch, that idle proves nothing and must not
    correct it into a success.
    """
    rig = _Rig("cursor-native")
    try:
        async with _runner_client(rig.app) as client:
            await rig.create_child_session(client)

            # An optimistic (unconfirmed) cancellation is delivered to the parent.
            ack = subagent_work.mark_subagent_work_terminal(
                rig.child_id, status="cancelled", output="partial work"
            )
            assert ack.delivered_now
            assert [(item["status"], item["output"]) for item in rig.drained()] == [
                ("cancelled", "partial work")
            ]

            # The legacy harness then goes idle without any confirming edge.
            resp = await rig.post_status(client, {"status": "idle", "output": "done"})
            assert resp.status_code == 204, resp.text
        assert rig.drained() == [], "an unconfirmed legacy idle must not correct a cancellation"
        entry = subagent_work.get_subagent_work(rig.child_id)
        assert entry is not None and entry.status == "cancelled"
    finally:
        rig.close()


def test_undelivered_confirmed_cancellation_survives_late_completion() -> None:
    """A confirmed cancellation blocks a late completion even if it never delivered.

    The confirmed kill lands while the parent inbox is briefly missing, so the
    ``cancelled`` is recorded but undelivered. A later ``completed`` for the same
    dispatch must not launder the confirmed abort into a success; it re-delivers
    the cancellation once the inbox returns.
    """
    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="cite-check",
    )
    try:
        # The confirmed kill lands while the parent inbox is unavailable.
        ack = subagent_work.mark_subagent_work_terminal(
            child_id, status="cancelled", output="", confirmed_cancellation=True
        )
        assert not ack.delivered
        entry = subagent_work.get_subagent_work(child_id)
        assert entry is not None and entry.cancellation_confirmed and not entry.delivered

        # The inbox returns and a late completion arrives for the same dispatch.
        subagent_work._session_inboxes_ref[parent_id] = inbox
        done = subagent_work.mark_subagent_work_terminal(
            child_id, status="completed", output="the verdict", confirmed_completion=True
        )
        assert done.delivered_now
        statuses = []
        while not inbox.empty():
            item = inbox.get_nowait()
            statuses.append((item["status"], item["output"]))
        assert statuses == [("cancelled", "")], (
            "a confirmed cancellation must re-deliver as cancelled, not be corrected"
        )
    finally:
        subagent_work.unregister_subagent_work(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


def test_undelivered_optimistic_cancel_replaced_by_later_unconfirmed_completion() -> None:
    """A later report replaces an undelivered optimistic cancellation.

    The optimistic ``cancelled`` is recorded while the parent inbox is missing, so
    it never delivered. Because nothing was shown to the parent, the later report --
    even an unconfirmed ``completed`` (a legacy bare idle) -- replaces it, keeping the
    existing latest-undelivered-wins contract. Only a confirmed kill makes an
    undelivered cancellation final, which is covered separately.
    """
    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="cite-check",
    )
    try:
        ack = subagent_work.mark_subagent_work_terminal(
            child_id, status="cancelled", output="partial work"
        )
        assert not ack.delivered
        entry = subagent_work.get_subagent_work(child_id)
        assert entry is not None and not entry.cancellation_confirmed and not entry.delivered

        subagent_work._session_inboxes_ref[parent_id] = inbox
        done = subagent_work.mark_subagent_work_terminal(
            child_id, status="completed", output="done"
        )
        assert done.delivered_now
        statuses = []
        while not inbox.empty():
            item = inbox.get_nowait()
            statuses.append((item["status"], item["output"]))
        assert statuses == [("completed", "done")], (
            "a later report must replace an undelivered, unconfirmed cancellation"
        )
    finally:
        subagent_work.unregister_subagent_work(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


def test_undelivered_optimistic_cancel_corrected_by_confirmed_completion() -> None:
    """A confirmed completion still corrects an undelivered optimistic cancel.

    The optimistic ``cancelled`` never delivered, but the child genuinely survived
    and its confirmed turn-end arrives: the dispatch must reach the parent as
    ``completed`` with the real result.
    """
    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="cite-check",
    )
    try:
        ack = subagent_work.mark_subagent_work_terminal(
            child_id, status="cancelled", output="partial work"
        )
        assert not ack.delivered

        subagent_work._session_inboxes_ref[parent_id] = inbox
        done = subagent_work.mark_subagent_work_terminal(
            child_id, status="completed", output="the verdict", confirmed_completion=True
        )
        assert done.delivered_now
        statuses = []
        while not inbox.empty():
            item = inbox.get_nowait()
            statuses.append((item["status"], item["output"]))
        assert statuses == [("completed", "the verdict")], (
            "a confirmed completion must correct an undelivered optimistic cancellation"
        )
    finally:
        subagent_work.unregister_subagent_work(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


def test_parent_teardown_clears_drained_provisional_cancellation() -> None:
    """Deleting a parent drops its drained optimistic cancellations.

    A drained optimistic cancellation is retained keyed by the child so a survivor
    can still correct it. Once the parent session is deleted that record is
    unreachable, so teardown must drop it; a late completion then acknowledges as
    already delivered instead of re-registering work against the gone parent.
    """
    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="cite-check",
    )
    try:
        ack = subagent_work.mark_subagent_work_terminal(
            child_id, status="cancelled", output="partial work"
        )
        assert ack.delivered_now

        # The parent drains the optimistic cancellation, retaining the record.
        subagent_work.unregister_subagent_work(child_id, remember_drained_delivery=True)
        assert child_id in subagent_work._provisional_cancelled_subagent_work
        assert child_id in subagent_work._drained_delivered_subagent_children

        # The parent session is deleted.
        subagent_work._session_inboxes_ref.pop(parent_id, None)
        subagent_work.unregister_subagent_work_for_session(parent_id)
        assert child_id not in subagent_work._provisional_cancelled_subagent_work

        # A late completion must not re-register work against the deleted parent.
        done = subagent_work.mark_subagent_work_terminal(
            child_id, status="completed", output="late result", confirmed_completion=True
        )
        assert done.delivered and not done.delivered_now
        assert subagent_work.get_subagent_work(child_id) is None
        assert parent_id not in subagent_work._subagent_work_by_parent
    finally:
        subagent_work.unregister_subagent_work_for_session(parent_id)
        subagent_work._drained_delivered_subagent_children.discard(child_id)
        subagent_work._provisional_cancelled_subagent_work.pop(child_id, None)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


def test_new_send_clears_drained_optimistic_cancellation() -> None:
    """Re-dispatching a child supersedes its retained optimistic-cancel state.

    A drained optimistic cancellation is kept keyed by the child so a late
    survivor completion can still correct it. Registering a fresh dispatch for
    the same child replaces that turn: the retained provisional record and the
    drained-delivery tombstone are cleared and the new dispatch becomes the live
    entry, so its own completion is not mistaken for the superseded one.
    """
    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="A",
    )
    try:
        # Dispatch A is optimistically cancelled, delivered, then drained.
        ack = subagent_work.mark_subagent_work_terminal(
            child_id, status="cancelled", output="partial work"
        )
        assert ack.delivered_now
        subagent_work.unregister_subagent_work(child_id, remember_drained_delivery=True)
        assert child_id in subagent_work._provisional_cancelled_subagent_work
        assert child_id in subagent_work._drained_delivered_subagent_children

        # Dispatch B reuses the same child session, superseding the retained state.
        entry_b = subagent_work.register_subagent_work(
            parent_session_id=parent_id,
            child_session_id=child_id,
            agent="researcher",
            title="B",
        )
        assert child_id not in subagent_work._provisional_cancelled_subagent_work
        assert child_id not in subagent_work._drained_delivered_subagent_children
        assert subagent_work.get_subagent_work(child_id) is entry_b
    finally:
        subagent_work.unregister_subagent_work_for_session(parent_id)
        subagent_work._drained_delivered_subagent_children.discard(child_id)
        subagent_work._provisional_cancelled_subagent_work.pop(child_id, None)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


def test_corrected_completion_redelivers_when_inbox_returns() -> None:
    """A correction blocked by a missing parent inbox is redelivered on retry.

    The parent drains an optimistic cancellation (retaining the provisional
    record), then its inbox is briefly unavailable when the surviving child's
    confirmed completion arrives. The correction re-registers the dispatch but
    cannot deliver yet. Once the inbox returns, retrying the confirmed completion
    delivers the real result exactly once -- still carrying the superseding
    ``corrected_status`` -- and a further retry acknowledges it as already
    delivered rather than resending.
    """
    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="cite-check",
    )
    try:
        # The optimistic cancellation is delivered, then drained by the parent.
        subagent_work.mark_subagent_work_terminal(child_id, status="cancelled", output="partial")
        subagent_work.unregister_subagent_work(child_id, remember_drained_delivery=True)
        assert child_id in subagent_work._provisional_cancelled_subagent_work
        while not inbox.empty():
            inbox.get_nowait()

        # The parent inbox is unavailable when the confirmed completion arrives.
        subagent_work._session_inboxes_ref.pop(parent_id, None)
        ack = subagent_work.mark_subagent_work_terminal(
            child_id, status="completed", output="the verdict", confirmed_completion=True
        )
        assert not ack.delivered
        assert ack.reason == subagent_work._SUBAGENT_DELIVERY_MISSING_PARENT_INBOX
        assert inbox.empty()

        # The inbox returns and the retried completion delivers the real result once.
        subagent_work._session_inboxes_ref[parent_id] = inbox
        redelivered = subagent_work.mark_subagent_work_terminal(
            child_id, status="completed", output="the verdict", confirmed_completion=True
        )
        assert redelivered.delivered and redelivered.delivered_now
        items = []
        while not inbox.empty():
            items.append(inbox.get_nowait())
        assert [(i["status"], i["output"], i.get("corrected_status")) for i in items] == [
            ("completed", "the verdict", "cancelled")
        ]

        # A further retry is acknowledged as already delivered, not resent.
        again = subagent_work.mark_subagent_work_terminal(
            child_id, status="completed", output="the verdict", confirmed_completion=True
        )
        assert again.delivered and not again.delivered_now
        assert inbox.empty()
    finally:
        subagent_work.unregister_subagent_work_for_session(parent_id)
        subagent_work._drained_delivered_subagent_children.discard(child_id)
        subagent_work._provisional_cancelled_subagent_work.pop(child_id, None)
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


def test_corrected_completion_line_notes_superseded_cancellation() -> None:
    """A completion correcting a cancellation tells the parent it superseded it."""
    from omnigent.runner.tool_dispatch import _format_async_task_item

    payload = {
        "type": "sub_agent",
        "handle_id": "task1",
        "agent": "researcher",
        "title": "cite-check",
        "status": "completed",
        "output": "the verdict",
        "corrected_status": "cancelled",
    }
    assert _format_async_task_item(payload) == (
        "[System: sub-agent task task1 completed — researcher:cite-check "
        "returned: the verdict, superseding its earlier cancelled notice]"
    )
    silent = _format_async_task_item({**payload, "output": ""})
    assert silent == (
        "[System: sub-agent task task1 completed — researcher:cite-check "
        "produced no output, superseding its earlier cancelled notice]"
    )
