"""Reusable pexpect helpers for driving the Omnigent REPL.

The Omnigent REPL is a prompt-toolkit app that renders a full
terminal layout (status bar + input area + streaming output +
Ctrl+G debug overview). Driving it from tests requires:

1. A PTY-backed subprocess (``pexpect.spawn``) with a real
   ``TERM`` so prompt-toolkit draws its layout instead of
   erroring out on CPR probes.
2. Deterministic synchronization — the status bar text
   (``state: sleeping`` / ``state: running``) is the most stable
   turn-boundary signal because it's printed on every redraw.
3. Keystroke-level input — ``pexpect.sendline`` sends
   ``\\r\\n`` which prompt-toolkit interprets as two distinct
   Enter presses in some builds; ``send(...) + send("\\r")``
   is what reliably submits a prompt.

The helpers here encapsulate that ceremony so individual tests
stay focused on the behavior they exercise.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0
shared infrastructure.
"""

from __future__ import annotations

import atexit
import contextlib
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

import pexpect

# Default PTY geometry. Large enough that the REPL's layout fits
# without wrapping assistant text onto many rows (which would
# scatter response content across ANSI control sequences).
_DEFAULT_ROWS = 40
_DEFAULT_COLS = 120

# Terminal type that prompt-toolkit recognizes. Without this
# ``TERM`` may be ``dumb`` and prompt-toolkit refuses to start.
_TERM = "xterm-256color"

# Regex that matches one full ANSI SGR / control sequence. Used
# by :func:`strip_ansi` so tests can assert on the plain text the
# REPL rendered instead of the raw escape-laden stream.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Status-bar substrings the REPL prints on every redraw. These
# are the stable synchronization points for turn boundaries:
# ``state: sleeping`` appears whenever the agent is waiting for
# user input, ``state: running`` appears while a turn is in
# progress. Matching substrings rather than full lines keeps the
# regex tolerant of the surrounding spinner/padding characters.
STATE_SLEEPING = r"state: sleeping"
STATE_RUNNING = r"state: running"
# Prompt marker fallback. In some pexpect / prompt-toolkit
# combinations the bottom toolbar is not rendered even though the
# input prompt itself is ready. Several REPL e2e tests already use
# this as the visible readiness signal.
PROMPT_READY = r"❯ "

# Persisted test theme used to bypass the first-run interactive theme
# picker. REPL tests already exercise prompt-toolkit interactions; the
# picker has its own focused unit tests and should not sit in front of
# every pexpect boot.
_TEST_THEME_CONFIG = "# Omnigent user configuration\ntui:\n  theme: light\n"


@dataclass(frozen=True)
class PexpectTurn:
    """
    Output captured from one REPL turn.

    :param raw: The raw PTY bytes between prompt submission and
        the next ``state: sleeping`` marker, decoded as UTF-8
        with prompt-toolkit's ANSI sequences still present.
    :param stripped: ``raw`` with ANSI control sequences
        removed, suitable for substring assertions.
    """

    raw: str
    stripped: str


def strip_ansi(text: str) -> str:
    """
    Remove ANSI SGR / control sequences from a prompt-toolkit
    render.

    :param text: Raw PTY output, e.g. output of
        ``child.before`` or ``read_nonblocking``.
    :returns: The same text with control sequences stripped, so
        substring assertions can target the actual rendered
        characters.
    """
    return _ANSI_RE.sub("", text)


def ensure_repl_test_theme_env(env: Mapping[str, str]) -> dict[str, str]:
    """
    Return an env dict whose HOME contains a persisted TUI theme.

    The startup REPL shows an interactive theme picker when
    ``$HOME/.omnigent/config.yaml`` has no ``tui.theme`` entry.
    That is correct for users but breaks pexpect tests that expect
    the normal REPL prompt to be the first interactive surface.

    When the caller already supplies an isolated ``HOME`` (typical
    ``tmp_path`` fixtures), this helper writes the theme config there.
    When the caller inherits the developer's real ``HOME``, it creates
    a temporary home instead and symlinks Databricks auth files back
    to the real home so profile-based harnesses still authenticate.

    :param env: Base subprocess environment, e.g. the
        ``omnigent_credentials_env`` fixture.
    :returns: A copy of *env* with ``HOME`` pointing at a directory
        that has ``.omnigent/config.yaml`` seeded.
    """
    prepared = dict(env)
    real_home = Path.home()
    requested_home = Path(prepared.get("HOME", str(real_home))).expanduser()
    if requested_home == real_home:
        home = Path(mkdtemp(prefix="omnigent-e2e-home-"))
        atexit.register(shutil.rmtree, home, ignore_errors=True)
        _link_if_exists(real_home / ".databrickscfg", home / ".databrickscfg")
        _link_if_exists(real_home / ".databricks", home / ".databricks")
        _link_if_exists(real_home / ".config" / "databricks", home / ".config" / "databricks")
        prepared["HOME"] = str(home)
    else:
        home = requested_home
        home.mkdir(parents=True, exist_ok=True)

    config_path = home / ".omnigent" / "config.yaml"
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(_TEST_THEME_CONFIG, encoding="utf-8")
    return prepared


