"""Legacy state-dir fallback for the claude/codex/opencode native wrappers.

Sessions created before ids dropped the ``conv_`` prefix named their state
directory ``sha256("conv_<hex>")[:N]``; lookups now receive the bare id. The
state-dir resolver must find the legacy directory (without renaming it) and
must converge to one directory regardless of which id form the caller holds.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import omnigent.claude_native_state as claude_state
import omnigent.codex_native_state as codex_state
import omnigent.opencode_native_state as opencode_state

_HEX = "12dcd7df501e40e9a506a5b0058cbafc"

_MODULES = [
    pytest.param(claude_state, "OMNIGENT_CLAUDE_NATIVE_STATE_DIR", id="claude"),
    pytest.param(codex_state, "OMNIGENT_CODEX_NATIVE_STATE_DIR", id="codex"),
    pytest.param(opencode_state, "OMNIGENT_OPENCODE_NATIVE_STATE_DIR", id="opencode"),
]


def _digest(value: str, module) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[: module._ID_HASH_CHARS]


@pytest.mark.parametrize(("module", "env_var"), _MODULES)
def test_legacy_prefixed_dir_found_for_bare_id(
    module, env_var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var, str(tmp_path))
    legacy_dir = tmp_path / _digest(f"conv_{_HEX}", module)
    legacy_dir.mkdir()

    resolved = module._state_dir_for_conversation_id(_HEX)

    assert resolved == legacy_dir, "pre-migration session state must stay reachable"


@pytest.mark.parametrize(("module", "env_var"), _MODULES)
def test_bare_dir_wins_when_present(
    module, env_var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var, str(tmp_path))
    bare_dir = tmp_path / _digest(_HEX, module)
    bare_dir.mkdir()
    (tmp_path / _digest(f"conv_{_HEX}", module)).mkdir()  # stale legacy sibling

    assert module._state_dir_for_conversation_id(_HEX) == bare_dir


@pytest.mark.parametrize(("module", "env_var"), _MODULES)
def test_prefixed_and_bare_inputs_converge(
    module, env_var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var, str(tmp_path))

    # No dirs exist: both forms must point at the SAME (bare-digest) location,
    # so a pasted legacy id and a DB-read bare id never split state.
    from_bare = module._state_dir_for_conversation_id(_HEX)
    from_prefixed = module._state_dir_for_conversation_id(f"conv_{_HEX}")
    assert from_bare == from_prefixed == tmp_path / _digest(_HEX, module)


@pytest.mark.parametrize(("module", "env_var"), _MODULES)
def test_fresh_session_uses_bare_digest(
    module, env_var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var, str(tmp_path))
    resolved = module._state_dir_for_conversation_id(_HEX)
    assert resolved == tmp_path / _digest(_HEX, module)
