"""Tests for E2E policy callable helpers."""

from __future__ import annotations

from omnigent._e2e_policy_callables import block_on_sentinel, taint_on_banana


def test_block_on_sentinel_allow_returns_fresh_decision() -> None:
    """Mutating one ALLOW response must not affect later policy decisions."""
    first = block_on_sentinel({"data": "safe input"})
    first["result"] = "DENY"
    first["reason"] = "mutated by caller"

    second = block_on_sentinel({"data": "another safe input"})

    assert second == {"result": "ALLOW"}
    assert second is not first


def test_block_on_sentinel_denies_reserved_token() -> None:
    """The sentinel branch still returns a DENY decision with a reason."""
    decision = block_on_sentinel({"data": "contains BLOCK_THIS_TOKEN"})

    assert decision["result"] == "DENY"
    assert "BLOCK_THIS_TOKEN" in decision["reason"]


# ── Structured request shape ({"user_content", "attachments"}) ────────────────
# Regression for #2906: the web input gate now passes REQUEST ``data`` as a dict
# so policies can see attachments. These fixtures must read the typed text from
# that dict (via ``request_user_text``), not assume a bare string — otherwise the
# sentinel/taint tokens never match and the e2e policy gates silently ALLOW.


def test_block_on_sentinel_denies_reserved_token_in_request_dict() -> None:
    """The sentinel is caught inside the structured request dict."""
    decision = block_on_sentinel(
        {
            "type": "request",
            "data": {"user_content": "contains BLOCK_THIS_TOKEN", "attachments": []},
        }
    )

    assert decision["result"] == "DENY"
    assert "BLOCK_THIS_TOKEN" in decision["reason"]


def test_block_on_sentinel_allows_clean_request_dict() -> None:
    """A clean structured request dict passes through with ALLOW."""
    decision = block_on_sentinel(
        {"type": "request", "data": {"user_content": "safe input", "attachments": []}}
    )

    assert decision == {"result": "ALLOW"}


def test_taint_on_banana_labels_from_request_dict() -> None:
    """The banana trigger inside the request dict still emits the taint label."""
    result = taint_on_banana(
        {"type": "request", "data": {"user_content": "BANANA_TRIGGER please", "attachments": []}}
    )

    assert result.set_labels == {"tainted": "1"}


def test_taint_on_banana_clean_request_dict_no_label() -> None:
    """A clean request dict emits no taint label."""
    result = taint_on_banana(
        {"type": "request", "data": {"user_content": "nothing to see", "attachments": []}}
    )

    assert result.set_labels is None
