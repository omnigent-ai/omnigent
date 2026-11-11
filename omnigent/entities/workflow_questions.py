"""Questions a workflow's agent asks the user about one of its tasks.

The agent asks with ``sys_workflow_request_input``. The server marks the task
``needs_input`` and raises an elicitation that the web renders with its shared
question form (the ``ask_user_question`` extra), and the runner returns the
answers to the agent. The runner and the server both check questions with
:func:`normalize_questions`. The form answers each question under its text,
as it does Claude Code's AskUserQuestion.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnigent.entities.session_journal import (
    STATUS_TEXT_MAX_CHARS,
    InvalidJournalEntryError,
    _line,
)

QUESTIONS_MAX = 3
OPTIONS_MIN = 2
OPTIONS_MAX = 4
QUESTION_MAX_CHARS = 400
# The short chip label above a question, e.g. "Profiles".
HEADER_MAX_CHARS = 30
OPTION_LABEL_MAX_CHARS = 80
OPTION_DESCRIPTION_MAX_CHARS = 240
_RECOMMENDED_SUFFIX = " (recommended)"


def _option(raw: object, where: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise InvalidJournalEntryError(f"{where} must be an object with a label")
    unknown = set(raw) - {"label", "description"}
    if unknown:
        raise InvalidJournalEntryError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")
    option: dict[str, Any] = {
        "label": _line(f"{where}.label", raw.get("label"), OPTION_LABEL_MAX_CHARS)
    }
    description = raw.get("description")
    if description is not None:
        option["description"] = _line(
            f"{where}.description", description, OPTION_DESCRIPTION_MAX_CHARS
        )
    return option


def _question(raw: object, index: int) -> dict[str, Any]:
    where = f"questions[{index}]"
    if not isinstance(raw, Mapping):
        raise InvalidJournalEntryError(f"{where} must be an object")
    unknown = set(raw) - {"question", "header", "options", "recommended"}
    if unknown:
        raise InvalidJournalEntryError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")
    options_raw = raw.get("options")
    if not isinstance(options_raw, list) or not OPTIONS_MIN <= len(options_raw) <= OPTIONS_MAX:
        raise InvalidJournalEntryError(
            f"{where}.options must list {OPTIONS_MIN} to {OPTIONS_MAX} options"
        )
    options = [_option(option, f"{where}.options[{i}]") for i, option in enumerate(options_raw)]
    labels = [option["label"] for option in options]
    if len(set(labels)) != len(labels):
        raise InvalidJournalEntryError(f"{where}.options must have different labels")
    recommended = raw.get("recommended")
    if recommended is not None:
        recommended = _line(f"{where}.recommended", recommended, OPTION_LABEL_MAX_CHARS)
        if recommended not in labels:
            raise InvalidJournalEntryError(f"{where}.recommended must be one of its option labels")
    return {
        "question": _line(f"{where}.question", raw.get("question"), QUESTION_MAX_CHARS),
        "header": _line(f"{where}.header", raw.get("header"), HEADER_MAX_CHARS),
        "options": options,
        "recommended": recommended,
    }


def normalize_questions(questions: object) -> list[dict[str, Any]]:
    """Check an agent's questions and return them in their stored form.

    :param questions: 1 to :data:`QUESTIONS_MAX` objects, each with a
        ``question``, a short ``header``, :data:`OPTIONS_MIN` to
        :data:`OPTIONS_MAX` ``options`` (a ``label`` and an optional
        ``description``) and an optional ``recommended`` option label.
    :returns: The questions, whitespace collapsed, ``recommended`` always present.
    :raises InvalidJournalEntryError: If a field is missing, unknown, too long, or
        out of range.
    """
    if not isinstance(questions, list) or not 1 <= len(questions) <= QUESTIONS_MAX:
        raise InvalidJournalEntryError(f"questions must list 1 to {QUESTIONS_MAX} questions")
    normalized = [_question(question, index) for index, question in enumerate(questions)]
    texts = [question["question"] for question in normalized]
    if len(set(texts)) != len(texts):
        # The form answers each question under its text.
        raise InvalidJournalEntryError("questions must differ from one another")
    return normalized


def question_form(questions: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``ask_user_question`` extra the web's question form renders.

    The recommended option is listed first and labeled, as Claude Code's
    AskUserQuestion does, so the form needs nothing new.

    :param questions: Output of :func:`normalize_questions`.
    """
    form = []
    for question in questions:
        recommended = question["recommended"]
        options = sorted(question["options"], key=lambda option: option["label"] != recommended)
        form.append(
            {
                "question": question["question"],
                "header": question["header"],
                "multiSelect": False,
                "options": [
                    {
                        **option,
                        "label": option["label"]
                        + (_RECOMMENDED_SUFFIX if option["label"] == recommended else ""),
                    }
                    for option in options
                ],
            }
        )
    return {"questions": form}


def status_text(questions: list[dict[str, Any]]) -> str:
    """The ``needs_input`` text: the first question, cut to fit a status."""
    text: str = questions[0]["question"]
    if len(text) <= STATUS_TEXT_MAX_CHARS:
        return text
    return text[: STATUS_TEXT_MAX_CHARS - 1].rstrip() + "…"


def answers_from_content(
    questions: list[dict[str, Any]], content: Mapping[str, Any] | None
) -> list[dict[str, str]]:
    """Pair each question with the user's answer from the form's ``content``.

    The form answers each question under its text with an option's label (the
    recommended one keeps its " (recommended)" mark, which is dropped here) or
    with text the user wrote.

    :param questions: Output of :func:`normalize_questions`.
    :param content: The resolved elicitation's ``content``, or ``None``.
    :returns: ``[{"question": …, "answer": …}]``, ``""`` for one left unanswered.
    """
    answers = []
    for question in questions:
        raw = (content or {}).get(question["question"])
        if isinstance(raw, list):
            raw = ", ".join(str(item) for item in raw if isinstance(item, str) and item)
        answer = raw if isinstance(raw, str) else ""
        if answer.endswith(_RECOMMENDED_SUFFIX) and answer[: -len(_RECOMMENDED_SUFFIX)] in {
            option["label"] for option in question["options"]
        }:
            answer = answer[: -len(_RECOMMENDED_SUFFIX)]
        answers.append({"question": question["question"], "answer": answer.strip()})
    return answers
