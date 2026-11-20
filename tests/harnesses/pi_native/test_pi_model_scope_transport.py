"""Failure handling and cache invalidation for catalog-only Pi discovery."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.pi_native import model_scope


@pytest.mark.parametrize("bundled", [False, True])
def test_scope_cache_tracks_settings_catalog_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundled: bool
) -> None:
    """The same picker poll is cached; settings, catalog and CLI edits invalidate it."""
    dist = tmp_path / "dist"
    cli = dist / "bundle" / "cli.js" if bundled else dist / "cli.js"
    cli.parent.mkdir(parents=True)
    resolver = dist / "core" / "model-resolver.js"
    resolver.parent.mkdir()
    resolver.write_text("resolver-v1")
    monkeypatch.setattr(model_scope, "resolve_cli_binary", lambda _: str(cli))
    monkeypatch.setattr(model_scope.shutil, "which", lambda _: "node")
    calls = []

    def run(*args: object, **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="[0]")

    monkeypatch.setattr(model_scope.subprocess, "run", run)
    model_scope._scoped_indices.cache_clear()
    catalog = [model_scope.ScopableModel("provider", "model")]
    assert model_scope.scope_models(["*"], catalog) == catalog
    assert model_scope.scope_models(["*"], catalog) == catalog
    assert len(calls) == 1
    assert model_scope.scope_models(["model"], catalog) == catalog
    catalog.append(model_scope.ScopableModel("provider", "other"))
    assert model_scope.scope_models(["model"], catalog) == catalog[:1]
    stamp = resolver.stat().st_mtime_ns + 1_000_000
    os.utime(resolver, ns=(stamp, stamp))
    assert model_scope.scope_models(["model"], catalog) == catalog[:1]
    assert len(calls) == 4


@pytest.mark.parametrize("failure", ["timeout", "invalid-json", "invalid-index"])
def test_scope_failure_is_retried_without_exposing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    """Broken discovery preserves the caller's fallback and does not cache failure."""
    cli = tmp_path / "dist" / "cli.js"
    resolver = cli.parent / "core" / "model-resolver.js"
    resolver.parent.mkdir(parents=True)
    resolver.write_text("resolver")
    monkeypatch.setattr(model_scope, "resolve_cli_binary", lambda _: str(cli))
    monkeypatch.setattr(model_scope.shutil, "which", lambda _: "node")
    calls = 0

    def run(*args: object, **kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise subprocess.TimeoutExpired("node", 3, stderr="private-diagnostic")
            return SimpleNamespace(
                stdout="private-diagnostic" if failure == "invalid-json" else "[999]"
            )
        return SimpleNamespace(stdout="[0]")

    monkeypatch.setattr(model_scope.subprocess, "run", run)
    model_scope._scoped_indices.cache_clear()
    catalog = [model_scope.ScopableModel("provider", "model")]
    assert model_scope.scope_models(["*"], catalog) == []
    assert "private-diagnostic" not in caplog.text
    assert model_scope.scope_models(["*"], catalog) == catalog
    assert calls == 2
