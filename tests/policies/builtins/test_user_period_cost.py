"""Tests for user_period_cost_budget policy.

Covers all period types (day, week, month, quarter, year).
"""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.cost import user_period_cost_budget
from omnigent.policies.schema import USER_PERIOD_ASK_APPROVED_STATE_KEY


def _event(
    period_cost: list[dict[str, float | str | None]] | None = None,
    usage: dict[str, float] | None = None,
    model: str | None = "opus",
    session_state: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a policy event for testing."""
    return {
        "type": "request",
        "context": {
            "user_daily_cost": period_cost or [],
            "usage": usage or {},
            "model": model,
        },
        "session_state": session_state or {},
    }


class TestPeriodValidation:
    """Test period parameter validation."""

    def test_valid_periods(self):
        """All valid period types should be accepted."""
        for period in ["day", "week", "month", "quarter", "year"]:
            policy = user_period_cost_budget(period=period, max_cost_usd=10.0)
            assert policy is not None

    def test_invalid_period(self):
        """Invalid period should raise ValueError."""
        with pytest.raises(ValueError, match="period must be"):
            user_period_cost_budget(period="invalid", max_cost_usd=10.0)


class TestMonthlyBudget:
    """Test monthly period budgets."""

    def test_allow_under_budget(self):
        """Should ALLOW when spend is under the limit."""
        policy = user_period_cost_budget(period="month", max_cost_usd=100.0)
        event = _event(
            period_cost=[
                {
                    "cost_usd": 50.0,
                    "ask_approved_usd": 0.0,
                    "user_id": "alice@example.com",
                    "day_utc": "2026-08-25",
                }
            ]
        )
        result = policy(event)
        assert result["result"] == "ALLOW"

    def test_deny_over_budget_expensive_model(self):
        """Should DENY when over budget on expensive model."""
        policy = user_period_cost_budget(
            period="month", max_cost_usd=100.0, expensive_models=["opus"]
        )
        event = _event(
            period_cost=[
                {
                    "cost_usd": 150.0,
                    "ask_approved_usd": 0.0,
                    "user_id": "alice@example.com",
                    "day_utc": "2026-08-25",
                }
            ],
            model="opus",
        )
        result = policy(event)
        assert result["result"] == "DENY"
        assert "monthly cost budget" in result["reason"]
        assert "$150.00" in result["reason"]

    def test_allow_over_budget_cheap_model(self):
        """Should ALLOW when over budget but on cheap model."""
        policy = user_period_cost_budget(
            period="month", max_cost_usd=100.0, expensive_models=["opus"]
        )
        event = _event(
            period_cost=[
                {
                    "cost_usd": 150.0,
                    "ask_approved_usd": 0.0,
                    "user_id": "alice@example.com",
                    "day_utc": "2026-08-25",
                }
            ],
            model="haiku",
        )
        result = policy(event)
        assert result["result"] == "ALLOW"

    def test_ask_at_soft_threshold(self):
        """Should ASK when crossing soft threshold."""
        policy = user_period_cost_budget(
            period="month", max_cost_usd=100.0, ask_thresholds_usd=[25.0, 50.0]
        )
        event = _event(
            period_cost=[
                {
                    "cost_usd": 30.0,
                    "ask_approved_usd": 0.0,
                    "user_id": "alice@example.com",
                    "day_utc": "2026-08-25",
                }
            ]
        )
        result = policy(event)
        assert result["result"] == "ASK"
        assert "$30.00" in result["reason"]
        assert "$25.00" in result["reason"]
        assert result["state_updates"][0]["key"] == USER_PERIOD_ASK_APPROVED_STATE_KEY
        assert result["state_updates"][0]["value"] == 25.0

    def test_no_ask_after_approval(self):
        """Should not ASK again at same threshold after approval."""
        policy = user_period_cost_budget(
            period="month", max_cost_usd=100.0, ask_thresholds_usd=[25.0, 50.0]
        )
        event = _event(
            period_cost=[
                {
                    "cost_usd": 30.0,
                    "ask_approved_usd": 25.0,  # Already approved
                    "user_id": "alice@example.com",
                    "day_utc": "2026-08-25",
                }
            ]
        )
        result = policy(event)
        assert result["result"] == "ALLOW"


class TestWeeklyBudget:
    """Test weekly period budgets."""

    def test_weekly_budget_labels(self):
        """Weekly budgets should use correct labels."""
        policy = user_period_cost_budget(period="week", max_cost_usd=50.0)
        event = _event(
            period_cost=[
                {
                    "cost_usd": 60.0,
                    "ask_approved_usd": 0.0,
                    "user_id": "alice@example.com",
                    "day_utc": "2026-08-25",
                }
            ],
            model="opus",
        )
        result = policy(event)
        assert result["result"] == "DENY"
        assert "weekly cost budget" in result["reason"]

    def test_weekly_threshold_message(self):
        """Weekly threshold messages should mention 'week'."""
        policy = user_period_cost_budget(
            period="week", max_cost_usd=50.0, ask_thresholds_usd=[10.0]
        )
        event = _event(
            period_cost=[
                {
                    "cost_usd": 15.0,
                    "ask_approved_usd": 0.0,
                    "user_id": "bob@example.com",
                    "day_utc": "2026-08-25",
                }
            ]
        )
        result = policy(event)
        assert result["result"] == "ASK"
        assert "this week" in result["reason"].lower()


class TestQuarterlyBudget:
    """Test quarterly period budgets."""

    def test_quarterly_budget_labels(self):
        """Quarterly budgets should use correct labels."""
        policy = user_period_cost_budget(period="quarter", max_cost_usd=500.0)
        event = _event(
            period_cost=[
                {
                    "cost_usd": 600.0,
                    "ask_approved_usd": 0.0,
                    "user_id": "alice@example.com",
                    "day_utc": "2026-08-25",
                }
            ],
            model="opus",
        )
        result = policy(event)
        assert result["result"] == "DENY"
        assert "quarterly cost budget" in result["reason"]


class TestYearlyBudget:
    """Test yearly period budgets."""

    def test_yearly_budget_labels(self):
        """Yearly budgets should use correct labels."""
        policy = user_period_cost_budget(period="year", max_cost_usd=2000.0)
        event = _event(
            period_cost=[
                {
                    "cost_usd": 2500.0,
                    "ask_approved_usd": 0.0,
                    "user_id": "alice@example.com",
                    "day_utc": "2026-08-25",
                }
            ],
            model="opus",
        )
        result = policy(event)
        assert result["result"] == "DENY"
        assert "yearly cost budget" in result["reason"]


class TestAggregation:
    """Test cost aggregation across days."""

    def test_aggregates_multiple_days(self):
        """Should aggregate costs from multiple days in the period."""
        policy = user_period_cost_budget(
            period="month", max_cost_usd=100.0, ask_thresholds_usd=[25.0]
        )
        event = _event(
            period_cost=[
                {
                    "cost_usd": 15.0,
                    "ask_approved_usd": 0.0,
                    "user_id": "alice@example.com",
                    "day_utc": "2026-08-24",
                },
                {
                    "cost_usd": 15.0,
                    "ask_approved_usd": 0.0,
                    "user_id": "alice@example.com",
                    "day_utc": "2026-08-25",
                },
            ]
        )
        result = policy(event)
        assert result["result"] == "ASK"


class TestBlockAllModels:
    """Test blocking all models (expensive_models=[])."""

    def test_block_all_models(self):
        """Empty expensive_models should block all models."""
        policy = user_period_cost_budget(period="month", max_cost_usd=100.0, expensive_models=[])
        event = _event(
            period_cost=[
                {
                    "cost_usd": 150.0,
                    "ask_approved_usd": 0.0,
                    "user_id": "alice@example.com",
                    "day_utc": "2026-08-25",
                }
            ],
            model="haiku",  # Even cheap model
        )
        result = policy(event)
        assert result["result"] == "DENY"
        assert "All model calls are blocked" in result["reason"]


class TestToolCallPhase:
    """Test policy on tool_call phase."""

    def test_deny_on_tool_call(self):
        """Should DENY tool calls when over budget."""
        policy = user_period_cost_budget(
            period="month", max_cost_usd=100.0, expensive_models=["opus"]
        )
        event = {
            "type": "tool_call",
            "context": {
                "user_daily_cost": [
                    {
                        "cost_usd": 150.0,
                        "ask_approved_usd": 0.0,
                        "user_id": "alice@example.com",
                        "day_utc": "2026-08-25",
                    }
                ],
                "usage": {},
                "model": "opus",
            },
            "session_state": {},
        }
        result = policy(event)
        assert result["result"] == "DENY"

    def test_abstain_on_other_phases(self):
        """Should ALLOW on phases other than request/tool_call."""
        policy = user_period_cost_budget(
            period="month", max_cost_usd=100.0, expensive_models=["opus"]
        )
        event = {
            "type": "response",  # Not gated
            "context": {
                "user_daily_cost": [
                    {
                        "cost_usd": 150.0,
                        "ask_approved_usd": 0.0,
                        "user_id": "alice@example.com",
                        "day_utc": "2026-08-25",
                    }
                ],
                "usage": {},
                "model": "opus",
            },
            "session_state": {},
        }
        result = policy(event)
        assert result["result"] == "ALLOW"


class TestSingleUserMode:
    """Test behavior in single-user mode (no owner)."""

    def test_allow_without_owner(self):
        """Should ALLOW when no owner (single-user mode)."""
        policy = user_period_cost_budget(period="month", max_cost_usd=100.0)
        event = _event(
            period_cost={
                "cost_usd": 0.0,  # No cost tracked without owner
                "ask_approved_usd": 0.0,
                # No user_id field
                "period": "2026-08",
            }
        )
        result = policy(event)
        assert result["result"] == "ALLOW"
