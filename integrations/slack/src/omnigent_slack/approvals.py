from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from omnigent_slack.omnigent import ElicitationRequest
from omnigent_slack.text import truncate_for_slack, truncate_option

_logger = logging.getLogger(__name__)


class ElicitationOutcome(str, Enum):
    """Past-tense label shown on a resolved elicitation card.

    Single source of truth shared by the resolver (which picks the outcome from
    the verdict) and ``resolved_card_blocks`` (which renders its icon/text) — so
    the two can't drift on a bare string. Binary approvals use APPROVED/DENIED;
    forms use ANSWERED/CANCELLED; TIMED_OUT is a no-response decline;
    ANSWERED_ELSEWHERE covers a web-UI/other-client resolution (accept or reject,
    unknown which). DELIVERY_FAILED is a click whose verdict POST to the server
    failed (so the server never got it); ABANDONED is a card left open when the
    turn ended without a resolution (declined server-side to release the park).
    """

    APPROVED = "Approved"
    DENIED = "Denied"
    ANSWERED = "Answered"
    CANCELLED = "Cancelled"
    TIMED_OUT = "Timed out"
    ANSWERED_ELSEWHERE = "Answered elsewhere"
    DELIVERY_FAILED = "Couldn't be delivered"
    ABANDONED = "Not answered"


# Block Kit action ids. Binary approve/deny each carry the resolve target in
# their ``value``; the form Submit does too, while the per-question radio/
# checkbox inputs are read from the submit payload's ``state.values``.
ACTION_APPROVE = "omnigent_approve_tool"
ACTION_DENY = "omnigent_deny_tool"
ACTION_FORM_SUBMIT = "omnigent_form_submit"
ACTION_FORM_CANCEL = "omnigent_form_cancel"
# The radio/checkbox inputs share this action id; they need a (no-op) handler
# registered so Slack doesn't flag an unhandled interaction, but their values
# are read from ``state.values`` at submit time, not on each change.
ACTION_FORM_ANSWER = "omnigent_form_answer"

# Per-question input blocks are keyed ``omnigent_q::<question_key>`` so the
# submit handler can map each answer back to its question without extra state.
_QUESTION_BLOCK_PREFIX = "omnigent_q::"

# How long the turn worker waits for a click before giving up (and declining, so
# the server-side park releases). Bounded so an unanswered request can't hold the
# thread's turn open indefinitely — while a turn streams, follow-up messages to
# that thread are deflected, so a parked card would block them until it clears.
# Kept short: a user who's engaging answers within a couple of minutes; if they've
# walked away, failing fast frees the thread (they can re-send). Note this is only
# the cap — an answer via the web UI unblocks immediately (external-resolution poll).
DEFAULT_ELICITATION_TIMEOUT_SECONDS = 3 * 60

# Returned by ``ElicitationCoordinator.await_verdict`` when the server pushed a
# ``response.elicitation_resolved`` (answered in the web UI or another client)
# rather than a Slack click — the caller clears the card but posts no verdict.
RESOLVED_EXTERNALLY = object()


@dataclass(frozen=True, slots=True)
class Verdict:
    """A user's answer to an elicitation.

    ``accepted`` picks the MCP action; ``content`` carries form answers for a
    form elicitation, else ``None``. As delivered from the click handler the
    answers are option indices (``{question_key: index|indices}``); the service
    maps them to full labels via :func:`resolve_form_answers` before forwarding.
    """

    accepted: bool
    content: dict[str, Any] | None = None


