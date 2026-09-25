"""Tests for the built-in token-budget policy.

What breaks if these tests fail:
- Token budget enforcement (soft warnings, hard blocks)
- Soft checkpoint approval tracking
- Handling of missing/unavailable usage data
- Policy factory validation
- REQUEST and TOOL_CALL phase firing
"""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.token_budget import (
    _session_tokens,
    _usage_has_tokens,
    token_budget,
)


class TestSessionTokens:
    """Test token extraction from policy events."""

    def test_read_total_tokens_when_present(self) -> None:
        """Uses explicit total_tokens when available."""
        event = {"context": {"usage": {"total_tokens": 1500}}}
        assert _session_tokens(event) == 1500

    def test_read_sum_of_input_and_output(self) -> None:
        """Sums input_tokens + output_tokens when total_tokens absent."""
        event = {"context": {"usage": {"input_tokens": 1000, "output_tokens": 500}}}
        assert _session_tokens(event) == 1500

    def test_includes_cached_reads(self) -> None:
        """Includes cache_read_input_tokens in the sum."""
        event = {
            "context": {
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_read_input_tokens": 200,
                }
            }
        }
        assert _session_tokens(event) == 1700

    def test_includes_cache_creation(self) -> None:
        """Includes cache_creation_input_tokens in the sum."""
        event = {
            "context": {
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_creation_input_tokens": 100,
                }
            }
        }
        assert _session_tokens(event) == 1600

    def test_prefers_total_tokens_over_sum(self) -> None:
        """total_tokens takes precedence even when components present."""
        event = {
            "context": {
                "usage": {
                    "total_tokens": 2000,
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_read_input_tokens": 100,
                }
            }
        }
        assert _session_tokens(event) == 2000

    def test_handles_missing_usage(self) -> None:
        """Returns 0 when no usage data present."""
        event = {"context": {}}
        assert _session_tokens(event) == 0

    def test_handles_malformed_usage(self) -> None:
        """Defaults to 0 on type errors."""
        event = {"context": {"usage": {"total_tokens": "not_a_number"}}}
        assert _session_tokens(event) == 0

    def test_negative_tokens_clamped_to_zero(self) -> None:
        """Negative token counts are clamped to 0."""
        event = {"context": {"usage": {"total_tokens": -100}}}
        assert _session_tokens(event) == 0


