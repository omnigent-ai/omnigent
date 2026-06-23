"""Interaction bridge for the native Antigravity (agy) RPC harness.

This is the correctness-sensitive piece of the RPC core rework: it surfaces an
agy WAITING interaction (``ask_question`` / command ``permission``) as an
Omnigent elicitation, waits for the human's verdict, and delivers it back to agy
via ``HandleCascadeUserInteraction`` — handling agy's **WAITING-interaction
timeout gotcha** end-to-end.

The gotcha (design ``docs/antigravity-native-rpc-core-design.md`` §2.1, memory
``agy-rpc-interaction-bridge``): a WAITING interaction **times out** server-side
(→ ``CORTEX_STEP_STATUS_ERROR``), after which agy **auto-retries with a fresh
WAITING step at a HIGHER ``stepIndex``**. Omnigent elicitations wait on a human
(potentially slow), so by the time a verdict arrives the captured
``trajectoryId`` / ``stepIndex`` may be STALE. Consequences this module handles:

* The bridge **re-reads the freshest WAITING step at delivery time** and targets
  THAT step's ids — never the ones captured at detection.
* ``HTTP 500 "input not registered for step N"`` is **overloaded**: it means
  either a missing ``trajectoryId`` *or* a step that already timed out. After a
  delivery raises it, the bridge re-reads for a NEW (higher-index) WAITING step
  and re-surfaces the elicitation against it (so the retry step gets a fresh
  elicitation id and the web UI re-prompts).
* The whole loop is bounded by ``max_retries`` so a pathological retry storm
  cannot spin forever.

Seam design (so the timeout logic is unit-testable without a live agy):

* ``get_steps`` — re-reads the freshest trajectory steps (production wraps
  :func:`omnigent.antigravity_native_rpc.get_trajectory_steps`).
* ``request_elicitation`` — publishes the elicitation under a deterministic id
  and long-poll-awaits the user's result; returns ``None`` on timeout/cancel
  (production, wired in Task 11, POSTs the
  ``antigravity-elicitation-request`` hook and long-polls — mirrors codex's
  ``_handle_codex_elicitation_request``).
* ``deliver`` — defaults to :func:`_deliver_via_rpc`, which offloads the blocking
  :func:`omnigent.antigravity_native_rpc.handle_user_interaction` to a worker
  thread (the function is synchronous); injectable so tests don't need a live agy.

The deterministic elicitation id is derived from
``(cascade_id, trajectory_id, step_index)`` (mirrors
:func:`omnigent.codex_native_elicitation.codex_elicitation_id`), so a
timeout-retry step (new ``step_index``) yields a NEW id → a fresh elicitation is
surfaced for the retry rather than silently reusing the stale card.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from omnigent.antigravity_native_rpc import (
    AntigravityRpcError,
    handle_user_interaction,
)
from omnigent.antigravity_native_steps import PendingInteraction, pending_interaction
from omnigent.server.routes._antigravity_elicitation import (
    to_elicitation_params,
    to_interaction_payload,
)
from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult

_logger = logging.getLogger(__name__)

# Length of the hex digest slice used in the deterministic elicitation id.
# Mirrors ``codex_native_elicitation._CODEX_ELICITATION_ID_DIGEST_LENGTH`` so the
# two harnesses produce ids of the same shape/cardinality.
_AGY_ELICITATION_ID_DIGEST_LENGTH = 32

# Substring agy returns (inside an HTTP 500 body) when the targeted step is no
# longer accepting input — either a missing ``trajectoryId`` or, the case this
# module retries, a step that already timed out (status ERROR) before delivery.
_INPUT_NOT_REGISTERED = "input not registered"

# Default bound on the detect→elicit→deliver loop. Each iteration surfaces one
# elicitation and attempts one delivery; a timeout-retry consumes one iteration.
# A handful covers any realistic chain of agy timeout-retries while guaranteeing
# the loop terminates even if every delivery keeps racing a fresh ERROR step.
_DEFAULT_MAX_RETRIES = 5


# A re-reader of the freshest trajectory steps (production wraps
# ``get_trajectory_steps(port, cascade_id)``).
GetSteps = Callable[[], Awaitable[list[dict[str, object]]]]

# Publishes one elicitation under ``elicitation_id`` and long-poll-awaits the
# user's verdict; ``None`` means timeout/cancel.
RequestElicitation = Callable[
    [str, ElicitationRequestParams],
    Awaitable[ElicitationResult | None],
]


class Deliver(Protocol):
    """
    Delivers a built interaction payload to agy.

    Matches :func:`omnigent.antigravity_native_rpc.handle_user_interaction`
    exactly (``port``/``cascade_id`` positional, ids + payload keyword-only) so
    that function is the drop-in default; spelled as a ``Protocol`` rather than a
    ``Callable[..., ...]`` alias to keep the signature precise (the package bans
    explicit ``Any``).
    """

    async def __call__(
        self,
        port: int,
        cascade_id: str,
        *,
        trajectory_id: str,
        step_index: int,
        payload: dict[str, object],
    ) -> None:
        """Deliver one interaction answer to agy (see ``handle_user_interaction``)."""
        ...


def agy_elicitation_id(cascade_id: str, trajectory_id: str, step_index: int) -> str:
    """
    Build the Omnigent elicitation id for one agy WAITING interaction.

    Deterministic over ``(cascade_id, trajectory_id, step_index)`` so a
    timeout-retry step (which agy issues at a HIGHER ``step_index``) maps to a
    DIFFERENT id — surfacing a fresh elicitation for the retry rather than
    re-using the stale card. Mirrors
    :func:`omnigent.codex_native_elicitation.codex_elicitation_id`.

    :param cascade_id: agy cascade id (equal to the conversation id).
    :param trajectory_id: agy trajectory id from the WAITING step.
    :param step_index: Trajectory step index of the WAITING step.
    :returns: Stable elicitation id beginning with ``"elicit_agy_"``.
    """
    payload = json.dumps(
        {
            "cascade_id": cascade_id,
            "trajectory_id": trajectory_id,
            "step_index": step_index,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:_AGY_ELICITATION_ID_DIGEST_LENGTH]
    return f"elicit_agy_{digest}"


async def _deliver_via_rpc(
    port: int,
    cascade_id: str,
    *,
    trajectory_id: str,
    step_index: int,
    payload: dict[str, object],
) -> None:
    """
    Default :class:`Deliver`: offload the blocking RPC to a worker thread.

    :func:`omnigent.antigravity_native_rpc.handle_user_interaction` is synchronous
    and uses a blocking ``httpx.Client``, so calling it directly from the async
    bridge would stall the event loop. This wraps it in
    :func:`asyncio.to_thread` — the same pattern the read driver uses for
    ``get_trajectory_steps`` — and preserves its :class:`AntigravityRpcError`
    (the bridge catches it to detect the timed-out-step race).

    :param port: Validated agy connect-RPC port.
    :param cascade_id: agy cascade id (equal to the conversation id).
    :param trajectory_id: agy trajectory id of the target WAITING step.
    :param step_index: Step index of the target WAITING step.
    :param payload: The interaction variant dict (``askQuestion`` / ``permission``).
    :returns: None.
    :raises AntigravityRpcError: Propagated from ``handle_user_interaction``.
    """
    await asyncio.to_thread(
        handle_user_interaction,
        port,
        cascade_id,
        trajectory_id=trajectory_id,
        step_index=step_index,
        payload=payload,
    )


def _freshest_waiting(
    steps: list[dict[str, object]],
    *,
    kind: str,
    after_index: int | None = None,
) -> PendingInteraction | None:
    """
    Return the highest-index WAITING interaction from a steps snapshot.

    The crux of the timeout handling: when several WAITING steps are present
    (agy left timed-out ones behind and issued retries), the freshest — highest
    ``step_index`` — is the live one to deliver against. Same-``kind`` steps are
    preferred so a question is not answered against a stray permission step that
    happens to sit at a higher index; if none of the requested kind exists, the
    highest-index WAITING of any kind is returned as a fallback.

    :param steps: A trajectory steps snapshot (from ``get_steps``).
    :param kind: Preferred interaction kind (``"ask_question"`` / ``"permission"``).
    :param after_index: When set, only steps with ``step_index > after_index``
        are considered — used after a timeout to require a strictly NEWER step
        than the one that just failed, so the loop cannot re-target the same
        stale index.
    :returns: The freshest matching :class:`PendingInteraction`, or ``None``.
    """
    same_kind: PendingInteraction | None = None
    any_kind: PendingInteraction | None = None
    for step in steps:
        pending = pending_interaction(step)
        if pending is None:
            continue
        if after_index is not None and pending["step_index"] <= after_index:
            continue
        if any_kind is None or pending["step_index"] > any_kind["step_index"]:
            any_kind = pending
        if pending["kind"] == kind and (
            same_kind is None or pending["step_index"] > same_kind["step_index"]
        ):
            same_kind = pending
    return same_kind if same_kind is not None else any_kind


async def bridge_interaction(
    cascade_id: str,
    pending: PendingInteraction,
    *,
    port: int,
    get_steps: GetSteps,
    request_elicitation: RequestElicitation,
    deliver: Deliver = _deliver_via_rpc,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> None:
    """
    Surface an agy WAITING interaction, await the verdict, and deliver it.

    Runs the tight detect→elicit→deliver loop that absorbs agy's WAITING-timeout
    gotcha (see the module docstring / design §2.1). Each iteration:

    1. Publishes the elicitation under the deterministic id for ``current``'s
       ``(cascade_id, trajectory_id, step_index)`` and long-poll-awaits a verdict.
    2. If the verdict is ``None`` (the human timed out or cancelled), **returns
       without delivering** — agy's own WAITING timeout handles the dangling step;
       forcing a deny here is out of scope.
    3. **Re-reads the freshest WAITING step** and delivers against THAT step's
       ids (never the captured ones — they may be stale because agy timed out and
       retried while the human deliberated). If no WAITING step remains, returns.
    4. On the overloaded ``"input not registered"`` error (the targeted step
       timed out before delivery), re-reads for a NEW higher-index WAITING step;
       if found, re-surfaces the elicitation against it (next iteration). Any
       other :class:`AntigravityRpcError` is logged and ends the loop (no infinite
       retry on a genuine shape error).

    The loop is bounded by ``max_retries`` so a pathological timeout-retry storm
    terminates.

    :param cascade_id: agy cascade id (equal to the conversation id).
    :param pending: The :class:`PendingInteraction` detected by the read driver.
    :param port: Validated agy connect-RPC port.
    :param get_steps: Re-reads the freshest trajectory steps (production wraps
        :func:`omnigent.antigravity_native_rpc.get_trajectory_steps`).
    :param request_elicitation: Publishes the elicitation under a deterministic
        id and long-poll-awaits the verdict; ``None`` on timeout/cancel.
    :param deliver: Delivers the built payload to agy; defaults to
        :func:`_deliver_via_rpc`, which offloads the blocking
        :func:`omnigent.antigravity_native_rpc.handle_user_interaction` to a
        worker thread.
    :param max_retries: Upper bound on detect→deliver iterations.
    :returns: None. Never raises on the expected timeout/cancel/RPC-error paths —
        all are logged and end the loop so the long-lived caller stays alive.
    """
    current: PendingInteraction = pending
    for attempt in range(max_retries):
        eid = agy_elicitation_id(cascade_id, current["trajectory_id"], current["step_index"])
        params = to_elicitation_params(dict(current))

        result = await request_elicitation(eid, params)
        if result is None:
            # Human timed out / cancelled. Don't deliver: agy's own WAITING
            # timeout reclaims the step. (Forcing a deny is a separate policy.)
            _logger.info(
                "agy elicitation %s returned no verdict (timeout/cancel); "
                "not delivering (cascade=%s, kind=%s, step=%d)",
                eid,
                cascade_id,
                current["kind"],
                current["step_index"],
            )
            return

        # Re-read the freshest WAITING step BEFORE delivering: the captured ids
        # may be stale if agy timed out + retried while the human deliberated.
        fresh = _freshest_waiting(await get_steps(), kind=current["kind"])
        if fresh is None:
            _logger.warning(
                "agy elicitation %s resolved but no WAITING step remains to "
                "deliver to (cascade=%s, kind=%s); the interaction likely timed "
                "out server-side",
                eid,
                cascade_id,
                current["kind"],
            )
            return

        payload = to_interaction_payload(current["kind"], result, current["spec"])
        try:
            await deliver(
                port,
                cascade_id,
                trajectory_id=fresh["trajectory_id"],
                step_index=fresh["step_index"],
                payload=payload,
            )
            return  # delivered successfully
        except AntigravityRpcError as exc:
            if _INPUT_NOT_REGISTERED not in str(exc):
                # A genuine shape/transport error — not the timed-out-step race.
                # Log and stop; retrying would not help and could loop forever.
                _logger.warning(
                    "agy interaction delivery failed (cascade=%s, step=%d): %s",
                    cascade_id,
                    fresh["step_index"],
                    exc,
                )
                return
            # The targeted step timed out (status ERROR) before delivery. agy
            # auto-retries with a fresh WAITING step at a HIGHER index; find it
            # and re-surface the elicitation against that step next iteration.
            retry = _freshest_waiting(
                await get_steps(),
                kind=current["kind"],
                after_index=fresh["step_index"],
            )
            if retry is None:
                _logger.info(
                    "agy step %d timed out before delivery and no newer WAITING "
                    "step appeared (cascade=%s); giving up",
                    fresh["step_index"],
                    cascade_id,
                )
                return
            _logger.info(
                "agy step %d timed out before delivery; re-surfacing against "
                "retry step %d (cascade=%s, attempt=%d)",
                fresh["step_index"],
                retry["step_index"],
                cascade_id,
                attempt + 1,
            )
            current = retry
    _logger.warning(
        "agy interaction bridge exhausted %d delivery attempts (cascade=%s); "
        "giving up on the timeout-retry chain",
        max_retries,
        cascade_id,
    )
