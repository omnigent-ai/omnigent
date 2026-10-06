"""Decide whether a required native terminal's exit was the person's own quit.

A person can leave Claude or Pi themselves (``/exit``, Ctrl-C, closing the
terminal). That is not a crash: the session goes idle and the next message
cold-resumes the agent. Everything else about a required terminal's exit stays
a failure, so a launch-time exit, a non-zero status or a fatal signal still
reaches the person as one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

from omnigent.native.native_coding_agents import native_coding_agent_for_terminal_name
from omnigent.runner.resource_registry import TerminalExitEvent

# Banner Claude Code prints on a /exit or /quit (exit 0). Printing it can flip the
# idle memo back to "running" before the pane dies.
CLAUDE_EXIT_BANNER = "Resume this session with:"

# Stored in place of a failed exit so a turn stream the release severs ends as cancelled.
VOLUNTARY_EXIT_CODE = "voluntary_terminal_exit"

# Native TUIs whose required terminal exit can be the person quitting. The Codex TUI
# is an auxiliary terminal and never reaches this decision.
_USER_QUIT_TERMINALS = frozenset({"claude", "pi"})
# Claude SessionEnd reasons that name something the person did (/exit, /logout, /clear).
_USER_SESSION_END_REASONS = frozenset({"prompt_input_exit", "logout", "clear"})
# SessionEnd reasons where policy, not the person, ended the session.
_POLICY_SESSION_END_REASONS = frozenset({"bypass_permissions_disabled"})
# Ctrl-C and a closed terminal; any other signal (SIGKILL, SIGSEGV, SIGTERM) is not the person.
_USER_SIGNALS = frozenset({"SIGINT", "SIGHUP"})
# Rules that rest on evidence of the person's action. A bare zero exit does not: a launcher
# wrapper can exit 0 over a crashed agent.
_STRONG_RULES = frozenset({"claude_exit_banner", "session_end_reason", "user_signal"})


@dataclass(frozen=True)
class TerminalExitDecision:
    """How a required terminal's exit is handled, with the evidence behind it.

    :param voluntary: ``True`` when the person ended the agent themselves, so the
        session goes idle instead of failing.
    :param rule: Slug of the rule that decided, e.g. ``"exit_zero"``.
    :param harness: Native harness of the terminal, e.g. ``"pi-native"``.
    :param command: Basename of the launched command, e.g. ``"env"`` for a wrapped Claude.
    :param exit_status: The launched command's exit code, when known.
    :param signal: Signal tmux reported for the launched command, e.g. ``"SIGINT"``.
    :param session_end_reason: Claude ``SessionEnd`` hook reason, e.g. ``"logout"``.
    :param session_end_signal: Signal Claude's ``SessionEnd`` hook reported.
    :param session_end_evidence: Whether Claude's ``SessionEnd`` hook fired
        (``"claude_hook"``) or was never observed (``"not_observed"``).
    :param banner_seen: Whether Claude's exit banner was in the captured pane tail.
    :param interactive: Whether the TUI had accepted input before it exited.
    """

    voluntary: bool
    rule: str
    harness: str | None
    command: str | None
    exit_status: int | None
    signal: str | None
    session_end_reason: str | None
    session_end_signal: str | None
    session_end_evidence: str | None
    banner_seen: bool
    interactive: bool

    @property
    def strong_evidence(self) -> bool:
        """Whether a voluntary verdict rests on the person's action, not a bare zero exit."""
        return self.voluntary and self.rule in _STRONG_RULES

    def log_attributes(self) -> dict[str, object]:
        """Return the content-free fields of the ``native_terminal_exit_classified`` event."""
        return {
            "harness": self.harness,
            "command": self.command,
            "exit_status": self.exit_status,
            "signal": self.signal,
            "session_end_reason": self.session_end_reason,
            "session_end_signal": self.session_end_signal,
            "session_end_evidence": self.session_end_evidence,
            "banner_seen": self.banner_seen,
            "interactive": self.interactive,
            "decision": "voluntary" if self.voluntary else "failed",
            "rule": self.rule,
        }


def _signal_name(value: str | None) -> str | None:
    """Return a normalized signal name, e.g. ``"sigint"`` -> ``"SIGINT"``."""
    if not value:
        return None
    name = value.strip().upper()
    return name if name.startswith("SIG") else f"SIG{name}"


def classify_terminal_exit(
    event: TerminalExitEvent, *, runner_shutting_down: bool = False
) -> TerminalExitDecision:
    """Decide whether a required terminal's exit was the person's own quit.

    Claude's exit banner on a clean exit is always voluntary. Otherwise a
    Claude or Pi exit is voluntary only when the TUI had already accepted
    input and nothing points to a crash: no non-zero status, no fatal signal
    (from tmux or Claude's hook) and no policy exit. The person's quit is then
    shown by Claude's ``SessionEnd`` reason, a zero exit status, or death by
    SIGINT/SIGHUP. Only the zero exit status alone is weak evidence.

    :param event: The required terminal's exit event.
    :param runner_shutting_down: Whether the runner is stopping, which takes the
        terminal down with it; such an exit says nothing about the person.
    :returns: The decision and the evidence it rests on.
    """
    agent = native_coding_agent_for_terminal_name(event.terminal_name)
    signal_name = _signal_name(event.exit_signal)
    hook_signal = _signal_name(event.session_end_signal)
    reason = event.session_end_reason
    banner_seen = event.last_output is not None and CLAUDE_EXIT_BANNER in event.last_output

    def decide(voluntary: bool, rule: str) -> TerminalExitDecision:
        return TerminalExitDecision(
            voluntary=voluntary,
            rule=rule,
            harness=agent.harness if agent is not None else None,
            command=PurePath(event.command).name if event.command else None,
            exit_status=event.exit_status,
            signal=signal_name,
            session_end_reason=reason,
            session_end_signal=hook_signal,
            session_end_evidence=event.session_end_evidence,
            banner_seen=banner_seen,
            interactive=event.interactive,
        )

    if event.terminal_name == "claude" and event.exit_status == 0 and banner_seen:
        return decide(True, "claude_exit_banner")
    if event.terminal_name not in _USER_QUIT_TERMINALS:
        return decide(False, "terminal_not_covered")
    if runner_shutting_down:
        return decide(False, "runner_shutting_down")
    if not event.interactive:
        return decide(False, "not_interactive")
    if event.exit_status not in (None, 0):
        return decide(False, "nonzero_exit")
    if signal_name is not None and signal_name not in _USER_SIGNALS:
        return decide(False, "fatal_signal")
    if hook_signal is not None and hook_signal not in _USER_SIGNALS:
        return decide(False, "external_signal")
    if reason in _POLICY_SESSION_END_REASONS:
        return decide(False, "policy_exit")
    if reason in _USER_SESSION_END_REASONS:
        return decide(True, "session_end_reason")
    if event.exit_status == 0:
        return decide(True, "exit_zero")
    if signal_name in _USER_SIGNALS or hook_signal in _USER_SIGNALS:
        return decide(True, "user_signal")
    return decide(False, "no_quit_evidence")
