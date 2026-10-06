"""Decide whether a required native terminal's exit was the person's own quit.

A person can leave a native TUI themselves (``/exit``, Ctrl-C, closing the
terminal). That is not a crash: the session goes idle and the next message
cold-resumes the agent. Everything else about a required terminal's exit stays
a failure, so a launch-time exit, a non-zero status or a fatal signal still
reaches the person as one.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass

import httpx

from omnigent.native.native_coding_agents import native_coding_agent_for_terminal_name
from omnigent.runner.resource_registry import TerminalExitEvent

_logger = logging.getLogger("omnigent.runner.app")

# Banner Claude Code prints on a /exit or /quit (exit 0). Printing it can flip the
# idle memo back to "running" before the pane dies.
CLAUDE_EXIT_BANNER = "Resume this session with:"

# Native TUIs whose exit after an interactive session can be the person quitting.
_USER_QUIT_TERMINALS = frozenset({"claude", "codex", "pi"})
# Claude SessionEnd reasons that name something the person did (/exit, /logout, /clear).
_USER_SESSION_END_REASONS = frozenset({"prompt_input_exit", "logout", "clear"})
# SessionEnd reasons where policy, not the person, ended the session.
_POLICY_SESSION_END_REASONS = frozenset({"bypass_permissions_disabled"})
# Ctrl-C and a closed terminal; any other signal (SIGKILL, SIGSEGV, OOM) is a crash.
_USER_SIGNALS = frozenset({"SIGINT", "SIGHUP"})

TERMINAL_EXIT_NOTICE_CODE = "native_terminal_exited"


@dataclass(frozen=True)
class TerminalExitDecision:
    """How a required terminal's exit is handled, with the evidence behind it.

    :param voluntary: ``True`` when the person ended the agent themselves, so the
        session goes idle instead of failing.
    :param rule: Slug of the rule that decided, e.g. ``"exit_zero"``.
    :param harness: Native harness of the terminal, e.g. ``"pi-native"``.
    :param exit_status: The launched command's exit code, when known.
    :param signal: Signal tmux reported for the launched command, e.g. ``"SIGINT"``.
    :param session_end_reason: Claude ``SessionEnd`` hook reason, e.g. ``"logout"``.
    :param session_end_signal: Signal Claude's ``SessionEnd`` hook reported.
    :param banner_seen: Whether Claude's exit banner was in the captured pane tail.
    :param interactive: Whether the TUI had accepted input before it exited.
    """

    voluntary: bool
    rule: str
    harness: str | None
    exit_status: int | None
    signal: str | None
    session_end_reason: str | None
    session_end_signal: str | None
    banner_seen: bool
    interactive: bool

    def log_attributes(self) -> dict[str, object]:
        """Return the content-free fields of the ``native_terminal_exit_classified`` event."""
        return {
            "harness": self.harness,
            "exit_status": self.exit_status,
            "signal": self.signal,
            "session_end_reason": self.session_end_reason,
            "session_end_signal": self.session_end_signal,
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
    Claude/Codex/Pi exit is voluntary only when the TUI had already accepted
    input and nothing points to a crash: no non-zero status, no fatal signal and
    no policy exit. The person's quit is then shown by Claude's ``SessionEnd``
    reason, a zero exit status, or death by SIGINT/SIGHUP.

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
            exit_status=event.exit_status,
            signal=signal_name,
            session_end_reason=reason,
            session_end_signal=hook_signal,
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
    if reason in _POLICY_SESSION_END_REASONS:
        return decide(False, "policy_exit")
    if reason in _USER_SESSION_END_REASONS:
        return decide(True, "session_end_reason")
    if event.exit_status == 0:
        return decide(True, "exit_zero")
    if signal_name in _USER_SIGNALS or hook_signal in _USER_SIGNALS:
        return decide(True, "user_signal")
    return decide(False, "no_quit_evidence")


def terminal_display_name(terminal_name: str) -> str:
    """Return the person-facing agent name for a terminal, e.g. ``"Claude"`` for ``claude``."""
    agent = native_coding_agent_for_terminal_name(terminal_name)
    return agent.display_name if agent is not None else terminal_name


async def post_terminal_exit_notice(
    server_client: httpx.AsyncClient, session_id: str, terminal_name: str
) -> None:
    """Append a neutral "exited in its terminal" notice to the session transcript.

    Best-effort: a failed post only loses the notice.

    :param server_client: Runner-to-server client.
    :param session_id: Session whose terminal the person closed.
    :param terminal_name: Terminal that exited, e.g. ``"claude"``.
    """
    # The headline carries the whole sentence: it is all the transcript shows until expanded.
    notice = (
        f"{terminal_display_name(terminal_name)} exited in its terminal. Send a message to resume."
    )
    try:
        resp = await server_client.post(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "error",
                    "item_data": {
                        "source": "harness",
                        "code": TERMINAL_EXIT_NOTICE_CODE,
                        "title": notice,
                        "message": notice,
                        "level": "info",
                    },
                },
            },
            timeout=10.0,
        )
        resp.raise_for_status()
    except (httpx.HTTPError, RuntimeError):
        _logger.warning(
            "Failed to post the terminal-exit notice for %s",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