def _link_if_exists(source: Path, dest: Path) -> None:
    """
    Symlink an auth file or directory into an isolated test HOME.

    :param source: Source path under the developer's real home,
        e.g. ``Path.home() / ".databrickscfg"``.
    :param dest: Destination path under the temporary test home.
    :returns: None.
    """
    if not source.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.symlink_to(source, target_is_directory=source.is_dir())
    except FileExistsError:
        return


def spawn_omnigent_run(
    omnigent_python: Path,
    yaml_path: Path | None,
    *,
    model: str,
    harness: str,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float,
    system_prompt: str | None = None,
    initial_prompt: str | None = None,
    no_log: bool = False,
    no_session: bool = True,
) -> pexpect.spawn:
    """
    Spawn ``omnigent run`` under a PTY for REPL tests.

    :param omnigent_python: Python interpreter with omnigent
        installed, e.g.
        ``Path("/Users/.../omnigent/.venv/bin/python")``.
    :param yaml_path: Absolute path to the agent YAML, e.g.
        ``examples/hello_world.yaml``. Pass ``None`` to exercise
        ``omnigent run --harness ...`` without an explicit
        agent argument.
    :param model: Model override passed via ``--model``, e.g.
        ``"databricks-gpt-5-mini"``.
    :param harness: Harness override passed via ``--harness``,
        e.g. ``"openai-agents"``.
    :param env: Environment dict for the subprocess — supplied
        by the ``omnigent_credentials_env`` fixture so PAT
        and base URL propagate.
    :param cwd: Working directory for the subprocess. Must be
        the Omnigent repo root so YAML ``callable:`` entries
        like ``tests.resources.examples._shared.tool_functions.get_current_time``
        resolve on sys.path.
    :param timeout: Default expect-timeout in seconds. Tests
        override per-expect when a specific step needs longer.
    :param system_prompt: Optional instructions override. Useful
        for the no-AGENT launcher path, where it maps to
        ``--system-prompt``. Existing YAML-based tests keep their
        source file's prompt by leaving this unset.
    :param initial_prompt: Optional ``-p`` / ``--prompt`` value.
        Lets tests drive a real turn without relying on interactive
        prompt-toolkit key submission.
    :param no_log: When True, pass ``--no-log`` for legacy CLI shapes that
        support it. Defaults to False because the current Click CLI only has
        opt-in ``--log``.
    :param no_session: When True, pass ``--no-session`` for CLI shapes
        that support it.
    :returns: A live ``pexpect.spawn`` child. Caller is
        responsible for ``child.sendcontrol("d")`` +
        ``child.expect(pexpect.EOF)`` teardown.
    """
    if yaml_path is None:
        # Exercise the public console-script entry point instead of
        # ``python -m omnigent``. The package ``__main__`` module
        # intentionally routes through the legacy argparse quick-chat
        # CLI, while the installed ``omnigent`` script invokes the
        # unified Click CLI where the no-AGENT ``run --harness``
        # launcher lives. ``PYTHONPATH`` from the fixture points this
        # console script at the worktree under test.
        args = [
            "run",
            "--model",
            model,
            "--harness",
            harness,
        ]
        # This no-AGENT console-script path is intentionally the same
        # public shape users run locally. The branch under test does
        # not expose the legacy ``--no-log`` / ``--no-session`` flags
        # on that shape, so do not append them here.
        command = str(omnigent_python.parent / "omnigent")
    else:
        args = [
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            model,
            "--harness",
            harness,
        ]
        if no_log:
            args.append("--no-log")
        if no_session:
            args.append("--no-session")
        command = str(omnigent_python)
    if system_prompt is not None:
        args.extend(["--system-prompt", system_prompt])
    if initial_prompt is not None:
        args.extend(["-p", initial_prompt])
    # NOTE: the omnigent CLI no longer accepts ``--profile``; Databricks
    # routing for spawned CLIs comes from the ``auth:`` block written into
    # the isolated ``OMNIGENT_CONFIG_HOME`` by ``omnigent_credentials_env``.
    spawn_env = ensure_repl_test_theme_env(env)
    return pexpect.spawn(
        command,
        args,
        env={
            **spawn_env,
            "TERM": _TERM,
            "LINES": str(_DEFAULT_ROWS),
            "COLUMNS": str(_DEFAULT_COLS),
        },
        cwd=str(cwd),
        encoding="utf-8",
        timeout=timeout,
        dimensions=(_DEFAULT_ROWS, _DEFAULT_COLS),
    )


def wait_for_ready(child: pexpect.spawn, *, timeout: float) -> None:
    """
    Block until the REPL reaches its initial waiting-for-input
    state.

    The preferred signal is the bottom toolbar's
    ``state: sleeping`` status line. Some prompt-toolkit/PTY
    combinations suppress the toolbar while still showing the
    actual ``❯`` input prompt, so fall back to that visible prompt
    marker.

    :param child: The spawn returned by
        :func:`spawn_omnigent_run`.
    :param timeout: Max seconds to wait for the REPL to boot.
    """
    child.expect([STATE_SLEEPING, PROMPT_READY], timeout=timeout)


