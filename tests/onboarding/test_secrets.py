"""Tests for the file-backend secret store (``secrets.py``)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import omnigent.onboarding.secrets as secrets


@pytest.fixture(autouse=True)
def _file_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force the file backend at a tmp config home (off the real keychain)."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")


def test_store_and_load_roundtrip() -> None:
    secrets.store_secret("anthropic", "sk-ant-value")
    assert secrets.load_secret("anthropic") == "sk-ant-value"


def test_secrets_file_created_0600_even_under_permissive_umask() -> None:
    """A freshly-created secrets file is 0600 from the start — never briefly
    group/world-readable, even under a permissive umask.

    Guards the window where a plain ``open()``+``chmod``-after would leave the
    file world-readable until the chmod landed; the store now creates it 0600
    atomically via ``os.open(O_CREAT, 0o600)``.
    """
    old = os.umask(0o000)  # most permissive: exposes any create-then-chmod gap
    try:
        secrets.store_secret("openai", "sk-openai-value")
    finally:
        os.umask(old)
    path = secrets._secrets_path()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_delete_secret_removes_it() -> None:
    secrets.store_secret("openrouter", "sk-or-value")
    secrets.delete_secret("openrouter")
    assert secrets.load_secret("openrouter") is None
