"""Tests for the OpenCode ``omni setup`` default-model picker helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.cli as cli
from omnigent.cli import _list_opencode_models, _load_global_config, _set_opencode_default_model


@pytest.fixture
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the global config at a tmp file so saves don't touch ``~/.omnigent``."""
    path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", path)
    return path


def _fake_spec() -> SimpleNamespace:
    return SimpleNamespace(binary="opencode")


# ── _list_opencode_models ───────────────────────────────────────────────────


def test_list_models_parses_nonblank_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_install_spec", lambda _key: _fake_spec()
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout="anthropic/claude-sonnet-4-5\nopenai/gpt-5.5\n\n  \n", stderr=""
        ),
    )
    assert _list_opencode_models() == ["anthropic/claude-sonnet-4-5", "openai/gpt-5.5"]


def test_list_models_empty_when_cli_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_install_spec", lambda _key: None
    )
    assert _list_opencode_models() == []


def test_list_models_empty_on_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_install_spec", lambda _key: _fake_spec()
    )

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("no binary")

    monkeypatch.setattr(cli.subprocess, "run", _boom)
    assert _list_opencode_models() == []


# ── _set_opencode_default_model ─────────────────────────────────────────────


def test_set_default_model_persists_choice(
    monkeypatch: pytest.MonkeyPatch, _isolated_config: Path
) -> None:
    monkeypatch.setattr(
        cli, "_list_opencode_models", lambda: ["anthropic/claude-sonnet-4-5", "x/y"]
    )
    monkeypatch.setattr("omnigent.onboarding.interactive.select", lambda *a, **k: 0)
    status = _set_opencode_default_model(current=None)
    assert status == "✓ default model: anthropic/claude-sonnet-4-5"
    assert _load_global_config()["opencode_model"] == "anthropic/claude-sonnet-4-5"


def test_set_default_model_clear_unsets(
    monkeypatch: pytest.MonkeyPatch, _isolated_config: Path
) -> None:
    cli._save_global_config({"opencode_model": "x/y"})
    monkeypatch.setattr(cli, "_list_opencode_models", lambda: ["a/b"])
    # options == ["a/b", "Clear default ..."]; index 1 is the clear row.
    monkeypatch.setattr("omnigent.onboarding.interactive.select", lambda *a, **k: 1)
    status = _set_opencode_default_model(current="x/y")
    assert status == "✓ default model cleared"
    assert "opencode_model" not in _load_global_config()


def test_set_default_model_cancel_is_noop(
    monkeypatch: pytest.MonkeyPatch, _isolated_config: Path
) -> None:
    monkeypatch.setattr(cli, "_list_opencode_models", lambda: ["a/b"])
    monkeypatch.setattr("omnigent.onboarding.interactive.select", lambda *a, **k: -1)
    assert _set_opencode_default_model(current=None) is None
    assert _load_global_config() == {}


def test_set_default_model_no_models_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_list_opencode_models", list)
    called = False

    def _select(*_a: object, **_k: object) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr("omnigent.onboarding.interactive.select", _select)
    status = _set_opencode_default_model(current=None)
    assert status is not None and status.startswith("✗")
    assert called is False  # never prompts when there's nothing to pick
