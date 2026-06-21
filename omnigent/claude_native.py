"""Native Claude Code terminal wrapper for the Omnigent CLI.

The wrapper deliberately treats Claude Code as a terminal-first
program. It creates or binds an Omnigent session, launches ``claude``
through the existing runner terminal resource API, then attaches the
local TTY to the existing terminal WebSocket protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import sys
import termios
import tty
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.onboarding.provider_config import ProviderEntry
    from omnigent.spec.types import AgentSpec

import click
import httpx
import yaml
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, WebSocketException
from websockets.frames import Close

from omnigent._native_resume_hint import echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._startup_profile import StartupProfiler
from omnigent._terminal_picker_theme import (
    PICKER_ACCENT as _PICKER_ACCENT,
)
from omnigent._terminal_picker_theme import (
    PICKER_MUTED as _PICKER_MUTED,
)
from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import (
    WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY,
)
from omnigent.claude_native_bridge import (
    BRIDGE_ID_LABEL_KEY,
    augment_claude_args,
    bridge_dir_for_bridge_id,
    prepare_bridge_dir,
    read_active_session_id,
    read_user_effort_level,
    url_component,
)
from omnigent.claude_native_forwarder import (
    reset_transcript_forward_state,
    supervise_forwarder,
)
from omnigent.claude_native_state import (
    read_launch_state,
    redirect_launch_state,
    write_launch_state,
)
from omnigent.conversation_browser import conversation_url, open_conversation_link_if_enabled
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.host.daemon_launch import (
    DAEMON_POLL_INTERVAL_S,
    error_text,
    launch_or_reuse_daemon_runner,
    wait_for_host_online,
    wait_for_runner_online,
)
from omnigent.native_terminal import (
    DAEMON_HOST_ONLINE_TIMEOUT_S as _DAEMON_HOST_ONLINE_TIMEOUT_S,
)
from omnigent.native_terminal import (
    DAEMON_RUNNER_ONLINE_TIMEOUT_S as _DAEMON_RUNNER_ONLINE_TIMEOUT_S,
)
from omnigent.native_terminal import (
    DAEMON_TERMINAL_READY_TIMEOUT_S as _DAEMON_TERMINAL_READY_TIMEOUT_S,
)
from omnigent.native_terminal import (
    bind_session_runner as _bind_session_runner,
)
from omnigent.native_terminal import (
    terminal_attach_url as _attach_url,
)
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_TERMINAL_DETACHED,
    WS_CLOSE_TERMINAL_NOT_FOUND,
)

_logger = logging.getLogger(__name__)

_AGENT_NAME = "claude-native-ui"
_DEFAULT_CLAUDE_COMMAND = "claude"
_CLAUDE_TERMINAL_SCROLLBACK_LINES = 100_000
_TERMINAL_NAME = "claude"
_TERMINAL_SESSION_KEY = "main"
_UCODE_CLAUDE_AGENT_NAME = "claude"
_UCODE_CLAUDE_BASE_URL_ENV = "ANTHROPIC_BASE_URL"
_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
_ANTHROPIC_BEDROCK_BASE_URL_ENV = "ANTHROPIC_BEDROCK_BASE_URL"
_AWS_BEARER_TOKEN_BEDROCK_ENV = "AWS_BEARER_TOKEN_BEDROCK"
_CLAUDE_CODE_USE_BEDROCK_ENV = "CLAUDE_CODE_USE_BEDROCK"
_CLAUDE_CODE_NESTED_SESSION_ENV = "CLAUDECODE"
_CLAUDE_CODE_API_KEY_HELPER_TTL_ENV = "CLAUDE_CODE_API_KEY_HELPER_TTL_MS"
_CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS_ENV = "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"
_CLAUDE_CODE_ENABLE_TOOL_SEARCH_ENV = "ENABLE_TOOL_SEARCH"
# Claude Code's agent view (the session list opened by `claude agents`, the
# left-arrow shortcut on an empty prompt, or /background) lets the user hop to
# other sessions inside the TUI.  Omnigent owns session switching in its own
# UI, so a wrapped terminal must stay pinned to the one session the UI thinks
# it is showing.
_CLAUDE_CODE_DISABLE_AGENT_VIEW_ENV = "CLAUDE_CODE_DISABLE_AGENT_VIEW"
# Claude Code env vars that pin each model-tier alias to a provider-specific
# model ID.  When set, the /model picker shows these IDs as options rather
# than normalising to canonical Anthropic names (which the Databricks gateway
# rejects).  See https://code.claude.com/docs/en/model-config#override-model-ids-per-version
_ANTHROPIC_DEFAULT_FABLE_MODEL_ENV = "ANTHROPIC_DEFAULT_FABLE_MODEL"
_ANTHROPIC_DEFAULT_OPUS_MODEL_ENV = "ANTHROPIC_DEFAULT_OPUS_MODEL"
_ANTHROPIC_DEFAULT_SONNET_MODEL_ENV = "ANTHROPIC_DEFAULT_SONNET_MODEL"
_ANTHROPIC_DEFAULT_HAIKU_MODEL_ENV = "ANTHROPIC_DEFAULT_HAIKU_MODEL"
_UCODE_CLAUDE_TIER_TO_ENV: dict[str, str] = {
    "fable": _ANTHROPIC_DEFAULT_FABLE_MODEL_ENV,
    "opus": _ANTHROPIC_DEFAULT_OPUS_MODEL_ENV,
    "sonnet": _ANTHROPIC_DEFAULT_SONNET_MODEL_ENV,
    "haiku": _ANTHROPIC_DEFAULT_HAIKU_MODEL_ENV,
}
_DEFAULT_UCODE_AUTH_REFRESH_INTERVAL_MS = 900_000
_SESSION_LABELS = {
    "omnigent.ui": "terminal",
    _WRAPPER_LABEL_KEY: _WRAPPER_LABEL_VALUE,
}
_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
_CLAUDE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RESUME_ACTION_SWITCH = "switch"
_RESUME_ACTION_MOVE = "move"
_RESUME_ACTION_LEAVE = "leave"

# Capped exponential backoff for the attach-WS reconnect loop.
_ATTACH_INITIAL_RECONNECT_DELAY_S = 0.5
_ATTACH_MAX_RECONNECT_DELAY_S = 5.0
_CLAUDE_ATTACH_WS_CLOSE_TIMEOUT_S = 0.25
_CLAUDE_TERMINAL_GONE_WATCH_INTERVAL_S = 0.25
_CLAUDE_TERMINAL_GONE_WATCH_HTTP_TIMEOUT_S = 1.0
_CLAUDE_STARTUP_PROFILE_ENV_VAR = "OMNIGENT_CLAUDE_STARTUP_PROFILE"


@dataclass(frozen=True)
class _ResumeWorkspaceActionOption:
    """
    One selectable action in the cwd-mismatch prompt.

    :param action: Stable action value returned to the caller, e.g.
        ``"switch"``.
    :param label: User-facing action label, e.g.
        ``"Switch working directory to /home/me/repo"``.
    """

    action: str
    label: str


@dataclass
class _ResumeWorkspaceActionPickerState:
    """
    Mutable state for the prompt-toolkit workspace action picker.

    :param options: Selectable workspace actions in display order.
    :param selected_index: Zero-based selected option index.
    """

    options: list[_ResumeWorkspaceActionOption]
    selected_index: int = 0

    def move_selection(self, delta: int) -> None:
        """
        Move the selected option up or down.

        :param delta: Signed row delta, e.g. ``1`` for down.
        :returns: None.
        """
        last_index = len(self.options) - 1
        self.selected_index = max(0, min(last_index, self.selected_index + delta))

    def selected_action(self) -> str:
        """
        Return the currently highlighted action.

        :returns: Action value, e.g. ``"move"``.
        """
        return self.options[self.selected_index].action


@dataclass(frozen=True)
class PreparedClaudeTerminal:
    """
    Prepared native Claude terminal attachment details.

    :param session_id: Omnigent session/conversation id.
    :param terminal_id: Terminal resource id to attach.
    :param bridge_dir: Filesystem bridge directory shared with
        Claude hooks/MCP helpers.
    :param reattached: ``True`` when the terminal already existed and
        was reused rather than launched in this invocation. Drives
        teardown ownership: a reattached invocation must not close
        the terminal on exit, because the launcher that originally
        created it owns its lifecycle.
    :param cold_resumed: ``True`` when we launched a fresh terminal
        against an existing Omnigent session (i.e. ``--resume <conv>`` with
        no live terminal). The forwarder must seek to the current
        transcript end in this case — when ``--resume <claude_sid>``
        is injected into the launch args, Claude reopens the prior
        JSONL transcript, and re-reading it from offset 0 would
        re-post every prior turn to AP. There is no server-side dedup:
        seeking to the end (plus the forwarder's persisted byte offset
        on subsequent ticks) is the only thing keeping old turns from
        being re-posted as new messages. ``cold_resumed`` is
        *independent* of ``reattached``: cold resume creates a new
        terminal (we own teardown) but the forwarder still needs the
        skip-existing behavior.
    :param tmux_socket: Runner tmux server socket path when the
        terminal exposed one and it is reachable from this process,
        e.g. ``Path("/tmp/omnigent-501/.../tmux.sock")``. ``None``
        when the runner did not advertise a socket. Drives the
        same-machine direct ``tmux attach`` fast path; a remote
        runner's socket won't exist locally, so the attach falls back
        to the WebSocket PTY bridge.
    :param tmux_target: tmux ``-t`` target for the terminal pane,
        e.g. ``"main"``. ``None`` when unavailable. Paired with
        ``tmux_socket`` for the direct attach.
    """

    session_id: str
    terminal_id: str
    bridge_dir: Path
    reattached: bool
    cold_resumed: bool = False
    tmux_socket: Path | None = None
    tmux_target: str | None = None


@dataclass(frozen=True)
class ClaudeNativeUcodeConfig:
    """
    Ucode-derived Claude Code launch configuration.

    :param env: Allowlisted environment variables for the ``claude``
        terminal process, e.g. ``{"ANTHROPIC_BASE_URL":
        "https://example.databricks.com/ai-gateway/anthropic"}``.
    :param api_key_helper: Claude Code ``apiKeyHelper`` command from
        ucode state, e.g. ``"databricks auth token --host
        https://example.databricks.com ..."``.
    :param model: Optional model id from ucode state, e.g.
        ``"databricks-claude-opus-4-7"``.
    """

    env: dict[str, str]
    api_key_helper: str
    model: str | None = None


def build_native_claude_terminal_env(
    claude_config: ClaudeNativeUcodeConfig | None,
) -> dict[str, str]:
    """
    Build env overrides for a native Claude Code terminal process.

    Forces MCP Tool Search on so Claude defers MCP tool schemas and
    loads them on demand, and disables Claude Code's agent view so the
    terminal stays pinned to the session the Omnigent UI is showing.

    :param claude_config: Optional provider/ucode launch config, e.g.
        one carrying ``{"ANTHROPIC_BASE_URL": "https://example.com"}``.
        ``None`` means use Claude Code's own native auth.
    :returns: Environment overrides for the terminal process, e.g.
        ``{"ENABLE_TOOL_SEARCH": "true"}``.
    """
    terminal_env = {
        _CLAUDE_CODE_ENABLE_TOOL_SEARCH_ENV: "true",
        _CLAUDE_CODE_DISABLE_AGENT_VIEW_ENV: "1",
    }
    if claude_config is not None:
        terminal_env.update(claude_config.env)
        terminal_env[_CLAUDE_CODE_ENABLE_TOOL_SEARCH_ENV] = "true"
        terminal_env[_CLAUDE_CODE_DISABLE_AGENT_VIEW_ENV] = "1"
    return terminal_env


def _mark_startup_step(
    startup_profiler: StartupProfiler,
    label: str,
    *,
    startup_progress: RunnerStartupProgress | None = None,
    progress_message: str | None = None,
    detail: str | None = None,
) -> None:
    """
    Record a startup phase for diagnostics and optional user progress.

    :param startup_profiler: Profiler receiving timing marks.
    :param label: Short phase label, e.g. ``"creating daemon claude session"``.
    :param startup_progress: Optional active progress renderer. ``None``
        means the phase is only recorded in the profiler.
    :param progress_message: Optional user-facing progress message,
        e.g. ``"Creating Claude session..."``. ``None`` keeps this
        mark out of the normal startup display.
    :param detail: Optional profiler detail, e.g. ``"runner=runner_abc123"``.
    :returns: None.
    """
    startup_profiler.mark(label, detail=detail)
    if startup_progress is not None and progress_message is not None:
        startup_progress.update(progress_message)


def run_claude_native(
    *,
    server: str | None,
    session_id: str | None,
    claude_args: tuple[str, ...],
    resume_picker: bool = False,
    command: str = _DEFAULT_CLAUDE_COMMAND,
    use_claude_config: bool = False,
    auto_open_conversation: bool = False,
    startup_profiler: StartupProfiler | None = None,
) -> None:
    """
    Launch Claude Code in an Omnigent terminal and attach locally.

    :param server: Optional remote Omnigent server URL. ``None`` starts a
        local Omnigent server using the existing chat server machinery.
    :param session_id: Optional existing session to bind and reuse,
        e.g. ``"conv_abc123"``. ``None`` creates a new bundled
        session.
    :param claude_args: Args after ``claude``, e.g.
        ``("--dangerously-skip-permissions",)``. Stray ``--resume`` /
        ``-r`` is stripped defensively (Omnigent owns resume).
    :param resume_picker: ``True`` runs the claude-native picker
        once the server is reachable; ``False`` keeps the existing
        ``session_id``-or-fresh-session behavior.
    :param command: Executable to run in the terminal resource,
        e.g. ``"claude"``. Kept off the public CLI surface so v0
        always exposes Claude Code, while tests can supply a fake
        executable.
    :param use_claude_config: When ``True``, skip Databricks/ucode auth
        and let Claude use its own existing ``~/.claude/`` configuration.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL after the session is prepared.
    :param startup_profiler: Optional shared startup profiler from the
        Click command. ``None`` creates one from
        ``OMNIGENT_CLAUDE_STARTUP_PROFILE``.
    :returns: None after the attach session ends.
    :raises click.ClickException: If setup, launch, or attach fails.
    """
    startup_profiler = startup_profiler or StartupProfiler.from_env(
        name="omnigent claude",
        env_var=_CLAUDE_STARTUP_PROFILE_ENV_VAR,
    )
    startup_profiler.mark("native launch entered")
    resolved_command = command.strip()
    if not resolved_command:
        raise click.ClickException("Claude command must not be empty.")
    startup_profiler.mark("checking local tools")
    _preflight_local_tools(resolved_command)
    startup_profiler.mark("local tools ready")
    sanitized_args = _strip_resume_from_claude_args(claude_args)
    startup_profiler.mark("claude args normalized")
    # Resolve the launch config across all offerings: a configured provider
    # (configure harnesses), the Databricks ucode profile, or Claude's own
    # login — so `omnigent claude` honors the provider selection just like
    # the in-process claude-sdk harness. ``use_claude_config`` forces the
    # CLI's own ~/.claude config (skips all of it).
    startup_profiler.mark("resolving claude config")
    claude_config = None if use_claude_config else resolve_native_claude_config(spec=None)
    startup_profiler.mark(
        "claude config resolved",
        detail="native config" if claude_config is not None else "claude cli config",
    )

    with TemporaryDirectory(prefix="omnigent-claude-native-") as tmpdir:
        spec_path = _materialize_claude_agent_spec(Path(tmpdir))
        startup_profiler.mark("agent spec materialized")
        if server is None:
            _run_with_local_server(
                spec_path,
                session_id=session_id,
                resume_picker=resume_picker,
                claude_args=sanitized_args,
                command=resolved_command,
                claude_config=claude_config,
                auto_open_conversation=auto_open_conversation,
                startup_profiler=startup_profiler,
            )
        else:
            # The daemon-spawned runner launches ``claude`` itself and
            # derives the ucode config from the provider config, so the
            # remote path takes neither ``command`` nor ``claude_config``.
            _run_with_remote_server(
                server.rstrip("/"),
                spec_path,
                session_id=session_id,
                resume_picker=resume_picker,
                claude_args=sanitized_args,
                auto_open_conversation=auto_open_conversation,
                startup_profiler=startup_profiler,
            )


def _resolve_session_id_for_resume(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    resume_picker: bool,
) -> str | None:
    """
    Translate the CLI's resume inputs into a concrete session id.

    The picker is scoped to claude-native conversations.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers; ``{}`` for local server.
    :param session_id: Explicit ``--resume <id>``; wins over the picker.
    :param resume_picker: ``True`` for bare ``--resume`` (no value).
    :returns: Conversation id, or ``None`` for "start fresh" / picker cancelled.
    :raises click.ClickException: Picker requested but no prior sessions exist.
    """
    if session_id is not None:
        return session_id
    if not resume_picker:
        return None
    # Deferred — omnigent_client / repl pull in heavy graphs we don't want at startup.
    from omnigent_client import OmnigentClient

    from omnigent.repl._resume_picker import pick_conversation_by_wrapper_label_from_sdk

    async def _drive() -> str | None:
        async with OmnigentClient(
            base_url=base_url, headers=headers if headers else None
        ) as client:
            return await pick_conversation_by_wrapper_label_from_sdk(
                client, wrapper_value=_WRAPPER_LABEL_VALUE, agent_name=_AGENT_NAME
            )

    return asyncio.run(_drive())


def _align_working_directory_with_session(
    session_id: str,
    *,
    base_url: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """
    Resolve cwd mismatch before resuming a Claude-native session.

    Claude Code's ``--resume <claude_sid>`` (which the cold-resume
    path injects -- see :func:`_resolve_cold_resume_args`) requires
    the cwd of the resumed invocation to match the cwd of the
    original session. If they differ, Claude exits immediately on
    launch. The wrapper records the launch cwd in client-side
    persistent state at session creation (see
    :mod:`omnigent.claude_native_state`); this helper reads it back
    on resume and asks the user whether to switch cwd, move Claude's
    transcript into the current cwd, or leave without resuming.

    The state is **client-side and per-user**, not server-side, so:

    * A user resuming on the same machine they created the session
      on gets the chdir prompt; this is the common path.
    * A user resuming from a different machine has no recorded
      state for this conv id locally -- the helper silently
      proceeds (no prompt) and Claude will likely exit, at which
      point the user knows to start a fresh session. The wrapper
      cannot fabricate the cwd; only the original client knew it.

    Decision table:

    - **No state recorded**: silent no-op. Either a legacy session
      created before this tracking landed, or a session created on
      a different machine. Echoing a hint here would be noisy on
      every legacy resume; the user finds out via Claude's own
      "session not found / cwd mismatch" message if it matters.
    - **Recorded cwd matches current cwd**: silent no-op.
    - **Recorded cwd differs, recorded path exists**: offer
      ``switch`` (default), ``move``, or ``leave``. ``switch``
      mutates process cwd. ``move`` copies Claude's transcript
      into the current cwd's Claude project directory and updates the
      client-side launch state. ``leave`` cancels the resume before
      Claude can crash on the cwd mismatch.
    - **Recorded cwd differs, recorded path missing**: offer
      ``move`` when the Claude transcript can still be found;
      otherwise fail loud with a :class:`click.ClickException`.

    :param session_id: Resolved Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param base_url: Omnigent server base URL used to look up Claude's
        external session id for redirect, e.g. ``"http://127.0.0.1:6767"``.
        ``None`` means redirect is unavailable.
    :param headers: HTTP auth headers for *base_url*, e.g.
        ``{"Authorization": "Bearer ..."}``. ``None`` is treated as
        no headers.
    :returns: None. Side-effect-only -- the cwd is mutated when
        the user chooses ``switch``; Claude state is moved when the
        user chooses ``move``.
    :raises click.ClickException: When no viable switch or move path
        exists, when moving fails, or when the user leaves.
    """
    state = read_launch_state(session_id)
    if state is None:
        return
    current = Path.cwd().resolve()
    recorded_path = Path(state.working_directory).resolve()
    if current == recorded_path:
        return
    external_session_id = _fetch_external_session_id_for_redirect(
        base_url=base_url,
        headers=headers or {},
        session_id=session_id,
    )
    redirect_available = _redirect_available(external_session_id)
    if not recorded_path.is_dir() and not redirect_available:
        raise click.ClickException(
            f"Session {session_id} was created in {recorded_path}, but that "
            f"directory no longer exists and Claude transcript "
            f"{external_session_id or '<unknown>'!r} was not found locally. "
            f"Recreate or move the project back before resuming."
        )
    action = _prompt_resume_workspace_action(
        recorded_path=recorded_path,
        current=current,
        redirect_available=redirect_available,
    )
    if action == _RESUME_ACTION_SWITCH:
        _switch_to_recorded_working_directory(recorded_path)
        return
    if action == _RESUME_ACTION_MOVE:
        if external_session_id is None:
            raise click.ClickException(
                "Cannot move Claude transcript: no external session id was found."
            )
        _redirect_claude_transcript_to_current_project(
            session_id=session_id,
            external_session_id=external_session_id,
            current=current,
        )
        return
    raise click.ClickException("Resume cancelled.")


def _prompt_resume_workspace_action(
    *,
    recorded_path: Path,
    current: Path,
    redirect_available: bool,
) -> str:
    """
    Ask how to handle a Claude resume cwd mismatch.

    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :param redirect_available: Whether a local Claude transcript for
        the session was found and can be moved into *current*.
    :returns: One of ``"switch"``, ``"move"``, or ``"leave"``.
    """
    options = _resume_workspace_action_options(
        recorded_path=recorded_path,
        current=current,
        redirect_available=redirect_available,
    )
    if _stream_is_tty(sys.stdin):
        return _pick_resume_workspace_action_prompt_toolkit(
            options,
            recorded_path=recorded_path,
            current=current,
            out=sys.stderr,
            in_=sys.stdin,
        )
    return _prompt_resume_workspace_action_text(
        options,
        recorded_path=recorded_path,
        current=current,
    )


def _resume_workspace_action_options(
    *,
    recorded_path: Path,
    current: Path,
    redirect_available: bool,
) -> list[_ResumeWorkspaceActionOption]:
    """
    Build the valid actions for a cwd-mismatched resume.

    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :param redirect_available: Whether a local Claude transcript for
        the session was found and can be moved into *current*.
    :returns: Action options in display order.
    """
    recorded_exists = recorded_path.is_dir()
    options: list[_ResumeWorkspaceActionOption] = []
    if recorded_exists:
        options.append(
            _ResumeWorkspaceActionOption(
                action=_RESUME_ACTION_SWITCH,
                label=f"Switch working directory to {recorded_path}",
            )
        )
    if redirect_available:
        options.append(
            _ResumeWorkspaceActionOption(
                action=_RESUME_ACTION_MOVE,
                label=f"Move conversation to {current}",
            )
        )
    options.append(
        _ResumeWorkspaceActionOption(
            action=_RESUME_ACTION_LEAVE,
            label="Leave",
        )
    )
    return options


def _prompt_resume_workspace_action_text(
    options: list[_ResumeWorkspaceActionOption],
    *,
    recorded_path: Path,
    current: Path,
) -> str:
    """
    Ask for a workspace action using Click's text prompt fallback.

    :param options: Selectable workspace actions in display order.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :returns: Selected action, e.g. ``"switch"``.
    """
    click.echo(f"\nSession was started in: {recorded_path}", err=True)
    click.echo(f"Current working directory: {current}", err=True)
    click.echo(
        "Claude resume is directory-scoped. Choose an action:",
        err=True,
    )
    for option in options:
        click.echo(f"  {option.action:<6} - {option.label}", err=True)
    return click.prompt(
        "Resume action",
        type=click.Choice([option.action for option in options]),
        default=options[0].action,
        show_choices=True,
        err=True,
    )


def _pick_resume_workspace_action_prompt_toolkit(
    options: list[_ResumeWorkspaceActionOption],
    *,
    recorded_path: Path,
    current: Path,
    out: IO[str],
    in_: IO[str],
) -> str:
    """
    Run the interactive workspace action selector.

    :param options: Selectable workspace actions in display order.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :param out: Output stream for prompt-toolkit rendering.
    :param in_: Input stream for prompt-toolkit keypresses.
    :returns: Selected action, e.g. ``"move"``.
    :raises KeyboardInterrupt: Propagated when the user presses
        Ctrl+C.
    """
    state = _ResumeWorkspaceActionPickerState(options=options)
    app = _resume_workspace_action_application(
        state,
        recorded_path=recorded_path,
        current=current,
        out=out,
        in_=in_,
    )
    return app.run(
        handle_sigint=False,
        set_exception_handler=False,
        in_thread=_has_running_event_loop(),
    )


def _resume_workspace_action_application(
    state: _ResumeWorkspaceActionPickerState,
    *,
    recorded_path: Path,
    current: Path,
    out: IO[str],
    in_: IO[str],
) -> Any:
    """
    Build the prompt-toolkit application for the action selector.

    :param state: Mutable picker state.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :param out: Output stream for prompt-toolkit rendering.
    :param in_: Input stream for prompt-toolkit keypresses.
    :returns: A :class:`prompt_toolkit.application.Application`.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.input.defaults import create_input
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.output.defaults import create_output

    control = _resume_workspace_action_control(
        state,
        recorded_path=recorded_path,
        current=current,
    )
    return Application(
        layout=Layout(Window(content=control, wrap_lines=True, always_hide_cursor=True)),
        key_bindings=_resume_workspace_action_key_bindings(state),
        style=_resume_workspace_action_style(),
        include_default_pygments_style=False,
        full_screen=False,
        erase_when_done=False,
        input=create_input(stdin=in_),
        output=create_output(stdout=out),
    )


