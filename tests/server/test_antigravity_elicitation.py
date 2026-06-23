"""Tests for the agy elicitation adapter.

Pure-function tests — no HTTP or runtime needed.
"""

from __future__ import annotations

import pytest

from omnigent.server.routes._antigravity_elicitation import (
    to_elicitation_params,
    to_interaction_payload,
)
from omnigent.server.schemas import ElicitationResult

# ── Shared fixtures ──────────────────────────────────────────────────


_ASK_QUESTION_PENDING: dict[str, object] = {
    "kind": "ask_question",
    "trajectory_id": "traj-abc",
    "step_index": 3,
    "spec": {
        "questions": [
            {
                "question": "What type of project?",
                "is_multi_select": False,
                "options": [
                    {"id": "1", "text": "Web app"},
                    {"id": "2", "text": "CLI tool"},
                    {"id": "3", "text": "Testing"},
                ],
            }
        ]
    },
}

_MULTI_SELECT_PENDING: dict[str, object] = {
    "kind": "ask_question",
    "trajectory_id": "traj-def",
    "step_index": 7,
    "spec": {
        "questions": [
            {
                "question": "Select frameworks",
                "is_multi_select": True,
                "options": [
                    {"id": "1", "text": "React"},
                    {"id": "2", "text": "Vue"},
                    {"id": "3", "text": "Angular"},
                ],
            }
        ]
    },
}

_MULTI_QUESTION_PENDING: dict[str, object] = {
    "kind": "ask_question",
    "trajectory_id": "traj-multi",
    "step_index": 9,
    "spec": {
        "questions": [
            {
                "question": "What type of project?",
                "is_multi_select": False,
                "options": [
                    {"id": "1", "text": "Web app"},
                    {"id": "2", "text": "CLI tool"},
                ],
            },
            {
                "question": "Which language?",
                "is_multi_select": False,
                "options": [
                    {"id": "1", "text": "Python"},
                    {"id": "2", "text": "TypeScript"},
                ],
            },
        ]
    },
}

_PERMISSION_PENDING: dict[str, object] = {
    "kind": "permission",
    "trajectory_id": "traj-xyz",
    "step_index": 6,
    "spec": {
        "resource": {
            "action": "command",
            "target": "pwd",
        },
        "actionDescription": "Running pwd command",
    },
}


# ── to_elicitation_params: ask_question ─────────────────────────────


