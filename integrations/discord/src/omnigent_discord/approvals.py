"""Elicitation vocabulary: the coordinator, the card content, and the verdict.

Everything here is pure — no ``discord`` import — so the decision shapes and
the card copy can be unit-tested without a gateway. ``views.py`` turns a
:class:`Card` into a ``discord.Embed`` and wires the buttons/selects that
produce a :class:`Verdict`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from omnigent_bot_core.events import ElicitationRequest

from omnigent_discord.text import (
    EMBED_DESCRIPTION_LIMIT,
    EMBED_FIELD_VALUE_LIMIT,
    MAX_ACTION_ROWS,
    MAX_SELECT_OPTIONS,
    OPTION_VALUE_LIMIT,
    truncate_for_message,
    truncate_option,
)

# A Discord view holds at most five action rows and the Submit/Cancel row takes
# one, so an ``AskUserQuestion`` form renders at most four select menus. A
# larger form (or one with more options than a select holds) is answered in the
# web UI instead of being silently truncated into a wrong answer.
MAX_FORM_QUESTIONS = MAX_ACTION_ROWS - 1

# Discord embed accent colours, by outcome family.
COLOR_PENDING = 0xF0B232
COLOR_POSITIVE = 0x57F287
COLOR_NEGATIVE = 0xED4245
COLOR_NEUTRAL = 0x99AAB5
COLOR_WARNING = 0xFEE75C


class ElicitationOutcome(str, Enum):
    """Past-tense label shown on a resolved elicitation card.

    Single source of truth shared by the resolver (which picks the outcome from
    the verdict) and :func:`resolved_card` (which renders its icon/colour) — so
    the two can't drift on a bare string. Binary approvals use APPROVED/DENIED;
    forms use ANSWERED/CANCELLED; TIMED_OUT is a no-response decline;
    ANSWERED_ELSEWHERE covers a web-UI/other-client resolution (accept or
    reject, unknown which). DELIVERY_FAILED is a click whose verdict POST to the
    server failed (so the server never got it); ABANDONED is a card left open
    when the turn ended without a resolution (declined server-side to release
    the park).
    """

    APPROVED = "Approved"
    DENIED = "Denied"
    ANSWERED = "Answered"
    CANCELLED = "Cancelled"
    TIMED_OUT = "Timed out"
    ANSWERED_ELSEWHERE = "Answered elsewhere"
    DELIVERY_FAILED = "Couldn't be delivered"
    ABANDONED = "Not answered"


# How long the turn worker waits for an answer before giving up (and declining,
# so the server-side park releases). Bounded so an unanswered request can't hold
# the channel's turn open indefinitely — while a turn streams, follow-up
# messages to that channel are deflected, so a parked card would block them
# until it clears. Kept short: a user who's engaging answers within a couple of
# minutes; if they've walked away, failing fast frees the channel (they can
# re-send). This is only the cap — an answer via the web UI unblocks
# immediately (the server pushes ``elicitation_resolved``).
DEFAULT_ELICITATION_TIMEOUT_SECONDS = 3 * 60

# Returned by ``ElicitationCoordinator.await_verdict`` when the server pushed a
# ``response.elicitation_resolved`` (answered in the web UI or another client)
# rather than a Discord interaction — the caller clears the card but posts no
# verdict.
RESOLVED_EXTERNALLY = object()


@dataclass(frozen=True, slots=True)
class Verdict:
    """A user's answer to an elicitation.

    ``accepted`` picks the MCP action; ``content`` carries form answers for a
    form elicitation, else ``None``. As delivered from the view the answers are
    option indices (``{question_key: index|indices}``); the controller maps them
    to full labels via :func:`resolve_form_answers` before forwarding.
    """

    accepted: bool
    content: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Card:
    """Renderable content for an elicitation embed.

    A plain value so the card copy is testable without constructing a
    ``discord.Embed``; :func:`omnigent_discord.views.to_embed` converts it.
    """

    title: str
    description: str
    color: int
    fields: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ClickTarget:
    """The routing/authorization data an interaction carries.

    Held as attributes on the view rather than packed into a component
    ``custom_id``: Discord caps a custom id at 100 characters, which a session
    id plus an elicitation id can exceed, and a non-persistent view lives
    exactly as long as the in-memory coordinator waiter it answers.
    """

    owner_user_id: str
    session_id: str
    elicitation_id: str


class ElicitationCoordinator:
    """Bridges the turn worker (which awaits a verdict) and the Discord view
    (which delivers it).

    The worker registers a future keyed by ``(session_id, elicitation_id)`` and
    awaits it; the component callback resolves that future when the user
    answers. Both run on the same asyncio loop (discord.py's), so setting the
    future's result from the callback is safe.

    Keying on the *pair* — not the bare ``elicitation_id`` — keeps the
    authorization boundary self-contained: a verdict can only ever wake the
    waiter for its own session, even if the server ever reused an
    ``elicitation_id`` across two concurrently-parked sessions.
    """

    def __init__(self, timeout_seconds: float = DEFAULT_ELICITATION_TIMEOUT_SECONDS) -> None:
        # All access is on the single discord.py event loop, so plain dict ops
        # are safe without a lock. Future result is a Verdict (an interaction)
        # or RESOLVED_EXTERNALLY.
        self._pending: dict[tuple[str, str], asyncio.Future[Verdict | object]] = {}
        self._timeout = timeout_seconds

    def register(self, session_id: str, elicitation_id: str) -> None:
        """Register a waiter for ``(session_id, elicitation_id)`` synchronously.

        Must be called BEFORE the card is posted, so a fast click can't arrive
        at :meth:`resolve` before the future exists (a lost wakeup that would
        silently drop the verdict). Refuses to clobber an existing live waiter —
        a duplicate request for the same key (e.g. a stream-reconnect replay)
        keeps the original future rather than orphaning the worker already
        blocked on it.
        """
        key = (session_id, elicitation_id)
        existing = self._pending.get(key)
        if existing is not None and not existing.done():
            return
        self._pending[key] = asyncio.get_running_loop().create_future()

    def unregister(self, session_id: str, elicitation_id: str) -> None:
        """Drop a registered waiter that will never be awaited.

        Used when posting the card fails after :meth:`register` — the future
        would otherwise be orphaned in ``_pending`` forever. No-op if absent.
        """
        self._pending.pop((session_id, elicitation_id), None)

    async def await_verdict(self, session_id: str, elicitation_id: str) -> Verdict | object | None:
        """Block on the pre-:meth:`register`ed future until answered or timeout.

        Returns the :class:`Verdict` (a Discord interaction),
        :data:`RESOLVED_EXTERNALLY` (the server pushed
        ``elicitation_resolved`` — answered in the web UI or another client, so
        the caller must NOT post its own verdict), or ``None`` when no one
        answered within the timeout (the caller then declines so the server
        doesn't hang). Registers on demand if the caller skipped
        :meth:`register`.
        """
        key = (session_id, elicitation_id)
        future = self._pending.get(key)
        if future is None:
            self.register(session_id, elicitation_id)
            future = self._pending[key]
        try:
            return await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            return None
        finally:
            self._pending.pop(key, None)

    def resolve(self, session_id: str, elicitation_id: str, verdict: Verdict) -> bool:
        """Deliver an interaction verdict to a waiting elicitation.

        Returns whether a live waiter was found — ``False`` means the answer
        arrived after the worker gave up (timeout), a duplicate click, or the
        request was already resolved externally.
        """
        return self._settle(session_id, elicitation_id, verdict)

    def resolve_external(self, session_id: str, elicitation_id: str) -> bool:
        """Signal that the elicitation was resolved on the server (web UI/other).

        The turn loop keeps reading the stream and calls this when it observes a
        pushed ``response.elicitation_resolved``. The waiter wakes with
        :data:`RESOLVED_EXTERNALLY` so it clears the card WITHOUT posting a
        verdict (the server already has one). No-op if already settled — e.g.
        our own click won the race and the server is echoing it back.
        """
        return self._settle(session_id, elicitation_id, RESOLVED_EXTERNALLY)

    def _settle(self, session_id: str, elicitation_id: str, result: Verdict | object) -> bool:
        future = self._pending.get((session_id, elicitation_id))
        if future is None or future.done():
            return False
        future.set_result(result)
        return True


def is_renderable(request: ElicitationRequest) -> bool:
    """Whether this elicitation fits Discord's component budget.

    Beyond the decision-shape check the server-side parser already does
    (``is_supported``), Discord imposes hard structural caps: at most
    :data:`MAX_FORM_QUESTIONS` select menus alongside the Submit/Cancel row, and
    at most :data:`~omnigent_discord.text.MAX_SELECT_OPTIONS` options per menu.
    A form that exceeds either is sent to the web UI rather than rendered with
    questions or options silently missing — a partial form would round-trip a
    wrong answer to the agent.
    """
    if not request.is_supported:
        return False
    if not request.is_form:
        return True
    if len(request.questions) > MAX_FORM_QUESTIONS:
        return False
    return all(len(q.options) <= MAX_SELECT_OPTIONS for q in request.questions)


# A zero-width space between backticks: renders as nothing, but stops a triple
# backtick in prompt-controlled text from closing the fence we wrap it in.
_FENCE_BREAK = "`\u200b`\u200b`"


def _defuse_fences(text: str) -> str:
    """Stop a fence inside ``text`` from closing the block it will be wrapped in."""
    return text.replace("```", _FENCE_BREAK)


def pending_card(request: ElicitationRequest) -> Card:
    """The embed content for an elicitation awaiting an answer."""
    if request.is_form:
        return Card(
            title="Omnigent needs your input",
            description=truncate_for_message(request.message, EMBED_DESCRIPTION_LIMIT),
            color=COLOR_PENDING,
        )
    fields: tuple[tuple[str, str], ...] = ()
    if request.content_preview:
        # A fenced block keeps a command/diff preview readable and stops the
        # markdown inside it from rendering. The preview is the action the agent
        # wants to run, so its text is substantially prompt-controlled: a triple
        # backtick inside it would close the fence early and let the remainder
        # render live. Neutralize those before fencing.
        preview = truncate_for_message(
            _defuse_fences(request.content_preview), EMBED_FIELD_VALUE_LIMIT - 10
        )
        fields = (("Pending action", f"```\n{preview}\n```"),)
    return Card(
        title="🔒 Approval needed",
        description=truncate_for_message(request.message, EMBED_DESCRIPTION_LIMIT),
        color=COLOR_PENDING,
        fields=fields,
    )


_OUTCOME_STYLE: dict[ElicitationOutcome, tuple[str, int]] = {
    ElicitationOutcome.APPROVED: ("✅", COLOR_POSITIVE),
    ElicitationOutcome.ANSWERED: ("✅", COLOR_POSITIVE),
    # "Answered elsewhere" covers accept OR reject in the web UI — neutral
    # styling since we don't know which way it went.
    ElicitationOutcome.ANSWERED_ELSEWHERE: ("ℹ️", COLOR_NEUTRAL),
    ElicitationOutcome.DENIED: ("⛔", COLOR_NEGATIVE),
    ElicitationOutcome.CANCELLED: ("⛔", COLOR_NEGATIVE),
    ElicitationOutcome.DELIVERY_FAILED: ("⚠️", COLOR_WARNING),
    ElicitationOutcome.ABANDONED: ("⌛", COLOR_NEUTRAL),
    ElicitationOutcome.TIMED_OUT: ("⌛", COLOR_NEUTRAL),
}

# Extra guidance appended for the outcomes where the user must act again.
_OUTCOME_NOTE: dict[ElicitationOutcome, str] = {
    # A timeout declines server-side so the channel frees; tell the user the
    # request was dropped and that re-sending starts a fresh attempt.
    ElicitationOutcome.TIMED_OUT: (
        "_No response in time — I declined it. Send your message again to retry._"
    ),
    # The click never reached the server, so it's still parked on this request
    # and the turn can't continue.
    ElicitationOutcome.DELIVERY_FAILED: (
        "_I couldn't deliver your answer to Omnigent. Send your message again to retry._"
    ),
    # The turn ended before this was answered; declined server-side to free the
    # session.
    ElicitationOutcome.ABANDONED: (
        "_This wasn't answered before the turn ended — I declined it. "
        "Send your message again to retry._"
    ),
}


def resolved_card(request: ElicitationRequest, *, outcome: ElicitationOutcome) -> Card:
    """The embed content that replaces a card once it is settled."""
    icon, color = _OUTCOME_STYLE.get(outcome, ("⌛", COLOR_NEUTRAL))
    body = request.message
    note = _OUTCOME_NOTE.get(outcome)
    if note:
        body = f"{body}\n\n{note}"
    return Card(
        title=f"{icon} {outcome.value}",
        description=truncate_for_message(body, EMBED_DESCRIPTION_LIMIT),
        color=color,
    )


def question_options(request: ElicitationRequest) -> list[list[tuple[str, str, str | None]]]:
    """Per-question ``(label, value, description)`` triples for the selects.

    The value is the option INDEX as a string, not the label: Discord caps an
    option value at :data:`~omnigent_discord.text.OPTION_VALUE_LIMIT` (100)
    characters while the agent needs the untruncated label, so the index is
    carried and mapped back in :func:`resolve_form_answers`.
    """
    assert OPTION_VALUE_LIMIT >= 3, "an index must fit an option value"
    return [
        [
            (
                truncate_option(option.label),
                str(index),
                truncate_option(option.description) if option.description else None,
            )
            for index, option in enumerate(question.options)
        ]
        for question in request.questions
    ]


def resolve_form_answers(
    request: ElicitationRequest, raw: dict[str, Any] | None
) -> dict[str, Any]:
    """Map the index-based selection map to full option labels.

    The card carries each option by index, so this resolves indices back to the
    untruncated labels the server forwards to the agent — keyed by each
    question's full ``key``. An index that doesn't resolve to an option is
    dropped; a question with no resolvable answer is omitted.
    """
    if not raw:
        return {}
    by_key = {q.key: q for q in request.questions}
    answers: dict[str, Any] = {}
    for key, value in raw.items():
        question = by_key.get(key)
        if question is None:
            continue
        options = question.options
        if isinstance(value, list):
            resolved = [
                options[i].label
                for item in value
                if (i := _as_index(item)) is not None and i < len(options)
            ]
            if resolved:
                answers[question.key] = resolved
        else:
            index = _as_index(value)
            if index is not None and index < len(options):
                answers[question.key] = options[index].label
    return answers


def _as_index(value: Any) -> int | None:
    if not isinstance(value, str) or not value.isdigit():
        return None
    return int(value)


@dataclass
class Selections:
    """Accumulates a form's per-question selections across select interactions.

    Discord delivers each select menu's choice as its own interaction, so the
    Submit button needs somewhere to read them all back from. Keyed by the
    question's ``key``; values are option indices as strings (single) or lists
    of them (multi-select), exactly the shape :func:`resolve_form_answers`
    consumes.
    """

    values: dict[str, Any] = field(default_factory=dict)

    def record(self, question_key: str, chosen: list[str], *, multi: bool) -> None:
        if not chosen:
            self.values.pop(question_key, None)
            return
        self.values[question_key] = list(chosen) if multi else chosen[0]

    def missing(self, request: ElicitationRequest) -> list[str]:
        """Question texts with no selection yet, for the "answer these" nudge."""
        return [q.question for q in request.questions if q.key not in self.values]


NON_OWNER_NOTICE = (
    "This request belongs to whoever started the session — only they can answer "
    "it. Mention me to start your own."
)
