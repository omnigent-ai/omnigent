"""Unit tests for the Goose onboarding readiness/config reporter."""

from __future__ import annotations

import subprocess
from pathlib import Path

from omnigent.onboarding import goose_auth

# Realistic ``goose info -v`` stdout: the "goose Configuration:" block lists
# GOOSE_MODEL / GOOSE_PROVIDER (2-space indented) followed by extensions.
_GOOSE_INFO_V_CONFIGURED = """\
goose Version:
  Version:                  1.38.0

Paths:
Config dir:              /home/u/.config/goose
Config yaml:             /home/u/.config/goose/config.yaml

goose Configuration:
  GOOSE_MODEL: claude-haiku-4-5-20251001
  GOOSE_PROVIDER: anthropic
  extensions:
    developer:
      enabled: true
"""

# When no provider is configured, Goose omits the GOOSE_* lines entirely.
_GOOSE_INFO_V_UNCONFIGURED = """\
goose Version:
  Version:                  1.38.0

goose Configuration:
  extensions:
    developer:
      enabled: true
"""


def _fake_run(stdout: str, returncode: int = 0):
    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["goose"], returncode=returncode, stdout=stdout, stderr="")

    return _run


def test_config_path_honors_xdg(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/cfg")
    assert goose_auth.goose_config_path() == Path("/cfg/goose/config.yaml")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert goose_auth.goose_config_path().parts[-2:] == ("goose", "config.yaml")


# ── goose info -v detection (the authoritative path) ─────────────────────


def test_goose_info_config_parses_provider_and_model(monkeypatch) -> None:
    monkeypatch.setattr(goose_auth, "_goose_binary", lambda: "/usr/local/bin/goose")
    monkeypatch.setattr(subprocess, "run", _fake_run(_GOOSE_INFO_V_CONFIGURED))
    provider, model = goose_auth.goose_info_config()
    assert provider == "anthropic"
    assert model == "claude-haiku-4-5-20251001"


def test_goose_info_config_none_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(goose_auth, "_goose_binary", lambda: "/usr/local/bin/goose")
    monkeypatch.setattr(subprocess, "run", _fake_run(_GOOSE_INFO_V_UNCONFIGURED))
    assert goose_auth.goose_info_config() == (None, None)


def test_goose_info_config_none_when_binary_absent(monkeypatch) -> None:
    monkeypatch.setattr(goose_auth, "_goose_binary", lambda: None)
    assert goose_auth.goose_info_config() == (None, None)


def test_goose_info_config_none_on_command_failure(monkeypatch) -> None:
    monkeypatch.setattr(goose_auth, "_goose_binary", lambda: "/usr/local/bin/goose")
    monkeypatch.setattr(subprocess, "run", _fake_run("", returncode=1))
    assert goose_auth.goose_info_config() == (None, None)


def test_summary_prefers_goose_info_over_config_file(tmp_path: Path, monkeypatch) -> None:
    """`goose info -v` wins over a stale/differently-shaped config.yaml — the fix
    for "configured via goose configure but setup shows unconfigured"."""
    cfg_dir = tmp_path / "goose"
    cfg_dir.mkdir()
    # A config file that our naive parser can't read a provider from.
    (cfg_dir / "config.yaml").write_text("extensions:\n  developer:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GOOSE_PROVIDER", raising=False)
    monkeypatch.delenv("GOOSE_MODEL", raising=False)
    monkeypatch.setattr(goose_auth, "goose_cli_installed", lambda: True)
    monkeypatch.setattr(goose_auth, "_goose_binary", lambda: "/usr/local/bin/goose")
    monkeypatch.setattr(subprocess, "run", _fake_run(_GOOSE_INFO_V_CONFIGURED))
    summary = goose_auth.goose_config_summary()
    assert summary.provider == "anthropic"
    assert summary.model == "claude-haiku-4-5-20251001"


# ── config.yaml fallback (when the binary can't be run) ──────────────────


def test_summary_falls_back_to_config_file(tmp_path: Path, monkeypatch) -> None:
    cfg_dir = tmp_path / "goose"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        "GOOSE_PROVIDER: anthropic\nGOOSE_MODEL: claude-x\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GOOSE_PROVIDER", raising=False)
    monkeypatch.delenv("GOOSE_MODEL", raising=False)
    monkeypatch.setattr(goose_auth, "goose_cli_installed", lambda: True)
    # No goose binary → info path returns (None, None) → file fallback used.
    monkeypatch.setattr(goose_auth, "_goose_binary", lambda: None)
    summary = goose_auth.goose_config_summary()
    assert summary.provider == "anthropic"
    assert summary.model == "claude-x"
    assert summary.ready is True


def test_env_overrides_everything(tmp_path: Path, monkeypatch) -> None:
    cfg_dir = tmp_path / "goose"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("GOOSE_PROVIDER: anthropic\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("GOOSE_PROVIDER", "openrouter")
    monkeypatch.setattr(goose_auth, "goose_cli_installed", lambda: False)
    monkeypatch.setattr(goose_auth, "_goose_binary", lambda: None)
    summary = goose_auth.goose_config_summary()
    assert summary.provider == "openrouter"  # env wins
    assert summary.ready is False  # binary missing


def test_summary_tolerates_missing_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GOOSE_PROVIDER", raising=False)
    monkeypatch.delenv("GOOSE_MODEL", raising=False)
    monkeypatch.setattr(goose_auth, "goose_cli_installed", lambda: True)
    monkeypatch.setattr(goose_auth, "_goose_binary", lambda: None)
    summary = goose_auth.goose_config_summary()
    assert summary.provider is None and summary.model is None
    assert summary.ready is True
