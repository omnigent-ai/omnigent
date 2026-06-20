"""Unit tests for the goose-native onboarding readiness/config reporter."""

from __future__ import annotations

from pathlib import Path

from omnigent.onboarding import goose_auth


def test_config_path_honors_xdg(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/cfg")
    assert goose_auth.goose_config_path() == Path("/cfg/goose/config.yaml")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert goose_auth.goose_config_path().parts[-2:] == ("goose", "config.yaml")


def test_summary_reads_config_file(tmp_path: Path, monkeypatch) -> None:
    cfg_dir = tmp_path / "goose"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        "GOOSE_PROVIDER: anthropic\nGOOSE_MODEL: claude-x\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GOOSE_PROVIDER", raising=False)
    monkeypatch.delenv("GOOSE_MODEL", raising=False)
    monkeypatch.setattr(goose_auth, "goose_cli_installed", lambda: True)
    summary = goose_auth.goose_config_summary()
    assert summary.provider == "anthropic"
    assert summary.model == "claude-x"
    assert summary.ready is True


def test_env_overrides_config(tmp_path: Path, monkeypatch) -> None:
    cfg_dir = tmp_path / "goose"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("GOOSE_PROVIDER: anthropic\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("GOOSE_PROVIDER", "openrouter")
    monkeypatch.setattr(goose_auth, "goose_cli_installed", lambda: False)
    summary = goose_auth.goose_config_summary()
    assert summary.provider == "openrouter"  # env wins
    assert summary.ready is False  # binary missing


def test_summary_tolerates_missing_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GOOSE_PROVIDER", raising=False)
    monkeypatch.delenv("GOOSE_MODEL", raising=False)
    monkeypatch.setattr(goose_auth, "goose_cli_installed", lambda: True)
    summary = goose_auth.goose_config_summary()
    assert summary.provider is None and summary.model is None
    assert summary.ready is True
