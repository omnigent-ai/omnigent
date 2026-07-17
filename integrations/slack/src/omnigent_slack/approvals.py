from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from omnigent_slack.omnigent import ElicitationRequest
from omnigent_slack.text import truncate_for_slack

_logger = logging.getLogger(__name__)

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

    The worker registers a future keyed by ``elicitation_id`` and awaits it;
    the block-action handler resolves that future when the user answers. Both
    run on the same asyncio loop (slack_bolt's), so setting the future's result
    from the handler is safe.
    """

    def __init__(self, timeout_seconds: float = DEFAULT_ELICITATION_TIMEOUT_SECONDS) -> None:
        # All access is on the single slack_bolt event loop (register/await from
        # the turn worker, resolve from the block-action handler), so plain dict
        # ops are safe without a lock.
        self._pending: dict[str, asyncio.Future[Verdict]] = {}
        self._timeout = timeout_seconds

    def register(self, elicitation_id: str) -> None:
        """Register a waiter for ``elicitation_id`` synchronously.

        Must be called BEFORE the approval card is posted, so a fast click can't
        arrive at :meth:`resolve` before the future exists (a lost wakeup that
        would silently drop the verdict). :meth:`await_verdict` then awaits it.
        """
        self._pending[elicitation_id] = asyncio.get_running_loop().create_future()

    async def await_verdict(self, elicitation_id: str) -> Verdict | None:
        """Block on the pre-:meth:`register`ed future until answered or timeout.

        Returns the :class:`Verdict`, or ``None`` when no one answered within
        the timeout (the caller then declines so the server doesn't hang).
        Registers on demand if the caller skipped :meth:`register` (keeps the
        method usable standalone, e.g. in tests).
        """
        future = self._pending.get(elicitation_id)
        if future is None:
            self.register(elicitation_id)
            future = self._pending[elicitation_id]
        try:
            return await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            return None
        finally:
            self._pending.pop(elicitation_id, None)

    def resolve(self, elicitation_id: str, verdict: Verdict) -> bool:
        """Deliver a verdict for a waiting elicitation.

        Returns whether a live waiter was found — ``False`` means the answer
        arrived after the worker gave up (timeout) or a duplicate click, so the
        caller can note the request already closed.
        """
        future = self._pending.get(elicitation_id)
        if future is None or future.done():
            return False
        future.set_result(verdict)
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
                "text": {"type": "plain_text", "text": _plain(opt.label)},
                "value": str(index),
            }
            for index, opt in enumerate(question.options)
        ]
        element = {
            "type": "checkboxes" if question.multi_select else "radio_buttons",
            "action_id": ACTION_FORM_ANSWER,
            "options": options,
        }
        blocks.append(
            {
                "type": "section",
                "block_id": f"{_QUESTION_BLOCK_PREFIX}{_plain(question.key, limit=200)}",
                "text": {"type": "mrkdwn", "text": f"*{_plain(question.question, limit=140)}*"},
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


def resolved_card_blocks(request: ElicitationRequest, *, outcome: str) -> list[dict[str, Any]]:
    """Blocks that replace the card once answered (no controls).

    ``outcome`` is a short past-tense label (``"Approved"``, ``"Denied"``,
    ``"Answered"``, ``"Timed out"``, ``"Cancelled"``).
    """
    icon = {
        "Approved": ":white_check_mark:",
        "Answered": ":white_check_mark:",
        # "Answered elsewhere" covers accept OR reject in the web UI — neutral
        # icon since we don't know which way it went.
        "Answered elsewhere": ":information_source:",
        "Denied": ":no_entry:",
        "Cancelled": ":no_entry:",
    }.get(outcome, ":hourglass:")
    text = f"{icon} *{outcome}*\n{truncate_for_slack(request.message, limit=2000)}"
    if outcome == "Timed out":
        # A timeout declines server-side so the thread's queue is freed; tell the
        # user the request was dropped and that re-sending starts a fresh attempt.
        text += "\n_No response in time — I declined it. Send your message again to retry._"
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _plain(text: str, limit: int = 75) -> str:
    # Slack option text/value are capped (75 chars for option value/text).
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
    by_block_key = {_plain(q.key, limit=200): q for q in request.questions}
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
        self, *, elicitation_id: str, verdict: Verdict
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
        elicitation_id=target.elicitation_id, verdict=Verdict(accepted=accepted, content=content)
    )
    if not delivered:
        _logger.info("Approval click had no waiter elicitation_id=%s", target.elicitation_id)
