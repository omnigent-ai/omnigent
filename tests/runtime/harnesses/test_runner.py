"""
Tests for the harness runner CLI argument parsing, module
resolution, and parent-death watchdog.

Spawn-and-serve is exercised by ``test_process_manager.py`` since
that requires actually waiting on uvicorn — covering the same
ground here would just duplicate.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

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
            "tests.runtime.harnesses._test_harness",
            "--socket",
            "/tmp/example.sock",
            "--conversation-id",
            "conv_abc",
        ]
    )
    assert ns.harness == "test"
    assert ns.module == "tests.runtime.harnesses._test_harness"
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
            "tests.runtime.harnesses._test_harness",
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
            "tests.runtime.harnesses._test_harness",
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
    app = _runner._load_harness_app("test", "tests.runtime.harnesses._test_harness", "conv_xyz")
    # The fixture's create_app() returns a real FastAPI app — the
    # runner's job is to stash the conversation id on it. If this
    # fails, the harness can't scope its in-memory state per
    # §Harness in-memory state.
    assert app.state.conversation_id == "conv_xyz"
    # The harness name is also stashed for introspection /
    # logging — verifies the second app.state plumbing.
    assert app.state.harness == "test"


def test_create_uvicorn_config_for_unix_socket() -> None:
    config = _runner._create_uvicorn_config(FastAPI(), "/tmp/runner.sock", None)

    assert config.uds == "/tmp/runner.sock"
    assert config.timeout_graceful_shutdown == _runner._GRACEFUL_SHUTDOWN_TIMEOUT_S
    assert config.log_level == _runner._UVICORN_LOG_LEVEL


def test_create_uvicorn_config_for_tcp_bind() -> None:
    config = _runner._create_uvicorn_config(FastAPI(), None, "127.0.0.1:8765")

    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert config.timeout_graceful_shutdown == _runner._GRACEFUL_SHUTDOWN_TIMEOUT_S
    assert config.log_level == _runner._UVICORN_LOG_LEVEL


def test_create_uvicorn_config_requires_endpoint() -> None:
    with pytest.raises(SystemExit, match="exactly one of --socket or --bind"):
        _runner._create_uvicorn_config(FastAPI(), None, None)


# ---------------------------------------------------------------------------
# Log destination: a harness child's logs must land in a readable file
# ---------------------------------------------------------------------------


def test_configure_logging_writes_to_the_parents_log_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the runner published a log path, harness lines join that file.

    Keeps a harness line next to the spawn that caused it, instead of in a
    separate file an operator has to know to look for.
    """
    import logging

    from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR

    parent_log = tmp_path / "runner.log"
    monkeypatch.setenv(PROCESS_LOG_FILE_ENV_VAR, str(parent_log))
    _reset_omnigent_log_handlers()

    _runner._configure_logging("acp", "conv_abc")
    logging.getLogger("omnigent.inner.acp_executor").warning("acp[Grok] boom")

    assert "acp[Grok] boom" in parent_log.read_text()


def test_configure_logging_falls_back_to_a_per_conversation_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no parent log path, the child still gets its own file."""
    import logging

    from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR

    monkeypatch.delenv(PROCESS_LOG_FILE_ENV_VAR, raising=False)
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    _reset_omnigent_log_handlers()

    _runner._configure_logging("acp", "conv_xyz")
    logging.getLogger("omnigent.inner.acp_executor").warning("acp[Grok] boom")

    written = list((tmp_path / "logs" / "harness").glob("acp-conv_xyz-*.log"))
    assert written, "expected a per-conversation harness log"
    assert "acp[Grok] boom" in written[0].read_text()


def test_configure_logging_survives_an_unwritable_log_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Logging is diagnostics: a bad log dir must not stop a harness serving."""
    from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR

    monkeypatch.delenv(PROCESS_LOG_FILE_ENV_VAR, raising=False)
    # A file where the logs/ directory needs to be: mkdir raises underneath.
    blocked = tmp_path / "data"
    blocked.write_text("not a directory")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(blocked))
    _reset_omnigent_log_handlers()

    _runner._configure_logging("acp", "conv_bad")  # must not raise


def _reset_omnigent_log_handlers() -> None:
    """Drop handlers a previous test attached, so each case asserts its own file."""
    import logging

    for name in ("", "omnigent", "uvicorn"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
