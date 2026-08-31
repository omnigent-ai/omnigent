"""Tests for the cel_policy builtin factory."""

from __future__ import annotations

import pytest

pytest.importorskip("celpy", reason="cel-python not installed")

from omnigent.policies.builtins.cel import cel_policy
from omnigent.policies.function import _coerce_to_policy_result
from omnigent.policies.types import ApprovalPresentation
from omnigent.runtime.policies.approval import validate_approval_presentation

# ── Map return: DENY ────────────────────────────────────────────


def test_deny_matching_tool_call() -> None:
    """Expression returning DENY map on tool_call match."""
    evaluate = cel_policy(
        expression=(
            'event.type == "tool_call" && event.data.name == "sys_os_shell"'
            ' ? {"result": "DENY", "reason": "Shell blocked."}'
            ' : {"result": "ALLOW"}'
        ),
    )
    result = evaluate(
        {
            "type": "tool_call",
            "data": {"name": "sys_os_shell", "arguments": {}},
        }
    )
    assert result == {"result": "DENY", "reason": "Shell blocked."}


def test_allow_non_matching_tool_call() -> None:
    """Non-matching tool call returns ALLOW."""
    evaluate = cel_policy(
        expression=(
            'event.type == "tool_call" && event.data.name == "sys_os_shell"'
            ' ? {"result": "DENY", "reason": "Shell blocked."}'
            ' : {"result": "ALLOW"}'
        ),
    )
    result = evaluate(
        {
            "type": "tool_call",
            "data": {"name": "web_search", "arguments": {}},
        }
    )
    assert result == {"result": "ALLOW"}


def test_deny_with_fallback_reason() -> None:
    """Map without reason key uses the factory default."""
    evaluate = cel_policy(
        expression='{"result": "DENY"}',
        reason="Factory default.",
    )
    result = evaluate({"type": "request"})
    assert result == {"result": "DENY", "reason": "Factory default."}


def test_deny_with_custom_reason() -> None:
    """Map with reason key overrides the factory default."""
    evaluate = cel_policy(
        expression='{"result": "DENY", "reason": "Custom."}',
        reason="Factory default.",
    )
    result = evaluate({"type": "request"})
    assert result == {"result": "DENY", "reason": "Custom."}


# ── Map return: ASK ─────────────────────────────────────────────


def test_ask_verdict() -> None:
    """Expression returning ASK parks for user approval."""
    evaluate = cel_policy(
        expression=(
            'event.type == "tool_call"'
            ' ? {"result": "ASK", "reason": "Approve this?"}'
            ' : {"result": "ALLOW"}'
        ),
    )
    result = evaluate({"type": "tool_call", "data": {"name": "x"}})
    assert result == {"result": "ASK", "reason": "Approve this?"}


def test_ask_with_fallback_reason() -> None:
    """ASK without reason in map uses factory default."""
    evaluate = cel_policy(
        expression='{"result": "ASK"}',
        reason="Please approve.",
    )
    result = evaluate({"type": "request"})
    assert result == {"result": "ASK", "reason": "Please approve."}


def test_broker_merge_ask_extracts_approval_presentation() -> None:
    """The real broker field shape produces a typed approval target."""
    evaluate = cel_policy(
        expression="""
        {
          "result": "ASK",
          "reason": "Merge this pull request?",
          "approval": {
            "title": event.data.arguments.repository_owner + "/" +
                     event.data.arguments.repository_name + " #" +
                     string(event.data.arguments.pr_number),
            "href": "https://github.com/" +
                    event.data.arguments.repository_owner + "/" +
                    event.data.arguments.repository_name + "/pull/" +
                    string(event.data.arguments.pr_number),
            "secondary_arguments": ["grant_id", "idempotency_key"]
          }
        }
        """,
    )
    result = evaluate(
        {
            "type": "tool_call",
            "data": {
                "name": "github_merge_pr",
                "arguments": {
                    "repository_owner": "acme",
                    "repository_name": "widgets",
                    "pr_number": 123,
                    "grant_id": "grant_demo",
                    "idempotency_key": "idem_demo",
                },
            },
        }
    )
    assert result == {
        "result": "ASK",
        "reason": "Merge this pull request?",
        "approval": ApprovalPresentation(
            title="acme/widgets #123",
            href="https://github.com/acme/widgets/pull/123",
            secondary_arguments=("grant_id", "idempotency_key"),
        ),
    }
    policy_result = _coerce_to_policy_result(result, spec_name="github_approval_gate")
    validated = validate_approval_presentation(
        policy_result.approval,
        {
            "repository_owner": "acme",
            "repository_name": "widgets",
            "pr_number": 123,
            "grant_id": "grant_demo",
            "idempotency_key": "idem_demo",
        },
    )
    assert validated == result["approval"]


