"""Tests for the agy interaction bridge (detect → elicit → deliver loop).

These exercise :func:`omnigent.antigravity_native_interactions.bridge_interaction`
with fakes for its three injectable seams (``get_steps``,
``request_elicitation``, ``deliver``) so the timeout/re-read logic is unit-tested
WITHOUT a live agy server.

The load-bearing behaviour under test is the agy WAITING-interaction timeout
gotcha (design §2.1, memory ``agy-rpc-interaction-bridge``): a WAITING step times
out server-side and agy re-issues a FRESH WAITING step at a HIGHER ``stepIndex``.
So the bridge must:

* re-read the freshest WAITING step at delivery time (never trust the captured
  detection-time ids), and
* on the overloaded ``HTTP 500 "input not registered for step N"`` (a step that
  timed out before delivery), re-read for a new higher-index WAITING step and
  re-surface the elicitation against it.

Scenarios (from the plan's Step-1):
- (a) happy path: question → result selects "2" → one ``deliver`` with the
  freshest step's ids and ``askQuestion.responses[0].selectedOptionIds == ["2"]``.
- (b) timeout/re-read: first ``deliver`` raises ``"input not registered"`` and a
  NEW higher-index WAITING step is now present → second ``deliver`` targets the
  NEW ``step_index``.
- (c) permission accept → ``deliver`` called with ``{"permission": {"allow": True}}``.
- (d) staleness-before-first-delivery: captured ``step_index=N`` but the freshest
  WAITING at delivery time is ``N+1`` → delivery targets ``N+1``.
- (e) elicitation returns ``None`` (timeout/cancel) → no ``deliver``, no crash.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.antigravity_native_interactions import (
    _freshest_pending,
    _freshest_waiting,
    agy_elicitation_id,
    bridge_interaction,
    deny_pending_interaction,
)
from omnigent.antigravity_native_rpc import AntigravityRpcError
from omnigent.antigravity_native_steps import PendingInteraction
from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult

_CASCADE = "test-cascade-id"
_TRAJ = "test-trajectory-id"


# ---------------------------------------------------------------------------
# Fixture / fake helpers
# ---------------------------------------------------------------------------


def _question_step(
    *, step_index: int, status: str = "CORTEX_STEP_STATUS_WAITING"
) -> dict[str, Any]:
    """
    Build a WAITING ask_question step dict at a given trajectory index.

    Mirrors the live RPC shape consumed by
    :func:`omnigent.antigravity_native_steps.pending_interaction`:
    ``status``, ``requestedInteraction.askQuestion``, and the
    ``metadata.sourceTrajectoryStepInfo`` ids.

    :param step_index: Trajectory ``stepIndex`` for this step.
    :param status: Step status; defaults to WAITING.
    :returns: A step dict suitable for ``pending_interaction``.
    """
    return {
        "type": "CORTEX_STEP_TYPE_ASK_QUESTION",
        "status": status,
        "requestedInteraction": {
            "askQuestion": {
                "questions": [
                    {
                        "question": "Pick one",
                        "options": [
                            {"id": "1", "text": "First"},
                            {"id": "2", "text": "Second"},
                        ],
                    }
                ]
            }
        },
        "metadata": {
            "sourceTrajectoryStepInfo": {
                "trajectoryId": _TRAJ,
                "stepIndex": step_index,
                "cascadeId": _CASCADE,
            }
        },
    }


def _permission_step(*, step_index: int) -> dict[str, Any]:
    """
    Build a WAITING command-permission step dict at a given trajectory index.

    :param step_index: Trajectory ``stepIndex`` for this step.
    :returns: A step dict carrying ``requestedInteraction.permission``.
    """
    return {
        "type": "CORTEX_STEP_TYPE_RUN_COMMAND",
        "status": "CORTEX_STEP_STATUS_WAITING",
        "requestedInteraction": {
            "permission": {
                "resource": {"action": "command", "target": "ls -la"},
                "actionDescription": "List files",
            }
        },
        "metadata": {
            "sourceTrajectoryStepInfo": {
                "trajectoryId": _TRAJ,
                "stepIndex": step_index,
                "cascadeId": _CASCADE,
            }
        },
    }


def _pending_question(*, step_index: int) -> PendingInteraction:
    """
    Build a captured ask_question :class:`PendingInteraction` at an index.

    :param step_index: The captured ``step_index`` (may be stale by delivery).
    :returns: A ``PendingInteraction`` of kind ``"ask_question"``.
    """
    return PendingInteraction(
        kind="ask_question",
        trajectory_id=_TRAJ,
        step_index=step_index,
        spec={
            "questions": [
                {
                    "question": "Pick one",
                    "options": [
                        {"id": "1", "text": "First"},
                        {"id": "2", "text": "Second"},
                    ],
                }
            ]
        },
    )


def _pending_permission(*, step_index: int) -> PendingInteraction:
    """
    Build a captured permission :class:`PendingInteraction` at an index.

    :param step_index: The captured ``step_index``.
    :returns: A ``PendingInteraction`` of kind ``"permission"``.
    """
    return PendingInteraction(
        kind="permission",
        trajectory_id=_TRAJ,
        step_index=step_index,
        spec={"resource": {"action": "command", "target": "ls -la"}},
    )


class _DeliverRecorder:
    """
    Records every ``deliver`` call and optionally raises a scripted error.

    Used in place of :func:`handle_user_interaction` so a test can assert the
    exact ``trajectory_id`` / ``step_index`` / ``payload`` each call received and
    drive the timeout branch by raising on the first call.
    """

    def __init__(self, *, errors: list[Exception | None] | None = None) -> None:
        """
        :param errors: Per-call outcomes; entry ``i`` (when not ``None``) is
            raised on call ``i``. ``None`` (or a short list) means success.
        """
        self.calls: list[dict[str, Any]] = []
        self._errors = errors or []

    async def __call__(
        self,
        port: int,
        cascade_id: str,
        *,
        trajectory_id: str,
        step_index: int,
        payload: dict[str, object],
    ) -> None:
        """Record one delivery, raising the scripted error for this call index."""
        idx = len(self.calls)
        self.calls.append(
            {
                "port": port,
                "cascade_id": cascade_id,
                "trajectory_id": trajectory_id,
                "step_index": step_index,
                "payload": payload,
            }
        )
        if idx < len(self._errors):
            err = self._errors[idx]
            if err is not None:
                raise err


class _InjectTuiRecorder:
    """
    Records every ``inject_tui`` call's key sequence (and optionally raises).

    Stands in for :func:`_inject_via_tui` so a test can assert the EXACT tmux key
    sequence the bridge types into the agy TUI pane after a successful RPC
    delivery (#1200), without a live pane. Mirrors :class:`_DeliverRecorder`.
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        """
        :param error: When not ``None``, raised on every call so a test can drive
            the best-effort "TUI dismissal failed but the verdict still landed"
            branch.
        """
        self.calls: list[list[str]] = []
        self._error = error

    async def __call__(self, keys: list[str]) -> None:
        """Record one TUI key sequence, raising the scripted error if set."""
        self.calls.append(list(keys))
        if self._error is not None:
            raise self._error


def _steps_returner(*frames: list[dict[str, Any]]) -> Any:
    """
    Build a ``get_steps`` fake that returns successive snapshots per call.

    The last frame is repeated for any further calls so a re-read never runs off
    the end.

    :param frames: One steps-list per expected ``get_steps`` call.
    :returns: An async callable matching the ``get_steps`` seam.
    """
    state = {"i": 0}

    async def _get_steps() -> list[dict[str, Any]]:
        i = min(state["i"], len(frames) - 1)
        state["i"] += 1
        return list(frames[i])

    return _get_steps


def _elicitation_returner(
    *results: ElicitationResult | None,
) -> tuple[Any, list[tuple[str, ElicitationRequestParams]]]:
    """
    Build a ``request_elicitation`` fake plus a log of its calls.

    :param results: One result per expected ``request_elicitation`` call; the
        last is repeated for any further calls.
    :returns: ``(callable, calls)`` where ``calls`` accumulates
        ``(elicitation_id, params)`` tuples in call order.
    """
    calls: list[tuple[str, ElicitationRequestParams]] = []
    state = {"i": 0}

    async def _request(eid: str, params: ElicitationRequestParams) -> ElicitationResult | None:
        i = min(state["i"], len(results) - 1)
        state["i"] += 1
        calls.append((eid, params))
        return results[i]

    return _request, calls


# ---------------------------------------------------------------------------
# (a) happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_delivers_selected_option_to_fresh_step() -> None:
    """Question resolved with option "2" → one delivery with that step's ids."""
    pending = _pending_question(step_index=3)
    waiting = _question_step(step_index=3)
    request, elicit_calls = _elicitation_returner(
        ElicitationResult(action="accept", content={"0": "Second"})
    )
    deliver = _DeliverRecorder()
    inject_tui = _InjectTuiRecorder()

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_steps_returner([waiting]),
        request_elicitation=request,
        deliver=deliver,
        inject_tui=inject_tui,
    )

    assert len(deliver.calls) == 1
    call = deliver.calls[0]
    assert call["cascade_id"] == _CASCADE
    assert call["trajectory_id"] == _TRAJ
    assert call["step_index"] == 3
    ask = call["payload"]["askQuestion"]
    assert ask["responses"][0]["selectedOptionIds"] == ["2"]
    # The elicitation was published under the deterministic id for these ids.
    assert len(elicit_calls) == 1
    assert elicit_calls[0][0] == agy_elicitation_id(_CASCADE, _TRAJ, 3)
    # The TUI prompt was dismissed by typing the selected option id + Enter.
    assert inject_tui.calls == [["2", "Enter"]]


