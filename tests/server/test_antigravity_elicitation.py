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


class TestAskUserQuestionExtra:
    """The agy question is also stamped under the web UI's ``ask_user_question``
    key (Claude AskUserQuestion shape) so the existing interactive form renders
    it and returns a real selection (not a generic approve/reject card)."""

    def test_ask_user_question_extra_present(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        extra = params.model_extra or {}
        assert "ask_user_question" in extra

    def test_ask_user_question_questions_shape(self) -> None:
        params = to_elicitation_params(_ASK_QUESTION_PENDING)
        extra = params.model_extra or {}
        payload = extra["ask_user_question"]
        assert isinstance(payload, dict)
        questions = payload.get("questions")
        assert isinstance(questions, list) and len(questions) == 1
        q = questions[0]
        # Synthetic string id == index so the form keys its answer by it.
        assert q["id"] == "0"
        assert q["question"] == "What type of project?"
        assert q["multiSelect"] is False
        # agy option text becomes the Claude option label (no id leaked to UI).
        labels = [o["label"] for o in q["options"]]
        assert labels == ["Web app", "CLI tool", "Testing"]
        assert all("id" not in o for o in q["options"])

    def test_ask_user_question_multiselect_flag(self) -> None:
        params = to_elicitation_params(_MULTI_SELECT_PENDING)
        extra = params.model_extra or {}
        q = extra["ask_user_question"]["questions"][0]
        assert q["multiSelect"] is True

    def test_ask_user_question_multi_question_indices(self) -> None:
        params = to_elicitation_params(_MULTI_QUESTION_PENDING)
        extra = params.model_extra or {}
        questions = extra["ask_user_question"]["questions"]
        assert [q["id"] for q in questions] == ["0", "1"]


# ── to_elicitation_params: permission ───────────────────────────────


class TestToElicitationParamsPermission:
    """Tests for permission → ElicitationRequestParams."""

    def test_mode_is_form(self) -> None:
        params = to_elicitation_params(_PERMISSION_PENDING)
        assert params.mode == "form"

    def test_message_contains_command(self) -> None:
        params = to_elicitation_params(_PERMISSION_PENDING)
        assert "pwd" in params.message
        # The web ApprovalCard renders this message in a plain (non-markdown)
        # span, so it must not carry literal markdown asterisks.
        assert "**" not in params.message

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
    """ask_question result → handleUserInteraction payload.

    The web form posts a flat ``content`` keyed by each question's synthetic id
    (its index), valued by the selected option *label(s)* / custom text. The
    mapper translates labels back to agy option *ids* by matching option text.
    """

    def _spec(self) -> dict[str, object]:
        spec = _ASK_QUESTION_PENDING["spec"]
        assert isinstance(spec, dict)
        return spec

    def test_single_option_selected(self) -> None:
        # Form posts the chosen LABEL keyed by question id "0".
        result = ElicitationResult(action="accept", content={"0": "CLI tool"})
        payload = to_interaction_payload("ask_question", result, self._spec())
        responses = payload["askQuestion"]["responses"]
        assert len(responses) == 1
        assert responses[0]["question"] == "What type of project?"
        # Label "CLI tool" maps back to its agy option id "2".
        assert responses[0]["selectedOptionIds"] == ["2"]

    def test_selected_id_is_agy_id_not_label(self) -> None:
        result = ElicitationResult(action="accept", content={"0": "Web app"})
        payload = to_interaction_payload("ask_question", result, self._spec())
        selected = payload["askQuestion"]["responses"][0]["selectedOptionIds"]
        assert selected == ["1"]
        assert "Web app" not in selected

    def test_answer_keyed_by_question_text_fallback(self) -> None:
        # Robustness: a content keyed by verbatim question text still maps.
        result = ElicitationResult(action="accept", content={"What type of project?": "Testing"})
        payload = to_interaction_payload("ask_question", result, self._spec())
        assert payload["askQuestion"]["responses"][0]["selectedOptionIds"] == ["3"]

    def test_decline_returns_empty(self) -> None:
        result = ElicitationResult(action="decline")
        payload = to_interaction_payload("ask_question", result, self._spec())
        assert payload["askQuestion"]["responses"] == []

    def test_cancel_returns_empty(self) -> None:
        result = ElicitationResult(action="cancel")
        payload = to_interaction_payload("ask_question", result, self._spec())
        assert payload["askQuestion"]["responses"] == []

    def test_multi_select_multiple_ids(self) -> None:
        multi_spec = _MULTI_SELECT_PENDING["spec"]
        assert isinstance(multi_spec, dict)
        # Multi-select posts a list of labels.
        result = ElicitationResult(action="accept", content={"0": ["React", "Angular"]})
        payload = to_interaction_payload("ask_question", result, multi_spec)
        responses = payload["askQuestion"]["responses"]
        assert len(responses) == 1
        assert responses[0]["selectedOptionIds"] == ["1", "3"]

    def test_write_in_response_included(self) -> None:
        # A label with no matching option is the user's free-form answer.
        result = ElicitationResult(action="accept", content={"0": "my custom answer"})
        payload = to_interaction_payload("ask_question", result, self._spec())
        response = payload["askQuestion"]["responses"][0]
        assert response["selectedOptionIds"] == []
        assert response["writeInResponse"] == "my custom answer"

    def test_write_in_absent_when_only_predefined(self) -> None:
        result = ElicitationResult(action="accept", content={"0": "CLI tool"})
        payload = to_interaction_payload("ask_question", result, self._spec())
        assert "writeInResponse" not in payload["askQuestion"]["responses"][0]


class TestToInteractionPayloadMultiQuestion:
    """Multi-question prompts round-trip EVERY question (the form answers all)."""

    def _spec(self) -> dict[str, object]:
        spec = _MULTI_QUESTION_PENDING["spec"]
        assert isinstance(spec, dict)
        return spec

    def test_each_question_answered_with_its_own_option(self) -> None:
        result = ElicitationResult(action="accept", content={"0": "CLI tool", "1": "Python"})
        payload = to_interaction_payload("ask_question", result, self._spec())
        responses = payload["askQuestion"]["responses"]
        assert len(responses) == 2
        assert responses[0]["question"] == "What type of project?"
        assert responses[0]["selectedOptionIds"] == ["2"]
        assert responses[1]["question"] == "Which language?"
        assert responses[1]["selectedOptionIds"] == ["1"]

    def test_no_cross_question_broadcast(self) -> None:
        # Each question's ids come from ITS own options (both questions reuse the
        # ids "1"/"2", so a broadcast bug would be invisible without per-question
        # mapping). "Web app" is q0 id 1; "TypeScript" is q1 id 2.
        result = ElicitationResult(action="accept", content={"0": "Web app", "1": "TypeScript"})
        payload = to_interaction_payload("ask_question", result, self._spec())
        responses = payload["askQuestion"]["responses"]
        assert responses[0]["selectedOptionIds"] == ["1"]  # Web app
        assert responses[1]["selectedOptionIds"] == ["2"]  # TypeScript

    def test_decline_returns_empty(self) -> None:
        result = ElicitationResult(action="decline")
        payload = to_interaction_payload("ask_question", result, self._spec())
        assert payload["askQuestion"]["responses"] == []


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
