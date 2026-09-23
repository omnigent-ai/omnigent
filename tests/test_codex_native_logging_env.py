"""Native runtime diagnostics are opt-in and preserve explicit logging choices."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest

from omnigent.harnesses.codex_native import app_server
from omnigent.harnesses.codex_native.stderr_diagnostics import (
    CODEX_DIAGNOSTIC_RUST_LOG,
    codex_app_server_diagnostic_env,
)
from omnigent.process_logging import HARNESS_STDERR_ENABLED_ENV_VAR


def test_default_filter_supports_both_http_clients_without_legacy_payload_tracing() -> None:
    directives = CODEX_DIAGNOSTIC_RUST_LOG.split(",")
    assert "codex_http_client=debug" in directives
    assert "codex_client::default_client=debug" in directives
    assert not any(
        directive.partition("=")[0] in {"codex_api", "codex_client", "codex_client::transport"}
        for directive in directives
    )


@pytest.mark.parametrize("setting", [None, "0", "false", "1", "true"])
@pytest.mark.parametrize("override", [None, "", "off", "codex_http_client=trace"])
def test_native_logging_env_is_flag_scoped_and_preserves_host_override(
    monkeypatch: pytest.MonkeyPatch, setting: str | None, override: str | None
) -> None:
    if setting is None:
        monkeypatch.delenv(HARNESS_STDERR_ENABLED_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, setting)
    if override is None:
        monkeypatch.delenv("RUST_LOG", raising=False)
    else:
        monkeypatch.setenv("RUST_LOG", override)
    monkeypatch.setenv("UNRELATED_SECRET", "not-forwarded")
    original = {"CODEX_HOME": "/private/codex-home"}

    configured = codex_app_server_diagnostic_env(original)

    assert original == {"CODEX_HOME": "/private/codex-home"}
    expected = dict(original)
    if setting in {"1", "true"}:
        expected["RUST_LOG"] = override if override is not None else CODEX_DIAGNOSTIC_RUST_LOG
    assert configured == expected
    assert configured is not original


@pytest.mark.parametrize("setting", ["0", "1"])
@pytest.mark.parametrize("override", ["", "off", "codex_http_client=trace"])
def test_explicit_launch_filter_wins_without_mutating_input(
    monkeypatch: pytest.MonkeyPatch, setting: str, override: str
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, setting)
    monkeypatch.setenv("RUST_LOG", "host-filter=debug")
    original = {"CODEX_HOME": "/private/codex-home", "RUST_LOG": override}

    assert codex_app_server_diagnostic_env(original) == original
    assert original["RUST_LOG"] == override


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("host_filter", [None, "off"])
async def test_app_server_start_passes_native_filter_only_to_enabled_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    host_filter: str | None,
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1" if enabled else "0")
    if host_filter is None:
        monkeypatch.delenv("RUST_LOG", raising=False)
    else:
        monkeypatch.setenv("RUST_LOG", host_filter)
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setattr(app_server, "_codex_cli_version", AsyncMock(return_value=(0, 152, 1)))
    monkeypatch.setattr(app_server, "acquire_codex_native_process_owner_lock", lambda: None)

    class SpawnObserved(Exception):
        pass

    spawn_env: dict[str, str] = {}

    async def spawn(*args: object, **kwargs: object) -> None:
        assert args[1] == "app-server"
        assert kwargs["stderr"] == asyncio.subprocess.PIPE
        spawn_env.update(cast("dict[str, str]", kwargs["env"]))
        raise SpawnObserved

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    server = app_server.build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=None,
        profile=None,
        bridge_dir=tmp_path / "bridge",
        codex_path="/test/codex",
        reconcile_process_registry=False,
    )
    original_env = dict(server.env)

    with pytest.raises(SpawnObserved):
        await server.start()

    expected = {**original_env, "CODEX_HOME": str(server.codex_home)}
    if enabled:
        expected["RUST_LOG"] = (
            host_filter if host_filter is not None else CODEX_DIAGNOSTIC_RUST_LOG
        )
    assert spawn_env == expected
    assert server.env == original_env
    assert "RUST_LOG" not in app_server.codex_terminal_env(server)
    assert not (source_home / "config.toml").exists()
    assert "log_dir" not in (server.codex_home / "config.toml").read_text()
