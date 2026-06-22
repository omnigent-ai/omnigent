"""Tests for ``omnigent._runner_startup`` (startup UX helpers).

These cover the two pieces of UX added when local-runner startup
fails or stalls:

1. ``format_runner_log_tail`` — the failure-message helper that
   surfaces the captured runner log path and a tail of its
   contents on the ``ClickException``.
2. ``runner_startup_progress`` — the context manager that wraps
   the runner-spawn → registration-wait window with a rich
   spinner on a TTY, falling back to plain ``click.echo`` lines
   off-TTY.

The behavior assertions matter because a regression in either
helper degrades the diagnosis story for "my runner won't start"
back to the opaque pre-fix state ("Local runner did not register
within 60s.").
"""

from __future__ import annotations

import re
import sys

import pytest

from omnigent._runner_startup import (
    _NO_SPINNER_ENV_VAR,
    STARTUP_PHASE_LABELS,
    _spinner_enabled,
    format_runner_log_tail,
    runner_startup_progress,
)

# ---------------------------------------------------------------------------
# format_runner_log_tail
# ---------------------------------------------------------------------------


def test_format_runner_log_tail_none_path_returns_empty_string() -> None:
    """``log_path=None`` means no log was captured; suppress the block.

    Callers concatenate the return value onto a ``ClickException``
    message, so an empty string is the right signal that nothing
    should be appended.

    :returns: None.
    """
    assert format_runner_log_tail(None) == ""


def test_format_runner_log_tail_surfaces_path_only(tmp_path) -> None:
    """A real log path is surfaced as a single ``Runner log:`` line.

    The helper deliberately does NOT include log contents inline:
    a wall of log lines drowns the actual error summary, and the
    log file is one ``cat`` away once the user has the path. This
    test pins the policy so a future regression that re-introduces
    inline log tailing fails loud.

    :param tmp_path: Pytest tmp dir fixture.
    :returns: None.
    """
    log = tmp_path / "runner.log"
    log.write_text("ERROR: tunnel rejected (HTTP 401)\n")
    out = format_runner_log_tail(log)
    # The path is named explicitly so the user can ``cat`` it.
    assert out == f"\nRunner log: {log}"
    # Content of the log MUST NOT leak into the error message —
    # that was the previous, overwhelming behavior.
    assert "ERROR: tunnel rejected" not in out


def test_format_runner_log_tail_does_not_require_existing_file(tmp_path) -> None:
    """The helper does not stat the path before surfacing it.

    The runner may fail before its log file actually lands on
    disk (fork error, bad cwd, immediate import crash). We still
    want to surface the configured log location so the user can
    see where we *expected* the log to live — "file is missing"
    is itself useful diagnostic information.

    :param tmp_path: Pytest tmp dir fixture.
    :returns: None.
    """
    missing = tmp_path / "never-existed.log"
    assert format_runner_log_tail(missing) == f"\nRunner log: {missing}"


# ---------------------------------------------------------------------------
# _spinner_enabled (TTY + env-var policy)
# ---------------------------------------------------------------------------


def test_spinner_enabled_requires_a_tty() -> None:
    """Off a TTY (CI logs, piped stderr) we must not emit a spinner.

    A spinner's cursor-rewrites would render as raw escape codes
    in a captured log, defeating the point of capturing it.

    :returns: None.
    """
    assert _spinner_enabled(stream_isatty=False, env={}) is False


def test_spinner_enabled_on_tty_default() -> None:
    """On a TTY with no opt-out, the spinner renders.

    :returns: None.
    """
    assert _spinner_enabled(stream_isatty=True, env={}) is True


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_spinner_disabled_by_env_var(value: str) -> None:
    """Any truthy ``OMNIGENT_NO_SPINNER`` value disables the spinner.

    Lets users with mis-detecting terminals (tmux quirks, ssh into
    bare containers) force the plain-echo fallback without code
    changes.

    :param value: Truthy spelling under test.
    :returns: None.
    """
    assert _spinner_enabled(stream_isatty=True, env={_NO_SPINNER_ENV_VAR: value}) is False


def test_spinner_not_disabled_by_falsy_env_var() -> None:
    """``OMNIGENT_NO_SPINNER=0`` (or empty) leaves the spinner on.

    Mirrors typical "0 is off, 1 is on" UX so users do not
    accidentally suppress the spinner by exporting the variable
    with the value they meant as a disable.

    :returns: None.
    """
    assert _spinner_enabled(stream_isatty=True, env={_NO_SPINNER_ENV_VAR: "0"}) is True
    assert _spinner_enabled(stream_isatty=True, env={_NO_SPINNER_ENV_VAR: ""}) is True


# ---------------------------------------------------------------------------
# runner_startup_progress
# ---------------------------------------------------------------------------


def test_runner_startup_progress_plain_mode_prints_to_stderr(
    capsys,
) -> None:
    """Off-TTY: initial + every ``.update`` call go to stderr.

    Plain-mode lines stay in scrollback so a CI log capture
    reflects the same phase transitions a user would see
    interactively.

    :param capsys: Pytest stdio capture fixture.
    :returns: None.
    """
    with runner_startup_progress(
        initial_message="Starting local runner\u2026",
        enabled=False,
    ) as p:
        p.update("Waiting for runner to register with example.com\u2026")

    captured = capsys.readouterr()
    # Nothing leaks to stdout (one-shot ``-p`` callers pipe stdout
    # to consumers that should see only the agent's reply).
    assert captured.out == ""
    # Both phases land on stderr with the standard prefix.
    assert "omnigent: Starting local runner" in captured.err
    assert "omnigent: Waiting for runner to register" in captured.err


