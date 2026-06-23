"""Tests for the importer registry."""

from __future__ import annotations

import pytest

from omnigent.importers import available_harnesses, get_adapter
from omnigent.importers.claude_code import ClaudeCodeAdapter
from omnigent.importers.codex import CodexAdapter


def test_available_harnesses() -> None:
    """Both shipped harnesses are registered, sorted."""
    assert available_harnesses() == ["claude_code", "codex"]


def test_get_adapter_returns_matching_adapter() -> None:
    """Each name resolves to its adapter."""
    assert isinstance(get_adapter("claude_code"), ClaudeCodeAdapter)
    assert isinstance(get_adapter("codex"), CodexAdapter)


def test_get_adapter_unknown_raises() -> None:
    """An unregistered harness raises ``KeyError`` listing the known names."""
    with pytest.raises(KeyError, match="unknown harness"):
        get_adapter("pi")


def test_harness_name_matches_registry_key() -> None:
    """The adapter's ``harness_name`` is the key it's registered under — the
    invariant that keeps ``import_transcript`` labels consistent."""
    for name in available_harnesses():
        assert get_adapter(name).harness_name == name
