"""Tests for the always-on CLI diagnostics log."""

from __future__ import annotations

import io
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from omnigent import cli_diagnostics


@dataclass(frozen=True)
class _LoggerSnapshot:
    """
    Logger state captured before a test configures CLI diagnostics.

    :param handlers: Handlers present before the test started.
    :param level: Numeric logging level configured before the test started.
    :param propagate: Whether the logger propagated before the test started.
    """

    handlers: list[logging.Handler]
    level: int
    propagate: bool


class _FailingRedirectedStderr:
    """
    Stderr stub whose close path fails after exposing the original stream.

    :param original: Terminal stderr stream that ``restore_stderr`` should
        restore before attempting to close the redirected stream.
    """

    def __init__(self, original: io.TextIOBase) -> None:
        """
        Create the failing redirected stderr stub.

        :param original: Terminal stderr stream saved for restoration.
        :returns: ``None``.
        """
        self._original_stderr = original

    def close(self) -> None:
        """
        Raise the close failure that ``restore_stderr`` must log.

        :returns: ``None``.
        :raises OSError: Always, to exercise the diagnostics path.
        """
        raise OSError("close failed")


def _capture_logger_snapshots() -> dict[str, _LoggerSnapshot]:
    """
    Capture package logger state mutated by ``setup_cli_logging``.

    :returns: Snapshot keyed by logger name.
    """
    snapshots: dict[str, _LoggerSnapshot] = {}
    for name in ("", "omnigent", "omnigent_ui_sdk", "databricks.sdk"):
        logger = logging.getLogger(name)
        snapshots[name] = _LoggerSnapshot(
            handlers=list(logger.handlers),
            level=logger.level,
            propagate=logger.propagate,
        )
    return snapshots


def _restore_logger_snapshots(snapshots: dict[str, _LoggerSnapshot]) -> None:
    """
    Restore package loggers after ``setup_cli_logging`` added file handlers.

    :param snapshots: Logger state returned by
        :func:`_capture_logger_snapshots`.
    :returns: ``None``.
    """
    for name, snapshot in snapshots.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            if handler not in snapshot.handlers:
                handler.close()
        for handler in snapshot.handlers:
            logger.addHandler(handler)
        logger.setLevel(snapshot.level)
        logger.propagate = snapshot.propagate


@pytest.fixture
def isolated_cli_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """
    Isolate CLI diagnostics global logging state for a test.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary home directory root for diagnostics logs.
    :returns: Iterator yielding control to the test body.
    """
    snapshots = _capture_logger_snapshots()
    original_stderr = sys.stderr
    monkeypatch.setenv("HOME", str(tmp_path))
    yield
    cli_diagnostics.restore_stderr()
    sys.stderr = original_stderr
    _restore_logger_snapshots(snapshots)


def test_redirect_stderr_to_log_redacts_direct_stderr_writes(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Redirected raw stderr writes must honor the diagnostics redaction contract.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])

    cli_diagnostics.redirect_stderr_to_log()
    print("MY_API_KEY=super-secret-token", file=sys.stderr)
    sys.stderr.write("Authorization: Bearer sk-directsecret12345\n")
    sys.stderr.flush()
    cli_diagnostics.restore_stderr()

    log_text = ctx.path.read_text(encoding="utf-8")
    assert "super-secret-token" not in log_text, (
        f"redirected stderr leaked an API key into the CLI diagnostics log: {log_text!r}"
    )
    assert "sk-directsecret12345" not in log_text, (
        f"redirected stderr leaked an SDK token into the CLI diagnostics log: {log_text!r}"
    )
    assert "[REDACTED]" in log_text, (
        f"redirected stderr should preserve context with redacted values, got: {log_text!r}"
    )
    assert terminal_stderr.getvalue() == "", (
        f"redirected stderr should not paint into the terminal, got: "
        f"{terminal_stderr.getvalue()!r}"
    )


def test_restore_stderr_returns_writes_to_original_terminal(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``restore_stderr`` must return subsequent writes to the original stream.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])

    cli_diagnostics.redirect_stderr_to_log()
    print("during-tui", file=sys.stderr)
    cli_diagnostics.restore_stderr()
    print("after-tui", file=sys.stderr)

    log_text = ctx.path.read_text(encoding="utf-8")
    assert "during-tui" in log_text, (
        f"stderr written during the TUI lifetime should land in the log: {log_text!r}"
    )
    assert "after-tui" not in log_text, (
        f"stderr written after restore should not keep landing in the log: {log_text!r}"
    )
    assert terminal_stderr.getvalue() == "after-tui\n", (
        f"stderr was not restored to the original terminal stream: {terminal_stderr.getvalue()!r}"
    )


