"""Unit tests for the omni coco CLI-side helpers (no server needed)."""

from __future__ import annotations

import click
import pytest

from omnigent import coco_native as cn


def test_resolve_coco_executable_found() -> None:
    resolved = cn.resolve_coco_executable(
        env={}, which=lambda cmd: f"/usr/local/bin/{cmd}" if cmd == "cortex" else None
    )
    assert resolved == "/usr/local/bin/cortex"


def test_resolve_coco_executable_honors_path_override() -> None:
    resolved = cn.resolve_coco_executable(
        env={"OMNIGENT_CORTEX_PATH": "/opt/cortex"},
        which=lambda cmd: cmd if cmd == "/opt/cortex" else None,
    )
    assert resolved == "/opt/cortex"


def test_resolve_coco_executable_missing_raises_with_hint() -> None:
    with pytest.raises(click.ClickException) as exc:
        cn.resolve_coco_executable(env={}, which=lambda _cmd: None)
    assert "install.sh" in str(exc.value)
    assert "OMNIGENT_CORTEX_PATH" in str(exc.value)


def test_build_coco_launch_argv() -> None:
    launch = cn.build_coco_launch(
        ["--resume", "abc"],
        env={},
        which=lambda cmd: f"/bin/{cmd}",
    )
    assert launch.executable == "/bin/cortex"
    assert launch.argv == ["/bin/cortex", "--resume", "abc"]


def test_terminal_resource_id_stable() -> None:
    assert cn.coco_terminal_resource_id() == cn.coco_terminal_resource_id()
    assert cn.coco_terminal_resource_id().startswith("terminal_")
