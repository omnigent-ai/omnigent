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
    # The verdict the resolver posted (a Slack click), or None if it hasn't
    # posted (external answer) — decides the card's outcome label.
    verdict: Verdict | None = None
    # Set when the resolver declined because nobody answered in time.
    timed_out: bool = False


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

    async def handle_action(self, *, elicitation_id: str, verdict: Verdict) -> bool:
        """Deliver a button/form verdict to the waiting resolver.

        Returns whether a live waiter received it — ``False`` means the request
        already expired or was answered, so the caller can tell the user.
        """
        return self._coordinator.resolve(elicitation_id, verdict)

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
        self._coordinator.register(request.elicitation_id)
        posted = await client.chat_postMessage(
            channel=key.channel_id,
            thread_ts=key.thread_ts,
            text="Omnigent needs your input to continue.",
            blocks=elicitation_card_blocks(request, turn.owner_user_id),
        )
        card_ts = posted.get("ts")
        pending = PendingElicitation(
            request=request, card_ts=card_ts if isinstance(card_ts, str) else None
        )
        state.pending[request.elicitation_id] = pending
        pending.resolver = asyncio.create_task(self._resolve_verdict(omnigent, request, pending))

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
        posts. On a Slack click it POSTs the verdict and records it on ``pending``
        (for the card's outcome label); the server then pushes
        ``elicitation_resolved`` back, which finalizes the card. On timeout it
        declines so the server-side park releases.
        """
        verdict = await self._coordinator.await_verdict(request.elicitation_id)
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
        pending.verdict = verdict
        await omnigent.resolve_elicitation(
            request.session_id,
            request.elicitation_id,
            accepted=verdict.accepted,
            content=content,
        )

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
        self._coordinator.resolve_external(elicitation_id)
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

    async def finish_pending(self, turn: SlackTurn, state: ElicitationTurnState) -> None:
        """At turn end, settle any elicitation still in flight.

        Normally every elicitation is finalized by its pushed
        ``elicitation_resolved`` before the turn ends. This is the backstop for a
        turn that ends (or is torn down) with a card still open: wake/await the
        resolver and finalize the card so no resolver task leaks.
        """
        for eid, pending in list(state.pending.items()):
            if not pending.finalized:
                await self.on_resolved(turn, eid, state)

    @staticmethod
    def _outcome(pending: PendingElicitation) -> ElicitationOutcome:
        if pending.timed_out:
            return ElicitationOutcome.TIMED_OUT
        verdict = pending.verdict
        if verdict is None:
            # No Slack verdict was posted — answered elsewhere (web UI/other
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
