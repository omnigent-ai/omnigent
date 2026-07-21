"""Tests for the subscription-unpriced native-session cost gate.

A native session backed by a flat-rate subscription (ChatGPT/Codex or Claude
Pro/Max) has ~$0 marginal spend. When the bound runner marks it with the
``cost_control.unpriced`` label, :func:`_persist_native_cumulative_usage` must
record token usage for display but price the cost at a present $0 — so the
cost-budget gate never fires on a phantom token-priced figure, and the session
is not treated as "unpriced" (which would itself trigger an ASK/DENY). These
tests drive that helper directly against a file-backed SQLite store, and cover
the per-harness subscription detectors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.cost_plan import (
    COST_CONTROL_UNPRICED_LABEL_KEY,
    COST_CONTROL_UNPRICED_LABEL_VALUE,
    cost_unpriced_label_set,
)
from omnigent.llms.context_window import ModelPricing
from omnigent.server.routes.sessions import _persist_native_cumulative_usage
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# A codex-native usage payload: cumulative token counts, no explicit cost.
_CODEX_TOKENS = {
    "cumulative_input_tokens": 1_000_000,
    "cumulative_output_tokens": 500_000,
    "model": "databricks-gpt-5-5",
}
# A claude-native usage payload: explicit cumulative + enforcement cost.
_CLAUDE_COST = {
    "cumulative_cost_usd": 42.0,
    "policy_cost_usd": 55.0,
    "model": "databricks-claude-opus-4-8",
}

_FIXED_PRICING = ModelPricing(input_per_token=1e-5, output_per_token=3e-5)


def _store(db_uri: str) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(db_uri)


def _seed(store: SqlAlchemyConversationStore, *, unpriced: bool) -> str:
    """Create a conversation, optionally marked unpriced.

    :param store: The conversation store under test.
    :param unpriced: When ``True`` set the ``cost_control.unpriced`` label.
    :returns: The new conversation id.
    """
    conv = store.create_conversation(title="native session", agent_id="ag_test")
    if unpriced:
        store.set_labels(
            conv.id,
            {COST_CONTROL_UNPRICED_LABEL_KEY: COST_CONTROL_UNPRICED_LABEL_VALUE},
        )
    return conv.id


@pytest.fixture(autouse=True)
def _deterministic_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Price any model at a fixed rate so the priced-control path is stable."""
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: _FIXED_PRICING,
    )


def test_unpriced_label_skips_token_pricing(db_uri: str) -> None:
    """A codex-style token payload is priced at $0 (present) when the label is set.

    Present-$0 (not a missing key) is deliberate: it keeps the cost-budget gate
    quiet AND avoids the "unpriced model" ASK that a missing ``total_cost_usd``
    would trigger for a session that has consumed tokens.
    """
    store = _store(db_uri)
    conv_id = _seed(store, unpriced=True)

    result = _persist_native_cumulative_usage(conv_id, dict(_CODEX_TOKENS), store)

    usage = store.get_conversation(conv_id).session_usage
    assert usage["total_cost_usd"] == 0.0
    assert result == 0.0
    # Token buckets are still recorded for display.
    assert usage["output_tokens"] == 500_000
    assert usage["total_tokens"] == 1_500_000


def test_priced_when_label_absent(db_uri: str) -> None:
    """The same payload IS token-priced when the label is absent."""
    store = _store(db_uri)
    conv_id = _seed(store, unpriced=False)

    result = _persist_native_cumulative_usage(conv_id, dict(_CODEX_TOKENS), store)

    usage = store.get_conversation(conv_id).session_usage
    assert "total_cost_usd" in usage
    # 1e6 input * 1e-5 + 5e5 output * 3e-5 = 10 + 15 = 25.
    assert usage["total_cost_usd"] == pytest.approx(25.0)
    assert result == pytest.approx(25.0)


def test_unpriced_label_zeroes_explicit_and_policy_cost(db_uri: str) -> None:
    """A claude-style explicit cost + policy cost are both zeroed when subscription-covered."""
    store = _store(db_uri)
    conv_id = _seed(store, unpriced=True)

    result = _persist_native_cumulative_usage(conv_id, dict(_CLAUDE_COST), store)

    assert result == 0.0
    usage = store.get_conversation(conv_id).session_usage
    assert usage["total_cost_usd"] == 0.0
    assert usage["policy_cost_usd"] == 0.0


