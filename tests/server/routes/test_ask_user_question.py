"""Tests for the ``sys_ask_user_question`` protocol adapter.

These are pure-function tests — no HTTP or runtime needed. Covers the
flatten (request-building) and reconstruct (response-building) halves of
the round trip, plus the ``recommended`` extension surviving both.
"""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.server.routes._ask_user_question import (
    build_ask_user_question_params,
    reconstruct_ask_user_question_output,
    validate_ask_user_question_questions,
)
from omnigent.server.schemas import ElicitationResult

_ONE_QUESTION = [
    {
        "question": "Which framework?",
        "header": "Framework",
        "options": [
            {"label": "React", "description": "Component-based UI library."},
            {"label": "Vue", "description": "Progressive framework.", "recommended": True},
        ],
        "multiSelect": False,
    }
]

_MULTI_SELECT_QUESTION = [
    {
        "question": "Which features?",
        "header": "Features",
        "options": [
            {"label": "Auth", "description": "User accounts."},
            {"label": "Billing", "description": "Payments."},
            {"label": "Search", "description": "Full-text search."},
        ],
        "multiSelect": True,
    }
]


# ── validate_ask_user_question_questions ──────────────────────────


class TestValidateAskUserQuestionQuestions:
    def test_valid_single_question(self) -> None:
        result = validate_ask_user_question_questions(_ONE_QUESTION)
        assert len(result) == 1
        assert result[0]["question"] == "Which framework?"
        assert result[0]["options"][0]["recommended"] is False
        assert result[0]["options"][1]["recommended"] is True

    def test_rejects_zero_questions(self) -> None:
        with pytest.raises(OmnigentError, match="1-4 questions"):
            validate_ask_user_question_questions([])

    def test_rejects_more_than_four_questions(self) -> None:
        with pytest.raises(OmnigentError, match="1-4 questions"):
            validate_ask_user_question_questions(_ONE_QUESTION * 5)

    def test_rejects_non_list(self) -> None:
        with pytest.raises(OmnigentError, match="1-4 questions"):
            validate_ask_user_question_questions("not a list")

    def test_rejects_missing_question_text(self) -> None:
        bad = [{**_ONE_QUESTION[0], "question": ""}]
        with pytest.raises(OmnigentError, match="non-empty 'question'"):
            validate_ask_user_question_questions(bad)

    def test_rejects_missing_header(self) -> None:
        bad = [{**_ONE_QUESTION[0], "header": ""}]
        with pytest.raises(OmnigentError, match="non-empty 'header'"):
            validate_ask_user_question_questions(bad)

    def test_rejects_one_option(self) -> None:
        bad = [{**_ONE_QUESTION[0], "options": _ONE_QUESTION[0]["options"][:1]}]
        with pytest.raises(OmnigentError, match="2-4 options"):
            validate_ask_user_question_questions(bad)

    def test_rejects_five_options(self) -> None:
        base_option = _ONE_QUESTION[0]["options"][0]
        bad = [{**_ONE_QUESTION[0], "options": [base_option] * 5}]
        with pytest.raises(OmnigentError, match="2-4 options"):
            validate_ask_user_question_questions(bad)

    def test_rejects_option_missing_description(self) -> None:
        bad_option = {"label": "React"}
        bad = [{**_ONE_QUESTION[0], "options": [bad_option, _ONE_QUESTION[0]["options"][1]]}]
        with pytest.raises(OmnigentError, match="non-empty 'description'"):
            validate_ask_user_question_questions(bad)

    def test_preserves_preview(self) -> None:
        options = [
            {"label": "React", "description": "UI lib.", "preview": "import React"},
            {"label": "Vue", "description": "Progressive."},
        ]
        result = validate_ask_user_question_questions([{**_ONE_QUESTION[0], "options": options}])
        assert result[0]["options"][0]["preview"] == "import React"
        assert "preview" not in result[0]["options"][1]

    def test_demotes_extra_recommended_options_to_at_most_one(self) -> None:
        """A model mistake (multiple 'recommended: true') is normalized,
        not rejected: only the first stays true."""
        options = [
            {"label": "React", "description": "UI lib.", "recommended": True},
            {"label": "Vue", "description": "Progressive.", "recommended": True},
        ]
        result = validate_ask_user_question_questions([{**_ONE_QUESTION[0], "options": options}])
        assert result[0]["options"][0]["recommended"] is True
        assert result[0]["options"][1]["recommended"] is False

    def test_defaults_recommended_to_false(self) -> None:
        options = [
            {"label": "React", "description": "UI lib."},
            {"label": "Vue", "description": "Progressive."},
        ]
        result = validate_ask_user_question_questions([{**_ONE_QUESTION[0], "options": options}])
        assert all(opt["recommended"] is False for opt in result[0]["options"])

    def test_multi_select_flag_preserved(self) -> None:
        result = validate_ask_user_question_questions(_MULTI_SELECT_QUESTION)
        assert result[0]["multiSelect"] is True