def test_broker_open_pr_ask_uses_branch_target_and_repository_link() -> None:
    """Open-PR approval describes the target before a PR number exists."""
    evaluate = cel_policy(
        expression="""
        {
          "result": "ASK",
          "reason": "Open this pull request?",
          "approval": {
            "title": event.data.arguments.repository_owner + "/" +
                     event.data.arguments.repository_name + ": " +
                     event.data.arguments.head + " -> " +
                     event.data.arguments.base,
            "href": "https://github.com/" +
                    event.data.arguments.repository_owner + "/" +
                    event.data.arguments.repository_name,
            "secondary_arguments": ["grant_id", "idempotency_key"]
          }
        }
        """,
    )
    result = evaluate(
        {
            "type": "tool_call",
            "data": {
                "name": "github_open_pr",
                "arguments": {
                    "repository_owner": "acme",
                    "repository_name": "widgets",
                    "head": "feat/rate-limiter",
                    "base": "main",
                    "grant_id": "grant_demo",
                    "idempotency_key": "idem_demo",
                },
            },
        }
    )
    assert result is not None
    assert result["approval"] == ApprovalPresentation(
        title="acme/widgets: feat/rate-limiter -> main",
        href="https://github.com/acme/widgets",
        secondary_arguments=("grant_id", "idempotency_key"),
    )


@pytest.mark.parametrize(
    "approval",
    [
        '{"href": "https://example.com"}',
        '{"title": 42}',
        '{"title": "target", "href": 42}',
        '{"title": "target", "secondary_arguments": [42]}',
    ],
)
def test_malformed_cel_approval_is_dropped(approval: str) -> None:
    """Malformed display metadata never changes the ASK verdict."""
    evaluate = cel_policy(
        expression=f'{{"result": "ASK", "reason": "Review", "approval": {approval}}}',
    )
    assert evaluate({"type": "request"}) == {"result": "ASK", "reason": "Review"}


@pytest.mark.parametrize(
    "title",
    [
        "line\nbreak",
        "escape\x1btitle",
        "delete\x7ftitle",
        "line separator\u2028title",
        "paragraph separator\u2029title",
        "arabic mark\u061ctitle",
        "override\u202etitle",
        "isolate\u2066title",
        "mark\u200ftitle",
        "\x1b\u202e",
    ],
)
def test_cel_presentation_drops_unsafe_title_characters(title: str) -> None:
    """CEL policy titles use the same boundary validation as Python policies."""
    evaluate = cel_policy(
        expression=(
            '{"result": "ASK", "reason": "Review", '
            '"approval": {"title": event.data.arguments.title}}'
        ),
    )
    result = evaluate({"type": "tool_call", "data": {"arguments": {"title": title}}})
    assert result is not None
    assert validate_approval_presentation(result["approval"], {}) is None


@pytest.mark.parametrize(
    "title",
    [
        "example/project #451",
        "בקשת אישור 451",
        "طلب موافقة 451",
    ],
)
def test_cel_presentation_preserves_clean_titles(title: str) -> None:
    """Clean LTR and RTL CEL titles pass without rewriting."""
    evaluate = cel_policy(
        expression=(
            '{"result": "ASK", "reason": "Review", '
            '"approval": {"title": event.data.arguments.title}}'
        ),
    )
    result = evaluate({"type": "tool_call", "data": {"arguments": {"title": title}}})
    assert result is not None
    assert validate_approval_presentation(result["approval"], {}) == ApprovalPresentation(
        title=title
    )


# ── Map return: ALLOW ───────────────────────────────────────────


