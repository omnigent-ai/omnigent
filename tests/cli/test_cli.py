"""Tests for omnigent.cli — bundle env var resolution."""

from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
import yaml
from click import ClickException
from click.testing import CliRunner, Result

from omnigent.cli import (
    _CLICK_SUBCOMMANDS,
    _GLOBAL_CONFIG_KEYS,
    _adopt_ambient_credentials,
    _announce_auto_configured_credentials,
    _bundle,
    _bundled_example_path,
    _dispatch_native_terminal_harness,
    _dispatch_run,
    _ensure_sqlite_parent_dir,
    _expand_config_env_vars,
    _extract_global_logging_flags,
    _HostHttpResult,
    _is_removed_ad_hoc_invocation,
    _is_run_shorthand,
    _load_global_config,
    _manage_goose_harness,
    _manage_kimi_harness,
    _manage_qwen_harness,
    _materialize_harness_launcher_file,
    _node_dependency_problem,
    _node_version,
    _pick_first_run_harness,
    _preregister_agent,
    _qwen_auth_configured,
    _resolve_auto_open_conversation_from_config,
    _resolve_auto_open_conversation_setting,
    _resolve_bundle_env_vars,
    _resolve_default_agent_target,
    _resolve_first_run_plan,
    _save_global_config,
    _server_uvicorn_log_config,
    _start_cli_runner_process,
    _warn_missing_harness_dependencies,
    cli,
)
from omnigent.errors import OmnigentError
from omnigent.onboarding.ambient import DetectedProvider
from omnigent.process_logging import (
    DEFAULT_LOG_DATEFMT,
    DEFAULT_LOG_FORMAT,
    DEFAULT_LOG_PREFIX_FORMAT,
)
from omnigent.runner.identity import (
    RUNNER_ID_ENV_VAR,
    RUNNER_PARENT_PID_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    RUNNER_WORKSPACE_ENV_VAR,
    token_bound_runner_id,
)
from omnigent.runner.transports.ws_tunnel.limits import (
    RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
    TUNNEL_KEEPALIVE_PING_INTERVAL_S,
    TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
)


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """
    Restore process-global logging mutations after each CLI test.

    Several CLI tests exercise the real entrypoint, which installs
    diagnostics handlers and sets package loggers to ``propagate=False``.
    Leaving that state behind makes later ``caplog`` assertions in
    other modules silently miss package warnings.

    :returns: A pytest finalizer implemented by yielding.
    """
    names = ("omnigent", "omnigent_ui_sdk", "httpx", "httpcore", "asyncio", "urllib3")
    snapshots = {}
    for name in names:
        logger = logging.getLogger(name)
        snapshots[name] = (logger.level, logger.propagate, tuple(logger.handlers))

    yield

    for name, (level, propagate, handlers) in snapshots.items():
        logger = logging.getLogger(name)
        original_handler_ids = {id(handler) for handler in handlers}
        for handler in list(logger.handlers):
            if id(handler) not in original_handler_ids:
                logger.removeHandler(handler)
                handler.close()
        logger.handlers = list(handlers)
        logger.setLevel(level)
        logger.propagate = propagate


def test_python_module_entrypoint_uses_unified_click_cli() -> None:
    """
    ``python -m omnigent`` must dispatch through the same click CLI
    as the installed ``omnigent`` console script.

    This catches ``omnigent/__main__.py`` pointing at the legacy
    argparse CLI, which bypasses the Omnigent REPL path. In that broken
    state ``python -m omnigent run ...`` opens the old ``>``
    prompt and loses AP-only input features such as slash-command
    autocomplete and bracketed-paste abstraction.
    """
    result = subprocess.run(
        [sys.executable, "-m", "omnigent", "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert "Usage: python -m omnigent [OPTIONS] COMMAND [ARGS]..." in result.stdout
    assert "Commands:" in result.stdout
    assert "run" in result.stdout and "Attach the REPL to a LIVE session" in result.stdout
    assert "Omnigent quick chat" not in result.stdout


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["run", "tests/resources/examples/hello_world.yaml"], False),
        (["attach", "tests/resources/examples/hello_world.yaml"], False),
        (["--help"], False),
        (["what does this repo do?"], True),
        (["--system-prompt", "You are terse"], True),
        # A single command-shaped word is an unknown subcommand, not
        # ad-hoc chat — it must fall through to click's "No such
        # command" handling rather than the ad-hoc removal notice.
        (["blah"], False),
    ],
)
def test_removed_ad_hoc_detection(argv: list[str], expected: bool) -> None:
    """
    Top-level prompt-shaped invocations no longer reach ``inner.cli``.

    :param argv: CLI arguments without program name.
    :param expected: Whether the arguments target the removed ad-hoc
        prompt shape.
    """
    assert _is_removed_ad_hoc_invocation(argv) is expected


def test_extract_global_logging_flags_preserves_run_shorthand() -> None:
    """Global logging flags are stripped before run-shorthand detection."""
    argv, debug, log_to_stderr = _extract_global_logging_flags(
        ["--debug", "--log-to-stderr", "--harness", "claude"]
    )

    assert argv == ["--harness", "claude"]
    assert debug is True
    assert log_to_stderr is True
    assert _is_run_shorthand(argv) is False


def test_extract_global_logging_flags_preserves_passthrough_args() -> None:
    """Logging flag names after ``--`` belong to the wrapped CLI."""
    argv, debug, log_to_stderr = _extract_global_logging_flags(
        ["claude", "--debug", "--", "--log-to-stderr", "--debug"]
    )

    assert argv == ["claude", "--", "--log-to-stderr", "--debug"]
    assert debug is True
    assert log_to_stderr is False


def test_server_uvicorn_log_config_uses_terminal_handler_when_requested(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Uvicorn logs mirror through the shared terminal handler on request."""
    monkeypatch.setattr("omnigent.process_logging.terminal_supports_color", lambda: True)

    log_config = _server_uvicorn_log_config(tmp_path / "server.log", log_to_stderr=True)

    assert log_config["handlers"]["server_terminal"]["()"] == (
        "omnigent.process_logging.terminal_stream_handler"
    )
    assert log_config["handlers"]["server_access_terminal"]["()"] == (
        "omnigent.process_logging.terminal_stream_handler"
    )
    assert log_config["handlers"]["server_terminal"]["formatter"] == "default"
    assert log_config["handlers"]["server_access_terminal"]["formatter"] == "access"
    assert log_config["handlers"]["server_file"]["formatter"] == "default_file"
    assert log_config["handlers"]["server_access_file"]["formatter"] == "access_file"
    assert log_config["loggers"]["uvicorn"]["handlers"] == [
        "server_terminal",
        "server_file",
    ]
    assert log_config["loggers"]["uvicorn.access"]["handlers"] == [
        "server_access_terminal",
        "server_access_file",
    ]


def test_server_uvicorn_log_config_mirrors_foreground_tty_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Foreground ``omnigent server`` keeps uvicorn access logs on stderr."""
    monkeypatch.delenv("OMNIGENT_LOG_TO_STDERR", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    log_config = _server_uvicorn_log_config(tmp_path / "server.log")

    assert log_config["loggers"]["uvicorn"]["handlers"] == [
        "server_terminal",
        "server_file",
    ]
    assert log_config["loggers"]["uvicorn.access"]["handlers"] == [
        "server_access_terminal",
        "server_access_file",
    ]


def test_server_uvicorn_log_config_keeps_noninteractive_default_file_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Spawned or redirected servers do not mirror uvicorn logs by default."""
    monkeypatch.delenv("OMNIGENT_LOG_TO_STDERR", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)

    log_config = _server_uvicorn_log_config(tmp_path / "server.log")

    assert "server_terminal" not in log_config["handlers"]
    assert "server_access_terminal" not in log_config["handlers"]
    assert log_config["loggers"]["uvicorn"]["handlers"] == ["server_file"]
    assert log_config["loggers"]["uvicorn.access"]["handlers"] == ["server_access_file"]


def test_server_uvicorn_log_config_standardizes_timestamp_and_color(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Uvicorn default and access logs include timestamps and terminal colors."""
    monkeypatch.setattr("omnigent.process_logging.terminal_supports_color", lambda: True)

    log_config = _server_uvicorn_log_config(tmp_path / "server.log", log_to_stderr=True)
    expected_access_format = (
        DEFAULT_LOG_PREFIX_FORMAT + '%(client_addr)s - "%(request_line)s" %(status_code)s'
    )

    assert log_config["formatters"]["default"]["fmt"] == DEFAULT_LOG_FORMAT
    assert log_config["formatters"]["default_file"]["fmt"] == DEFAULT_LOG_FORMAT
    assert log_config["formatters"]["access"]["fmt"] == expected_access_format
    assert log_config["formatters"]["access_file"]["fmt"] == expected_access_format
    for formatter in (
        log_config["formatters"]["default"],
        log_config["formatters"]["access"],
        log_config["formatters"]["default_file"],
        log_config["formatters"]["access_file"],
    ):
        assert formatter["datefmt"] == DEFAULT_LOG_DATEFMT

    assert log_config["formatters"]["default"]["use_colors"] is True
    assert log_config["formatters"]["access"]["use_colors"] is True
    assert log_config["formatters"]["default_file"]["use_colors"] is False
    assert log_config["formatters"]["access_file"]["use_colors"] is False


def test_debug_logs_runner_session_reads_new_and_legacy_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``debug logs --session`` finds canonical runner and legacy host-runner logs."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data_dir))
    runner_dir = data_dir / "logs" / "runner"
    legacy_dir = data_dir / "logs" / "host-runner"
    runner_dir.mkdir(parents=True)
    legacy_dir.mkdir(parents=True)
    (runner_dir / "runner-conv_abc-new.log").write_text("new\n", encoding="utf-8")
    (legacy_dir / "runner-conv_abc-old.log").write_text("old\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        ["debug", "logs", "--type", "runner", "--session", "conv_abc", "--list"],
    )

    assert result.exit_code == 0, result.output
    assert "runner-conv_abc-new.log" in result.output
    assert "runner-conv_abc-old.log" in result.output


def test_debug_logs_host_daemon_alias_reads_host_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy host-daemon type aliases to the canonical host destination."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data_dir))
    host_dir = data_dir / "logs" / "host"
    host_dir.mkdir(parents=True)
    (host_dir / "host-20260101-000000-000000.log").write_text("host log\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["debug", "logs", "--type", "host-daemon", "-n", "0"])

    assert result.exit_code == 0, result.output
    assert "host log" in result.output


def _fake_run_claude_native_capture(
    captured: dict[str, object],
) -> Callable[..., None]:
    """
    Build a ``run_claude_native`` stub that records its kwargs.

    Shared by the ``omnigent claude`` CLI parsing tests below so a
    signature change to ``run_claude_native`` (new kwarg, renamed
    kwarg) updates one place instead of every test.

    :param captured: Dict the stub writes recorded kwargs into.
    :returns: Stub callable that accepts arbitrary kwargs.
    """

    def _stub(**kwargs: object) -> None:
        """
        Capture parsed CLI arguments without launching Claude.

        :param kwargs: Whatever ``omnigent.claude_native.run_claude_native``
            is called with — accepted permissively so new kwargs
            (``resume_picker``, future flags) flow through to assertions
            without breaking the signature here.
        """
        captured.update(kwargs)

    return _stub


def _fake_run_codex_native_capture(
    captured: dict[str, object],
) -> Callable[..., None]:
    """
    Build a ``run_codex_native`` stub that records its kwargs.

    Shared by ``omnigent codex`` parsing tests so wrapper signature
    changes only update one helper.

    :param captured: Dict the stub writes recorded kwargs into.
    :returns: Stub callable that accepts arbitrary kwargs.
    """

    def _stub(**kwargs: object) -> None:
        """
        Capture parsed CLI arguments without launching Codex.

        :param kwargs: Whatever ``omnigent.codex_native.run_codex_native``
            is called with.
        """
        captured.update(kwargs)

    return _stub


def _fake_run_kiro_native_capture(
    captured: dict[str, object],
) -> Callable[..., None]:
    """Build a ``run_kiro_native`` stub that records its kwargs."""

    def _stub(**kwargs: object) -> None:
        captured.update(kwargs)

    return _stub


def test_claude_command_resume_binds_session_and_passes_unknown_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``omnigent claude --resume <conv_id>`` binds the Omnigent
    session; unknown args after ``--`` reach ``run_claude_native``
    as raw passthrough.

    The wrapper's defensive strip (``_strip_resume_from_claude_args``)
    runs INSIDE ``run_claude_native`` and is tested separately at
    ``tests/test_claude_native.py::test_strip_resume_from_claude_args_*``.
    This test mocks ``run_claude_native`` so it covers the Click
    parsing seam: ``--resume`` is consumed by Click, the post-``--``
    tokens land in ``claude_args`` raw, and the wrapper takes it from
    there.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native",
        _fake_run_claude_native_capture(captured),
    )

    result = CliRunner().invoke(
        cli,
        [
            "claude",
            "--server",
            "https://example.com",
            "--resume",
            "conv_abc",
            "--",
            "--resume",
            "claude-session",
            "-p",
            "say hi",
        ],
    )

    assert result.exit_code == 0, result.output
    # ``--resume conv_abc`` binds the Omnigent conv id; everything
    # post-``--`` reaches ``run_claude_native`` raw (the strip runs
    # there).
    assert captured["server"] == "https://example.com"
    assert captured["session_id"] == "conv_abc"
    assert captured["claude_args"] == ("--resume", "claude-session", "-p", "say hi")
    # No picker requested when ``--resume`` carries a value.
    assert captured["resume_picker"] is False
    # Default: Databricks auth is active (``--use-native-config`` not set) —
    # a True here means the configured provider would be silently skipped.
    assert captured["use_claude_config"] is False


def test_claude_command_short_r_binds_omnigent_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``omnigent claude -r <conv_id>`` is the Omnigent resume shortcut.

    With the unified ``--resume`` UX, ``-r`` is the Omnigent alias
    (not Claude's own short flag). Users who need Claude's own
    resume can rely on the wrapper to translate the Omnigent conv
    id internally — see ``omnigent.claude_native._resolve_cold_resume_args``
    for the cold-resume injection.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda *_: "http://localhost:0")
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native",
        _fake_run_claude_native_capture(captured),
    )

    result = CliRunner().invoke(cli, ["claude", "-r", "conv_abc"])

    assert result.exit_code == 0, result.output
    assert captured["session_id"] == "conv_abc"
    # ``-r <conv_id>`` consumes both tokens; no leftover claude args.
    assert captured["claude_args"] == ()
    assert captured["resume_picker"] is False


def test_claude_command_bare_resume_requests_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``omnigent claude --resume`` (no value) requests the picker.

    Bare ``--resume`` sets the picker sentinel, which the CLI
    translates into ``resume_picker=True`` for ``run_claude_native``.
    Critical: the Omnigent conv id MUST stay ``None`` so the
    wrapper actually runs the picker instead of binding to a
    bogus literal sentinel string.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda *_: "http://localhost:0")
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native",
        _fake_run_claude_native_capture(captured),
    )

    result = CliRunner().invoke(cli, ["claude", "--resume"])

    assert result.exit_code == 0, result.output
    assert captured["session_id"] is None
    assert captured["resume_picker"] is True


def test_claude_command_session_legacy_alias_routes_to_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``--session <id>`` is the legacy spelling kept around for one
    release. It must route into ``session_id`` exactly like
    ``--resume <id>``; mixing it with ``--resume`` is a usage
    error (mutually exclusive).
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda *_: "http://localhost:0")
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native",
        _fake_run_claude_native_capture(captured),
    )

    result = CliRunner().invoke(cli, ["claude", "--session", "conv_legacy"])

    assert result.exit_code == 0, result.output
    assert captured["session_id"] == "conv_legacy"
    assert captured["resume_picker"] is False


def test_claude_command_session_and_resume_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Passing ``--session`` and ``--resume`` together fails fast.

    Both spellings target the same kwarg; accepting both would
    silently let one win (and which one would depend on the
    implementation), which is exactly the kind of ambiguity the
    unified resume UX is trying to fix.
    """
    monkeypatch.setattr(
        "omnigent.cli._ensure_backend",
        lambda *_: pytest.fail("invalid args must not start the backend"),
    )

    result = CliRunner().invoke(
        cli,
        ["claude", "--session", "conv_a", "--resume", "conv_b"],
    )

    # ``UsageError`` translates to a non-zero exit at the Click layer.
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_claude_command_profile_startup_threads_profiler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``--profile-startup`` starts timing before backend setup.

    This covers the slow-start diagnostic path users need for
    ``omnigent claude``: the profiler must be created in the Click
    command, emit early marks, and be passed to ``run_claude_native``
    so native launch marks share the same timer.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr(
        "omnigent.cli._ensure_backend",
        lambda server: "https://example.com",
    )
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native",
        _fake_run_claude_native_capture(captured),
    )

    result = CliRunner().invoke(
        cli,
        ["claude", "--server", "https://example.com", "--profile-startup"],
    )

    assert result.exit_code == 0, result.output
    assert "cli entered" in result.stderr
    assert "ensuring backend" in result.stderr
    startup_profiler = captured["startup_profiler"]
    assert startup_profiler.enabled is True


def test_claude_command_use_native_config_bypasses_databricks_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``--use-native-config`` sets ``use_claude_config=True`` in ``run_claude_native``.

    Regression target: if the flag is dropped at the Click parsing
    seam, ``use_claude_config`` stays ``False`` and Databricks/ucode
    auth is injected even when the user explicitly opted out.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda *_: "http://localhost:0")
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native",
        _fake_run_claude_native_capture(captured),
    )

    result = CliRunner().invoke(cli, ["claude", "--use-native-config"])

    assert result.exit_code == 0, result.output
    # Flag must arrive as True — a False here means Click dropped it.
    assert captured["use_claude_config"] is True


def test_codex_command_resume_binds_session_and_passes_unknown_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``omnigent codex --resume <conv_id>`` binds the Omnigent
    session and preserves Codex CLI passthrough args after ``--``.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr(
        "omnigent.codex_native.run_codex_native",
        _fake_run_codex_native_capture(captured),
    )

    result = CliRunner().invoke(
        cli,
        [
            "codex",
            "--server",
            "https://example.com",
            "--resume",
            "conv_abc",
            "--model",
            "gpt-test",
            "-p",
            "say hi",
            "--",
            "-c",
            "approval_policy=on-request",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["server"] == "https://example.com"
    assert captured["session_id"] == "conv_abc"
    assert captured["codex_args"] == ("-c", "approval_policy=on-request")
    assert captured["model"] == "gpt-test"
    assert captured["prompt"] == "say hi"
    assert captured["resume_picker"] is False


def test_codex_command_bare_resume_requests_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``omnigent codex --resume`` requests the codex-native picker.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda *_: "http://localhost:0")
    monkeypatch.setattr(
        "omnigent.codex_native.run_codex_native",
        _fake_run_codex_native_capture(captured),
    )

    result = CliRunner().invoke(cli, ["codex", "--resume"])

    assert result.exit_code == 0, result.output
    assert captured["session_id"] is None
    assert captured["resume_picker"] is True


def test_codex_command_session_legacy_alias_routes_to_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``omnigent codex --session <id>`` routes into ``session_id``.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda *_: "http://localhost:0")
    monkeypatch.setattr(
        "omnigent.codex_native.run_codex_native",
        _fake_run_codex_native_capture(captured),
    )

    result = CliRunner().invoke(cli, ["codex", "--session", "conv_legacy"])

    assert result.exit_code == 0, result.output
    assert captured["session_id"] == "conv_legacy"
    assert captured["resume_picker"] is False


def test_codex_command_session_and_resume_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Passing ``--session`` and ``--resume`` together fails fast.
    """
    monkeypatch.setattr(
        "omnigent.cli._ensure_backend",
        lambda *_: pytest.fail("invalid args must not start the backend"),
    )

    result = CliRunner().invoke(
        cli,
        ["codex", "--session", "conv_a", "--resume", "conv_b"],
    )

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_kiro_command_is_registered_in_click_help() -> None:
    """``omnigent kiro`` is a true top-level Click command."""
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "kiro" in _CLICK_SUBCOMMANDS
    assert "kiro" in result.output