def _resume_workspace_action_control(
    state: _ResumeWorkspaceActionPickerState,
    *,
    recorded_path: Path,
    current: Path,
) -> Any:
    """
    Build the formatted-text control for the action selector.

    :param state: Mutable picker state.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :returns: A :class:`prompt_toolkit.layout.controls.FormattedTextControl`.
    """
    from prompt_toolkit.layout.controls import FormattedTextControl

    return FormattedTextControl(
        lambda: _resume_workspace_action_fragments(
            state,
            recorded_path=recorded_path,
            current=current,
        ),
        focusable=True,
    )


def _resume_workspace_action_key_bindings(state: _ResumeWorkspaceActionPickerState) -> Any:
    """
    Build keybindings for the workspace action selector.

    :param state: Mutable picker state.
    :returns: A :class:`prompt_toolkit.key_binding.KeyBindings`
        instance.
    """
    from prompt_toolkit.key_binding import KeyBindings

    key_bindings = KeyBindings()
    _bind_resume_workspace_action_navigation(key_bindings, state)
    _bind_resume_workspace_action_completion(key_bindings, state)
    _bind_resume_workspace_action_interrupt(key_bindings)
    return key_bindings


def _bind_resume_workspace_action_navigation(
    key_bindings: Any,
    state: _ResumeWorkspaceActionPickerState,
) -> None:
    """
    Add movement keys to the workspace action selector.

    :param key_bindings: prompt-toolkit keybinding registry.
    :param state: Mutable picker state.
    :returns: None.
    """

    @key_bindings.add("up")
    @key_bindings.add("k")
    def _move_up(event: Any) -> None:
        """
        Move the highlighted action upward.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        state.move_selection(-1)
        event.app.invalidate()

    @key_bindings.add("down")
    @key_bindings.add("j")
    def _move_down(event: Any) -> None:
        """
        Move the highlighted action downward.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        state.move_selection(1)
        event.app.invalidate()


def _bind_resume_workspace_action_completion(
    key_bindings: Any,
    state: _ResumeWorkspaceActionPickerState,
) -> None:
    """
    Add selection and cancellation keys to the action selector.

    :param key_bindings: prompt-toolkit keybinding registry.
    :param state: Mutable picker state.
    :returns: None.
    """

    @key_bindings.add("enter")
    def _select(event: Any) -> None:
        """
        Select the highlighted action.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        event.app.exit(result=state.selected_action())

    @key_bindings.add("q")
    @key_bindings.add("escape")
    @key_bindings.add("c-d")
    def _leave(event: Any) -> None:
        """
        Leave without resuming.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        event.app.exit(result=_RESUME_ACTION_LEAVE)


def _bind_resume_workspace_action_interrupt(key_bindings: Any) -> None:
    """
    Add Ctrl+C handling to the action selector.

    :param key_bindings: prompt-toolkit keybinding registry.
    :returns: None.
    """

    @key_bindings.add("c-c")
    def _interrupt(event: Any) -> None:
        """
        Propagate Ctrl+C as KeyboardInterrupt.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        event.app.exit(exception=KeyboardInterrupt)


def _resume_workspace_action_style() -> Any:
    """
    Build prompt-toolkit styles for the workspace action selector.

    :returns: A :class:`prompt_toolkit.styles.Style` instance.
    """
    from prompt_toolkit.styles import Style

    return Style.from_dict(
        {
            "accent": _PICKER_ACCENT,
            "accent-bold": f"{_PICKER_ACCENT} bold",
            "muted": _PICKER_MUTED,
            "selected": f"{_PICKER_ACCENT} bold",
            "title": "bold",
        }
    )


def _resume_workspace_action_fragments(
    state: _ResumeWorkspaceActionPickerState,
    *,
    recorded_path: Path,
    current: Path,
) -> list[tuple[str, str]]:
    """
    Render the workspace action selector as prompt-toolkit fragments.

    :param state: Mutable picker state.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :returns: ``(style, text)`` fragments for prompt-toolkit.
    """
    fragments: list[tuple[str, str]] = []
    _append_resume_workspace_action_header(
        fragments,
        recorded_path=recorded_path,
        current=current,
    )
    _append_resume_workspace_action_options(fragments, state)
    _append_resume_workspace_action_footer(fragments)
    return fragments


def _append_resume_workspace_action_header(
    fragments: list[tuple[str, str]],
    *,
    recorded_path: Path,
    current: Path,
) -> None:
    """
    Append the action selector header.

    :param fragments: Fragment list being built.
    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :returns: None.
    """
    fragments.extend(
        [
            ("class:title", "Resume from another directory\n"),
            ("class:muted", "Started in: "),
            ("", f"{recorded_path}\n"),
            ("class:muted", "Current:    "),
            ("", f"{current}\n\n"),
        ]
    )


def _append_resume_workspace_action_options(
    fragments: list[tuple[str, str]],
    state: _ResumeWorkspaceActionPickerState,
) -> None:
    """
    Append selectable action rows.

    :param fragments: Fragment list being built.
    :param state: Mutable picker state.
    :returns: None.
    """
    for index, option in enumerate(state.options):
        selected = index == state.selected_index
        marker_style = "class:accent-bold" if selected else "class:muted"
        text_style = "class:selected" if selected else ""
        fragments.extend(
            [
                (marker_style, "> " if selected else "  "),
                (text_style, option.label),
                ("", "\n"),
            ]
        )


def _append_resume_workspace_action_footer(fragments: list[tuple[str, str]]) -> None:
    """
    Append the action selector keybinding footer.

    :param fragments: Fragment list being built.
    :returns: None.
    """
    fragments.extend(
        [
            ("", "\n"),
            ("class:muted", "Keys: "),
            ("class:accent-bold", "↑"),
            ("class:muted", "/"),
            ("class:accent-bold", "↓"),
            ("class:muted", " move  ·  "),
            ("class:accent-bold", "Enter"),
            ("class:muted", " select  ·  "),
            ("class:accent-bold", "Esc"),
            ("class:muted", "/"),
            ("class:accent-bold", "q"),
            ("class:muted", " leave\n"),
        ]
    )


def _has_running_event_loop() -> bool:
    """
    Return whether the current thread is already running asyncio.

    prompt-toolkit's synchronous runner calls :func:`asyncio.run` by
    default, so nested use from an active async caller has to run the
    prompt-toolkit application in a worker thread.

    :returns: ``True`` when an asyncio loop is active in this thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _stream_is_tty(stream: IO[str]) -> bool:
    """
    Return whether *stream* is attached to a terminal.

    :param stream: Text stream to inspect, e.g. ``sys.stdin``.
    :returns: ``True`` when ``stream.isatty()`` reports a TTY.
    """
    return bool(stream.isatty())


def _switch_to_recorded_working_directory(recorded_path: Path) -> None:
    """
    Switch process cwd to *recorded_path* for Claude resume.

    :param recorded_path: Existing recorded launch cwd.
    :returns: None.
    """
    os.chdir(recorded_path)
    click.echo(f"Switched to {recorded_path}.", err=True)


def _fetch_external_session_id_for_redirect(
    *,
    base_url: str | None,
    headers: dict[str, str],
    session_id: str,
) -> str | None:
    """
    Fetch Claude's external session id for optional redirect.

    Redirect is an optional convenience layered on top of the normal
    resume path. If the lookup fails, return ``None`` and leave the
    regular switch / leave behavior available; the later cold resume
    path still performs the authoritative server validation.

    :param base_url: Omnigent server base URL, or ``None`` when unavailable.
    :param headers: HTTP headers for the Omnigent request.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: Claude session id, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"``, or ``None``.
    """
    if base_url is None:
        return None
    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=10.0) as client:
            resp = client.get(f"/v1/sessions/{url_component(session_id)}")
        if resp.status_code >= 400:
            return None
        payload = resp.json()
    except Exception:  # noqa: BLE001 - optional redirect preflight
        _logger.warning(
            "failed to fetch external Claude session id for redirect; session=%s",
            session_id,
            exc_info=True,
        )
        return None
    external_session_id = payload.get("external_session_id") if isinstance(payload, dict) else None
    if not isinstance(external_session_id, str) or not external_session_id:
        return None
    return external_session_id


def _redirect_available(external_session_id: str | None) -> bool:
    """
    Return whether a Claude transcript can be redirected.

    :param external_session_id: Claude session id, or ``None``.
    :returns: ``True`` when a matching local transcript exists.
    """
    if external_session_id is None:
        return False
    return _find_claude_transcript(external_session_id) is not None


def _redirect_claude_transcript_to_current_project(
    *,
    session_id: str,
    external_session_id: str,
    current: Path,
) -> Path:
    """
    Move a Claude transcript into the current cwd's Claude project.

    The moved JSONL gets top-level ``cwd`` fields rewritten to
    *current* so Claude sees the session as belonging to the current
    project directory. The old transcript file is removed after the
    new file is safely in place; a Claude session id has exactly one
    local project owner.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param external_session_id: Claude session id / transcript stem,
        e.g. ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param current: Current cwd, already resolved.
    :returns: Path to the moved transcript.
    :raises click.ClickException: If the source transcript is missing
        or unsafe.
    """
    target_dir = _claude_project_dir_for_cwd(current)
    target = target_dir / f"{external_session_id}.jsonl"
    source = _find_claude_transcript(external_session_id, exclude=target)
    if source is None and target.is_file():
        redirect_launch_state(session_id, str(current))
        return target
    if source is None:
        raise click.ClickException(
            f"Claude transcript {external_session_id!r} was not found under "
            f"{_CLAUDE_PROJECTS_DIR}."
        )
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    try:
        _copy_transcript_with_cwd(source=source, target=tmp, current=current)
        os.replace(tmp, target)
        source.unlink()
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    redirect_launch_state(session_id, str(current))
    click.echo(f"Moved Claude transcript to: {target}", err=True)
    return target


