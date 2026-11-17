"""Workflow questions: validation, the web form they become, and the answers back."""

from __future__ import annotations

import pytest

from omnigent.entities.session_journal import InvalidJournalEntryError
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


@pytest.mark.parametrize("description", [None, "", " \n "])
def test_questions_are_collapsed_and_keep_an_optional_recommendation(
    description: str | None,
) -> None:
    [question] = normalize_questions(
        [
            _question(
                recommended="Use prod",
                options=[
                    {"label": "Use staging", "description": "Probe staging first"},
                    {"label": "Use prod", "description": description},
                ],
            )
        ]
    )
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
        (
            [
                _question(
                    options=[{"label": "Use prod"}, {"label": "Use prod (recommended)"}],
                    recommended="Use prod",
                )
            ],
            "different labels",
        ),
        ([_question(recommended="Use dev")], "recommended must be one of its option labels"),
        ([_question(header="")], "header must not be empty"),
        ([_question(), _question()], "questions must differ"),
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
    assert [question["question"] for question in form] == [
        "Which profiles should the release use?",
        "Deploy now?",
    ]
    assert "id" not in form[0]
    assert [option["label"] for option in form[0]["options"]] == [
        "Use prod (recommended)",
        "Use staging",
    ]
    assert form[0]["multiSelect"] is False and form[1]["header"] == "Deploy"

    answers = answers_from_content(
        questions,
        {
            "Which profiles should the release use?": "Use prod (recommended)",
            "Deploy now?": " Wait for the freeze ",
        },
    )
    assert answers == [
        {"question": "Which profiles should the release use?", "answer": "Use prod"},
        {"question": "Deploy now?", "answer": "Wait for the freeze"},
    ]
    assert answers_from_content(questions, None)[0]["answer"] == ""

    literal_label = "Use prod (recommended)"
    for recommended in (None, "Use staging"):
        literal = normalize_questions(
            [
                _question(
                    options=[
                        {"label": "Use prod"},
                        {"label": literal_label},
                        {"label": "Use staging"},
                    ],
                    recommended=recommended,
                )
            ]
        )
        assert answers_from_content(literal, {literal[0]["question"]: literal_label}) == [
            {"question": literal[0]["question"], "answer": literal_label}
        ]


def test_the_status_text_is_the_whole_first_question() -> None:
    short = normalize_questions([_question()])
    assert status_text(short) == "Which profiles should the release use?"
    long = normalize_questions([_question(question="Why " * 200)])
    assert status_text(long) == ("Why " * 200).strip()


def test_questions_have_no_length_limit_on_their_text() -> None:
    [question] = normalize_questions(
        [
            _question(
                question="Should the release use the staging profiles? " * 20,
                header="Release profiles " * 5,
                options=[
                    {"label": "Use staging " * 10, "description": "Probe it first. " * 30},
                    {"label": "Use prod"},
                ],
            )
        ]
    )
    assert question["question"] == ("Should the release use the staging profiles? " * 20).strip()
    assert question["header"] == ("Release profiles " * 5).strip()
    assert question["options"][0] == {
        "label": ("Use staging " * 10).strip(),
        "description": ("Probe it first. " * 30).strip(),
    }