def test_kiro_command_parses_native_options_and_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``omnigent kiro`` routes mapped options to the native Kiro runner."""
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", lambda: {"server": "https://cfg"})
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda server: server)
    monkeypatch.setattr(
        "omnigent.kiro_native.run_kiro_native",
        _fake_run_kiro_native_capture(captured),
    )

    result = CliRunner().invoke(
        cli,
        [
            "kiro",
            "--model",
            "auto",
            "--effort",
            "high",
            "--agent",
            "dev",
            "--trust-tools",
            "Read",
            "--trust-all-tools",
            "-p",
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["server"] == "https://cfg"
    assert captured["session_id"] is None
    assert captured["resume_picker"] is False
    assert captured["model"] == "auto"
    assert captured["prompt"] == "hi"
    assert captured["kiro_args"] == (
        "--effort",
        "high",
        "--agent",
        "dev",
        "--trust-tools",
        "Read",
        "--trust-all-tools",
    )


def test_kiro_command_bare_resume_requests_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    """``omnigent kiro --resume`` requests the Kiro-native picker."""
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda *_: "http://localhost:0")
    monkeypatch.setattr(
        "omnigent.kiro_native.run_kiro_native",
        _fake_run_kiro_native_capture(captured),
    )

    result = CliRunner().invoke(cli, ["kiro", "--resume"])

    assert result.exit_code == 0, result.output
    assert captured["session_id"] is None
    assert captured["resume_picker"] is True


def test_kiro_command_session_and_resume_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid Kiro resume inputs fail before backend side effects."""
    monkeypatch.setattr(
        "omnigent.cli._ensure_backend",
        lambda *_: pytest.fail("invalid args must not start the backend"),
    )

    result = CliRunner().invoke(cli, ["kiro", "--session", "conv_a", "--resume", "conv_b"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_kiro_command_rejects_kiro_resume_passthrough_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiro-owned resume flags are reserved for internal cold-resume mapping."""
    monkeypatch.setattr(
        "omnigent.cli._ensure_backend",
        lambda *_: pytest.fail("invalid args must not start the backend"),
    )

    result = CliRunner().invoke(cli, ["kiro", "--", "--resume-id", "kiro-session"])

    assert result.exit_code != 0
    assert "Kiro resume flags are reserved" in result.output


# ── bundled-agent shorthands (omnigent polly / omnigent debby) ──────────


def _invoke_bundled_agent_command(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> tuple[Result, Mock]:
    """Invoke a bundled-agent shorthand with ``run``'s dispatcher mocked.

    Stubs ``_load_effective_config`` (no developer-machine config leakage)
    and ``_dispatch_run`` (no server/daemon side effects), so the test
    observes exactly what the forwarded ``run`` invocation would launch.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param args: Full CLI argv, e.g. ``["polly", "-p", "hi"]``.
    :returns: The Click invocation result and the ``_dispatch_run`` mock.
    """
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    dispatch = Mock()
    monkeypatch.setattr("omnigent.cli._dispatch_run", dispatch)
    result = CliRunner().invoke(cli, args)
    return result, dispatch


def test_polly_command_runs_bundled_polly_and_forwards_run_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``omnigent polly`` dispatches ``run`` on the packaged polly agent.

    The shorthand must target the same bundled polly directory the bare
    ``omnigent`` first-run plan uses, and pass-through ``run`` flags
    (``-p``, ``--model``) must survive the forwarding unchanged.
    """
    result, dispatch = _invoke_bundled_agent_command(
        monkeypatch, ["polly", "-p", "review the last commit", "--model", "m1"]
    )

    assert result.exit_code == 0, result.output
    dispatch.assert_called_once()
    kwargs = dispatch.call_args.kwargs
    assert kwargs["target"] == _bundled_example_path("polly")
    assert kwargs["prompt"] == "review the last commit"
    assert kwargs["model"] == "m1"
    # The resume replay prefix must be the canonical (re-runnable) run form,
    # not "omnigent polly <path>" which would parse the path as a 2nd target.
    assert kwargs["resume_parts"][:3] == ["omnigent", "run", _bundled_example_path("polly")]


def test_debby_command_runs_bundled_debby(monkeypatch: pytest.MonkeyPatch) -> None:
    """``omnigent debby`` dispatches ``run`` on the packaged debby agent."""
    result, dispatch = _invoke_bundled_agent_command(monkeypatch, ["debby"])

    assert result.exit_code == 0, result.output
    dispatch.assert_called_once()
    assert dispatch.call_args.kwargs["target"] == _bundled_example_path("debby")


def test_bundled_agent_command_rejects_extra_positional_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray positional after the shorthand is a usage error, not a launch.

    ``run`` takes a single AGENT positional which the shorthand already
    supplies (the bundled path); a second one must fail loudly rather than
    silently launching the wrong agent.
    """
    result, dispatch = _invoke_bundled_agent_command(monkeypatch, ["polly", "other_agent.yaml"])

    assert result.exit_code != 0
    dispatch.assert_not_called()


def test_first_run_plan_and_polly_command_agree_on_bundled_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare ``omnigent`` (Claude creds) and ``omnigent polly`` launch the SAME agent.

    Pins the "same thing as bare ``omni``" contract: the first-run plan's
    default agent and the polly shorthand resolve to one bundled directory.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.default_provider_for_harness",
        _fake_provider_for("claude-sdk"),
    )
    plan = _pick_first_run_harness()
    assert plan is not None
    assert plan.agent == _bundled_example_path("polly")


def _write_isolated_provider_config(
    config_home: Path,
    providers: dict[str, object],
) -> Path:
    """Write an isolated ``~/.omnigent/config.yaml`` with *providers*.

    :param config_home: Directory to use as ``$OMNIGENT_CONFIG_HOME``.
    :param providers: The raw ``providers:`` mapping to write.
    :returns: The written config file path.
    """
    config_home.mkdir(parents=True, exist_ok=True)
    config_path = config_home / "config.yaml"
    config_path.write_text(yaml.safe_dump({"providers": providers}))
    return config_path


@pytest.mark.parametrize("shorthand", ["polly", "debby"])
def test_bundled_agent_launches_with_first_available_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    shorthand: str,
) -> None:
    """Polly/Debby launch with the first available credential (#334).

    With a Claude (``anthropic``) credential configured but NOT marked
    ``default: true``, the shorthand must mark it the default for the
    brain's family and proceed to launch — rather than requiring the user
    to pick/configure a specific default up front. Both shorthands share
    the ``_run_bundled_agent`` path and the ``claude-sdk`` brain, so the
    fallback fires identically for each.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    # No ambient credentials — the explicit provider below is the only one.
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    _write_isolated_provider_config(
        tmp_path,
        {
            "anthropic_key": {
                "kind": "key",
                "anthropic": {
                    "base_url": "https://api.anthropic.invalid/v1",
                    "api_key_ref": "env:ANTHROPIC_KEY",
                },
            }
        },
    )
    dispatch = Mock()
    monkeypatch.setattr("omnigent.cli._dispatch_run", dispatch)

    result = CliRunner().invoke(cli, [shorthand])

    assert result.exit_code == 0, result.output
    # The first available credential was promoted to the anthropic default.
    # A single-family (anthropic-only) provider renders its default as the
    # compact `True` form — the whole served set, per _default_raw_value.
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    provider = saved["providers"]["anthropic_key"]
    assert provider.get("default") is True
    # The silent config mutation is announced (mirrors setup / /model).
    assert (
        "No default Claude credential set — using Anthropic Key API Key and saving "
        "it as the default (change anytime with: omnigent /model)." in result.output
    )
    assert "credentials found" not in result.output
    # And the launch proceeded (the brain credential resolved).
    dispatch.assert_called_once()
    assert dispatch.call_args.kwargs["target"] == _bundled_example_path(shorthand)


def test_bundled_agent_multiple_credentials_notice_preserves_first_pick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Multiple matching credentials still launch non-interactively with context.

    When two Claude-family credentials exist and neither is default, the
    shorthand remains headless-safe: it promotes the first configured
    credential, reports the ambiguity, and tells the user where to change it.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)

    def _fail_prompt(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bundled launch must not prompt")

    monkeypatch.setattr("omnigent.cli.click.prompt", _fail_prompt)
    monkeypatch.setattr("omnigent.cli.click.confirm", _fail_prompt)
    _write_isolated_provider_config(
        tmp_path,
        {
            "anthropic_first": {
                "kind": "key",
                "anthropic": {
                    "base_url": "https://api.anthropic.invalid/v1",
                    "api_key_ref": "env:ANTHROPIC_FIRST",
                },
            },
            "anthropic_second": {
                "kind": "key",
                "anthropic": {
                    "base_url": "https://api.anthropic.invalid/v1",
                    "api_key_ref": "env:ANTHROPIC_SECOND",
                },
            },
        },
    )
    dispatch = Mock()
    monkeypatch.setattr("omnigent.cli._dispatch_run", dispatch)

    result = CliRunner().invoke(cli, ["polly"])

    assert result.exit_code == 0, result.output
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    providers = saved["providers"]
    assert providers["anthropic_first"].get("default") is True
    assert "default" not in providers["anthropic_second"]
    assert (
        "No default Claude credential set — using Anthropic First API Key "
        "(2 Claude credentials found; pick another with: omnigent /model) "
        "and saving it as the default." in result.output
    )
    dispatch.assert_called_once()
    assert dispatch.call_args.kwargs["target"] == _bundled_example_path("polly")


def test_bundled_agent_leaves_existing_default_credential_alone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An existing explicit default is not re-written on bundled launch (#334).

    When a ``default: true`` credential is already configured for the
    brain's family, the shorthand must NOT touch the config — the fallback
    only fires when no default exists.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    config_path = _write_isolated_provider_config(
        tmp_path,
        {
            "anthropic_key": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.invalid/v1",
                    "api_key_ref": "env:ANTHROPIC_KEY",
                },
            }
        },
    )
    before = config_path.read_text()
    dispatch = Mock()
    monkeypatch.setattr("omnigent.cli._dispatch_run", dispatch)

    result = CliRunner().invoke(cli, ["polly"])

    assert result.exit_code == 0, result.output
    assert config_path.read_text() == before  # unchanged — default already set
    dispatch.assert_called_once()


def test_bundled_agent_no_credential_does_not_write_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No available credential → no config write; the launch still dispatches (#334).

    When nothing serves the brain's family, the shorthand must not fabricate
    a default (the harness raises its own launch error downstream). The
    config is left untouched and ``run`` is still forwarded so that error
    surfaces in the normal launch path rather than as a silent no-op.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    # An OpenAI-only credential — the claude-sdk brain needs anthropic.
    config_path = _write_isolated_provider_config(
        tmp_path,
        {
            "openai_key": {
                "kind": "key",
                "openai": {
                    "base_url": "https://api.openai.invalid/v1",
                    "api_key_ref": "env:OPENAI_KEY",
                },
            }
        },
    )
    before = config_path.read_text()
    dispatch = Mock()
    monkeypatch.setattr("omnigent.cli._dispatch_run", dispatch)

    result = CliRunner().invoke(cli, ["polly"])

    assert result.exit_code == 0, result.output
    assert config_path.read_text() == before  # no default fabricated
    dispatch.assert_called_once()


def test_bundled_agent_unreadable_global_config_degrades_to_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A corrupt on-disk config degrades to launch, never crashes (#334).

    The fallback reads the on-disk providers via the non-forgiving
    ``_load_global_config`` to decide what to persist. If that read blows up
    on a malformed/corrupt config, the bundled launch must degrade to a no-op
    (letting the harness raise its own credential error) rather than crashing
    before the harness ever runs — the exact failure mode this path exists to
    avoid. An explicit anthropic key keeps the fallback loop reaching that read.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    _write_isolated_provider_config(
        tmp_path,
        {
            "anthropic_key": {
                "kind": "key",
                "anthropic": {
                    "base_url": "https://api.anthropic.invalid/v1",
                    "api_key_ref": "env:ANTHROPIC_KEY",
                },
            }
        },
    )

    def _corrupt() -> dict[str, object]:
        raise yaml.YAMLError("corrupt global config")

    monkeypatch.setattr("omnigent.cli._load_global_config", _corrupt)
    dispatch = Mock()
    monkeypatch.setattr("omnigent.cli._dispatch_run", dispatch)

    result = CliRunner().invoke(cli, ["polly"])

    # Degrades cleanly: no traceback, and the launch still dispatches so the
    # harness surfaces any credential error through the normal path.
    assert result.exit_code == 0, result.output
    assert result.exception is None, result.exception
    dispatch.assert_called_once()


def test_bundled_agent_ambiguous_default_config_degrades_to_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An ambiguous config (two defaults for one family) degrades, never crashes.

    The fallback touches the config through several readers
    (``effective_config_with_detected``, ``default_provider_for_harness``,
    ``set_default_provider``), any of which raise ``OmnigentError`` on a
    malformed/ambiguous config. Here two anthropic providers are both marked
    ``default: true`` — which ``get_default_provider`` rejects — so the read
    raises before the loop. The bundled launch must swallow it and dispatch,
    letting the harness raise its own credential error, rather than crashing
    with a traceback. Regression for the narrow guard that caught only the
    ``_load_global_config`` read.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    _write_isolated_provider_config(
        tmp_path,
        {
            "anthropic_a": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.invalid/v1",
                    "api_key_ref": "env:ANTHROPIC_A",
                },
            },
            "anthropic_b": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.invalid/v1",
                    "api_key_ref": "env:ANTHROPIC_B",
                },
            },
        },
    )
    dispatch = Mock()
    monkeypatch.setattr("omnigent.cli._dispatch_run", dispatch)

    result = CliRunner().invoke(cli, ["polly"])

    assert result.exit_code == 0, result.output
    assert result.exception is None, result.exception
    dispatch.assert_called_once()


def test_start_cli_runner_process_uses_token_bound_runner_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Authenticated remote runners advertise the tunnel-token-bound id.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory fixture used as the runner
        workspace root.
    :returns: None.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr("omnigent.cli.secrets.token_urlsafe", lambda _size: "bind-token")

    class _Proc:
        """Subprocess stub returned by ``subprocess.Popen``.

        :param args: Command line passed to ``Popen``.
        :param env: Environment passed to ``Popen``.
        :param _kwargs: Remaining Popen kwargs (e.g. ``stdout``/
            ``stderr`` when ``capture_logs=True``); absorbed.
        """

        returncode: int | None = None

        def __init__(self, args: list[str], *, env: dict[str, str], **_kwargs: object) -> None:
            captured["args"] = args
            captured["env"] = env

        def poll(self) -> None:
            """Report the runner process as still alive.

            :returns: ``None``.
            """
            return

    monkeypatch.setattr("omnigent.cli.subprocess.Popen", _Proc)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = _start_cli_runner_process(
        server_url="https://example.databricksapps.com",
        workspace_cwd=workspace,
    )

    expected_runner_id = token_bound_runner_id("bind-token")
    assert runner.runner_id == expected_runner_id
    assert runner.tunnel_token == "bind-token"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env[RUNNER_ID_ENV_VAR] == expected_runner_id
    assert "OMNIGENT_RUNNER_TUNNEL_TOKEN" not in env
    assert env[RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR] == "bind-token"
    assert env[RUNNER_PARENT_PID_ENV_VAR] == str(os.getpid())
    assert env[RUNNER_WORKSPACE_ENV_VAR] == str(workspace.resolve())


def test_start_cli_runner_process_binds_stable_local_runner_to_generated_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local server runners keep stable identity and use token auth.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    captured: dict[str, object] = {}
    monkeypatch.delenv("OMNIGENT_RUNNER_TUNNEL_TOKEN", raising=False)
    monkeypatch.setattr(
        "omnigent.cli.secrets.token_urlsafe",
        lambda _size: "local-bind-token",
    )

    class _Proc:
        """Subprocess stub returned by ``subprocess.Popen``.

        :param args: Command line passed to ``Popen``.
        :param env: Environment passed to ``Popen``.
        :param _kwargs: Remaining Popen kwargs (e.g. ``stdout``/
            ``stderr`` when ``capture_logs=True``); absorbed.
        """

        returncode: int | None = None

        def __init__(self, args: list[str], *, env: dict[str, str], **_kwargs: object) -> None:
            captured["args"] = args
            captured["env"] = env

        def poll(self) -> None:
            """Report the runner process as still alive.

            :returns: ``None``.
            """
            return

    monkeypatch.setattr("omnigent.cli.subprocess.Popen", _Proc)

    runner = _start_cli_runner_process(
        server_url="http://127.0.0.1:8000",
        runner_id="runner_local_stable",
    )

    assert runner.runner_id == "runner_local_stable"
    assert runner.tunnel_token == "local-bind-token"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["RUNNER_SERVER_URL"] == "http://127.0.0.1:8000"
    assert env[RUNNER_ID_ENV_VAR] == "runner_local_stable"
    assert env[RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR] == "local-bind-token"
    assert env[RUNNER_PARENT_PID_ENV_VAR] == str(os.getpid())
    assert "OMNIGENT_RUNNER_TUNNEL_TOKEN" not in env


