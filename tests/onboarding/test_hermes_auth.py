"""Unit tests for the Hermes onboarding readiness/config reporter."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.onboarding import hermes_auth
from omnigent.onboarding.hermes_auth import HermesConfigSummary, hermes_config_summary


def _write_config(home: Path, model_section: object) -> None:
    """Write ``~/.hermes/config.yaml`` under *home* with the given ``model`` block."""
    hermes_dir = home / ".hermes"
    hermes_dir.mkdir(parents=True, exist_ok=True)
    (hermes_dir / "config.yaml").write_text(yaml.safe_dump({"model": model_section}))


def test_config_path_honors_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert hermes_auth.hermes_config_path() == tmp_path / ".hermes" / "config.yaml"


def test_summary_reports_concrete_provider_and_model(tmp_path: Path, monkeypatch) -> None:
    """A finished ``hermes model`` run (concrete provider + model) reads ready."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hermes_auth, "hermes_cli_installed", lambda: True)
    _write_config(tmp_path, {"default": "z-ai/glm-5.2", "provider": "openrouter"})

    summary = hermes_config_summary()
    assert summary == HermesConfigSummary(
        installed=True, provider="openrouter", model="z-ai/glm-5.2"
    )
    assert summary.ready is True
    assert summary.describe() == "openrouter / z-ai/glm-5.2"


def test_summary_auto_provider_is_not_configured(tmp_path: Path, monkeypatch) -> None:
    """The fresh-scaffold ``provider: auto`` sentinel reports as not-yet-configured.

    A fresh ``hermes`` install ships ``model.provider: auto`` (auto-detect) with a
    placeholder model. That must not read as ready, or the overview would call an
    untouched install "configured" — the exact bug this reporter fixes.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hermes_auth, "hermes_cli_installed", lambda: True)
    _write_config(tmp_path, {"default": "anthropic/claude-opus-4.6", "provider": "auto"})

    summary = hermes_config_summary()
    assert summary.provider is None
    assert summary.ready is False


@pytest.mark.parametrize("provider", ["", "  ", "AUTO", "Auto"])
def test_summary_blank_or_auto_provider_variants_not_ready(
    tmp_path: Path, monkeypatch, provider: str
) -> None:
    """Empty/whitespace and any-case ``auto`` all collapse to unconfigured."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hermes_auth, "hermes_cli_installed", lambda: True)
    _write_config(tmp_path, {"default": "some/model", "provider": provider})
    assert hermes_config_summary().ready is False


def test_summary_accepts_model_alternate_key(tmp_path: Path, monkeypatch) -> None:
    """Hermes allows ``model.model`` as well as ``model.default`` for the model id."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hermes_auth, "hermes_cli_installed", lambda: True)
    _write_config(tmp_path, {"model": "moonshot/kimi-k2", "provider": "openrouter"})

    summary = hermes_config_summary()
    assert summary.model == "moonshot/kimi-k2"
    assert summary.describe() == "openrouter / moonshot/kimi-k2"


def test_summary_missing_config_is_not_configured(tmp_path: Path, monkeypatch) -> None:
    """No ``~/.hermes/config.yaml`` at all → installed but not configured."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hermes_auth, "hermes_cli_installed", lambda: True)

    summary = hermes_config_summary()
    assert summary == HermesConfigSummary(installed=True, provider=None, model=None)
    assert summary.ready is False


@pytest.mark.parametrize("body", ["", "not-a-mapping", "model: [a, b]", ": : :"])
def test_summary_malformed_config_never_raises(tmp_path: Path, monkeypatch, body: str) -> None:
    """A missing/garbage/non-mapping config degrades to not-configured, never raises."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hermes_auth, "hermes_cli_installed", lambda: True)
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir(parents=True, exist_ok=True)
    (hermes_dir / "config.yaml").write_text(body)

    summary = hermes_config_summary()
    assert summary.provider is None
    assert summary.ready is False


def test_ready_gates_on_installed_binary(tmp_path: Path, monkeypatch) -> None:
    """A configured provider with no ``hermes`` binary is not ready (nothing to launch)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hermes_auth, "hermes_cli_installed", lambda: False)
    _write_config(tmp_path, {"default": "z-ai/glm-5.2", "provider": "openrouter"})

    summary = hermes_config_summary()
    assert summary.provider == "openrouter"  # config is still read…
    assert summary.installed is False
    assert summary.ready is False  # …but an absent binary can't be launched.


@pytest.mark.parametrize(
    "summary,expected",
    [
        (HermesConfigSummary(True, "openrouter", "z-ai/glm-5.2"), "openrouter / z-ai/glm-5.2"),
        (HermesConfigSummary(True, "openrouter", None), "openrouter"),
        (HermesConfigSummary(True, None, "z-ai/glm-5.2"), "z-ai/glm-5.2"),
        (HermesConfigSummary(True, None, None), "Configured"),
    ],
)
def test_describe_variants(summary: HermesConfigSummary, expected: str) -> None:
    assert summary.describe() == expected
