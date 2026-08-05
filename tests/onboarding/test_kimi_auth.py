"""Tests for :mod:`omnigent.onboarding.kimi_auth`.

``kimi login`` writes ``~/.kimi-code/credentials/kimi-code.json`` (verified
against kimi CLI v0.29.1). Detection is a subprocess-free file check: a present,
non-empty file is a completed login; a missing or empty file is not.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.onboarding import kimi_auth as ka


def test_credential_present_and_nonempty_detected(tmp_path: Path) -> None:
    """A present, non-empty credential file reads as a completed login."""
    creds = tmp_path / "kimi-code.json"
    creds.write_text('{"access_token": "sk-abc"}', encoding="utf-8")
    assert ka.kimi_login_detected(creds) is True


def test_credential_absent_not_detected(tmp_path: Path) -> None:
    """A missing credential file reads as not-logged-in."""
    assert ka.kimi_login_detected(tmp_path / "kimi-code.json") is False


def test_credential_empty_not_detected(tmp_path: Path) -> None:
    """An empty (zero-byte) credential file is not a completed login."""
    creds = tmp_path / "kimi-code.json"
    creds.write_text("", encoding="utf-8")
    assert ka.kimi_login_detected(creds) is False


def test_credential_directory_not_detected(tmp_path: Path) -> None:
    """A path that is a directory (not a regular file) is not a login."""
    creds = tmp_path / "kimi-code.json"
    creds.mkdir()
    assert ka.kimi_login_detected(creds) is False


def test_default_path_uses_home_credentials(monkeypatch, tmp_path: Path) -> None:
    """With no argument, detection checks the real ``~/.kimi-code`` location.

    Point ``HOME`` at a tmp dir and recompute the module default so the check is
    deterministic and never depends on the developer's real credential file.
    """
    fake_creds = tmp_path / ".kimi-code" / "credentials" / "kimi-code.json"
    fake_creds.parent.mkdir(parents=True, exist_ok=True)
    fake_creds.write_text('{"access_token": "sk-xyz"}', encoding="utf-8")
    monkeypatch.setattr(ka, "KIMI_CREDENTIALS_PATH", fake_creds)
    assert ka.kimi_login_detected() is True

    fake_creds.unlink()
    assert ka.kimi_login_detected() is False
