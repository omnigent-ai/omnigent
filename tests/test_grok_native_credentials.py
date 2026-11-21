"""Unit tests for grok-native credential/binary resolution."""

from __future__ import annotations

from omnigent.harnesses.grok_native import credentials


def test_env_override_wins(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "grok"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("OMNIGENT_GROK_PATH", str(fake))
    assert credentials.resolve_grok_executable() == str(fake)


def test_env_override_missing_file_falls_through(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_GROK_PATH", "/no/such/path")
    monkeypatch.setattr("shutil.which", lambda _n: None)
    assert credentials.resolve_grok_executable() is None


def test_path_lookup(monkeypatch) -> None:
    monkeypatch.delenv("OMNIGENT_GROK_PATH", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/grok" if name == "grok" else None)
    assert credentials.resolve_grok_executable() == "/usr/bin/grok"


def test_missing_messages_mention_install_and_login() -> None:
    assert "install.sh" in credentials.missing_grok_message()
    assert "login --device-auth" in credentials.missing_grok_message()
    assert "XAI_API_KEY" in credentials.missing_grok_auth_message()
