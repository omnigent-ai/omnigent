"""Tests for the Pi extension UI elicitation adapter.

Pure-function tests — no HTTP or runtime needed.
"""

from __future__ import annotations

import pytest

from omnigent.pi_extension_ui import (
    PHASE,
    POLICY_NAME,
    is_dialog_method,
    is_fire_and_forget_method,
    timeout_seconds,
    to_elicitation_params,
    to_ui_response,
)
from omnigent.server.schemas import ElicitationResult

_CONFIRM: dict[str, object] = {
    "type": "extension_ui_request",
    "id": "uuid-2",
    "method": "confirm",
    "title": "Clear session?",
    "message": "All messages will be lost.",
}

_SELECT: dict[str, object] = {
    "type": "extension_ui_request",
    "id": "uuid-1",
    "method": "select",
    "title": "Allow dangerous command?",
    "options": ["Allow", "Block"],
}

_INPUT: dict[str, object] = {
    "type": "extension_ui_request",
    "id": "uuid-3",
    "method": "input",
    "title": "Enter a value",
    "placeholder": "type something...",
}

_EDITOR: dict[str, object] = {
    "type": "extension_ui_request",
    "id": "uuid-4",
    "method": "editor",
    "title": "Edit some text",
    "prefill": "Line 1\nLine 2",
}

_EMPTY_SELECT: dict[str, object] = {
    "id": "uuid-empty",
    "method": "select",
    "title": "Anything to add?",
    "options": [],
}


class TestMethodClassification:
    def test_dialog_methods(self) -> None:
        for method in ("confirm", "select", "input", "editor"):
            assert is_dialog_method(method)
            assert not is_fire_and_forget_method(method)

    def test_fire_and_forget_methods(self) -> None:
        for method in ("notify", "setStatus", "setWidget", "setTitle", "set_editor_text"):
            assert is_fire_and_forget_method(method)
            assert not is_dialog_method(method)

    def test_custom_is_neither(self) -> None:
        assert not is_dialog_method("custom")
        assert not is_fire_and_forget_method("custom")


class TestTimeoutSeconds:
    def test_ms_to_seconds(self) -> None:
        assert timeout_seconds({"timeout": 5000}) == 5.0

    def test_missing_or_invalid(self) -> None:
        assert timeout_seconds({}) is None
        assert timeout_seconds({"timeout": 0}) is None
        assert timeout_seconds({"timeout": True}) is None
        assert timeout_seconds({"timeout": "5000"}) is None


class TestToElicitationParamsConfirm:
    def test_binary_card(self) -> None:
        params = to_elicitation_params(_CONFIRM)
        assert params.mode == "form"
        assert params.phase == PHASE
        assert params.policy_name == POLICY_NAME
        extra = params.model_extra or {}
        assert "ask_user_question" not in extra
        assert extra.get("pi_extension_ui") == _CONFIRM

    def test_message_composes_title_and_body_without_markdown(self) -> None:
        params = to_elicitation_params(_CONFIRM)
        assert "Clear session?" in params.message
        assert "All messages will be lost." in params.message
        assert "**" not in params.message


class TestToElicitationParamsSelect:
    def test_stamps_ask_user_question(self) -> None:
        params = to_elicitation_params(_SELECT)
        extra = params.model_extra or {}
        payload = extra["ask_user_question"]
        questions = payload["questions"]
        assert len(questions) == 1
        question = questions[0]
        assert question["id"] == "0"
        assert question["question"] == "Allow dangerous command?"
        assert question["multiSelect"] is False
        assert question["isOther"] is False
        assert [opt["label"] for opt in question["options"]] == ["Allow", "Block"]

    def test_empty_options_become_input(self) -> None:
        params = to_elicitation_params(_EMPTY_SELECT)
        extra = params.model_extra or {}
        question = extra["ask_user_question"]["questions"][0]
        assert question["options"] == []
        assert question["header"] == "Input"
        assert question["isOther"] is True


class TestToElicitationParamsInputEditor:
    def test_input_has_no_dummy_option(self) -> None:
        params = to_elicitation_params(_INPUT)
        extra = params.model_extra or {}
        question = extra["ask_user_question"]["questions"][0]
        assert question["options"] == []
        assert question["header"] == "type something..."
        assert question["isOther"] is True

    def test_input_without_placeholder_uses_input_header(self) -> None:
        params = to_elicitation_params({"id": "uuid-3", "method": "input", "title": "Name?"})
        extra = params.model_extra or {}
        question = extra["ask_user_question"]["questions"][0]
        assert question["options"] == []
        assert question["header"] == "Input"
        assert question["isOther"] is True

    def test_editor_keep_as_is_option(self) -> None:
        params = to_elicitation_params(_EDITOR)
        extra = params.model_extra or {}
        question = extra["ask_user_question"]["questions"][0]
        assert question["options"][0]["label"] == "Keep as-is"
        assert question["options"][0]["preview"] == "Line 1\nLine 2"
        assert question["isOther"] is True


class TestToUiResponse:
    def test_confirm_accept(self) -> None:
        result = ElicitationResult(action="accept")
        assert to_ui_response(_CONFIRM, result) == {
            "type": "extension_ui_response",
            "id": "uuid-2",
            "confirmed": True,
        }

    def test_confirm_decline_is_false_not_interrupt(self) -> None:
        result = ElicitationResult(action="decline")
        assert to_ui_response(_CONFIRM, result) == {
            "type": "extension_ui_response",
            "id": "uuid-2",
            "confirmed": False,
        }

    def test_confirm_timeout_is_false(self) -> None:
        assert to_ui_response(_CONFIRM, None)["confirmed"] is False

    def test_select_accept_value(self) -> None:
        result = ElicitationResult(action="accept", content={"0": "Allow"})
        assert to_ui_response(_SELECT, result) == {
            "type": "extension_ui_response",
            "id": "uuid-1",
            "value": "Allow",
        }

    def test_select_accept_falls_back_to_question_text_key(self) -> None:
        result = ElicitationResult(action="accept", content={"Allow dangerous command?": "Block"})
        assert to_ui_response(_SELECT, result)["value"] == "Block"

    def test_select_decline_is_cancelled(self) -> None:
        result = ElicitationResult(action="decline")
        assert to_ui_response(_SELECT, result) == {
            "type": "extension_ui_response",
            "id": "uuid-1",
            "cancelled": True,
        }

    def test_input_custom_text(self) -> None:
        result = ElicitationResult(action="accept", content={"0": "hello world"})
        assert to_ui_response(_INPUT, result)["value"] == "hello world"

    def test_editor_keep_as_is_returns_prefill(self) -> None:
        result = ElicitationResult(action="accept", content={"0": "Keep as-is"})
        assert to_ui_response(_EDITOR, result)["value"] == "Line 1\nLine 2"

    def test_unknown_method_raises(self) -> None:
        with pytest.raises(ValueError, match="notify"):
            to_elicitation_params({"method": "notify", "message": "hi"})
