"""Unit tests for the ACP model-picker branch in ``_fetch_model_options``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.server.routes._sessions import orchestration as orch


@pytest.fixture(autouse=True)
def _clear_model_options_caches() -> None:
    """Model-option caches are module-global; isolate one test from the next."""
    orch._model_options_cache.clear()
    orch._model_options_stale.clear()
    orch._model_options_inflight.clear()
    orch._pushed_model_options_cache.clear()


def _conv(**overrides: object) -> Conversation:
    """Minimal conversation for ACP picker-path testing."""
    defaults: dict[str, object] = {
        "id": "conv_acp",
        "created_at": 1700000000,
        "updated_at": 1700000001,
        "root_conversation_id": "conv_acp",
        "harness_override": None,
        "agent_id": "agent_1",
        "sub_agent_name": None,
    }
    defaults.update(overrides)
    return Conversation(**defaults)  # type: ignore[arg-type]


def test_resolve_harness_is_acp_detects_canonical_acp() -> None:
    """An explicit ``harness_override: acp`` matches.

    Other ``acp:<slug>`` harnesses also canonicalize to ``acp``, but the
    override path exercises the detection without an agent store.
    """
    conv = _conv(harness_override="acp")
    assert orch._resolve_harness_impl_is_acp(conv, None) is True


def test_resolve_harness_is_acp_rejects_other_harnesses() -> None:
    conv = _conv(harness_override="claude-native")
    assert orch._resolve_harness_impl_is_acp(conv, None) is False


def test_resolve_harness_is_acp_false_without_override_or_spec() -> None:
    """No override + no agent_id → harness unknown → not acp."""
    conv = _conv(harness_override=None, agent_id=None)
    assert orch._resolve_harness_impl_is_acp(conv, None) is False


@pytest.mark.asyncio
async def test_load_acp_model_options_returns_empty_without_agent_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No agent store and no runtime global → no spec → empty options."""
    # Another test in the same shard may have left a runtime-installed store
    # behind; pin the global to None so this branch is exercised, not the
    # ambient DB-backed store (whose agents id column rejects "agent_1").
    monkeypatch.setattr("omnigent.runtime._globals._agent_store", None)
    conv = _conv()
    result = await orch._load_acp_model_options("conv_acp", conv, None)
    assert result == []


@pytest.mark.asyncio
async def test_load_acp_model_options_caches_and_serves() -> None:
    """A resolved curated shortlist fills the cache and returns on hit."""
    conv = _conv()
    curated = ("gpt-5.4", "claude-fable-5")

    with (
        patch.object(orch, "_load_agent_spec_for_session", return_value=MagicMock()) as mock_load,
        patch.object(orch, "_publish_model_options", return_value=None) as mock_publish,
        patch("omnigent.model_catalog.acp_curated_models", return_value=curated),
    ):
        options = await orch._load_acp_model_options("conv_acp", conv, MagicMock())
        assert options == [
            {"id": "gpt-5.4", "displayName": "gpt-5.4", "isDefault": True},
            {"id": "claude-fable-5", "displayName": "claude-fable-5", "isDefault": False},
        ]
        mock_publish.assert_called_once_with("conv_acp")

        # Second call: cache hit - no re-resolution.
        mock_load.reset_mock()
        cached = await orch._load_acp_model_options("conv_acp", conv, MagicMock())
        assert cached == options
        mock_load.assert_not_called()


@pytest.mark.asyncio
async def test_load_acp_model_options_returns_empty_when_uncurated() -> None:
    """Fewer than two curated ids → no picker (nothing to pick between)."""
    conv = _conv()

    with (
        patch.object(orch, "_load_agent_spec_for_session", return_value=MagicMock()),
        patch("omnigent.model_catalog.acp_curated_models", return_value=("only-model",)),
    ):
        result = await orch._load_acp_model_options("conv_acp", conv, MagicMock())
        assert result == []