def test_start_cli_runner_process_reports_captured_log_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runner startup failure points users at the captured log file.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """

    class _ExitedProc:
        """Subprocess stub that exits before startup completes.

        :param args: Command line passed to ``Popen``.
        :param env: Environment passed to ``Popen``.
        :param kwargs: Remaining ``Popen`` keyword args.
        """

        returncode = 17

        def __init__(
            self,
            args: list[str],
            *,
            env: dict[str, str],
            **kwargs: object,
        ) -> None:
            del args, env, kwargs

        def poll(self) -> int:
            """Report the runner process as already exited.

            :returns: Exit code, e.g. ``17``.
            """
            return 17

    monkeypatch.setattr("omnigent.cli.subprocess.Popen", _ExitedProc)
    log_dir = tmp_path / "logs"

    with pytest.raises(ClickException) as excinfo:
        _start_cli_runner_process(
            server_url="http://127.0.0.1:8000",
            runner_id="runner_exited",
            capture_logs=True,
            log_dir=log_dir,
        )

    assert "Runner process exited early with code 17" in excinfo.value.message
    assert "Runner log:" in excinfo.value.message
    assert str(tmp_path / "logs" / "runner") in excinfo.value.message
    assert "runner-" in excinfo.value.message
    assert ".log" in excinfo.value.message


def test_server_command_reads_tunnel_token_and_does_not_spawn_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``omnigent server`` is a pure state server — no embedded runner.

    The server reads ``OMNIGENT_RUNNER_TUNNEL_TOKEN`` from the
    environment and passes it as ``runner_tunnel_tokens`` to
    ``create_app`` so the caller (``_start_local_server``) can spawn
    a sibling runner whose tunnel the server accepts. Without the env
    var, ``runner_tunnel_tokens`` is ``None`` (accept any token —
    the deployed-server posture).

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test data directory for the SQLite store.
    :returns: None.
    """
    import uvicorn
    import uvicorn.server

    captured: dict[str, Any] = {}

    _original_create_app = None

    def _spy_create_app(**kwargs: Any) -> Any:
        """Capture create_app kwargs and delegate to the real function.

        :param kwargs: Forwarded keyword arguments from ``server``.
        :returns: The real FastAPI app.
        """
        captured["create_app_kwargs"] = kwargs
        return _original_create_app(**kwargs)

    def _fake_server_run(self: Any) -> None:
        """Skip the blocking server loop; capture config as flat kwargs dict.

        :param self: The uvicorn Server instance whose config holds all options.
        :returns: None.
        """
        captured["uvicorn_kwargs"] = {
            "ws_max_size": self.config.ws_max_size,
            "ws_ping_interval": self.config.ws_ping_interval,
            "ws_ping_timeout": self.config.ws_ping_timeout,
            "log_config": self.config.log_config,
            "port": self.config.port,
            "host": self.config.host,
        }
        captured["uvicorn_called"] = True

    from omnigent.server import app as app_module

    _original_create_app = app_module.create_app
    monkeypatch.setattr(app_module, "create_app", _spy_create_app)
    monkeypatch.setattr(uvicorn.server.Server, "run", _fake_server_run)
    monkeypatch.setenv("OMNIGENT_RUNNER_TUNNEL_TOKEN", "test-tunnel-token-abc")

    # On a loopback bind the `server` command reuses an already-running
    # local server (and registers itself in ~/.omnigent/local_server.pid).
    # Pin the unified-server helpers so the test exercises the spawn path
    # deterministically: no pre-existing server to reuse, the requested port
    # is taken as-is, and we don't touch the developer's real pidfile.
    from omnigent.host import local_server as _local_server_mod

    monkeypatch.setattr(_local_server_mod, "local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr(_local_server_mod, "pick_local_port", lambda preferred: preferred)
    monkeypatch.setattr(_local_server_mod, "register_local_server", lambda port: None)
    monkeypatch.setattr(_local_server_mod, "clear_local_server_record", lambda: None)

    db_path = tmp_path / "chat.db"
    artifact_dir = tmp_path / "artifacts"

    result = CliRunner().invoke(
        cli,
        [
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            "9999",
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured.get("uvicorn_called") is True
    assert captured["uvicorn_kwargs"]["ws_max_size"] == RUNNER_TUNNEL_MAX_MESSAGE_BYTES
    # Tunnel protocol keepalive aligned to the 90s app-level budget, not uvicorn's
    # 20s default that drops a busy-but-healthy tunnel with 1011 (issue #1116).
    assert captured["uvicorn_kwargs"]["ws_ping_interval"] == TUNNEL_KEEPALIVE_PING_INTERVAL_S
    assert captured["uvicorn_kwargs"]["ws_ping_timeout"] == TUNNEL_KEEPALIVE_PING_TIMEOUT_S
    assert (
        captured["uvicorn_kwargs"]["log_config"]["formatters"]["access"]["()"]
        == "omnigent.server.performance_metrics.RequestDurationAccessFormatter"
    )
    assert captured["create_app_kwargs"]["runner_tunnel_tokens"] == frozenset(
        {"test-tunnel-token-abc"}
    )


def test_server_with_explicit_db_does_not_reuse_canonical_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A `server` with explicit --database-uri binds its own port, never reuses.

    Regression: the unified machine-global lifecycle (reuse a running
    canonical server via ~/.omnigent/local_server.pid) must apply ONLY
    to a *bare* loopback ``omnigent server``. The daemon and the e2e
    harness spawn DEDICATED servers with explicit --database-uri /
    --artifact-location / --port; if the reuse-check fired for them, a
    healthy canonical server on a *different* DB would make the dedicated
    spawn print "already running — reusing it" and exit WITHOUT binding
    its port, so the caller's "server failed to start". Here we assert
    that with a healthy canonical server present (stubbed), an explicit-DB
    invocation still starts uvicorn on its own port and never touches the
    shared pidfile.
    """
    import uvicorn
    import uvicorn.server

    captured: dict[str, Any] = {}
    _original_create_app = None

    def _spy_create_app(**kwargs: Any) -> Any:
        """Capture kwargs and delegate to the real create_app.

        :param kwargs: Forwarded keyword arguments from ``server``.
        :returns: The real FastAPI app.
        """
        captured["create_app_kwargs"] = kwargs
        return _original_create_app(**kwargs)

    def _fake_server_run(self: Any) -> None:
        """Skip the blocking server loop, record that it was called.

        :param self: The uvicorn Server instance.
        :returns: None.
        """
        captured["uvicorn_kwargs"] = {"port": self.config.port}
        captured["uvicorn_called"] = True

    from omnigent.server import app as app_module

    _original_create_app = app_module.create_app
    monkeypatch.setattr(app_module, "create_app", _spy_create_app)
    monkeypatch.setattr(uvicorn.server.Server, "run", _fake_server_run)

    # A healthy canonical server EXISTS. A bare `omnigent server` would
    # reuse it; an explicit-DB server must ignore it. register/clear must
    # never fire for the dedicated server (it doesn't own the pidfile).
    from omnigent.host import local_server as _local_server_mod

    monkeypatch.setattr(
        _local_server_mod, "local_server_url_if_healthy", lambda: "http://127.0.0.1:39811"
    )

    def _must_not_register(_port: int) -> None:
        raise AssertionError("explicit-DB server must not register in the shared pidfile")

    monkeypatch.setattr(_local_server_mod, "register_local_server", _must_not_register)

    db_path = tmp_path / "chat.db"
    result = CliRunner().invoke(
        cli,
        [
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            "44769",
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
            "--no-open",
        ],
    )

    assert result.exit_code == 0, result.output
    # Did NOT bail with "already running"; bound its own explicit port.
    assert "already running" not in result.output
    assert captured.get("uvicorn_called") is True
    assert captured["uvicorn_kwargs"]["port"] == 44769


def test_server_with_explicit_port_does_not_check_canonical_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An explicit ``--port`` starts a dedicated local server.

    A healthy canonical local server on another port must not make
    ``omnigent server --port <new>`` reuse and exit. Explicit port
    selection is the user asking for another listener, while the
    canonical local-server reuse path is only for bare
    ``omnigent server``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test data directory for default server state.
    :returns: None.
    """
    import uvicorn
    import uvicorn.server

    captured: dict[str, Any] = {}

    def _fake_server_run(self: Any) -> None:
        """
        Skip the blocking server loop.

        :param self: The uvicorn Server instance.
        :returns: None.
        """
        captured["uvicorn_kwargs"] = {"port": self.config.port}

    def _must_not_check_existing() -> str | None:
        """
        Fail if the explicit-port path consults the canonical server record.

        :returns: Never returns in this test.
        """
        raise AssertionError("explicit --port must not check the canonical local server")

    def _must_not_touch_pidfile(_port: int | None = None) -> None:
        """
        Fail if the explicit-port path mutates the canonical server record.

        :param _port: Optional port argument accepted for register calls.
        :returns: Never returns in this test.
        """
        raise AssertionError("explicit --port must not touch the shared pidfile")

    from omnigent.host import local_server as _local_server_mod

    monkeypatch.setattr(uvicorn.server.Server, "run", _fake_server_run)
    monkeypatch.setattr(_local_server_mod, "local_server_url_if_healthy", _must_not_check_existing)
    monkeypatch.setattr(_local_server_mod, "register_local_server", _must_not_touch_pidfile)
    monkeypatch.setattr(_local_server_mod, "clear_local_server_record", _must_not_touch_pidfile)
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))

    result = CliRunner().invoke(
        cli,
        [
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            "44770",
            "--no-open",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "already running" not in result.output
    assert captured["uvicorn_kwargs"]["port"] == 44770
    assert "Starting omnigent server on 127.0.0.1:44770" in result.output


def test_server_command_explicit_occupied_port_fails() -> None:
    """
    An explicit ``--port`` must fail instead of choosing a replacement.

    The test owns a real listening socket on a kernel-assigned port,
    then asks ``omnigent server`` for that exact port. The command
    must fail during preflight, before uvicorn or fallback-port logic
    can start the server elsewhere.

    :returns: None.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])

        result = CliRunner().invoke(
            cli,
            [
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
        )

    assert result.exit_code != 0
    assert f"Cannot start server on 127.0.0.1:{port}" in result.output
    assert "port is unavailable" in result.output
    assert "using" not in result.output
    assert "Starting omnigent server" not in result.output


def test_server_command_explicit_port_uses_bind_probe_not_connect_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A refused client connection does not make an explicit port unavailable.

    A connect-based probe would classify this free port as unusable
    because the pre-test client connection is refused. The CLI should
    use a bind probe instead, continue startup, and pass the requested
    port to uvicorn unchanged.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test data directory for the SQLite store.
    :returns: None.
    """
    import socket

    import uvicorn
    import uvicorn.server

    captured: dict[str, Any] = {}

    def _fake_server_run(self: Any) -> None:
        """
        Skip the blocking server loop.

        :param self: The uvicorn Server instance.
        :returns: None.
        """
        captured["uvicorn_kwargs"] = {"port": self.config.port}

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.01)

    monkeypatch.setattr(uvicorn.server.Server, "run", _fake_server_run)
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")

    db_path = tmp_path / "chat.db"
    artifact_dir = tmp_path / "artifacts"

    result = CliRunner().invoke(
        cli,
        [
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["uvicorn_kwargs"]["port"] == port
    assert f"Starting omnigent server on 127.0.0.1:{port}" in result.output
    assert "port is unavailable" not in result.output


def _write_config(
    agent_dir: Path,
    config: dict[str, Any],
) -> None:
    """
    Write a config.yaml to the agent directory.

    :param agent_dir: The agent image directory.
    :param config: The config dict to serialize.
    """
    (agent_dir / "config.yaml").write_text(
        yaml.dump(config, default_flow_style=False),
    )


def _write_mcp_config(
    agent_dir: Path,
    name: str,
    config: dict[str, Any],
) -> None:
    """
    Write an MCP server YAML file under tools/mcp/.

    :param agent_dir: The agent image directory.
    :param name: The MCP config filename (without .yaml).
    :param config: The MCP config dict to serialize.
    """
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / f"{name}.yaml").write_text(
        yaml.dump(config, default_flow_style=False),
    )


# ── _expand_config_env_vars ──────────────────────────


def test_expand_config_expands_llm_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_expand_config_env_vars`` resolves ``${VAR}`` in
    ``llm.connection`` values.
    """
    monkeypatch.setenv("TEST_API_KEY", "sk-resolved-123")
    from omnigent.spec import expand_env_vars

    raw: dict[str, Any] = {
        "spec_version": 1,
        "llm": {
            "model": "gpt-5.4",
            "connection": {"api_key": "${TEST_API_KEY}"},
        },
    }
    changed = _expand_config_env_vars(raw, expand_env_vars)

    assert changed is True
    # The resolved value should replace the ${VAR} reference.
    assert raw["llm"]["connection"]["api_key"] == "sk-resolved-123", (
        "llm.connection.api_key should be expanded from env var"
    )


def test_expand_config_expands_builtin_tool_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_expand_config_env_vars`` resolves ``${VAR}`` in
    ``tools.builtins`` dict-entry config fields.
    """
    monkeypatch.setenv("PPLX_KEY", "pplx-resolved")
    from omnigent.spec import expand_env_vars

    raw: dict[str, Any] = {
        "spec_version": 1,
        "tools": {
            "builtins": [
                "web_search",
                {"name": "web_search_pplx", "api_key": "${PPLX_KEY}"},
            ],
        },
    }
    changed = _expand_config_env_vars(raw, expand_env_vars)

    assert changed is True
    # String entries are untouched.
    assert raw["tools"]["builtins"][0] == "web_search"
    # Dict entry api_key should be expanded.
    entry = raw["tools"]["builtins"][1]
    assert entry["api_key"] == "pplx-resolved", (
        "builtin tool api_key should be expanded from env var"
    )
    # 'name' is preserved.
    assert entry["name"] == "web_search_pplx"


def test_expand_config_expands_executor_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_expand_config_env_vars`` resolves ``${VAR}`` in
    ``executor.connection`` values.

    The server no longer expands uploaded
    bundles, so the client must resolve ``executor.connection`` (not
    just ``llm.connection``) or local ``omnigent run`` specs using the
    consolidated executor block would ship unresolved ``${VAR}``.
    """
    monkeypatch.setenv("EXEC_API_KEY", "sk-exec-999")
    from omnigent.spec import expand_env_vars

    raw: dict[str, Any] = {
        "spec_version": 1,
        "executor": {
            "type": "omnigent",
            "model": "gpt-5.4-mini",
            "connection": {"api_key": "${EXEC_API_KEY}"},
            "config": {"harness": "openai-agents"},
        },
    }
    changed = _expand_config_env_vars(raw, expand_env_vars)

    assert changed is True
    # If this is still "${EXEC_API_KEY}", the executor.connection branch
    # was not wired and the secret would ship unresolved to the server.
    assert raw["executor"]["connection"]["api_key"] == "sk-exec-999"


def test_expand_config_expands_executor_auth_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_expand_config_env_vars`` resolves ``${VAR}`` in
    ``executor.auth.api_key`` / ``base_url`` when ``type: api_key``.

    Mirrors ``_parse_executor_auth``'s server-side expansion sites.
    """
    monkeypatch.setenv("AUTH_KEY", "sk-auth-7")
    monkeypatch.setenv("AUTH_URL", "https://llm.example.invalid/v1")
    from omnigent.spec import expand_env_vars

    raw: dict[str, Any] = {
        "spec_version": 1,
        "executor": {
            "type": "omnigent",
            "auth": {
                "type": "api_key",
                "api_key": "${AUTH_KEY}",
                "base_url": "${AUTH_URL}",
            },
        },
    }
    changed = _expand_config_env_vars(raw, expand_env_vars)

    assert changed is True
    assert raw["executor"]["auth"]["api_key"] == "sk-auth-7"
    assert raw["executor"]["auth"]["base_url"] == "https://llm.example.invalid/v1"
    # 'type' is preserved (not a secret-bearing field).
    assert raw["executor"]["auth"]["type"] == "api_key"


def test_expand_config_leaves_databricks_auth_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``executor.auth`` with a non-``api_key`` type is not expanded.

    A ``type: databricks`` auth block carries a profile name, not a
    secret value — expanding it would be wrong (and there is no
    ``api_key`` to resolve). ``changed`` stays ``False`` so the file
    is bundled as-is.
    """
    from omnigent.spec import expand_env_vars

    raw: dict[str, Any] = {
        "spec_version": 1,
        "executor": {
            "type": "omnigent",
            "auth": {"type": "databricks", "profile": "my-profile"},
        },
    }
    changed = _expand_config_env_vars(raw, expand_env_vars)

    # No secret-bearing field present → nothing expanded.
    assert changed is False
    assert raw["executor"]["auth"] == {"type": "databricks", "profile": "my-profile"}


def test_expand_config_no_env_vars_returns_false() -> None:
    """
    ``_expand_config_env_vars`` returns ``False`` when the
    config has no fields that need expansion.
    """
    from omnigent.spec import expand_env_vars

    raw: dict[str, Any] = {
        "spec_version": 1,
        "name": "simple-agent",
    }
    changed = _expand_config_env_vars(raw, expand_env_vars)
    assert changed is False


def test_expand_config_unresolved_var_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_expand_config_env_vars`` raises ``OmnigentError``
    when a ``${VAR}`` reference cannot be resolved.
    """
    monkeypatch.delenv("MISSING_KEY_12345", raising=False)
    from omnigent.spec import expand_env_vars

    raw: dict[str, Any] = {
        "llm": {
            "model": "gpt-5.4",
            "connection": {"api_key": "${MISSING_KEY_12345}"},
        },
    }
    with pytest.raises(OmnigentError, match="MISSING_KEY_12345"):
        _expand_config_env_vars(raw, expand_env_vars)


# ── _resolve_bundle_env_vars ─────────────────────────


def test_resolve_bundle_expands_config_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_resolve_bundle_env_vars`` returns resolved
    ``config.yaml`` content with expanded env vars.
    """
    monkeypatch.setenv("BUNDLE_TEST_KEY", "resolved-value")
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "llm": {
                "model": "gpt-5.4",
                "connection": {"api_key": "${BUNDLE_TEST_KEY}"},
            },
        },
    )

    resolved = _resolve_bundle_env_vars(tmp_path)

    assert "config.yaml" in resolved
    # Parse the resolved YAML and verify the value.
    parsed = yaml.safe_load(resolved["config.yaml"])
    assert parsed["llm"]["connection"]["api_key"] == "resolved-value"


def test_resolve_bundle_expands_mcp_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_resolve_bundle_env_vars`` returns resolved MCP config
    YAML with expanded header env vars.
    """
    monkeypatch.setenv("MCP_TOKEN", "tok-abc")
    _write_mcp_config(
        tmp_path,
        "github",
        {
            "name": "github",
            "transport": "http",
            "url": "http://localhost:9000/mcp",
            "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
        },
    )
    # config.yaml must exist (even if empty) for a valid agent dir.
    _write_config(tmp_path, {"spec_version": 1})

    resolved = _resolve_bundle_env_vars(tmp_path)

    arcname = "tools/mcp/github.yaml"
    assert arcname in resolved
    parsed = yaml.safe_load(resolved[arcname])
    assert parsed["headers"]["Authorization"] == "Bearer tok-abc"


def test_resolve_bundle_expands_mcp_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_resolve_bundle_env_vars`` resolves ``${VAR}`` in a stdio MCP
    server's ``env`` block, not just HTTP ``headers``.

    Stdio ``env`` is a server-side expansion site,
    so the client must resolve it too or a local stdio MCP server using
    ``${VAR}`` would receive the literal reference after the server
    stopped expanding uploaded bundles.
    """
    monkeypatch.setenv("STDIO_SECRET", "stdio-tok-xyz")
    _write_mcp_config(
        tmp_path,
        "local-tool",
        {
            "name": "local-tool",
            "transport": "stdio",
            "command": "my-mcp-server",
            "env": {"API_TOKEN": "${STDIO_SECRET}"},
        },
    )
    _write_config(tmp_path, {"spec_version": 1})

    resolved = _resolve_bundle_env_vars(tmp_path)

    arcname = "tools/mcp/local-tool.yaml"
    assert arcname in resolved
    parsed = yaml.safe_load(resolved[arcname])
    # If this is still "${STDIO_SECRET}", the stdio env branch was not
    # wired and the local MCP subprocess would get a literal reference.
    assert parsed["env"]["API_TOKEN"] == "stdio-tok-xyz"


def test_resolve_bundle_no_env_vars_returns_empty(
    tmp_path: Path,
) -> None:
    """
    ``_resolve_bundle_env_vars`` returns an empty dict when
    the config has no env var references.
    """
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "plain-agent",
        },
    )

    resolved = _resolve_bundle_env_vars(tmp_path)
    assert resolved == {}


def test_resolve_bundle_missing_env_var_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_resolve_bundle_env_vars`` raises ``OmnigentError``
    when a config.yaml env var cannot be resolved.
    """
    monkeypatch.delenv("NONEXISTENT_DEPLOY_KEY", raising=False)
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "tools": {
                "builtins": [
                    {
                        "name": "web_search_goog",
                        "api_key": "${NONEXISTENT_DEPLOY_KEY}",
                    },
                ],
            },
        },
    )

    with pytest.raises(OmnigentError, match="NONEXISTENT_DEPLOY_KEY"):
        _resolve_bundle_env_vars(tmp_path)


# ── _bundle integration tests ──────────────────────────


def _extract_yaml_from_bundle(
    bundle_bytes: bytes,
    arcname: str,
) -> dict[str, Any]:
    """
    Extract and parse a YAML file from a tar.gz bundle.

    :param bundle_bytes: The gzipped tarball bytes.
    :param arcname: The archive member name, e.g.
        ``"config.yaml"`` or ``"tools/mcp/github.yaml"``.
    :returns: The parsed YAML content as a dict.
    """
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tf:
        member = tf.getmember(arcname)
        extracted = tf.extractfile(member)
        assert extracted is not None, f"Expected {arcname!r} to be a regular file in the bundle"
        # ``yaml.safe_load`` returns ``Any``; the caller declares
        # ``dict[str, Any]``. Every call site feeds this real YAML
        # config bundles whose top-level is a mapping, so the cast
        # is sound and matches the annotated return type.
        parsed = yaml.safe_load(extracted.read())
        assert isinstance(parsed, dict), (
            f"Expected {arcname!r} to parse to a dict, got {type(parsed).__name__}"
        )
        return cast(dict[str, Any], parsed)


def test_bundle_resolves_config_env_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_bundle`` produces a tarball where ``config.yaml`` has
    ``${VAR}`` references replaced with resolved values.

    Verifies the end-to-end path: write agent dir with env var
    refs → call ``_bundle`` → extract tarball → assert resolved.
    """
    monkeypatch.setenv("BUNDLE_LLM_KEY", "sk-live-abc123")
    monkeypatch.setenv("BUNDLE_PPLX_KEY", "pplx-live-xyz")
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "env-test-agent",
            "llm": {
                "model": "openai/gpt-4o",
                "connection": {"api_key": "${BUNDLE_LLM_KEY}"},
            },
            "tools": {
                "builtins": [
                    "web_search",
                    {
                        "name": "web_search_pplx",
                        "api_key": "${BUNDLE_PPLX_KEY}",
                    },
                ],
            },
        },
    )

    bundle_bytes = _bundle(tmp_path)
    parsed = _extract_yaml_from_bundle(bundle_bytes, "config.yaml")

    # LLM connection key must be resolved — if still "${BUNDLE_LLM_KEY}",
    # the server would receive an unresolved reference it can't expand.
    assert parsed["llm"]["connection"]["api_key"] == "sk-live-abc123", (
        "LLM api_key should be resolved in the bundle tarball"
    )
    # Builtin tool config key must be resolved.
    perplexity_entry = parsed["tools"]["builtins"][1]
    assert perplexity_entry["api_key"] == "pplx-live-xyz", (
        "Builtin tool api_key should be resolved in the bundle tarball"
    )
    assert perplexity_entry["name"] == "web_search_pplx", (
        "Builtin tool name must be preserved after expansion"
    )
    # String entries pass through unchanged.
    assert parsed["tools"]["builtins"][0] == "web_search", (
        "String builtin entries should be unchanged in the bundle"
    )
    # Non-secret fields survive bundling.
    assert parsed["name"] == "env-test-agent"
    assert parsed["llm"]["model"] == "openai/gpt-4o"


def test_bundle_resolves_mcp_header_env_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_bundle`` produces a tarball where MCP server YAML files
    have ``${VAR}`` references in headers replaced with resolved
    values.
    """
    monkeypatch.setenv("BUNDLE_GH_TOKEN", "ghp-secret-tok")
    _write_config(tmp_path, {"spec_version": 1, "name": "mcp-agent"})
    _write_mcp_config(
        tmp_path,
        "github",
        {
            "name": "github",
            "transport": "http",
            "url": "http://localhost:9000/mcp",
            "headers": {"Authorization": "Bearer ${BUNDLE_GH_TOKEN}"},
        },
    )

    bundle_bytes = _bundle(tmp_path)
    parsed = _extract_yaml_from_bundle(bundle_bytes, "tools/mcp/github.yaml")

    # Header must be resolved — an unresolved "${BUNDLE_GH_TOKEN}"
    # would cause MCP auth failures on the server.
    assert parsed["headers"]["Authorization"] == "Bearer ghp-secret-tok", (
        "MCP header env var should be resolved in the bundle tarball"
    )
    # Non-header fields survive bundling.
    assert parsed["name"] == "github"
    assert parsed["url"] == "http://localhost:9000/mcp"


def test_bundle_no_env_vars_preserves_files(
    tmp_path: Path,
) -> None:
    """
    ``_bundle`` produces a valid tarball even when no env vars
    need expansion — files are included as-is.
    """
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "name": "plain-agent",
            "llm": {"model": "openai/gpt-4o"},
        },
    )

    bundle_bytes = _bundle(tmp_path)
    parsed = _extract_yaml_from_bundle(bundle_bytes, "config.yaml")

    # Config content should be preserved exactly.
    assert parsed["name"] == "plain-agent"
    assert parsed["llm"]["model"] == "openai/gpt-4o"