def test_log_cli_error_hint_uses_original_stderr_when_redirected(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Fatal-error hints must stay visible even while TUI stderr is redirected.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])
    cli_diagnostics.redirect_stderr_to_log()

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        cli_diagnostics.log_cli_error_hint(exc)

    cli_diagnostics.restore_stderr()

    hint = terminal_stderr.getvalue()
    assert hint == f"Details logged to {ctx.path}\n", (
        f"fatal-error hint should print to the original stderr stream, got: {hint!r}"
    )
    log_text = ctx.path.read_text(encoding="utf-8")
    assert "Fatal CLI error: boom" in log_text, (
        f"fatal exception context was not written to the diagnostics log: {log_text!r}"
    )
    assert "Details logged to" not in log_text, (
        f"user-facing fatal-error hint should not be redirected into the log: {log_text!r}"
    )


def test_redirect_stderr_to_log_retargets_existing_logging_stderr_handlers(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Existing stderr-backed logging handlers must follow TUI stderr redirect.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(terminal_stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    databricks_logger = logging.getLogger("databricks.sdk")
    databricks_logger.handlers.clear()
    databricks_logger.setLevel(logging.WARNING)
    databricks_logger.propagate = True
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])

    cli_diagnostics.redirect_stderr_to_log()
    databricks_logger.warning(
        "Databricks CLI v0.295.0 does not support --force-refresh "
        "(requires >= v0.296.0). The CLI's token cache may provide stale tokens."
    )
    cli_diagnostics.restore_stderr()
    databricks_logger.warning("after TUI")

    log_text = ctx.path.read_text(encoding="utf-8")
    warning_line = (
        "WARNING:databricks.sdk:Databricks CLI v0.295.0 does not support --force-refresh"
    )
    assert warning_line in log_text, (
        f"stderr-backed third-party logging should land in the CLI log: {log_text!r}"
    )
    assert "after TUI" not in log_text, (
        f"logging handlers should be restored after the TUI exits: {log_text!r}"
    )
    assert terminal_stderr.getvalue() == "WARNING:databricks.sdk:after TUI\n", (
        f"third-party logging painted into the terminal during TUI redirect: "
        f"{terminal_stderr.getvalue()!r}"
    )


def test_redirect_stderr_logs_open_failures(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``redirect_stderr_to_log`` must record failures instead of swallowing them.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])

    def _raise_open_failure(*_args: object, **_kwargs: object) -> io.TextIOWrapper:
        """
        Stand in for ``open`` when the redirected stderr file cannot be opened.

        :param _args: Positional arguments passed by ``redirect_stderr_to_log``.
        :param _kwargs: Keyword arguments passed by ``redirect_stderr_to_log``.
        :returns: Never returns.
        :raises OSError: Always, to exercise the diagnostics path.
        """
        raise OSError("open failed")

    monkeypatch.setattr(cli_diagnostics, "open", _raise_open_failure, raising=False)

    cli_diagnostics.redirect_stderr_to_log()

    assert sys.stderr is terminal_stderr, (
        "redirect_stderr_to_log should leave stderr alone when opening the diagnostics file fails."
    )
    log_text = ctx.path.read_text(encoding="utf-8")
    assert "Failed to redirect stderr to CLI log: open failed" in log_text, (
        f"stderr redirect setup failures must be captured in the diagnostics log: {log_text!r}"
    )
    assert "Traceback" in log_text, (
        f"stderr redirect setup failures should include traceback context: {log_text!r}"
    )


def test_restore_stderr_logs_close_failures(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``restore_stderr`` must log close failures after restoring the terminal.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])
    monkeypatch.setattr(sys, "stderr", _FailingRedirectedStderr(terminal_stderr))

    cli_diagnostics.restore_stderr()

    assert sys.stderr is terminal_stderr, (
        "restore_stderr should restore the original terminal stream before "
        "closing the redirected stream."
    )
    log_text = ctx.path.read_text(encoding="utf-8")
    assert "Failed to close redirected stderr: close failed" in log_text, (
        f"redirected stderr close failures must be captured in the diagnostics log: {log_text!r}"
    )
    assert "Traceback" in log_text, (
        f"redirected stderr close failures should include traceback context: {log_text!r}"
    )