def submit_prompt(child: pexpect.spawn, text: str) -> None:
    """
    Type a prompt into the REPL and submit it.

    Uses ``send(text)`` + ``send("\\r")`` rather than
    ``sendline`` because prompt-toolkit's binding for the
    submit key is the bare Enter (CR); ``sendline``'s implicit
    LF is a separate key event and can suppress the submission.

    :param child: Live ``pexpect.spawn`` child.
    :param text: The prompt text, e.g. ``"say hi in 5 words"``.
    """
    child.send(text)
    child.send("\r")


def await_turn_complete(
    child: pexpect.spawn,
    *,
    running_timeout: float,
    completion_timeout: float,
    running_marker: str = STATE_RUNNING,
    completion_pattern: str = STATE_SLEEPING,
) -> PexpectTurn:
    """
    Wait for the REPL to enter ``running`` and then reach a
    completion signal; return everything it printed from the
    ``running`` transition forward.

    The ``running`` transition is used as a handshake only — its
    appearance proves the prompt submission was accepted by the
    Session — so we concatenate the pre-running and post-running
    frame buffers. By default completion is the next
    ``state: sleeping`` toolbar render; tests whose terminal setup
    suppresses the toolbar can pass a visible response marker as
    *completion_pattern* instead.

    :param child: Live ``pexpect.spawn`` child.
    :param running_timeout: Max seconds to wait for the
        ``running`` transition after a prompt was submitted.
        Short — if this elapses the REPL almost certainly
        failed to register the prompt.
    :param completion_timeout: Max seconds to wait for the
        turn to finish. Long — real LLM latency sits here.
    :param running_marker: Regex marker that proves the submitted
        prompt was accepted and the turn started. Defaults to the
        bottom-toolbar ``state: running`` text; tests may pass a
        visible prompt marker when CPR suppresses toolbar output.
    :param completion_pattern: Regex/string expected after the
        turn starts. Defaults to ``state: sleeping``.
    :returns: Captured :class:`PexpectTurn` with raw and
        stripped forms of all output spanning the full turn.
    """
    child.expect(running_marker, timeout=running_timeout)
    # ``child.before`` here is everything rendered between the
    # previous expect (startup's ready signal) and the ``running``
    # transition — includes the echoed prompt text and the ``You>``
    # banner. We must snapshot it before the next expect overwrites
    # ``before``.
    pre_running = child.before or ""
    child.expect(completion_pattern, timeout=completion_timeout)
    post_running = child.before or ""
    raw = pre_running + post_running
    return PexpectTurn(raw=raw, stripped=strip_ansi(raw))


def clean_exit(child: pexpect.spawn, *, timeout: float) -> None:
    """
    Ask the REPL to exit and wait for the subprocess to terminate.

    The normal user gesture is Ctrl+D on an empty prompt. Under
    prompt-toolkit + pexpect, CI occasionally drops that keystroke
    while the bottom toolbar is repainting, leaving the process idle
    at ``state: sleeping`` until the test times out. Fall back to the
    explicit ``/quit`` slash command, which reaches the same
    ``host.request_exit()`` path without depending on prompt-toolkit's
    EOF key binding.

    If both the Ctrl+D gesture and the ``/quit`` fallback fail to
    produce EOF within ``timeout``, force-kill the child instead of
    raising. ``clean_exit`` is a teardown helper — every caller runs
    it as the final step after the test's real assertions have already
    passed — so a slow shutdown handshake should not fail an otherwise
    green test. The shutdown work (session-log write, task
    cancellation, ``app.exit()``) occasionally exceeds ``timeout`` on a
    loaded ``xdist`` worker, especially for workflows that leave parked
    tasks behind; that is a timing flake, not a product defect.

    :param child: Live ``pexpect.spawn`` child.
    :param timeout: Max seconds to wait for the child to
        terminate after each exit attempt. The REPL writes its
        session log, cancels pending tasks, and then calls
        ``app.exit()`` — all of which can take a few seconds on
        shutdown.
    """
    try:
        child.sendcontrol("d")
        try:
            child.expect(pexpect.EOF, timeout=timeout)
        except pexpect.TIMEOUT:
            # Clear any half-rendered prompt contents before submitting
            # the explicit exit command. If the Ctrl+D timeout happened
            # after the process actually exited, the expect below will see
            # EOF immediately.
            child.send("\x15")
            submit_prompt(child, "/quit")
            try:
                child.expect(pexpect.EOF, timeout=timeout)
            except pexpect.TIMEOUT:
                # Both graceful gestures stalled. The functional assertions
                # are already done; don't let a slow teardown fail the run.
                child.close(force=True)
                return
        child.close()
    except OSError:
        # Child already exited (closed PTY → [Errno 5] on the exit gesture);
        # a headless one-shot like ``claude -p`` self-terminates after
        # replying. Assertions ran before teardown, so treat as a clean exit.
        with contextlib.suppress(Exception):
            child.close(force=True)
