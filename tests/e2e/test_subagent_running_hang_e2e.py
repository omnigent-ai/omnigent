"""Regression test: a dispatched sub-agent must not hang forever at ``running``.

A dispatched ``codex-native`` sub-agent can hang
forever at status ``running``: the parent orchestrator's inbox stays empty, no
error surfaces, and the work handle is never released. Two independent defects
produce the single symptom; each is reproduced here against the real product
seams.

Defect 1 -- codex thread rotation orphans the waiting parent
------------------------------------------------------------
``_maybe_rotate_session_on_thread_started`` rotates the forwarder onto a fresh
Omnigent session on *any* ``thread/started`` whose id differs from the active
thread, excluding only a Codex AgentControl child thread and an ephemeral
thread. Neither exclusion covers a **dispatched sub-agent session**. When the
interactive TUI mints its own thread on connect, the forwarder repoints away
from the sub-agent's conversation; the real turn's ``turn/completed`` -- the
sole idle edge -- is then discarded as a stale thread event, so the
``idle`` -> ``external_session_status`` -> ``_mark_subagent_terminal_and_wake``
chain never fires and the parent waits forever.

Why the forwarder-seam drive rather than a live TUI connect: the triggering
competing ``thread/started`` is minted by an interactive Codex TUI (and by
Codex CLI 0.150.1 on connect); this headless runner has neither an interactive
TUI nor 0.150.1, so the competing edge cannot be produced end to end here. The
test therefore feeds the exact competing ``thread/started`` into the **real**
forwarder rotation entry point over a session snapshot carrying the
dispatched-sub-agent markers (``parent_session_id`` / ``sub_agent_name``), and
asserts on the real rotation it drives -- mirroring the sibling
``test_codex_ephemeral_thread_rotation_e2e`` convention. On a buggy build the
sub-agent session rotates and a replacement session is created (the orphaning);
once a guard declines rotation for a dispatched sub-agent session it does not.

Defect 2 -- nothing bounds a dispatched sub-agent once it has started
--------------------------------------------------------------------
``reap_stalled_subagent_launches`` only covers a child wedged in ``launching``.
Once a child reaches ``running`` / ``waiting`` nothing else bounds it, so every
harness-drift bug past that point (defect 1, and any future lost-terminal-edge
bug) presents identically as a silent infinite hang. This drives the real
runner registry and the real periodic reaper loop over a ``running`` child that
has gone silent well past any budget and asserts the parent is woken with a
terminal failure. On a buggy build the launch reaper skips the started child
and the parent is never notified (the test fails); a dispatch-level stall sweep
in the same loop surfaces it and the test passes.

Run::

    pytest tests/e2e/test_subagent_running_hang_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import httpx
import pytest

import omnigent.runner.app as runner_app
from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)

# ── Defect 1: dispatched sub-agent session identities ──────────────────────
PARENT_ORCH_SESSION = "conv_parent_orchestrator"
SUBAGENT_SESSION = "conv_dispatched_subagent"
SUBAGENT_THREAD = "thread_subagent_persistent"
REPLACEMENT_SESSION = "conv_replacement"
APP_SERVER_URL = "ws://127.0.0.1:9876"


class _RecordingAP:
    """httpx-shaped Omnigent client: serves a dispatched-sub-agent snapshot, records writes.

    ``_maybe_rotate_session_on_thread_started`` -> ``_create_thread_replacement_session``
    fetches the current session snapshot (GET), then, when it rotates, creates
    the replacement (POST /v1/sessions) and issues PATCH/POST calls to bind and
    transfer it. Recording those lets the test assert whether a rotation was
    performed at all.
    """

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.patches: list[tuple[str, dict]] = []

    async def get(self, url: str) -> httpx.Response:
        """Return the dispatched sub-agent's session snapshot for any GET.

        The snapshot carries ``parent_session_id`` / ``sub_agent_name``: the
        parent orchestrator is parked on this exact conversation id, so any
        rotation orphans it.
        """
        return httpx.Response(
            200,
            json={
                "id": SUBAGENT_SESSION,
                "agent_id": "ag_codex_native",
                "runner_id": "runner_1",
                "labels": {},
                "parent_session_id": PARENT_ORCH_SESSION,
                "sub_agent_name": "reviewer",
            },
            request=httpx.Request("GET", url),
        )

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        """Record a POST and return 200 (a session id for a create)."""
        self.posts.append((url, json))
        body: dict = {"id": REPLACEMENT_SESSION} if url == "/v1/sessions" else {}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    async def patch(self, url: str, *, json: dict) -> httpx.Response:
        """Record a PATCH and return 200."""
        self.patches.append((url, json))
        return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    def created_sessions(self) -> list[dict]:
        """Return the bodies of every ``POST /v1/sessions`` (session creates)."""
        return [body for url, body in self.posts if url == "/v1/sessions"]


def _make_subagent_target(ap: _RecordingAP) -> fwd._ForwarderTarget:
    """Build a forwarder target bound to the dispatched sub-agent's thread."""
    return fwd._ForwarderTarget(
        session_id=SUBAGENT_SESSION,
        thread_id=SUBAGENT_THREAD,
        delta_coalescer=fwd._OutputTextDeltaCoalescer(ap, SUBAGENT_SESSION),
        usage_coalescer=fwd._SessionUsageCoalescer(ap, SUBAGENT_SESSION),
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
    )