def test_bundle_materializes_standalone_omnigent_yaml(tmp_path: Path) -> None:
    """
    ``_bundle`` wraps a standalone omnigent YAML file in a tarball.

    ``omnigent run <yaml> --server`` uploads the returned bytes
    directly to ``POST /api/agents``. If the YAML bytes are passed
    through unchanged, the remote server rejects them as an invalid
    tarball before the runner tunnel can start.
    """
    yaml_path = tmp_path / "hello.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "name": "hello",
                "prompt": "Say hi briefly.",
                "executor": {"harness": "claude-sdk", "model": "test-model"},
            },
            sort_keys=False,
        )
    )

    bundle_bytes = _bundle(yaml_path)
    parsed = _extract_yaml_from_bundle(bundle_bytes, "hello.yaml")

    assert parsed["name"] == "hello"
    assert parsed["prompt"] == "Say hi briefly."
    assert parsed["executor"]["harness"] == "claude-sdk"


def test_bundle_passthrough_existing_tarball(
    tmp_path: Path,
) -> None:
    """
    ``_bundle`` returns the raw bytes of an existing ``.tar.gz``
    file without modification (env var expansion only applies to
    directories).
    """
    # Build a tarball with an unresolved env var reference.
    config_bytes = yaml.dump(
        {
            "spec_version": 1,
            "llm": {"connection": {"api_key": "${SHOULD_NOT_EXPAND}"}},
        }
    ).encode()
    tarball_path = tmp_path / "agent.tar.gz"
    with tarfile.open(tarball_path, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(config_bytes)
        tf.addfile(info, io.BytesIO(config_bytes))

    bundle_bytes = _bundle(tarball_path)

    # Passthrough: bytes must match the original file exactly.
    assert bundle_bytes == tarball_path.read_bytes(), (
        "Existing tarball should be returned as-is without expansion"
    )


def test_bundle_missing_env_var_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_bundle`` raises ``OmnigentError`` when the agent
    directory contains an unresolvable ``${VAR}`` reference.
    """
    monkeypatch.delenv("NONEXISTENT_BUNDLE_KEY", raising=False)
    _write_config(
        tmp_path,
        {
            "spec_version": 1,
            "llm": {
                "model": "openai/gpt-4o",
                "connection": {"api_key": "${NONEXISTENT_BUNDLE_KEY}"},
            },
        },
    )

    with pytest.raises(OmnigentError, match="NONEXISTENT_BUNDLE_KEY"):
        _bundle(tmp_path)


# ── _preregister_agent ──────────────────────────────────────


class _RecordingAgentStore:
    """
    In-memory agent store stub capturing the exact shape
    :func:`_preregister_agent` writes. Avoids MagicMock so an
    accidental attribute access on a missing method surfaces as
    ``AttributeError`` instead of silently returning a MagicMock.
    """

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def get_by_name(self, name: str) -> None:
        """:returns: Always ``None`` — fresh store, no collisions."""
        del name
        return

    def delete(self, agent_id: str) -> None:
        """Stubbed — replace-path not exercised by these tests."""
        raise AssertionError(
            f"delete() called unexpectedly with {agent_id!r} — "
            f"tests were not supposed to hit the replace path."
        )

    def create(
        self,
        *,
        agent_id: str,
        name: str,
        bundle_location: str,
        description: str | None,
    ) -> None:
        """Record the create-call for assertions."""
        self.created.append(
            {
                "agent_id": agent_id,
                "name": name,
                "bundle_location": bundle_location,
                "description": description,
            },
        )


class _RecordingArtifactStore:
    """
    In-memory artifact store stub.
    """

    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes]] = []

    def put(self, location: str, data: bytes) -> None:
        """Record the put-call for assertions."""
        self.puts.append((location, data))

    def delete(self, location: str) -> None:
        """Stubbed — replace-path not exercised."""
        raise AssertionError(f"delete() called unexpectedly with {location!r}")


class _RecordingAgentCache:
    """
    In-memory AgentCache stub. Captures the disk-cache swap path
    so tests can assert ``replace()`` is wired up correctly without
    hitting a real on-disk cache directory. Replace-path isn't
    exercised by the create-only tests (no existing agent), so
    ``replace`` is stubbed to assert if unexpectedly called.
    """

    def __init__(self) -> None:
        self.replaces: list[tuple[str, str, bytes]] = []

    def replace(
        self,
        agent_id: str,
        location: str,
        data: bytes,
        *,
        expand_env: bool = False,
    ) -> None:
        """Record the replace-call for assertions.

        :param agent_id: Agent id being replaced.
        :param location: New bundle location.
        :param data: New bundle bytes.
        :param expand_env: Ignored — accepted to match the real
            ``AgentCache.replace`` signature (
            ``_preregister_agent`` passes ``expand_env=True`` for the
            operator template). Omitting it would raise ``TypeError``.
        """
        del expand_env
        self.replaces.append((agent_id, location, data))


def test_preregister_agent_accepts_directory(tmp_path: Path) -> None:
    """
    A directory source (``config.yaml`` + assets) registers as the
    canonical agent-image bundle. The stored bytes round-trip
    through ``spec.load`` to the same name the YAML declared.

    What breaks if this fails: ``omnigent server --agent my-agent/``
    regresses — the standard case every omnigent user exercises.
    """
    agent_dir = tmp_path / "native-agent"
    agent_dir.mkdir()
    _write_config(
        agent_dir,
        {
            "spec_version": 1,
            "name": "native-agent",
            "executor": {"config": {"harness": "openai-agents"}},
        },
    )

    agent_store = _RecordingAgentStore()
    artifact_store = _RecordingArtifactStore()
    agent_cache = _RecordingAgentCache()

    agent_id = _preregister_agent(agent_dir, agent_store, artifact_store, agent_cache)

    # Exactly one create + one put.
    assert len(agent_store.created) == 1
    assert len(artifact_store.puts) == 1

    created = agent_store.created[0]
    assert agent_id == created["agent_id"]
    assert created["name"] == "native-agent"
    # bundle_location is the deterministic ``<agent_id>/<sha256>``
    # shape; checking it starts with the recorded agent_id is
    # sufficient — the sha256 is an implementation detail.
    assert created["bundle_location"].startswith(created["agent_id"] + "/")

    # Artifact store received the same location and non-empty
    # bytes.
    put_location, put_bytes = artifact_store.puts[0]
    assert put_location == created["bundle_location"]
    assert len(put_bytes) > 0


def test_preregister_agent_accepts_omnigent_yaml_file(tmp_path: Path) -> None:
    """
    A standalone omnigent YAML file registers identically — the
    spec's ``name`` field becomes the agent name, and the tarball
    stored in the artifact store round-trips through
    ``_find_omnigent_yaml_in_dir`` to the same spec.

    What breaks if this fails: ``omnigent server --agent coding_supervisor.yaml``
    either crashes on the bundle step or stores a broken tarball
    the server later fails to rehydrate.
    """
    yaml_path = tmp_path / "hello.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "hello-world",
                "prompt": "hi",
                "executor": {
                    "model": "databricks-claude-sonnet-4",
                    "harness": "claude-sdk",
                },
            },
        ),
    )

    agent_store = _RecordingAgentStore()
    artifact_store = _RecordingArtifactStore()
    agent_cache = _RecordingAgentCache()

    agent_id = _preregister_agent(yaml_path, agent_store, artifact_store, agent_cache)

    # The YAML's ``name`` field flows through the omnigent
    # adapter into the AgentSpec and lands on the stored row.
    # Any regression in that translation would surface here.
    assert len(agent_store.created) == 1
    assert agent_id == agent_store.created[0]["agent_id"]
    assert agent_store.created[0]["name"] == "hello-world"
    # A put happened with the tarball bytes.
    assert len(artifact_store.puts) == 1
    assert len(artifact_store.puts[0][1]) > 0


def test_preregister_agent_stored_tarball_rehydrates(tmp_path: Path) -> None:
    """
    The bytes written to the artifact store must be a valid tarball
    that, when extracted and loaded, produces the same spec name.
    Catches regressions where the tarball shape drifts (wrong
    ``arcname``, nested bundle dirs, etc.) — the rehydrate-and-load
    round-trip is the server's runtime contract.
    """
    import tempfile

    from omnigent.spec import load

    yaml_path = tmp_path / "supervisor.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "name": "supervisor-probe",
                "prompt": "probe",
                "executor": {
                    "model": "databricks-claude-sonnet-4",
                    "harness": "claude-sdk",
                },
            },
        ),
    )

    agent_store = _RecordingAgentStore()
    artifact_store = _RecordingArtifactStore()
    agent_cache = _RecordingAgentCache()
    _preregister_agent(yaml_path, agent_store, artifact_store, agent_cache)

    # Extract the stored bytes and re-load through the standard
    # entrypoint. A successful load with the right name proves the
    # bundle shape is correct end-to-end.
    _, stored_bytes = artifact_store.puts[0]
    with tempfile.TemporaryDirectory() as extracted_dir:
        spec = load(stored_bytes, dest=Path(extracted_dir))
    assert spec.name == "supervisor-probe"


# ── no-AGENT harness launch ───────────────────────────


def test_materialize_harness_launcher_file_writes_omnigent_yaml() -> None:
    """No-AGENT run materialization writes a standalone Omnigent YAML file."""
    generated = _materialize_harness_launcher_file(
        harness="claude",
        model="databricks-claude-sonnet-4-6",
        system_prompt="Custom instructions.",
    )

    assert generated.name == "claude-sdk.yaml"
    assert generated.is_file()
    raw = yaml.safe_load(generated.read_text())
    assert raw == {
        "name": "claude",
        "prompt": "Custom instructions.",
        "executor": {
            "harness": "claude-sdk",
            "model": "databricks-claude-sonnet-4-6",
        },
        "os_env": {"type": "caller_process", "sandbox": {"type": "none"}},
    }
    # The launcher must NEVER bake a Databricks profile into the ad-hoc
    # spec (the --profile flag was removed): a baked profile would make
    # _resolve_provider_for_build skip a configured provider and route the
    # turn through the Databricks gateway. A "profile" key here means the
    # removed baking behavior came back.
    assert "profile" not in raw["executor"], raw["executor"]


def test_materialize_harness_launcher_file_acp_slug() -> None:
    """``run --harness acp:<slug>`` produces a valid spec that keeps the slug.

    acp:<slug> canonicalizes to the base ``acp`` harness, but the slug selects a
    configured ACP agent resolved at spawn — so executor.harness must keep the
    full ``acp:<slug>``. The agent name has no colon (the validator requires
    [a-zA-Z0-9_-]+), so it is sanitized to ``acp-<slug>``.
    """
    import re

    generated = _materialize_harness_launcher_file(
        harness="acp:qwenacp", model=None, system_prompt=None
    )
    raw = yaml.safe_load(generated.read_text())
    assert raw["executor"]["harness"] == "acp:qwenacp"  # slug preserved for the runner
    assert raw["name"] == "acp-qwenacp"
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", raw["name"])  # passes the agent-name validator


def test_materialize_harness_launcher_file_kimi_gets_os_env() -> None:
    """``run --harness kimi`` bakes a caller-process ``os_env`` so the SDK kimi
    operates in the user's current directory, matching claude-sdk.

    Regression: kimi was missing from ``_OS_ENV_HARNESSES``, so its launcher
    spec had no ``os_env`` block and the headless harness ran detached from the
    cwd (the user saw it operate out of the /tmp spec dir, not their folder)."""
    for alias in ("kimi", "kimi-code"):
        generated = _materialize_harness_launcher_file(
            harness=alias, model=None, system_prompt=None
        )
        raw = yaml.safe_load(generated.read_text())
        assert raw["executor"]["harness"] == "kimi"
        assert raw["os_env"] == {"type": "caller_process", "sandbox": {"type": "none"}}


def test_run_without_agent_drops_into_configure_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bare ``run`` (no AGENT, no --harness) with nothing configured drops into
    ``configure harnesses`` and exits cleanly — it no longer hard-errors.

    The old "requires --harness" guidance was removed by the first-run
    smart-defaults path (``cli._resolve_first_run_plan``): a bare first ``run``
    now derives a harness from configured creds, or — when nothing is set up —
    offers ``configure harnesses`` and exits cleanly rather than erroring. The
    unconfigured decision itself is unit-tested in
    ``test_resolve_first_run_plan_drops_into_configure_when_empty``; this test
    pins the ``run``-command wiring at the CLI layer.

    Fully isolated so it neither depends on the developer's ambient creds nor
    mutates the real ``~/.omnigent`` config, and never launches a daemon.
    """
    # Empty config + no detectable provider before/after configure, so the
    # first-run plan resolves to "nothing configured".
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._promote_global_auth_to_provider", Mock())
    monkeypatch.setattr("omnigent.cli._adopt_detected_providers", Mock(return_value=[]))
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.default_provider_for_harness",
        _fake_provider_for(),  # nothing configured
    )
    # The kimi fallback in ``_pick_first_run_harness`` gates on the ``kimi``
    # binary being on PATH. Stub it to False so the test stays deterministic
    # on machines where the developer has kimi installed.
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed",
        lambda _key: False,
    )
    # The configure picker would block on a real terminal; stub it.
    configure = Mock()
    monkeypatch.setattr("omnigent.cli._run_configure_harnesses_interactive", configure)

    result = CliRunner().invoke(cli, ["run"])

    # Exits cleanly (no error, no daemon launch) having dropped into configure.
    assert result.exit_code == 0, result.output
    configure.assert_called_once_with()
    assert "Found no harnesses configured." in result.output
    # The removed hard-error guidance must not reappear (regression guard).
    assert "Provide an AGENT path" not in result.output