def _copy_transcript_with_cwd(
    *, source: Path, target: Path, current: Path, new_session_id: str | None = None
) -> None:
    """
    Copy *source* JSONL to *target* while rewriting top-level cwd.

    :param source: Existing Claude transcript JSONL.
    :param target: Temporary output path.
    :param current: Current cwd to write into top-level ``cwd`` fields.
    :param new_session_id: When set, also rewrite each record's
        top-level ``sessionId`` to this value, e.g.
        ``"ca414b0e-..."``. Used by :func:`_clone_claude_transcript`
        (forked clone) so the copied transcript belongs to the
        clone's own Claude session id rather than the source's. ``None``
        (the cwd-only redirect/move path) leaves ``sessionId`` untouched.
        The ``uuid`` / ``parentUuid`` chain is preserved verbatim in
        either case.
    :returns: None.
    :raises click.ClickException: If a transcript line is malformed.
    """
    current_text = str(current)
    with source.open("r", encoding="utf-8") as src, target.open("w", encoding="utf-8") as dst:
        for line_number, line in enumerate(src, start=1):
            if not line.strip():
                dst.write(line)
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise click.ClickException(
                    f"Cannot redirect malformed Claude transcript {source}: "
                    f"line {line_number} is not valid JSON."
                ) from exc
            if isinstance(payload, dict):
                if isinstance(payload.get("cwd"), str):
                    payload["cwd"] = current_text
                if new_session_id is not None and isinstance(payload.get("sessionId"), str):
                    payload["sessionId"] = new_session_id
            dst.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _clone_claude_transcript(
    *,
    source_external_session_id: str,
    target_external_session_id: str,
    clone_workspace: Path,
) -> Path | None:
    """
    Clone a source Claude transcript into the clone's project dir.

    Used to carry a forked claude-native session's history into the
    clone. We copy the source transcript ourselves into the clone's OWN
    project dir (``~/.claude/projects/<enc(clone_workspace)>/``) under a
    uuid we assign, rewriting per-record ``sessionId`` →
    *target_external_session_id* and ``cwd`` → *clone_workspace* (the
    ``uuid`` / ``parentUuid`` chain is preserved). The clone then
    launches plain ``--resume <target_external_session_id>``. Writing
    the file ourselves (rather than asking Claude to branch the source
    via ``--fork-session``) is what makes the worktree case work and
    avoids a double-render: the file is fully written before launch, so
    the forwarder's ``start_at_end`` seeks past the copied prefix, and
    it lives in the clone's own project dir, so cwd-scoped ``--resume``
    finds it regardless of which dir/worktree the clone runs in. See
    designs/FORK_SESSION_UX.md.

    :param source_external_session_id: The SOURCE session's Claude id /
        transcript stem to copy from, e.g.
        ``"d39070df-e10a-4de9-b078-a11b35d5b1fc"``.
    :param target_external_session_id: The uuid to assign the clone's
        copied transcript, e.g. ``"ca414b0e-..."``. Must be a safe
        transcript stem; the clone's ``external_session_id`` is set to
        this so a later relaunch resumes it via the normal cold-resume
        path.
    :param clone_workspace: The resolved directory the clone will run
        in (its worktree or same dir). Determines the destination
        project dir and the rewritten ``cwd`` value. Pass an
        already-resolved path (symlinks collapsed) so the project-dir
        encoding matches what Claude computes.
    :returns: Path to the written clone transcript, or ``None`` when the
        target id is unsafe or the source transcript can't be found on
        this host (caller launches fresh in that case).
    :raises click.ClickException: If the source transcript is malformed
        or the clone can't be written.
    """
    if not _CLAUDE_SESSION_ID_RE.fullmatch(target_external_session_id):
        return None
    source = _find_claude_transcript(source_external_session_id)
    if source is None:
        return None
    target_dir = _claude_project_dir_for_cwd(clone_workspace)
    target = target_dir / f"{target_external_session_id}.jsonl"
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    try:
        _copy_transcript_with_cwd(
            source=source,
            target=tmp,
            current=clone_workspace,
            new_session_id=target_external_session_id,
        )
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return target


def _find_claude_transcript(
    external_session_id: str, *, exclude: Path | None = None
) -> Path | None:
    """
    Find a local Claude transcript by session id.

    :param external_session_id: Claude session id / transcript stem,
        e.g. ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param exclude: Transcript path to ignore, e.g. the redirect
        target for the current cwd. ``None`` means include all matches.
    :returns: Transcript path, or ``None`` when absent.
    """
    if not _CLAUDE_SESSION_ID_RE.fullmatch(external_session_id):
        return None
    if not _CLAUDE_PROJECTS_DIR.is_dir():
        return None
    matches: list[Path] = []
    filename = f"{external_session_id}.jsonl"
    excluded = exclude.resolve() if exclude is not None else None
    for project_dir in _CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / filename
        if candidate.is_file() and (excluded is None or candidate.resolve() != excluded):
            matches.append(candidate)
    if not matches:
        return None
    matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0]


def _claude_project_dir_for_cwd(cwd: Path) -> Path:
    """
    Return Claude's project transcript directory for *cwd*.

    Claude Code stores transcripts under
    ``~/.claude/projects/<sanitized-cwd>/``. The observed sanitizer
    replaces non-alphanumeric path characters with ``-``.

    :param cwd: Absolute cwd, e.g. ``Path("/home/me/repo")``.
    :returns: Claude project transcript directory.
    """
    return _CLAUDE_PROJECTS_DIR / _sanitize_claude_project_name(str(cwd))