# ---------------------------------------------------------------------------
# (b) timeout / re-read path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_not_registered_reads_new_step_and_redelivers() -> None:
    """First delivery 500s ('input not registered'); re-deliver targets N+1."""
    pending = _pending_question(step_index=3)
    stale = _question_step(step_index=3)
    retry = _question_step(step_index=4)
    # request_elicitation is called once per surfaced step (original + retry).
    request, elicit_calls = _elicitation_returner(
        ElicitationResult(action="accept", content={"0": "Second"}),
        ElicitationResult(action="accept", content={"0": "Second"}),
    )
    deliver = _DeliverRecorder(
        errors=[AntigravityRpcError("input not registered for step 3"), None]
    )
    # First get_steps (before first delivery) still shows the stale step;
    # after the 500, get_steps shows the NEW higher-index WAITING step.
    get_steps = _steps_returner([stale], [retry], [retry])

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=get_steps,
        request_elicitation=request,
        deliver=deliver,
    )

    assert len(deliver.calls) == 2
    assert deliver.calls[0]["step_index"] == 3
    assert deliver.calls[1]["step_index"] == 4
    # A fresh elicitation was surfaced for the retry step (new deterministic id).
    assert len(elicit_calls) == 2
    assert elicit_calls[0][0] == agy_elicitation_id(_CASCADE, _TRAJ, 3)
    assert elicit_calls[1][0] == agy_elicitation_id(_CASCADE, _TRAJ, 4)


