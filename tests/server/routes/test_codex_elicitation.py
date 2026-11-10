"""Tests for the Codex elicitation protocol adapters.

These are pure-function tests — no HTTP or runtime needed.
"""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.server.routes._codex_elicitation import (
    _codex_command_approval_params,
    _codex_command_approval_response,
    _codex_command_preview,
    _codex_mcp_elicitation_response,
    _codex_mcp_persist_modes,
    _json_preview,
    _string_list_answer,
    parse_codex_elicitation_request,
)
from omnigent.server.schemas import ElicitationResult

# ── parse_codex_elicitation_request ──────────────────────────────────


class TestParseCodexElicitationRequest:
    """Tests for the top-level request parser."""

    def test_missing_method_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty method"):
            parse_codex_elicitation_request({"id": 1, "params": {}})

    def test_empty_method_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty method"):
            parse_codex_elicitation_request({"id": 1, "method": "", "params": {}})

    def test_non_dict_params_raises(self) -> None:
        with pytest.raises(OmnigentError, match="params must be an object"):
            parse_codex_elicitation_request(
                {"id": 1, "method": "mcpServer/elicitation/request", "params": "bad"}
            )

    def test_missing_id_raises(self) -> None:
        with pytest.raises(OmnigentError, match="string or integer id"):
            parse_codex_elicitation_request(
                {"method": "mcpServer/elicitation/request", "params": {}}
            )

    def test_unsupported_method_raises(self) -> None:
        with pytest.raises(OmnigentError, match="Unsupported"):
            parse_codex_elicitation_request({"id": 1, "method": "unknown/method", "params": {}})

    def test_valid_mcp_form_request(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 1,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "mode": "form",
                    "message": "Need input",
                    "requestedSchema": {"type": "object"},
                },
            }
        )
        assert req.method == "mcpServer/elicitation/request"
        assert req.params.mode == "form"

    def test_valid_command_approval(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 2,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "npm test"},
            }
        )
        assert req.method == "item/commandExecution/requestApproval"


# ── Codex MCP approval persistence ───────────────────────────────────


class TestCodexMcpApprovalPersistence:
    """Tests for Codex's session and durable MCP approval choices."""

    @pytest.mark.parametrize("mode", ["session", "always"])
    def test_returns_advertised_persistence_mode(self, mode: str) -> None:
        result = ElicitationResult.model_validate({"action": "accept", "_meta": {"persist": mode}})

        response = _codex_mcp_elicitation_response(
            result,
            "mcpServer/elicitation/request",
            {
                "_meta": {
                    "codex_approval_kind": "mcp_tool_call",
                    "persist": ["session", "always"],
                }
            },
        )

        assert response == {
            "action": "accept",
            "content": None,
            "_meta": {"persist": mode},
        }

    def test_rejects_unadvertised_persistence_mode(self) -> None:
        result = ElicitationResult.model_validate(
            {"action": "accept", "_meta": {"persist": "always"}}
        )

        with pytest.raises(OmnigentError, match="was not advertised"):
            _codex_mcp_elicitation_response(
                result,
                "mcpServer/elicitation/request",
                {
                    "_meta": {
                        "codex_approval_kind": "mcp_tool_call",
                        "persist": ["session"],
                    }
                },
            )

    def test_ignores_malformed_advertised_modes(self) -> None:
        params = {
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "persist": ["session", {"unexpected": "object"}, None],
            }
        }

        assert _codex_mcp_persist_modes(params) == {"session"}


# ── _string_list_answer ──────────────────────────────────────────────


class TestStringListAnswer:
    """Tests for answer normalization."""

    def test_string_input(self) -> None:
        assert _string_list_answer("React") == ["React"]

    def test_empty_string(self) -> None:
        assert _string_list_answer("") == []

    def test_list_input(self) -> None:
        assert _string_list_answer(["a", "b"]) == ["a", "b"]

    def test_list_with_non_strings(self) -> None:
        assert _string_list_answer(["a", 123, "b"]) == ["a", "b"]

    def test_none_input(self) -> None:
        assert _string_list_answer(None) == []

    def test_numeric_input(self) -> None:
        assert _string_list_answer(42) == ["42"]


# ── _codex_command_preview ───────────────────────────────────────────


class TestCodexCommandPreview:
    """Tests for command preview extraction."""

    def test_string_command(self) -> None:
        assert _codex_command_preview({"command": "npm test"}) == "npm test"

    def test_list_command(self) -> None:
        assert _codex_command_preview({"command": ["npm", "test"]}) == "npm test"

    def test_empty_command(self) -> None:
        assert _codex_command_preview({"command": ""}) is None

    def test_missing_command(self) -> None:
        assert _codex_command_preview({}) is None


# ── _json_preview ────────────────────────────────────────────────────