def _sanitize_claude_project_name(path: str) -> str:
    """
    Sanitize an absolute path the way Claude names project dirs.

    :param path: Absolute path, e.g. ``"/home/me/repo"``.
    :returns: Sanitized name, e.g. ``"-home-me-repo"``.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _record_launch_for_fresh_session(session_id: str) -> None:
    """
    Persist the wrapper's current cwd as the session's launch state.

    Called on the fresh-session path after
    :func:`_prepare_claude_terminal` returns a new conversation id
    but before attaching the terminal. The recorded value drives
    the resume-time chdir prompt on subsequent invocations.

    Best-effort: a failed write is logged and swallowed. The launch
    state is a UX nicety, not a correctness primitive -- a single-
    inode write failure shouldn't crash the wrapper between session
    creation and attach (the user would be left with a usable
    session they can't terminate cleanly). The fallout from a
    missing record is just "no chdir prompt on resume", which is
    the same as a legacy session.

    :param session_id: Newly created Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :returns: None.
    """
    try:
        write_launch_state(session_id, str(Path.cwd().resolve()))
    except OSError:
        # File-system error (read-only fs, disk full, permission
        # denied). Log and proceed -- attach still works; the user
        # just won't get the chdir prompt on resume.
        _logger.warning(
            "failed to record launch state for %s",
            session_id,
            exc_info=True,
        )


def _strip_resume_from_claude_args(args: tuple[str, ...]) -> tuple[str, ...]:
    """
    Strip any stray ``--resume`` / ``-r`` (and value) from raw args.

    Defense in depth: a user routing ``--resume`` past Click (e.g.
    ``omnigent claude -- --resume <id>``) must not have it reach
    upstream Claude, which would apply it to its own session-id
    namespace.

    :param args: Raw ``claude_args`` from Click pass-through.
    :returns: Args with stray ``--resume`` / ``-r`` removed.
    """
    out: list[str] = []
    consume_value = False
    for arg in args:
        if consume_value:
            consume_value = False
            # Only swallow the next token if it looks like a value, not a flag.
            # ``-- --resume --foo`` should drop ``--resume`` but keep ``--foo``.
            if not arg.startswith("-"):
                _logger.warning("Stripped stray --resume value %r from claude args.", arg)
                continue
            out.append(arg)
            continue
        if arg in ("--resume", "-r"):
            _logger.warning(
                "Stripped stray %s from claude args; use `omnigent claude --resume`.", arg
            )
            consume_value = True
            continue
        if arg.startswith(("--resume=", "-r=")):
            _logger.warning("Stripped stray %s from claude args.", arg.split("=", 1)[0])
            continue
        out.append(arg)
    return tuple(out)


def _ucode_config_for_profile(profile: str | None) -> ClaudeNativeUcodeConfig | None:
    """
    Resolve native Claude Code launch config from ucode state.

    The profile remains the explicit workspace selector. If no
    profile is selected, or the profile has no matching ucode state,
    the native wrapper leaves Claude Code's normal provider
    configuration alone.

    :param profile: Databricks CLI profile name, e.g.
        ``"<your-profile>"``.
    :returns: Ucode-derived launch config, or ``None`` when no matching
        ucode state exists.
    :raises click.ClickException: If the selected workspace has a
        malformed Claude ucode agent entry.
    """
    if not profile:
        return None

    from omnigent.onboarding.databricks_config import (
        DATABRICKS_CLAUDE_DEFAULT_MODEL,
        get_workspace_url_for_profile,
    )
    from omnigent.onboarding.ucode_state import read_ucode_state

    workspace_url = get_workspace_url_for_profile(profile)
    if workspace_url is None:
        return None
    workspace_state = read_ucode_state(workspace_url)
    if workspace_state is None:
        return None
    agent_state = workspace_state.agent(_UCODE_CLAUDE_AGENT_NAME)
    if agent_state is None:
        raise click.ClickException(
            f"ucode state for profile {profile!r} does not include a Claude agent entry. "
            "Run `omnigent setup --internal-beta` to refresh ucode configuration."
        )

    base_url = agent_state.env.get(_UCODE_CLAUDE_BASE_URL_ENV) or agent_state.base_url
    if base_url is None:
        base_url = agent_state.base_urls.get(_UCODE_CLAUDE_AGENT_NAME)
    if not base_url:
        raise click.ClickException(
            f"ucode state for profile {profile!r} is missing Claude base URL "
            f"({_UCODE_CLAUDE_BASE_URL_ENV} / base_url). "
            "Run `omnigent setup --internal-beta` to refresh ucode configuration."
        )
    if not agent_state.auth_command:
        raise click.ClickException(
            f"ucode state for profile {profile!r} is missing Claude auth_command. "
            "Run `omnigent setup --internal-beta` to refresh ucode configuration."
        )

    refresh_interval_ms = (
        agent_state.auth_refresh_interval_ms or _DEFAULT_UCODE_AUTH_REFRESH_INTERVAL_MS
    )
    env: dict[str, str] = {
        _UCODE_CLAUDE_BASE_URL_ENV: base_url,
        _CLAUDE_CODE_API_KEY_HELPER_TTL_ENV: str(refresh_interval_ms),
        _CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS_ENV: "1",
    }
    # Pin each Claude Code model-tier alias to the corresponding Databricks
    # gateway model ID so that the /model picker natively shows gateway model
    # names.  Without this Claude Code normalises the picked model to a
    # canonical Anthropic name (e.g. "claude-opus-4-7[1m]") that the
    # Databricks gateway rejects.
    for tier, env_var in _UCODE_CLAUDE_TIER_TO_ENV.items():
        model_id = workspace_state.claude_models.get(tier)
        if model_id:
            env[env_var] = model_id
    # When ucode caches no model, default it so Claude Code doesn't fall back
    # to its host-config model (an Anthropic-direct id the gateway rejects).
    return ClaudeNativeUcodeConfig(
        env=env,
        api_key_helper=agent_state.auth_command,
        model=agent_state.model or DATABRICKS_CLAUDE_DEFAULT_MODEL,
    )


def _provider_config_for_native_claude(entry: ProviderEntry) -> ClaudeNativeUcodeConfig | None:
    """Build native Claude Code launch config from a generic provider.

    The OSS counterpart to :func:`_ucode_config_for_profile`: it takes a
    resolved ``key`` / ``gateway`` / ``local`` provider serving the
    ``anthropic`` surface and injects the same knobs the native CLI needs —
    ``ANTHROPIC_BASE_URL`` plus a token ``apiKeyHelper`` and the default
    model — so a Claude Code terminal launched by ``omnigent`` routes
    through the configured provider exactly like the in-process claude-sdk
    harness does (:func:`omnigent.runtime.workflow.configure_agent_harness_with_provider`).

    :param entry: A resolved provider entry. Only ``key`` / ``gateway`` /
        ``local`` kinds serving the ``anthropic`` family produce a config.
    :returns: The launch config, or ``None`` when the provider does not
        serve the anthropic surface or carries no usable credential (the
        caller then falls back to the CLI's own login).
    """
    from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY

    family = entry.family(ANTHROPIC_FAMILY)
    if family is None:
        _logger.warning(
            "native-claude: provider %r is the Claude default but does not serve the "
            "anthropic surface — falling back to Claude Code's own login.",
            entry.name,
        )
        return None
    # Token delivery mirrors the claude-sdk executor: a dynamic auth_command
    # is used verbatim; a static key becomes a ``printf`` apiKeyHelper (the
    # runner env allowlist excludes ANTHROPIC_API_KEY, so the key must reach
    # Claude Code via the helper, not the environment).
    if family.auth_command:
        api_key_helper = family.auth_command
    elif family.api_key:
        api_key_helper = f"printf %s {shlex.quote(family.api_key)}"
    else:
        _logger.warning(
            "native-claude: provider %r is the Claude default but has no usable "
            "credential — falling back to Claude Code's own login.",
            entry.name,
        )
        return None
    _logger.info(
        "native-claude routing: provider %r (base_url=%s, model=%s)",
        entry.name,
        family.base_url,
        family.default_model,
    )
    return ClaudeNativeUcodeConfig(
        env={
            _UCODE_CLAUDE_BASE_URL_ENV: family.base_url,
            # Disable Claude Code's experimental anthropic-beta flags. Gateways
            # (Databricks serving-endpoints and the like) reject beta flags they
            # don't implement with a 400 "invalid beta flag", which kills every
            # turn. The ucode/databricks path already sets this; mirror it here
            # so the generic key/gateway/local provider path is equally
            # gateway-safe.
            _CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS_ENV: "1",
        },
        api_key_helper=api_key_helper,
        model=family.default_model,
    )


def _bedrock_config_for_native_claude(entry: ProviderEntry) -> ClaudeNativeUcodeConfig | None:
    """Build native Claude Code launch config for Bedrock-style gateways.

    AWS Bedrock and Bedrock-compatible gateways (like corporate AI gateways)
    use a different set of environment variables than the standard Anthropic API:
    - ``ANTHROPIC_BEDROCK_BASE_URL`` instead of ``ANTHROPIC_BASE_URL``
    - ``AWS_BEARER_TOKEN_BEDROCK`` for the API key
    - ``CLAUDE_CODE_USE_BEDROCK=1`` to enable Bedrock mode

    :param entry: A resolved provider entry with ``kind="bedrock"``.
    :returns: The launch config, or ``None`` when credentials are missing.
    """
    from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY

    family = entry.family(ANTHROPIC_FAMILY)
    if family is None:
        _logger.warning(
            "native-claude: bedrock provider %r does not serve the anthropic surface "
            "— falling back to Claude Code's own login.",
            entry.name,
        )
        return None
    if not family.api_key:
        _logger.warning(
            "native-claude: bedrock provider %r has no usable credential "
            "— falling back to Claude Code's own login.",
            entry.name,
        )
        return None
    _logger.info(
        "native-claude routing: bedrock provider %r (base_url=%s, model=%s)",
        entry.name,
        family.base_url,
        family.default_model,
    )
    return ClaudeNativeUcodeConfig(
        env={
            _ANTHROPIC_BEDROCK_BASE_URL_ENV: family.base_url,
            _AWS_BEARER_TOKEN_BEDROCK_ENV: family.api_key,
            _CLAUDE_CODE_USE_BEDROCK_ENV: "1",
            _CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS_ENV: "1",
        },
        api_key_helper="echo bedrock-auth-via-env",
        model=family.default_model,
    )


def _native_claude_config_from_entry(
    entry: ProviderEntry,
) -> ClaudeNativeUcodeConfig | None:
    """Map a resolved provider entry to a native Claude launch config.

    - ``key`` / ``gateway`` / ``local`` → provider gateway config
      (:func:`_provider_config_for_native_claude`).
    - ``bedrock`` → Bedrock-style gateway config
      (:func:`_bedrock_config_for_native_claude`).
    - ``databricks`` → the existing ucode path keyed on the provider profile.
    - ``subscription`` → ``None`` (use the ``claude`` CLI's own login, e.g. a
      Claude Enterprise seat) — intentional, not a fallback to ucode.

    :param entry: The resolved provider entry.
    :returns: The launch config, or ``None`` to use Claude's own login.
    """
    from omnigent.onboarding.provider_config import (
        BEDROCK_KIND,
        DATABRICKS_KIND,
        GATEWAY_KIND,
        KEY_KIND,
        LOCAL_KIND,
    )

    if entry.kind in (KEY_KIND, GATEWAY_KIND, LOCAL_KIND):
        return _provider_config_for_native_claude(entry)
    if entry.kind == BEDROCK_KIND:
        return _bedrock_config_for_native_claude(entry)
    if entry.kind == DATABRICKS_KIND:
        _logger.info("native-claude routing: Databricks ucode profile %r", entry.profile)
        return _ucode_config_for_profile(entry.profile)
    _logger.info("native-claude routing: Claude CLI login (subscription provider %r)", entry.name)
    return None


def resolve_native_claude_config(
    *,
    spec: AgentSpec | None,
) -> ClaudeNativeUcodeConfig | None:
    """Resolve the native Claude Code launch config across all offerings.

    The single entry point both native-claude launch paths use (the CLI
    ``omnigent claude`` and the runner's host-spawned auto-create), so the
    native harness honors ``omnigent setup`` exactly like the in-process
    claude-sdk harness. Precedence mirrors
    :func:`omnigent.runtime.workflow._resolve_provider_for_build`:

    1. when a *spec* is given, its resolved provider (spec ``executor.auth``
       → explicit per-family default → global ``auth:`` → ``databricks-*``
       model → ambient detection), falling back to the spec's own
       ``executor.profile`` (ucode) when it routed to legacy databricks;
    2. when spec-less (``omnigent claude``): an explicit per-family default
       → global ``auth:`` (→ ucode) → ambient detection;
    3. otherwise ``None`` (Claude's own login).

    Credentials are controlled exclusively by the spec or by
    ``omnigent setup`` provider config — there is no CLI/env profile
    override.

    :param spec: The agent spec, or ``None`` for the bare ``omnigent
        claude`` launch.
    :returns: The launch config, or ``None`` to use Claude's own login.
    """
    from omnigent.onboarding.detected import effective_config_with_detected
    from omnigent.onboarding.provider_config import (
        default_provider_for_harness,
        load_config,
    )
    from omnigent.runtime.workflow import _load_global_auth, _resolve_provider_for_build
    from omnigent.spec.types import DatabricksAuth

    # 1. Spec-driven: reuse the harness routing precedence verbatim. A
    #    non-None entry decides the config (including a deliberate None for a
    #    subscription); a None entry means the spec routed to databricks /
    #    global auth → fall back to the spec's own ucode profile.
    if spec is not None:
        entry = _resolve_provider_for_build(spec, harness_type="claude-sdk")
        if entry is not None:
            return _native_claude_config_from_entry(entry)
        return _ucode_config_for_profile(spec.executor.profile)

    # 2. Spec-less (omnigent claude): explicit default wins first.
    explicit = load_config()
    entry = default_provider_for_harness(explicit, "claude-sdk")
    if entry is not None:
        return _native_claude_config_from_entry(entry)
    # A global databricks auth block → ucode.
    global_auth = _load_global_auth()
    if isinstance(global_auth, DatabricksAuth):
        return _ucode_config_for_profile(global_auth.profile)
    if global_auth is not None:
        # A global api_key auth: let Claude's own login handle it (parity
        # with the subscription path); the in-process harness would inject
        # it, but the native CLI uses its configured account.
        return None
    # 3. Ambient detection (first run without configure).
    entry = default_provider_for_harness(effective_config_with_detected(explicit), "claude-sdk")
    if entry is not None:
        return _native_claude_config_from_entry(entry)
    _logger.info(
        "native-claude routing: Claude CLI login (no provider configured for the Claude "
        "harness, no Databricks profile). Run `omnigent setup --no-internal-beta` to route "
        "through a provider."
    )
    return None


def _materialize_claude_agent_spec(tmpdir: Path) -> Path:
    """
    Write the terminal-first session agent spec used by ``omnigent claude``.

    :param tmpdir: Temporary directory for the generated YAML file.
    :returns: Path to a generated YAML spec.
    """
    yaml_path = tmpdir / "claude-native-ui.yaml"
    raw = {
        "name": _AGENT_NAME,
        "prompt": (
            "Claude Code is running in the session terminal. Web UI messages are "
            "forwarded into that Claude Code process through the native bridge."
        ),
        "executor": {
            "harness": "claude-native",
            # Conservative pre-first-turn default; the forwarder
            # overrides it via ``external_session_usage`` once the
            # real model + ``[1m]`` alias are observed.
            "context_window": 200_000,
        },
        # Opt the native session into the child-session spawn writes
        # (sys_session_create / sys_session_send / sys_session_close)
        # so the wrapped Claude Code can author agent configs and
        # launch them as sub-agent sessions. The relay derives its
        # advertised tool set from this spec via ToolManager.
        "spawn": True,
        # Without an ``os_env`` block, the runner's filesystem APIs
        # (``/resources/environments/default/filesystem`` and siblings)
        # return 404 — see ``_require_os_env`` in
        # ``omnigent/runner/app.py``. Claude Code already operates
        # on the user's workspace with full filesystem access, so the
        # caller process / no sandbox combination matches reality and
        # enables the web UI's files panel.
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
        # Declare a default shell terminal so the relay advertises the
        # ``sys_terminal_*`` family to the wrapped Claude Code (the
        # relay's gate is a non-empty ``terminals:`` block on this
        # spec). Caller process / no sandbox matches the ``os_env``
        # stance above — the native CLI already runs unsandboxed on
        # the user's workspace.
        "terminals": {
            "shell": {
                "command": "bash",
                "allow_cwd_override": True,
                "os_env": {
                    "type": "caller_process",
                    "cwd": ".",
                    "sandbox": {"type": "none"},
                },
            },
        },
    }
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return yaml_path


def _run_with_local_server(
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    claude_args: tuple[str, ...],
    command: str,
    claude_config: ClaudeNativeUcodeConfig | None = None,
    auto_open_conversation: bool = False,
    startup_profiler: StartupProfiler | None = None,
) -> None:
    """
    Start a local Omnigent server, launch Claude, and attach to it.

    :param spec_path: Generated Claude wrapper agent spec.
    :param session_id: Optional existing session id.
    :param resume_picker: When ``True`` and ``session_id is None``, run the picker.
    :param claude_args: Claude CLI args.
    :param command: Executable to run in the terminal resource.
    :param claude_config: Optional ucode-derived Claude Code config.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL after the session is prepared.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :returns: None.
    """
    from omnigent.chat import (
        _bundle_agent,
        _find_free_port,
        _start_local_server,
        _stop_local_server,
        _wait_for_server,
    )

    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    startup_profiler.mark("local server selecting port")
    port = _find_free_port()
    startup_profiler.mark("local server port selected", detail=f"port={port}")
    server_handle = _start_local_server(spec_path, port, ephemeral=False)
    startup_profiler.mark("local server process started")
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(port, server_handle)
        startup_profiler.mark("local server healthy")
        resolved_session_id = _resolve_session_id_for_resume(
            base_url=base_url,
            headers={},
            session_id=session_id,
            resume_picker=resume_picker,
        )
        startup_profiler.mark(
            "session resolved",
            detail="fresh" if resolved_session_id is None else "resume",
        )
        if resolved_session_id is None and resume_picker and session_id is None:
            # Picker cancelled — exit before creating a session the user declined.
            return
        if resolved_session_id is not None:
            # Resume path: bring the wrapper's cwd in line with the
            # session's recorded launch cwd BEFORE the bundle / runner
            # / terminal-launch steps sample ``Path.cwd()``. Local
            # server is already up at this point but is cwd-
            # independent (writes to ``~/.omnigent/``), so chdiring
            # now is safe.
            _align_working_directory_with_session(
                resolved_session_id,
                base_url=base_url,
                headers={},
            )
            startup_profiler.mark("resume workspace aligned")
        with runner_startup_progress(initial_message="Preparing Claude...") as progress:
            _mark_startup_step(
                startup_profiler,
                "bundling local agent",
                startup_progress=progress,
            )
            bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
            _mark_startup_step(
                startup_profiler,
                "local agent bundle ready",
                startup_progress=progress,
            )
            _mark_startup_step(
                startup_profiler,
                "preparing local terminal",
                startup_progress=progress,
            )
            prepared = asyncio.run(
                _prepare_claude_terminal(
                    base_url=base_url,
                    headers={},
                    session_id=resolved_session_id,
                    runner_id=server_handle.runner_id,
                    session_bundle=bundle,
                    claude_args=claude_args,
                    command=command,
                    claude_config=claude_config,
                    startup_profiler=startup_profiler,
                    startup_progress=progress,
                )
            )
            _mark_startup_step(
                startup_profiler,
                "local terminal prepared",
                startup_progress=progress,
                detail=_tmux_profile_detail(prepared),
            )
        if resolved_session_id is None:
            # Fresh-session path: now that the server has assigned a
            # conv id, persist the cwd we used at create time so a
            # future ``--resume`` can detect mismatches.
            _record_launch_for_fresh_session(prepared.session_id)
            startup_profiler.mark("fresh session launch state recorded")
        click.echo(f"Web UI: {conversation_url(base_url, prepared.session_id)}", err=True)
        startup_profiler.mark("web ui url printed")
        open_conversation_link_if_enabled(
            base_url=base_url,
            conversation_id=prepared.session_id,
            enabled=auto_open_conversation,
            warn=lambda message: click.echo(message, err=True),
        )
        startup_profiler.mark("opening terminal attach")
        asyncio.run(
            _attach_with_transcript_forwarder(
                base_url=base_url,
                headers={},
                prepared=prepared,
                agent_name=_AGENT_NAME,
                attach_url=_attach_url(base_url, prepared.session_id, prepared.terminal_id),
                attach=attach_local_terminal,
                startup_profiler=startup_profiler,
            )
        )
        if resolved_session_id is None:
            active_session_id = read_active_session_id(prepared.bridge_dir) or prepared.session_id
            echo_native_resume_hint(
                native_command="claude",
                session_id=active_session_id,
            )
    finally:
        _stop_local_server(server_handle)


def _tmux_profile_detail(prepared: PreparedClaudeTerminal) -> str:
    """
    Return profile detail for the terminal attach path.

    :param prepared: Prepared Claude terminal details.
    :returns: Human-readable attach-path detail, e.g.
        ``"direct-tmux target=main"`` or ``"websocket attach"``.
    """
    if (
        isinstance(prepared.tmux_socket, Path)
        and prepared.tmux_target is not None
        and _can_attach_direct_tmux(prepared)
    ):
        return f"direct-tmux target={prepared.tmux_target}"
    if prepared.tmux_socket is not None and prepared.tmux_target is not None:
        return "websocket attach (tmux socket not local)"
    return "websocket attach"


class _AttachOutcome(Enum):
    """How a local Claude attach session ended.

    Distinguishes a user *detach* (tmux still alive, the runner should
    keep serving the web UI) from a real exit so the remote launcher
    can decide whether to tear the local runner down.

    :cvar EXITED: The user quit (stdin EOF / Ctrl-D), the terminal is
        gone, or the WS closed for a reason that ends the session. The
        launcher tears down the runner and Omnigent terminal resource.
    :cvar DETACHED: The user detached from tmux (close code 4405). The
        tmux session — and therefore Claude — is still running; the
        launcher adopts the runner so it outlives the local CLI and the
        web UI stays connected.
    """

    EXITED = "exited"
    DETACHED = "detached"


def _can_attach_direct_tmux(prepared: PreparedClaudeTerminal) -> bool:
    """
    Return whether this process can attach to the runner tmux directly.

    ``True`` only when the runner advertised a tmux socket + target, the
    socket exists on this host (so the runner shares this machine), and
    ``tmux`` is on PATH. This is the same-machine fast path: it wires the
    local TTY straight to the runner's tmux pane instead of relaying
    every keystroke over the WebSocket PTY bridge. A remote runner's
    socket won't exist locally, so this returns ``False`` and the caller
    falls back to the WebSocket attach. Mirrors the Codex wrapper's
    :func:`omnigent.codex_native._can_attach_direct_tmux`.

    :param prepared: Prepared terminal details.
    :returns: ``True`` when a direct local tmux attach is possible.
    """
    return (
        prepared.tmux_socket is not None
        and prepared.tmux_target is not None
        and prepared.tmux_socket.exists()
        and shutil.which("tmux") is not None
    )


async def _attach_direct_tmux(
    socket_path: Path,
    tmux_target: str,
    *,
    startup_profiler: StartupProfiler | None = None,
) -> _AttachOutcome:
    """
    Attach the current terminal directly to the runner-owned tmux pane.

    Lower latency than the WebSocket PTY relay because there is no
    server round-trip — the local TTY drives the runner's private tmux
    server over its Unix socket. ``TMUX`` is dropped from the child
    environment so a user who runs ``omnigent claude`` from inside
    their own tmux can still attach to Omnigent' server. After the
    ``tmux attach`` child exits, a ``has-session`` probe distinguishes a
    user *detach* (session still alive → keep the Omnigent terminal resource
    live) from Claude *exiting* (session gone → caller closes the
    resource), matching the WebSocket path's 4405-vs-4404 semantics.

    :param socket_path: Runner tmux server socket path.
    :param tmux_target: tmux ``-t`` target to attach, e.g. ``"main"``.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :returns: :attr:`_AttachOutcome.DETACHED` when the tmux session
        outlives the attach (user detached), else
        :attr:`_AttachOutcome.EXITED`.
    """
    from omnigent.terminals.ws_bridge import _tmux_session_alive

    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    env = dict(os.environ)
    env.pop("TMUX", None)
    startup_profiler.mark("starting tmux attach subprocess", detail=f"target={tmux_target}")
    process = await asyncio.create_subprocess_exec(
        "tmux",
        "-S",
        str(socket_path),
        "-f",
        os.devnull,
        "attach",
        "-t",
        tmux_target,
        env=env,
    )
    startup_profiler.mark("tmux attach subprocess started")
    await process.wait()
    startup_profiler.mark("tmux attach subprocess exited")
    if await _tmux_session_alive(str(socket_path), tmux_target):
        return _AttachOutcome.DETACHED
    return _AttachOutcome.EXITED


async def _attach_with_transcript_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    prepared: PreparedClaudeTerminal,
    agent_name: str,
    attach_url: str,
    attach: Callable[..., Any],
    recover: Callable[[], Awaitable[None]] | None = None,
    auth: httpx.Auth | None = None,
    run_transcript_forwarder: bool = True,
    startup_profiler: StartupProfiler | None = None,
) -> _AttachOutcome:
    """
    Attach to the terminal and optionally mirror Claude transcript output.

    The attach is wrapped in :func:`_attach_with_reconnect` so a
    server bounce does not end the session — the local runner +
    tmux survive the bounce, and the runner's tunnel reconnects on
    its own backoff. On exit the forwarder is cancelled and the
    AP-side terminal resource is best-effort marked stopped (skipped
    on reattach — the launcher owns teardown).

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP auth headers for Omnigent requests. For
        long-lived remote sessions, ``auth`` (not ``headers``) is the
        authoritative source of the bearer token so OAuth tokens
        refresh transparently per request.
    :param prepared: Prepared terminal details.
    :param agent_name: Agent/model name for mirrored Claude output.
    :param attach_url: Terminal WebSocket URL.
    :param attach: Async attach callable, usually
        :func:`attach_local_terminal`.
    :param recover: Optional async callback invoked between attach
        attempts. ``None`` disables reconnect (local-server flow).
    :param auth: Optional httpx Auth that mints a fresh bearer token
        per request, e.g. ``_server_auth(profile)``. Forwarded to the
        transcript forwarder's HTTP client so Omnigent posts continue to
        authenticate after Databricks OAuth token expiry (~1h).
    :param run_transcript_forwarder: Whether this attach process owns
        Claude transcript forwarding. ``False`` for daemon/runner-owned
        launches, where the runner already started the forwarder for
        the same bridge and a second tailer would duplicate messages.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :returns: How the session ended — :attr:`_AttachOutcome.DETACHED`
        when the user detached from tmux (runner kept alive), else
        :attr:`_AttachOutcome.EXITED`.
    """
    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    # ``start_at_end`` covers both reattach (terminal still live,
    # transcript JSONL still growing) and cold resume (new terminal
    # but ``claude --resume <sid>`` reopens the prior transcript so
    # offset 0 contains turns Omnigent already has from the previous run).
    # See ``PreparedClaudeTerminal.cold_resumed`` for the duplicate-
    # broadcast hazard this avoids.
    skip_existing_transcript = prepared.reattached or prepared.cold_resumed
    forwarder: asyncio.Task[None] | None = None
    if run_transcript_forwarder:
        forwarder = asyncio.create_task(
            supervise_forwarder(
                base_url=base_url,
                headers=headers,
                session_id=prepared.session_id,
                bridge_dir=prepared.bridge_dir,
                agent_name=agent_name,
                start_at_end=skip_existing_transcript,
                auth=auth,
            ),
            name="claude-native-transcript-forwarder",
        )
        startup_profiler.mark("transcript forwarder started")
    else:
        startup_profiler.mark("transcript forwarder skipped")
    outcome = _AttachOutcome.EXITED
    try:
        if _can_attach_direct_tmux(prepared):
            # Same machine as the runner: attach straight to its tmux
            # pane for a lower-latency TTY than the WebSocket PTY relay.
            # Transcript forwarding is owned by whichever process launched
            # the terminal; this attach path only handles the TTY.
            # A remote runner's socket won't exist locally, so we take
            # the WebSocket path instead.
            if prepared.tmux_socket is None or prepared.tmux_target is None:
                # Unreachable — ``_can_attach_direct_tmux`` already
                # checked both — but narrows the types for the call below.
                raise click.ClickException("Claude tmux attach metadata was incomplete.")
            startup_profiler.mark(
                "opening direct tmux attach",
                detail=f"target={prepared.tmux_target}",
            )
            outcome = await _attach_direct_tmux(
                prepared.tmux_socket,
                prepared.tmux_target,
                startup_profiler=startup_profiler,
            )
        else:
            startup_profiler.mark("opening websocket terminal attach")
            outcome = await _attach_with_reconnect(
                attach=attach,
                attach_url=attach_url,
                headers=headers,
                recover=recover,
                base_url=base_url,
                session_id=prepared.session_id,
                terminal_id=prepared.terminal_id,
                bridge_dir=prepared.bridge_dir,
                close_attach_on_terminal_gone=attach is attach_local_terminal,
            )
    finally:
        if forwarder is not None:
            forwarder.cancel()
            try:
                await forwarder
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — cleanup must run regardless
                # The forwarder is best-effort mirroring. A bug there
                # (corrupt transcript JSONL, file-system error, anything
                # uncaught in the parser) must not skip the Omnigent terminal
                # stop call below — otherwise the web UI shows a phantom
                # live terminal after the wrapper exits.
                _logger.warning(
                    "claude-native transcript forwarder raised on shutdown",
                    exc_info=True,
                )
        # On detach the tmux session — and Claude — is still running, so
        # the Omnigent terminal resource must stay live (the web UI keeps
        # rendering it). Only mark it stopped on a real exit.
        if not prepared.reattached and outcome is not _AttachOutcome.DETACHED:
            active_session_id = read_active_session_id(prepared.bridge_dir) or prepared.session_id
            await _close_claude_terminal(
                base_url=base_url,
                headers=headers,
                session_id=active_session_id,
                terminal_id=prepared.terminal_id,
            )
    return outcome


async def _attach_with_reconnect(
    *,
    attach: Callable[..., Any],
    attach_url: str,
    headers: dict[str, str],
    recover: Callable[[], Awaitable[None]] | None,
    base_url: str | None = None,
    session_id: str | None = None,
    terminal_id: str | None = None,
    bridge_dir: Path | None = None,
    active_session_id_reader: Callable[[], str | None] | None = None,
    close_attach_on_terminal_gone: bool = False,
) -> _AttachOutcome:
    """
    Attach to the terminal WebSocket, reconnecting on transient failures.

    The loop exits on user EOF, on SIGTERM/SIGHUP, on tmux detach
    (4405 close), or when the terminal is gone (4404 close, or
    post-close probe reports missing / not-running). Other outcomes —
    connection refused, abnormal close, clean close during a server
    bounce — back off and reattach. ``recover=None`` disables reconnect
    entirely (the local-server flow owns the server lifecycle and has
    nothing to reconnect to); the loop runs ``attach`` once and returns.

    :param attach: One attach attempt; signature
        ``(url, *, headers) -> bool``. ``True`` = user-requested
        exit, ``False`` = WS closed for any other reason. Runtime
        callable is :func:`attach_local_terminal`.
    :param attach_url: ``ws://`` / ``wss://`` terminal-attach URL.
    :param headers: WebSocket handshake headers. Mutated in place by
        ``recover`` so the next handshake sees the refreshed bearer;
        do not rebind.
    :param recover: Optional async callback invoked between attempts
        (not before the first). ``None`` disables reconnect; the
        loop returns after one ``attach`` call. Callback exceptions
        are logged and the loop still retries.
    :param base_url: Omnigent server URL for the post-close terminal probe;
        ``None`` disables the probe.
    :param session_id: Session/conversation id for the probe path.
    :param terminal_id: Terminal resource id for the probe path.
    :param bridge_dir: Native Claude bridge directory. When provided,
        each reconnect reads the active session id so attaches follow
        ``/clear`` terminal transfers.
    :param active_session_id_reader: Optional callback that returns
        the latest active Omnigent session id, e.g. ``"conv_new"``. This is
        used by other terminal-first wrappers that share the reconnect
        loop but store active session state outside Claude's bridge.
    :param close_attach_on_terminal_gone: When ``True``, pass a
        client-side terminal-gone watcher into
        :func:`attach_local_terminal`. The watcher closes the local
        WebSocket as soon as the terminal resource reports stopped, so
        CLI exit does not wait for delayed server-side close
        propagation.
    :returns: :attr:`_AttachOutcome.DETACHED` when the user detached
        from tmux (the runner should be kept alive); otherwise
        :attr:`_AttachOutcome.EXITED`.
    """
    delay = _ATTACH_INITIAL_RECONNECT_DELAY_S
    first_attempt = True
    while True:
        current_session_id = session_id
        if active_session_id_reader is not None:
            current_session_id = active_session_id_reader() or current_session_id
        elif bridge_dir is not None:
            current_session_id = read_active_session_id(bridge_dir) or current_session_id
        current_attach_url = attach_url
        if base_url is not None and current_session_id is not None and terminal_id is not None:
            current_attach_url = _attach_url(base_url, current_session_id, terminal_id)
        if not first_attempt and recover is not None:
            try:
                await recover()
            except Exception:  # noqa: BLE001 — best-effort recovery
                _logger.warning(
                    "claude-native reconnect recovery callback raised; retrying attach anyway",
                    exc_info=True,
                )
        first_attempt = False
        try:
            attach_kwargs: dict[str, Any] = {"headers": headers}
            if (
                close_attach_on_terminal_gone
                and base_url is not None
                and current_session_id is not None
                and terminal_id is not None
            ):

                async def _terminal_gone_probe(
                    *,
                    probe_session_id: str = current_session_id,
                ) -> bool:
                    """
                    Check whether the terminal resource is gone.

                    :param probe_session_id: Session id captured for
                        this attach attempt, e.g. ``"conv_abc123"``.
                    :returns: ``True`` when the Omnigent terminal resource
                        is definitively stopped.
                    """
                    return await _is_terminal_resource_gone(
                        base_url=base_url,
                        headers=headers,
                        session_id=probe_session_id,
                        terminal_id=terminal_id,
                        timeout_s=_CLAUDE_TERMINAL_GONE_WATCH_HTTP_TIMEOUT_S,
                    )

                attach_kwargs["terminal_gone_probe"] = _terminal_gone_probe
            user_requested_exit = await attach(current_attach_url, **attach_kwargs)
        except ConnectionClosed as exc:
            if _is_terminal_detached_close(exc):
                # The user detached from tmux: the session (and Claude)
                # is still alive. Do NOT reconnect or tear anything
                # down — the caller keeps the runner serving the web UI.
                _logger.info("claude-native terminal detached (close 4405); leaving session live")
                return _AttachOutcome.DETACHED
            if _is_terminal_not_found_close(exc):
                latest_session_id = None
                if active_session_id_reader is not None:
                    latest_session_id = active_session_id_reader()
                elif bridge_dir is not None:
                    latest_session_id = read_active_session_id(bridge_dir)
                if latest_session_id and latest_session_id != current_session_id:
                    continue
                _logger.info("claude-native terminal is gone (close 4404); ending session")
                return _AttachOutcome.EXITED
            if recover is None:
                raise
            click.echo(
                f"\nClaude session connection lost ({exc}); reconnecting...",
                err=True,
            )
        except (WebSocketException, OSError, ConnectionError) as exc:
            if recover is None:
                raise
            click.echo(
                f"\nClaude session connection lost ({type(exc).__name__}: {exc}); reconnecting...",
                err=True,
            )
        else:
            if user_requested_exit or recover is None:
                return _AttachOutcome.EXITED
            if base_url is not None and session_id is not None and terminal_id is not None:
                terminal_gone = await _is_terminal_resource_gone(
                    base_url=base_url,
                    headers=headers,
                    session_id=current_session_id,
                    terminal_id=terminal_id,
                )
                if terminal_gone:
                    _logger.info(
                        "claude-native terminal resource is gone after clean close; ending session"
                    )
                    return _AttachOutcome.EXITED
            click.echo(
                "\nClaude session connection closed by server; reconnecting...",
                err=True,
            )
        await _sleep(delay)
        delay = min(delay * 2, _ATTACH_MAX_RECONNECT_DELAY_S)


async def _is_terminal_resource_gone(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    terminal_id: str,
    timeout_s: float = 10.0,
) -> bool:
    """
    Probe the AP-side terminal resource to detect normal exit.

    Called by :func:`_attach_with_reconnect` after a *clean* server-side
    close (the WS ended without raising). That state is ambiguous: it
    can mean a server bounce mid-session (the wrapper should retry) or
    a clean tmux exit because Claude quit (the wrapper should stop).
    The runner's terminal-attach route emits close code ``4404`` when
    the resource is already marked stopped before attach, but a
    teardown that races attach can produce a code-``1000`` close from
    the PTY bridge instead. This GET disambiguates the two states.

    HTTP / connection errors are treated as "not gone" so a server
    that's still bouncing (probe also fails) keeps the wrapper in the
    retry loop instead of exiting prematurely. The 4404 close code
    remains the authoritative kill signal handled in
    :func:`_attach_with_reconnect`.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers for the Omnigent server. Mutated in
        place by the recover callback in remote mode; passing the
        same dict reference picks up the current bearer.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param terminal_id: Terminal resource id, e.g.
        ``"terminal_claude_main"``.
    :param timeout_s: HTTP timeout in seconds for the probe,
        e.g. ``1.0`` for the attach-time watcher.
    :returns: ``True`` when the resource is definitively gone (404 or
        ``metadata.running is False``). ``False`` for any other
        response, including transport errors, so the loop keeps
        retrying.
    """
    path = (
        f"/v1/sessions/{url_component(session_id)}"
        f"/resources/terminals/{url_component(terminal_id)}"
    )
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_s),
        ) as client:
            resp = await client.get(path)
    except (httpx.HTTPError, OSError):
        # Server is likely still bouncing; let the loop retry the
        # attach. The eventual 4404 close (or a subsequent successful
        # attach) decides the outcome.
        return False
    if resp.status_code == 404:
        return True
    if resp.status_code != 200:
        return False
    try:
        payload = resp.json()
    except ValueError:
        return False
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return metadata.get("running") is False


async def _sleep(seconds: float) -> None:
    """
    Stubbable indirection for :func:`asyncio.sleep` in the reconnect
    loop — see ``omnigent-testing`` skill rule 14 for why globally
    patching ``asyncio.sleep`` is banned.

    :param seconds: Delay in seconds.
    :returns: None after the sleep completes.
    """
    await asyncio.sleep(seconds)


def _is_terminal_not_found_close(exc: ConnectionClosed) -> bool:
    """
    Return whether *exc* indicates the terminal resource is gone.

    The runner closes the attach WebSocket with code
    ``WS_CLOSE_TERMINAL_NOT_FOUND`` (``4404``) when there is no
    matching terminal in its resource registry — typically because
    Claude exited and the tmux session terminated. Reconnecting in
    that state would just hit the same close, so the reconnect loop
    treats this code as a terminal exit signal.

    :param exc: WebSocket close exception raised during attach.
    :returns: ``True`` when the close code matches ``4404``;
        ``False`` otherwise (including when the close code is
        unavailable, e.g. for a TCP-level disconnect).
    """
    rcvd = exc.rcvd
    if rcvd is None:
        return False
    return rcvd.code == WS_CLOSE_TERMINAL_NOT_FOUND


def _is_terminal_detached_close(exc: ConnectionClosed) -> bool:
    """
    Return whether *exc* indicates the user detached from tmux.

    The runner's PTY bridge closes the attach WebSocket with code
    ``WS_CLOSE_TERMINAL_DETACHED`` (``4405``) when the ``tmux attach``
    child exits but ``has-session`` confirms the session is still
    alive — i.e. the user pressed the tmux detach key. Unlike a 4404
    (terminal gone), this must NOT end the session: the runner keeps
    running so the web UI stays connected.

    :param exc: WebSocket close exception raised during attach.
    :returns: ``True`` when the close code matches ``4405``;
        ``False`` otherwise (including when the close code is
        unavailable, e.g. for a TCP-level disconnect).
    """
    rcvd = exc.rcvd
    if rcvd is None:
        return False
    return rcvd.code == WS_CLOSE_TERMINAL_DETACHED


async def _close_claude_terminal(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    terminal_id: str,
) -> None:
    """
    Best-effort close of the AP-side Claude terminal resource on exit.

    Issued after the local attach loop returns so subsequent web
    attaches see the resource as stopped rather than waiting on
    runner-disconnect signaling. Failures are intentionally silenced —
    the local wrapper is already exiting and a stop notification is
    not load-bearing.
    """
    path = (
        f"/v1/sessions/{url_component(session_id)}"
        f"/resources/terminals/{url_component(terminal_id)}"
    )
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=httpx.Timeout(10.0)
        ) as client:
            await client.delete(path)


# ── daemon-routed launch (HOST_BY_DEFAULT) ─────────────────
#
# The remote-server flow spawns the runner via the connect daemon
# (``omnigent host``) rather than this CLI: ensure the daemon, ask
# the server to launch a runner on this host, wait for it to come
# online, then wait for the runner to auto-create the Claude terminal
# (``_auto_create_claude_terminal`` in ``runner/app.py``, which also
# applies the ucode gateway auth from the runner's profile) before
# attaching. See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.


async def _wait_for_claude_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> str:
    """
    Poll until the runner has auto-created the Claude terminal.

    A daemon-spawned runner brings the terminal up itself
    (``_auto_create_claude_terminal``) once it is notified of the
    session, so the CLI waits for the resource to appear rather than
    creating it.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param timeout_s: Max seconds to wait, e.g. ``60.0``.
    :returns: The terminal resource id, e.g. ``"terminal_claude_main"``.
    :raises click.ClickException: If no terminal appears in time.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        terminal_id = await _find_running_claude_terminal(client, session_id)
        if terminal_id is not None:
            return terminal_id
        await asyncio.sleep(DAEMON_POLL_INTERVAL_S)
    raise click.ClickException(
        f"The runner did not create the Claude terminal for {session_id!r} "
        f"within {timeout_s:.0f}s."
    )


async def _ensure_claude_terminal_on_runner(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """
    Ask the bound runner to ensure the session's Claude terminal exists.

    Used on the resume path: when the CLI reattaches to a session whose
    daemon runner is still online but whose terminal was torn down (the
    auto-create only fires on session-start, not on runner reuse), this
    POSTs an "ensure" request — no ``spec`` and no ``bridge_inject_dir``,
    which the runner routes to ``_auto_create_claude_terminal`` (the full
    native setup, incl. cold resume) rather than a generic launch. The
    runner makes it idempotent: it returns the live terminal if one is
    already running, so this is a cheap no-op for the common
    runner-and-terminal-still-alive resume.

    Best-effort: a failure here is not fatal — the subsequent
    :func:`_wait_for_claude_terminal_ready` poll surfaces the clear error
    if the terminal still never appears.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :returns: None.
    """
    with contextlib.suppress(httpx.HTTPError):
        await client.post(
            f"/v1/sessions/{url_component(session_id)}/resources/terminals",
            # ``ensure_native_terminal`` is the explicit signal that routes this
            # to the full claude-native auto-create (incl. cold resume) on the
            # runner. A bare ``{terminal, session_key}`` body is ambiguous with
            # a plain generic launch, so the runner keys on this marker — not on
            # the absence of ``spec``/``bridge_inject_dir``.
            json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            timeout=60.0,
        )


async def _prepare_claude_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    claude_args: tuple[str, ...],
    host_id: str,
    workspace: str,
    startup_profiler: StartupProfiler | None = None,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedClaudeTerminal:
    """
    Create/resolve a session and bring its terminal up via the daemon.

    Unlike :func:`_prepare_claude_terminal` (which binds a CLI-spawned
    runner and POSTs the terminal itself), this persists the launch args
    on the session and lets the daemon-spawned runner bring the terminal
    up — applying those args, the persisted model, cold resume, and the
    ucode gateway auth, all runner-side. The session is created *without*
    a bridge-id label so the bridge dir keys by session id, matching the
    runner's auto-create convention. See
    designs/NATIVE_RUNNER_SERVER_LAUNCH.md.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP auth headers for Omnigent requests.
    :param session_id: Existing session id to resume, or ``None`` to
        create a fresh session from *session_bundle*.
    :param session_bundle: Gzipped agent bundle, required when
        *session_id* is ``None``.
    :param claude_args: User pass-through ``claude`` args. ``--resume``
        is stripped (the runner derives it from the session's
        ``external_session_id``); the rest are persisted as the
        session's ``terminal_launch_args`` so the runner launches with
        them. On resume, non-empty args replace the stored set
        (last-write-wins); empty reuses the stored set.
    :param host_id: This machine's host id, e.g. ``"host_abc123"``.
    :param workspace: Absolute host path for the runner cwd, e.g.
        ``"/Users/me/proj"``.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :param startup_progress: Optional user-visible progress renderer,
        e.g. a handle from :func:`runner_startup_progress`.
    :returns: Prepared terminal details (with tmux coordinates when the
        runner is local, enabling the direct-attach fast path).
    :raises click.ClickException: If any setup step fails.
    """
    from omnigent.claude_native_bridge import bridge_dir_for_conversation_id

    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    persist_args = list(_strip_resume_from_claude_args(claude_args))
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        startup_profiler.mark("daemon prepare http client ready")
        # Resuming an existing session must not re-close its terminal on
        # exit; a fresh launch owns teardown.
        reattached = session_id is not None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Claude session requires a session bundle.")
            _mark_startup_step(
                startup_profiler,
                "creating daemon claude session",
                startup_progress=startup_progress,
                progress_message="Creating Claude session...",
            )
            session_id = await _create_claude_session(
                client,
                session_bundle,
                bridge_id=None,
                terminal_launch_args=persist_args or None,
            )
            _mark_startup_step(
                startup_profiler,
                "daemon claude session created",
                startup_progress=startup_progress,
            )
        elif persist_args:
            # Resume with new flags: replace the stored args
            # (last-write-wins). No new flags → leave the stored set so
            # the runner reuses them.
            _mark_startup_step(
                startup_profiler,
                "persisting resume launch args",
                startup_progress=startup_progress,
                progress_message="Updating Claude session...",
            )
            await client.patch(
                f"/v1/sessions/{url_component(session_id)}",
                json={"terminal_launch_args": persist_args},
            )
            _mark_startup_step(
                startup_profiler,
                "resume launch args persisted",
                startup_progress=startup_progress,
            )
        _mark_startup_step(
            startup_profiler,
            "waiting for host online",
            startup_progress=startup_progress,
        )
        await wait_for_host_online(client, host_id, timeout_s=_DAEMON_HOST_ONLINE_TIMEOUT_S)
        _mark_startup_step(
            startup_profiler,
            "host online",
            startup_progress=startup_progress,
        )
        _mark_startup_step(
            startup_profiler,
            "launching or reusing daemon runner",
            startup_progress=startup_progress,
            progress_message="Starting runner...",
        )
        runner_id = await launch_or_reuse_daemon_runner(
            client,
            host_id=host_id,
            session_id=session_id,
            workspace=workspace,
        )
        _mark_startup_step(
            startup_profiler,
            "daemon runner launch requested",
            startup_progress=startup_progress,
            detail=f"runner={runner_id}",
        )
        _mark_startup_step(
            startup_profiler,
            "waiting for runner online",
            startup_progress=startup_progress,
            progress_message="Waiting for runner...",
        )
        await wait_for_runner_online(client, runner_id, timeout_s=_DAEMON_RUNNER_ONLINE_TIMEOUT_S)
        _mark_startup_step(
            startup_profiler,
            "daemon runner online",
            startup_progress=startup_progress,
        )
        if reattached:
            # Resume onto an already-online daemon runner reuses it without
            # re-running the session-start auto-create, so a runner whose
            # terminal was torn down (e.g. after a ``-p`` one-shot) comes
            # back terminal-less and the wait below would time out. Ask the
            # runner to ensure the claude terminal: idempotent (returns the
            # live one if present) and otherwise auto-creates it with cold
            # resume so history is restored. A fresh launch already creates
            # it on session-start, so this is only needed when reattaching.
            _mark_startup_step(
                startup_profiler,
                "ensuring resumed terminal on runner",
                startup_progress=startup_progress,
                progress_message="Restoring Claude terminal...",
            )
            await _ensure_claude_terminal_on_runner(client, session_id)
            _mark_startup_step(
                startup_profiler,
                "resumed terminal ensure requested",
                startup_progress=startup_progress,
            )
        _mark_startup_step(
            startup_profiler,
            "waiting for claude terminal ready",
            startup_progress=startup_progress,
            progress_message="Starting Claude terminal...",
        )
        terminal_id = await _wait_for_claude_terminal_ready(
            client, session_id, timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S
        )
        _mark_startup_step(
            startup_profiler,
            "claude terminal ready",
            startup_progress=startup_progress,
            progress_message="Claude terminal ready.",
        )
        tmux = await _read_claude_terminal_tmux(client, session_id)
        _mark_startup_step(
            startup_profiler,
            "daemon terminal tmux metadata read",
            startup_progress=startup_progress,
        )
    return PreparedClaudeTerminal(
        session_id=session_id,
        terminal_id=terminal_id,
        bridge_dir=bridge_dir_for_conversation_id(session_id),
        reattached=reattached,
        tmux_socket=tmux.socket,
        tmux_target=tmux.target,
    )


def _run_with_remote_server(
    base_url: str,
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    claude_args: tuple[str, ...],
    auto_open_conversation: bool = False,
    startup_profiler: StartupProfiler | None = None,
) -> None:
    """
    Launch Claude on a remote Omnigent server via the connect daemon.

    Ensures the connect daemon is running for *base_url*, then routes
    the runner launch through it (HOST_BY_DEFAULT): the daemon — not
    this CLI — spawns the runner, which brings the Claude terminal up
    itself (applying the persisted launch args, model, cold resume, and
    the ucode gateway auth from the provider config). The CLI
    creates/resolves the session, persists the pass-through args, waits
    for the daemon-spawned runner + its auto-created terminal, and
    attaches (directly to the runner's tmux when it is local, else over
    the WebSocket PTY bridge). See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.

    :param base_url: Remote Omnigent server base URL without a trailing
        slash, e.g. ``"https://example.databricks.com"``.
    :param spec_path: Generated Claude wrapper agent spec.
    :param session_id: Optional existing session id.
    :param resume_picker: When ``True`` and ``session_id is None``, run the picker.
    :param claude_args: Claude CLI args, persisted on the session as
        ``terminal_launch_args`` for the runner to apply. (The runner
        launches ``claude`` itself and derives the ucode config from the
        provider config, so this path takes neither a ``command`` nor a
        ``claude_config``.)
    :param auto_open_conversation: When ``True``, open the browser
        conversation URL after the session is prepared.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :returns: None.
    """
    from omnigent.chat import _bundle_agent, _remote_headers, _server_auth
    from omnigent.cli import _ensure_host_daemon
    from omnigent.host.identity import load_or_create_host_identity

    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    startup_profiler.mark("remote headers resolving")
    headers = _remote_headers(server_url=base_url)
    startup_profiler.mark("remote headers resolved")
    # ``headers`` carries the bearer for the WebSocket attach handshake
    # (refreshed in place by ``_recover``). For HTTP requests we additionally
    # supply an ``httpx.Auth`` that mints a fresh token per request, so the
    # long-lived transcript-forwarder client survives the ~1h Databricks
    # OAuth token TTL.
    startup_profiler.mark("remote auth resolving")
    forwarder_auth = _server_auth(server_url=base_url)
    startup_profiler.mark("remote auth resolved")
    prepared: PreparedClaudeTerminal | None = None
    # Bound before the attach call so the ``finally`` can read it even
    # if setup raises early; only a real tmux detach flips it.
    outcome = _AttachOutcome.EXITED
    attach_completed = False
    should_print_resume_hint = False
    try:
        startup_profiler.mark("resolving remote session")
        resolved_session_id = _resolve_session_id_for_resume(
            base_url=base_url,
            headers=headers,
            session_id=session_id,
            resume_picker=resume_picker,
        )
        startup_profiler.mark(
            "remote session resolved",
            detail="fresh" if resolved_session_id is None else "resume",
        )
        if resolved_session_id is None and resume_picker and session_id is None:
            # Picker cancelled — don't launch a runner or fresh session.
            return
        should_print_resume_hint = resolved_session_id is None
        with runner_startup_progress(initial_message="Preparing Claude...") as progress:
            if resolved_session_id is not None:
                # Align cwd with the resumed session before we sample
                # ``Path.cwd()`` for the runner workspace below.
                _align_working_directory_with_session(
                    resolved_session_id,
                    base_url=base_url,
                    headers=headers,
                )
                _mark_startup_step(
                    startup_profiler,
                    "remote resume workspace aligned",
                    startup_progress=progress,
                )

            # Ensure the connect daemon is up for this server, then route the
            # runner launch through it. The runner the daemon spawns brings
            # up the Claude terminal itself, so the CLI just waits and
            # attaches.
            _mark_startup_step(
                startup_profiler,
                "ensuring host daemon",
                startup_progress=progress,
                progress_message="Connecting to local daemon...",
            )
            _ensure_host_daemon(base_url)
            _mark_startup_step(
                startup_profiler,
                "host daemon ready",
                startup_progress=progress,
            )
            host_id = load_or_create_host_identity().host_id
            _mark_startup_step(
                startup_profiler,
                "host identity loaded",
                startup_progress=progress,
                detail=f"host={host_id}",
            )

            _mark_startup_step(
                startup_profiler,
                "bundling remote agent",
                startup_progress=progress,
            )
            bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
            _mark_startup_step(
                startup_profiler,
                "remote agent bundle ready",
                startup_progress=progress,
            )
            try:
                _mark_startup_step(
                    startup_profiler,
                    "preparing daemon terminal",
                    startup_progress=progress,
                )
                prepared = asyncio.run(
                    _prepare_claude_terminal_via_daemon(
                        base_url=base_url,
                        headers=headers,
                        session_id=resolved_session_id,
                        session_bundle=bundle,
                        claude_args=claude_args,
                        host_id=host_id,
                        workspace=str(Path.cwd().resolve()),
                        startup_profiler=startup_profiler,
                        startup_progress=progress,
                    )
                )
                _mark_startup_step(
                    startup_profiler,
                    "daemon terminal prepared",
                    startup_progress=progress,
                    detail=_tmux_profile_detail(prepared),
                )
            except httpx.ConnectError as exc:
                # The first server contact (session create) could not open a
                # TCP connection — the Omnigent server at this URL isn't reachable.
                # Fail loud with the URL instead of a raw httpx traceback.
                raise click.ClickException(
                    f"Could not reach the omnigent server at {base_url}. "
                    "Confirm the server is running and reachable from here "
                    f"(e.g. `curl {base_url}/health`), and that --server is correct."
                ) from exc
        if resolved_session_id is None:
            _record_launch_for_fresh_session(prepared.session_id)
            startup_profiler.mark("fresh remote launch state recorded")
        click.echo(f"Web UI: {conversation_url(base_url, prepared.session_id)}", err=True)
        startup_profiler.mark("remote web ui url printed")
        open_conversation_link_if_enabled(
            base_url=base_url,
            conversation_id=prepared.session_id,
            enabled=auto_open_conversation,
            warn=lambda message: click.echo(message, err=True),
        )

        async def _recover() -> None:
            """
            Refresh the bearer in place between attach attempts.

            The daemon owns the runner lifecycle now, so — unlike the
            old CLI-spawned path — recovery does not restart a runner. It
            only re-resolves the Databricks bearer and mutates the shared
            *headers* dict in place so a reconnect after a server bounce
            or token expiry handshakes with a fresh token. If the
            daemon-spawned runner died, the server relaunches it on the
            next message (host-bound auto-relaunch).
            """
            new_headers = _remote_headers(server_url=base_url)
            headers.clear()
            headers.update(new_headers)

        startup_profiler.mark("opening remote terminal attach")
        outcome = asyncio.run(
            _attach_with_transcript_forwarder(
                base_url=base_url,
                headers=headers,
                prepared=prepared,
                agent_name=_AGENT_NAME,
                attach_url=_attach_url(base_url, prepared.session_id, prepared.terminal_id),
                attach=attach_local_terminal,
                recover=_recover,
                auth=forwarder_auth,
                run_transcript_forwarder=False,
                startup_profiler=startup_profiler,
            )
        )
        attach_completed = True
    finally:
        # The daemon owns the runner — the CLI no longer adopts or stops
        # it. On detach the session keeps running for the web UI; on a
        # clean exit the server idle-reaps the runner.
        if prepared is not None and outcome is _AttachOutcome.DETACHED:
            active_session_id = read_active_session_id(prepared.bridge_dir) or prepared.session_id
            click.echo(
                f"\nDetached. Agent still running at "
                f"{conversation_url(base_url, active_session_id)}",
                err=True,
            )
            echo_native_resume_hint(
                native_command="claude",
                session_id=active_session_id,
                server=base_url,
            )
        elif prepared is not None and attach_completed and should_print_resume_hint:
            # Reached only when the attach did NOT detach (the ``if``
            # above handled DETACHED), so this is a clean fresh-session
            # exit — print the resume command for next time.
            active_session_id = read_active_session_id(prepared.bridge_dir) or prepared.session_id
            echo_native_resume_hint(
                native_command="claude",
                session_id=active_session_id,
                server=base_url,
            )


async def _prepare_claude_terminal(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    runner_id: str | None,
    session_bundle: bytes | None,
    claude_args: tuple[str, ...],
    command: str,
    claude_config: ClaudeNativeUcodeConfig | None = None,
    startup_profiler: StartupProfiler | None = None,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedClaudeTerminal:
    """
    Create/bind a session and launch its Claude terminal resource.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP auth headers for the Omnigent server.
    :param session_id: Optional existing session id.
    :param runner_id: Runner id to bind to the session.
    :param session_bundle: Gzipped agent bundle for new sessions.
        Required when *session_id* is ``None``.
    :param claude_args: Claude CLI args.
    :param command: Executable to run in the terminal resource.
    :param claude_config: Optional ucode-derived Claude Code config.
    :param startup_profiler: Optional startup profiler for timing
        marks. ``None`` disables output.
    :param startup_progress: Optional user-visible progress renderer,
        e.g. a handle from :func:`runner_startup_progress`.
    :returns: Prepared terminal details.
    :raises click.ClickException: If any server operation fails.
    """
    startup_profiler = startup_profiler or StartupProfiler(name="omnigent claude", enabled=False)
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        startup_profiler.mark("prepare http client ready")
        cold_resume_args: tuple[str, ...] = ()
        # Cold resume = session existed but no live terminal. Even when
        # ``_resolve_cold_resume_args`` returns ``()`` (no captured
        # external_session_id, so Claude starts a fresh transcript),
        # Omnigent already holds the prior conversation history from the
        # earlier run. The forwarder must not re-read whatever the new
        # transcript file contains at startup and republish it as new
        # Omnigent events. Both subcases — injected ``--resume <claude_sid>``
        # and the warn-and-fallback path — share this hazard, so a
        # single ``cold_resumed`` flag covers both.
        cold_resumed = False
        bridge_id: str | None = None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Claude session requires a session bundle.")
            _mark_startup_step(
                startup_profiler,
                "creating claude session",
                startup_progress=startup_progress,
                progress_message="Creating Claude session...",
            )
            bridge_id = secrets.token_urlsafe(24)
            session_id = await _create_claude_session(
                client,
                session_bundle,
                bridge_id=bridge_id,
            )
            _mark_startup_step(
                startup_profiler,
                "claude session created",
                startup_progress=startup_progress,
            )
        else:
            _mark_startup_step(
                startup_profiler,
                "fetching resume session labels",
                startup_progress=startup_progress,
                progress_message="Loading Claude session...",
            )
            labels = await _fetch_claude_session_labels(client, session_id)
            _mark_startup_step(
                startup_profiler,
                "resume session labels fetched",
                startup_progress=startup_progress,
            )
            bridge_id = labels.get(BRIDGE_ID_LABEL_KEY) or session_id
            _mark_startup_step(
                startup_profiler,
                "checking existing terminal",
                startup_progress=startup_progress,
            )
            existing_terminal_id = await _find_running_claude_terminal(client, session_id)
            if existing_terminal_id is not None:
                _mark_startup_step(
                    startup_profiler,
                    "existing terminal found",
                    startup_progress=startup_progress,
                )
                reattach_tmux = await _read_claude_terminal_tmux(client, session_id)
                _mark_startup_step(
                    startup_profiler,
                    "existing terminal tmux metadata read",
                    startup_progress=startup_progress,
                )
                return PreparedClaudeTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal_id,
                    bridge_dir=bridge_dir_for_bridge_id(bridge_id),
                    reattached=True,
                    tmux_socket=reattach_tmux.socket,
                    tmux_target=reattach_tmux.target,
                )
            # Session exists but no live terminal — recover claude's prior transcript via --resume.
            _mark_startup_step(
                startup_profiler,
                "resolving cold resume args",
                startup_progress=startup_progress,
                progress_message="Restoring Claude session...",
            )
            cold_resume_args = await _resolve_cold_resume_args(client, session_id)
            _mark_startup_step(
                startup_profiler,
                "cold resume args resolved",
                startup_progress=startup_progress,
            )
            cold_resumed = True

        if runner_id is not None:
            _mark_startup_step(
                startup_profiler,
                "binding session runner",
                startup_progress=startup_progress,
            )
            await _bind_session_runner(client, session_id, runner_id)
            _mark_startup_step(
                startup_profiler,
                "session runner bound",
                startup_progress=startup_progress,
            )
        bridge_dir = prepare_bridge_dir(
            session_id,
            bridge_id=bridge_id,
            workspace=Path.cwd(),
            launch_model=claude_config.model if claude_config else None,
        )
        _mark_startup_step(
            startup_profiler,
            "bridge dir prepared",
            startup_progress=startup_progress,
        )
        reset_transcript_forward_state(bridge_dir)
        _mark_startup_step(
            startup_profiler,
            "transcript forward state reset",
            startup_progress=startup_progress,
        )
        # Cold-resume args first so user-supplied tail args keep their relative position.
        _mark_startup_step(
            startup_profiler,
            "launching claude terminal",
            startup_progress=startup_progress,
            progress_message="Starting Claude terminal...",
        )
        terminal_id = await _launch_claude_terminal(
            client,
            session_id,
            (*cold_resume_args, *claude_args),
            command=command,
            bridge_dir=bridge_dir,
            claude_config=claude_config,
        )
        _mark_startup_step(
            startup_profiler,
            "claude terminal launched",
            startup_progress=startup_progress,
            progress_message="Claude terminal ready.",
        )
        # Read the runner's tmux coordinates while the client is open so
        # the attach step can prefer a direct local tmux attach.
        launch_tmux = await _read_claude_terminal_tmux(client, session_id)
        _mark_startup_step(
            startup_profiler,
            "terminal tmux metadata read",
            startup_progress=startup_progress,
        )
    return PreparedClaudeTerminal(
        session_id=session_id,
        terminal_id=terminal_id,
        bridge_dir=bridge_dir,
        reattached=False,
        cold_resumed=cold_resumed,
        tmux_socket=launch_tmux.socket,
        tmux_target=launch_tmux.target,
    )


async def _fetch_claude_session_labels(
    client: httpx.AsyncClient,
    session_id: str,
) -> dict[str, str]:
    """
    Fetch labels for an existing Claude-native Omnigent session.

    :param client: HTTP client for the Omnigent server.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: Session labels as a string dictionary. Empty when the
        session has no labels.
    :raises click.ClickException: If the session lookup fails.
    """
    resp = await client.get(f"/v1/sessions/{url_component(session_id)}")
    if resp.status_code == 404:
        raise click.ClickException(
            f"Conversation {session_id!r} not found on the server. "
            "Run `omnigent claude` (no --resume) to start a new session.",
        )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Failed to fetch conversation {session_id!r} "
            f"({resp.status_code}): {error_text(resp)}",
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise click.ClickException(
            f"Conversation fetch returned non-JSON body: {exc}",
        ) from exc
    labels = payload.get("labels") if isinstance(payload, dict) else None
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


async def _resolve_cold_resume_args(
    client: httpx.AsyncClient,
    session_id: str,
) -> tuple[str, ...]:
    """
    Build the ``claude --resume <sid>`` args for a cold-resume launch.

    Looks up the claude session id captured into
    ``conversations.external_session_id`` and injects it so the new
    terminal reattaches to the prior claude transcript. Fails loud if
    the conversation isn't claude-native; warns and returns empty if
    no external session id was ever captured, or if synthesizing the
    local transcript yields no resumable records (an empty transcript
    would make ``claude --resume`` exit instead of start).

    :param client: HTTP client for the Omnigent server.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: ``("--resume", "<claude_sid>")`` or ``()`` when no id is
        mapped or there is no resumable history.
    :raises click.ClickException: Conversation missing or not claude-native.
    """
    resp = await client.get(f"/v1/sessions/{url_component(session_id)}")
    if resp.status_code == 404:
        raise click.ClickException(
            f"Conversation {session_id!r} not found on the server. "
            "Run `omnigent claude` (no --resume) to start a new session.",
        )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Failed to fetch conversation {session_id!r} "
            f"({resp.status_code}): {error_text(resp)}",
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise click.ClickException(
            f"Conversation fetch returned non-JSON body: {exc}",
        ) from exc
    labels = payload.get("labels") if isinstance(payload, dict) else None
    wrapper = labels.get(_WRAPPER_LABEL_KEY) if isinstance(labels, dict) else None
    if wrapper != _WRAPPER_LABEL_VALUE:
        raise click.ClickException(
            f"Conversation {session_id!r} is not a claude-native session "
            f"(wrapper={wrapper!r}). Use `omnigent run --resume "
            f"{session_id}` to resume it through the right runtime.",
        )
    external_session_id = payload.get("external_session_id")
    if not isinstance(external_session_id, str) or not external_session_id:
        # Omnigent conv survives; claude side starts fresh. Warn on
        # both channels: ``click.echo`` for the foreground user,
        # ``_logger.warning`` for log aggregation (Sentry).
        message = (
            f"claude session id was never captured for {session_id!r}; "
            f"resuming with no prior claude context."
        )
        click.echo(f"warning: {message}", err=True)
        _logger.warning(message)
        return ()
    transcript = await _ensure_local_claude_resume_transcript(
        client,
        session_id=session_id,
        external_session_id=external_session_id,
        workspace=Path.cwd().resolve(),
    )
    if transcript is None:
        # No resumable records: ``claude --resume`` against an empty (or
        # absent) transcript exits with "No conversation found" instead of
        # starting. Launch fresh — the Omnigent conv survives.
        message = (
            f"no resumable claude history for {session_id!r}; "
            f"resuming with no prior claude context."
        )
        click.echo(f"warning: {message}", err=True)
        _logger.warning(message)
        return ()
    return ("--resume", external_session_id)


async def _ensure_local_claude_resume_transcript(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    external_session_id: str,
    workspace: Path,
) -> Path | None:
    """
    Refresh Claude Code's local JSONL transcript for cold resume.

    Cross-machine resume has the Omnigent conversation and Claude external
    session id on the server, but not Claude Code's local
    ``~/.claude/projects/<cwd>/<sid>.jsonl`` file. Claude's
    ``--resume <sid>`` consults that local project transcript. The
    wrapper always rewrites it from committed Omnigent items before launch so
    Omnigent remains the source of truth when a previous local Claude JSONL
    has diverged.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param external_session_id: Claude-native session id, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param workspace: Resolved directory Claude will run in — its
        ``~/.claude/projects/<encoded-workspace>/`` is where the
        transcript must land for ``--resume`` to find it. The CLI
        passes ``Path.cwd()``; a runner-side launch passes its
        ``OMNIGENT_RUNNER_WORKSPACE``. Pass an already-resolved
        path (symlinks collapsed) so the project-dir encoding matches
        what Claude computes.
    :returns: Path to the local transcript that was written; ``None`` if
        *external_session_id* is not a safe transcript stem, or if the AP
        history yields no resumable records (an empty transcript would make
        ``claude --resume`` exit instead of start, so the caller must launch
        fresh).
    :raises click.ClickException: If Omnigent history cannot be fetched or
        the transcript cannot be written.
    """
    if not _CLAUDE_SESSION_ID_RE.fullmatch(external_session_id):
        return None
    current = workspace
    target_dir = _claude_project_dir_for_cwd(current)
    target = target_dir / f"{external_session_id}.jsonl"

    items = await _fetch_all_session_items_for_claude_resume(client, session_id)
    records = _claude_transcript_records_from_session_items(
        items,
        session_id=session_id,
        external_session_id=external_session_id,
        cwd=current,
    )
    # Empty transcript → ``claude --resume`` exits fatally ("No conversation
    # found"), killing the terminal-as-agent. Return None so the caller
    # launches fresh instead of resuming nothing.
    if not records:
        return None
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        os.replace(tmp, target)
    except OSError as exc:
        raise click.ClickException(
            f"Failed to write Claude resume transcript {target}: {exc}"
        ) from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return target


async def _fetch_all_session_items_for_claude_resume(
    client: httpx.AsyncClient,
    session_id: str,
) -> list[dict[str, Any]]:
    """
    Fetch committed session items in chronological order.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :returns: Flat API item dicts from
        ``GET /v1/sessions/{id}/items``.
    :raises click.ClickException: If an item page cannot be fetched or
        parsed.
    """
    items: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 1000, "order": "asc"}
        if after is not None:
            params["after"] = after
        resp = await client.get(
            f"/v1/sessions/{url_component(session_id)}/items",
            params=params,
        )
        if resp.status_code >= 400:
            raise click.ClickException(
                f"Failed to fetch history for {session_id!r} "
                f"({resp.status_code}): {error_text(resp)}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise click.ClickException(
                f"History fetch for {session_id!r} returned non-JSON body: {exc}"
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise click.ClickException(
                f"History fetch for {session_id!r} returned an invalid item list."
            )
        for item in data:
            if isinstance(item, dict):
                items.append(item)
        if not payload.get("has_more"):
            return items
        last_id = payload.get("last_id")
        if not isinstance(last_id, str) or not last_id:
            raise click.ClickException(
                f"History fetch for {session_id!r} set has_more without last_id."
            )
        after = last_id


def _claude_transcript_records_from_session_items(
    items: list[dict[str, Any]],
    *,
    session_id: str,
    external_session_id: str,
    cwd: Path,
) -> list[dict[str, Any]]:
    """
    Convert Omnigent session items into Claude Code transcript records.

    :param items: Flat Omnigent item dicts in chronological order, e.g.
        ``{"type": "message", "role": "user", "content": [...]}``.
    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``. Used as part of deterministic synthetic
        UUID generation.
    :param external_session_id: Claude-native session id, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param cwd: Working directory to write into each transcript
        record, e.g. ``Path("/home/me/repo")``.
    :returns: Claude JSONL record dictionaries.
    """
    records: list[dict[str, Any]] = []
    parent_uuid: str | None = None
    tool_parent_by_call_id: dict[str, str] = {}
    for index, item in enumerate(items):
        record_uuid = _synthetic_claude_transcript_uuid(
            session_id=session_id,
            external_session_id=external_session_id,
            item=item,
            index=index,
        )
        record = _claude_transcript_record_from_session_item(
            item,
            session_id=external_session_id,
            record_uuid=record_uuid,
            parent_uuid=tool_parent_by_call_id.get(str(item.get("call_id"))) or parent_uuid,
            cwd=cwd,
        )
        if record is None:
            continue
        records.append(record)
        if item.get("type") == "function_call":
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id:
                tool_parent_by_call_id[call_id] = record_uuid
        parent_uuid = record_uuid
    return records


def _claude_transcript_record_from_session_item(
    item: dict[str, Any],
    *,
    session_id: str,
    record_uuid: str,
    parent_uuid: str | None,
    cwd: Path,
) -> dict[str, Any] | None:
    """
    Convert one Omnigent item into one Claude transcript record.

    :param item: Flat Omnigent item dict, e.g.
        ``{"type": "function_call", "name": "Read", ...}``.
    :param session_id: Claude-native session id for the transcript,
        e.g. ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param record_uuid: Deterministic UUID for this synthetic
        transcript line.
    :param parent_uuid: Previous transcript record UUID, or ``None``
        for the first line.
    :param cwd: Current working directory to record, e.g.
        ``Path("/home/me/repo")``.
    :returns: Claude transcript record, or ``None`` for unsupported or
        empty Omnigent items.
    """
    item_type = item.get("type")
    message: dict[str, Any] | None = None
    record_type: str | None = None
    extra: dict[str, Any] = {}
    if item_type == "message":
        role = item.get("role")
        if role == "user":
            content = _claude_user_content_from_api_blocks(item.get("content"))
            if content is None:
                return None
            record_type = "user"
            message = {"role": "user", "content": content}
        elif role == "assistant":
            content = _claude_assistant_content_from_api_blocks(item.get("content"))
            if content is None:
                return None
            record_type = "assistant"
            message = {"role": "assistant", "content": content}
            model = item.get("model")
            if isinstance(model, str) and model:
                message["model"] = model
        else:
            return None
    elif item_type == "function_call":
        name = item.get("name")
        call_id = item.get("call_id")
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(call_id, str) or not call_id:
            return None
        record_type = "assistant"
        message = {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": _json_object_from_string(item.get("arguments")),
                }
            ],
        }
        model = item.get("model")
        if isinstance(model, str) and model:
            message["model"] = model
    elif item_type == "function_call_output":
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return None
        output = item.get("output")
        if not isinstance(output, str):
            output = "" if output is None else json.dumps(output, separators=(",", ":"))
        record_type = "user"
        message = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": output,
                }
            ],
        }
        extra["toolUseResult"] = output
    else:
        return None
    return {
        "type": record_type,
        "uuid": record_uuid,
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "userType": "external",
        "sessionId": session_id,
        "cwd": str(cwd),
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "message": message,
        **extra,
    }


