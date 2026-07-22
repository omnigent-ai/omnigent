"""Tests for E2E policy callable helpers."""

from __future__ import annotations

from omnigent._e2e_policy_callables import block_on_sentinel


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
