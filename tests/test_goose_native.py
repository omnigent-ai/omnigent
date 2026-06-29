"""Unit tests for the omni goose CLI-side helpers (no server needed)."""

from __future__ import annotations

import click
import pytest

from omnigent import goose_native as gn


def test_resolve_goose_executable_found() -> None:
    resolved = gn.resolve_goose_executable(
        env={}, which=lambda cmd: f"/usr/local/bin/{cmd}" if cmd == "goose" else None
    )
    assert resolved == "/usr/local/bin/goose"


def test_resolve_goose_executable_honors_path_override() -> None:
    resolved = gn.resolve_goose_executable(
        env={"OMNIGENT_GOOSE_PATH": "/opt/goose"},
        which=lambda cmd: cmd if cmd == "/opt/goose" else None,
    )
    assert resolved == "/opt/goose"


def test_resolve_goose_executable_missing_raises_with_hint() -> None:
    with pytest.raises(click.ClickException) as exc:
        gn.resolve_goose_executable(env={}, which=lambda _cmd: None)
    assert "block-goose-cli" in str(exc.value)


def test_build_goose_launch_argv() -> None:
    launch = gn.build_goose_launch(
        ["session", "--name", "x"],
        env={},
        which=lambda cmd: f"/bin/{cmd}",
    )
    assert launch.executable == "/bin/goose"
    assert launch.argv == ["/bin/goose", "session", "--name", "x"]


def test_terminal_resource_id_stable() -> None:
    assert gn.goose_terminal_resource_id() == gn.goose_terminal_resource_id()
