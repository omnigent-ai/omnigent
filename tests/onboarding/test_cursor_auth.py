"""Tests for ``omnigent/onboarding/cursor_auth.py`` — the Cursor API-key store.

Cursor's ``CURSOR_API_KEY`` lives in a dedicated top-level ``cursor:`` config
block (not the shared global ``auth:``) and the omnigent secret store, resolved
with the same ``resolve_secret`` resolver the provider families use. These
tests isolate the config + secret store to a tmp dir (file backend, no OS
keychain) and assert the read/resolve/configured helpers behave — including the
**soft** resolution that returns ``None`` on a dangling reference instead of
raising, so a run / setup readout falls back rather than crashing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.onboarding import secrets as secret_store
from omnigent.onboarding.cursor_auth import (
    CURSOR_SECRET_NAME,
    cursor_api_key_configured,
    cursor_api_key_ref,
    cursor_api_key_settings,
    looks_like_cursor_api_key,
    resolve_cursor_api_key,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate config + secrets to tmp with the file secret backend.

    :returns: The tmp config-home dir, so a test can write a ``config.yaml``.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    return tmp_path


def _write_config(tmp_path: Path, block: dict[str, object]) -> None:
    """Write *block* as the isolated ``config.yaml``."""
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(block))


def test_looks_like_cursor_api_key() -> None:
    """The soft prefix check accepts ``crsr_`` keys and rejects others."""
    assert looks_like_cursor_api_key("crsr_AbC123")
    assert not looks_like_cursor_api_key("sk-ant-123")
    assert not looks_like_cursor_api_key("")


def test_unconfigured_reads_as_none(_isolate: Path) -> None:
    """With no ``cursor:`` block, every accessor reports "not configured"."""
    assert cursor_api_key_ref() is None
    assert resolve_cursor_api_key() is None
    assert cursor_api_key_configured() is False


def test_keychain_ref_resolves(_isolate: Path) -> None:
    """A ``keychain:`` ref resolves to the secret stored under that name."""
    secret_store.store_secret(CURSOR_SECRET_NAME, "crsr_stored")
    _write_config(_isolate, {"cursor": {"api_key_ref": "keychain:cursor"}})
    assert cursor_api_key_ref() == "keychain:cursor"
    assert resolve_cursor_api_key() == "crsr_stored"
    assert cursor_api_key_configured() is True


def test_env_ref_resolves(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``env:`` ref resolves from the environment (no secret-store entry)."""
    monkeypatch.setenv("MY_CURSOR_KEY", "crsr_fromenv")
    _write_config(_isolate, {"cursor": {"api_key_ref": "env:MY_CURSOR_KEY"}})
    assert resolve_cursor_api_key() == "crsr_fromenv"
    assert cursor_api_key_configured() is True


def test_inline_api_key_field_accepted(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-edited inline ``api_key: $VAR`` is honored as a fallback shape."""
    monkeypatch.setenv("INLINE_CURSOR", "crsr_inline")
    _write_config(_isolate, {"cursor": {"api_key": "$INLINE_CURSOR"}})
    assert resolve_cursor_api_key() == "crsr_inline"


def test_dangling_keychain_ref_is_soft_none(_isolate: Path) -> None:
    """A reference to a never-stored keychain entry resolves softly to ``None``.

    Failure (an ``OmnigentError`` escaping) would crash a cursor run / the
    setup readout on a deleted secret instead of falling back to cursor's own
    login.
    """
    _write_config(_isolate, {"cursor": {"api_key_ref": "keychain:cursor"}})
    assert resolve_cursor_api_key() is None
    assert cursor_api_key_configured() is False


def test_unset_env_ref_is_soft_none(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``env:`` ref to an unset variable resolves softly to ``None``."""
    monkeypatch.delenv("NOPE_CURSOR_KEY", raising=False)
    _write_config(_isolate, {"cursor": {"api_key_ref": "env:NOPE_CURSOR_KEY"}})
    assert resolve_cursor_api_key() is None


def test_settings_shape() -> None:
    """``cursor_api_key_settings`` builds the dedicated ``cursor:`` block."""
    assert cursor_api_key_settings("keychain:cursor") == {
        "cursor": {"api_key_ref": "keychain:cursor"}
    }
