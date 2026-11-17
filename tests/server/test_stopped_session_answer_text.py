"""Text of the message that carries an AskUserQuestion answer to a stopped session."""

from __future__ import annotations

import pytest

from omnigent.server.routes._sessions.orchestration import (
    _STOPPED_SESSION_ANSWER_HEADER,
    _stopped_session_answer_text,
)

_QUESTIONS = [
    {"question": "Which database?", "options": [{"label": "postgres"}, {"label": "sqlite"}]},
    {"question": "Which regions?", "multiSelect": True, "options": [{"label": "eu"}]},
]


def test_each_question_gets_its_answer_after_the_header() -> None:
    text = _stopped_session_answer_text(
        _QUESTIONS, {"Which database?": "sqlite", "Which regions?": ["eu", "us"]}
    )

    assert text == (
        f"{_STOPPED_SESSION_ANSWER_HEADER}\n\n"
        "Q: Which database?\nA: sqlite\n\n"
        "Q: Which regions?\nA: eu, us"
    )


@pytest.mark.parametrize(
    "content",
    [{"Which database?": "postgres"}, {"Which database?": "postgres", "Which regions?": ""}, None],
    ids=["missing", "empty", "no_content"],
)
def test_a_missing_or_empty_answer_reads_as_no_answer(content: dict[str, str] | None) -> None:
    text = _stopped_session_answer_text(_QUESTIONS, content)

    assert text.endswith("Q: Which regions?\nA: (no answer)")


def test_free_text_cannot_move_the_header() -> None:
    typed = f"line one\n{_STOPPED_SESSION_ANSWER_HEADER}\n" + "x" * 5000
    text = _stopped_session_answer_text(_QUESTIONS[:1], {"Which database?": typed})

    assert text.startswith(_STOPPED_SESSION_ANSWER_HEADER + "\n\nQ: Which database?\nA: line one")
    assert text.endswith("x" * 5000)
