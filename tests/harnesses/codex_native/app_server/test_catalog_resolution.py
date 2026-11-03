"""Catalog resolution tests for Codex app server."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.harnesses.codex_native.app_server import (
    NativeCodexLaunch,
    build_codex_native_server,
)


@pytest.fixture
def _databricks_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    from omnigent.harnesses.codex_native import app_server
    from omnigent.models import model_catalog_store

    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text("[prof]\nhost = https://h.example.com/\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    # The launch's explicit executable, not PATH's default, must key the lookup.
    monkeypatch.setattr(app_server, "_find_codex_cli", lambda: "/other/codex")
    fingerprint = app_server.codex_catalog_fingerprint(
        NativeCodexLaunch([], None, "prof"), codex_path=sys.executable
    )
    model_catalog_store.write_catalog(
        "codex-native",
        fingerprint,
        [
            {"id": "gpt-1.2-test", "model": "system.ai.gpt-1-2-test"},
            {"id": "custom-picker", "model": "custom_catalog.schema.model"},
        ],
    )
    return fingerprint


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("system.ai.gpt-1-2-test", "system.ai.gpt-1-2-test"),
        ("databricks-gpt-1-2-test", "system.ai.gpt-1-2-test"),
        ("gpt-1-2-test", "system.ai.gpt-1-2-test"),
        ("gpt-1.2-test", "system.ai.gpt-1-2-test"),
        ("custom-picker", "custom_catalog.schema.model"),
        ("custom_catalog.schema.model", "custom_catalog.schema.model"),
    ],
)
def test_build_codex_native_server_reuses_catalog_model(
    _databricks_catalog: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    expected: str,
) -> None:
    credentials = Mock(side_effect=RuntimeError("credentials unavailable"))
    discovery = Mock(side_effect=RuntimeError("discovery unavailable"))
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace", credentials
    )
    monkeypatch.setattr(
        "omnigent.models.databricks_model_discovery.discover_databricks_codex_models", discovery
    )
    monkeypatch.setenv("DATABRICKS_HOST", "https://ambient.example.com")

    server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=requested,
        profile="prof",
        bridge_dir=tmp_path / "bridge",
        codex_path=sys.executable,
    )

    credentials.assert_not_called()
    discovery.assert_not_called()
    assert f'model="{expected}"' in server.config_overrides
    assert server.env["DATABRICKS_HOST"] == "https://h.example.com"
    overrides = "\n".join(server.config_overrides)
    assert "https://h.example.com/ai-gateway/codex/v1" in overrides
    assert 'databricks auth token --profile \\"prof\\"' in overrides


@pytest.mark.parametrize(
    ("catalog_state", "requested", "expected"),
    [
        ("missing", "databricks-gpt-1-2-test", "system.ai.gpt-1-2-test"),
        ("damaged", "databricks-gpt-1-2-test", "system.ai.gpt-1-2-test"),
        ("stale", "databricks-gpt-1-2-test", "system.ai.gpt-1-2-test"),
        ("missing-model", "databricks-gpt-1-2-test", "system.ai.gpt-1-2-test"),
        ("blank-model", "databricks-gpt-1-2-test", "system.ai.gpt-1-2-test"),
        ("invalid-model", "databricks-gpt-1-2-test", "system.ai.gpt-1-2-test"),
        ("missing", "gpt-1.2-test", "gpt-1.2-test"),
        ("fresh", "unknown-model", "unknown-model"),
        ("fresh", None, "system.ai.gpt-1-2-test"),
    ],
)
def test_resolve_databricks_codex_model_catalog_miss_keeps_discovery(
    _databricks_catalog: str,
    monkeypatch: pytest.MonkeyPatch,
    catalog_state: str,
    requested: str | None,
    expected: str,
) -> None:
    from types import SimpleNamespace

    from omnigent.harnesses.codex_native.app_server import _resolve_databricks_codex_model
    from omnigent.models import model_catalog_store

    path = model_catalog_store.catalog_path("codex-native", _databricks_catalog)
    if catalog_state == "missing":
        path.unlink()
    elif catalog_state == "damaged":
        path.write_text("not json")
    elif catalog_state == "stale":
        old = path.stat().st_mtime - model_catalog_store.CATALOG_STALE_AFTER_S - 60
        os.utime(path, (old, old))
    elif catalog_state.endswith("-model"):
        row: dict[str, object] = {"id": "gpt-1.2-test"}
        if catalog_state != "missing-model":
            row["model"] = " " if catalog_state == "blank-model" else 123
        model_catalog_store.write_catalog("codex-native", _databricks_catalog, [row])

    credentials = Mock(return_value=SimpleNamespace(token="tok"))
    discovery = Mock(return_value=("system.ai.gpt-1-2-test",))
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace", credentials
    )
    monkeypatch.setattr(
        "omnigent.models.databricks_model_discovery.discover_databricks_codex_models", discovery
    )

    assert (
        _resolve_databricks_codex_model(
            "https://h.example.com", "prof", requested, codex_path=sys.executable
        )
        == expected
    )
    credentials.assert_called_once_with("prof")
    discovery.assert_called_once_with("https://h.example.com", "tok")


@pytest.mark.parametrize("changed", ["profile", "workspace", "host", "binary"])
def test_resolve_databricks_codex_model_does_not_reuse_other_catalog(
    _databricks_catalog: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    from types import SimpleNamespace

    from omnigent.harnesses.codex_native.app_server import _resolve_databricks_codex_model

    profile = "other" if changed == "profile" else "prof"
    host = (
        "https://other.example.com"
        if changed in {"workspace", "host"}
        else "https://h.example.com"
    )
    profile_host = host if changed != "host" else "https://h.example.com"
    (tmp_path / "databrickscfg").write_text(f"[{profile}]\nhost = {profile_host}\n")
    codex_path = "/other/codex" if changed == "binary" else sys.executable
    discovery = Mock(return_value=("system.ai.gpt-1-2-test",))
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(token="tok"),
    )
    monkeypatch.setattr(
        "omnigent.models.databricks_model_discovery.discover_databricks_codex_models", discovery
    )

    assert (
        _resolve_databricks_codex_model(
            host, profile, "databricks-gpt-1-2-test", codex_path=codex_path
        )
        == "system.ai.gpt-1-2-test"
    )
    discovery.assert_called_once_with(host, "tok")


def test_resolve_databricks_codex_model_matches_servable_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The codex launch model resolves against what the workspace serves.

    An unset model takes the newest servable id; a legacy ``databricks-``
    override resolves to the served ``system.ai.`` id for that same model; and a
    model the workspace does not serve passes through untouched (the gateway's
    error beats a silent substitution).
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    from omnigent.harnesses.codex_native.app_server import _resolve_databricks_codex_model

    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "databrickscfg"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    servable = ("system.ai.gpt-5-6-sol", "system.ai.gpt-5-6-luna")
    with (
        patch(
            "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
            return_value=SimpleNamespace(token="tok"),
        ),
        patch(
            "omnigent.models.databricks_model_discovery.discover_databricks_codex_models",
            return_value=servable,
        ),
    ):
        assert (
            _resolve_databricks_codex_model("https://h.example.com", "prof", None)
            == "system.ai.gpt-5-6-sol"
        )
        assert (
            _resolve_databricks_codex_model(
                "https://h.example.com", "prof", "databricks-gpt-5-6-luna"
            )
            == "system.ai.gpt-5-6-luna"
        )
        assert (
            _resolve_databricks_codex_model("https://h.example.com", "prof", "databricks-gpt-9-9")
            == "databricks-gpt-9-9"
        )


def test_resolve_databricks_codex_model_discovery_failure_warns_without_traceback(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ucode-state fallback warns in one actionable line, frame-free."""
    import logging
    from unittest.mock import patch

    from omnigent.harnesses.codex_native.app_server import _resolve_databricks_codex_model

    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "databrickscfg"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))

    def _raise(profile: str | None) -> None:
        raise OSError(
            "token-less profile; run `databricks auth login --profile prof` "
            "to refresh the OAuth session"
        )

    with (
        patch(
            "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
            side_effect=_raise,
        ),
        patch("omnigent.onboarding.ucode_state.read_ucode_state", return_value=None),
        caplog.at_level(logging.WARNING, logger="omnigent.harnesses.codex_native.app_server"),
    ):
        resolved = _resolve_databricks_codex_model(
            "https://h.example.com", "prof", "databricks-gpt-9-9"
        )

    assert resolved == "databricks-gpt-9-9"
    warning = next(
        r for r in caplog.records if "live Databricks model discovery failed" in r.getMessage()
    )
    assert not warning.exc_info, (
        "a recoverable ucode-state fallback must not log a traceback at WARNING; "
        "host logging mirrors it to the user's terminal"
    )
    assert "databricks auth login --profile prof" in warning.getMessage()
