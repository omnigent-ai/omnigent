"""Shared UX helpers for local-runner startup.

The CLI flows that spawn the laptop-side runner subprocess
(``omnigent run --server``, ``omnigent claude --server``)
share two needs:

1. A progress indicator while waiting for the runner to come up,
   so a slow cold-start is not silent. The indicator must clear
   itself on success — once the runner is online there should be
   no leftover text in the terminal (the REPL or the Claude PTY
   takes over and any residual line would corrupt the UI).

2. A clear failure message that points the user at the captured
   runner log when something goes wrong. Today the timeout
   surface is ``"Local runner did not register within 60s"`` and
   the log file (with the actual root cause) sits unreferenced in
   ``~/.omnigent/logs/runner/``.

Both behaviors live here so the run / claude / runner paths
stay in sync.

The progress context manager is also reused for the broader
``run`` / ``claude`` cold-start path — ``_ensure_backend`` (daemon
spawn + local-server boot) and ``_prepare_chat_session_via_daemon``
(agent upload + runner bring-up) — so the user sees plain-language
forward motion ("Starting up…", "Starting the local server…",
"Connecting…", "Launching your agent…") instead of a silent gap.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path

import click
from rich.console import Console

# Env var users can set to force-disable the spinner even on a TTY.
# Useful for CI captures that still allocate a PTY, and for the
# integration-test harnesses that pipe stderr through their own
# log collectors.
_NO_SPINNER_ENV_VAR = "OMNIGENT_NO_SPINNER"

# User-facing labels for the ``run`` / ``claude`` cold-start sequence,
# in the order the user sees them. Single source of truth so the
# ``cli`` (backend bring-up) and ``chat`` (agent/runner bring-up)
# call sites stay consistent. Deliberately plain language: a waiting
# user should read ordinary forward motion, not internal architecture
# terms (daemon, host, runner, tunnel, socket, bundle). The
# ``test_startup_phase_labels_avoid_internal_jargon`` test pins that
# intent so a future "Waiting for runner tunnel registration…" label
# fails loud.
STARTUP_PHASE_STARTING = "Starting up…"
STARTUP_PHASE_LOCAL_SERVER = "Starting the local server…"
STARTUP_PHASE_CONNECTING_REMOTE = "Connecting to the server…"
STARTUP_PHASE_PREPARING_AGENT = "Preparing your agent…"
STARTUP_PHASE_CONNECTING = "Connecting…"
# Also the label held through the tail of bring-up (session attach + the
# wrapper-redirect probe) right before the REPL paints: rather than clear
# the spinner into an empty gap there, we keep this last real phase on
# screen. The label lags the exact step, which reads better than inventing
# a vaguer "Almost ready…".
STARTUP_PHASE_LAUNCHING_AGENT = "Launching your agent…"

# All cold-start labels, for the jargon guard test. Not an enum: each
# constant is referenced by name at its call site for readability.
STARTUP_PHASE_LABELS: tuple[str, ...] = (
    STARTUP_PHASE_STARTING,
    STARTUP_PHASE_LOCAL_SERVER,
    STARTUP_PHASE_CONNECTING_REMOTE,
    STARTUP_PHASE_PREPARING_AGENT,
    STARTUP_PHASE_CONNECTING,
    STARTUP_PHASE_LAUNCHING_AGENT,
)


def _noop() -> None:
    """No-op default for :attr:`RunnerStartupProgress.finish`.

    :returns: None.
    """


@dataclass
class RunnerStartupProgress:
    """
    Handle returned by :func:`runner_startup_progress`.

    Callers update the progress message via :meth:`update`. The
    underlying renderer (rich spinner or plain echo) is owned by
    the context manager and torn down on context exit — or earlier
    via :meth:`finish`, when the caller needs the spinner gone before
    the context block ends (e.g. right before the REPL takes over the
    terminal, so the spinner doesn't linger across the hand-off).

    :param update: Set the current progress message, e.g.
        ``progress.update("Starting local runner…")``.
    :param finish: Tear the renderer down now (idempotent). Calling it
        is optional — the context manager also tears down on exit — but
        it lets a caller end the spinner at a precise point mid-block.
        Defaults to a no-op so direct constructions stay valid.
    """

    update: Callable[[str], None]
    finish: Callable[[], None] = _noop


def _spinner_enabled(stream_isatty: bool, env: dict[str, str]) -> bool:
    """
    Decide whether to render the rich spinner.

    :param stream_isatty: Result of ``sys.stderr.isatty()`` at the
        call site. Threaded as a parameter so tests can exercise
        both branches without mocking ``sys``.
    :param env: Environment snapshot, e.g. ``dict(os.environ)``.
        ``OMNIGENT_NO_SPINNER`` (any truthy value) force-disables
        the spinner.
    :returns: ``True`` if the spinner should render, ``False`` if
        the caller should fall back to plain ``click.echo`` lines.
    """
    if not stream_isatty:
        return False
    raw = env.get(_NO_SPINNER_ENV_VAR, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return False
    return True


@contextlib.contextmanager
def runner_startup_progress(
    *,
    initial_message: str,
    enabled: bool | None = None,
) -> Generator[RunnerStartupProgress, None, None]:
    """
    Context manager that renders runner-startup progress.

    On a TTY (and absent ``OMNIGENT_NO_SPINNER``) a rich spinner
    animates on stderr; the line is cleared on context exit so a
    successful startup leaves nothing behind. Off a TTY (CI, piped
    stderr) the helper falls back to plain ``click.echo`` updates
    on stderr so logs stay readable.

    Typical use lives inside :func:`omnigent.chat._wait_for_remote_runner`,
    which wraps the poll loop with this context manager so every
    runner-spawning flow (``run --server``, ``claude --server``)
    gets the same UX::

        with runner_startup_progress(initial_message="Starting…"):
            _poll_remote_runner(...)
        # On success, nothing remains on screen here.

    Callers can update the message mid-flight via ``.update`` when
    they want to surface phase transitions to the user.

    :param initial_message: First line shown when the context
        opens, e.g. ``"Starting local runner…"``.
    :param enabled: Force the renderer choice. ``None`` (default)
        auto-detects from ``sys.stderr.isatty()`` and
        ``OMNIGENT_NO_SPINNER``. ``True`` always renders the
        spinner; ``False`` always falls back to plain echo. Used
        by tests; production callers should leave this ``None``.
    :yields: A :class:`RunnerStartupProgress` whose ``update``
        callback sets the current message.
    """
    if enabled is None:
        enabled = _spinner_enabled(
            sys.stderr.isatty(),
            dict(os.environ),
        )

    if enabled:
        from rich.live import Live
        from rich.spinner import Spinner

        from omnigent.inner.mascots import MASCOT_ART_COLOR

        # ``Console(stderr=True)`` keeps the spinner off stdout so piped
        # one-shot output (``omnigent run … -p "…"``) stays clean.
        # ``transient=True`` erases the spinner line on stop. We drive a
        # ``Live`` directly (rather than ``console.status``) for two
        # reasons: (1) ``finish()`` can stop it mid-block, so one spinner
        # can span several backend steps and only clear right before the
        # REPL paints — no empty gap between steps; (2)
        # ``redirect_stdout/stderr=False`` leaves the process std streams
        # unwrapped (our startup spans are silent), so the hand-off to the
        # prompt-toolkit REPL — which manages stdout via ``patch_stdout``
        # and a CPR handshake — isn't disturbed by a stream proxy being
        # torn down a frame before the prompt's first paint.
        console = Console(stderr=True)
        spinner = Spinner("dots", text=initial_message, style=MASCOT_ART_COLOR)
        live = Live(
            spinner,
            console=console,
            transient=True,
            redirect_stdout=False,
            redirect_stderr=False,
            refresh_per_second=12.5,
        )
        live.start()
        _stopped = [False]

        def _update_rich(msg: str) -> None:
            """
            Replace the spinner's current label.

            :param msg: New progress message, e.g.
                ``"Launching your agent…"``.
            :returns: None.
            """
            spinner.update(text=msg)

        def _finish_rich() -> None:
            """
            Stop and erase the spinner now (idempotent).

            :returns: None.
            """
            if _stopped[0]:
                return
            _stopped[0] = True
            live.stop()

        try:
            yield RunnerStartupProgress(update=_update_rich, finish=_finish_rich)
        finally:
            _finish_rich()
        return

    # Plain mode: each ``update`` prints a fresh line on stderr.
    # No clear-on-success because the line was the user's only
    # signal that something was happening — leaving it in the
    # scrollback is the right behavior for a log capture.
    click.echo(f"omnigent: {initial_message}", err=True)

    def _update_plain(msg: str) -> None:
        """
        Print a new progress line on stderr.

        :param msg: Progress message, e.g.
            ``"Launching your agent…"``.
        :returns: None.
        """
        click.echo(f"omnigent: {msg}", err=True)

    # Plain mode has no live region to tear down, so ``finish`` is a
    # no-op (the printed lines stay in scrollback by design).
    yield RunnerStartupProgress(update=_update_plain, finish=_noop)


def format_runner_log_tail(log_path: Path | None) -> str:
    """
    Build a one-line log-path hint for failure messages.

    The returned string always starts with a leading newline so it
    can be concatenated directly onto a ``click.ClickException``
    message without manual spacing. The intent is to point the
    user at the captured log file without dumping a wall of log
    lines into the terminal — those make the actual error summary
    hard to spot.

    The function name still carries ``_tail`` for historical
    reasons (an earlier version included the trailing N lines)
    so the call sites in ``omnigent.chat`` and
    ``omnigent.cli`` keep working unchanged.

    :param log_path: Path to the captured runner log, e.g.
        ``Path("/home/u/.omnigent/logs/runner/runner-abcd.log")``.
        ``None`` returns an empty string (no log was captured —
        usually because the runner was started with stdio
        inherited).
    :returns: A formatted block ready to append to an exception
        message, e.g.::

            \nRunner log: /home/u/.omnigent/logs/runner/runner-abcd.log

        The hint sits flush with the surrounding error and
        setup-suggestion lines so the three pieces read as one
        small block, not a stair-stepped list.

        Returns ``""`` when ``log_path is None``.
    """
    if log_path is None:
        return ""
    return f"\nRunner log: {log_path}"
