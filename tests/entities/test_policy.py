"""Tests for policy entity dataclass."""

from __future__ import annotations

from omnigent.entities.policy import Policy


def test_policy_minimal() -> None:
    pol = Policy(
        id="pol_abc123",
        name="block_push",
        session_id="conv_1",
        scope="session",
        created_at=1700000000,
        type="python",
        handler="omnigent.policies.builtins.safety.block_push",
    )
    assert pol.id == "pol_abc123"
    assert pol.name == "block_push"
    assert pol.session_id == "conv_1"
    assert pol.scope == "session"
    assert pol.type == "python"
    assert pol.handler == "omnigent.policies.builtins.safety.block_push"
    assert pol.factory_params is None
    assert pol.enabled is True
    assert pol.updated_at is None
    assert pol.created_by is None


def test_policy_full() -> None:
    pol = Policy(
        id="pol_xyz",
        name="cost_budget",
        session_id=None,
        scope="default",
        created_at=1700000000,
        type="python",
        handler="omnigent.policies.builtins.cost.cost_budget",
        factory_params={"limit": 10.0, "currency": "USD"},
        enabled=False,
        updated_at=1700001000,
        created_by="admin@example.com",
    )
    assert pol.session_id is None  # server-wide default
    assert pol.scope == "default"
    assert pol.factory_params == {"limit": 10.0, "currency": "USD"}
    assert pol.enabled is False
    assert pol.updated_at == 1700001000
    assert pol.created_by == "admin@example.com"


def test_policy_url_type() -> None:
    pol = Policy(
        id="pol_url1",
        name="external_check",
        session_id="conv_1",
        scope="session",
        created_at=1700000000,
        type="url",
        handler="https://hooks.example.com/policy",
    )
    assert pol.type == "url"
    assert pol.handler.startswith("https://")


def test_policy_is_mutable() -> None:
    pol = Policy(
        id="pol_1",
        name="p",
        session_id=None,
        scope="default",
        created_at=1,
        type="python",
        handler="mod.func",
    )
    pol.enabled = False
    pol.updated_at = 2
    assert pol.enabled is False
    assert pol.updated_at == 2