class TestToElicitationParamsAskQuestion:
    """Tests for ask_question → ElicitationRequestParams."""

    def test_mode_is_form(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        assert params.mode == "form"

    def test_message_set(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        assert params.message

    def test_phase_is_agy_ask_question(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        assert params.phase == "agy_ask_question"

    def test_policy_name_is_agy_native_ask_question(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        assert params.policy_name == "agy_native_ask_question"

    def test_ask_question_spec_present(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        extra = params.model_extra or {}
        assert "ask_question" in extra

    def test_ask_question_spec_carries_questions(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        extra = params.model_extra or {}
        spec = extra["ask_question"]
        assert isinstance(spec, dict)
        questions = spec.get("questions")
        assert isinstance(questions, list)
        assert len(questions) == 1

    def test_ask_question_option_ids_present(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        extra = params.model_extra or {}
        spec = extra["ask_question"]
        assert isinstance(spec, dict)
        questions = spec.get("questions")
        assert isinstance(questions, list)
        first = questions[0]
        assert isinstance(first, dict)
        options = first.get("options")
        assert isinstance(options, list)
        ids = [o["id"] for o in options if isinstance(o, dict)]
        assert ids == ["1", "2", "3"]

    def test_multi_select_flag_preserved(self) -> None:
        params = to_elicitation_params(_MULTI_SELECT_PENDING)
        extra = params.model_extra or {}
        spec = extra["ask_question"]
        assert isinstance(spec, dict)
        questions = spec.get("questions")
        assert isinstance(questions, list)
        first = questions[0]
        assert isinstance(first, dict)
        assert first.get("is_multi_select") is True

    def test_single_select_flag_preserved(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        extra = params.model_extra or {}
        spec = extra["ask_question"]
        assert isinstance(spec, dict)
        questions = spec.get("questions")
        assert isinstance(questions, list)
        first = questions[0]
        assert isinstance(first, dict)
        assert first.get("is_multi_select") is False

    def test_trajectory_id_stored(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        extra = params.model_extra or {}
        assert extra.get("trajectory_id") == "traj-abc"

    def test_step_index_stored(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        extra = params.model_extra or {}
        assert extra.get("step_index") == 3


# ── to_elicitation_params: permission ───────────────────────────────


class TestToElicitationParamsPermission:
    """Tests for permission → ElicitationRequestParams."""

    def test_mode_is_form(self) -> None:
        params = to_elicitation_params(_PERMISSION_PENDING)
        assert params.mode == "form"

    def test_message_contains_command(self) -> None:
        params = to_elicitation_params(_PERMISSION_PENDING)
        assert "pwd" in params.message

    def test_phase_is_agy_permission(self) -> None:
        params = to_elicitation_params(_PERMISSION_PENDING)
        assert params.phase == "agy_permission"

    def test_policy_name_is_agy_native_permission(self) -> None:
        params = to_elicitation_params(_PERMISSION_PENDING)
        assert params.policy_name == "agy_native_permission"

    def test_permission_spec_present(self) -> None:
        params = to_elicitation_params(_PERMISSION_PENDING)
        extra = params.model_extra or {}
        assert "permission_spec" in extra

    def test_trajectory_id_stored(self) -> None:
        params = to_elicitation_params(_PERMISSION_PENDING)
        extra = params.model_extra or {}
        assert extra.get("trajectory_id") == "traj-xyz"


# ── to_interaction_payload: ask_question ────────────────────────────


class TestToInteractionPayloadAskQuestion:
    """Tests for ask_question result → handleUserInteraction payload."""

    def _spec(self) -> dict[str, object]:
        spec = _ASK_QUESTION_PENDING["spec"]
        assert isinstance(spec, dict)
        return spec

    def test_single_option_selected(self) -> None:
        result = ElicitationResult(
            action="accept",
            content={"selectedOptionIds": ["2"]},
        )
        payload = to_interaction_payload("ask_question", result, self._spec())
        assert "askQuestion" in payload
        responses = payload["askQuestion"]["responses"]
        assert len(responses) == 1
        assert responses[0]["question"] == "What type of project?"
        assert responses[0]["selectedOptionIds"] == ["2"]

    def test_option_id_not_text(self) -> None:
        result = ElicitationResult(
            action="accept",
            content={"selectedOptionIds": ["1"]},
        )
        payload = to_interaction_payload("ask_question", result, self._spec())
        selected = payload["askQuestion"]["responses"][0]["selectedOptionIds"]
        assert selected == ["1"]
        assert "Web app" not in selected

    def test_decline_returns_empty(self) -> None:
        result = ElicitationResult(action="decline")
        payload = to_interaction_payload("ask_question", result, self._spec())
        assert "askQuestion" in payload
        responses = payload["askQuestion"]["responses"]
        assert responses == []

    def test_cancel_returns_empty(self) -> None:
        result = ElicitationResult(action="cancel")
        payload = to_interaction_payload("ask_question", result, self._spec())
        assert "askQuestion" in payload
        responses = payload["askQuestion"]["responses"]
        assert responses == []

    def test_multi_select_multiple_ids(self) -> None:
        multi_spec = _MULTI_SELECT_PENDING["spec"]
        assert isinstance(multi_spec, dict)
        result = ElicitationResult(
            action="accept",
            content={"selectedOptionIds": ["1", "3"]},
        )
        payload = to_interaction_payload("ask_question", result, multi_spec)
        responses = payload["askQuestion"]["responses"]
        assert len(responses) == 1
        assert responses[0]["selectedOptionIds"] == ["1", "3"]

    def test_write_in_response_included(self) -> None:
        result = ElicitationResult(
            action="accept",
            content={"selectedOptionIds": [], "writeInResponse": "my custom answer"},
        )
        payload = to_interaction_payload("ask_question", result, self._spec())
        responses = payload["askQuestion"]["responses"]
        assert responses[0].get("writeInResponse") == "my custom answer"

    def test_write_in_absent_when_not_in_content(self) -> None:
        result = ElicitationResult(
            action="accept",
            content={"selectedOptionIds": ["2"]},
        )
        payload = to_interaction_payload("ask_question", result, self._spec())
        responses = payload["askQuestion"]["responses"]
        assert "writeInResponse" not in responses[0]


class TestToInteractionPayloadMultiQuestion:
    """Multi-question guard: a flat verdict must NOT broadcast to all questions."""

    def _spec(self) -> dict[str, object]:
        spec = _MULTI_QUESTION_PENDING["spec"]
        assert isinstance(spec, dict)
        return spec

    def test_multi_question_answers_only_first(self) -> None:
        # The flat ElicitationResult.content carries one answer; it belongs to the
        # first question. Broadcasting it to BOTH questions (the prior behaviour)
        # is semantically wrong, so only the first question is answered.
        result = ElicitationResult(
            action="accept",
            content={"selectedOptionIds": ["2"]},
        )
        payload = to_interaction_payload("ask_question", result, self._spec())
        responses = payload["askQuestion"]["responses"]
        assert len(responses) == 1
        assert responses[0]["question"] == "What type of project?"
        assert responses[0]["selectedOptionIds"] == ["2"]

    def test_multi_question_does_not_broadcast(self) -> None:
        result = ElicitationResult(
            action="accept",
            content={"selectedOptionIds": ["2"]},
        )
        payload = to_interaction_payload("ask_question", result, self._spec())
        responses = payload["askQuestion"]["responses"]
        # The second question must NOT be answered with the first's option ids.
        answered_questions = [r["question"] for r in responses]
        assert "Which language?" not in answered_questions

    def test_multi_question_logs_limitation(self, caplog: pytest.LogCaptureFixture) -> None:
        result = ElicitationResult(
            action="accept",
            content={"selectedOptionIds": ["1"]},
        )
        with caplog.at_level("WARNING"):
            to_interaction_payload("ask_question", result, self._spec())
        assert any(
            "askQuestion carried" in rec.message and "questions" in rec.message
            for rec in caplog.records
        )

    def test_multi_question_decline_returns_empty(self) -> None:
        result = ElicitationResult(action="decline")
        payload = to_interaction_payload("ask_question", result, self._spec())
        assert payload["askQuestion"]["responses"] == []

    def test_single_question_does_not_log(self, caplog: pytest.LogCaptureFixture) -> None:
        # The dominant single-question case must stay silent (no spurious warning).
        single_spec = _ASK_QUESTION_PENDING["spec"]
        assert isinstance(single_spec, dict)
        result = ElicitationResult(
            action="accept",
            content={"selectedOptionIds": ["2"]},
        )
        with caplog.at_level("WARNING"):
            to_interaction_payload("ask_question", result, single_spec)
        assert not any("askQuestion carried" in rec.message for rec in caplog.records)


# ── to_interaction_payload: permission ──────────────────────────────


class TestToInteractionPayloadPermission:
    """Tests for permission result → handleUserInteraction payload."""

    def _spec(self) -> dict[str, object]:
        spec = _PERMISSION_PENDING["spec"]
        assert isinstance(spec, dict)
        return spec

    def test_accept_returns_allow_true(self) -> None:
        result = ElicitationResult(action="accept")
        payload = to_interaction_payload("permission", result, self._spec())
        assert payload == {"permission": {"allow": True}}

    def test_decline_returns_allow_false(self) -> None:
        result = ElicitationResult(action="decline")
        payload = to_interaction_payload("permission", result, self._spec())
        assert payload == {"permission": {"allow": False}}

    def test_cancel_returns_allow_false(self) -> None:
        result = ElicitationResult(action="cancel")
        payload = to_interaction_payload("permission", result, self._spec())
        assert payload == {"permission": {"allow": False}}


# ── unknown kind guard ───────────────────────────────────────────────


class TestUnknownKind:
    """Guard against unknown interaction kinds."""

    def test_to_elicitation_params_unknown_kind_raises(self) -> None:
        bad: dict[str, object] = {
            "kind": "unknown_kind",
            "trajectory_id": "t",
            "step_index": 0,
            "spec": {},
        }
        with pytest.raises(ValueError, match="unknown_kind"):
            to_elicitation_params(bad)

    def test_to_interaction_payload_unknown_kind_raises(self) -> None:
        result = ElicitationResult(action="accept")
        with pytest.raises(ValueError, match="unknown_kind"):
            to_interaction_payload("unknown_kind", result, {})