def _synthetic_claude_transcript_uuid(
    *,
    session_id: str,
    external_session_id: str,
    item: dict[str, Any],
    index: int,
) -> str:
    """
    Build a stable UUID for one synthesized transcript record.

    :param session_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param external_session_id: Claude-native session id, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    :param item: Omnigent item dict. ``id`` is used when present.
    :param index: Zero-based fallback index.
    :returns: UUID string, e.g.
        ``"d4ffea8e-87dc-5c7b-8f86-3dece5760a22"``.
    """
    item_id = item.get("id")
    stable_item_id = item_id if isinstance(item_id, str) and item_id else f"index-{index}"
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"omnigent-claude-resume:{session_id}:{external_session_id}:{stable_item_id}",
        )
    )


def _claude_user_content_from_api_blocks(content: object) -> str | list[dict[str, Any]] | None:
    """
    Convert Omnigent user message blocks into Claude message content.

    :param content: Omnigent ``content`` value, e.g.
        ``[{"type": "input_text", "text": "hello"}]``.
    :returns: A string for simple text prompts, a Claude content block
        list for multi-block prompts, or ``None`` when no text exists.
    """
    blocks = _claude_text_blocks_from_api_content(content, api_type="input_text")
    if not blocks:
        return None
    if len(blocks) == 1:
        return str(blocks[0]["text"])
    return blocks