def test_run_without_agent_claude_alias_dispatches_generated_yaml_headlessly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run --harness -p`` dispatches headlessly with the generated YAML.

    Under the daemon-backed model, headless ``-p`` (without ``--no-session``)
    runs against the daemon-backed server via ``run_chat`` rather than the
    legacy in-process ``run_prompt``.
    """
    # Isolate from any real ~/.omnigent/config.yaml on the developer's machine
    # (config defaults and ambient creds must not leak into the generated YAML
    # or the dispatch kwargs asserted below).
    monkeypatch.setattr("omnigent.cli._load_global_config", dict)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for _var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(_var, raising=False)
    run_chat = Mock()
    run_prompt = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)
    monkeypatch.setattr("omnigent.chat.run_prompt", run_prompt)

    result = CliRunner().invoke(
        cli,
        [
            "run",
            "--harness",
            "claude",
            "--model",
            "databricks-claude-sonnet-4-6",
            "--system-prompt",
            "Custom instructions.",
            "--tools",
            "coding",
            "-p",
            "hello",
        ],
    )

    assert result.exit_code == 0, result.output
    run_prompt.assert_not_called()
    run_chat.assert_called_once()
    kwargs = run_chat.call_args.kwargs
    generated = Path(kwargs["target"])
    assert generated.suffix == ".yaml"
    raw = yaml.safe_load(generated.read_text())
    assert raw == {
        "name": "claude",
        "prompt": "Custom instructions.",
        "executor": {
            "harness": "claude-sdk",
            "model": "databricks-claude-sonnet-4-6",
        },
        "os_env": {"type": "caller_process", "sandbox": {"type": "none"}},
    }
    # Daemon-backed one-shot: server_url=None (local daemon backend) and
    # ephemeral=False. harness/model are already baked into the generated
    # YAML, so they pass through as None.
    assert kwargs["client_tools"] == "coding"
    assert kwargs["server_url"] is None
    assert kwargs["ephemeral"] is False
    assert kwargs["prompt"] == "hello"


def _write_default_agent(tmp_path: Path, harness: str) -> str:
    """Write a minimal default-agent YAML declaring *harness*; return its path.

    :param tmp_path: pytest temp dir.
    :param harness: The ``executor.harness`` value, e.g. ``"openai-agents"``.
    :returns: Absolute path to the written YAML.
    """
    agent = tmp_path / "default_agent.yaml"
    agent.write_text(f"name: a\nexecutor:\n  harness: {harness}\n  model: databricks-gpt-5-5\n")
    return str(agent)


def test_resolve_default_agent_target_no_default_agent() -> None:
    """With no default_agent, the target is None (no-AGENT launcher / error path)."""
    assert _resolve_default_agent_target(None, "codex") is None
    assert _resolve_default_agent_target(None, None) is None


def test_resolve_default_agent_target_no_harness_uses_default(tmp_path: Path) -> None:
    """No --harness → the configured default_agent is used (unchanged behavior)."""
    agent = _write_default_agent(tmp_path, "openai-agents")
    assert _resolve_default_agent_target(agent, None) == agent


def test_resolve_default_agent_target_matching_harness_uses_default(tmp_path: Path) -> None:
    """--harness matching the default agent's harness → use the configured agent."""
    agent = _write_default_agent(tmp_path, "openai-agents")
    # canonicalize_harness("openai-agents") == the YAML's canonicalized harness.
    assert _resolve_default_agent_target(agent, "openai-agents") == agent


def test_resolve_default_agent_target_mismatched_harness_warns_and_falls_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    A --harness that differs from the default agent's harness warns and returns
    None, so a minimal built-in agent launches instead of forcing the wrong
    harness onto the configured (e.g. gpt) spec.
    """
    agent = _write_default_agent(tmp_path, "openai-agents")
    result = _resolve_default_agent_target(agent, "claude-sdk")
    # Falls back to the minimal launcher (None), NOT the openai-agents default agent.
    assert result is None
    err = capsys.readouterr().err
    # Warning names both the default agent's harness and the requested one.
    assert "openai-agents" in err
    assert "claude-sdk" in err


def test_run_without_agent_unsupported_harness_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported no-AGENT harness values fail before run_chat dispatch."""
    run_chat = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)

    result = CliRunner().invoke(cli, ["run", "--harness", "unknown"])

    assert result.exit_code != 0
    assert "Unsupported harness 'unknown'" in result.output
    run_chat.assert_not_called()


def test_run_with_agent_unsupported_harness_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported harness values are validated for existing AGENT mode too."""
    run_chat = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)

    result = CliRunner().invoke(
        cli,
        ["run", "tests/resources/examples/hello_world.yaml", "--harness", "unknown"],
    )

    assert result.exit_code != 0
    assert "Unsupported harness 'unknown'" in result.output
    run_chat.assert_not_called()


def test_run_with_agent_accepts_openai_agents_sdk_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--harness openai-agents-sdk`` passes validation and dispatches.

    This is the spelling the project docs use in run examples; before
    the alias existed, ``_validate_harness`` rejected it as unsupported.
    """
    monkeypatch.setattr("omnigent.cli._load_global_config", dict)
    run_chat = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)

    result = CliRunner().invoke(
        cli,
        ["run", "tests/resources/examples/hello_world.yaml", "--harness", "openai-agents-sdk"],
    )

    assert result.exit_code == 0, result.output
    # Dispatch happened — the alias cleared validation. The canonical
    # rewrite happens later, at override materialization.
    run_chat.assert_called_once()


@pytest.mark.parametrize("flag", ["--omnigent", "--no-sessions-api"])
def test_removed_runner_flow_flags_are_rejected(flag: str) -> None:
    """Removed runner-flow escape hatches are no longer accepted by click."""
    result = CliRunner().invoke(cli, ["run", "tests/resources/examples/hello_world.yaml", flag])

    assert result.exit_code != 0
    # click 8.2 reworded this from "No such option: --x" to "No such option
    # '--x'." (and may append a "Did you mean" suggestion), so match loosely.
    assert "No such option" in result.output and flag in result.output


def test_attach_without_server_errors_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """``attach`` fails loud when there is no server to join — it never spawns one."""
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    # No --server, no configured server, and no running local server.
    monkeypatch.setattr("omnigent.cli.local_server_url_if_healthy", lambda: None)

    result = CliRunner().invoke(cli, ["attach", "conv_abc"])

    assert result.exit_code != 0
    assert "No server to attach to" in result.output


def test_attach_without_conversation_errors_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """``attach`` with a server but no conversation id fails loud (no picker, no spawn)."""
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)

    result = CliRunner().invoke(cli, ["attach", "--server", "http://localhost:8000"])

    assert result.exit_code != 0
    assert "Nothing to attach to" in result.output


def test_run_with_agent_still_dispatches_existing_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing ``run AGENT --harness`` behavior still passes through."""
    monkeypatch.setattr("omnigent.cli._load_global_config", dict)
    run_chat = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)

    result = CliRunner().invoke(
        cli,
        ["run", "tests/resources/examples/hello_world.yaml", "--harness", "codex", "--model", "m"],
    )

    assert result.exit_code == 0, result.output
    run_chat.assert_called_once_with(
        target="tests/resources/examples/hello_world.yaml",
        client_tools=None,
        server_url=None,
        harness="codex",
        model="m",
        prompt=None,
        system_prompt=None,
        ephemeral=False,
        resume_conversation_id=None,
        resume_latest=False,
        resume_picker=False,
        fork_session_id=None,
        log=False,
        debug_events=False,
        resume_parts=[
            "cli",
            "run",
            "tests/resources/examples/hello_world.yaml",
            "--harness",
            "codex",
            "--model",
            "m",
        ],
        # Interactive ``run`` (no -p) defaults the browser-open ON.
        auto_open_conversation=True,
    )


def test_run_resume_picker_forwards_to_run_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare ``--resume`` forwards as ``resume_picker=True``."""
    monkeypatch.setattr("omnigent.cli._load_global_config", dict)
    run_chat = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)

    result = CliRunner().invoke(
        cli,
        [
            "run",
            "tests/resources/examples/hello_world.yaml",
            "--resume",
            "--continue",
            "--no-session",
            "--log",
        ],
    )

    assert result.exit_code == 0, result.output
    run_chat.assert_called_once_with(
        target="tests/resources/examples/hello_world.yaml",
        client_tools=None,
        server_url=None,
        harness=None,
        model=None,
        prompt=None,
        system_prompt=None,
        ephemeral=True,
        resume_conversation_id=None,
        resume_latest=True,
        resume_picker=True,
        fork_session_id=None,
        log=True,
        debug_events=False,
        resume_parts=[
            "cli",
            "run",
            "tests/resources/examples/hello_world.yaml",
            "--log",
        ],
        # Interactive ``run`` (no -p) defaults the browser-open ON.
        auto_open_conversation=True,
    )


def test_run_resume_with_conversation_id_forwards_to_run_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--resume <id>`` forwards as ``resume_conversation_id`` (not picker)."""
    monkeypatch.setattr("omnigent.cli._load_global_config", dict)
    run_chat = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)

    result = CliRunner().invoke(
        cli,
        [
            "run",
            "examples/hello_world.yaml",
            "--resume",
            "conv_123",
            "--no-session",
            "--log",
        ],
    )

    assert result.exit_code == 0, result.output
    run_chat.assert_called_once_with(
        target="examples/hello_world.yaml",
        client_tools=None,
        server_url=None,
        harness=None,
        model=None,
        prompt=None,
        system_prompt=None,
        ephemeral=True,
        resume_conversation_id="conv_123",
        resume_latest=False,
        resume_picker=False,
        fork_session_id=None,
        log=True,
        debug_events=False,
        resume_parts=["cli", "run", "examples/hello_world.yaml", "--log"],
        # Interactive ``run`` (no -p) defaults the browser-open ON.
        auto_open_conversation=True,
    )


def test_attach_forwards_live_conversation_to_run_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``attach <id> --server`` joins the live conversation via ``run_attach``
    (the co-drive client that dispatches to the host's existing runner)."""
    # Isolate from the developer's real ~/.omnigent config (a configured
    # server/auto-open default would otherwise leak into the asserted
    # run_attach kwargs).
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    # The session-exists probe is exercised separately; here we assert the forward.
    monkeypatch.setattr("omnigent.cli._require_live_conversation", lambda **_kw: None)
    run_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)

    result = CliRunner().invoke(cli, ["attach", "conv_456", "--server", "http://localhost:8000"])

    assert result.exit_code == 0, result.output
    # The server URL + live conversation id are forwarded; no harness/model/
    # ephemeral knobs exist on attach (the host owns the agent + persistence).
    run_attach.assert_called_once_with(
        base_url="http://localhost:8000",
        conversation_id="conv_456",
        client_tools=None,
        debug_events=False,
        auto_open_conversation=False,
        resume_parts=["cli", "attach", "conv_456", "--server", "http://localhost:8000"],
    )


def test_attach_nonlive_conversation_errors_loud_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``attach`` fails loud when the session is not live, and never calls run_attach."""
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    # Server reports the conversation does not exist (404).
    monkeypatch.setattr(
        "omnigent.cli._host_http_json",
        lambda **_kw: _HostHttpResult(status_code=404, body={"detail": "not found"}),
    )
    run_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)

    result = CliRunner().invoke(cli, ["attach", "conv_x", "--server", "http://localhost:8000"])

    assert result.exit_code != 0
    assert "No live session" in result.output
    run_attach.assert_not_called()


def test_resume_flags_with_prompt_dispatch_to_session_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headless ``-p`` can resume by routing through the session-backed chat path."""
    monkeypatch.setattr("omnigent.cli._load_global_config", dict)
    run_chat = Mock()
    run_prompt = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)
    monkeypatch.setattr("omnigent.chat.run_prompt", run_prompt)

    result = CliRunner().invoke(
        cli,
        ["run", "tests/resources/examples/hello_world.yaml", "-p", "hi", "--resume"],
    )

    assert result.exit_code == 0, result.output
    run_prompt.assert_not_called()
    run_chat.assert_called_once_with(
        target="tests/resources/examples/hello_world.yaml",
        client_tools=None,
        server_url=None,
        harness=None,
        model=None,
        prompt="hi",
        system_prompt=None,
        ephemeral=False,
        resume_conversation_id=None,
        resume_latest=False,
        resume_picker=True,
        debug_events=False,
        auto_open_conversation=False,
    )


def test_run_with_agent_prompt_dispatches_headlessly(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run AGENT -p`` runs one-shot against the daemon-backed server.

    Without ``--no-session``, headless ``-p`` routes through ``run_chat``
    (daemon-backed), not the legacy in-process ``run_prompt``.
    """
    monkeypatch.setattr("omnigent.cli._load_global_config", dict)
    run_chat = Mock()
    run_prompt = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)
    monkeypatch.setattr("omnigent.chat.run_prompt", run_prompt)

    result = CliRunner().invoke(
        cli,
        [
            "run",
            "tests/resources/examples/hello_world.yaml",
            "--harness",
            "codex",
            "--model",
            "m",
            "-p",
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    run_prompt.assert_not_called()
    run_chat.assert_called_once_with(
        target="tests/resources/examples/hello_world.yaml",
        client_tools=None,
        server_url=None,
        harness="codex",
        model="m",
        prompt="hi",
        system_prompt=None,
        ephemeral=False,
        debug_events=False,
        auto_open_conversation=False,
    )


def test_dispatch_rejects_positional_server_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server addresses must be passed with ``--server``, not as AGENT."""
    run_chat = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)

    with pytest.raises(ClickException, match="Server URLs are no longer accepted"):
        _dispatch_run(
            target="http://localhost:8000",
            tools=None,
            harness=None,
            model=None,
            prompt=None,
            system_prompt=None,
        )

    run_chat.assert_not_called()


def test_run_server_without_agent_dispatches_direct_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run --server URL`` connects directly to that server."""
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    run_chat = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)

    result = CliRunner().invoke(cli, ["run", "--server", "http://localhost:8000"])

    assert result.exit_code == 0, result.output
    run_chat.assert_called_once_with(
        target="http://localhost:8000",
        client_tools=None,
        server_url=None,
        harness=None,
        model=None,
        prompt=None,
        system_prompt=None,
        ephemeral=False,
        resume_conversation_id=None,
        resume_latest=False,
        resume_picker=False,
        fork_session_id=None,
        log=False,
        debug_events=False,
        resume_parts=["cli", "run", "--server", "http://localhost:8000"],
        # Interactive ``run --server`` (no -p) defaults the browser-open ON,
        # including for remote servers.
        auto_open_conversation=True,
    )


def test_run_server_resume_by_id_forwards_to_run_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run --server URL --resume <id>`` (no AGENT) resumes via ``run_attach``.

    Direct ``--server`` with no AGENT is a non-spawning client: it has no local
    agent and therefore never launches a runner. Resuming a specific
    conversation is an ATTACH (co-drive the session's existing host-bound
    runner), so it must route to ``run_attach`` — not the picker+create
    ``run_chat`` path, which entered a non-attach REPL and crashed at
    runner-bind with "Sessions API dispatch requires a registered runner id"
    the moment it tried to bind a runner it never started.
    """
    # Isolate from the developer's real ~/.omnigent config so a configured
    # server default can't leak into the asserted run_attach kwargs.
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    # The live-session probe (precise not-found error) is exercised by the
    # attach tests; here we assert the forward, so stub it out.
    monkeypatch.setattr("omnigent.cli._require_live_conversation", lambda **_kw: None)
    monkeypatch.setattr("omnigent.chat._redirect_native_resume_if_needed", lambda **_kw: False)
    run_attach = Mock()
    run_chat = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)

    result = CliRunner().invoke(
        cli, ["run", "--server", "http://localhost:8000", "--resume", "conv_456"]
    )

    assert result.exit_code == 0, result.output
    # The picker+create ``run_chat`` path is what crashed at bind — resuming a
    # direct-``--server`` session must never reach it. If this fails (run_chat
    # called), the fix regressed and the cryptic runner-id error is back.
    run_chat.assert_not_called()
    # The resume id is handed to the attach co-drive client, which reads the
    # agent from the session snapshot (no bogus agent picker) and pre-flights an
    # online runner. ``--resume conv_456`` is stripped from the on-exit resume
    # hint (the caller re-supplies it); browser-open defaults ON for an
    # interactive (no -p) invocation.
    run_attach.assert_called_once_with(
        base_url="http://localhost:8000",
        conversation_id="conv_456",
        client_tools=None,
        debug_events=False,
        auto_open_conversation=True,
        resume_parts=["cli", "run", "--server", "http://localhost:8000"],
    )


def test_run_server_resume_native_redirects_before_attach_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal-native ``run --server --resume`` redirects before attach checks.

    The pre-attach liveness check is for Omnigent REPL co-drive. Native-wrapper
    sessions need to hand off to ``omnigent claude`` / ``omnigent codex``
    even when their old runner is gone, otherwise a cold native resume fails
    before the wrapper can relaunch its terminal.
    """
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    redirected: list[dict[str, object]] = []

    def _redirect(**kwargs: object) -> bool:
        """Record the native redirect probe and claim the resume."""
        redirected.append(kwargs)
        return True

    def _must_not_preflight(**_kwargs: object) -> None:
        """Fail if direct-server resume reaches attach-only preflight."""
        raise AssertionError("native resume should redirect before live-session preflight")

    def _must_not_attach(**_kwargs: object) -> None:
        """Fail if direct-server resume reaches Omnigent attach after native redirect."""
        raise AssertionError("native resume should not call run_attach after redirect")

    monkeypatch.setattr("omnigent.chat._redirect_native_resume_if_needed", _redirect)
    monkeypatch.setattr("omnigent.cli._require_live_conversation", _must_not_preflight)
    monkeypatch.setattr("omnigent.chat.run_attach", _must_not_attach)

    result = CliRunner().invoke(
        cli, ["run", "--server", "http://localhost:8000", "--resume", "conv_native"]
    )

    assert result.exit_code == 0, result.output
    assert redirected == [
        {
            "base_url": "http://localhost:8000",
            "conversation_id": "conv_native",
            "auto_open_conversation": True,
        }
    ]


def test_run_server_resume_with_prompt_does_not_silently_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-shot ``-p`` with ``--server --resume`` must NOT reroute to attach.

    The attach client has no prompt channel, so rerouting a ``-p`` invocation
    there would silently drop the turn and leave the user in interactive
    attach. The reroute is gated to the pure-interactive shape; a prompt falls
    through to the existing remote-URL ``run_chat`` path (which one-shots /
    fails loud), carrying the prompt forward rather than discarding it.
    """
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    # If the reroute were taken this would fire; it must not be.
    monkeypatch.setattr("omnigent.cli._require_live_conversation", lambda **_kw: None)
    run_attach = Mock()
    run_chat = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)

    result = CliRunner().invoke(
        cli,
        ["run", "--server", "http://localhost:8000", "--resume", "conv_456", "-p", "hello"],
    )

    assert result.exit_code == 0, result.output
    # Prompt-bearing resume must stay off the attach path (no prompt channel).
    run_attach.assert_not_called()
    # ...and the prompt must reach run_chat, not be dropped. If run_chat is not
    # called or prompt is None, the -p turn was silently lost (the P1 regression).
    run_chat.assert_called_once()
    assert run_chat.call_args.kwargs["prompt"] == "hello"
    assert run_chat.call_args.kwargs["resume_conversation_id"] == "conv_456"