@pytest.mark.asyncio
async def test_input_not_registered_match_is_case_insensitive() -> None:
    """A mixed-case 'Input Not Registered' is the retryable race, not a fatal error.

    Regression for Fix G: the race discriminator is matched case-INSENSITIVELY
    against agy's raw 500 body. A capitalized variant ('Input Not Registered for
    step 3') must still be treated as the timed-out-step race (re-read for a NEW
    higher-index WAITING step and re-deliver), NOT misclassified as fatal — which
    would silently drop the human's verdict.
    """
    pending = _pending_question(step_index=3)
    stale = _question_step(step_index=3)
    retry = _question_step(step_index=4)
    request, _ = _elicitation_returner(
        ElicitationResult(action="accept", content={"0": "Second"}),
        ElicitationResult(action="accept", content={"0": "Second"}),
    )
    # Mixed-case message (note the capitalization) — must still match the race.
    deliver = _DeliverRecorder(
        errors=[AntigravityRpcError("Input Not Registered for step 3"), None]
    )
    get_steps = _steps_returner([stale], [retry], [retry])

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=get_steps,
        request_elicitation=request,
        deliver=deliver,
    )

    # Treated as the retryable race: a second delivery was attempted at N+1.
    assert len(deliver.calls) == 2
    assert deliver.calls[1]["step_index"] == 4


