"""Catalog resolution tests for Codex app server."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.harnesses.codex_native.app_server import (
    NativeCodexLaunch,
    build_codex_native_server,
)


@pytest.fixture
def _databricks_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Seed a fresh Codex catalog for the ``prof`` shape that a profile launch must ignore."""
    from omnigent.harnesses.codex_native import app_server
    from omnigent.models import model_catalog_store

    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text("[prof]\nhost = https://h.example.com/\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
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
        ("gpt-1.2-test", "system.ai.gpt-1-2-test"),
        ("databricks-gpt-1-2-test", "system.ai.gpt-1-2-test"),
        ("custom-picker", "custom-picker"),
        (None, "system.ai.gpt-1-2-test"),
    ],
)
def test_build_codex_native_server_resolves_the_model_from_the_workspace(
    _databricks_catalog: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: str | None,
    expected: str,
) -> None:
    """A profile launch pins the workspace's served id; a stored Codex row never stands in."""
    from types import SimpleNamespace

    credentials = Mock(return_value=SimpleNamespace(token="tok"))
    discovery = Mock(return_value=("system.ai.gpt-1-2-test",))
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

    discovery.assert_called_once_with("https://h.example.com", "tok")
    assert f'model="{expected}"' in server.config_overrides
    assert server.env["DATABRICKS_HOST"] == "https://h.example.com"
    overrides = "\n".join(server.config_overrides)
    assert "https://h.example.com/ai-gateway/codex/v1" in overrides
    assert 'databricks auth token --profile \\"prof\\"' in overrides


@pytest.mark.parametrize(
    ("servable", "expected"),
    [
        (
            ("system.ai.gpt-5-6-sol", "system.ai.gpt-5-5"),
            [
                {
                    "id": "system.ai.gpt-5-6-sol",
                    "model": "system.ai.gpt-5-6-sol",
                    "isDefault": True,
                },
                {"id": "system.ai.gpt-5-5", "model": "system.ai.gpt-5-5"},
            ],
        ),
        ((), None),
    ],
)
async def test_codex_launch_models_lists_a_profile_launch_from_its_workspace(
    monkeypatch: pytest.MonkeyPatch,
    servable: tuple[str, ...],
    expected: list[dict[str, object]] | None,
) -> None:
    """A profile shape lists the workspace's models, default first; an empty listing is unknown."""
    from omnigent.harnesses.codex_native import app_server

    listing = Mock(return_value=servable)
    monkeypatch.setattr(app_server, "databricks_profile_codex_models", listing)
    codex_catalog = AsyncMock(side_effect=AssertionError("profile shapes skip Codex rows"))
    monkeypatch.setattr(app_server, "codex_launch_catalog", codex_catalog)

    models = await app_server.codex_launch_models(
        codex_path=sys.executable, launch=NativeCodexLaunch([], None, "prof")
    )

    assert (models.rows if models is not None else None) == expected
    assert models is None or models.stale is False
    listing.assert_called_once_with("prof")


@pytest.mark.parametrize(
    ("live_listing", "cached_listing", "expected"),
    [
        pytest.param(
            ("system.ai.gpt-6-1-sol",),
            ["system.ai.gpt-5-6-sol"],
            ("system.ai.gpt-6-1-sol",),
            id="live",
        ),
        pytest.param((), ["system.ai.gpt-5-6-sol"], (), id="empty-live"),
        pytest.param(
            OSError("listing failed"),
            ["system.ai.gpt-6-1-sol"],
            ("system.ai.gpt-6-1-sol",),
            id="cache",
        ),
        pytest.param(OSError("listing failed"), None, (), id="no-listing"),
        pytest.param(
            OSError("listing failed"), ValueError("bad state"), (), id="cache-unreadable"
        ),
    ],
)
def test_databricks_profile_codex_models_reads_live_then_cached_listing(
    monkeypatch: pytest.MonkeyPatch,
    live_listing: tuple[str, ...] | Exception,
    cached_listing: list[str] | Exception | None,
    expected: tuple[str, ...],
) -> None:
    """The live listing decides, even when empty; a failed read uses ucode's copy, if readable."""
    from types import SimpleNamespace

    from omnigent.harnesses.codex_native import app_server

    monkeypatch.setattr(
        app_server, "_databricks_gateway_host", Mock(return_value="https://h.example.com/")
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        Mock(return_value=SimpleNamespace(token="tok")),
    )
    discover = Mock(return_value=live_listing)
    if isinstance(live_listing, Exception):
        discover.side_effect = live_listing
    monkeypatch.setattr(
        "omnigent.models.databricks_model_discovery.discover_databricks_codex_models", discover
    )
    read_cached_state = Mock(return_value=None)
    if isinstance(cached_listing, Exception):
        read_cached_state.side_effect = cached_listing
    elif cached_listing is not None:
        read_cached_state.return_value = SimpleNamespace(codex_models=cached_listing)
    monkeypatch.setattr("omnigent.onboarding.ucode_state.read_ucode_state", read_cached_state)

    assert app_server.databricks_profile_codex_models("prof") == expected
    discover.assert_called_once_with("https://h.example.com", "tok")
    if isinstance(live_listing, Exception):
        read_cached_state.assert_called_once_with("https://h.example.com")
    else:
        read_cached_state.assert_not_called()


@pytest.mark.parametrize(
    "gateway_host",
    [
        pytest.param(Mock(side_effect=ConnectionError("token endpoint unreachable")), id="raises"),
        pytest.param(Mock(return_value=None), id="no-host"),
    ],
)
def test_databricks_profile_codex_models_is_empty_without_a_host(
    monkeypatch: pytest.MonkeyPatch, gateway_host: Mock
) -> None:
    """A profile whose workspace host cannot resolve has no listing to read."""
    from omnigent.harnesses.codex_native import app_server

    monkeypatch.setattr(app_server, "_databricks_gateway_host", gateway_host)
    discover = Mock(side_effect=AssertionError("no host means no listing"))
    monkeypatch.setattr(
        "omnigent.models.databricks_model_discovery.discover_databricks_codex_models", discover
    )

    assert app_server.databricks_profile_codex_models("prof") == ()
    discover.assert_not_called()


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