@pytest.mark.parametrize(
    "extra_flags",
    [
        ["--log"],
        ["--no-session"],
        ["--model", "gpt-x"],
        ["--system-prompt", "be terse"],
    ],
)
def test_run_server_resume_with_local_only_flag_fails_loud_not_attach(
    monkeypatch: pytest.MonkeyPatch,
    extra_flags: list[str],
) -> None:
    """Local-agent-only flags with ``--server --resume`` fail loud, not no-op.

    ``--log`` / ``--no-session`` / ``--model`` / ``--system-prompt`` have no
    meaning against a remote server, so they must keep their existing
    remote-URL fail-loud handling (in ``run_chat``) rather than being silently
    swallowed by the attach reroute. The real ``run_chat`` is left unmocked so
    its early validation raises before any network call.
    """
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    run_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)

    result = CliRunner().invoke(
        cli,
        ["run", "--server", "http://localhost:8000", "--resume", "conv_456", *extra_flags],
    )

    # Fail loud (non-zero) with the existing "only apply to local agent paths"
    # rejection — not a silent attach that ignores the flag.
    assert result.exit_code != 0
    assert "local agent" in result.output
    run_attach.assert_not_called()


# ---------------------------------------------------------------------------
# Global config helpers
# ---------------------------------------------------------------------------


def test_load_global_config_uses_env_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``OMNIGENT_CONFIG_HOME`` redirects the user config path.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used as a fake config home.
    """
    config_home = tmp_path / "isolated"
    config_home.mkdir()
    (config_home / "config.yaml").write_text("server: https://isolated.example.com\n")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    result = _load_global_config()

    assert result == {"server": "https://isolated.example.com"}


def test_load_global_config_returns_empty_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``_load_global_config`` returns ``{}`` when the config file does not exist.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used as a fake HOME.
    """
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")

    result = _load_global_config()

    # No file → empty dict; a missing config is not an error
    assert result == {}


def test_save_and_load_global_config_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``_save_global_config`` persists values that ``_load_global_config``
    reads back unchanged.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used as a fake config location.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)

    _save_global_config({"default_agent": "examples/hello.yaml", "profile": "oss"})
    result = _load_global_config()

    # Both keys must survive the YAML round-trip intact
    assert result == {"default_agent": "examples/hello.yaml", "profile": "oss"}


def test_save_global_config_merges_with_existing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A second ``_save_global_config`` call merges new keys without
    overwriting existing ones that were not passed.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used as a fake config location.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)

    _save_global_config({"default_agent": "examples/hello.yaml"})
    _save_global_config({"profile": "oss"})
    result = _load_global_config()

    # Both keys must be present: second call must not clobber first
    assert result == {"default_agent": "examples/hello.yaml", "profile": "oss"}


def test_save_global_config_unset_removes_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``_save_global_config`` with ``unset_keys`` removes specified keys.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used as a fake config location.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)

    _save_global_config({"default_agent": "examples/hello.yaml", "server": "https://example.com"})
    _save_global_config({}, unset_keys=("server",))
    result = _load_global_config()

    # server must be removed; default_agent must remain untouched
    assert result == {"default_agent": "examples/hello.yaml"}


# ---------------------------------------------------------------------------
# _is_run_shorthand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # YAML file paths — should be treated as shorthand for `run`
        (["myagent.yaml"], True),
        (["examples/hello_world.yaml"], True),
        (["./relative.yaml"], True),
        (["../sibling.yaml"], True),
        # HTTP URLs must be passed through --server, not as shorthand targets.
        (["http://localhost:8000"], False),
        (["https://example.databricksapps.com"], False),
        # Known subcommands — must NOT be redirected
        (["run", "myagent.yaml"], False),
        (["attach", "myagent.yaml"], False),
        (["config", "list"], False),
        (["version"], False),
        # Flag-only argv — not a shorthand (ad-hoc check handles these)
        (["--harness", "codex"], False),
        ([], False),
        # Plain text that looks like a prompt — must NOT redirect
        (["what does this repo do?"], False),
    ],
)
def test_is_run_shorthand(argv: list[str], expected: bool) -> None:
    """
    ``_is_run_shorthand`` returns True only for file-path targets.

    :param argv: CLI arguments without program name.
    :param expected: Whether the arguments should be redirected to ``run``.
    """
    assert _is_run_shorthand(argv) is expected


# ---------------------------------------------------------------------------
# `omnigent config` command
# ---------------------------------------------------------------------------


def test_config_list_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent config list`` prints a no-defaults message when neither
    global nor project-level config files exist.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent and cwd.
    """
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr("omnigent.cli._load_local_config", dict)
    # Isolate the defaults section — the credentials section reads ambient
    # machine state (env keys / CLI logins), which is not under test here.
    monkeypatch.setattr("omnigent.cli._print_credentials_by_harness", lambda: None)

    result = CliRunner().invoke(cli, ["config", "list"])

    assert result.exit_code == 0, result.output
    # Must mention how to set defaults, not crash or print nothing
    assert "none set" in result.output


@pytest.mark.parametrize(
    ("argv", "hint"),
    [
        # Pre-split flat forms → point at the new noun-verb subcommand.
        (["config", "default_agent=foo.yaml"], "config set"),
        (["config", "--list"], "config list"),
        (["config", "--unset", "server"], "config unset"),
        (["config", "--global", "server=https://example.com"], "--global"),
    ],
)
def test_config_legacy_form_hints_at_new_subcommand(argv: list[str], hint: str) -> None:
    """The removed flat ``config`` forms error with a hint at the new subcommand.

    Without the ``_ConfigGroup`` nudge, click would emit an opaque
    ``No such command`` / ``No such option``; this proves a migrating user is
    pointed at ``config set`` / ``config list`` / ``config unset`` instead.

    :param argv: A legacy ``config`` invocation, e.g. ``["config", "--list"]``.
    :param hint: A substring the error must contain, e.g. ``"config list"``.
    """
    result = CliRunner().invoke(cli, argv)

    assert result.exit_code != 0
    assert hint in result.output


def test_config_set_global_writes_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent config set --global key=value`` persists the value so that
    ``_load_global_config`` returns it.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)

    result = CliRunner().invoke(
        cli,
        [
            "config",
            "set",
            "--global",
            "default_agent=examples/hello_world.yaml",
            "model=databricks-claude-sonnet-4-6",
        ],
    )

    assert result.exit_code == 0, result.output
    cfg = _load_global_config()
    # default_agent is resolved to an absolute path for --global writes
    assert Path(cfg["default_agent"]).is_absolute()
    assert cfg["default_agent"].endswith("examples/hello_world.yaml")
    # The second key from the same invocation must land too (multi-key set).
    assert cfg["model"] == "databricks-claude-sonnet-4-6"


def test_config_set_global_writes_auto_open_conversation_bool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``auto_open_conversation=true`` persists as a real YAML boolean.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)

    result = CliRunner().invoke(
        cli,
        ["config", "set", "--global", "auto_open_conversation=true"],
    )

    assert result.exit_code == 0, result.output
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert cfg["auto_open_conversation"] is True
    assert _resolve_auto_open_conversation_from_config(cfg) is True


def test_config_set_global_reports_effective_config_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``OMNIGENT_CONFIG_HOME`` redirects both the write and the reported path.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))

    result = CliRunner().invoke(
        cli,
        ["config", "set", "--global", "auto_open_conversation=true"],
    )

    assert result.exit_code == 0, result.output
    assert f"Set 1 key(s) in {tmp_path / 'config.yaml'}" in result.output
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["auto_open_conversation"] is True


def test_config_set_rejects_invalid_auto_open_conversation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``auto_open_conversation`` accepts only explicit boolean values.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")

    result = CliRunner().invoke(
        cli,
        ["config", "set", "--global", "auto_open_conversation=maybe"],
    )

    assert result.exit_code != 0
    assert "auto_open_conversation" in result.output
    assert "must be a boolean" in result.output


def test_config_list_shows_saved_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent config list`` prints all defaults that were previously
    written with ``config set --global``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)
    monkeypatch.setattr("omnigent.cli._load_local_config", dict)
    _save_global_config({"default_agent": "examples/hello_world.yaml", "model": "my-model"})
    monkeypatch.setattr("omnigent.cli._print_credentials_by_harness", lambda: None)

    result = CliRunner().invoke(cli, ["config", "list"])

    assert result.exit_code == 0, result.output
    # Both saved keys must appear in the defaults section
    assert "default_agent=examples/hello_world.yaml" in result.output
    assert "model=my-model" in result.output


def test_config_list_dedups_when_cwd_is_config_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``config list`` shows a shared config file once when cwd is its home.

    When the command runs from the user's home directory, the project-level
    path (``cwd/.omnigent/config.yaml``) resolves to the SAME file as the
    user-level path (``~/.omnigent/config.yaml``).  The defaults section
    must dedup on the resolved absolute path and print that one file once,
    not twice under two different spellings.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for the home dir.
    """
    # Global config lives at ``<home>/.omnigent/config.yaml``; chdir to
    # that same home dir so the local loader reads the identical file.
    config_dir = tmp_path / ".omnigent"
    config_dir.mkdir()
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_dir / "config.yaml")
    monkeypatch.chdir(tmp_path)  # cwd == home → local path resolves to global
    monkeypatch.setattr("omnigent.cli._print_credentials_by_harness", lambda: None)
    _save_global_config({"default_agent": "examples/hello_world.yaml"})

    result = CliRunner().invoke(cli, ["config", "list"])

    assert result.exit_code == 0, result.output
    # Exactly one value line and one source-comment line. Before the
    # absolute-path dedup fix this appeared twice — once under the hardcoded
    # ``# ~/.omnigent/config.yaml`` literal and once under the resolved
    # local path — because the two sources were compared by raw spelling,
    # not resolved path. A count of 2 means the dedup regressed.
    assert result.output.count("default_agent=examples/hello_world.yaml") == 1
    source_comments = [ln for ln in result.output.splitlines() if ln.lstrip().startswith("# ")]
    assert len(source_comments) == 1, f"expected one config source, got {source_comments}"


def test_config_unset_removes_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent config unset --global server`` removes the key from
    the config file without touching other keys.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)
    _save_global_config(
        {"default_agent": "examples/hello_world.yaml", "server": "https://example.com"}
    )

    result = CliRunner().invoke(cli, ["config", "unset", "--global", "server"])

    assert result.exit_code == 0, result.output
    cfg = _load_global_config()
    # server must be gone; default_agent must remain
    assert "server" not in cfg
    assert cfg["default_agent"] == "examples/hello_world.yaml"


def test_config_unknown_key_raises_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent config set --global unknown=value`` rejects keys that are
    not in ``_GLOBAL_CONFIG_KEYS``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")

    result = CliRunner().invoke(cli, ["config", "set", "--global", "unknown=value"])

    assert result.exit_code != 0
    # Must name the bad key and the supported ones
    assert "unknown" in result.output
    assert any(k in result.output for k in _GLOBAL_CONFIG_KEYS)


def test_config_set_profile_rejected_as_unknown_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent config set profile=...`` fails with the unknown-key error.

    Pins the removal of the ``profile`` config key alongside the
    ``--profile`` CLI flag: a user migrating from an older release must
    get the fail-loud unknown-key message (listing the supported keys),
    not a silently persisted-but-ignored value.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", tmp_path / "config.yaml")

    result = CliRunner().invoke(cli, ["config", "set", "--global", "profile=oss"])

    assert result.exit_code != 0
    # Exit alone isn't enough — the error must name the rejected key so the
    # user knows profile-based server auth config is gone.
    assert "profile" in result.output
    assert "Unknown config key" in result.output
    # Nothing may be written: a rejected set must not leave a partial file.
    assert not (tmp_path / "config.yaml").exists()


def test_config_set_local_writes_project_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent config set key=value`` without ``--global`` writes to
    ``.omnigent/config.yaml`` in the current directory.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used as a stand-in project root.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", tmp_path / "global.yaml")

    result = CliRunner().invoke(cli, ["config", "set", "model=my-model"])

    assert result.exit_code == 0, result.output
    local_path = tmp_path / ".omnigent" / "config.yaml"
    assert local_path.exists(), "local config file should have been created"
    cfg = yaml.safe_load(local_path.read_text())
    assert cfg["model"] == "my-model"
    # Global config must not be touched
    assert not (tmp_path / "global.yaml").exists()


# ---------------------------------------------------------------------------
# `omnigent run` picks up global config defaults
# ---------------------------------------------------------------------------


def test_run_applies_global_config_agent_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent run`` (no AGENT arg) uses the ``default_agent`` key from
    global config as the target when no explicit target is given.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)
    _save_global_config({"default_agent": "examples/hello_world.yaml"})

    dispatched: dict[str, object] = {}

    def fake_dispatch(**kwargs: object) -> None:
        """Capture dispatch kwargs without launching the REPL."""
        dispatched.update(kwargs)

    monkeypatch.setattr("omnigent.cli._dispatch_run", fake_dispatch)
    monkeypatch.setattr("omnigent.cli._build_resume_parts", lambda: None)
    monkeypatch.setattr(
        "omnigent.cli._split_resume_value",
        lambda _: SimpleNamespace(picker=False, conversation_id=None),
    )

    result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    # Global agent default must be forwarded as the target
    assert dispatched["target"] == "examples/hello_world.yaml"


def test_run_cli_arg_overrides_global_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An explicit CLI arg on ``omnigent run`` takes precedence over the
    corresponding key in global config.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)
    _save_global_config({"model": "global-model", "server": "https://global.example.com"})

    dispatched: dict[str, object] = {}

    def fake_dispatch(**kwargs: object) -> None:
        """Capture dispatch kwargs without launching the REPL."""
        dispatched.update(kwargs)

    monkeypatch.setattr("omnigent.cli._dispatch_run", fake_dispatch)
    monkeypatch.setattr("omnigent.cli._build_resume_parts", lambda: None)
    monkeypatch.setattr(
        "omnigent.cli._split_resume_value",
        lambda _: SimpleNamespace(picker=False, conversation_id=None),
    )

    result = CliRunner().invoke(cli, ["run", "myagent.yaml", "--model", "explicit-model"])

    assert result.exit_code == 0, result.output
    # Explicit --model must win over global config value
    assert dispatched["model"] == "explicit-model"
    # server had no CLI override — global config value must be used
    assert dispatched["server"] == "https://global.example.com"


def test_run_applies_auto_open_conversation_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent run`` forwards the persisted browser-open setting.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)
    _save_global_config({"auto_open_conversation": True})

    dispatched: dict[str, object] = {}

    def fake_dispatch(**kwargs: object) -> None:
        """Capture dispatch kwargs without launching the REPL."""
        dispatched.update(kwargs)

    monkeypatch.setattr("omnigent.cli._dispatch_run", fake_dispatch)
    monkeypatch.setattr("omnigent.cli._build_resume_parts", lambda: None)
    monkeypatch.setattr(
        "omnigent.cli._split_resume_value",
        lambda _: SimpleNamespace(picker=False, conversation_id=None),
    )

    result = CliRunner().invoke(cli, ["run", "myagent.yaml"])

    assert result.exit_code == 0, result.output
    assert dispatched["auto_open_conversation"] is True


def _capture_run_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, object]:
    """
    Wire ``omnigent run`` to capture dispatch kwargs without launching.

    Points the global config at an empty *tmp_path* file (so the test is
    isolated from the developer's real ``~/.omnigent/config.yaml``) and
    stubs the dispatch / resume helpers. Callers that want a non-empty
    config call ``_save_global_config`` any time before invoking the CLI
    (the ``_GLOBAL_CONFIG_PATH`` monkeypatch this helper installs persists
    for the rest of the test).

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    :returns: A dict that ``_dispatch_run`` populates with its
        kwargs once ``run`` is invoked, e.g.
        ``{"auto_open_conversation": True, "target": "myagent.yaml"}``.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)

    dispatched: dict[str, object] = {}

    def fake_dispatch(**kwargs: object) -> None:
        """Capture dispatch kwargs without launching the REPL."""
        dispatched.update(kwargs)

    monkeypatch.setattr("omnigent.cli._dispatch_run", fake_dispatch)
    monkeypatch.setattr("omnigent.cli._build_resume_parts", lambda: None)
    monkeypatch.setattr(
        "omnigent.cli._split_resume_value",
        lambda _: SimpleNamespace(picker=False, conversation_id=None),
    )
    return dispatched


def test_run_interactive_defaults_browser_open_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Interactive ``omnigent run`` opens the browser by default.

    With no ``auto_open_conversation`` configured, a bare interactive
    ``run`` (no ``-p``) defaults the browser-open ON so users discover
    the web UI once the server is up.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    dispatched = _capture_run_dispatch(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli, ["run", "myagent.yaml"])

    assert result.exit_code == 0, result.output
    assert dispatched["auto_open_conversation"] is True


def test_run_headless_prompt_defaults_browser_open_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Headless ``omnigent run -p`` stays quiet by default.

    A one-shot ``-p`` invocation with no configured preference must NOT
    open the browser — the user is scripting, not exploring the UI.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    dispatched = _capture_run_dispatch(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli, ["run", "myagent.yaml", "-p", "hi"])

    assert result.exit_code == 0, result.output
    assert dispatched["auto_open_conversation"] is False


def test_run_interactive_respects_explicit_opt_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An explicit ``auto_open_conversation: false`` suppresses the open.

    Users who opted out keep the browser closed even on interactive
    ``run`` — the new interactive default never overrides an explicit
    config value.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    dispatched = _capture_run_dispatch(monkeypatch, tmp_path)
    _save_global_config({"auto_open_conversation": False})

    result = CliRunner().invoke(cli, ["run", "myagent.yaml"])

    assert result.exit_code == 0, result.output
    assert dispatched["auto_open_conversation"] is False


def test_run_headless_honors_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Headless ``run -p`` still opens when the user explicitly opted in.

    The headless default is OFF, but an explicit
    ``auto_open_conversation: true`` wins for ``-p`` too.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    dispatched = _capture_run_dispatch(monkeypatch, tmp_path)
    _save_global_config({"auto_open_conversation": True})

    result = CliRunner().invoke(cli, ["run", "myagent.yaml", "-p", "hi"])

    assert result.exit_code == 0, result.output
    assert dispatched["auto_open_conversation"] is True


def test_resolve_auto_open_conversation_setting_is_tristate() -> None:
    """
    ``_resolve_auto_open_conversation_setting`` distinguishes unset from set.

    Returns ``None`` when the key is absent (so ``run`` can apply its
    interactive default) and the parsed boolean when present.
    """
    assert _resolve_auto_open_conversation_setting({}) is None
    assert _resolve_auto_open_conversation_setting({"auto_open_conversation": True}) is True
    assert _resolve_auto_open_conversation_setting({"auto_open_conversation": "false"}) is False


def test_attach_applies_auto_open_conversation_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent attach`` reads browser-open from config and forwards it to run_attach.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)
    _save_global_config({"auto_open_conversation": True})
    monkeypatch.setattr("omnigent.cli._require_live_conversation", lambda **_kw: None)
    run_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)

    result = CliRunner().invoke(cli, ["attach", "conv_1", "--server", "http://localhost:8000"])

    assert result.exit_code == 0, result.output
    # The config-derived browser-open setting reaches the client unchanged.
    assert run_attach.call_args.kwargs["auto_open_conversation"] is True


def test_claude_applies_auto_open_conversation_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent claude`` forwards the persisted browser-open setting.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)
    _save_global_config({"auto_open_conversation": True})

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native",
        _fake_run_claude_native_capture(captured),
    )

    result = CliRunner().invoke(cli, ["claude"])

    assert result.exit_code == 0, result.output
    assert captured["auto_open_conversation"] is True


def test_codex_applies_auto_open_conversation_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``omnigent codex`` forwards the persisted browser-open setting.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)
    _save_global_config({"auto_open_conversation": True})

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "omnigent.codex_native.run_codex_native",
        _fake_run_codex_native_capture(captured),
    )

    result = CliRunner().invoke(cli, ["codex"])

    assert result.exit_code == 0, result.output
    assert captured["auto_open_conversation"] is True


def test_run_bare_omnigent_with_harness_only_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Bare ``omnigent`` with only ``harness`` in global config dispatches
    to ``run`` (not ``--help``), so the harness default is applied.

    Regression target: the bare-invocation check previously only looked
    at ``agent`` and ``server``, missing the ``harness``-only case.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory standing in for ~/.omnigent.
    """
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("omnigent.cli._GLOBAL_CONFIG_PATH", config_path)
    _save_global_config({"harness": "claude-sdk"})

    dispatched: dict[str, object] = {}

    def fake_dispatch(**kwargs: object) -> None:
        """Capture dispatch kwargs without launching the REPL."""
        dispatched.update(kwargs)

    monkeypatch.setattr("omnigent.cli._dispatch_run", fake_dispatch)
    monkeypatch.setattr("omnigent.cli._build_resume_parts", lambda: None)
    monkeypatch.setattr(
        "omnigent.cli._split_resume_value",
        lambda _: SimpleNamespace(picker=False, conversation_id=None),
    )

    result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    # harness default must be forwarded; no target was set
    assert dispatched["harness"] == "claude-sdk"
    assert dispatched["target"] is None


