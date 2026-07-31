"""Tests for the legacy onboarding wizard terminal helpers."""

from __future__ import annotations

import sys

import pytest

from omnigent.onboarding import wizard


def _feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Route *lines* to ``click.prompt`` as if typed at the console."""
    fed = iter(lines)

    def _fake_prompt(_text: str) -> str:
        return next(fed)

    monkeypatch.setattr("click.termui.visible_prompt_func", _fake_prompt)


def test_arrow_menu_uses_numbered_fallback_on_windows_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY wizard menus still work on Windows, where raw-termios menus are unavailable."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(wizard, "IS_WINDOWS", True)
    _feed(monkeypatch, ["2"])

    result = wizard._arrow_menu(["alpha", "beta"])

    assert result == 1


def test_arrow_menu_fallback_preserves_back_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback wizard menus preserve the TTY path's Esc-to-go-back behavior."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    _feed(monkeypatch, ["q"])

    with pytest.raises(wizard._GoBack):
        wizard._arrow_menu(["alpha", "beta"])


def test_arrow_menu_fallback_q_is_invalid_when_back_disabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``q`` only goes back when the caller opted into back navigation."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    _feed(monkeypatch, ["q", "2"])

    result = wizard._arrow_menu(["alpha", "beta"], allow_back=False)

    assert result == 1
    assert "Invalid selection." in capsys.readouterr().out


def test_openai_supervisor_uses_catalog_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OpenAI API choice suggests the provider catalog's current default."""
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(wizard, "_list_databricks_profiles", list)
    monkeypatch.setattr(wizard, "_arrow_menu", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        wizard,
        "default_chat_model",
        lambda provider: "catalog-openai-default" if provider == "openai" else None,
    )
    prompts: list[tuple[str, str | None]] = []

    def prompt(label: str, *, default: str | None = None, **_: object) -> str:
        prompts.append((label, default))
        return default or ""

    monkeypatch.setattr(wizard, "_text_prompt", prompt)

    config = wizard._prompt_openai_agents_config()

    assert config.model == "catalog-openai-default"
    assert prompts == [("Supervisor model", "catalog-openai-default")]


def test_databricks_supervisor_uses_catalog_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Databricks profile suggests the Databricks catalog's current default."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(wizard, "_list_databricks_profiles", lambda: ["PROFILE"])
    monkeypatch.setattr(wizard, "_arrow_menu", lambda *args, **kwargs: 2)
    monkeypatch.setattr(
        wizard,
        "default_chat_model",
        lambda provider: "catalog-databricks-default" if provider == "databricks" else None,
    )

    def prompt(label: str, *, default: str | None = None, **_: object) -> str:
        assert label == "Supervisor model"
        return default or ""

    monkeypatch.setattr(wizard, "_text_prompt", prompt)

    config = wizard._prompt_openai_agents_config()

    assert config.model == "catalog-databricks-default"
    assert config.profile == "PROFILE"


def test_custom_supervisor_endpoint_requires_explicit_model(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown custom endpoint never inherits an unrelated vendor model."""
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example.test/v1")
    monkeypatch.setattr(wizard, "_list_databricks_profiles", list)
    monkeypatch.setattr(wizard, "_arrow_menu", lambda *args, **kwargs: 1)

    def unexpected_catalog_lookup(provider: str) -> None:
        raise AssertionError(f"unexpected catalog lookup for {provider}")

    monkeypatch.setattr(wizard, "default_chat_model", unexpected_catalog_lookup)

    answers = iter(("", "custom-model"))

    def prompt(label: str, *, default: str | None = None, **_: object) -> str:
        assert (label, default) == ("Supervisor model", None)
        return next(answers)

    monkeypatch.setattr(wizard, "_text_prompt", prompt)

    config = wizard._prompt_openai_agents_config()

    assert config.model == "custom-model"
    assert config.base_url == "https://gateway.example.test/v1"
    assert "Please enter a model id." in capsys.readouterr().out


def test_openai_supervisor_requires_model_when_catalog_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty OpenAI catalog falls back to explicit operator input."""
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(wizard, "_list_databricks_profiles", list)
    monkeypatch.setattr(wizard, "_arrow_menu", lambda *args, **kwargs: 0)
    monkeypatch.setattr(wizard, "default_chat_model", lambda provider: None)
    answers = iter(("", "operator-model"))

    def prompt(label: str, *, default: str | None = None, **_: object) -> str:
        assert (label, default) == ("Supervisor model", None)
        return next(answers)

    monkeypatch.setattr(wizard, "_text_prompt", prompt)

    config = wizard._prompt_openai_agents_config()

    assert config.model == "operator-model"