class TestUsageHasTokens:
    """Test usage availability detection."""

    def test_returns_true_when_total_tokens_present(self) -> None:
        """Detects total_tokens."""
        usage = {"total_tokens": 500}
        assert _usage_has_tokens(usage) is True

    def test_returns_true_when_input_tokens_present(self) -> None:
        """Detects input_tokens."""
        usage = {"input_tokens": 100}
        assert _usage_has_tokens(usage) is True

    def test_returns_true_when_output_tokens_present(self) -> None:
        """Detects output_tokens."""
        usage = {"output_tokens": 200}
        assert _usage_has_tokens(usage) is True

    def test_returns_true_when_cache_read_present(self) -> None:
        """Detects cache_read_input_tokens."""
        usage = {"cache_read_input_tokens": 50}
        assert _usage_has_tokens(usage) is True

    def test_returns_true_when_cache_creation_present(self) -> None:
        """Detects cache_creation_input_tokens."""
        usage = {"cache_creation_input_tokens": 75}
        assert _usage_has_tokens(usage) is True

    def test_returns_false_when_no_usage(self) -> None:
        """Returns False when usage dict absent."""
        usage = {}
        assert _usage_has_tokens(usage) is False

    def test_returns_false_when_all_tokens_zero(self) -> None:
        """Returns False when all token counts are 0."""
        usage = {
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        assert _usage_has_tokens(usage) is False


class TestTokenBudgetFactory:
    """Test the token_budget factory function."""

    def test_factory_returns_callable(self) -> None:
        """Factory returns a callable."""
        evaluator = token_budget(max_tokens=100000)
        assert callable(evaluator)

    def test_factory_validates_on_construction(self) -> None:
        """Factory validates config at construction time."""
        with pytest.raises(ValueError, match="requires max_tokens and/or ask_thresholds_tokens"):
            token_budget()

    def test_max_tokens_must_be_positive(self) -> None:
        """Raises when max_tokens <= 0."""
        with pytest.raises(ValueError, match="max_tokens must be > 0"):
            token_budget(max_tokens=0)

    def test_ask_thresholds_must_be_positive(self) -> None:
        """Raises when thresholds contain non-positive values."""
        with pytest.raises(ValueError):
            token_budget(ask_thresholds_tokens=[0, 100000], max_tokens=200000)

    def test_thresholds_must_be_below_hard_limit(self) -> None:
        """Raises when threshold >= hard limit."""
        with pytest.raises(ValueError, match="must be in"):
            token_budget(ask_thresholds_tokens=[100000, 200000], max_tokens=100000)


class TestTokenBudgetPolicy:
    """Test token budget policy evaluation."""

    def test_allows_on_non_gated_phases(self) -> None:
        """Abstains (ALLOW) on non-gated phases."""
        evaluator = token_budget(max_tokens=100000)
        event = {"type": "response", "context": {"usage": {"total_tokens": 50000}}}
        result = evaluator(event)
        assert result["result"] == "ALLOW"

    def test_asks_when_usage_unavailable_on_request(self) -> None:
        """Fails closed (ASK) when no usage data on request phase."""
        evaluator = token_budget(max_tokens=100000)
        event = {"type": "request", "context": {"usage": {}}}
        result = evaluator(event)
        assert result["result"] == "ASK"
        assert "usage" in result["reason"].lower()

    def test_asks_when_usage_unavailable_on_tool_call(self) -> None:
        """Fails closed (ASK) when no usage data on tool_call phase."""
        evaluator = token_budget(max_tokens=100000)
        event = {"type": "tool_call", "context": {"usage": {}}}
        result = evaluator(event)
        assert result["result"] == "ASK"

    def test_allows_below_soft_threshold(self) -> None:
        """ALLOW when below soft threshold."""
        evaluator = token_budget(max_tokens=100000, ask_thresholds_tokens=[50000, 75000])
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 25000}},
            "session_state": {},
        }
        result = evaluator(event)
        assert result["result"] == "ALLOW"

    def test_asks_at_soft_threshold_request_phase(self) -> None:
        """ASK when crossing soft threshold on request phase."""
        evaluator = token_budget(ask_thresholds_tokens=[50000])
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 50000}},
            "session_state": {},
        }
        result = evaluator(event)
        assert result["result"] == "ASK"
        assert "crossed" in result["reason"].lower() or "passed" in result["reason"].lower()

    def test_asks_at_soft_threshold_tool_call_phase(self) -> None:
        """ASK when crossing soft threshold on tool_call phase."""
        evaluator = token_budget(ask_thresholds_tokens=[50000])
        event = {
            "type": "tool_call",
            "context": {"usage": {"total_tokens": 50000}},
            "session_state": {},
        }
        result = evaluator(event)
        assert result["result"] == "ASK"

    def test_soft_threshold_not_re_asked_after_approval(self) -> None:
        """Soft checkpoint not re-asked after approval."""
        evaluator = token_budget(ask_thresholds_tokens=[50000])
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 50000}},
            "session_state": {"token_budget_ask_approved": 50000},
        }
        result = evaluator(event)
        assert result["result"] == "ALLOW"

    def test_denies_at_hard_limit_request_phase(self) -> None:
        """DENY when reaching hard limit on request phase."""
        evaluator = token_budget(max_tokens=100000)
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 100000}},
            "session_state": {},
        }
        result = evaluator(event)
        assert result["result"] == "DENY"
        assert "exceeded" in result["reason"].lower() or "reached" in result["reason"].lower()

    def test_denies_at_hard_limit_tool_call_phase(self) -> None:
        """DENY when reaching hard limit on tool_call phase."""
        evaluator = token_budget(max_tokens=100000)
        event = {
            "type": "tool_call",
            "context": {"usage": {"total_tokens": 100000}},
            "session_state": {},
        }
        result = evaluator(event)
        assert result["result"] == "DENY"

    def test_denies_above_hard_limit(self) -> None:
        """DENY when exceeding hard limit."""
        evaluator = token_budget(max_tokens=100000)
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 150000}},
            "session_state": {},
        }
        result = evaluator(event)
        assert result["result"] == "DENY"

    def test_hard_limit_takes_precedence_over_soft(self) -> None:
        """Hard limit DENY takes precedence when both would fire."""
        evaluator = token_budget(max_tokens=100000, ask_thresholds_tokens=[50000])
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 100000}},
            "session_state": {},
        }
        result = evaluator(event)
        assert result["result"] == "DENY"

    def test_soft_only_no_hard_limit(self) -> None:
        """Policy works with only soft thresholds (no hard limit)."""
        evaluator = token_budget(ask_thresholds_tokens=[50000])
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 100000}},
            "session_state": {},
        }
        result = evaluator(event)
        # Exceeds soft threshold, so ASK (no hard limit to DENY)
        assert result["result"] == "ASK"

    def test_hard_only_no_soft_thresholds(self) -> None:
        """Policy works with only hard limit (no soft thresholds)."""
        evaluator = token_budget(max_tokens=100000)
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 100000}},
            "session_state": {},
        }
        result = evaluator(event)
        assert result["result"] == "DENY"

    def test_token_count_from_components(self) -> None:
        """Correctly sums token components."""
        evaluator = token_budget(max_tokens=100000)
        event = {
            "type": "request",
            "context": {
                "usage": {
                    "input_tokens": 60000,
                    "output_tokens": 50000,
                    "cache_read_input_tokens": 0,
                }
            },
            "session_state": {},
        }
        result = evaluator(event)
        # 60k + 50k = 110k, which >= 100k, so should DENY
        assert result["result"] == "DENY"

    def test_multiple_soft_thresholds_sorted(self) -> None:
        """Multiple soft thresholds are sorted and checked in order."""
        evaluator = token_budget(max_tokens=200000, ask_thresholds_tokens=[100000, 50000, 150000])
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 75000}},
            "session_state": {},
        }
        result = evaluator(event)
        # Crosses the 50000 threshold (the lowest one <= 75000)
        assert result["result"] == "ASK"
        assert "50000" in result["reason"] or "50,000" in result["reason"]

    def test_soft_threshold_re_asks_on_decline(self) -> None:
        """Soft threshold re-asks if not approved."""
        evaluator = token_budget(ask_thresholds_tokens=[50000])
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 60000}},
            "session_state": {"token_budget_ask_approved": 0},
        }
        result = evaluator(event)
        # Crosses 50000 threshold, not approved (approved_up_to=0), so ASK
        assert result["result"] == "ASK"

    def test_handles_malformed_state(self) -> None:
        """Handles malformed session_state gracefully."""
        evaluator = token_budget(max_tokens=100000)
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 50000}},
            "session_state": {"token_budget_ask_approved": "not_a_number"},
        }
        result = evaluator(event)
        # Should treat malformed state as 0 and allow
        assert result["result"] == "ALLOW"

    def test_includes_all_cached_token_types(self) -> None:
        """Token count includes all cached read types."""
        evaluator = token_budget(max_tokens=100)
        event = {
            "type": "request",
            "context": {
                "usage": {
                    "input_tokens": 30,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 25,
                }
            },
            "session_state": {},
        }
        result = evaluator(event)
        # 30 + 20 + 30 + 25 = 105, which >= 100, so should DENY
        assert result["result"] == "DENY"

    def test_state_updates_on_ask(self) -> None:
        """ASK includes state_updates for approved checkpoint."""
        evaluator = token_budget(ask_thresholds_tokens=[50000])
        event = {
            "type": "request",
            "context": {"usage": {"total_tokens": 50000}},
            "session_state": {},
        }
        result = evaluator(event)
        assert result["result"] == "ASK"
        assert result["state_updates"] is not None
        assert len(result["state_updates"]) == 1
        assert result["state_updates"][0]["key"] == "token_budget_ask_approved"
        assert result["state_updates"][0]["value"] == 50000
