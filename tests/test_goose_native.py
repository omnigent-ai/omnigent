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
    from omnigent import _platform

    with pytest.raises(click.ClickException) as exc:
        # No PATH hit and an empty fallback ladder, so the CLI is truly absent.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_platform, "_cli_fallback_dirs", tuple)
            gn.resolve_goose_executable(env={}, which=lambda _cmd: None)
    assert "block-goose-cli" in str(exc.value)


def test_resolve_goose_executable_uses_fallback_ladder(monkeypatch, tmp_path) -> None:
    """Regression for #3341: a CLI installed in a global install dir that is not
    on ``PATH`` (e.g. an npm-global bin dir the daemon's frozen ``PATH`` omits)
    is now found at launch via the same ``resolve_cli_binary`` ladder readiness
    and the SDK executors use, so a green readiness badge implies a launchable
    binary. Before, the bare ``shutil.which`` lookup raised despite the CLI
    being installed. Representative of all seven native ``resolve_*_executable``
    functions, which share this pattern."""
    from omnigent import _platform

    fallback = tmp_path / "bin"
    fallback.mkdir()
    goose = fallback / "goose"
    goose.write_text("#!/bin/sh\n")
    goose.chmod(0o755)
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: (fallback,))
    # Not on PATH (which returns None), but present in the ladder dir.
    resolved = gn.resolve_goose_executable(env={}, which=lambda _cmd: None)
    assert resolved == str(goose)


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