def _seed_subagent_bridge(tmp_path: Path) -> Path:
    """Write bridge state pinning the dispatched sub-agent's persistent thread."""
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id=SUBAGENT_SESSION,
            socket_path=APP_SERVER_URL,
            thread_id=SUBAGENT_THREAD,
            codex_home=str(tmp_path / "codex_home"),
        ),
    )
    return tmp_path


def _competing_tui_thread_started() -> dict:
    """A competing top-level ``thread/started`` an interactive TUI mints on connect.

    Non-ephemeral and not an AgentControl sub-agent thread, so neither existing
    guard excludes it -- exactly the event the report describes.
    """
    return {
        "method": "thread/started",
        "params": {
            "thread": {
                "id": "0195-interactive-tui-connect-thread",
                "ephemeral": False,
                "path": "/rollout/0195-tui.jsonl",
                "threadSource": "user",
            }
        },
    }


async def test_dispatched_subagent_session_must_not_rotate_on_competing_thread(
    tmp_path: Path,
) -> None:
    """Defect 1: a competing ``thread/started`` must not rotate a dispatched sub-agent.

    On a buggy build the forwarder rotates the sub-agent's Omnigent session onto
    the interactive TUI's thread and creates a replacement session; the parent
    orchestrator is left parked on a conversation id the forwarder no longer
    owns, so the child's real ``turn/completed`` is dropped as stale and the
    parent hangs forever. The sub-agent session must stay bound to its own
    persistent thread.
    """
    ap = _RecordingAP()
    bridge_dir = _seed_subagent_bridge(tmp_path)
    target = _make_subagent_target(ap)

    rotated = await fwd._maybe_rotate_session_on_thread_started(
        ap_client=ap,
        target=target,
        bridge_dir=bridge_dir,
        app_server_url=APP_SERVER_URL,
        event=_competing_tui_thread_started(),
    )

    assert rotated is False, (
        "a competing thread/started rotated a dispatched sub-agent session, "
        "orphaning the waiting parent (the reported hang)"
    )
    assert ap.created_sessions() == [], "rotation created a replacement session for a sub-agent"
    assert target.session_id == SUBAGENT_SESSION
    assert target.thread_id == SUBAGENT_THREAD
    state = read_bridge_state(bridge_dir)
    assert state.session_id == SUBAGENT_SESSION
    assert state.thread_id == SUBAGENT_THREAD


async def test_top_level_clear_thread_still_rotates(tmp_path: Path) -> None:
    """A genuine top-level ``/clear`` on a non-sub-agent session must still rotate.

    The guard must be narrow: a real user ``/clear`` on a top-level session
    (no ``parent_session_id`` / ``sub_agent_name``) starts a fresh persistent
    thread and must keep rotating the Omnigent session as before.
    """

    class _TopLevelAP(_RecordingAP):
        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": SUBAGENT_SESSION,
                    "agent_id": "ag_codex_native",
                    "runner_id": "runner_1",
                    "labels": {},
                },
                request=httpx.Request("GET", url),
            )

    ap = _TopLevelAP()
    bridge_dir = _seed_subagent_bridge(tmp_path)
    target = _make_subagent_target(ap)

    rotated = await fwd._maybe_rotate_session_on_thread_started(
        ap_client=ap,
        target=target,
        bridge_dir=bridge_dir,
        app_server_url=APP_SERVER_URL,
        event=_competing_tui_thread_started(),
    )

    assert rotated is True, "a real top-level /clear thread must still rotate the session"
    assert len(ap.created_sessions()) == 1


# ── Defect 2: nothing bounds a started sub-agent that goes silent ──────────
CHILD_SESSION = "conv_started_then_silent"
LAUNCHING_CHILD_SESSION = "conv_never_started"


