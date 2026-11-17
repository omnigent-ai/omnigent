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

from omnigent.inner.executor import NativeTurnAcceptance
from omnigent.runner import create_runner_app, subagent_work
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient, accept_subagent_turn


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
    subagent_work._session_inboxes_ref[parent_id] = session_inbox
    subagent_work.register_child_session(
        child_id,
        parent_session_id=parent_id,
        title="claude:impl",
        tool="claude",
        session_name="impl",
    )
    subagent_work.register_subagent_work(
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
        subagent_work.unregister_subagent_work(child_id)
        subagent_work.unregister_child_session(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)
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

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    subagent_work._session_inboxes_ref[parent_id] = session_inbox
    subagent_work.register_subagent_work(
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

            entry = subagent_work.get_subagent_work(child_id)
    finally:
        subagent_work.unregister_subagent_work(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)

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

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        # Absent on an unguarded runner: without the sweep, the wedged
        # dispatch simply stays "launching" and the behavioral asserts
        # below observe the silent loss directly.
        reap = getattr(subagent_work, "reap_stalled_subagent_launches", None)
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
        assert "no start acknowledgment" in str(payload["output"]), (
            f"the parent must receive an explanatory failure, got {payload['output']!r}"
        )
    finally:
        subagent_work.unregister_subagent_work(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start_reported_before_prompt_accepted",
    [False, True],
    ids=["running-edge", "codex-start-reported-before-prompt-accepted"],
)
async def test_started_dispatch_is_not_reaped_by_launch_liveness(
    start_reported_before_prompt_accepted: bool,
) -> None:
    """
    A dispatch that produced a real edge must never be launch-reaped.

    Once the child has reported ``running`` (the launch proof), the liveness
    budget no longer applies — a long-running healthy child is not a wedge.
    A codex child's start report can also beat the runner's own note that
    Codex accepted the prompt; the later note must not re-arm the bound.
    """

    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        if start_reported_before_prompt_accepted:
            subagent_work.note_subagent_child_start_reported(child_id, response_id="codex_running")
        subagent_work.mark_subagent_work_started(child_id)
        if start_reported_before_prompt_accepted:
            accept_subagent_turn(child_id, thread_id="thread_running", response_id="codex_running")
        assert entry.status == "running"
        reaped = subagent_work.reap_stalled_subagent_launches(
            now=entry.created_at + 10_000.0, timeout_s=180.0
        )
        assert reaped == []
        assert entry.status == "running"
        assert inbox.empty()
    finally:
        subagent_work.unregister_subagent_work_for_session(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


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

    subagent_work._session_inboxes_ref[parent_id] = inbox
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="impl",
    )

    try:
        reaped = subagent_work.reap_stalled_subagent_launches(
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
        subagent_work.unregister_subagent_work(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)

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

    default = subagent_work._DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S
    for bad in ("nan", "inf", "-inf", "+inf", "bogus"):
        monkeypatch.setenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", bad)
        assert subagent_work.resolve_subagent_launch_timeout_s() == default, bad
    # Explicit disable and a valid override still work.
    monkeypatch.setenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", "0")
    assert subagent_work.resolve_subagent_launch_timeout_s() == 0.0
    monkeypatch.setenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S", "45.5")
    assert subagent_work.resolve_subagent_launch_timeout_s() == 45.5
    monkeypatch.delenv("OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S")
    assert subagent_work.resolve_subagent_launch_timeout_s() == default


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_before_result", [False, True], ids=["direct", "after-unconfirmed-cancel"]
)
async def test_real_completion_replaces_a_launch_timeout_failure(
    cancel_before_result: bool,
) -> None:
    """
    A child's own terminal edge overrides the reaper's "no start acknowledgment".

    User journey: a parent sends a follow-up to a worker that is already busy;
    no fresh ``running`` edge is relayed, so the launch-liveness reaper fails
    the dispatch at the budget and tells the parent. Minutes later the worker
    genuinely finishes and its ``idle`` edge arrives as ``completed``. That
    completion must be delivered — the reaper's failure was a guess made
    without a single edge from the child — or the parent waits forever on a
    report that is sitting in the child's transcript. An interrupt's
    unconfirmed ``cancelled`` in between must not spend that guess.
    """
    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="claude-native",
        title="critique-desktop",
    )
    try:
        subagent_work.reap_stalled_subagent_launches(now=entry.created_at + 200.0, timeout_s=180.0)
        assert entry.status == "failed" and entry.delivered
        assert inbox.get_nowait()["status"] == "failed"
        if cancel_before_result:
            cancel = subagent_work.mark_subagent_work_terminal(
                child_id, status="cancelled", output=None
            )
            assert not cancel.delivered_now and entry.launch_timed_out

        ack = subagent_work.mark_subagent_work_terminal(
            child_id, status="completed", output="Both assessments are in. Full report: ..."
        )
        assert ack.delivered_now, (
            "the child's genuine completion after a launch-timeout guess must be "
            f"delivered to the parent, got reason={ack.reason!r}"
        )
        assert entry.status == "completed"
        payload = inbox.get_nowait()
        assert payload["status"] == "completed"
        assert "Full report" in str(payload["output"])
        # The guess is spent: a later trailing idle is not re-delivered.
        again = subagent_work.mark_subagent_work_terminal(child_id, status="completed", output="")
        assert not again.delivered_now and inbox.empty()
    finally:
        subagent_work.unregister_subagent_work(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_real_failure_still_replaces_a_launch_timeout_failure() -> None:
    """A genuine ``failed`` edge after the reaper's guess carries the real error text."""
    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent_id, child_session_id=child_id, agent="claude-native", title="x"
    )
    try:
        subagent_work.reap_stalled_subagent_launches(now=entry.created_at + 200.0, timeout_s=180.0)
        inbox.get_nowait()
        ack = subagent_work.mark_subagent_work_terminal(
            child_id, status="failed", output="Error: 400 from the model API"
        )
        assert ack.delivered_now
        assert inbox.get_nowait()["output"] == "Error: 400 from the model API"
    finally:
        subagent_work.unregister_subagent_work(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_native_prompt_delivery_acknowledges_the_launch() -> None:
    """
    The runner's own verified prompt delivery counts as the launch proof.

    A follow-up sent to a child that is already active yields no fresh
    ``running`` edge, so the only evidence the child took the message is the
    runner's verified paste. Once it is delivered the dispatch must leave
    ``launching`` so the reaper cannot fail a working child.
    """
    parent_id = uuid.uuid4().hex
    child_id = uuid.uuid4().hex
    entry = subagent_work.register_subagent_work(
        parent_session_id=parent_id, child_session_id=child_id, agent="claude-native", title="x"
    )
    try:
        assert subagent_work.mark_subagent_work_started(uuid.uuid4().hex) is None
        assert subagent_work.mark_subagent_work_started(child_id) is entry
        assert entry.status == "running"
        assert (
            subagent_work.reap_stalled_subagent_launches(
                now=entry.created_at + 900.0, timeout_s=180.0
            )
            == []
        )
    finally:
        subagent_work.unregister_subagent_work(child_id)


# Start-bound lifecycle for a steered Codex child. Evidence belongs to the child's turn:
# a settled turn clears it, a registration consumes it once, and draining or replacing a
# dispatch entry never touches it. Each case interleaves the events of a previous turn
# (``*_prev``) with the steered turn that the parent's send starts.

_START_BUDGET_S = 180.0
_PREVIOUS_ACCEPTED_AT = 1000.0
_STEER_ACCEPTED_AT = 5000.0


def _drive_steer_lifecycle(
    events: list[str], *, parent_id: str, child_id: str
) -> subagent_work._SubagentWorkEntry | None:
    """Apply the events in order to a drained previous dispatch and the steer that follows it.

    ``accept_prev`` / ``run_prev`` / ``settle_prev`` belong to the previous turn; ``accept_steer``
    / ``run_steer`` to the steered turn; ``register`` is the steer's fresh registration;
    ``drain_old`` drains the previous dispatch's result; ``forget_session`` deletes the child.
    """
    previous = subagent_work.register_subagent_work(
        parent_session_id=parent_id, child_session_id=child_id, agent="codex", title="review"
    )
    previous.status = "completed"
    previous.delivered = True
    previous_work_id = previous.work_id
    fresh: subagent_work._SubagentWorkEntry | None = None
    previous_request = subagent_work.begin_subagent_child_acceptance(child_id)
    steer_request = None
    previous_acceptance = NativeTurnAcceptance(child_id, "thread_lifecycle", "codex_previous")
    steer_acceptance = NativeTurnAcceptance(
        child_id,
        "thread_lifecycle",
        "codex_previous"
        if "accept_prev" in events
        and not {"settle_prev", "forget_session", "forget_parent"}.intersection(events)
        else "codex_steer",
    )
    try:
        for event in events:
            if event == "accept_prev":
                subagent_work.arm_subagent_child_start_timeout(
                    child_id,
                    acceptance=previous_acceptance,
                    request=previous_request,
                    now=_PREVIOUS_ACCEPTED_AT,
                )
            elif event in ("run_prev", "run_steer", "run_unidentified"):
                subagent_work.note_subagent_child_start_reported(
                    child_id,
                    response_id=(
                        None
                        if event == "run_unidentified"
                        else (
                            previous_acceptance if event == "run_prev" else steer_acceptance
                        ).response_id
                    ),
                )
            elif event in ("settle_prev", "settle_unidentified"):
                subagent_work.note_subagent_child_turn_settled(
                    child_id,
                    response_id=previous_acceptance.response_id
                    if event == "settle_prev"
                    else None,
                )
                assert not subagent_work.child_start_already_reported(child_id)
                assert child_id not in subagent_work._children_accepted_turns
            elif event == "begin_steer":
                steer_request = subagent_work.begin_subagent_child_acceptance(child_id)
            elif event == "accept_steer":
                if steer_request is None:
                    steer_request = subagent_work.begin_subagent_child_acceptance(child_id)
                subagent_work.arm_subagent_child_start_timeout(
                    child_id,
                    acceptance=steer_acceptance,
                    request=steer_request,
                    now=_STEER_ACCEPTED_AT,
                )
            elif event == "drain_old":
                accepted_at = subagent_work._children_accepted_turns.get(child_id)
                subagent_work.unregister_subagent_work(
                    child_id, work_id=previous_work_id, remember_drained_delivery=True
                )
                assert subagent_work._children_accepted_turns.get(child_id) == accepted_at
                assert subagent_work.get_subagent_work(child_id) is fresh
            elif event == "forget_session" or event == "forget_parent":
                subagent_work.unregister_subagent_work_for_session(
                    child_id if event == "forget_session" else parent_id
                )
            elif event == "register":
                fresh = subagent_work.register_subagent_work(
                    parent_session_id=parent_id,
                    child_session_id=child_id,
                    agent="codex",
                    title="review",
                )
                fresh.status = "running"
                subagent_work.apply_child_start_evidence(fresh)
            else:
                raise AssertionError(f"unknown event {event!r}")
    finally:
        subagent_work.finish_subagent_child_acceptance(previous_request)
        if steer_request is not None:
            subagent_work.finish_subagent_child_acceptance(steer_request)
        assert child_id not in subagent_work._pending_child_acceptances
    return fresh


@pytest.mark.parametrize(
    ("events", "expected_start"),
    [
        # The steer's acceptance and registration, in each order, with a drain anywhere.
        (["accept_steer", "register"], _STEER_ACCEPTED_AT),
        (["accept_steer", "drain_old", "register"], _STEER_ACCEPTED_AT),
        (["register", "accept_steer", "drain_old"], _STEER_ACCEPTED_AT),
        (["register", "drain_old", "accept_steer"], _STEER_ACCEPTED_AT),
        # A previous turn's acceptance that settled, with no report, must not bound the steer.
        (["accept_prev", "settle_prev", "accept_steer", "register"], _STEER_ACCEPTED_AT),
        (["accept_prev", "settle_prev", "register", "accept_steer"], _STEER_ACCEPTED_AT),
        pytest.param(
            ["settle_prev", "accept_prev", "accept_steer", "register"],
            _STEER_ACCEPTED_AT,
            id="settled-before-delayed-acceptance",
        ),
        pytest.param(
            ["settle_prev", "accept_prev", "drain_old", "accept_steer", "register"],
            _STEER_ACCEPTED_AT,
            id="settled-before-delayed-acceptance-and-drain",
        ),
        pytest.param(
            ["settle_prev", "accept_steer", "accept_prev", "register"],
            _STEER_ACCEPTED_AT,
            id="stale-acceptance-after-current-acceptance",
        ),
        pytest.param(
            ["begin_steer", "settle_prev", "accept_prev", "accept_steer", "register"],
            _STEER_ACCEPTED_AT,
            id="current-request-spans-previous-settlement",
        ),
        # A previous turn's running report that settled must not exempt the steered turn.
        (
            ["accept_prev", "run_prev", "settle_prev", "accept_steer", "register"],
            _STEER_ACCEPTED_AT,
        ),
        (["run_prev", "settle_prev", "accept_steer", "register"], _STEER_ACCEPTED_AT),
        # Deleting the session forgets evidence from the old child lifetime.
        (["accept_prev", "forget_session", "accept_steer", "register"], _STEER_ACCEPTED_AT),
        pytest.param(
            ["accept_prev", "forget_session", "accept_prev", "accept_steer", "register"],
            _STEER_ACCEPTED_AT,
            id="deleted-session-rejects-outstanding-finalizer",
        ),
        pytest.param(
            ["accept_prev", "forget_parent", "accept_prev", "accept_steer", "register"],
            _STEER_ACCEPTED_AT,
            id="deleted-parent-rejects-outstanding-finalizer",
        ),
        # A previous turn that never settled still owns its silence; the steer joins it.
        (["accept_prev", "accept_steer", "register"], _PREVIOUS_ACCEPTED_AT),
        (["accept_prev", "register", "accept_steer"], _PREVIOUS_ACCEPTED_AT),
        pytest.param(
            ["begin_steer", "settle_unidentified", "accept_steer", "register"],
            None,
            id="unidentified-settlement-during-request-stays-unbounded",
        ),
        pytest.param(
            ["settle_unidentified", "accept_steer", "register"],
            _STEER_ACCEPTED_AT,
            id="new-request-after-unidentified-settlement-is-attributable",
        ),
        pytest.param(
            ["register", "accept_steer", "run_prev"],
            _STEER_ACCEPTED_AT,
            id="stale-running-after-current-acceptance",
        ),
        pytest.param(
            ["register", "run_prev", "accept_steer"],
            _STEER_ACCEPTED_AT,
            id="stale-running-before-current-acceptance",
        ),
        pytest.param(
            ["register", "accept_steer", "settle_prev"],
            _STEER_ACCEPTED_AT,
            id="stale-settlement-after-current-acceptance",
        ),
        pytest.param(
            ["register", "run_prev", "settle_prev", "accept_steer"],
            _STEER_ACCEPTED_AT,
            id="previous-start-settled-before-current-acceptance",
        ),
        pytest.param(
            ["run_prev", "register", "settle_prev", "accept_steer"],
            _STEER_ACCEPTED_AT,
            id="retained-previous-start-settled-before-current-acceptance",
        ),
    ],
)
@pytest.mark.asyncio
async def test_steered_turn_is_bounded_from_its_own_acceptance(
    events: list[str], expected_start: float | None
) -> None:
    """Only an attributable unresolved acceptance can bound a silent steered turn."""
    parent_id, child_id = "conv_parent_lifecycle", "conv_child_lifecycle"
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subagent_work._session_inboxes_ref[parent_id] = inbox
    try:
        fresh = _drive_steer_lifecycle(events, parent_id=parent_id, child_id=child_id)
        assert fresh is not None
        assert fresh.child_start_reported is False
        assert fresh.awaiting_child_start_since == expected_start
        if expected_start is None:
            assert (
                subagent_work.reap_stalled_subagent_launches(
                    now=_STEER_ACCEPTED_AT + 10 * _START_BUDGET_S, timeout_s=_START_BUDGET_S
                )
                == []
            )
            assert fresh.status == "running"
            assert inbox.empty()
            return
        # Still inside the budget from its own acceptance: the steer keeps its full budget.
        assert (
            subagent_work.reap_stalled_subagent_launches(
                now=expected_start + _START_BUDGET_S - 1.0, timeout_s=_START_BUDGET_S
            )
            == []
        )
        reaped = subagent_work.reap_stalled_subagent_launches(
            now=expected_start + _START_BUDGET_S + 1.0, timeout_s=_START_BUDGET_S
        )
        assert [entry.child_session_id for entry in reaped] == [child_id]
        assert fresh.status == "failed"
        notice = inbox.get_nowait()
        assert notice["status"] == "failed"
        assert "no turn start" in notice["output"]
        assert inbox.empty()
        assert (
            subagent_work.reap_stalled_subagent_launches(
                now=expected_start + 2 * _START_BUDGET_S, timeout_s=_START_BUDGET_S
            )
            == []
        )
        assert inbox.empty()
    finally:
        subagent_work.unregister_subagent_work_for_session(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.parametrize(
    "events",
    [
        # The steered turn reports running before or after its registration: never bounded.
        ["accept_steer", "run_steer", "register"],
        ["register", "accept_steer", "run_steer"],
        ["accept_steer", "register", "run_steer"],
        ["accept_steer", "drain_old", "run_steer", "register"],
        pytest.param(
            ["run_steer", "register", "accept_steer"],
            id="running-before-registration-and-acceptance",
        ),
        pytest.param(
            ["register", "run_unidentified", "settle_unidentified", "accept_steer"],
            id="unidentified-start-remains-unbounded",
        ),
    ],
)
@pytest.mark.asyncio
async def test_steered_turn_that_reports_running_is_never_bounded(events: list[str]) -> None:
    """A steer into a turn whose running report arrived is exempt from the launch bound."""
    parent_id, child_id = "conv_parent_running", "conv_child_running"
    subagent_work._session_inboxes_ref[parent_id] = asyncio.Queue()
    try:
        fresh = _drive_steer_lifecycle(events, parent_id=parent_id, child_id=child_id)
        assert fresh is not None
        assert fresh.child_start_reported is True
        assert fresh.awaiting_child_start_since is None
        assert (
            subagent_work.reap_stalled_subagent_launches(
                now=_STEER_ACCEPTED_AT + 10 * _START_BUDGET_S, timeout_s=_START_BUDGET_S
            )
            == []
        )
    finally:
        subagent_work.unregister_subagent_work_for_session(child_id)
        subagent_work._session_inboxes_ref.pop(parent_id, None)