def test_allow_explicit() -> None:
    """Explicit ALLOW map passes through without reason."""
    evaluate = cel_policy(expression='{"result": "ALLOW"}')
    result = evaluate({"type": "request"})
    assert result == {"result": "ALLOW"}


# ── Abstain (non-map returns) ───────────────────────────────────


def test_non_map_return_abstains() -> None:
    """Non-map return (e.g. bool, string) abstains."""
    evaluate = cel_policy(expression="true")
    assert evaluate({"type": "request"}) is None


def test_map_without_result_key_abstains() -> None:
    """Map missing the result key abstains."""
    evaluate = cel_policy(expression='{"reason": "no verdict"}')
    assert evaluate({"type": "request"}) is None


# ── CEL features ────────────────────────────────────────────────


def test_string_contains() -> None:
    """CEL string methods work."""
    evaluate = cel_policy(
        expression=(
            'event.type == "request" && event.data.contains("SECRET")'
            ' ? {"result": "DENY", "reason": "Secret detected."}'
            ' : {"result": "ALLOW"}'
        ),
    )
    assert evaluate({"type": "request", "data": "my SECRET key"}) == {
        "result": "DENY",
        "reason": "Secret detected.",
    }
    assert evaluate({"type": "request", "data": "normal"}) == {"result": "ALLOW"}


def test_request_dict_data_projected_to_user_text() -> None:
    """A request-phase ``data`` dict is projected to ``user_content`` for CEL.

    Regression for #2906: the web input gate now passes REQUEST ``data`` as
    ``{"user_content", "attachments"}``. String CEL expressions authored for the
    request phase (e.g. ``event.data.contains(...)``) must keep matching — a raw
    map would fail-open (``.contains`` raises → abstain → ALLOW), silently
    disabling a UI-configured DENY policy.
    """
    evaluate = cel_policy(
        expression=(
            'event.type == "request" && event.data.contains("SECRET")'
            ' ? {"result": "DENY", "reason": "Secret detected."}'
            ' : {"result": "ALLOW"}'
        ),
    )
    # Structured dict shape with the secret in user_content → still DENY.
    assert evaluate(
        {"type": "request", "data": {"user_content": "my SECRET key", "attachments": []}}
    ) == {"result": "DENY", "reason": "Secret detected."}
    # Clean structured dict → ALLOW (not a crash / abstain).
    assert evaluate(
        {"type": "request", "data": {"user_content": "normal", "attachments": []}}
    ) == {"result": "ALLOW"}


def test_in_list() -> None:
    """CEL ``in`` operator works."""
    evaluate = cel_policy(
        expression=(
            'event.type == "tool_call" && event.data.name in ["rm", "drop"]'
            ' ? {"result": "DENY", "reason": "Blocked."}'
            ' : {"result": "ALLOW"}'
        ),
    )
    assert evaluate({"type": "tool_call", "data": {"name": "drop"}}) == {
        "result": "DENY",
        "reason": "Blocked.",
    }
    assert evaluate({"type": "tool_call", "data": {"name": "read"}}) == {
        "result": "ALLOW",
    }


# ── Error handling ──────────────────────────────────────────────


def test_eval_error_returns_none() -> None:
    """CEL eval errors abstain (fail-open)."""
    evaluate = cel_policy(
        expression='event.nonexistent == "x" ? {"result": "DENY"} : {"result": "ALLOW"}'
    )
    assert evaluate({"type": "request", "data": "hello"}) is None


def test_invalid_syntax_raises() -> None:
    """Invalid CEL syntax is rejected at compile time."""
    with pytest.raises(ValueError, match="CEL"):
        cel_policy(expression="event.type ==== bad")


def test_llm_client_stripped_from_cel_event() -> None:
    """llm_client is dropped before json_to_cel; the expression still evaluates."""

    class _FakeLLMClient:
        pass

    evaluate = cel_policy(expression='{"result": "DENY"}')
    # The engine injects llm_client (a live object) into every real event.
    # CEL expressions cannot use it and json_to_cel cannot convert it, so it
    # is stripped before marshalling. The expression must evaluate normally.
    result = evaluate({"type": "request", "llm_client": _FakeLLMClient()})  # type: ignore[typeddict-unknown-key]
    assert result == {"result": "DENY", "reason": "Denied by policy."}