def _claude_assistant_content_from_api_blocks(content: object) -> list[dict[str, Any]] | None:
    """
    Convert Omnigent assistant message blocks into Claude text blocks.

    :param content: Omnigent ``content`` value, e.g.
        ``[{"type": "output_text", "text": "hello"}]``.
    :returns: Claude ``text`` content blocks, or ``None`` when no
        assistant text exists.
    """
    blocks = _claude_text_blocks_from_api_content(content, api_type="output_text")
    return blocks or None


def _claude_text_blocks_from_api_content(
    content: object,
    *,
    api_type: str,
) -> list[dict[str, Any]]:
    """
    Extract text blocks from an Omnigent content array.

    :param content: Omnigent content array, e.g.
        ``[{"type": "input_text", "text": "hello"}]``.
    :param api_type: Omnigent block type to include, e.g.
        ``"input_text"`` or ``"output_text"``.
    :returns: Claude ``{"type": "text", "text": ...}`` blocks.
    """
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != api_type:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
    return blocks


def _json_object_from_string(value: object) -> dict[str, Any]:
    """
    Parse a JSON object string, returning ``{}`` on non-object input.

    :param value: JSON string from an Omnigent function-call item, e.g.
        ``"{\"file_path\":\"README.md\"}"``.
    :returns: Parsed object suitable for a Claude ``tool_use.input``
        field.
    """
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _preflight_local_tools(command: str) -> None:
    """
    Verify local executables required by the native Claude wrapper.

    :param command: Claude executable to run locally, e.g.
        ``"claude"``.
    :returns: None when the local runner can launch Claude.
    :raises click.ClickException: If ``command`` or ``tmux`` is not
        available on the local ``PATH``.
    """
    if shutil.which(command) is None:
        raise click.ClickException(
            f"Claude Code CLI command {command!r} was not found on local PATH. "
            "--server selects the Omnigent server only; Claude still runs locally."
        )
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native Claude wrapper "
            "launches Claude through the local runner's tmux terminal."
        )