# ---------------------------------------------------------------------------
# (c) permission accept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_accept_delivers_allow_true() -> None:
    """Permission accepted → deliver payload ``{"permission": {"allow": True}}``."""
    pending = _pending_permission(step_index=2)
    waiting = _permission_step(step_index=2)
    request, _ = _elicitation_returner(ElicitationResult(action="accept", content=None))
    deliver = _DeliverRecorder()
    inject_tui = _InjectTuiRecorder()

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_steps_returner([waiting]),
        request_elicitation=request,
        deliver=deliver,
        inject_tui=inject_tui,
    )

    assert len(deliver.calls) == 1
    assert deliver.calls[0]["payload"] == {"permission": {"allow": True}}
    assert deliver.calls[0]["step_index"] == 2
    # #1200: Approve drives agy's TUI prompt to option 1 ("Yes") + Enter, so the
    # attended terminal advances (the RPC alone leaves the TUI prompt open).
    assert inject_tui.calls == [["1", "Enter"]]


@pytest.mark.asyncio
async def test_permission_reject_delivers_allow_false_and_types_no() -> None:
    """Permission rejected → deliver ``allow: False`` AND type option 4 ("No")."""
    pending = _pending_permission(step_index=2)
    waiting = _permission_step(step_index=2)
    request, _ = _elicitation_returner(ElicitationResult(action="decline", content=None))
    deliver = _DeliverRecorder()
    inject_tui = _InjectTuiRecorder()

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_steps_returner([waiting]),
        request_elicitation=request,
        deliver=deliver,
        inject_tui=inject_tui,
    )

    assert deliver.calls[0]["payload"] == {"permission": {"allow": False}}
    # #1200: Reject drives agy's TUI prompt to option 4 ("No") + Enter.
    assert inject_tui.calls == [["4", "Enter"]]


@pytest.mark.asyncio
async def test_tui_dismissal_failure_does_not_undo_delivered_verdict() -> None:
    """A TUI send-keys failure is best-effort: the RPC verdict still stands.

    The backend step is already answered by the time the keystroke is typed, so a
    flaky/exited pane must NOT raise out of ``bridge_interaction`` (which would
    look like the verdict failed). The delivery is recorded; the TUI error is
    swallowed (logged).
    """
    pending = _pending_permission(step_index=2)
    waiting = _permission_step(step_index=2)
    request, _ = _elicitation_returner(ElicitationResult(action="accept", content=None))
    deliver = _DeliverRecorder()
    inject_tui = _InjectTuiRecorder(error=RuntimeError("the agy terminal exited"))

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_steps_returner([waiting]),
        request_elicitation=request,
        deliver=deliver,
        inject_tui=inject_tui,
    )

    # The RPC verdict was delivered exactly once and the bridge returned cleanly
    # despite the TUI keystroke raising.
    assert len(deliver.calls) == 1
    assert deliver.calls[0]["payload"] == {"permission": {"allow": True}}
    assert inject_tui.calls == [["1", "Enter"]]


@pytest.mark.asyncio
async def test_no_tui_keystroke_when_nothing_delivered() -> None:
    """When the elicitation returns None (timeout/cancel), no TUI key is typed."""
    pending = _pending_permission(step_index=2)
    waiting = _permission_step(step_index=2)
    request, _ = _elicitation_returner(None)
    deliver = _DeliverRecorder()
    inject_tui = _InjectTuiRecorder()

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_steps_returner([waiting]),
        request_elicitation=request,
        deliver=deliver,
        inject_tui=inject_tui,
    )

    assert deliver.calls == []
    assert inject_tui.calls == []


