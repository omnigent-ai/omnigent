"""Tests for opencode-native runner orchestration helpers (launch, fork, TUI args)."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

import omnigent.runner.native.orchestration as orchestration
from omnigent.harnesses.opencode_native import bridge as opencode_bridge
from omnigent.harnesses.opencode_native.bridge import (
    OpenCodeNativeBridgeState,
    opencode_db_path_for_bridge_dir,
    write_bridge_state,
)
from omnigent.runner.native.orchestration import (
    _OpenCodeNativeLaunchConfig,
    _prepare_opencode_native_fork,
    _sanitize_opencode_tui_args,
)


class _Resp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _SnapshotClient:
    """Async client stub returning one session snapshot."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot

    async def get(
        self, url: str, timeout: float | None = None, params: dict[str, str] | None = None
    ) -> _Resp:
        return _Resp(200, self._snapshot)


async def test_launch_config_reads_fork_source_labels(monkeypatch: Any) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot = {
        "workspace": "/tmp/repo",
        "labels": {
            "omnigent.fork.source_id": "conv_source",
            "omnigent.fork.source_external_session_id": "ses_src",
            "omnigent.fork.carry_history": "1",
        },
    }
    cfg = await orchestration._opencode_native_launch_config(
        session_id="conv_clone",
        server_client=_SnapshotClient(snapshot),  # type: ignore[arg-type]
    )
    assert cfg.fork_source_id == "conv_source"
    assert cfg.fork_source_external_id == "ses_src"
    assert cfg.fork_carry_history is True


async def test_launch_config_without_fork_labels(monkeypatch: Any) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    cfg = await orchestration._opencode_native_launch_config(
        session_id="conv_plain",
        server_client=_SnapshotClient({"workspace": "/tmp/repo"}),  # type: ignore[arg-type]
    )
    assert cfg.fork_source_id is None
    assert cfg.fork_source_external_id is None
    assert cfg.fork_carry_history is False


def _fork_config(**overrides: Any) -> _OpenCodeNativeLaunchConfig:
    values: dict[str, Any] = {
        "workspace": Path("/repo"),
        "policy_server_url": "http://127.0.0.1:8123",
        "terminal_launch_args": None,
        "model_override": None,
        "external_session_id": None,
        "fork_carry_history": True,
        "fork_source_id": "conv_source",
        "fork_source_external_id": "ses_src",
    }
    values.update(overrides)
    return _OpenCodeNativeLaunchConfig(**values)


@pytest.fixture
def bridge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(opencode_bridge, "_BRIDGE_ROOT", tmp_path / "opencode-native")
    return tmp_path / "opencode-native"


def _seed_source(workspace: str) -> None:
    import sqlite3

    source_dir = opencode_bridge.prepare_bridge_dir("conv_source")
    with sqlite3.connect(opencode_db_path_for_bridge_dir(source_dir)) as conn:
        conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, time_suspended INTEGER)")
        conn.execute("INSERT INTO session_v2 VALUES ('ses_src', NULL)")
    conn.close()
    write_bridge_state(
        source_dir,
        OpenCodeNativeBridgeState(
            session_id="conv_source",
            server_base_url="http://127.0.0.1:1",
            opencode_session_id="ses_src",
            workspace=workspace,
        ),
    )


def test_prepare_native_fork_copies_source_db(bridge_root: Path) -> None:
    _seed_source("/repo")
    clone_dir = opencode_bridge.prepare_bridge_dir("conv_clone")

    source_session = _prepare_opencode_native_fork(
        _fork_config(), bridge_dir=clone_dir, workspace="/repo"
    )

    assert source_session == "ses_src"
    assert opencode_db_path_for_bridge_dir(clone_dir).is_file()


@pytest.mark.parametrize(
    "overrides",
    [
        {"external_session_id": "ses_own"},
        {"fork_carry_history": False},
        {"fork_source_id": None},
        {"fork_source_external_id": None},
        {"fork_source_external_id": "0b8f-claude-uuid"},
    ],
)
def test_prepare_native_fork_skips_without_directive(
    bridge_root: Path, overrides: dict[str, Any]
) -> None:
    _seed_source("/repo")
    clone_dir = opencode_bridge.prepare_bridge_dir("conv_clone")

    assert (
        _prepare_opencode_native_fork(
            _fork_config(**overrides), bridge_dir=clone_dir, workspace="/repo"
        )
        is None
    )
    assert not opencode_db_path_for_bridge_dir(clone_dir).exists()


def test_prepare_native_fork_skips_other_workspace(bridge_root: Path) -> None:
    _seed_source("/elsewhere")
    clone_dir = opencode_bridge.prepare_bridge_dir("conv_clone")

    assert (
        _prepare_opencode_native_fork(_fork_config(), bridge_dir=clone_dir, workspace="/repo")
        is None
    )
    assert not opencode_db_path_for_bridge_dir(clone_dir).exists()


def test_prepare_native_fork_skips_unreachable_source(bridge_root: Path) -> None:
    clone_dir = opencode_bridge.prepare_bridge_dir("conv_clone")
    assert (
        _prepare_opencode_native_fork(_fork_config(), bridge_dir=clone_dir, workspace="/repo")
        is None
    )


@pytest.mark.parametrize(
    "args,expected",
    [
        (["--auto"], []),
        (["--standalone", "--continue", "-c"], []),
        (["--server", "http://x", "--session", "ses_1", "-s", "ses_2"], []),
        (["--server=http://x", "--session=ses_1"], []),
        (["--prompt", "hi", "--log-level", "debug"], ["--prompt", "hi", "--log-level", "debug"]),
        (["--auto", "--prompt", "hi"], ["--prompt", "hi"]),
    ],
)
def test_sanitize_opencode_tui_args(args: list[str], expected: list[str]) -> None:
    assert _sanitize_opencode_tui_args(args) == expected


def test_auto_create_opencode_terminal_accepts_fresh() -> None:
    parameter = inspect.signature(orchestration._auto_create_opencode_terminal).parameters["fresh"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False


def test_auto_create_opencode_terminal_has_no_v1_session_calls() -> None:
    source = inspect.getsource(orchestration._auto_create_opencode_terminal)
    assert "create_session({" not in source, "v1 dict-payload create_session must be gone"
    assert "_resolve_opencode_session(" in source
    assert "_prepare_opencode_native_fork(" in source
    assert "_sanitize_opencode_tui_args(" in source
