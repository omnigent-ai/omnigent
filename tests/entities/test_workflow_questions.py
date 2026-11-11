"""Workflow questions: validation, the web form they become, and the answers back."""

from __future__ import annotations

import pytest

from omnigent.entities.session_journal import STATUS_TEXT_MAX_CHARS, InvalidJournalEntryError
from omnigent.entities.workflow_questions import (
    answers_from_content,
    normalize_questions,
    question_form,
    status_text,
)


def _question(**fields: object) -> dict[str, object]:
    return {
        "question": "Which  profiles should the release use?",
        "header": "Profiles",
        "options": [
            {"label": "Use staging", "description": "Probe staging first"},
            {"label": "Use prod"},
        ],
        **fields,
    }


def test_questions_are_collapsed_and_keep_an_optional_recommendation() -> None:
    [question] = normalize_questions([_question(recommended="Use prod")])
    assert question == {
        "question": "Which profiles should the release use?",
        "header": "Profiles",
        "options": [
            {"label": "Use staging", "description": "Probe staging first"},
            {"label": "Use prod"},
        ],
        "recommended": "Use prod",
    }
    assert normalize_questions([_question()])[0]["recommended"] is None


@pytest.mark.parametrize(
    ("questions", "message"),
    [
        ([], "questions must list 1 to 3"),
        ([_question()] * 4, "questions must list 1 to 3"),
        ([_question(options=[{"label": "Only one"}])], "options must list 2 to 4"),
        ([_question(options=[{"label": "Same"}, {"label": "Same"}])], "different labels"),
        ([_question(recommended="Use dev")], "recommended must be one of its option labels"),
        ([_question(header="")], "header must not be empty"),
        ([_question(colour="red")], "unknown fields: colour"),
        ([_question(options=[{"label": "A", "icon": "x"}, {"label": "B"}])], "unknown fields"),
    ],
)
def test_invalid_questions_are_refused(questions: object, message: str) -> None:
    with pytest.raises(InvalidJournalEntryError, match=message):
        normalize_questions(questions)


def test_the_form_lists_the_recommendation_first_and_answers_map_back() -> None:
    questions = normalize_questions(
        [_question(recommended="Use prod"), _question(question="Deploy now?", header="Deploy")]
    )
    form = question_form(questions)["questions"]
    assert [question["id"] for question in form] == ["q1", "q2"]
    assert [option["label"] for option in form[0]["options"]] == [
        "Use prod (recommended)",
        "Use staging",
    ]
    assert form[0]["multiSelect"] is False and form[1]["header"] == "Deploy"

    answers = answers_from_content(
        questions, {"q1": "Use prod (recommended)", "q2": " Wait for the freeze "}
    )
    assert answers == [
        {"question": "Which profiles should the release use?", "answer": "Use prod"},
        {"question": "Deploy now?", "answer": "Wait for the freeze"},
    ]
    assert answers_from_content(questions, None)[0]["answer"] == ""


def test_the_status_text_is_the_first_question_cut_to_fit() -> None:
    short = normalize_questions([_question()])
    assert status_text(short) == "Which profiles should the release use?"
    long = normalize_questions([_question(question="Why " * 40)])
    text = status_text(long)
    assert len(text) == STATUS_TEXT_MAX_CHARS and text.endswith("…")