async def _create_claude_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    bridge_id: str | None,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """
    Create a bundled terminal-first Claude session.

    Leaves ``title`` unset so the server's generic seed helper
    populates it from the first forwarded user message — the same
    path every other session type takes. The sidebar renders a
    ``"Claude Code"`` default label off the
    ``omnigent.wrapper = claude-code-native-ui`` label until the
    real title lands, so no server-side placeholder is needed.

    :param client: HTTP client pointed at the Omnigent server.
    :param bundle: Gzipped Claude wrapper agent bundle.
    :param bridge_id: Opaque bridge id to write on the session labels,
        e.g. ``"bridge_abc123"``. ``None`` omits the label so every
        consumer keys the bridge dir by the session id instead — the
        convention the runner's own auto-create path uses, so a
        daemon-routed launch (where the runner brings the terminal up)
        stays consistent. See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    :param terminal_launch_args: Pass-through ``claude`` CLI args to
        persist on the session, e.g.
        ``["--dangerously-skip-permissions"]``. The runner reads these
        and applies them when it auto-launches the terminal. ``None``
        (the CLI-direct path, which passes args via the live terminal
        POST instead) persists nothing.
    :returns: New session id, e.g. ``"conv_abc123"``.
    :raises click.ClickException: If creation fails.
    """
    labels = dict(_SESSION_LABELS)
    if bridge_id is not None:
        labels[BRIDGE_ID_LABEL_KEY] = bridge_id
    metadata: dict[str, Any] = {"labels": labels}
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    # Stamp the wrapped claude's real effortLevel so the pill isn't a guess.
    effort = read_user_effort_level()
    if effort is not None:
        metadata["reasoning_effort"] = effort
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("claude-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Claude session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise click.ClickException("Claude session creation response did not include session_id.")
    return session_id


async def _launch_claude_terminal(
    client: httpx.AsyncClient,
    session_id: str,
    claude_args: tuple[str, ...],
    *,
    command: str,
    bridge_dir: Path,
    claude_config: ClaudeNativeUcodeConfig | None = None,
) -> str:
    """
    Launch the server-backed Claude terminal resource.

    :param client: HTTP client pointed at the Omnigent server. Its
        ``base_url`` and ``headers`` are reused as the
        ``PermissionRequest`` command hook's Omnigent URL and auth. The hook
        subprocess posts back to the same server with the same auth the
        wrapper already negotiated.
    :param session_id: Session/conversation id.
    :param claude_args: Claude CLI args.
    :param command: Executable to run in the terminal resource.
    :param bridge_dir: Bridge directory shared with Claude's MCP
        MCP server and the web-chat harness.
    :param claude_config: Optional ucode-derived Claude Code config.
    :returns: Terminal resource id.
    :raises click.ClickException: If terminal launch fails.
    """
    body = _claude_terminal_request(
        claude_args,
        command=command,
        bridge_dir=bridge_dir,
        ap_server_url=str(client.base_url),
        ap_auth_headers=dict(client.headers),
        claude_config=claude_config,
    )
    resp = await client.post(
        f"/v1/sessions/{url_component(session_id)}/resources/terminals",
        json=body,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Claude terminal launch failed ({resp.status_code}): {error_text(resp)}"
        )
    payload = resp.json()
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException("Claude terminal launch response did not include terminal id.")
    return terminal_id


async def _find_running_claude_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> str | None:
    """
    Return the existing running ``claude/main`` terminal id if present.

    Lookup happens before rebinding an existing session to this
    invocation's local runner. That preserves reattach behavior for a
    live terminal hosted by the currently bound runner; if the session
    has no runner, the runner is offline, or the terminal is absent,
    callers deterministically bind the current local runner and launch
    a fresh terminal.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session/conversation id, e.g.
        ``"conv_abc123"``.
    :returns: The deterministic Claude terminal id, or ``None`` when
        the wrapper should launch a new terminal.
    :raises click.ClickException: If the server rejects the lookup for
        a reason other than "not currently attachable".
    """
    terminal_id = claude_terminal_resource_id()
    resp = await client.get(
        (
            f"/v1/sessions/{url_component(session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}"
        ),
        timeout=30.0,
    )
    if resp.status_code == 200:
        payload = resp.json()
        if payload.get("id") != terminal_id or payload.get("type") != "terminal":
            raise click.ClickException(
                "Claude terminal lookup returned an unexpected resource shape."
            )
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("running") is False:
            return None
        return terminal_id
    if resp.status_code in {404, 409, 502, 503}:
        return None
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Claude terminal lookup failed ({resp.status_code}): {error_text(resp)}"
        )
    return None


