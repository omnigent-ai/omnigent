"""Native Devin question hooks, using payloads captured from its real TUI."""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from omnigent.harnesses.devin_native import hook
from omnigent.harnesses.devin_native.questions import answered_question_input, question_payload

_COLOR = "Which color should the demo use?"
_FEATURES = "Which features should the demo include?"
_INPUT = {
    "questions": [
        {
            "question": _COLOR,
            "header": "Demo color",
            "multi_select": False,
            "options": [
                {"label": "Blue", "description": "A calm, classic color."},
                {"label": "Green", "description": "A fresh, natural color."},
            ],
        },
        {
            "question": _FEATURES,
            "header": "Features",
            "multi_select": True,
            "options": [
                {"label": "Search", "description": "Find demo content quickly."},
                {"label": "Export", "description": "Save demo output."},
                {"label": "Sharing", "description": "Share the demo with others."},
            ],
        },
    ],
}


def _payload() -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "ask_user_question",
        "tool_input": copy.deepcopy(_INPUT),
        "tool_use_id": "ask_user_question:0#test",
        "session_id": "native-session",
        "prompt_id": "native-turn",
    }


def _http(monkeypatch: pytest.MonkeyPatch, responses: list[object]) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    client = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        result = responses[min(len(requests) - 1, len(responses) - 1)]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)

    monkeypatch.setattr(
        hook.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(hook, "policy_hook_request_headers", dict)
    monkeypatch.setattr(hook, "policy_hook_reauth", lambda *_: lambda: None)
    return requests


def _mirror() -> dict | None:
    return hook._mirror_user_question(
        _payload(), server_url="https://example.test", session_id="conv_test"
    )


def test_question_payload_keeps_all_questions_options_and_descriptions() -> None:
    tool_input = copy.deepcopy(_INPUT)
    tool_input["questions"][1]["options"][0]["description"] = "Long description " * 200
    form = question_payload(tool_input)
    assert form is not None
    assert form["questions"] == [
        {key: value for key, value in question.items() if key != "multi_select"}
        | {"multiSelect": question["multi_select"]}
        for question in tool_input["questions"]
    ]
    assert "multiSelect" not in tool_input["questions"][0]


@pytest.mark.parametrize(
    "tool_input",
    [None, {}, {"questions": []}, {"questions": [None]}, {"questions": [{"question": "Q"}]}],
)
def test_malformed_questions_defer_to_devin(tool_input: object) -> None:
    assert question_payload(tool_input) is None


@pytest.mark.parametrize("answers", [{}, {_COLOR: {"selected": ["Green"]}}])
def test_supplied_native_answers_do_not_prompt_again(answers: dict) -> None:
    assert question_payload({**_INPUT, "answers": answers}) is None


def test_native_answers_preserve_single_multiple_and_custom_values() -> None:
    result = answered_question_input(
        _INPUT, {_COLOR: "Purple with gold accents", _FEATURES: ["Search", "Export", "Offline"]}
    )
    assert result["answers"] == {
        _COLOR: {"selected": [], "custom_text": "Purple with gold accents"},
        _FEATURES: {"selected": ["Search", "Export"], "custom_text": "Offline"},
    }
    assert "answers" not in _INPUT
    assert result["questions"] == _INPUT["questions"]


def test_unanswered_questions_stay_absent_and_unrelated_fields_are_ignored() -> None:
    result = answered_question_input(_INPUT, {_COLOR: "", _FEATURES: [], "unrelated": "value"})
    assert result["answers"] == {}
    result = answered_question_input(_INPUT, {_COLOR: "Green"})
    assert result["answers"] == {_COLOR: {"selected": ["Green"]}}


def test_option_with_comma_is_one_selection() -> None:
    tool_input = copy.deepcopy(_INPUT)
    tool_input["questions"][0]["options"][0]["label"] = "Blue, with gold"
    result = answered_question_input(tool_input, {_COLOR: "Blue, with gold"})
    assert result["answers"] == {_COLOR: {"selected": ["Blue, with gold"]}}


@pytest.mark.parametrize("value", [True, 42, None, {"selected": ["Blue"]}, ["Blue", 42]])
def test_invalid_answer_types_are_not_coerced(value: object) -> None:
    with pytest.raises(ValueError, match="must be text"):
        answered_question_input(_INPUT, {_COLOR: value})


def test_single_choice_does_not_accept_multiple_answers() -> None:
    with pytest.raises(ValueError, match="single-choice"):
        answered_question_input(_INPUT, {_COLOR: ["Blue", "Green"]})


def test_web_question_answer_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _http(
        monkeypatch,
        [{"action": "accept", "content": {_COLOR: "Green", _FEATURES: ["Search", "Export"]}}],
    )
    result = _mirror()
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/sessions/conv_test/hooks/native-permission-request"
    body = json.loads(requests[0].content)
    assert body["agent"] == "Devin"
    assert body["policy_name"] == "devin_native_ask_question"
    assert body["operation_type"] == "question"
    assert body["ask_user_question"] == question_payload(_INPUT)
    assert body["elicitation_id"].startswith("elicit_devin_question_")
    assert result == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                **_INPUT,
                "answers": {
                    _COLOR: {"selected": ["Green"]},
                    _FEATURES: {"selected": ["Search", "Export"]},
                },
            },
        }
    }


