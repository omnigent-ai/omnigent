"""Tests for ``omnigent/onboarding/antigravity_auth.py`` — the Gemini key store.

Isolate config + secret store to a tmp dir (file backend) and assert the
read/resolve/configured helpers — including the soft resolution that returns
``None`` on a dangling reference instead of raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.onboarding import secrets as secret_store
from omnigent.onboarding.antigravity_auth import (
    ANTIGRAVITY_SECRET_NAME,
    antigravity_api_key_configured,
    antigravity_api_key_ref,
    antigravity_api_key_settings,
    looks_like_gemini_api_key,
    resolve_antigravity_api_key,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate config + secrets to tmp with the file secret backend.

    :returns: The tmp config-home dir, so a test can write a ``config.yaml``.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_API_KEY", raising=False)
    return tmp_path


def _write_config(tmp_path: Path, block: dict[str, object]) -> None:
    """Write *block* as the isolated ``config.yaml``."""
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(block))


def test_looks_like_gemini_api_key() -> None:
    """The soft prefix check accepts ``AIza`` keys and rejects others."""
    assert looks_like_gemini_api_key("AIzaSyAbC123")
    assert not looks_like_gemini_api_key("sk-ant-123")
    assert not looks_like_gemini_api_key("")


def test_unconfigured_reads_as_none(_isolate: Path) -> None:
    """With no ``antigravity:`` block, every accessor reports "not configured"."""
    assert antigravity_api_key_ref() is None
    assert resolve_antigravity_api_key() is None
    assert antigravity_api_key_configured() is False


def test_keychain_ref_resolves(_isolate: Path) -> None:
    """A ``keychain:`` ref resolves to the secret stored under that name."""
    secret_store.store_secret(ANTIGRAVITY_SECRET_NAME, "AIza_stored")
    _write_config(_isolate, {"antigravity": {"api_key_ref": "keychain:antigravity"}})
    assert antigravity_api_key_ref() == "keychain:antigravity"
    assert resolve_antigravity_api_key() == "AIza_stored"
    assert antigravity_api_key_configured() is True


def test_env_ref_resolves(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``env:`` ref resolves from the environment (no secret-store entry)."""
    monkeypatch.setenv("MY_GEMINI_KEY", "AIza_fromenv")
    _write_config(_isolate, {"antigravity": {"api_key_ref": "env:MY_GEMINI_KEY"}})
    assert resolve_antigravity_api_key() == "AIza_fromenv"
    assert antigravity_api_key_configured() is True


def test_inline_api_key_field_accepted(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-edited inline ``api_key: $VAR`` is honored as a fallback shape."""
    monkeypatch.setenv("INLINE_GEMINI", "AIza_inline")
    _write_config(_isolate, {"antigravity": {"api_key": "$INLINE_GEMINI"}})
    assert resolve_antigravity_api_key() == "AIza_inline"


def test_dangling_keychain_ref_is_soft_none(_isolate: Path) -> None:
    """A reference to a never-stored keychain entry resolves softly to ``None``.

    Failure (an ``OmnigentError`` escaping) would crash an antigravity run / the
    setup readout on a deleted secret instead of falling back to the SDK's
    ambient / Vertex credentials.
    """
    _write_config(_isolate, {"antigravity": {"api_key_ref": "keychain:antigravity"}})
    assert resolve_antigravity_api_key() is None
    assert antigravity_api_key_configured() is False


def test_unset_env_ref_is_soft_none(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``env:`` ref to an unset variable resolves softly to ``None``."""
    monkeypatch.delenv("NOPE_GEMINI_KEY", raising=False)
    _write_config(_isolate, {"antigravity": {"api_key_ref": "env:NOPE_GEMINI_KEY"}})
    assert resolve_antigravity_api_key() is None


def test_settings_shape() -> None:
    """``antigravity_api_key_settings`` builds the dedicated ``antigravity:`` block."""
    assert antigravity_api_key_settings("keychain:antigravity") == {
        "antigravity": {"api_key_ref": "keychain:antigravity"}
    }
