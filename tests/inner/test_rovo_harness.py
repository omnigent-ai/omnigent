"""Unit tests for the rovo harness factory (:mod:`omnigent.inner.rovo_harness`)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from omnigent.inner import rovo_harness
from omnigent.inner.rovo_executor import RovoExecutor


def test_create_app_returns_fastapi() -> None:
    app = rovo_harness.create_app()
    assert isinstance(app, FastAPI)


def test_build_executor_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_ROVO_MODEL", "Claude Sonnet 4.6")
    monkeypatch.setenv("HARNESS_ROVO_CWD", "/work")
    monkeypatch.setenv("HARNESS_ROVO_ACLI_PATH", "/opt/acli")
    monkeypatch.setenv("HARNESS_ROVO_CONFIG_FILE", "/cfg.yml")
    monkeypatch.setenv("HARNESS_ROVO_SITE_URL", "https://site")

    ex = rovo_harness._build_rovo_executor()
    assert isinstance(ex, RovoExecutor)
    assert ex._model_override == "Claude Sonnet 4.6"
    assert ex._cwd == "/work"
    assert ex._acli_path == "/opt/acli"
    assert ex._config_file == "/cfg.yml"
    assert ex._site_url == "https://site"
    # The command builder reflects the configured acli path + flags.
    assert ex._command() == [
        "/opt/acli",
        "rovodev",
        "acp",
        "--config-file",
        "/cfg.yml",
        "--site-url",
        "https://site",
    ]


def test_build_executor_defaults_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HARNESS_ROVO_MODEL",
        "HARNESS_ROVO_CWD",
        "HARNESS_ROVO_ACLI_PATH",
        "HARNESS_ROVO_CONFIG_FILE",
        "HARNESS_ROVO_SITE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    ex = rovo_harness._build_rovo_executor()
    assert ex._model_override is None
    assert ex._command() == ["acli", "rovodev", "acp"]
