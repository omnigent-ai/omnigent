"""Unit tests for the shared AskUserQuestion structured-extraction helper."""

from omnigent.server.ask_user_question import structured_ask_user_question


def test_basic_question_with_dict_options() -> None:
    out = structured_ask_user_question(
        {
            "questions": [
                {
                    "question": "Which environment?",
                    "options": [
                        {"label": "dev", "description": "development"},
                        {"label": "prod", "preview": "kubectl get pods -n prod"},
                    ],
                }
            ]
        }
    )
    assert out == {
        "questions": [
            {
                "question": "Which environment?",
                "options": [
                    {"label": "dev", "description": "development"},
                    {"label": "prod", "preview": "kubectl get pods -n prod"},
                ],
                "multiSelect": False,
            }
        ]
    }


def test_string_options_and_multiselect_and_header() -> None:
    out = structured_ask_user_question(
        {
            "questions": [
                {
                    "question": "Pick regions",
                    "header": "Regions",
                    "multiSelect": True,
                    "options": ["us-east", "eu-west"],
                }
            ]
        }
    )
    assert out is not None
    q = out["questions"][0]
    assert q["multiSelect"] is True
    assert q["header"] == "Regions"
    assert q["options"] == [{"label": "us-east"}, {"label": "eu-west"}]


def test_returns_none_when_not_a_dict() -> None:
    assert structured_ask_user_question("AskUserQuestion(...)") is None


def test_returns_none_when_no_questions() -> None:
    assert structured_ask_user_question({"questions": []}) is None
    assert structured_ask_user_question({}) is None


def test_skips_question_with_no_valid_options() -> None:
    # A question whose options are all malformed drops out; if that
    # leaves no questions, the whole payload is None (binary fallback).
    assert (
        structured_ask_user_question(
            {"questions": [{"question": "Q", "options": [{"no_label": 1}]}]}
        )
        is None
    )