def test_runner_startup_progress_plain_mode_does_not_swallow_exceptions(
    capsys,
) -> None:
    """An exception inside the body propagates with no extra wrapping.

    The context manager only owns the renderer — it must not turn
    a real runner-startup failure into a generic "context exited
    with error" message.

    :param capsys: Pytest stdio capture fixture.
    :returns: None.
    """
    with pytest.raises(RuntimeError, match="boom"):
        with runner_startup_progress(
            initial_message="Starting\u2026",
            enabled=False,
        ):
            raise RuntimeError("boom")
    # The initial message still printed; the exception did not
    # suppress it.
    assert "omnigent: Starting" in capsys.readouterr().err


def test_runner_startup_progress_rich_mode_writes_only_to_stderr(
    capsys,
) -> None:
    """Spinner output goes to stderr; stdout stays clean.

    Stdout cleanliness matters for ``run --server -p "\u2026"`` where the
    one-shot prints the agent's reply to stdout; we cannot let a
    spinner contaminate that stream.

    :param capsys: Pytest stdio capture fixture.
    :returns: None.
    """
    with runner_startup_progress(
        initial_message="Starting\u2026",
        enabled=True,
    ) as p:
        p.update("Waiting\u2026")

    captured = capsys.readouterr()
    assert captured.out == ""
    # Rich's transient spinner emits ANSI escape sequences while
    # running, but on successful exit the line is cleared. We do
    # NOT assert the post-exit stream is byte-empty (a CR or
    # cursor-restore sequence may remain). The contract that
    # matters for the user is: stdout is clean, and the visible
    # cursor row is empty when the context exits.


# ---------------------------------------------------------------------------
# Cold-start phase labels (run / chat startup UX)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", STARTUP_PHASE_LABELS)
def test_startup_phase_labels_avoid_internal_jargon(label: str) -> None:
    """Cold-start labels must stay plain-language, not architecture terms.

    The whole point of the ``run`` / ``chat`` startup spinner is to make
    the silent cold-start gap read as ordinary forward motion to a user
    who does not (and should not need to) know the framework's internals.
    A regression that surfaces an internal term — "Waiting for runner
    tunnel registration…", "Spawning host daemon…" — defeats that, so
    pin the intent: none of the labels may contain an internal term.

    A failure here means a phase label was changed to leak an
    architecture word; rename it to something a non-developer user would
    understand.

    :param label: One cold-start phase label under test, e.g.
        ``"Starting the local server…"``.
    :returns: None.
    """
    # Internal nouns that mean nothing to an end user staring at startup.
    # "server" is intentionally NOT here: "local server" is user-facing
    # vocabulary (it is the ``omnigent server`` they may run directly).
    jargon = {
        "daemon",
        "host",
        "runner",
        "tunnel",
        "websocket",
        "socket",
        "rpc",
        "bundle",
        "registration",
        "executor",
        "subprocess",
    }
    words = set(re.findall(r"[a-z]+", label.lower()))
    leaked = words & jargon
    assert not leaked, (
        f"Startup label {label!r} leaks internal jargon {sorted(leaked)}. "
        "Cold-start labels are shown to end users; keep them plain-language."
    )


def test_startup_phase_labels_render_in_order_plain_mode(capsys) -> None:
    """Driving the spinner with the real labels emits them in sequence.

    This is the contract the ``cli._ensure_backend`` /
    ``chat._prepare_chat_session_via_daemon`` wiring relies on: each
    ``update`` with a ``STARTUP_PHASE_*`` constant produces a distinct
    stderr line, in call order, with stdout untouched (so one-shot
    ``-p`` stdout stays clean).

    A failure means the progress helper stopped forwarding ``update``
    calls to stderr in order, or started leaking onto stdout.

    :param capsys: Pytest stdio capture fixture.
    :returns: None.
    """
    with runner_startup_progress(
        initial_message=STARTUP_PHASE_LABELS[0],
        enabled=False,
    ) as p:
        for label in STARTUP_PHASE_LABELS[1:]:
            p.update(label)

    captured = capsys.readouterr()
    # stdout stays clean — the spinner is a stderr-only affordance.
    assert captured.out == ""
    # Every label landed on stderr, and in the order it was emitted (the
    # find-index of each label is strictly increasing). A wrong order
    # would mean the helper buffered/reordered updates.
    indices = [captured.err.find(label) for label in STARTUP_PHASE_LABELS]
    assert all(i != -1 for i in indices), (
        f"Not all labels reached stderr. Indices: {indices}. stderr was:\n{captured.err}"
    )
    assert indices == sorted(indices), (
        f"Labels rendered out of order. Indices: {indices}. stderr was:\n{captured.err}"
    )


def test_runner_startup_progress_auto_detects_tty(monkeypatch, capsys) -> None:
    """``enabled=None`` consults ``sys.stderr.isatty()``.

    Production callers leave ``enabled`` unset; this test pins the
    contract that a non-TTY stderr in pytest's captured environment
    falls through to plain-echo mode.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param capsys: Pytest stdio capture fixture.
    :returns: None.
    """
    # Under pytest capture stderr.isatty() is already False, but be
    # explicit so the test does not depend on capture mode.
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    with runner_startup_progress(initial_message="Starting\u2026"):
        pass
    assert "omnigent: Starting" in capsys.readouterr().err