class ElicitationCoordinator:
    """Bridges the turn worker (which blocks awaiting a verdict) and the Slack
    button handler (which delivers it).

    The worker registers a future keyed by ``(session_id, elicitation_id)`` and
    awaits it; the block-action handler resolves that future when the user
    answers. Both run on the same asyncio loop (slack_bolt's), so setting the
    future's result from the handler is safe.

    Keying on the *pair* — not the bare ``elicitation_id`` — keeps the
    authorization boundary self-contained: a verdict can only ever wake the
    waiter for its own session, even if the server ever reused an
    ``elicitation_id`` across two concurrently-parked sessions. Without the
    session in the key, user A's legitimately-owned click could resolve a
    colliding id now owned by user B's resolver, posting a verdict to B's session.
    """

    def __init__(self, timeout_seconds: float = DEFAULT_ELICITATION_TIMEOUT_SECONDS) -> None:
        # All access is on the single slack_bolt event loop (register/await from
        # the turn worker, resolve from the block-action handler), so plain dict
        # ops are safe without a lock.
        # Future result is a Verdict (Slack click) or RESOLVED_EXTERNALLY.
        self._pending: dict[tuple[str, str], asyncio.Future[Verdict | object]] = {}
        self._timeout = timeout_seconds

    def register(self, session_id: str, elicitation_id: str) -> None:
        """Register a waiter for ``(session_id, elicitation_id)`` synchronously.

        Must be called BEFORE the approval card is posted, so a fast click can't
        arrive at :meth:`resolve` before the future exists (a lost wakeup that
        would silently drop the verdict). :meth:`await_verdict` then awaits it.
        Refuses to clobber an existing live waiter — a duplicate request for the
        same key (e.g. a stream-reconnect replay) keeps the original future
        rather than orphaning the worker already blocked on it.
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

        Returns the :class:`Verdict` (a Slack click), :data:`RESOLVED_EXTERNALLY`
        (the server pushed ``elicitation_resolved`` — answered in the web UI or
        another client, so the caller must NOT post its own verdict), or ``None``
        when no one answered within the timeout (the caller then declines so the
        server doesn't hang). Registers on demand if the caller skipped
        :meth:`register` (keeps the method usable standalone, e.g. in tests).
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
        """Deliver a Slack-click verdict to a waiting elicitation.

        Returns whether a live waiter was found — ``False`` means the answer
        arrived after the worker gave up (timeout), a duplicate click, or the
        request was already resolved externally, so the caller can note it closed.
        """
        return self._settle(session_id, elicitation_id, verdict)

    def resolve_external(self, session_id: str, elicitation_id: str) -> bool:
        """Signal that the elicitation was resolved on the server (web UI/other).

        The turn loop keeps reading the stream and calls this when it observes a
        pushed ``response.elicitation_resolved``. The waiter wakes with
        :data:`RESOLVED_EXTERNALLY` so it clears the card WITHOUT posting a
        verdict (the server already has one). No-op if already settled — e.g. our
        own click won the race and the server is just echoing it back.
        """
        return self._settle(session_id, elicitation_id, RESOLVED_EXTERNALLY)

    def _settle(self, session_id: str, elicitation_id: str, result: Verdict | object) -> bool:
        future = self._pending.get((session_id, elicitation_id))
        if future is None or future.done():
            return False
        future.set_result(result)
        return True


def _resolve_value(request: ElicitationRequest, owner_user_id: str) -> str:
    # "<owner> <session_id> <elicitation_id>" — carried on every control so the
    # handler can (a) route the verdict to the right session and (b) verify the
    # clicking user is the thread owner before resolving (authorization gate).
    return f"{owner_user_id} {request.session_id} {request.elicitation_id}"


def elicitation_card_blocks(
    request: ElicitationRequest, owner_user_id: str
) -> list[dict[str, Any]]:
    """Block Kit blocks for a pending elicitation.

    A form elicitation (``AskUserQuestion``) renders each question as a
    radio/checkbox input plus a Submit; a binary elicitation renders Approve /
    Deny. Both controls carry the resolve target AND the owner id, so a
    non-owner's click can be rejected even though the card is visible to the
    whole channel.
    """
    if request.is_form:
        return _form_card_blocks(request, owner_user_id)
    return _binary_card_blocks(request, owner_user_id)


def _binary_card_blocks(request: ElicitationRequest, owner_user_id: str) -> list[dict[str, Any]]:
    value = _resolve_value(request, owner_user_id)
    prompt = truncate_for_slack(request.message, limit=2000)
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":lock: *Approval needed*\n{prompt}"},
        }
    ]
    if request.content_preview:
        preview = truncate_for_slack(request.content_preview, limit=2500)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{preview}```"}})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": ACTION_APPROVE,
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": ACTION_DENY,
                    "value": value,
                },
            ],
        }
    )
    return blocks


def _form_card_blocks(request: ElicitationRequest, owner_user_id: str) -> list[dict[str, Any]]:
    value = _resolve_value(request, owner_user_id)
    prompt = truncate_for_slack(request.message, limit=2000)
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f":speech_balloon: {prompt}"}}
    ]
    for question in request.questions:
        # Slack caps the option value at 75 chars, but the agent needs the FULL
        # label — so carry the option INDEX as the value (short, unique) and
        # display the (possibly truncated) label as text. The index is mapped
        # back to the untruncated label at resolve time (`resolve_form_answers`).
        options = [
            {
                "text": {"type": "plain_text", "text": truncate_option(opt.label)},
                "value": str(index),
            }
            for index, opt in enumerate(question.options)
        ]
        element = {
            "type": "checkboxes" if question.multi_select else "radio_buttons",
            "action_id": ACTION_FORM_ANSWER,
            "options": options,
        }
        block_key = truncate_option(question.key, limit=200)
        label = truncate_option(question.question, limit=140)
        blocks.append(
            {
                "type": "section",
                "block_id": f"{_QUESTION_BLOCK_PREFIX}{block_key}",
                "text": {"type": "mrkdwn", "text": f"*{label}*"},
                "accessory": element,
            }
        )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Submit"},
                    "style": "primary",
                    "action_id": ACTION_FORM_SUBMIT,
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "action_id": ACTION_FORM_CANCEL,
                    "value": value,
                },
            ],
        }
    )
    return blocks


def resolved_card_blocks(
    request: ElicitationRequest, *, outcome: ElicitationOutcome
) -> list[dict[str, Any]]:
    """Blocks that replace the card once answered (no controls)."""
    icon = {
        ElicitationOutcome.APPROVED: ":white_check_mark:",
        ElicitationOutcome.ANSWERED: ":white_check_mark:",
        # "Answered elsewhere" covers accept OR reject in the web UI — neutral
        # icon since we don't know which way it went.
        ElicitationOutcome.ANSWERED_ELSEWHERE: ":information_source:",
        ElicitationOutcome.DENIED: ":no_entry:",
        ElicitationOutcome.CANCELLED: ":no_entry:",
        ElicitationOutcome.DELIVERY_FAILED: ":warning:",
        ElicitationOutcome.ABANDONED: ":hourglass:",
    }.get(outcome, ":hourglass:")
    text = f"{icon} *{outcome.value}*\n{truncate_for_slack(request.message, limit=2000)}"
    if outcome is ElicitationOutcome.TIMED_OUT:
        # A timeout declines server-side so the thread frees; tell the user the
        # request was dropped and that re-sending starts a fresh attempt.
        text += "\n_No response in time — I declined it. Send your message again to retry._"
    elif outcome is ElicitationOutcome.DELIVERY_FAILED:
        # The click never reached the server, so it's still parked on this request
        # — the turn can't continue. Re-sending starts a fresh attempt.
        text += "\n_I couldn't deliver your answer to Omnigent. Send your message again to retry._"
    elif outcome is ElicitationOutcome.ABANDONED:
        # The turn ended before this was answered; declined server-side to free
        # the session. Re-sending starts a fresh attempt.
        text += (
            "\n_This wasn't answered before the turn ended — I declined it. "
            "Send your message again to retry._"
        )
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


@dataclass(frozen=True, slots=True)
class ClickTarget:
    """The routing/authorization data carried on an elicitation control."""

    owner_user_id: str
    session_id: str
    elicitation_id: str


def parse_action_value(value: str) -> ClickTarget | None:
    """Parse a control ``value`` into its owner / session / elicitation ids."""
    parts = value.split(" ", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return ClickTarget(owner_user_id=parts[0], session_id=parts[1], elicitation_id=parts[2])


def parse_form_answers(state_values: dict[str, Any]) -> dict[str, Any]:
    """Build the ``{question_key: option_index}`` map from a submit's ``state.values``.

    Reads each ``omnigent_q::<key>`` input block: a radio yields the single
    selected option's value; checkboxes yield the list of selected values.
    Option values are the option INDEX (as a string), not the label — the label
    can exceed Slack's 75-char value cap, so it's carried by index and mapped
    back to the full label in :func:`resolve_form_answers`. Unanswered questions
    are omitted.
    """
    answers: dict[str, Any] = {}
    for block_id, actions in state_values.items():
        if not isinstance(block_id, str) or not block_id.startswith(_QUESTION_BLOCK_PREFIX):
            continue
        if not isinstance(actions, dict):
            continue
        state = actions.get(ACTION_FORM_ANSWER)
        if not isinstance(state, dict):
            continue
        key = block_id[len(_QUESTION_BLOCK_PREFIX) :]
        selected = state.get("selected_option")
        if isinstance(selected, dict) and isinstance(selected.get("value"), str):
            answers[key] = selected["value"]
            continue
        multi = state.get("selected_options")
        if isinstance(multi, list):
            indices = [
                o["value"]
                for o in multi
                if isinstance(o, dict) and isinstance(o.get("value"), str)
            ]
            if indices:
                answers[key] = indices
    return answers


def resolve_form_answers(
    request: ElicitationRequest, raw: dict[str, Any] | None
) -> dict[str, Any]:
    """Map the index-based ``parse_form_answers`` map to full option labels.

    The card carries each option by index (labels can exceed Slack's 75-char
    value cap), so this resolves indices back to the untruncated labels the
    server forwards to the agent — keyed by each question's full ``key``. An
    index that doesn't resolve to an option is dropped; a question with no
    resolvable answer is omitted.
    """
    if not raw:
        return {}
    # Match each answer's (possibly truncated) block key back to its question.
    by_block_key = {truncate_option(q.key, limit=200): q for q in request.questions}
    answers: dict[str, Any] = {}
    for block_key, value in raw.items():
        question = by_block_key.get(block_key)
        if question is None:
            continue
        labels = question.options
        if isinstance(value, list):
            resolved = [
                labels[i].label
                for s in value
                if (i := _as_index(s)) is not None and i < len(labels)
            ]
            if resolved:
                answers[question.key] = resolved
        else:
            i = _as_index(value)
            if i is not None and i < len(labels):
                answers[question.key] = labels[i].label
    return answers


def _as_index(value: Any) -> int | None:
    if not isinstance(value, str) or not value.isdigit():
        return None
    return int(value)


class _ElicitationSink(Protocol):
    async def handle_elicitation_action(
        self, *, session_id: str, elicitation_id: str, verdict: Verdict
    ) -> bool: ...

    async def reject_non_owner_click(
        self, client: Any, body: dict[str, Any], target: ClickTarget
    ) -> None: ...


def _clicking_user_id(body: dict[str, Any]) -> str | None:
    user = body.get("user")
    uid = user.get("id") if isinstance(user, dict) else None
    return uid if isinstance(uid, str) else None


async def route_elicitation_click(
    sink: _ElicitationSink,
    client: Any,
    body: dict[str, Any],
    *,
    accepted: bool,
    is_form_submit: bool = False,
) -> None:
    """Route a Block Kit interaction to the waiting turn worker.

    Enforces the per-thread owner boundary: the control carries the owner id, so
    a click from anyone else (the card is visible channel-wide) is rejected
    before any verdict is delivered — fail-safe, matching the message-routing
    owner check. Otherwise hands a :class:`Verdict` to ``sink``; a click that
    arrives after the worker gave up finds no waiter and is dropped.
    """
    actions = body.get("actions") or []
    value = actions[0].get("value") if actions and isinstance(actions[0], dict) else None
    target = parse_action_value(value) if isinstance(value, str) else None
    if target is None:
        return

    clicker = _clicking_user_id(body)
    if clicker != target.owner_user_id:
        _logger.info(
            "Rejecting non-owner elicitation click elicitation_id=%s owner=%s clicker=%s",
            target.elicitation_id,
            target.owner_user_id,
            clicker,
        )
        await sink.reject_non_owner_click(client, body, target)
        return

    content: dict[str, Any] | None = None
    if is_form_submit and accepted:
        state_values = (body.get("state") or {}).get("values") or {}
        content = parse_form_answers(state_values) if isinstance(state_values, dict) else None
    delivered = await sink.handle_elicitation_action(
        session_id=target.session_id,
        elicitation_id=target.elicitation_id,
        verdict=Verdict(accepted=accepted, content=content),
    )
    if not delivered:
        _logger.info("Approval click had no waiter elicitation_id=%s", target.elicitation_id)