def test_explicit_cost_priced_when_label_absent(db_uri: str) -> None:
    """The claude explicit-cost path bills verbatim when the label is absent."""
    store = _store(db_uri)
    conv_id = _seed(store, unpriced=False)

    _persist_native_cumulative_usage(conv_id, dict(_CLAUDE_COST), store)

    usage = store.get_conversation(conv_id).session_usage
    assert usage["total_cost_usd"] == pytest.approx(42.0)
    assert usage["policy_cost_usd"] == pytest.approx(55.0)


def test_subscription_usage_does_not_read_as_unpriced(db_uri: str) -> None:
    """The $0 result must NOT trip the cost policy's unpriced ASK/DENY path.

    ``_usage_is_unpriced`` fires only when tokens are present but
    ``total_cost_usd`` is absent; pricing the session at a present $0 keeps it
    False, so a subscription session is neither warned nor blocked.
    """
    from omnigent.policies.builtins.cost import _usage_is_unpriced

    store = _store(db_uri)
    conv_id = _seed(store, unpriced=True)
    _persist_native_cumulative_usage(conv_id, dict(_CODEX_TOKENS), store)

    usage = store.get_conversation(conv_id).session_usage
    assert _usage_is_unpriced(usage) is False


def test_cost_unpriced_label_set_helper() -> None:
    """Only the exact key+value counts as unpriced."""
    assert cost_unpriced_label_set({COST_CONTROL_UNPRICED_LABEL_KEY: "true"})
    assert not cost_unpriced_label_set({COST_CONTROL_UNPRICED_LABEL_KEY: "false"})
    assert not cost_unpriced_label_set({"team": "ml"})
    assert not cost_unpriced_label_set({})


# ── Per-harness subscription detectors ───────────────────────────────────────


def _write_auth_json(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_codex_oauth_only_true_for_chatgpt_login(tmp_path: Path) -> None:
    from omnigent.codex_native import _codex_auth_json_is_oauth_only

    path = _write_auth_json(tmp_path, {"tokens": {"access_token": "a", "refresh_token": "r"}})
    assert _codex_auth_json_is_oauth_only(path) is True


def test_codex_oauth_only_false_for_api_key(tmp_path: Path) -> None:
    from omnigent.codex_native import _codex_auth_json_is_oauth_only

    path = _write_auth_json(tmp_path, {"OPENAI_API_KEY": "sk-abc"})
    assert _codex_auth_json_is_oauth_only(path) is False


def test_codex_oauth_only_false_when_api_key_alongside_tokens(tmp_path: Path) -> None:
    """An API key present means the launch may bill the API — not subscription."""
    from omnigent.codex_native import _codex_auth_json_is_oauth_only

    path = _write_auth_json(
        tmp_path,
        {"OPENAI_API_KEY": "sk-abc", "tokens": {"access_token": "a"}},
    )
    assert _codex_auth_json_is_oauth_only(path) is False


def test_codex_oauth_only_false_when_missing(tmp_path: Path) -> None:
    from omnigent.codex_native import _codex_auth_json_is_oauth_only

    assert _codex_auth_json_is_oauth_only(tmp_path / "nope.json") is False


def test_claude_login_subscription_false_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray ANTHROPIC_API_KEY means Claude Code bills the API — not subscription."""
    from omnigent import claude_native

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz")
    monkeypatch.setattr("omnigent.onboarding.ambient._claude_login_detected", lambda: True)
    assert claude_native.claude_login_is_subscription() is False


def test_claude_login_subscription_true_with_login_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent import claude_native

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("omnigent.onboarding.ambient._claude_login_detected", lambda: True)
    assert claude_native.claude_login_is_subscription() is True


def test_claude_login_subscription_false_without_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent import claude_native

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("omnigent.onboarding.ambient._claude_login_detected", lambda: False)
    assert claude_native.claude_login_is_subscription() is False