def test_bare_omnigent_harness_flag_dispatches_to_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``omnigent --harness ...`` is shorthand for ``omnigent run --harness ...``."""
    from omnigent.cli import main

    dispatched: dict[str, object] = {}

    def fake_dispatch(**kwargs: object) -> None:
        dispatched.update(kwargs)

    monkeypatch.setattr("omnigent.cli._load_global_config", dict)
    monkeypatch.setattr("omnigent.cli._dispatch_run", fake_dispatch)
    monkeypatch.setattr("omnigent.cli._build_resume_parts", lambda: None)
    monkeypatch.setattr(
        "omnigent.cli._split_resume_value",
        lambda _: SimpleNamespace(picker=False, conversation_id=None),
    )
    monkeypatch.setattr(sys, "argv", ["omnigent", "--harness", "claude"])

    main()

    assert dispatched["harness"] == "claude"
    assert dispatched["target"] is None


def test_bare_omnigent_non_tty_shows_help(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare ``omnigent`` in a non-interactive shell (no TTY) shows help.

    On a pipe / CI there is no terminal to drive a REPL, so the bare command
    falls back to ``--help`` rather than launching ``run`` (which would hang
    waiting on stdin).
    """
    from omnigent.cli import main

    monkeypatch.setattr(sys, "argv", ["omnigent"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    stdout = capsys.readouterr().out
    assert "Usage:" in stdout
    assert "Commands:" in stdout


def test_bare_omnigent_tty_dispatches_to_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare ``omnigent`` on an interactive terminal behaves like ``omnigent run``.

    ``run`` then resolves the configured default / first-run plan. We assert
    only that the bare invocation is rewritten to ``run`` before dispatch.
    """
    from omnigent import cli as cli_module

    monkeypatch.setattr(sys, "argv", ["omnigent"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    dispatched: dict[str, list[str]] = {}

    def fake_cli(*, args: list[str], standalone_mode: bool = True) -> None:
        dispatched["args"] = args

    monkeypatch.setattr(cli_module, "cli", fake_cli)

    cli_module.main()

    assert dispatched["args"] == ["run"]


def test_bare_omnigent_rejects_positional_server_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Top-level server URLs must use ``run --server`` explicitly."""
    from omnigent.cli import main

    monkeypatch.setattr(sys, "argv", ["omnigent", "http://localhost:8000"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    terminal = capsys.readouterr()
    assert "server URLs must be passed with --server" in terminal.err
    assert "omnigent run --server http://localhost:8000" in terminal.err


def test_unknown_command_reports_no_such_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    An unknown subcommand falls through to click's standard error.

    A typo'd command (``omnigent blah``) must produce click's
    "No such command" usage error, not the removed-ad-hoc-chat notice
    that previously swallowed every non-subcommand invocation.
    """
    from omnigent.cli import main

    monkeypatch.setattr(sys, "argv", ["omnigent", "blah"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    terminal = capsys.readouterr()
    combined = terminal.out + terminal.err
    assert "No such command 'blah'" in combined
    assert "ad-hoc chat was removed" not in combined


def test_setup_command_replaces_wizard(monkeypatch: pytest.MonkeyPatch) -> None:
    """``omnigent setup`` is the visible standard setup flow command."""
    configure_flow = Mock()
    configure_databricks = Mock()
    run_onboarding = Mock(return_value=True)
    monkeypatch.setattr(
        "omnigent.cli._run_configure_harnesses_interactive",
        configure_flow,
    )
    monkeypatch.setattr("omnigent.cli._run_configure_databricks", configure_databricks)
    monkeypatch.setattr("omnigent.onboarding.setup.run_onboarding", run_onboarding)

    result = CliRunner().invoke(cli, ["setup"])

    assert result.exit_code == 0, result.output
    configure_flow.assert_called_once_with()
    configure_databricks.assert_not_called()
    run_onboarding.assert_not_called()

    help_result = CliRunner().invoke(cli, ["--help"])
    assert help_result.exit_code == 0
    assert "setup" in help_result.output
    assert "wizard" not in help_result.output

    removed_result = CliRunner().invoke(cli, ["wizard"])
    assert removed_result.exit_code != 0
    assert "No such command 'wizard'" in removed_result.output

    onboard_result = CliRunner().invoke(cli, ["onboard"])
    assert onboard_result.exit_code != 0
    assert "No such command 'onboard'" in onboard_result.output


def test_setup_no_internal_beta_runs_configure_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-internal-beta`` runs the model/credential picker, not the Databricks bootstrap.

    The generic onboarding wizard was removed; ``setup --no-internal-beta``
    now runs the same interactive flow as ``configure harnesses``.
    """
    configure_databricks = Mock()
    run_onboarding = Mock()
    configure_flow = Mock()
    monkeypatch.setattr("omnigent.cli._run_configure_databricks", configure_databricks)
    monkeypatch.setattr(
        "omnigent.cli._run_configure_harnesses_interactive",
        configure_flow,
    )
    monkeypatch.setattr("omnigent.onboarding.setup.run_onboarding", run_onboarding)

    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"])

    assert result.exit_code == 0, result.output
    configure_flow.assert_called_once_with()
    configure_databricks.assert_not_called()
    run_onboarding.assert_not_called()


def test_setup_uses_compact_branding_on_short_terminals(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short first-run terminal should show the setup picker, not scroll past it."""
    configure_flow = Mock()
    print_landing = Mock()
    print_brandmark = Mock()
    monkeypatch.setattr("omnigent.cli._run_configure_harnesses_interactive", configure_flow)
    monkeypatch.setattr("omnigent.inner.ui.print_landing", print_landing)
    monkeypatch.setattr("omnigent.inner.ui.print_brandmark", print_brandmark)
    monkeypatch.setattr(
        "omnigent.cli.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((80, 24)),
    )

    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"])

    assert result.exit_code == 0, result.output
    print_brandmark.assert_called_once_with("setup")
    print_landing.assert_not_called()
    configure_flow.assert_called_once_with()


def test_setup_keeps_full_landing_on_tall_terminals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Roomy terminals keep the full first-run lockup."""
    configure_flow = Mock()
    print_landing = Mock()
    print_brandmark = Mock()
    monkeypatch.setattr("omnigent.cli._run_configure_harnesses_interactive", configure_flow)
    monkeypatch.setattr("omnigent.inner.ui.print_landing", print_landing)
    monkeypatch.setattr("omnigent.inner.ui.print_brandmark", print_brandmark)
    monkeypatch.setattr(
        "omnigent.cli.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((120, 40)),
    )

    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"])

    assert result.exit_code == 0, result.output
    print_landing.assert_called_once_with(tagline="all your agents, one cli")
    print_brandmark.assert_not_called()
    configure_flow.assert_called_once_with()


# ─── setup dependency preflight (Node / tmux) ─────────────────────────


def _fake_node_run(
    version: str,
    probe_returncode: int,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """
    Build a fake ``subprocess.run`` for the Node preflight helpers.

    Dispatches on the command so a single fake serves both calls the
    helpers make: ``node --version`` yields *version* on stdout, while the
    ``node -e`` capability probe yields *probe_returncode* (0 = the
    ``markAsUncloneable`` symbol is present, 1 = too old).

    :param version: Version string to report for ``node --version``,
        without the trailing newline that the real CLI emits.
    :param probe_returncode: Exit code the capability probe should return.
    :returns: A callable suitable for ``monkeypatch.setattr`` on
        ``omnigent.cli.subprocess.run``.
    """

    def _run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{version}\n", stderr="")
        return subprocess.CompletedProcess(cmd, probe_returncode, stdout="", stderr="")

    return _run


def test_node_dependency_problem_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A machine without ``node`` on PATH reports the missing-binary problem.

    The harnesses that need Node (Claude, Codex, Pi) should be named so the
    user knows why it matters, rather than a bare "not found".
    """
    monkeypatch.setattr("omnigent.cli.shutil.which", lambda _: None)

    problem = _node_dependency_problem()

    assert problem is not None
    assert "node not found" in problem
    assert "Pi" in problem  # the harnesses that need it are named


def test_node_dependency_problem_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Node new enough for the probe (exit 0) reports no problem."""
    monkeypatch.setattr("omnigent.cli.shutil.which", lambda _: "/usr/bin/node")
    monkeypatch.setattr(
        "omnigent.cli.subprocess.run",
        _fake_node_run("v22.14.0", probe_returncode=0),
    )

    assert _node_dependency_problem() is None


def test_node_dependency_problem_too_old(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A Node failing the capability probe surfaces the detected version and
    the exact runtime symptom so the warning is actionable.
    """
    monkeypatch.setattr("omnigent.cli.shutil.which", lambda _: "/usr/bin/node")
    monkeypatch.setattr(
        "omnigent.cli.subprocess.run",
        _fake_node_run("v20.12.2", probe_returncode=1),
    )

    problem = _node_dependency_problem()

    assert problem is not None
    assert "too old" in problem
    # The concrete detected version and the required floor must both appear —
    # that's what makes the compact warning recognizable and actionable.
    assert "v20.12.2" in problem
    assert "Node.js 22 LTS" in problem


def test_node_dependency_problem_probe_inconclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A flaky/timed-out probe yields no problem — setup must not block on a
    transient ``subprocess`` failure.
    """
    monkeypatch.setattr("omnigent.cli.shutil.which", lambda _: "/usr/bin/node")

    def _boom(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="node", timeout=10)

    monkeypatch.setattr("omnigent.cli.subprocess.run", _boom)

    assert _node_dependency_problem() is None


def test_node_version_trims_and_handles_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_node_version`` strips the trailing newline and is non-fatal."""
    monkeypatch.setattr(
        "omnigent.cli.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(["node"], 0, stdout="v22.14.0\n", stderr=""),
    )
    assert _node_version("/usr/bin/node") == "v22.14.0"

    def _boom(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("node vanished")

    monkeypatch.setattr("omnigent.cli.subprocess.run", _boom)
    assert _node_version("/usr/bin/node") is None


def test_warn_missing_harness_dependencies_silent_when_present(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With a recent Node and tmux on PATH, the preflight prints nothing."""
    monkeypatch.setattr("omnigent.cli.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "omnigent.cli.subprocess.run",
        _fake_node_run("v22.14.0", probe_returncode=0),
    )

    _warn_missing_harness_dependencies()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_warn_missing_harness_dependencies_lists_all_gaps(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    When both Node (too old) and tmux (missing) are problems, a single
    warning block lists both — the point of the up-front preflight is that
    a fresh machine sees every gap at once.
    """

    def _which(name: str) -> str | None:
        return None if name == "tmux" else "/usr/bin/node"

    monkeypatch.setattr("omnigent.cli.shutil.which", _which)
    monkeypatch.setattr(
        "omnigent.cli.subprocess.run",
        _fake_node_run("v20.12.2", probe_returncode=1),
    )

    _warn_missing_harness_dependencies()

    err = capsys.readouterr().err
    assert "Node.js is too old" in err
    assert "tmux not found" in err


def test_click_subcommands_allowlist_covers_registered_commands() -> None:
    """Every command registered on the ``cli`` group is in ``_CLICK_SUBCOMMANDS``.

    ``main()`` consults this allowlist *before* handing argv to click: a
    first token not in the set is rejected as removed top-level ad-hoc chat
    (see ``_is_removed_ad_hoc_invocation``). So a command registered on the
    group but absent from the allowlist is unreachable from the real
    entrypoint — exactly the bug where ``omnigent configure`` errored with
    "ad-hoc chat was removed" despite being registered. A failure here means
    a newly added top-level command must be added to ``_CLICK_SUBCOMMANDS``.
    """
    from omnigent.cli import _CLICK_SUBCOMMANDS, cli

    # Direction matters: the allowlist must be a superset of the registered
    # commands. Extra allowlist entries (not registered) are harmless; a
    # registered command missing from the allowlist is the unreachable bug.
    missing = set(cli.commands) - set(_CLICK_SUBCOMMANDS)
    assert missing == set(), (
        "commands registered on the cli group but missing from "
        f"_CLICK_SUBCOMMANDS (unreachable from main()): {sorted(missing)}"
    )


# ── first-run smart defaults (omnigent run) ──────────


def _fake_provider_for(*configured: str):
    """Return a default_provider_for_harness stub truthy only for *configured*.

    :param configured: Harness ids treated as having a usable credential, e.g.
        ``"claude-sdk"``.
    :returns: A ``(config, harness) -> object|None`` callable.
    """

    def _fn(config: object, harness: str) -> object | None:
        return object() if harness in configured else None

    return _fn


def test_pick_first_run_prefers_claude_with_polly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude configured → claude-sdk + the bundled polly agent.

    Claude wins the priority order and is the only family that gets a default
    *example* agent (polly). A regression that dropped polly or picked the
    wrong harness fails here.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.default_provider_for_harness",
        _fake_provider_for("claude-sdk", "codex"),  # both configured → Claude wins
    )
    plan = _pick_first_run_harness()
    assert plan is not None
    assert plan.harness == "claude-sdk"
    assert plan.agent is not None and plan.agent.endswith("polly")


def test_pick_first_run_harness_codex_then_pi_no_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Claude → Codex (then Pi) with NO default example agent (bare REPL)."""
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.default_provider_for_harness",
        _fake_provider_for("codex"),
    )
    plan = _pick_first_run_harness()
    assert plan is not None and plan.harness == "codex" and plan.agent is None

    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.default_provider_for_harness",
        _fake_provider_for("pi"),
    )
    plan = _pick_first_run_harness()
    assert plan is not None and plan.harness == "pi" and plan.agent is None


def test_pick_first_run_harness_none_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing configured → None (caller drops into configure)."""
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.default_provider_for_harness",
        _fake_provider_for(),  # nothing configured
    )
    # The kimi fallback in _pick_first_run_harness gates on the ``kimi``
    # binary being on PATH. Stub it to False so the test stays deterministic
    # on machines where the developer has kimi installed.
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed",
        lambda _key: False,
    )
    assert _pick_first_run_harness() is None


def test_resolve_first_run_plan_does_not_persist_derived_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The derived first-run pick is returned but NOT persisted as a default.

    Persisting it would pin a Codex-only user to Codex even after they add
    Claude. Keeping it ephemeral lets the next bare ``run`` re-derive from the
    current creds (and promote them to polly). Asserts the resolved plan is
    Claude→polly yet no global ``harness`` / ``default_agent`` was written.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._promote_global_auth_to_provider", Mock())
    monkeypatch.setattr("omnigent.cli._adopt_detected_providers", Mock(return_value=[]))
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.default_provider_for_harness",
        _fake_provider_for("claude-sdk"),
    )

    plan = _resolve_first_run_plan()

    assert plan is not None and plan.harness == "claude-sdk"
    assert plan.agent is not None and plan.agent.endswith("polly")
    # No global harness / default_agent was persisted — the pick is ephemeral.
    config_path = tmp_path / "config.yaml"
    saved = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    assert "harness" not in saved, f"derived harness must not be persisted; got {saved!r}"
    assert "default_agent" not in saved, (
        f"derived default_agent must not be persisted; got {saved!r}"
    )


def test_resolve_first_run_plan_re_derives_when_creds_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Adding Claude promotes a Codex-only user to polly on the next bare run.

    Because the pick is never persisted, the second resolution reflects the
    *current* creds: Codex-only → a bare codex REPL; after Claude is added →
    claude-sdk + polly (our primary). A regression that re-persisted the first
    pick would pin the user to codex and fail the second half.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._promote_global_auth_to_provider", Mock())
    monkeypatch.setattr("omnigent.cli._adopt_detected_providers", Mock(return_value=[]))

    # 1) Only Codex configured → codex REPL, no example agent.
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.default_provider_for_harness",
        _fake_provider_for("codex"),
    )
    first = _resolve_first_run_plan()
    assert first is not None and first.harness == "codex" and first.agent is None

    # 2) Claude added (now both configured) → promoted to claude-sdk + polly,
    #    NOT pinned to the earlier codex pick.
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.default_provider_for_harness",
        _fake_provider_for("claude-sdk", "codex"),
    )
    second = _resolve_first_run_plan()
    assert second is not None and second.harness == "claude-sdk"
    assert second.agent is not None and second.agent.endswith("polly")


def test_resolve_first_run_plan_drops_into_configure_when_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No creds → drop into `configure harnesses`; still none after → None.

    The configure picker is stubbed (it would block on a real terminal). A
    return of None signals the caller to exit cleanly rather than error.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._promote_global_auth_to_provider", Mock())
    monkeypatch.setattr("omnigent.cli._adopt_detected_providers", Mock(return_value=[]))
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.default_provider_for_harness",
        _fake_provider_for(),  # nothing configured, before and after configure
    )
    # The kimi fallback in ``_pick_first_run_harness`` gates on the ``kimi``
    # binary being on PATH. Stub it to False so the test stays deterministic
    # regardless of the developer's local install.
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed",
        lambda _key: False,
    )
    configure = Mock()
    monkeypatch.setattr("omnigent.cli._run_configure_harnesses_interactive", configure)

    plan = _resolve_first_run_plan()

    assert plan is None
    configure.assert_called_once_with()  # the user was dropped into configure


def test_announce_auto_configured_credentials_names_creds_compactly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The callout names each adopted credential inline with a brand-qualified label.

    A user who never ran setup must see exactly which credentials were
    auto-configured. Asserts every credential's compact human label reaches the
    one-line output — an env key as ``Anthropic API Key``, and the two CLI
    logins as brand-qualified ``Claude Subscription`` / ``ChatGPT Subscription``
    (NOT a bare ``Subscription``, which would be ambiguous in an inline list).
    Detections are real :class:`DetectedProvider` objects so a field-handling
    regression surfaces.
    """
    detected = [
        DetectedProvider(
            name="anthropic", kind="key", family="anthropic", source="$ANTHROPIC_API_KEY"
        ),
        DetectedProvider(
            name="claude", kind="subscription", family="anthropic", source="claude CLI login"
        ),
        DetectedProvider(
            name="codex", kind="subscription", family="openai", source="codex CLI login"
        ),
    ]
    monkeypatch.setattr("omnigent.onboarding.ambient.detect_providers", lambda: detected)

    _announce_auto_configured_credentials(["anthropic", "claude", "codex"])

    # Normalize whitespace: Rich wraps the line at the console width, so collapse
    # any inserted newlines/runs of spaces before matching the inline sequence.
    normalized = " ".join(capsys.readouterr().out.split())
    assert "Found existing credentials on your machine" in normalized
    # The three credentials render inline, comma-joined, in adoption order —
    # the env key by vendor, the CLI logins brand-qualified (not a bare,
    # ambiguous "Subscription").
    assert "Anthropic API Key, Claude Subscription, ChatGPT Subscription" in normalized


def test_announce_auto_configured_credentials_empty_is_silent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing adopted → no callout at all (no stray header on a quiet run).

    The wrapper only announces when something was newly adopted; this guards
    the announce helper itself printing a bare header for an empty list.
    """
    _announce_auto_configured_credentials([])
    assert capsys.readouterr().out == ""


def test_adopt_ambient_credentials_announces_only_what_was_adopted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shared adopt step self-heals, adopts, and announces the adopted creds.

    Exercises the real announce path through the wrapper: with one credential
    newly adopted the callout names it; the databricks backfill stays silent.
    A regression that stopped calling the callout (or announced credentials
    that were not actually adopted) fails here.
    """
    monkeypatch.setattr("omnigent.cli._promote_global_auth_to_provider", Mock())
    monkeypatch.setattr("omnigent.cli._adopt_detected_providers", Mock(return_value=["anthropic"]))
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.detect_providers",
        lambda: [
            DetectedProvider(
                name="anthropic", kind="key", family="anthropic", source="$ANTHROPIC_API_KEY"
            )
        ],
    )

    adopted = _adopt_ambient_credentials()

    assert adopted == ["anthropic"]
    assert "Anthropic API Key" in capsys.readouterr().out


def test_ensure_sqlite_parent_dir_creates_missing_dir(tmp_path: Path) -> None:
    """A file-backed SQLite URI gets its parent directory created.

    Reproduces the first-run failure: the machine-global default DB lives
    at ``<data_dir>/chat.db`` and SQLite refuses to open it
    ("unable to open database file") when ``<data_dir>`` doesn't exist
    yet. The helper must create the parent so the stores can connect; we
    assert the dir exists afterward (the file itself is created later, on
    first connect, so we don't assert on it here).
    """
    db_path = tmp_path / "fresh" / "nested" / "chat.db"
    assert not db_path.parent.exists()

    _ensure_sqlite_parent_dir(f"sqlite:///{db_path}")

    assert db_path.parent.is_dir()


def test_ensure_sqlite_parent_dir_idempotent_when_dir_exists(tmp_path: Path) -> None:
    """Calling twice (dir already present) is a no-op, not an error.

    ``exist_ok=True`` semantics — a second boot against an existing data
    dir must not raise ``FileExistsError``.
    """
    db_path = tmp_path / "chat.db"
    _ensure_sqlite_parent_dir(f"sqlite:///{db_path}")
    _ensure_sqlite_parent_dir(f"sqlite:///{db_path}")  # must not raise

    assert db_path.parent.is_dir()


def test_ensure_sqlite_parent_dir_noop_for_memory_and_non_sqlite(tmp_path: Path) -> None:
    """In-memory SQLite and non-SQLite URIs create nothing and don't raise.

    ``:memory:`` has no filesystem path, and Postgres/MySQL manage their
    own storage — the helper must skip both rather than trying to mkdir a
    bogus path (which would crash a Postgres-backed deployment at boot).
    """
    # Neither call should raise, and neither should create a stray dir.
    _ensure_sqlite_parent_dir("sqlite:///:memory:")
    _ensure_sqlite_parent_dir("postgresql://user:pw@db.example.com:5432/omnigent")

    assert list(tmp_path.iterdir()) == []


def _native_dispatch_kwargs(**overrides: object) -> dict[str, object]:
    """Build ``_dispatch_native_terminal_harness`` kwargs with safe defaults.

    Keeps the call sites focused on the one or two fields each test exercises,
    and means a new parameter only updates this helper rather than every test.
    """
    base: dict[str, object] = {
        "harness": "cursor-native",
        "server": None,
        "model": None,
        "prompt": None,
        "system_prompt": None,
        "tools": None,
        "log": False,
        "debug_events": False,
        "resume_conversation_id": None,
        "resume_picker": False,
        "resume_latest": False,
        "fork_session_id": None,
        "ephemeral": False,
        "auto_open_conversation": False,
    }
    base.update(overrides)
    return base


def test_dispatch_native_terminal_harness_cursor_launches_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run --harness cursor-native`` dispatches to the cursor TUI wrapper.

    Regression for duplicate user messages: the materialized-launcher REPL
    drove an Omnigent turn (which persists its own user item) on top of the
    cursor forwarder mirroring the same message back, recording each message
    twice. Native terminal harnesses must launch their wrapper directly so the
    TUI is the single source of turns. A top-level ``--model`` is forwarded as a
    passthrough ``--model`` flag.
    """
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda _s: "http://localhost:0")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "omnigent.cursor_native.run_cursor_native",
        lambda **kwargs: captured.update(kwargs),
    )

    handled = _dispatch_native_terminal_harness(
        **_native_dispatch_kwargs(
            model="composer-2.5",
            resume_conversation_id="conv_abc123",
            auto_open_conversation=True,
        )
    )

    assert handled is True
    assert captured == {
        "server": "http://localhost:0",
        "session_id": "conv_abc123",
        "resume_picker": False,
        "auto_open_conversation": True,
        "cursor_args": ("--model", "composer-2.5"),
    }


def test_dispatch_native_terminal_harness_ignores_non_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-native harness returns False and never touches the backend.

    The SDK harnesses (``cursor``, ``codex``, ``claude-sdk``, ...) must keep
    falling through to the materialized-launcher REPL.
    """

    def _must_not_run(_server: str | None) -> str:
        raise AssertionError("_ensure_backend called for a non-native harness")

    monkeypatch.setattr("omnigent.cli._ensure_backend", _must_not_run)

    handled = _dispatch_native_terminal_harness(**_native_dispatch_kwargs(harness="openai-agents"))

    assert handled is False


@pytest.mark.parametrize(
    ("override", "expected_flag"),
    [
        ({"prompt": "hi"}, "-p/--prompt"),
        ({"system_prompt": "be terse"}, "--system-prompt"),
        ({"tools": "fs"}, "--tools"),
        ({"log": True}, "--log"),
        ({"debug_events": True}, "--debug-events"),
        ({"fork_session_id": "conv_src"}, "--fork"),
        ({"ephemeral": True}, "--no-session"),
    ],
)
def test_dispatch_native_terminal_harness_rejects_unsupported_flags(
    monkeypatch: pytest.MonkeyPatch, override: dict[str, object], expected_flag: str
) -> None:
    """REPL-only flags have no analog in the TUI wrapper — fail loud, don't drop.

    Covers Copilot's finding that ``--tools`` / ``--log`` / ``--debug-events``
    were silently ignored on the native-harness path.
    """

    def _must_not_run(_server: str | None) -> str:
        raise AssertionError("_ensure_backend called despite unsupported flags")

    monkeypatch.setattr("omnigent.cli._ensure_backend", _must_not_run)

    # The rejected flag is named in the error, and it's framed as "remove them"
    # (not "use the subcommand" — the subcommand doesn't accept these either).
    with pytest.raises(ClickException, match=re.escape(expected_flag)) as excinfo:
        _dispatch_native_terminal_harness(**_native_dispatch_kwargs(**override))
    assert "remove them" in str(excinfo.value)


def test_dispatch_native_terminal_harness_continue_resumes_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--continue`` resolves the harness's latest conversation, not an error.

    Regression guard for the resume path: before this, ``--continue`` was listed
    as unsupported and raised. It must instead resolve the most-recent
    ``cursor-native-ui`` conversation and hand that to the wrapper as the
    session id.
    """
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda _s: "http://localhost:0")
    seen: dict[str, object] = {}

    def _fake_latest(*, base_url: str, agent_name: str, headers: object) -> str:
        seen["base_url"] = base_url
        seen["agent_name"] = agent_name
        return "conv_latest"

    monkeypatch.setattr("omnigent.chat._resolve_latest_conversation_id", _fake_latest)
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda **_kw: {})
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "omnigent.cursor_native.run_cursor_native",
        lambda **kwargs: captured.update(kwargs),
    )

    handled = _dispatch_native_terminal_harness(**_native_dispatch_kwargs(resume_latest=True))

    assert handled is True
    # Resolved against the cursor-native agent identity, then passed as session_id.
    assert seen == {"base_url": "http://localhost:0", "agent_name": "cursor-native-ui"}
    assert captured["session_id"] == "conv_latest"


def test_dispatch_native_terminal_harness_continue_with_no_prior_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--continue`` with nothing to continue errors, not a silent fresh start.

    The user explicitly asked to resume; passing the unresolved ``None`` through
    as ``session_id`` would silently open a new session. Matches the REPL's
    ``_resolve_resume_target`` "No prior conversation" behavior.
    """
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda _s: "http://localhost:0")
    monkeypatch.setattr("omnigent.chat._resolve_latest_conversation_id", lambda **_kw: None)
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda **_kw: {})

    def _must_not_launch(**_kw: object) -> None:
        raise AssertionError("wrapper launched despite no conversation to continue")

    monkeypatch.setattr("omnigent.cursor_native.run_cursor_native", _must_not_launch)

    with pytest.raises(ClickException, match="No prior conversation"):
        _dispatch_native_terminal_harness(**_native_dispatch_kwargs(resume_latest=True))


def test_dispatch_native_terminal_harness_explicit_id_skips_latest_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``--resume <id>`` wins over ``--continue`` (no latest lookup)."""
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda _s: "http://localhost:0")

    def _must_not_lookup(**_kw: object) -> str:
        raise AssertionError("latest-conversation lookup ran despite an explicit id")

    monkeypatch.setattr("omnigent.chat._resolve_latest_conversation_id", _must_not_lookup)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "omnigent.cursor_native.run_cursor_native",
        lambda **kwargs: captured.update(kwargs),
    )

    _dispatch_native_terminal_harness(
        **_native_dispatch_kwargs(resume_conversation_id="conv_explicit", resume_latest=True)
    )

    assert captured["session_id"] == "conv_explicit"


def test_run_agent_with_native_terminal_harness_is_rejected() -> None:
    """``run AGENT --harness cursor-native`` is rejected (the TUI is the agent).

    The native TUI ignores the AGENT spec, and routing it through the REPL would
    double-record every message — so the combination has no coherent meaning.
    """
    with pytest.raises(ClickException, match="ignores an AGENT spec"):
        _dispatch_run(
            target="examples/hello_world.yaml",
            tools=None,
            harness="cursor-native",
            model=None,
            prompt=None,
            system_prompt=None,
        )


# ── omnigent setup: Qwen Code drill-in (_manage_qwen_harness) ────────────


def test_qwen_auth_configured_detects_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Env-var auth is detected; a fresh install with no auth is NOT.

    Guards the reported bug: a just-installed qwen (no env vars, no ~/.qwen
    creds) must report unauthenticated rather than a false "signed in".
    """
    # Point HOME at an empty dir so on-disk creds/settings are absent
    # (Path.home() honors $HOME on POSIX).
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("OPENAI_API_KEY", "BAILIAN_CODING_PLAN_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    assert _qwen_auth_configured() is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert _qwen_auth_configured() is True


def test_qwen_auth_configured_reads_settings_selected_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An auth type selected via /auth (persisted to settings.json) is detected."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("OPENAI_API_KEY", "BAILIAN_CODING_PLAN_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    qwen_dir = tmp_path / ".qwen"
    qwen_dir.mkdir()
    (qwen_dir / "settings.json").write_text(
        '{"security": {"auth": {"selectedType": "openai"}}}', encoding="utf-8"
    )

    assert _qwen_auth_configured() is True


def test_manage_qwen_harness_declines_install_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the qwen CLI is missing and the user declines, the drill-in returns.

    Declining install (choice 1) must bail without installing or launching qwen.
    """
    import omnigent.onboarding.harness_install as hi
    import omnigent.onboarding.interactive as it

    monkeypatch.setattr(hi, "harness_cli_installed", lambda key: False)
    monkeypatch.setattr(it, "console", Mock())
    install = Mock()
    monkeypatch.setattr(hi, "install_harness_cli", install)
    launch = Mock()
    monkeypatch.setattr("omnigent.cli._launch_qwen_auth", launch)
    # The install prompt offers [install, no, show-command]; pick "No".
    monkeypatch.setattr(it, "select", lambda *a, **k: 1)

    _manage_qwen_harness()

    install.assert_not_called()
    launch.assert_not_called()


def test_manage_qwen_harness_back_does_not_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the CLI installed, choosing "← Back" exits without launching qwen.

    There is no ``qwen login`` to drive — the drill-in only offers /auth launch
    and help, so a plain Back must be a clean no-op.
    """
    import omnigent.onboarding.harness_install as hi
    import omnigent.onboarding.interactive as it

    monkeypatch.setattr(hi, "harness_cli_installed", lambda key: True)
    monkeypatch.setattr("omnigent.cli._qwen_auth_configured", lambda: False)
    monkeypatch.setattr(it, "console", Mock())
    launch = Mock(return_value="x")
    monkeypatch.setattr("omnigent.cli._launch_qwen_auth", launch)
    # rows = [Open Qwen to run /auth, Show auth options, ← Back]; pick Back (2).
    monkeypatch.setattr(it, "select", lambda *a, **k: 2)

    _manage_qwen_harness()

    launch.assert_not_called()


# ── omnigent setup: Goose drill-in (_manage_goose_harness) ───────────────


def test_manage_goose_harness_missing_cli_shows_hint_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the goose CLI is missing, the drill-in shows the install hint and
    returns without launching ``goose configure`` (Goose has no npm auto-install).
    """
    import omnigent.onboarding.harness_install as hi
    import omnigent.onboarding.interactive as it

    monkeypatch.setattr(hi, "harness_cli_installed", lambda key: False)
    monkeypatch.setattr(it, "console", Mock())
    launch = Mock()
    monkeypatch.setattr("omnigent.cli._launch_goose_configure", launch)
    # Should never reach the select() menu when the CLI is absent.
    monkeypatch.setattr(it, "select", Mock(side_effect=AssertionError("select called")))

    _manage_goose_harness()

    launch.assert_not_called()


def test_manage_goose_harness_back_does_not_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the CLI installed, choosing "← Back" exits without launching configure."""
    import omnigent.onboarding.goose_auth as ga
    import omnigent.onboarding.harness_install as hi
    import omnigent.onboarding.interactive as it

    monkeypatch.setattr(hi, "harness_cli_installed", lambda key: True)
    monkeypatch.setattr(
        ga,
        "goose_config_summary",
        lambda: ga.GooseConfigSummary(installed=True, provider=None, model=None),
    )
    monkeypatch.setattr(it, "console", Mock())
    launch = Mock(return_value="x")
    monkeypatch.setattr("omnigent.cli._launch_goose_configure", launch)
    # rows = [Run goose configure, Show configuration options, ← Back]; pick Back (2).
    monkeypatch.setattr(it, "select", lambda *a, **k: 2)

    _manage_goose_harness()

    launch.assert_not_called()


def test_manage_goose_harness_configure_launches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing "Run goose configure" launches the configure flow, then exits."""
    import omnigent.onboarding.goose_auth as ga
    import omnigent.onboarding.harness_install as hi
    import omnigent.onboarding.interactive as it

    monkeypatch.setattr(hi, "harness_cli_installed", lambda key: True)
    monkeypatch.setattr(
        ga,
        "goose_config_summary",
        lambda: ga.GooseConfigSummary(installed=True, provider="anthropic", model="claude-x"),
    )
    monkeypatch.setattr(it, "console", Mock())
    launch = Mock(return_value="✓ provider configured: anthropic")
    monkeypatch.setattr("omnigent.cli._launch_goose_configure", launch)
    # First iteration: pick "Run goose configure" (0); second: "← Back" (2).
    choices = iter([0, 2])
    monkeypatch.setattr(it, "select", lambda *a, **k: next(choices))

    _manage_goose_harness()

    launch.assert_called_once()


# ── omnigent setup: Kimi Code drill-in (_manage_kimi_harness) ────────────


def test_manage_kimi_harness_not_installed_shows_hint_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing kimi CLI shows the curl install_hint and returns.

    Kimi is curl-installed (no npm ``package``), so the drill-in can't
    auto-install it — it must surface the install_hint and bail without
    touching login / logout.
    """
    import omnigent.onboarding.harness_install as hi
    import omnigent.onboarding.interactive as it

    monkeypatch.setattr(hi, "harness_cli_installed", lambda key: False)
    console = Mock()
    monkeypatch.setattr(it, "console", console)
    login = Mock()
    logout = Mock()
    monkeypatch.setattr(hi, "harness_login", login)
    monkeypatch.setattr(hi, "harness_logout", logout)
    # If the drill-in wrongly reached the menu loop, this select would drive it.
    monkeypatch.setattr(it, "select", lambda *a, **k: 0)

    _manage_kimi_harness()

    login.assert_not_called()
    logout.assert_not_called()
    # The curl install command was surfaced to the user.
    printed = " ".join(str(c.args[0]) for c in console.print.call_args_list if c.args)
    assert "code.kimi.com/kimi-code/install.sh" in printed


def test_manage_kimi_harness_back_does_not_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the CLI installed, choosing "← Back" exits without signing in."""
    import omnigent.onboarding.harness_install as hi
    import omnigent.onboarding.interactive as it

    monkeypatch.setattr(hi, "harness_cli_installed", lambda key: True)
    monkeypatch.setattr(it, "console", Mock())
    login = Mock()
    logout = Mock()
    monkeypatch.setattr(hi, "harness_login", login)
    monkeypatch.setattr(hi, "harness_logout", logout)
    # rows = [Sign in, Sign out, Show auth options, ← Back]; pick Back (3).
    monkeypatch.setattr(it, "select", lambda *a, **k: 3)

    _manage_kimi_harness()

    login.assert_not_called()
    logout.assert_not_called()


def test_manage_kimi_harness_login_runs_kimi_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting "Sign in" drives ``harness_login(KIMI_KEY)`` then loops; Back exits."""
    import omnigent.onboarding.harness_install as hi
    import omnigent.onboarding.interactive as it

    monkeypatch.setattr(hi, "harness_cli_installed", lambda key: True)
    monkeypatch.setattr(it, "console", Mock())
    login = Mock(return_value=False)  # kimi has no status probe; return is ignored
    logout = Mock()
    monkeypatch.setattr(hi, "harness_login", login)
    monkeypatch.setattr(hi, "harness_logout", logout)
    # First iteration: Sign in (0); second: ← Back (3) to exit the loop.
    choices = iter([0, 3])
    monkeypatch.setattr(it, "select", lambda *a, **k: next(choices))

    _manage_kimi_harness()

    login.assert_called_once_with(hi.KIMI_KEY)
    logout.assert_not_called()
