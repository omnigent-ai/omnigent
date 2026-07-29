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
* ``inject_tui`` — defaults to :func:`_inject_via_tui`, which types the verdict's
  key sequence into the agy TUI pane AFTER the RPC delivery. The attended agy TUI
  keeps its OWN permission/question prompt open in parallel with the RPC step
  (live-verified — ``docs/claude/antigravity-rpc-spike-notes.md`` §"attended
  TUI"): the RPC flips the backend step to DONE, but the TUI prompt lingers, so a
  web Approve/Reject would not advance the terminal (and the next typed turn would
  fold into the stale prompt's buffer) without this keystroke (#1200). Injectable
  so tests assert the key sequence without a live pane; mirrors cursor-native's
  ``send_cursor_pane_keys`` approval drive.

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
    to_tui_selection_keys,
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
# Matched case-INSENSITIVELY against agy's raw body (see the delivery
# error-handling below): this substring is the SOLE discriminator between a
# retryable step-timeout race and a fatal delivery error, so a capitalization
# change in agy's message must not silently reclassify the race as fatal.
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


# Types the verdict's tmux key sequence into the agy TUI pane to dismiss its
# in-process prompt (see ``_inject_via_tui`` / the module's "attended TUI" note).
InjectTui = Callable[[list[str]], Awaitable[None]]


async def _inject_via_tui(keys: list[str]) -> None:
    """
    Default :class:`InjectTui`: type the verdict keys into the agy TUI pane.

    Offloads the blocking tmux ``send-keys`` (via
    :func:`omnigent.antigravity_native_bridge.send_interaction_keys_via_tui`) to a
    worker thread — the same pattern :func:`_deliver_via_rpc` uses for the RPC —
    so the async bridge does not stall the event loop. The bridge directory is
    resolved from the harness spawn env (the reader/CLI both run with it set), so
    this seam takes only the keys.

    A missing tmux target / exited pane raises ``RuntimeError`` from the bridge
    primitive; the caller logs it and proceeds (the RPC delivery already advanced
    the backend step — the keystroke is the belt-and-braces TUI dismissal).

    :param keys: Ordered tmux key arguments (e.g. ``["1", "Enter"]`` to approve).
    :returns: None.
    :raises RuntimeError: Propagated from the bridge primitive (no target / pane
        exited / send-keys failed).
    """
    # Lazy imports: keep this module importable from the lightweight CLI process
    # without eagerly pulling the bridge env helpers, and resolve the bridge dir
    # the same way the executor does.
    from omnigent.antigravity_native_bridge import send_interaction_keys_via_tui
    from omnigent.inner.antigravity_native_executor import _bridge_dir_from_env

    bridge_dir = _bridge_dir_from_env()
    await asyncio.to_thread(send_interaction_keys_via_tui, bridge_dir, *keys)


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
    ``step_index`` — is the live one to deliver against. Only steps of the
    requested ``kind`` are eligible: agy keys delivery on
    ``trajectoryId``+``stepIndex`` with no kind check, so pairing a payload built
    for one kind with a WAITING step of a different kind would mis-deliver (e.g.
    answer a question against a stray permission step). When no WAITING step of
    the requested kind exists, ``None`` is returned and the caller stops cleanly.

    :param steps: A trajectory steps snapshot (from ``get_steps``).
    :param kind: Required interaction kind (``"ask_question"`` / ``"permission"``).
    :param after_index: When set, only steps with ``step_index > after_index``
        are considered — used after a timeout to require a strictly NEWER step
        than the one that just failed, so the loop cannot re-target the same
        stale index.
    :returns: The freshest same-``kind`` :class:`PendingInteraction`, or ``None``
        when no WAITING step of that kind exists.
    """
    same_kind: PendingInteraction | None = None
    for step in steps:
        pending = pending_interaction(step)
        if pending is None:
            continue
        if after_index is not None and pending["step_index"] <= after_index:
            continue
        if pending["kind"] != kind:
            continue
        if same_kind is None or pending["step_index"] > same_kind["step_index"]:
            same_kind = pending
    return same_kind


def _waiting_step_at(
    steps: list[dict[str, object]],
    *,
    trajectory_id: str,
    step_index: int,
) -> PendingInteraction | None:
    """
    Return the WAITING interaction at an exact ``(trajectory_id, step_index)``.

    Pins verdict delivery to the step the elicitation was surfaced for rather than
    the freshest WAITING step (which could be a different gate that appeared
    meanwhile). Returns ``None`` when that step is no longer WAITING (timed out or
    answered), letting the caller fall back to ``_freshest_waiting`` for agy's
    same-gate timeout-retry.

    :param steps: Trajectory steps snapshot.
    :param trajectory_id: The surfaced step's trajectory id.
    :param step_index: The surfaced step's index.
    :returns: The matching WAITING :class:`PendingInteraction`, or ``None``.
    """
    for step in steps:
        pending = pending_interaction(step)
        if pending is None:
            continue
        if pending["trajectory_id"] == trajectory_id and pending["step_index"] == step_index:
            return pending
    return None


async def bridge_interaction(
    cascade_id: str,
    pending: PendingInteraction,
    *,
    port: int,
    get_steps: GetSteps,
    request_elicitation: RequestElicitation,
    deliver: Deliver = _deliver_via_rpc,
    inject_tui: InjectTui = _inject_via_tui,
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
    :param inject_tui: Types the verdict's key sequence into the agy TUI pane to
        dismiss its in-process prompt AFTER a successful RPC delivery; defaults to
        :func:`_inject_via_tui` (offloads ``send-keys`` to a worker thread). The
        RPC flips the backend step, but the attended TUI keeps its own prompt open
        in parallel — so the keystroke is required to actually advance the
        terminal (#1200). A failure here is logged, not raised: the backend step
        is already answered, so the turn proceeds regardless.
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
            # No verdict. Usually a human timeout/cancel (agy's own WAITING
            # timeout reclaims the step; forcing a deny is a separate policy), but
            # ``request_elicitation`` also returns ``None`` on a hook rejection
            # (a 4xx logged distinctly by the reader's elicitation poster), so the
            # cause is not exclusively timeout/cancel. Don't deliver either way.
            _logger.info(
                "agy elicitation %s returned no verdict (human timeout/cancel, or "
                "hook rejection — see reader warnings); not delivering "
                "(cascade=%s, kind=%s, step=%d)",
                eid,
                cascade_id,
                current["kind"],
                current["step_index"],
            )
            return

        # Re-read the steps BEFORE delivering: the captured ids may be stale if agy
        # timed out + retried while the human deliberated. PIN to the step we
        # surfaced if it is STILL WAITING — deliver THIS verdict to THAT gate, never
        # to a different higher-index gate that appeared meanwhile (#1472 review).
        # Only when our captured step is gone (timed out → ERROR) do we fall back to
        # the freshest WAITING, which is agy's same-gate timeout-retry (§2.1).
        steps = await get_steps()
        fresh = _waiting_step_at(
            steps, trajectory_id=current["trajectory_id"], step_index=current["step_index"]
        ) or _freshest_waiting(steps, kind=current["kind"])
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
            # The RPC flipped the backend trajectory step, but the attended agy
            # TUI keeps its OWN permission/question prompt open in parallel
            # (live-verified — see the module note / spike doc). Type the verdict's
            # selection into the pane so the terminal actually advances (#1200) and
            # the next typed turn does not land in the stale prompt's buffer. The
            # backend is already answered, so a TUI-typing failure is logged, not
            # raised — never undo a delivered verdict over a flaky pane.
            keys = to_tui_selection_keys(current["kind"], result, current["spec"])
            if keys:
                try:
                    await inject_tui(keys)
                except Exception as tui_exc:
                    # Best-effort TUI dismissal — the backend step is already
                    # answered, so never undo a delivered verdict over a flaky pane.
                    _logger.warning(
                        "agy interaction delivered over RPC but TUI dismissal failed "
                        "(cascade=%s, kind=%s, step=%d, keys=%r): %r",
                        cascade_id,
                        current["kind"],
                        fresh["step_index"],
                        keys,
                        tui_exc,
                    )
            return  # delivered successfully
        except AntigravityRpcError as exc:
            if _INPUT_NOT_REGISTERED not in str(exc).lower():
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