# ---------------------------------------------------------------------------
# (d) staleness before first delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_freshest_waiting_overrides_stale_captured_index() -> None:
    """Captured index N but freshest WAITING is N+1 → delivery targets N+1."""
    pending = _pending_question(step_index=5)  # captured at detection time
    fresher = _question_step(step_index=6)  # agy retried before the human answered
    request, _ = _elicitation_returner(ElicitationResult(action="accept", content={"0": "First"}))
    deliver = _DeliverRecorder()

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_steps_returner([fresher]),
        request_elicitation=request,
        deliver=deliver,
    )

    assert len(deliver.calls) == 1
    assert deliver.calls[0]["step_index"] == 6  # NOT the stale 5


# ---------------------------------------------------------------------------
# (e) elicitation returns None (timeout / cancel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_elicitation_none_does_not_deliver() -> None:
    """A None elicitation result (timeout/cancel) → no delivery, no crash."""
    pending = _pending_question(step_index=3)
    waiting = _question_step(step_index=3)
    request, _ = _elicitation_returner(None)
    deliver = _DeliverRecorder()

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_steps_returner([waiting]),
        request_elicitation=request,
        deliver=deliver,
    )

    assert deliver.calls == []


# ---------------------------------------------------------------------------
# Bounded retry + non-retryable error guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_fresh_waiting_step_skips_delivery() -> None:
    """No WAITING step at delivery time (all timed out) → no delivery, no crash."""
    pending = _pending_question(step_index=3)
    done = _question_step(step_index=3, status="CORTEX_STEP_STATUS_DONE")
    request, _ = _elicitation_returner(ElicitationResult(action="accept", content={"0": "Second"}))
    deliver = _DeliverRecorder()

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_steps_returner([done]),
        request_elicitation=request,
        deliver=deliver,
    )

    assert deliver.calls == []


# ---------------------------------------------------------------------------
# _freshest_waiting: strict same-kind selection (Fix E)
# ---------------------------------------------------------------------------


def test_freshest_waiting_returns_none_for_only_different_kind() -> None:
    """A snapshot with ONLY a different-kind WAITING step yields ``None``.

    Regression for Fix E: the cross-kind ``any_kind`` fallback was dropped. agy
    keys delivery on ``trajectoryId``+``stepIndex`` with no kind check, so
    pairing an ask_question payload with a WAITING permission step would
    mis-deliver. With no same-kind WAITING step present, the function must return
    ``None`` (the caller then stops cleanly) rather than the other kind.
    """
    steps = [_permission_step(step_index=5)]
    assert _freshest_waiting(steps, kind="ask_question") is None


def test_freshest_waiting_returns_highest_same_kind() -> None:
    """Among same-kind WAITING steps the highest ``step_index`` is returned.

    Confirms the strict-same-kind change did not regress the freshest-wins
    selection, and that a higher-index DIFFERENT-kind step does not shadow it.
    """
    steps = [
        _question_step(step_index=2),
        _permission_step(step_index=9),  # higher index, wrong kind → ignored
        _question_step(step_index=4),
    ]
    fresh = _freshest_waiting(steps, kind="ask_question")
    assert fresh is not None
    assert fresh["kind"] == "ask_question"
    assert fresh["step_index"] == 4


@pytest.mark.asyncio
async def test_other_rpc_error_does_not_loop() -> None:
    """A non-'input not registered' RPC error stops the loop after one attempt."""
    pending = _pending_question(step_index=3)
    waiting = _question_step(step_index=3)
    request, _ = _elicitation_returner(ElicitationResult(action="accept", content={"0": "Second"}))
    deliver = _DeliverRecorder(errors=[AntigravityRpcError("trajectory not found")])

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_steps_returner([waiting]),
        request_elicitation=request,
        deliver=deliver,
    )

    assert len(deliver.calls) == 1  # tried once, did not retry on an unrelated error


