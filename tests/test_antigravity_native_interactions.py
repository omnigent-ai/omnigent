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
    agy_elicitation_id,
    bridge_interaction,
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
        ElicitationResult(action="accept", content={"selectedOptionIds": ["2"]})
    )
    deliver = _DeliverRecorder()

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_steps_returner([waiting]),
        request_elicitation=request,
        deliver=deliver,
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
        ElicitationResult(action="accept", content={"selectedOptionIds": ["2"]}),
        ElicitationResult(action="accept", content={"selectedOptionIds": ["2"]}),
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

    await bridge_interaction(
        _CASCADE,
        pending,
        port=52548,
        get_steps=_steps_returner([waiting]),
        request_elicitation=request,
        deliver=deliver,
    )

    assert len(deliver.calls) == 1
    assert deliver.calls[0]["payload"] == {"permission": {"allow": True}}
    assert deliver.calls[0]["step_index"] == 2


# ---------------------------------------------------------------------------
# (d) staleness before first delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_freshest_waiting_overrides_stale_captured_index() -> None:
    """Captured index N but freshest WAITING is N+1 → delivery targets N+1."""
    pending = _pending_question(step_index=5)  # captured at detection time
    fresher = _question_step(step_index=6)  # agy retried before the human answered
    request, _ = _elicitation_returner(
        ElicitationResult(action="accept", content={"selectedOptionIds": ["1"]})
    )
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
    request, _ = _elicitation_returner(
        ElicitationResult(action="accept", content={"selectedOptionIds": ["2"]})
    )
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


@pytest.mark.asyncio
async def test_other_rpc_error_does_not_loop() -> None:
    """A non-'input not registered' RPC error stops the loop after one attempt."""
    pending = _pending_question(step_index=3)
    waiting = _question_step(step_index=3)
    request, _ = _elicitation_returner(
        ElicitationResult(action="accept", content={"selectedOptionIds": ["2"]})
    )
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

    request, _ = _elicitation_returner(
        ElicitationResult(action="accept", content={"selectedOptionIds": ["2"]})
    )
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
