"""Tests for keyring and file-backed secrets."""

from __future__ import annotations

import json
import os
import stat
import traceback
from pathlib import Path

import keyring.errors
import pytest

import omnigent.onboarding.secrets as secrets
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.onboarding.provider_config import resolve_secret


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


@pytest.mark.parametrize(
    "error_type",
    [keyring.errors.NoKeyringError, keyring.errors.KeyringLocked, keyring.errors.KeyringError],
)
def test_keyring_failure_without_fallback_reports_access_error(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[keyring.errors.KeyringError],
) -> None:
    monkeypatch.delenv("OMNIGENT_DISABLE_KEYRING")

    def get_password(service: str, username: str) -> str:
        raise error_type("private backend details")

    monkeypatch.setattr(keyring, "get_password", get_password)

    with pytest.raises(OmnigentError) as raised:
        resolve_secret("keychain:openrouter")

    message = str(raised.value)
    assert raised.value.code == ErrorCode.INVALID_INPUT
    assert "openrouter" in message
    assert "OS keyring" in message
    assert error_type.__name__ in message
    assert "DBUS_SESSION_BUS_ADDRESS" in message
    assert "XDG_RUNTIME_DIR" in message
    assert "no stored secret" not in message
    assert "private backend details" not in message
    assert "private backend details" not in "".join(traceback.format_exception(raised.value))


def test_keyring_failure_uses_existing_file_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets.store_secret("openrouter", "test-file-key")
    monkeypatch.delenv("OMNIGENT_DISABLE_KEYRING")

    def get_password(service: str, username: str) -> str:
        raise keyring.errors.KeyringError("desktop session unavailable")

    monkeypatch.setattr(keyring, "get_password", get_password)

    assert resolve_secret("keychain:openrouter") == "test-file-key"


def test_available_keyring_missing_secret_keeps_setup_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_DISABLE_KEYRING")
    monkeypatch.setattr(keyring, "get_password", lambda service, username: None)

    with pytest.raises(OmnigentError, match="no stored secret named 'openrouter'"):
        resolve_secret("keychain:openrouter")


def test_file_backend_does_not_access_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    def get_password(service: str, username: str) -> str:
        pytest.fail("keyring must not be queried when disabled")

    monkeypatch.setattr(keyring, "get_password", get_password)
    secrets.store_secret("openrouter", "test-file-key")

    assert resolve_secret("keychain:openrouter") == "test-file-key"
    with pytest.raises(OmnigentError, match="no stored secret named 'absent'"):
        resolve_secret("keychain:absent")


@pytest.mark.parametrize("file_failure", ["corrupt", "unreadable"])
def test_keyring_and_file_failure_does_not_expose_backend_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, file_failure: str
) -> None:
    monkeypatch.delenv("OMNIGENT_DISABLE_KEYRING")

    def get_password(service: str, username: str) -> str:
        raise keyring.errors.KeyringError("private backend details")

    monkeypatch.setattr(keyring, "get_password", get_password)
    if file_failure == "corrupt":
        (tmp_path / "secrets.json").write_text("invalid JSON")
        expected_error = json.JSONDecodeError
    else:

        def read_file() -> dict[str, str]:
            raise PermissionError("file unreadable")

        monkeypatch.setattr(secrets, "_read_secrets_file", read_file)
        expected_error = PermissionError

    with pytest.raises(expected_error) as raised:
        resolve_secret("keychain:openrouter")

    assert "private backend details" not in "".join(traceback.format_exception(raised.value))


def test_working_keyring_does_not_read_corrupt_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OMNIGENT_DISABLE_KEYRING")
    monkeypatch.setattr(keyring, "get_password", lambda service, username: "test-keyring-key")
    (tmp_path / "secrets.json").write_text("invalid JSON")

    assert resolve_secret("keychain:openrouter") == "test-keyring-key"