@pytest.mark.asyncio
async def test_retry_storm_is_bounded_by_max_retries() -> None:
    """Every delivery 500s with a fresh retry step → loop stops at max_retries."""
    pending = _pending_question(step_index=0)

    # get_steps always returns a fresh higher-index WAITING step; deliver always
    # raises 'input not registered'. Without a bound this would spin forever.
    counter = {"n": 0}

    async def _get_steps() -> list[dict[str, Any]]:
        counter["n"] += 1
        return [_question_step(step_index=counter["n"])]

    request, _ = _elicitation_returner(ElicitationResult(action="accept", content={"0": "Second"}))
    deliver = _DeliverRecorder(
        errors=[AntigravityRpcError("input not registered for step N")] * 50
    )

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_get_steps,
        request_elicitation=request,
        deliver=deliver,
        max_retries=3,
    )

    # Exactly max_retries delivery attempts, then it gives up.
    assert len(deliver.calls) == 3


# ---------------------------------------------------------------------------
# Deterministic id
# ---------------------------------------------------------------------------


def test_elicitation_id_is_deterministic_and_index_sensitive() -> None:
    """Same ids → same elicitation id; a different step_index → a different id."""
    a = agy_elicitation_id(_CASCADE, _TRAJ, 3)
    b = agy_elicitation_id(_CASCADE, _TRAJ, 3)
    c = agy_elicitation_id(_CASCADE, _TRAJ, 4)
    assert a == b
    assert a != c
    assert a.startswith("elicit_agy_")


# ---------------------------------------------------------------------------
# deny_pending_interaction (interrupt DENY of a WAITING step)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deny_pending_interaction_denies_freshest_waiting_question() -> None:
    """A WAITING ask_question is refused with an EMPTY responses payload."""
    deliver = _DeliverRecorder()
    denied = await deny_pending_interaction(
        _CASCADE,
        port=52548,
        get_steps=_steps_returner([_question_step(step_index=2), _question_step(step_index=5)]),
        deliver=deliver,
    )
    assert denied is True
    assert len(deliver.calls) == 1
    call = deliver.calls[0]
    # Freshest (highest index) WAITING step is targeted.
    assert call["step_index"] == 5
    assert call["cascade_id"] == _CASCADE
    # A decline yields an empty askQuestion responses list (no option selected).
    assert call["payload"]["askQuestion"]["responses"] == []


@pytest.mark.asyncio
async def test_deny_pending_interaction_denies_permission_with_allow_false() -> None:
    """A WAITING command-permission is refused with ``allow=False``."""
    deliver = _DeliverRecorder()
    denied = await deny_pending_interaction(
        _CASCADE,
        port=52548,
        get_steps=_steps_returner([_permission_step(step_index=4)]),
        deliver=deliver,
    )
    assert denied is True
    assert deliver.calls[0]["payload"]["permission"]["allow"] is False
    assert deliver.calls[0]["step_index"] == 4


@pytest.mark.asyncio
async def test_deny_pending_interaction_noop_when_no_waiting_step() -> None:
    """No WAITING step → no delivery and a ``False`` return (cancel path runs)."""
    deliver = _DeliverRecorder()
    denied = await deny_pending_interaction(
        _CASCADE,
        port=52548,
        # A DONE step carries an interaction block but is not WAITING.
        get_steps=_steps_returner(
            [_question_step(step_index=1, status="CORTEX_STEP_STATUS_DONE")]
        ),
        deliver=deliver,
    )
    assert denied is False
    assert deliver.calls == []


@pytest.mark.asyncio
async def test_deny_pending_interaction_swallows_rpc_error() -> None:
    """A delivery RPC error is logged and swallowed (returns ``False``)."""
    deliver = _DeliverRecorder(errors=[AntigravityRpcError("boom")])
    denied = await deny_pending_interaction(
        _CASCADE,
        port=52548,
        get_steps=_steps_returner([_question_step(step_index=3)]),
        deliver=deliver,
    )
    assert denied is False
    assert len(deliver.calls) == 1


def test_freshest_pending_picks_highest_index_any_kind() -> None:
    """``_freshest_pending`` returns the highest-index WAITING step of any kind."""
    fresh = _freshest_pending([_question_step(step_index=2), _permission_step(step_index=7)])
    assert fresh is not None
    assert fresh["step_index"] == 7
    assert fresh["kind"] == "permission"
    # No WAITING step → None.
    assert (
        _freshest_pending([_question_step(step_index=1, status="CORTEX_STEP_STATUS_DONE")]) is None
    )