def _reset_subagent_registry() -> None:
    runner_app._subagent_work_by_child.clear()
    runner_app._subagent_work_by_parent.clear()
    runner_app._drained_delivered_subagent_children.clear()


async def _drive_reaper_loop(
    *,
    mark_terminal: object,
    iterations: int = 6,
    interval_s: float = 0.05,
) -> None:
    """Run the real periodic reaper loop for a few sweeps, then cancel it.

    ``run_subagent_launch_reaper`` is the production backstop loop; a
    dispatch-level stall sweep belongs alongside the launch sweep it already
    runs. Driving the real loop keeps this test honest about where the bound
    must live.
    """
    task = asyncio.create_task(
        runner_app.run_subagent_launch_reaper(
            interval_s=interval_s,
            mark_terminal=mark_terminal,  # type: ignore[arg-type]
        )
    )
    await asyncio.sleep(interval_s * iterations + 0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_started_subagent_gone_silent_is_bounded_and_wakes_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 2: a ``running`` child that goes silent past its budget must be failed.

    Registers a dispatched child, promotes it to ``running`` (its first edge
    landed), then ages it far past any budget with no further activity -- the
    lost-terminal-edge state defect 1 produces. Driving the real reaper loop
    must surface a terminal failure to the parent. On a buggy build the launch
    reaper skips the started child and nothing else bounds it, so the parent is
    never woken and this fails; a dispatch-level stall sweep makes it pass.
    """
    monkeypatch.setenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", "1")
    monkeypatch.setenv("OMNIGENT_SUBAGENT_STALL_TIMEOUT_S", "1")
    _reset_subagent_registry()

    entry = runner_app.register_subagent_work(
        parent_session_id=PARENT_ORCH_SESSION,
        child_session_id=CHILD_SESSION,
        agent="reviewer",
        title="review the diff",
    )
    runner_app.mark_subagent_work_started(CHILD_SESSION)
    assert entry.status == "running"
    stale = time.time() - 100_000.0
    entry.created_at = stale
    # A started-then-silent child: age every liveness stamp the reaper may key
    # on. ``last_activity_at`` exists only once a dispatch-level bound is added.
    if hasattr(entry, "last_activity_at"):
        entry.last_activity_at = stale

    delivered: list[tuple[str, str]] = []

    def _record_terminal(
        child_session_id: str, *, status: str, output: str | None
    ) -> runner_app._SubagentDeliveryAck:
        delivered.append((child_session_id, status))
        return runner_app._SubagentDeliveryAck(
            entry=entry,
            delivered=True,
            delivered_now=True,
            reason="delivered",
        )

    try:
        await _drive_reaper_loop(mark_terminal=_record_terminal)
    finally:
        _reset_subagent_registry()

    child_deliveries = [status for child, status in delivered if child == CHILD_SESSION]
    assert child_deliveries, (
        "a dispatched sub-agent that reached 'running' and then went silent past "
        "its budget was never bounded: the parent orchestrator is never woken and "
        "hangs forever with an empty inbox (the reported symptom)"
    )
    assert child_deliveries[-1] in runner_app._SUBAGENT_TERMINAL_STATUSES
    assert child_deliveries[-1] == "failed"


async def test_launching_subagent_is_still_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: the pre-existing launch reaper must still fail a wedged ``launching`` child.

    Confirms the defect-2 gap is specific to *started* children: a child stuck
    in ``launching`` past its budget is reaped and its parent notified today,
    which is exactly why a silent hang past ``running`` is invisible by contrast.
    """
    monkeypatch.setenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", "1")
    _reset_subagent_registry()

    entry = runner_app.register_subagent_work(
        parent_session_id=PARENT_ORCH_SESSION,
        child_session_id=LAUNCHING_CHILD_SESSION,
        agent="reviewer",
        title="review the diff",
    )
    assert entry.status == "launching"
    entry.created_at = time.time() - 100_000.0

    delivered: list[tuple[str, str]] = []

    def _record_terminal(
        child_session_id: str, *, status: str, output: str | None
    ) -> runner_app._SubagentDeliveryAck:
        delivered.append((child_session_id, status))
        return runner_app._SubagentDeliveryAck(
            entry=entry, delivered=True, delivered_now=True, reason="delivered"
        )

    try:
        reaped = runner_app.reap_stalled_subagent_launches(mark_terminal=_record_terminal)
    finally:
        _reset_subagent_registry()

    assert [e.child_session_id for e in reaped] == [LAUNCHING_CHILD_SESSION]
    assert (LAUNCHING_CHILD_SESSION, "failed") in delivered
