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
            [],
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
            **(
                {"textInput": {"default": "one\ntwo"}}
                if dialog_request["method"] == "editor"
                else {}
            ),
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
        ("editor", "accept", {"0": "Keep as-is"}, {"value": "Keep as-is"}),
        ("editor", "accept", {"0": ""}, {"value": ""}),
        ("editor", "accept", {"0": "  one\ntwo\n"}, {"value": "  one\ntwo\n"}),
        ("editor", "decline", None, {"cancelled": True}),
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


def _omp_form_request() -> dict:
    return {
        "id": "native-form",
        "method": "formDialog",
        "title": "Configure deployment",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "f0": {"type": "string", "x-multiline": True, "x-preserve-empty": True},
                "f1": {"type": "number", "minimum": 1, "maximum": 8},
                "f2_0": {"type": "boolean", "default": True},
                "f2_1": {"type": "boolean", "default": False},
                "f3": {"type": "number", "x-preserve-empty": True},
            },
            "required": ["f1"],
        },
    }


def test_omp_form_schema_uses_safe_controls_without_changing_pi_methods() -> None:
    request = _omp_form_request()
    params = to_elicitation_params(request, harness="omp-native")
    assert params.requestedSchema == request["requestedSchema"]
    assert params.phase == "omp_extension_ui"
    assert params.policy_name == "omp_native_extension_ui"
    assert params.model_extra["omp_extension_ui"] == request
    assert "ask_user_question" not in params.model_extra
    assert not is_dialog_method("formDialog")
    with pytest.raises(ValueError, match="formDialog"):
        to_elicitation_params(request)


def test_omp_form_response_preserves_native_typed_and_exact_text_answers() -> None:
    content = {
        "f0": "line one\n\n",
        "f1": 3.5,
        "f2_0": False,
        "f2_1": True,
        "f3": "",
        "unrequested": "ignored",
    }
    result = to_ui_response(
        _omp_form_request(), ElicitationResult(action="accept", content=content)
    )
    assert result == {
        "type": "extension_ui_response",
        "id": "native-form",
        "value": {key: value for key, value in content.items() if key != "unrequested"},
    }


@pytest.mark.parametrize("action", ["cancel", "decline"])
def test_omp_form_cancel_never_becomes_timeout_or_default_submission(action: str) -> None:
    response = to_ui_response(
        _omp_form_request(), ElicitationResult(action=action), timed_out=True
    )
    assert response["cancelled"] is True
    assert response["timedOut"] is False
    assert "value" not in response
    expired = to_ui_response(_omp_form_request(), None, timed_out=True)
    assert expired["cancelled"] is True and expired["timedOut"] is True


@pytest.mark.parametrize("content", [{"f1": True}, {"f2_0": "true"}, {"f0": 3}])
def test_omp_form_rejects_wrong_primitive_answer_shapes(content: dict) -> None:
    assert to_ui_response(
        _omp_form_request(), ElicitationResult(action="accept", content=content)
    )["cancelled"]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": []},
        {"type": "object", "properties": {"x": {"type": "object"}}},
        {"type": "object", "properties": {}, "required": ["missing"]},
    ],
)
def test_omp_form_rejects_nonprimitive_or_malformed_schema(schema: dict) -> None:
    with pytest.raises(ValueError, match="OMP formDialog"):
        to_elicitation_params(
            {**_omp_form_request(), "requestedSchema": schema}, harness="omp-native"
        )