@dataclass(frozen=True)
class _ClaudeTerminalTmux:
    """
    Local tmux coordinates for a Claude terminal resource.

    :param socket: tmux server socket path the runner advertised in
        the terminal resource metadata, e.g.
        ``Path("/tmp/omnigent-501/.../tmux.sock")``. ``None`` when
        absent.
    :param target: tmux ``-t`` target, e.g. ``"main"``. ``None`` when
        absent.
    """

    socket: Path | None
    target: str | None


async def _read_claude_terminal_tmux(
    client: httpx.AsyncClient,
    session_id: str,
) -> _ClaudeTerminalTmux:
    """
    Read the tmux socket/target the Claude terminal resource exposes.

    Lets the caller decide whether to attach to the runner's tmux
    directly (same machine, low latency) instead of relaying over the
    WebSocket PTY bridge. Best-effort: any lookup failure, non-200, or
    missing metadata yields ``(None, None)``, which callers treat as
    "not locally attachable" and fall back to the WebSocket path.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: The tmux coordinates, or ``_ClaudeTerminalTmux(None,
        None)`` when unavailable.
    """
    terminal_id = claude_terminal_resource_id()
    try:
        resp = await client.get(
            f"/v1/sessions/{url_component(session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}",
            timeout=30.0,
        )
    except httpx.HTTPError:
        return _ClaudeTerminalTmux(socket=None, target=None)
    if resp.status_code != 200:
        return _ClaudeTerminalTmux(socket=None, target=None)
    metadata = resp.json().get("metadata")
    if not isinstance(metadata, dict):
        return _ClaudeTerminalTmux(socket=None, target=None)
    raw_socket = metadata.get("tmux_socket")
    raw_target = metadata.get("tmux_target")
    socket = Path(raw_socket) if isinstance(raw_socket, str) and raw_socket else None
    target = raw_target if isinstance(raw_target, str) and raw_target else None
    return _ClaudeTerminalTmux(socket=socket, target=target)


def _claude_terminal_request(
    claude_args: tuple[str, ...],
    *,
    command: str,
    bridge_dir: Path,
    ap_server_url: str | None = None,
    ap_auth_headers: dict[str, str] | None = None,
    claude_config: ClaudeNativeUcodeConfig | None = None,
) -> dict[str, Any]:
    """
    Build the terminal resource creation body for Claude Code.

    :param claude_args: Claude CLI args.
    :param command: Executable to run in the terminal resource.
    :param bridge_dir: Bridge directory shared with Claude's MCP
        server and the web-chat harness.
    :param ap_server_url: Omnigent server base URL passed through to
        :func:`augment_claude_args` so Claude's
        ``PermissionRequest`` command hook is registered against the
        live Omnigent server.
    :param ap_auth_headers: Auth headers for the
        ``PermissionRequest`` command hook.
    :param claude_config: Optional ucode-derived Claude Code config.
    :returns: JSON body for ``POST /resources/terminals``.
    """
    claude_args = _merge_default_model_arg(
        claude_args,
        model=claude_config.model if claude_config is not None else None,
    )
    args = augment_claude_args(
        claude_args,
        bridge_dir=bridge_dir,
        ap_server_url=ap_server_url,
        ap_auth_headers=ap_auth_headers,
        api_key_helper=claude_config.api_key_helper if claude_config is not None else None,
    )
    spec: dict[str, Any] = {
        "command": command,
        "args": args,
        "os_env_type": "caller_process",
        # Pin the terminal cwd to the user's launch directory.
        # The wrapper runs locally on the same host as the runner
        # subprocess, so ``Path.cwd()`` here equals the runner's
        # ``RUNNER_WORKSPACE`` env. Without this, the runner falls
        # through to ``SessionResourceRegistry.compute_default_env_root``
        # which (under ``per_session_workspace=True``, set
        # whenever ``runner_workspace`` is non-None) returns
        # ``<workspace>/<conversation_id>`` -- a path the runner
        # never actually creates. tmux is then launched with
        # ``-c <that-missing-dir>`` and silently falls back to
        # ``$HOME``, so ``claude`` starts in the wrong directory.
        # The per-session isolation is meaningful for shared
        # deployments; ``omnigent claude`` is a local-only
        # single-user wrapper, so taking the explicit-cwd path
        # short-circuits it safely.
        "cwd": str(Path.cwd().resolve()),
        "scrollback": _CLAUDE_TERMINAL_SCROLLBACK_LINES,
    }
    spec["env"] = build_native_claude_terminal_env(claude_config)
    if claude_config is not None:
        # The runner's terminal layer inherits the parent process env.
        # Remove provider/session variables that can override the
        # ucode apiKeyHelper or make Claude think it is nested.
        unset_env_vars = [
            _ANTHROPIC_API_KEY_ENV,
            _CLAUDE_CODE_NESTED_SESSION_ENV,
        ]
        env_args = [part for var in unset_env_vars for part in ("-u", var)]
        spec["command"] = "env"
        spec["args"] = [*env_args, command, *args]
    return {
        "terminal": _TERMINAL_NAME,
        "session_key": _TERMINAL_SESSION_KEY,
        "spec": spec,
        # Boolean opt-in; the runner derives the bridge dir from session_id.
        "bridge_inject_dir": True,
    }


def _merge_default_model_arg(
    claude_args: tuple[str, ...],
    *,
    model: str | None,
) -> tuple[str, ...]:
    """
    Add a ucode model default unless the user already selected one.

    :param claude_args: User-provided Claude Code args, e.g.
        ``("--model", "sonnet")``.
    :param model: Ucode model id, e.g.
        ``"databricks-claude-opus-4-7"``.
    :returns: Args with ``--model <model>`` appended when appropriate.
    """
    if not model:
        return claude_args
    for arg in claude_args:
        if arg == "--model" or arg.startswith("--model="):
            return claude_args
    return (*claude_args, "--model", model)


async def attach_local_terminal(
    attach_url: str,
    *,
    headers: dict[str, str],
    stdin_fd: int | None = None,
    stdout_fd: int | None = None,
    terminal_gone_probe: Callable[[], Awaitable[bool]] | None = None,
    terminal_gone_watch_interval_s: float = _CLAUDE_TERMINAL_GONE_WATCH_INTERVAL_S,
) -> bool:
    """
    Attach the local TTY to an Omnigent terminal WebSocket.

    :param attach_url: Fully-qualified ``ws://`` or ``wss://`` attach
        URL.
    :param headers: WebSocket handshake headers, e.g.
        ``{"Authorization": "Bearer ..."}``.
    :param stdin_fd: File descriptor to read local input from.
        ``None`` uses ``sys.stdin``.
    :param stdout_fd: File descriptor to write terminal output to.
        ``None`` uses ``sys.stdout``.
    :param terminal_gone_probe: Optional async callback returning
        ``True`` once the Omnigent terminal resource is stopped. When set,
        the client closes its WebSocket locally instead of waiting for
        the server close frame to propagate.
    :param terminal_gone_watch_interval_s: Poll interval for
        ``terminal_gone_probe`` in seconds, e.g. ``0.25``.
    :returns: ``True`` when the local user requested termination
        (stdin EOF). ``False`` when the WebSocket closed for any
        other reason (server bounce, runner restart, clean close
        initiated by the server). On SIGTERM/SIGHUP, ``SystemExit``
        propagates before this function returns. Callers use the
        boolean to decide whether to reconnect or exit cleanly.
    """
    stdin_fd = sys.stdin.fileno() if stdin_fd is None else stdin_fd
    stdout_fd = sys.stdout.fileno() if stdout_fd is None else stdout_fd

    stdin_eof = asyncio.Event()
    async with _websocket_connect(attach_url, headers=headers) as ws:
        old_attrs = _enter_raw_mode(stdin_fd)
        signal_restore = _install_attach_signal_handlers(ws, stdin_fd)
        try:
            await _send_resize(ws, stdin_fd)
            stop_waiter = signal_restore.stop_event.wait()
            tasks = {
                asyncio.create_task(
                    _stdin_to_websocket(ws, stdin_fd, eof_event=stdin_eof),
                    name="claude-stdin-to-ws",
                ),
                asyncio.create_task(
                    _websocket_to_stdout(ws, stdout_fd), name="claude-ws-to-stdout"
                ),
                asyncio.create_task(stop_waiter, name="claude-attach-signal"),
            }
            if terminal_gone_probe is not None:
                tasks.add(
                    asyncio.create_task(
                        _close_ws_when_terminal_gone(
                            ws,
                            terminal_gone_probe=terminal_gone_probe,
                            poll_interval_s=terminal_gone_watch_interval_s,
                        ),
                        name="claude-terminal-gone-watcher",
                    )
                )
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()
        finally:
            signal_restore.restore()
            _restore_terminal(stdin_fd, old_attrs)
        if signal_restore.received_signal is not None:
            raise SystemExit(128 + signal_restore.received_signal)
    return stdin_eof.is_set()


async def _close_ws_when_terminal_gone(
    ws: Any,
    *,
    terminal_gone_probe: Callable[[], Awaitable[bool]],
    poll_interval_s: float,
) -> None:
    """
    Close the client WebSocket when the Omnigent terminal resource stops.

    This is a client-side fast-exit path for native Claude shutdown:
    the runner can mark the terminal stopped before the attach
    WebSocket close frame reaches the CLI. Closing locally unblocks
    the stdout bridge without waiting for delayed close propagation.

    :param ws: Connected ``websockets`` client.
    :param terminal_gone_probe: Async callback returning ``True``
        when the terminal resource is stopped.
    :param poll_interval_s: Seconds to sleep between probes,
        e.g. ``0.25``.
    :returns: None after closing the WebSocket, or when cancelled.
    """
    while True:
        await asyncio.sleep(poll_interval_s)
        terminal_gone = await terminal_gone_probe()
        if not terminal_gone:
            continue

        try:
            await ws.close(code=1000, reason="terminal resource stopped")
        except (WebSocketException, OSError, ConnectionError):
            _logger.debug(
                "claude-native terminal-gone watcher close failed",
                exc_info=True,
            )
        return


def _websocket_connect(attach_url: str, *, headers: dict[str, str]) -> Any:
    """
    Return a websockets connection context manager.

    The ``websockets`` package renamed the handshake header argument
    across releases. This compatibility wrapper keeps attach working
    across both supported versions.

    :param attach_url: Fully-qualified ``ws://`` or ``wss://`` URL.
    :param headers: Headers to send during the WebSocket handshake.
    :returns: Async context manager yielded by ``websockets.connect``.
    """
    import websockets

    from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN

    # Identify as a first-party client so the server's WebSocket origin
    # guard (CSWSH protection) allows the handshake — this attach client
    # is not a browser. Set on a copy so the caller's dict (which also
    # carries auth headers and may be reused) is not mutated here.
    handshake_headers = {**headers, "Origin": OMNIGENT_INTERNAL_WS_ORIGIN}
    try:
        return websockets.connect(
            attach_url,
            additional_headers=handshake_headers,
            close_timeout=_CLAUDE_ATTACH_WS_CLOSE_TIMEOUT_S,
        )
    except TypeError:
        return websockets.connect(
            attach_url,
            extra_headers=handshake_headers,
            close_timeout=_CLAUDE_ATTACH_WS_CLOSE_TIMEOUT_S,
        )


async def _stdin_to_websocket(
    ws: Any,
    stdin_fd: int,
    *,
    eof_event: asyncio.Event | None = None,
) -> None:
    """
    Copy local stdin bytes to the terminal WebSocket.

    :param ws: Connected ``websockets`` client.
    :param stdin_fd: Local stdin file descriptor.
    :param eof_event: Optional event set when the local stdin
        reaches EOF (i.e. the user closed the TTY input). Lets the
        outer attach loop distinguish a user-initiated exit from a
        server-initiated close.
    :returns: None on EOF or WebSocket close.
    """
    while True:
        data = await _read_fd(stdin_fd)
        if not data:
            if eof_event is not None:
                eof_event.set()
            await ws.close()
            return
        await ws.send(data)


async def _websocket_to_stdout(ws: Any, stdout_fd: int) -> None:
    """
    Copy terminal WebSocket bytes to local stdout.

    ``async for message in ws`` ends silently on any close, so the
    4404 "terminal gone" code never reaches the outer reconnect
    loop on its own. Surface that specific code as
    :class:`ConnectionClosedError`; other codes fall through to the
    outer loop's existing transient-close path (probe + backoff
    retry).

    :param ws: Connected ``websockets`` client.
    :param stdout_fd: Local stdout file descriptor.
    :returns: ``None`` on a transient close; never returns on 4404.
    :raises ConnectionClosedError: When the peer closed with
        :data:`WS_CLOSE_TERMINAL_NOT_FOUND`, so the outer loop's
        :func:`_is_terminal_not_found_close` check fires.
    """
    async for message in ws:
        if isinstance(message, str):
            continue
        await asyncio.to_thread(os.write, stdout_fd, bytes(message))
    close_code = getattr(ws, "close_code", None)
    if close_code == WS_CLOSE_TERMINAL_NOT_FOUND:
        raise ConnectionClosedError(
            Close(close_code, getattr(ws, "close_reason", None) or ""),
            None,
        )


async def _read_fd(fd: int) -> bytes:
    """
    Await one readable event on *fd* and return bytes from it.

    :param fd: File descriptor to read, e.g. ``0`` for stdin.
    :returns: Bytes read from the descriptor; ``b""`` means EOF.
    """
    loop = asyncio.get_running_loop()
    try:
        return await _read_fd_with_reader(loop, fd)
    except (NotImplementedError, RuntimeError):
        return await asyncio.to_thread(os.read, fd, 4096)


async def _read_fd_with_reader(loop: asyncio.AbstractEventLoop, fd: int) -> bytes:
    """
    Read *fd* using the event loop's reader callback API.

    :param loop: Running event loop.
    :param fd: File descriptor to read.
    :returns: Bytes read from *fd*.
    """
    fut: asyncio.Future[bytes] = loop.create_future()

    def _ready() -> None:
        """Complete the pending read future from the selector callback."""
        if fut.done():
            return
        try:
            fut.set_result(os.read(fd, 4096))
        except OSError as exc:
            fut.set_exception(exc)
        finally:
            with contextlib.suppress(Exception):
                loop.remove_reader(fd)

    loop.add_reader(fd, _ready)
    try:
        return await fut
    finally:
        if not fut.done():
            with contextlib.suppress(Exception):
                loop.remove_reader(fd)


async def _send_resize(ws: Any, stdin_fd: int) -> None:
    """
    Send the current local terminal size over the attach protocol.

    :param ws: Connected ``websockets`` client.
    :param stdin_fd: Local stdin file descriptor used for terminal
        size detection.
    :returns: None.
    """
    size = os.get_terminal_size(stdin_fd) if os.isatty(stdin_fd) else os.terminal_size((80, 24))
    await ws.send(json.dumps({"type": "resize", "cols": size.columns, "rows": size.lines}))


def _enter_raw_mode(fd: int) -> list[Any] | None:
    """
    Put *fd* into raw mode when it is a TTY.

    :param fd: File descriptor to update.
    :returns: Previous termios attributes, or ``None`` when *fd* is
        not a TTY.
    """
    if not os.isatty(fd):
        return None
    old_attrs = termios.tcgetattr(fd)
    tty.setraw(fd)
    return old_attrs


def _restore_terminal(fd: int, old_attrs: list[Any] | None) -> None:
    """
    Restore termios attributes saved by :func:`_enter_raw_mode`.

    :param fd: File descriptor to restore.
    :param old_attrs: Attributes returned from
        :func:`_enter_raw_mode`.
    :returns: None.
    """
    if old_attrs is None:
        return
    with contextlib.suppress(termios.error, OSError):
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


@dataclass
class _SignalRestore:
    """
    Restore handle for attach-time signal handlers.

    :param restore: Callable that reinstalls previous handlers.
    :param stop_event: Event set when SIGTERM/SIGHUP arrives.
    :param received_signal: Last stop signal number, if any.
    """

    restore: Callable[[], None]
    stop_event: asyncio.Event
    received_signal: int | None = None


def _install_attach_signal_handlers(ws: Any, stdin_fd: int) -> _SignalRestore:
    """
    Install resize and stop signal handlers for local attach.

    :param ws: Connected ``websockets`` client.
    :param stdin_fd: Local stdin file descriptor.
    :returns: Restore handle for previous signal handlers.
    """
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    previous: dict[signal.Signals, Any] = {}
    resize_tasks: set[asyncio.Task[None]] = set()
    restore_handle = _SignalRestore(lambda: None, stop_event)

    def _request_stop(sig: signal.Signals) -> None:
        """Record *sig* and let the attach loop unwind normally."""
        restore_handle.received_signal = int(sig)
        stop_event.set()

    def _resize() -> None:
        """Forward SIGWINCH as an attach-protocol resize message."""
        task = asyncio.create_task(_send_resize(ws, stdin_fd))
        resize_tasks.add(task)
        task.add_done_callback(resize_tasks.discard)

    for sig, handler in {
        signal.SIGWINCH: _resize,
        signal.SIGTERM: lambda: _request_stop(signal.SIGTERM),
        signal.SIGHUP: lambda: _request_stop(signal.SIGHUP),
    }.items():
        previous[sig] = signal.getsignal(sig)
        try:
            loop.add_signal_handler(sig, handler)
        except (NotImplementedError, RuntimeError):
            continue

    def _restore() -> None:
        """Restore handlers replaced by this attach session."""
        for sig, handler in previous.items():
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)
            with contextlib.suppress(Exception):
                signal.signal(sig, handler)

    restore_handle.restore = _restore
    return restore_handle


def claude_terminal_resource_id() -> str:
    """
    Return the deterministic terminal id used by ``omnigent claude``.

    :returns: Terminal resource id, e.g. ``"terminal_claude_main"``.
    """
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)