# ── build_ask_user_question_params ────────────────────────────────


class TestBuildAskUserQuestionParams:
    def test_builds_form_mode_params_with_no_requested_schema(self) -> None:
        questions = validate_ask_user_question_questions(_ONE_QUESTION)
        params = build_ask_user_question_params(questions)
        assert params.mode == "form"
        assert params.requestedSchema is None
        assert params.url is None

    def test_ask_user_question_extra_carries_recommended_flag(self) -> None:
        """The recommended flag survives from validated input into the
        elicitation params extra the web UI's ApprovalCard reads."""
        questions = validate_ask_user_question_questions(_ONE_QUESTION)
        params = build_ask_user_question_params(questions)
        extra = params.model_extra or {}
        ask_payload = extra["ask_user_question"]
        options = ask_payload["questions"][0]["options"]
        assert options[0]["recommended"] is False
        assert options[1]["recommended"] is True

    def test_message_mentions_question_count(self) -> None:
        one = build_ask_user_question_params(validate_ask_user_question_questions(_ONE_QUESTION))
        assert "question" in one.message.lower()
        two_questions = _ONE_QUESTION + _MULTI_SELECT_QUESTION
        two = build_ask_user_question_params(validate_ask_user_question_questions(two_questions))
        assert "2" in two.message


# ── reconstruct_ask_user_question_output ──────────────────────────


class TestReconstructAskUserQuestionOutput:
    def test_timeout_returns_error_with_echoed_questions(self) -> None:
        questions = validate_ask_user_question_questions(_ONE_QUESTION)
        output = reconstruct_ask_user_question_output(questions, None)
        assert output["questions"] == questions
        assert output["answers"] == {}
        assert "timed out" in output["error"]

    def test_decline_returns_error(self) -> None:
        questions = validate_ask_user_question_questions(_ONE_QUESTION)
        result = ElicitationResult(action="decline")
        output = reconstruct_ask_user_question_output(questions, result)
        assert output["answers"] == {}
        assert "declined" in output["error"]

    def test_cancel_returns_error(self) -> None:
        questions = validate_ask_user_question_questions(_ONE_QUESTION)
        result = ElicitationResult(action="cancel")
        output = reconstruct_ask_user_question_output(questions, result)
        assert output["answers"] == {}
        assert "cancelled" in output["error"]

    def test_single_select_accept_round_trips_scalar_answer(self) -> None:
        questions = validate_ask_user_question_questions(_ONE_QUESTION)
        result = ElicitationResult(action="accept", content={"Which framework?": "Vue"})
        output = reconstruct_ask_user_question_output(questions, result)
        assert output["questions"] == questions
        assert output["answers"] == {"Which framework?": "Vue"}
        assert "response" not in output

    def test_multi_select_accept_round_trips_list_answer(self) -> None:
        questions = validate_ask_user_question_questions(_MULTI_SELECT_QUESTION)
        result = ElicitationResult(
            action="accept", content={"Which features?": ["Auth", "Billing"]}
        )
        output = reconstruct_ask_user_question_output(questions, result)
        assert output["answers"] == {"Which features?": ["Auth", "Billing"]}

    def test_free_text_fallback_becomes_top_level_response(self) -> None:
        """A reply keyed off something other than a known question text
        (e.g. a minimal client answering with one blob of text) surfaces
        as the top-level ``response`` field instead of an answer."""
        questions = validate_ask_user_question_questions(_ONE_QUESTION)
        result = ElicitationResult(action="accept", content={"response": "Just use Vue please"})
        output = reconstruct_ask_user_question_output(questions, result)
        assert output["answers"] == {}
        assert output["response"] == "Just use Vue please"

    def test_accept_with_no_content_is_lossless_empty(self) -> None:
        questions = validate_ask_user_question_questions(_ONE_QUESTION)
        result = ElicitationResult(action="accept", content=None)
        output = reconstruct_ask_user_question_output(questions, result)
        assert output == {"questions": questions, "answers": {}}
