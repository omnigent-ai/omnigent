"""Classify harness launch/terminal failures into human-readable diagnoses.

When a harness terminal exits unexpectedly (or a launch/connection step
fails), the raw signal is a terse code plus a tail of PTY output — hard to
act on. This module maps that raw signal to a :class:`FailureDiagnosis`
(``title`` / ``cause`` / ``remediation``) the web UI can render as a clear
failure card, and provides English fallbacks for the failure *codes* that
have no output to pattern-match.

The design goal is one shared classification layer for every harness: the
terminal-exit path is common to all ~20 harnesses, so a matcher added here
covers them all. A new harness quirk becomes one entry in
:data:`_TERMINAL_EXIT_MATCHERS`, never per-harness branching.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "FailureDiagnosis",
    "classify_terminal_failure",
    "describe_failure_code",
]


@dataclass(frozen=True)
class FailureDiagnosis:
    """A human-readable interpretation of a launch/terminal failure.

    :param title: Short headline naming what went wrong, e.g.
        ``"Claude Code can't run as root"``. Renders as the card title.
    :param cause: One or two sentences explaining *why* it failed, in terms
        the user can act on.
    :param remediation: The concrete next step, e.g. a command to run or a
        config to change. ``None`` when there is no single clear fix.
    """

    title: str
    cause: str
    remediation: str | None = None


@dataclass(frozen=True)
class _Signal:
    """The normalized terminal-exit signal a matcher inspects.

    :param command: Launched executable basename, lowercased (``""`` if unknown).
    :param exit_code: Inner process exit code, or ``None`` if unknown.
    :param output: Terminal's last captured output, lowercased (``""`` if none).
    """

    command: str
    exit_code: int | None
    output: str

    def output_contains_any(self, needles: tuple[str, ...]) -> bool:
        return any(n in self.output for n in needles)


@dataclass(frozen=True)
class _TerminalMatcher:
    """One declarative rule mapping a terminal-exit signal to a diagnosis.

    :param name: Short slug for the matcher (test/debug identifier).
    :param predicate: Returns ``True`` when this rule explains the failure,
        given the normalized :class:`_Signal`.
    :param diagnosis: The :class:`FailureDiagnosis` to return on a match.
    """

    name: str
    predicate: Callable[[_Signal], bool]
    diagnosis: FailureDiagnosis


# --- root + --dangerously-skip-permissions -----------------------------------
# Claude Code refuses that flag as root ("cannot be run with root privileges
# for security reasons"). The harness passes the flag for autonomous runs, so a
# root container's agent terminal exits immediately.
_ROOT_MARKERS = ("root privileges", "security reasons", "cannot be run with root")

# --- not authenticated --------------------------------------------------------
_AUTH_MARKERS = (
    "not logged in",
    "please run /login",
    "please run `/login`",
    "invalid api key",
    "authentication_error",
    "401 unauthorized",
    "not authenticated",
)

# --- binary missing -----------------------------------------------------------
_MISSING_MARKERS = (
    "command not found",
    "no such file or directory",
    "not recognized as an internal or external command",
    "executable file not found",
)


# Ordered most-specific first: the root case also reads like a permission /
# auth problem, so it must win over the broader rules below it.
_TERMINAL_EXIT_MATCHERS: tuple[_TerminalMatcher, ...] = (
    _TerminalMatcher(
        "root_permission",
        lambda s: "security reasons" in s.output and s.output_contains_any(_ROOT_MARKERS),
        FailureDiagnosis(
            title="Claude Code can't run as root",
            cause=(
                "The agent terminal exited immediately because Claude Code refuses "
                "--dangerously-skip-permissions when running as the root user."
            ),
            remediation="Run the host as a non-root user (uid != 0).",
        ),
    ),
    _TerminalMatcher(
        "missing_binary",
        lambda s: s.exit_code == 127 or s.output_contains_any(_MISSING_MARKERS),
        FailureDiagnosis(
            title="Agent command not found",
            cause=(
                "The host couldn't find the agent's CLI on its PATH, so the terminal "
                "exited before the session could start."
            ),
            remediation="Install the harness on the host (e.g. run `omnigent setup`).",
        ),
    ),
    _TerminalMatcher(
        "not_authenticated",
        lambda s: s.output_contains_any(_AUTH_MARKERS),
        FailureDiagnosis(
            title="Agent isn't signed in",
            cause=(
                "The agent CLI exited because it has no valid credentials for this "
                "host — it needs to be logged in before it can run a session."
            ),
            remediation=(
                "Sign the agent in on the host (e.g. run its `/login`, or `omnigent login`)."
            ),
        ),
    ),
)


def classify_terminal_failure(
    *,
    command: str | None,
    exit_status: int | None,
    output: str | None,
) -> FailureDiagnosis | None:
    """Classify a required-terminal exit into a :class:`FailureDiagnosis`.

    :param command: The launched executable (may be a full path); only the
        basename is matched, case-insensitively.
    :param exit_status: The inner process's exit code, or ``None`` if unknown.
    :param output: The terminal's last captured output, or ``None``.
    :returns: A diagnosis when a matcher recognizes the failure, else ``None``
        (the caller falls back to the generic message).
    """
    signal = _Signal(
        command=(command or "").rsplit("/", 1)[-1].lower(),
        exit_code=exit_status,
        output=(output or "").lower(),
    )
    for matcher in _TERMINAL_EXIT_MATCHERS:
        if matcher.predicate(signal):
            return matcher.diagnosis
    return None


# --- failure-code English fallbacks -------------------------------------------
# Every server-emitted failure code, mapped to a one-line human sentence, so
# even an unclassified failure reads as English instead of a raw enum. Terminal
# exits that a matcher recognizes use the richer diagnosis above; this table
# is the floor for everything else.
#
# This is the canonical source; the frontend keeps a hand-mirrored copy
# (``FAILURE_CODE_DESCRIPTIONS`` in ``web/src/components/blocks/StatusBlocks.tsx``)
# because the failure card renders client-side. Keep the two in sync when adding
# or editing a code.
_FAILURE_CODE_DESCRIPTIONS: dict[str, str] = {
    "required_terminal_exited": (
        "The agent's terminal exited unexpectedly, so the session can't continue."
    ),
    "terminal_launch_failed": "The agent's terminal couldn't be started on the host.",
    "runner_error": "Something went wrong setting up the turn on the host.",
    "runner_disconnected": "The connection to the host dropped unexpectedly.",
    "connection_error": "The connection to the agent dropped mid-turn.",
    "context_length_exceeded": "The conversation grew past the model's context window.",
    "executor_error": "The agent runtime hit an error while running the turn.",
}


def describe_failure_code(code: str | None) -> str | None:
    """Return a one-line English description for a failure *code*.

    Server-side counterpart of the frontend's ``FAILURE_CODE_DESCRIPTIONS``
    map. The live failure card is rendered client-side, so this function is
    currently a parity mirror exercised by tests; it exists so a server-side
    caller (e.g. a future REPL/CLI failure renderer) has the same code→sentence
    fallback the web UI uses, without re-deriving it.

    :param code: The machine-readable failure code, e.g. ``"runner_error"``.
    :returns: A human sentence, or ``None`` for an unknown/empty code (callers
        fall back to whatever message they already have).
    """
    if not code:
        return None
    return _FAILURE_CODE_DESCRIPTIONS.get(code)
