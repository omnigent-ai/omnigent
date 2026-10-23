"""Pi protocol conversion, shared by SDK and native transports."""

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


@pytest.mark.parametrize(
    "methods, dialog, notification",
    [
        (["confirm", "select", "input", "editor"], True, False),
        (["notify", "setStatus", "setWidget", "setTitle", "set_editor_text"], False, True),
        (["custom"], False, False),
    ],
)
def test_method_classification(methods: list[str], dialog: bool, notification: bool) -> None:
    for method in methods:
        assert is_dialog_method(method) is dialog
        assert is_fire_and_forget_method(method) is notification


@pytest.mark.parametrize(
    "dialog_request, expected",
    [
        ({"timeout": 5000}, 5.0),
        ({}, None),
        ({"timeout": 0}, None),
        ({"timeout": True}, None),
        ({"timeout": "5000"}, None),
    ],
)
def test_timeout(dialog_request: dict, expected: float | None) -> None:
    assert timeout_seconds(dialog_request) == expected


@pytest.mark.parametrize(
    "dialog_request, options, header, is_other",
    [
        (
            {"method": "select", "options": ["Allow", "Block"]},
            [{"label": "Allow"}, {"label": "Block"}],
            None,
            False,
        ),
        ({"method": "select", "options": []}, [], "Input", True),
        ({"method": "input", "placeholder": "Name"}, [], "Name", True),
        ({"method": "input"}, [], "Input", True),
        (
            {"method": "editor", "prefill": "one\ntwo"},
            [{"label": "Keep as-is", "preview": "one\ntwo"}],
            "Editor",
            True,
        ),
    ],
)
def test_question_schema(
    dialog_request: dict, options: list, header: str | None, is_other: bool
) -> None:
    dialog_request = {"id": "dialog", "title": "Continue?", **dialog_request}
    params = to_elicitation_params(dialog_request)
    assert (params.mode, params.phase, params.policy_name) == ("form", PHASE, POLICY_NAME)
    assert params.model_extra["pi_extension_ui"] == dialog_request
    assert params.model_extra["ask_user_question"]["questions"] == [
        {
            "id": "0",
            "question": "Continue?",
            "options": options,
            "multiSelect": False,
            "isOther": is_other,
            **({"header": header} if header else {}),
        }
    ]


def test_confirmation_schema() -> None:
    params = to_elicitation_params({"method": "confirm", "title": "Clear?", "message": "Lost."})
    assert params.mode == "form" and params.message == "Clear?\n\nLost."
    assert "ask_user_question" not in params.model_extra
    with pytest.raises(ValueError, match="notify"):
        to_elicitation_params({"method": "notify"})


@pytest.mark.parametrize(
    "action, confirmed", [("accept", True), ("decline", False), (None, False)]
)
def test_confirm_answer(action: str | None, confirmed: bool) -> None:
    result = ElicitationResult(action=action) if action else None
    assert to_ui_response({"id": "dialog", "method": "confirm"}, result) == {
        "type": "extension_ui_response",
        "id": "dialog",
        "confirmed": confirmed,
    }


@pytest.mark.parametrize(
    "method, action, content, expected",
    [
        ("select", "accept", {"0": "Allow"}, {"value": "Allow"}),
        ("select", "accept", {"0": ["", "Allow"]}, {"value": "Allow"}),
        ("select", "accept", {"Choice?": "Block"}, {"value": "Block"}),
        ("select", "accept", {"0": "Unlisted"}, {"cancelled": True}),
        ("select", "accept", {"0": ["", "Unlisted"]}, {"cancelled": True}),
        ("select", "decline", None, {"cancelled": True}),
        ("input", "accept", {"0": "hello world"}, {"value": "hello world"}),
        ("editor", "accept", {"0": "Keep as-is"}, {"value": "one\ntwo"}),
    ],
)
def test_text_answer(method: str, action: str, content: dict | None, expected: dict) -> None:
    dialog_request = {
        "id": "dialog",
        "method": method,
        "title": "Choice?",
        "options": ["Allow", "Block"],
        "prefill": "one\ntwo",
    }
    assert to_ui_response(dialog_request, ElicitationResult(action=action, content=content)) == {
        "type": "extension_ui_response",
        "id": "dialog",
        **expected,
    }