@pytest.mark.parametrize("action", ["cancel", "decline"])
def test_dismissed_question_denies_without_supplying_an_answer(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    _http(monkeypatch, [{"action": action}])
    result = _mirror()
    assert result is not None
    specific = result["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "updatedInput" not in specific


@pytest.mark.parametrize("content", [None, {_COLOR: True}, {_COLOR: ["Blue", "Green"]}])
def test_invalid_web_answers_deny_without_guessing(
    monkeypatch: pytest.MonkeyPatch, content: object
) -> None:
    _http(monkeypatch, [{"action": "accept", "content": content}])
    assert _mirror()["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200),
        httpx.Response(503),
        httpx.Response(200, text="not JSON"),
        httpx.ConnectError("offline"),
        httpx.ReadTimeout("unanswered"),
        [],
        {"action": "unknown"},
    ],
)
def test_unavailable_web_question_falls_back_to_native_picker(
    monkeypatch: pytest.MonkeyPatch, response: object
) -> None:
    _http(monkeypatch, [response])
    assert _mirror() is None


def test_auth_refresh_reuses_question_id(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _http(
        monkeypatch,
        [httpx.Response(401), {"action": "accept", "content": {_COLOR: "Green"}}],
    )
    refresh = Mock(return_value={"Authorization": "Bearer refreshed"})
    monkeypatch.setattr(hook, "policy_hook_reauth", lambda *_: refresh)
    assert _mirror()["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert len(requests) == 2
    assert (
        json.loads(requests[0].content)["elicitation_id"]
        == json.loads(requests[1].content)["elicitation_id"]
    )
    assert requests[1].headers["Authorization"] == "Bearer refreshed"
    refresh.assert_called_once()


def _run_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict, policy: Mock
) -> None:
    monkeypatch.setenv(hook._SERVER_URL_ENV, "https://example.test")
    monkeypatch.setenv(hook._SESSION_ID_ENV, "conv_test")
    monkeypatch.setattr(hook, "_evaluate_policy", policy)
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hook.main([str(tmp_path)]) == 0


@pytest.mark.parametrize("permission_mode", ["auto", "bypass"])
def test_native_pretool_question_runs_policy_then_collects_web_answers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    permission_mode: str,
) -> None:
    requests = _http(monkeypatch, [{"action": "accept", "content": {_COLOR: "Green"}}])

    def allow_policy(*args: object, **kwargs: object) -> None:
        assert requests == [], "policy must run before the question is published"

    policy = Mock(side_effect=allow_policy)
    payload = {**_payload(), "permission_mode": permission_mode}
    _run_hook(monkeypatch, tmp_path, payload, policy)
    policy.assert_called_once()
    assert len(requests) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["updatedInput"]["answers"] == {
        _COLOR: {"selected": ["Green"]}
    }
    assert (
        json.loads((tmp_path / "hooks.jsonl").read_text())["payload"]["tool_use_id"]
        == payload["tool_use_id"]
    )


def test_policy_denial_prevents_question_even_in_bypass_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    denied = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Blocked by policy",
        }
    }
    requests = _http(monkeypatch, [{"action": "accept", "content": {_COLOR: "Green"}}])
    _run_hook(
        monkeypatch,
        tmp_path,
        {**_payload(), "permission_mode": "bypass"},
        Mock(return_value=denied),
    )
    assert requests == []
    assert json.loads(capsys.readouterr().out) == denied


def test_preanswered_native_call_is_not_reprompted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _payload()
    payload["tool_input"]["answers"] = {_COLOR: {"selected": ["Blue"]}}
    requests = _http(monkeypatch, [{"action": "accept"}])
    _run_hook(monkeypatch, tmp_path, payload, Mock(return_value=None))
    assert requests == []
    assert capsys.readouterr().out == ""
