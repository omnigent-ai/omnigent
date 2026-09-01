"""Native sub-agent terminal-status integrity tests.

Native sub-agent turns: a trailing ``idle`` must not launder a ``failed`` turn
into ``completed``, a late ``failed`` report must not be discarded by an
earlier delivered ``completed``, and a dispatch wedged in ``launching`` must
fail loudly to the parent instead of being silently lost.

The status tests drive the runner's real HTTP event route — the same
``external_session_status`` POSTs the claude-native forwarder and the
PTY-activity watcher emit — so they exercise the genuine edge-processing
path, not internal helpers.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_trailing_idle_must_not_launder_failed_child_status() -> None:
    """
    A trailing watcher ``idle`` must not flip a ``failed`` child to ``completed``.

    User journey: a parent agent dispatches a native sub-agent turn; the turn
    errors (``StopFailure`` → ``failed`` edge); the pane then goes quiet, so
    the PTY-activity watcher emits a trailing ``idle`` ~1s later — by design,
    on error exactly as on success. The operator watching the session tree
    must still see the child as failed.

    The server enforces exactly this sticky-``failed`` invariant
    (``omnigent/server/routes/_sessions/helpers.py`` — "``failed`` is sticky
    against a trailing ``idle``"), but the runner's child fan-out has no such
    guard: ``_session_status_to_task_status("idle")`` returns ``"completed"``
    unconditionally, so the trailing ``idle`` republishes the parent-visible
    child record as ``completed`` and the session tree shows a green child for
    a turn that died.
    """
    from omnigent.runner import app as runner_app

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_event_queues_ref.pop(parent_id, None)
    runner_app._session_event_queues_ref.pop(child_id, None)
    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="claude:impl",
        tool="claude",
        session_name="impl",
    )
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        async with _runner_client(app) as client:
            # The real edge sequence for a failed claude-native turn:
            # running (turn starts) → failed (StopFailure hook) → trailing
            # idle (PTY-activity watcher, ~1s after the pane goes quiet).
            for data in (
                {"status": "running"},
                {"status": "failed", "output": "Error: native sub-agent turn failed"},
                {"status": "idle"},
            ):
                resp = await client.post(
                    f"/v1/sessions/{child_id}/events",
                    json={"type": "external_session_status", "data": data},
                )
                assert resp.status_code == 204, resp.text

        events = _drain_session_event_queue(runner_app._session_event_queues_ref.get(parent_id))
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app.unregister_child_session(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(parent_id, None)
        runner_app._session_event_queues_ref.pop(child_id, None)

    task_statuses = [
        event["child"]["current_task_status"]
        for event in events
        if isinstance(event, dict) and event.get("type") == "session.child_session.updated"
    ]
    assert task_statuses and task_statuses[-1] == "failed", (
        f"parent-visible child task-status sequence was {task_statuses!r}: the "
        f"trailing watcher 'idle' after 'failed' must be dropped (sticky "
        f"failed, mirroring the server invariant), not republished as "
        f"'completed' — otherwise the session tree shows a green child for a "
        f"turn that died."
    )


@pytest.mark.asyncio
async def test_late_failed_report_must_not_be_discarded_by_delivered_completed() -> None:
    """
    A later ``failed`` edge must not be silently discarded by an earlier ``idle``.

    ``mark_subagent_work_terminal`` is first-delivered-wins: once an ``idle``
    edge has marked the work entry ``completed`` and delivered it, a later
    ``failed`` edge for the same turn returns "already delivered" without
    recording the new status or output — the failure, and its error text, are
    silently dropped. The parent (and any orchestrator reading child status)
    is left believing the turn succeeded.
    """
    from omnigent.runner import app as runner_app

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        async with _runner_client(app) as client:
            # idle processed first (e.g. Stop journaled/observed before
            # StopFailure, or the watcher's quiescence edge racing the hook).
            idle_resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "idle", "output": "partial output"},
                },
            )
            assert idle_resp.status_code == 204, idle_resp.text

            # The turn's real terminal state arrives next: failed.
            failed_resp = await client.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "failed", "output": "Error: BOOM from provider"},
                },
            )
            assert failed_resp.status_code == 204, failed_resp.text

            entry = runner_app.get_subagent_work(child_id)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert entry is not None
    assert entry.status == "failed", (
        f"work entry status is {entry.status!r}: a delivered 'completed' must "
        f"not stand against a later 'failed' for the same turn — the failure "
        f"report (and its output) was silently discarded by the "
        f"first-delivered-wins early return in mark_subagent_work_terminal."
    )
    assert entry.output == "Error: BOOM from provider", (
        f"work entry output is {entry.output!r}: the failed edge's error text "
        f"was dropped along with the status."
    )


@pytest.mark.asyncio
async def test_wedged_launching_dispatch_fails_loudly_to_parent() -> None:
    """
    A dispatch wedged in ``launching`` must fail loudly, never be swallowed.

    User journey: a parent dispatches a native sub-agent turn; the child
    never emits any edge (no running/waiting/terminal status — e.g. the
    native pane never came up, or the send was buffered against a turn that
    never ends). A message buffered against a healthy active turn drains as
    a continuation turn once that turn settles — that path is contractual
    and covered elsewhere — but a child that produces NO edge at all leaves
    the work entry in ``launching`` forever: the parent never hears back and
    the dispatched work is silently lost with no error surfaced anywhere.

    The runner must enforce a launch-liveness budget: a dispatch still in
    ``launching`` past the budget is failed and that failure is delivered to
    the parent inbox, so the send is run-or-failed — never accepted and
    dropped in silence.
    """
    from omnigent.runner import app as runner_app

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[parent_id] = inbox
    entry = runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        # Absent on an unguarded runner: without the sweep, the wedged
        # dispatch simply stays "launching" and the behavioral asserts
        # below observe the silent loss directly.
        reap = getattr(runner_app, "reap_stalled_subagent_launches", None)
        if reap is not None:
            # Within the budget: the dispatch is left alone (still launching).
            assert reap(now=entry.created_at + 10.0, timeout_s=180.0) == []
            assert entry.status == "launching"
            assert inbox.empty()
            # Past the budget with no edge ever seen: the sweep must act.
            reap(now=entry.created_at + 200.0, timeout_s=180.0)

        assert entry.status == "failed", (
            f"work entry is {entry.status!r} long past any reasonable launch "
            f"budget: a child that never produced a single edge is wedged, the "
            f"parent is never told, and the dispatched work is silently lost. "
            f"The runner must fail the dispatch loudly."
        )
        payload = inbox.get_nowait()
        assert payload["status"] == "failed", payload
        assert "never started" in str(payload["output"]), (
            f"the parent must receive an explanatory failure, got {payload['output']!r}"
        )
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_started_dispatch_is_not_reaped_by_launch_liveness() -> None:
    """
    A dispatch that produced a real edge must never be launch-reaped.

    Once the child has reported ``running`` (the launch proof), the liveness
    budget no longer applies — a long-running healthy child is not a wedge.
    """
    from omnigent.runner import app as runner_app

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[parent_id] = inbox
    entry = runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        runner_app.mark_subagent_work_started(child_id)
        assert entry.status == "running"
        reaped = runner_app.reap_stalled_subagent_launches(
            now=entry.created_at + 10_000.0, timeout_s=180.0
        )
        assert reaped == []
        assert entry.status == "running"
        assert inbox.empty()
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_reaped_dispatch_wakes_parent_not_just_inbox() -> None:
    """
    A launch-reaped failure must schedule the parent wake POST.

    The wake POST is the sole delivery signal for an idle parent: it only
    drains its inbox when a turn is triggered, and nothing else will rouse a
    parent whose child wedged in ``launching``. A reap that inserts the
    failure into the inbox without the wake leaves the parent hanging exactly
    as before — the failure is recorded where nobody will ever read it.
    """
    from omnigent.runner import app as runner_app

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    wake_bodies: list[dict[str, Any]] = []
    wake_seen = asyncio.Event()

    class _WakeRecordingServerClient(NullServerClient):
        """Records wake POSTs aimed at the parent session's events route."""

        async def post(self, url: str, **kwargs: Any) -> Any:
            if url.rstrip("/").endswith(f"/v1/sessions/{parent_id}/events"):
                wake_bodies.append(kwargs.get("json") or {})
                wake_seen.set()
            return self._Response()

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=_WakeRecordingServerClient(),  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = inbox
    entry = runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        reaped = runner_app.reap_stalled_subagent_launches(
            now=entry.created_at + 200.0,
            timeout_s=180.0,
            mark_terminal=app.state.mark_subagent_terminal_and_wake,
        )
        assert reaped == [entry]
        assert entry.status == "failed"
        payload = inbox.get_nowait()
        assert payload["status"] == "failed"

        await asyncio.wait_for(wake_seen.wait(), timeout=5.0)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    notice = wake_bodies[0]["data"]["content"][0]["text"]
    assert "finished (failed)" in notice, (
        f"the reap's wake notice must tell the parent the dispatch failed, got {notice!r}"
    )


def test_launch_timeout_resolver_rejects_non_finite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``nan``/``inf`` overrides must not silently disable the launch reaper.

    ``float("nan")`` makes every ``elapsed < budget`` comparison false and
    ``inf`` makes it always true-to-skip — either silently disables reaping
    without the explicit ``<= 0`` "disabled" intent. Non-finite values are
    rejected like non-numeric ones, falling back to the default.
    """
    from omnigent.runner import app as runner_app

    default = runner_app._DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S
    for bad in ("nan", "inf", "-inf", "+inf", "bogus"):
        monkeypatch.setenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", bad)
        assert runner_app.resolve_subagent_launch_timeout_s() == default, bad
    # Explicit disable and a valid override still work.
    monkeypatch.setenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", "0")
    assert runner_app.resolve_subagent_launch_timeout_s() == 0.0
    monkeypatch.setenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", "45.5")
    assert runner_app.resolve_subagent_launch_timeout_s() == 45.5
    monkeypatch.delenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S")
    assert runner_app.resolve_subagent_launch_timeout_s() == default