class TestJsonPreview:
    """Tests for the bounded preview function."""

    def test_simple_object(self) -> None:
        result = _json_preview({"key": "value"})
        assert '"key"' in result

    def test_truncated(self) -> None:
        big = {"k": "x" * 2000}
        result = _json_preview(big)
        assert len(result) <= 1024


# ── Codex command approval persistence ──────────────────────────────

_COMMAND_METHOD = "item/commandExecution/requestApproval"
_OFFERED_RULE = [".venv/bin/python", "-m", "pytest"]


def _command_params(command: str, rule: list[str]) -> dict[str, object]:
    return {
        "command": command,
        "availableDecisions": [{"acceptWithExecpolicyAmendment": {"execpolicy_amendment": rule}}],
    }


def _fingerprint_of(params: dict[str, object]) -> str:
    value = _codex_command_approval_params(1, _COMMAND_METHOD, params).model_dump()[
        "approval_request_fingerprint"
    ]
    assert isinstance(value, str)
    return value


class TestCodexCommandApprovalPersistence:
    """The web choice selects an offered rule; the server derives it."""

    def test_offered_rule_is_exposed_to_approval_card(self) -> None:
        card = _codex_command_approval_params(
            1, _COMMAND_METHOD, _command_params("pytest -q", _OFFERED_RULE)
        ).model_dump()

        assert card["execpolicy_amendment"] == _OFFERED_RULE
        assert isinstance(card["approval_request_fingerprint"], str)

    def test_plain_accept_remains_one_time(self) -> None:
        params = _command_params("pytest -q", _OFFERED_RULE)

        assert _codex_command_approval_response(
            ElicitationResult(action="accept"), _COMMAND_METHOD, params
        ) == {"decision": "accept"}

    def test_remember_uses_server_side_offered_rule(self) -> None:
        params = _command_params("pytest -q", _OFFERED_RULE)
        result = ElicitationResult(
            action="accept",
            content={"remember": True, "request_fingerprint": _fingerprint_of(params)},
        )

        assert _codex_command_approval_response(result, _COMMAND_METHOD, params) == {
            "decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": _OFFERED_RULE}}
        }

    def test_remember_requires_an_offered_rule(self) -> None:
        params: dict[str, object] = {"command": "pytest -q"}
        result = ElicitationResult(
            action="accept",
            content={"remember": True, "request_fingerprint": _fingerprint_of(params)},
        )

        with pytest.raises(OmnigentError, match="did not offer a persistent rule"):
            _codex_command_approval_response(result, _COMMAND_METHOD, params)

    def test_remember_requires_displayed_fingerprint(self) -> None:
        params = _command_params("pytest -q", _OFFERED_RULE)
        result = ElicitationResult(action="accept", content={"remember": True})

        with pytest.raises(OmnigentError, match="changed"):
            _codex_command_approval_response(result, _COMMAND_METHOD, params)

    def test_reused_fingerprint_for_a_different_request_is_rejected(self) -> None:
        displayed = _fingerprint_of(_command_params("pytest -q", _OFFERED_RULE))
        later = _command_params("sh -c other", ["sh", "-c"])
        result = ElicitationResult(
            action="accept", content={"remember": True, "request_fingerprint": displayed}
        )

        with pytest.raises(OmnigentError, match="changed"):
            _codex_command_approval_response(result, _COMMAND_METHOD, later)

    @pytest.mark.parametrize("action", ["decline", "cancel"])
    def test_remember_requires_approval(self, action: str) -> None:
        params = _command_params("pytest -q", _OFFERED_RULE)
        result = ElicitationResult(
            action=action,  # type: ignore[arg-type]
            content={"remember": True, "request_fingerprint": _fingerprint_of(params)},
        )

        with pytest.raises(OmnigentError, match="Only an approval"):
            _codex_command_approval_response(result, _COMMAND_METHOD, params)

    @pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
    def test_client_supplied_rule_is_rejected(self, action: str) -> None:
        result = ElicitationResult(
            action=action,  # type: ignore[arg-type]
            content={"execpolicy_amendment": _OFFERED_RULE},
        )

        with pytest.raises(OmnigentError, match="cannot supply its own execpolicy amendment"):
            _codex_command_approval_response(
                result, _COMMAND_METHOD, _command_params("pytest -q", _OFFERED_RULE)
            )

    def test_legacy_command_approval_cannot_remember(self) -> None:
        params = _command_params("pytest -q", _OFFERED_RULE)
        result = ElicitationResult(
            action="accept",
            content={"remember": True, "request_fingerprint": _fingerprint_of(params)},
        )

        with pytest.raises(OmnigentError, match="do not offer persistent rules"):
            _codex_command_approval_response(result, "execCommandApproval", params)
