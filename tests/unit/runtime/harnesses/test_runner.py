"""
Tests for the harness runner CLI argument parsing, module
resolution, and parent-death watchdog.

Spawn-and-serve is exercised by ``test_process_manager.py`` since
that requires actually waiting on uvicorn — covering the same
ground here would just duplicate.
"""

from __future__ import annotations

import pytest

from omnigent.runtime.harnesses import _runner


def test_parse_args_requires_all_args() -> None:
    """Missing any of the four required args is a CLI error.

    Catches a regression where one of the arguments gets a default
    or becomes optional — the runner's contract is that all four
    (harness, module, socket, conversation-id) are AP-allocated
    and must arrive on the command line.
    """
    with pytest.raises(SystemExit):
        # Empty argv → argparse rejects, raising SystemExit(2).
        _runner._parse_args([])


def test_parse_args_returns_all_fields() -> None:
    """All required args round-trip into the namespace."""
    ns = _runner._parse_args(
        [
            "--harness",
            "test",
            "--module",
            "tests.unit.runtime.harnesses._test_harness",
            "--socket",
            "/tmp/example.sock",
            "--conversation-id",
            "conv_abc",
        ]
    )
    assert ns.harness == "test"
    assert ns.module == "tests.unit.runtime.harnesses._test_harness"
    assert ns.socket == "/tmp/example.sock"
    assert ns.conversation_id == "conv_abc"


def test_parse_args_parent_pid_defaults_to_none() -> None:
    """``--parent-pid`` is optional and defaults to ``None``.

    When the parent doesn't pass it (e.g. during manual testing or
    standalone use), the watchdog thread should not start.
    """
    ns = _runner._parse_args(
        [
            "--harness",
            "test",
            "--module",
            "tests.unit.runtime.harnesses._test_harness",
            "--socket",
            "/tmp/example.sock",
            "--conversation-id",
            "conv_abc",
        ]
    )
    assert ns.parent_pid is None


def test_parse_args_parent_pid_parses_integer() -> None:
    """``--parent-pid`` parses as an integer when supplied.

    The watchdog thread needs an integer for ``os.kill(pid, 0)``.
    If argparse stored it as a string, the ``os.kill`` call would
    raise ``TypeError`` silently in the daemon thread.
    """
    ns = _runner._parse_args(
        [
            "--harness",
            "test",
            "--module",
            "tests.unit.runtime.harnesses._test_harness",
            "--socket",
            "/tmp/example.sock",
            "--conversation-id",
            "conv_abc",
            "--parent-pid",
            "12345",
        ]
    )
    assert ns.parent_pid == 12345
    assert isinstance(ns.parent_pid, int)


def test_load_harness_app_import_error_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-importable module path is fatal at boot.

    Per §Process management: misconfigurations should surface at
    spawn time as a non-zero exit, not as a connection refused
    on the first request. Verifies SystemExit(2) + a stderr
    message naming the bad module path.
    """
    with pytest.raises(SystemExit) as excinfo:
        _runner._load_harness_app("missing", "omnigent.does_not_exist", "conv_x")
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    # Catch a future regression where the loud-fail message gets
    # silenced or the module path gets dropped from it.
    assert "cannot import harness module" in err
    assert "'omnigent.does_not_exist'" in err


def test_load_harness_app_module_without_create_app_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A module that doesn't export create_app is fatal.

    Verifies the runner's structural check (``getattr(module,
    "create_app", None)``) catches the misnaming case loudly.
    Pointing the runner at a real module without ``create_app``
    (``omnigent.errors``) reproduces the failure mode.
    """
    with pytest.raises(SystemExit) as excinfo:
        _runner._load_harness_app("broken", "omnigent.errors", "conv_x")
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "does not export create_app" in err


def test_load_harness_app_loads_test_fixture() -> None:
    """A real module with create_app loads + stashes app state.

    Verifies the happy path: import → factory call →
    app.state.conversation_id + app.state.harness stash. The
    conversation id plumbing is the most fragile part of the
    runner contract (the design doc explicitly forbids parsing
    it from the socket path), so it gets a focused assertion.
    """
    app = _runner._load_harness_app(
        "test", "tests.unit.runtime.harnesses._test_harness", "conv_xyz"
    )
    # The fixture's create_app() returns a real FastAPI app — the
    # runner's job is to stash the conversation id on it. If this
    # fails, the harness can't scope its in-memory state per
    # §Harness in-memory state.
    assert app.state.conversation_id == "conv_xyz"
    # The harness name is also stashed for introspection /
    # logging — verifies the second app.state plumbing.
    assert app.state.harness == "test"
