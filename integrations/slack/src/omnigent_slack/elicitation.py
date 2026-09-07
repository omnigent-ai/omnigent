"""In-turn elicitation (tool-approval) orchestration for the Slack bot.

Owns everything about a pending approval/AskUserQuestion card *during a turn*:
posting the card, spawning the background resolver that awaits the Slack click
(or times out) and posts the verdict, and finalizing the card in place when the
server pushes ``response.elicitation_resolved``. Pure-push, mirroring the web
UI: the turn loop keeps reading the stream, so the continuation and the resolved
event arrive as normal events — no polling.

Extracted from ``SlackOmnigentService`` so that class is left with event
routing + turn lifecycle. The card-building blocks, the coordinator, and the
outcome enum live in ``approvals``; this module is the orchestration on top.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from omnigent_slack.approvals import (
    RESOLVED_EXTERNALLY,
    ClickTarget,
    ElicitationCoordinator,
    ElicitationOutcome,
    Verdict,
    elicitation_card_blocks,
    resolve_form_answers,
    resolved_card_blocks,
)
from omnigent_slack.models import SlackTurn, ThreadKey
from omnigent_slack.omnigent import ElicitationRequest, OmnigentClient

if TYPE_CHECKING:
    from omnigent_slack.streaming import SlackClientProtocol

# Posts a plain thread reply (used for the unsupported-elicitation web link).
PostReply = Callable[["SlackClientProtocol", ThreadKey, str], Awaitable[None]]


@dataclass
class PendingElicitation:
    """An elicitation card in flight during a turn (pure-push model).

    The turn loop keeps reading the stream while the card is shown; a background
    ``resolver`` task awaits the Slack click (or times out) and posts the verdict.
    The pushed ``response.elicitation_resolved`` — or the resolver itself —
    finalizes the card exactly once (``finalized`` guards the race).
    """

    request: ElicitationRequest
    card_ts: str | None
    resolver: asyncio.Task[None] | None = None
    finalized: bool = False
    # The verdict the resolver successfully DELIVERED to the server (a Slack
    # click whose POST returned), or None if it hasn't delivered one (external
    # answer, timeout, or delivery failure) — decides the card's outcome label.
    verdict: Verdict | None = None
    # Set when the resolver declined because nobody answered in time.
    timed_out: bool = False
    # Set when a Slack verdict was clicked but its POST to the server failed, so
    # the server never received it and stays parked on the request.
    delivery_failed: bool = False


@dataclass
class ElicitationTurnState:
    """Per-turn registry of in-flight elicitations, keyed by elicitation_id.

    Owned by the turn loop and passed to each controller call, so the controller
    holds no per-turn state itself (one controller serves all threads).
    """

    pending: dict[str, PendingElicitation] = field(default_factory=dict)


class ElicitationController:
    """Orchestrates elicitation cards for a turn, pure-push style.

    Stateless across turns: all per-turn state lives in the
    :class:`ElicitationTurnState` the caller threads through. Collaborators are
    the shared :class:`ElicitationCoordinator` (bridges the Slack button handler
    to the resolver), a ``post_reply`` for the web-link fallback, and the server
    URL for building that link.
    """

    def __init__(
        self,
        coordinator: ElicitationCoordinator,
        *,
        server_url: str,
        post_reply: PostReply,
        logger: logging.Logger,
    ) -> None:
        self._coordinator = coordinator
        self._server_url = server_url
        self._post_reply = post_reply
        self._logger = logger
        # Live resolver tasks, tracked so shutdown can cancel them rather than
        # leave them pending ("Task was destroyed but it is pending"). Each task
        # removes itself on completion via a done callback.
        self._resolvers: set[asyncio.Task[None]] = set()

    async def shutdown(self) -> None:
        """Cancel any still-running resolver tasks (called on bot shutdown)."""
        tasks = list(self._resolvers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def handle_action(
        self, *, session_id: str, elicitation_id: str, verdict: Verdict
    ) -> bool:
        """Deliver a button/form verdict to the waiting resolver.

        Returns whether a live waiter received it — ``False`` means the request
        already expired or was answered, so the caller can tell the user. The
        verdict can only wake the waiter for its own ``session_id``, so a click
        never resolves another session's elicitation even on an id collision.
        """
        return self._coordinator.resolve(session_id, elicitation_id, verdict)

    async def reject_non_owner_click(
        self, client: SlackClientProtocol, body: dict[str, Any], target: ClickTarget
    ) -> None:
        """Privately tell a non-owner their click on someone else's card was ignored.

        The verdict is NOT delivered (the owner check already blocked it); this is
        just feedback so the clicker isn't left wondering. Channel/thread come from
        the interaction body (a Block Kit action payload).
        """
        channel = (body.get("channel") or {}).get("id")
        clicker = (body.get("user") or {}).get("id")
        message = body.get("message") or {}
        thread_ts = message.get("thread_ts") or message.get("ts")
        if not isinstance(channel, str) or not isinstance(clicker, str):
            return
        try:
            await client.chat_postEphemeral(
                channel=channel,
                user=clicker,
                thread_ts=thread_ts if isinstance(thread_ts, str) else None,
                text=(
                    "This request belongs to whoever started the thread — only they "
                    "can answer it. Start your own thread by mentioning me (or DM me)."
                ),
            )
        except Exception:
            self._logger.warning("Non-owner click ephemeral failed; continuing")

    async def start(
        self,
        omnigent: OmnigentClient,
        turn: SlackTurn,
        request: ElicitationRequest,
        state: ElicitationTurnState,
    ) -> None:
        """Post the elicitation card and spawn its resolver WITHOUT blocking.

        Renders a form (``AskUserQuestion``) or binary Approve/Deny and returns
        immediately so the turn loop keeps reading the stream. A background
        ``resolver`` task awaits the Slack click (or times out) and posts the
        verdict; the pushed ``response.elicitation_resolved`` finalizes the card.

        For an elicitation the bot can't render (a ``url``-mode page or free-form
        typed input), it posts a web-UI link and returns — no card, no resolver;
        the user completes it there and the stream resumes.
        """
        client = turn.slack_client
        key = turn.key
        # Idempotent against a re-delivered elicitation (e.g. the server replaying
        # a request on a stream reconnect): if we've already created a pending for
        # this id THIS TURN — whether still live or already finalized (timed out /
        # declined) — don't post a second card and orphan the first. The
        # coordinator waiter is also replay-safe (register no-ops for a live key).
        if request.elicitation_id in state.pending:
            self._logger.info(
                "Elicitation already seen this turn thread=%s elicitation_id=%s; skipping "
                "duplicate",
                key.display(),
                request.elicitation_id,
            )
            return
        if not request.is_supported:
            await self._post_reply(
                client,
                key,
                (
                    ":link: Omnigent needs input I can't collect here "
                    f"({request.message}). Open the session to respond:\n"
                    f"{self._approve_link(request.session_id, request.elicitation_id)}"
                ),
            )
            self._logger.info(
                "Unsupported elicitation surfaced as web link thread=%s elicitation_id=%s mode=%s",
                key.display(),
                request.elicitation_id,
                request.mode,
            )
            return

        self._logger.info(
            "Elicitation requested thread=%s elicitation_id=%s policy=%s form=%s",
            key.display(),
            request.elicitation_id,
            request.policy_name,
            request.is_form,
        )
        # Register the waiter BEFORE posting the card so a fast click can't reach
        # the action handler before the future exists (lost wakeup).
        self._coordinator.register(request.session_id, request.elicitation_id)
        try:
            posted = await client.chat_postMessage(
                channel=key.channel_id,
                thread_ts=key.reply_ts,
                text="Omnigent needs your input to continue.",
                blocks=elicitation_card_blocks(request, turn.owner_user_id),
            )
        except Exception:
            await self._abandon_unpostable(omnigent, request, key, reason="post failed")
            return
        # No ``ts`` means the card can never be finalized (its buttons can't be
        # replaced), so it would sit live-looking forever even after the verdict
        # is delivered — treat it like a failed post: drop the waiter and decline.
        card_ts = posted.get("ts")
        if not isinstance(card_ts, str):
            await self._abandon_unpostable(omnigent, request, key, reason="no message ts")
            return
        pending = PendingElicitation(request=request, card_ts=card_ts)
        state.pending[request.elicitation_id] = pending
        resolver = asyncio.create_task(self._resolve_verdict(omnigent, request, pending))
        pending.resolver = resolver
        # Track for shutdown cancellation; drop from the set when it finishes.
        self._resolvers.add(resolver)
        resolver.add_done_callback(self._resolvers.discard)

    async def _abandon_unpostable(
        self,
        omnigent: OmnigentClient,
        request: ElicitationRequest,
        key: ThreadKey,
        *,
        reason: str,
    ) -> None:
        """Give up on an elicitation whose card can't be shown/finalized.

        Reached when the card post raises or returns no ``ts`` (so its buttons
        could never be replaced). There's no answerable card and no pending entry
        for ``finish_pending`` to settle, so drop the orphaned waiter and DECLINE
        server-side — otherwise the turn stays parked and later thread messages
        deflect as "needs action". Best-effort; the decline may itself fail.
        """
        self._coordinator.unregister(request.session_id, request.elicitation_id)
        self._logger.warning(
            "Cannot show elicitation card (%s) thread=%s elicitation_id=%s; declining",
            reason,
            key.display(),
            request.elicitation_id,
        )
        with contextlib.suppress(Exception):
            await omnigent.resolve_elicitation(
                request.session_id,
                request.elicitation_id,
                accepted=False,
                content=None,
            )

    async def _resolve_verdict(
        self,
        omnigent: OmnigentClient,
        request: ElicitationRequest,
        pending: PendingElicitation,
    ) -> None:
        """Resolver task: await the Slack verdict, then POST it to the server.

        Runs concurrently with the turn's read loop. If the user answered
        elsewhere, the loop sees ``elicitation_resolved`` first and wakes this
        task with ``RESOLVED_EXTERNALLY`` (via :meth:`on_resolved`), so it never
        posts. On a Slack click it POSTs the verdict FIRST, then records it on
        ``pending`` only once the POST returned — the server then pushes
        ``elicitation_resolved`` back, which finalizes the card. On timeout it
        declines so the server-side park releases. A POST that raises is logged
        and flagged (``delivery_failed``) WITHOUT recording the verdict, so the
        card never shows "Approved/Denied" for an answer the server never got.
        """
        verdict = await self._coordinator.await_verdict(request.session_id, request.elicitation_id)
        if verdict is RESOLVED_EXTERNALLY:
            # Already resolved server-side; post nothing (the loop finalizes).
            return
        content: dict[str, Any] | None = None
        if verdict is None:
            # Nobody answered in time — decline so the server park releases, and
            # flag it so the card shows "Timed out" + retry, not "Denied".
            verdict = Verdict(accepted=False)
            pending.timed_out = True
        elif isinstance(verdict, Verdict) and request.is_form:
            # Form Submit = accept with selections; Cancel = decline. Selections
            # arrive as option indices — map back to the full labels the agent
            # expects (labels can exceed Slack's value cap).
            content = resolve_form_answers(request, verdict.content)
        assert isinstance(verdict, Verdict)
        # POST BEFORE recording: the outcome label must reflect what the server
        # actually received. If the POST raises, the server never got the verdict
        # (still parked), so leave ``verdict`` unset and flag the failure — the
        # card then shows a delivery-failure notice, not a false "Approved".
        try:
            await omnigent.resolve_elicitation(
                request.session_id,
                request.elicitation_id,
                accepted=verdict.accepted,
                content=content,
            )
        except Exception:
            pending.delivery_failed = True
            self._logger.warning(
                "Failed to deliver elicitation verdict to server "
                "session_id=%s elicitation_id=%s accepted=%s",
                request.session_id,
                request.elicitation_id,
                verdict.accepted,
            )
            return
        pending.verdict = verdict

    async def on_resolved(
        self, turn: SlackTurn, elicitation_id: str, state: ElicitationTurnState
    ) -> None:
        """Finalize a resolved elicitation's card (idempotent).

        Fired when the server pushes ``response.elicitation_resolved`` — for our
        own posted verdict or an external answer. Wakes/awaits the resolver and
        replaces the card with its outcome, exactly once.
        """
        pending = state.pending.get(elicitation_id)
        if pending is None or pending.finalized:
            return
        pending.finalized = True
        # Wake the resolver if it's still waiting on a click (external answer):
        # RESOLVED_EXTERNALLY makes it return without posting. If it already
        # posted (our own click), this is a no-op and the resolver just finishes.
        self._coordinator.resolve_external(pending.request.session_id, elicitation_id)
        if pending.resolver is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending.resolver
        outcome = self._outcome(pending)
        self._logger.info(
            "Elicitation resolved thread=%s elicitation_id=%s outcome=%s",
            turn.key.display(),
            elicitation_id,
            outcome.value,
        )
        await self._finalize_card(turn, pending, outcome)

    async def finish_pending(
        self, omnigent: OmnigentClient, turn: SlackTurn, state: ElicitationTurnState
    ) -> None:
        """At turn end, settle any elicitation still in flight.

        Normally every elicitation is finalized by its pushed
        ``elicitation_resolved`` before the turn ends. This is the backstop for a
        turn that ends (or is torn down) with a card still open.

        A card whose resolver already delivered a verdict (or timed out / failed
        delivery) is finalized with that outcome. A card left genuinely
        unanswered — the server is still parked on it — is DECLINED here so the
        server-side park releases, and labelled "abandoned" rather than
        mislabelled "answered elsewhere" (nothing answered it).
        """
        for _eid, pending in list(state.pending.items()):
            if pending.finalized:
                continue
            pending.finalized = True
            resolver = pending.resolver
            if resolver is not None:
                # Stop it waiting on a click so we don't block on its full
                # timeout; if it already delivered/timed-out/failed, awaiting is a
                # no-op and we honor that recorded outcome below.
                resolver.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await resolver
            # The server-side park is released only when a POST actually reached
            # the server: a delivered verdict, or a timeout whose decline POST
            # landed. ``delivery_failed`` means the LAST POST (verdict OR the
            # timeout decline) never landed — so even a timed-out request whose
            # decline failed is still parked. Excluding delivery_failed here routes
            # that compound case into _decline_abandoned (best-effort re-decline)
            # so the session isn't wedged, while keeping the DELIVERY_FAILED label.
            server_park_released = not pending.delivery_failed and (
                pending.verdict is not None or pending.timed_out
            )
            if server_park_released:
                outcome = self._outcome(pending)
            else:
                # Nothing reached the server (unanswered, or delivery failed) and
                # the park is still open — decline to release it (best-effort).
                await self._decline_abandoned(omnigent, pending)
                # Keep an explicit delivery-failure label; otherwise "abandoned".
                outcome = (
                    self._outcome(pending)
                    if pending.delivery_failed
                    else ElicitationOutcome.ABANDONED
                )
            self._logger.info(
                "Elicitation settled at turn end thread=%s elicitation_id=%s outcome=%s",
                turn.key.display(),
                pending.request.elicitation_id,
                outcome.value,
            )
            await self._finalize_card(turn, pending, outcome)

    async def _decline_abandoned(
        self, omnigent: OmnigentClient, pending: PendingElicitation
    ) -> None:
        """Decline an unanswered elicitation at turn end so the server park frees.

        Best-effort: a failed decline is logged, not raised — the turn is already
        ending. The server treats a decline on an already-resolved request as a
        harmless no-op, covering the rare race where a click landed as we tore
        down.
        """
        request = pending.request
        try:
            await omnigent.resolve_elicitation(
                request.session_id,
                request.elicitation_id,
                accepted=False,
                content=None,
            )
        except Exception:
            self._logger.warning(
                "Failed to decline abandoned elicitation session_id=%s elicitation_id=%s",
                request.session_id,
                request.elicitation_id,
            )

    @staticmethod
    def _outcome(pending: PendingElicitation) -> ElicitationOutcome:
        if pending.delivery_failed:
            # A Slack verdict was clicked but its POST to the server failed — the
            # server never got it and is still parked. Say so, don't imply it went
            # through.
            return ElicitationOutcome.DELIVERY_FAILED
        if pending.timed_out:
            return ElicitationOutcome.TIMED_OUT
        verdict = pending.verdict
        if verdict is None:
            # No Slack verdict was delivered — answered elsewhere (web UI/other
            # client). We don't know which way it went — neutral label.
            return ElicitationOutcome.ANSWERED_ELSEWHERE
        if pending.request.is_form:
            return (
                ElicitationOutcome.ANSWERED if verdict.accepted else ElicitationOutcome.CANCELLED
            )
        return ElicitationOutcome.APPROVED if verdict.accepted else ElicitationOutcome.DENIED

    async def _finalize_card(
        self, turn: SlackTurn, pending: PendingElicitation, outcome: ElicitationOutcome
    ) -> None:
        if pending.card_ts is None:
            return
        # Best-effort: replace the card with its outcome (no controls). A failed
        # update must not abort the turn.
        try:
            await turn.slack_client.chat_update(
                channel=turn.key.channel_id,
                ts=pending.card_ts,
                text=f"Request {outcome.value.lower()}.",
                blocks=resolved_card_blocks(pending.request, outcome=outcome),
            )
        except Exception:
            self._logger.warning(
                "Elicitation card update failed thread=%s; continuing", turn.key.display()
            )

    def _approve_link(self, session_id: str, elicitation_id: str) -> str:
        # Deep link to the elicitation's approve page in the Omnigent web UI, so
        # a user can resolve a request the bot can't render in Slack.
        base = self._server_url.rstrip("/")
        return f"{base}/approve/{session_id}/{elicitation_id}"
