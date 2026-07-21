from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from omnigent.cli import cli


def _config_path(config_home: Path) -> Path:
    return config_home / "config.yaml"


def test_harness_list_reports_visible_and_dismissed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    _config_path(tmp_path).write_text("dismissed_harnesses:\n- claude-sdk\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["harness", "list"])

    assert result.exit_code == 0, result.output
    assert "claude-sdk" in result.output
    assert "dismissed" in result.output
    assert "codex" in result.output
    assert "visible" in result.output


def test_harness_hide_persists_canonical_alias(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))

    result = CliRunner().invoke(cli, ["harness", "hide", "claude"])

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(_config_path(tmp_path).read_text())["dismissed_harnesses"] == [
        "claude-sdk"
    ]


def test_harness_unhide_removes_harness_from_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    _config_path(tmp_path).write_text(
        "dismissed_harnesses:\n- claude-sdk\n- codex\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["harness", "unhide", "claude"])

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(_config_path(tmp_path).read_text())["dismissed_harnesses"] == ["codex"]


def test_harness_hide_rejects_invalid_id_without_modifying_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    before = "model: gpt-5\n"
    _config_path(tmp_path).write_text(before, encoding="utf-8")

    result = CliRunner().invoke(cli, ["harness", "hide", "not-a-harness"])

    assert result.exit_code != 0
    assert "Unknown harness id" in result.output
    assert _config_path(tmp_path).read_text(encoding="utf-8") == before