def test_main_logs_click_exceptions(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Click-handled command errors must still reach the diagnostics log.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param capsys: Pytest capture fixture for terminal stderr.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    from omnigent import cli as cli_module

    # An unsupported --harness is a deterministic ClickException trigger that
    # raises before any daemon/network work. (A bare `omnigent run` no longer
    # errors — it drops into first-run `configure harnesses` — so it can't be
    # the trigger here.)
    monkeypatch.setattr(sys, "argv", ["omnigent", "run", "--harness", "not-a-real-harness"])
    # Isolate from any real ~/.omnigent/config.yaml on the developer's machine.
    monkeypatch.setattr(cli_module, "_load_global_config", dict)

    with pytest.raises(SystemExit) as exc_info:
        cli_module.main()

    assert exc_info.value.code == 1, (
        f"ClickException should preserve Click's exit code, got {exc_info.value.code!r}"
    )
    terminal = capsys.readouterr()
    assert "Error: Unsupported harness 'not-a-real-harness'" in terminal.err, (
        f"Click's normal user-facing error output changed: {terminal.err!r}"
    )
    path = cli_diagnostics.current_cli_log_path()
    assert path is not None, "main() should set up the active CLI diagnostics log."
    log_text = path.read_text(encoding="utf-8")
    assert "Click CLI error: Unsupported harness 'not-a-real-harness'" in log_text, (
        f"ClickException was not captured in the diagnostics log: {log_text!r}"
    )
    assert "Traceback" in log_text, (
        f"ClickException log entry should include traceback context: {log_text!r}"
    )


@pytest.mark.asyncio
async def test_slash_command_exceptions_reach_cli_log(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    REPL slash-command exceptions must be recorded in the CLI diagnostics log.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    from omnigent_ui_sdk import RichBlockFormatter

    from omnigent.repl._repl import handle_slash_command
    from tests.repl.helpers import CapturingHost

    class _SessionWithoutModelSetter:
        """
        Session stub matching the broken adapter surface from the REPL.

        It intentionally exposes ``model_override`` and ``is_streaming``
        but not ``set_model_override`` so ``/model <name>`` raises the
        production AttributeError this regression covers.
        """

        model_override: str | None = None
        is_streaming = False

    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])
    host = CapturingHost()

    await handle_slash_command(
        "/model openai/gpt-5.4-mini",
        _SessionWithoutModelSetter(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        host,
        RichBlockFormatter(),
    )

    assert "Error: '_SessionWithoutModelSetter' object has no attribute" in host.text, (
        f"slash-command failures should still render inline for the user: {host.text!r}"
    )
    log_text = ctx.path.read_text(encoding="utf-8")
    assert "Slash command failed: /model" in log_text, (
        f"slash-command failures must be captured in the diagnostics log: {log_text!r}"
    )
    assert "AttributeError" in log_text, (
        f"slash-command diagnostics should include the exception type: {log_text!r}"
    )
    assert "openai/gpt-5.4-mini" not in log_text, (
        f"slash-command diagnostics should not copy command arguments into the log: {log_text!r}"
    )


def test_safe_mtime_returns_zero_for_vanished_file(tmp_path: Path) -> None:
    """A file present at glob time but gone before stat resolves to 0.0, not a raise."""
    real = tmp_path / "cli-real.log"
    real.write_text("x")
    assert cli_diagnostics._safe_mtime(real) > 0.0
    assert cli_diagnostics._safe_mtime(tmp_path / "cli-gone.log") == 0.0


def test_prune_old_logs_survives_file_vanishing_mid_sort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent ``omnigent run`` launches race to prune the same logs; a file
    globbed by one but deleted by the other must not crash the sort (previously a
    FileNotFoundError in the stat sort key aborted CLI startup)."""
    real = [tmp_path / f"cli-{i:03d}.log" for i in range(cli_diagnostics.MAX_LOG_FILES + 3)]
    for p in real:
        p.write_text("x")
    vanished = tmp_path / "cli-vanished.log"  # globbed, then deleted by a peer run
    monkeypatch.setattr(Path, "glob", lambda self, pattern: [*real, vanished])

    cli_diagnostics._prune_old_logs(tmp_path)  # must not raise

    surviving = [p for p in real if p.exists()]
    assert len(surviving) == cli_diagnostics.MAX_LOG_FILES  # newest kept, oldest pruned
