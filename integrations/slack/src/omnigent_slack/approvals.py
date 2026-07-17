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

# How long the turn worker waits for a click before giving up. Bounded so a
# thread's worker can't park forever if the user never answers.
DEFAULT_ELICITATION_TIMEOUT_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class Verdict:
    """A user's answer to an elicitation.

    ``accepted`` picks the MCP action; ``content`` carries form answers (the
    ``{question_key: label|labels}`` map) for a form elicitation, else ``None``.
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
        self._pending: dict[str, asyncio.Future[Verdict]] = {}
        self._lock = asyncio.Lock()
        self._timeout = timeout_seconds

    async def await_verdict(self, elicitation_id: str) -> Verdict | None:
        """Block until the elicitation is answered, or the wait times out.

        Returns the :class:`Verdict`, or ``None`` when no one answered within
        the timeout (the caller then declines so the server doesn't hang).
        """
        future: asyncio.Future[Verdict] = asyncio.get_running_loop().create_future()
        async with self._lock:
            self._pending[elicitation_id] = future
        try:
            return await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            return None
        finally:
            async with self._lock:
                self._pending.pop(elicitation_id, None)

    async def resolve(self, elicitation_id: str, verdict: Verdict) -> bool:
        """Deliver a verdict for a waiting elicitation.

        Returns whether a live waiter was found — ``False`` means the answer
        arrived after the worker gave up (timeout) or a duplicate click, so the
        caller can note the request already closed.
        """
        async with self._lock:
            future = self._pending.get(elicitation_id)
        if future is None or future.done():
            return False
        future.set_result(verdict)
        return True


def _resolve_value(request: ElicitationRequest) -> str:
    # "<session_id> <elicitation_id>" — carried on the buttons so the handler
    # routes the verdict without re-deriving the target.
    return f"{request.session_id} {request.elicitation_id}"


def elicitation_card_blocks(request: ElicitationRequest) -> list[dict[str, Any]]:
    """Block Kit blocks for a pending elicitation.

    A form elicitation (``AskUserQuestion``) renders each question as a
    radio/checkbox input plus a Submit; a binary elicitation renders Approve /
    Deny. Both carry the resolve target in their controls.
    """
    if request.is_form:
        return _form_card_blocks(request)
    return _binary_card_blocks(request)


def _binary_card_blocks(request: ElicitationRequest) -> list[dict[str, Any]]:
    value = _resolve_value(request)
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


def _form_card_blocks(request: ElicitationRequest) -> list[dict[str, Any]]:
    value = _resolve_value(request)
    prompt = truncate_for_slack(request.message, limit=2000)
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f":speech_balloon: {prompt}"}}
    ]
    for question in request.questions:
        # Slack option value is capped at 75 chars; the label is what round-trips
        # to the agent, so send the (truncated) label as the value verbatim.
        options = [
            {
                "text": {"type": "plain_text", "text": _plain(opt.label)},
                "value": _plain(opt.label),
            }
            for opt in question.options
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
        "Denied": ":no_entry:",
        "Cancelled": ":no_entry:",
    }.get(outcome, ":hourglass:")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{icon} *{outcome}*\n{truncate_for_slack(request.message, limit=2000)}",
            },
        }
    ]


def _plain(text: str, limit: int = 75) -> str:
    # Slack option text/value are capped (75 chars for option value/text).
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_action_value(value: str) -> tuple[str, str] | None:
    """Split a control ``value`` back into ``(session_id, elicitation_id)``."""
    parts = value.split(" ", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def parse_form_answers(state_values: dict[str, Any]) -> dict[str, Any]:
    """Build the ``{question_key: answer}`` map from a submit's ``state.values``.

    Reads each ``omnigent_q::<key>`` input block: a radio yields the single
    selected option's value (a label); checkboxes yield the list of selected
    labels. Unanswered questions are omitted. Values are the option labels,
    exactly the shape the server forwards to the agent as the tool result.
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
            labels = [
                o["value"]
                for o in multi
                if isinstance(o, dict) and isinstance(o.get("value"), str)
            ]
            if labels:
                answers[key] = labels
    return answers


class _ElicitationSink(Protocol):
    async def handle_elicitation_action(
        self, *, elicitation_id: str, verdict: Verdict
    ) -> bool: ...


async def route_elicitation_click(
    sink: _ElicitationSink,
    body: dict[str, Any],
    *,
    accepted: bool,
    is_form_submit: bool = False,
) -> None:
    """Route a Block Kit interaction to the waiting turn worker.

    Pulls the elicitation id from the clicked control's ``value`` and, for a
    form Submit, the answers from ``state.values``. Hands a :class:`Verdict` to
    ``sink``. A click that arrives after the worker gave up finds no waiter —
    logged and dropped, since the card already shows its outcome.
    """
    actions = body.get("actions") or []
    value = actions[0].get("value") if actions and isinstance(actions[0], dict) else None
    parsed = parse_action_value(value) if isinstance(value, str) else None
    if parsed is None:
        return
    _session_id, elicitation_id = parsed
    content: dict[str, Any] | None = None
    if is_form_submit and accepted:
        state_values = (body.get("state") or {}).get("values") or {}
        content = parse_form_answers(state_values) if isinstance(state_values, dict) else None
    delivered = await sink.handle_elicitation_action(
        elicitation_id=elicitation_id, verdict=Verdict(accepted=accepted, content=content)
    )
    if not delivered:
        _logger.info("Approval click had no waiter elicitation_id=%s", elicitation_id)
