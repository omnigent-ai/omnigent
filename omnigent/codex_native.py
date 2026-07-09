"""Native Codex TUI wrapper for the Omnigent CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import click
import httpx
import yaml

from omnigent._native_resume_hint import echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._wrapper_labels import (
    CODEX_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY
from omnigent.claude_native import (
    _attach_with_reconnect,
    attach_local_terminal,
)
from omnigent.claude_native_bridge import url_component
from omnigent.codex_native_app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
    build_codex_native_server,
    build_codex_remote_args,
    client_for_transport,
    codex_session_meta_model_provider,
    codex_terminal_env,
    preload_codex_thread_for_resume,
    resolve_native_codex_launch,
)
from omnigent.codex_native_bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    CodexNativeBridgeState,
    bridge_dir_for_bridge_id,
    clear_bridge_state,
    codex_home_for_bridge_dir,
    prepare_bridge_dir,
    read_bridge_state,
    socket_path_for_bridge_dir,
    write_bridge_state,
)
from omnigent.codex_native_forwarder import supervise_forwarder
from omnigent.codex_native_state import read_launch_state, write_launch_state
from omnigent.conversation_browser import conversation_url, open_conversation_link_if_enabled
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.host.daemon_launch import (
    error_text,
    launch_or_reuse_daemon_runner,
    wait_for_host_online,
    wait_for_runner_online,
)
from omnigent.native_coding_agents import native_shell_terminal_spec
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

_logger = logging.getLogger(__name__)

_AGENT_NAME = "codex-native-ui"
_DEFAULT_CODEX_COMMAND = "codex"
_TERMINAL_NAME = "codex"
_TERMINAL_SESSION_KEY = "main"
_CODEX_TERMINAL_SCROLLBACK_LINES = 100_000
_CODEX_THREAD_START_TIMEOUT_SECONDS = 15.0
_SESSION_LABELS = {
    "omnigent.ui": "terminal",
    _WRAPPER_LABEL_KEY: _WRAPPER_LABEL_VALUE,
}
_RESUME_ACTION_SWITCH = "switch"
_RESUME_ACTION_CANCEL = "cancel"
_RUNNER_UNAVAILABLE_ERROR_CODE = "runner_unavailable"
_CONFLICT_ERROR_CODE = "conflict"
_RUNNER_OFFLINE_MESSAGE_FRAGMENT = " is offline for conversation "
_UNBOUND_RUNNER_MESSAGE_FRAGMENT = "not bound to a runner"
# Codex thread ids are UUIDv7 (time-ordered), e.g.
# ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``. Restricting the cloned id to
# hex + hyphens keeps it safe to interpolate into a rollout filename and a
# ``codex resume`` argument (no path separators / traversal).
_CODEX_THREAD_ID_RE = re.compile(r"^[0-9a-fA-F-]+$")
_CODEX_AUTH_UNAVAILABLE_BINARY_MISSING = "binary-missing"
_CODEX_AUTH_UNAVAILABLE_NEEDS_AUTH = "needs-auth"


@dataclass(frozen=True)
class _CodexAuthSource:
    """Resolved source for Codex authentication material."""

    auth_path: Path


def _resolve_codex_auth_source() -> _CodexAuthSource:
    """
    Resolve the local Codex auth source used for availability checks.

    This is the seam for future managed credentials: once Omnigent can provide
    centrally managed Codex credentials, this resolver can return that source
    instead. For now it deliberately defaults to Codex's local ``auth.json`` via
    the same ``CODEX_HOME`` source resolver used when launching native Codex, so
    inherited private Omnigent homes map back to the user's real Codex home.

    :returns: Local Codex auth source to inspect synchronously.
    """
    from omnigent.inner.codex_executor import _codex_home_config_source_from_env

    return _CodexAuthSource(auth_path=_codex_home_config_source_from_env() / "auth.json")


def _codex_auth_json_has_available_credential(auth_path: Path) -> bool:
    """Return whether ``auth.json`` parses and carries a usable credential.

    Presence-based by design. A real Codex ``auth.json`` (see the openai/codex
    ``AuthDotJson`` shape) is one of:

    * **API-key** auth — a top-level ``OPENAI_API_KEY`` (or a
      ``personal_access_token`` from ``codex login --with-access-token``).
    * **ChatGPT / OAuth** auth — a ``tokens`` object holding ``access_token`` /
      ``refresh_token`` (and an ``id_token``).

    There is intentionally **no expiry judgement**. Codex stores no top-level
    expiry field; ChatGPT-mode access tokens are short-lived JWTs that Codex
    silently refreshes via the long-lived ``refresh_token`` (recorded in
    ``last_refresh``), so an "expired" ``access_token`` is the normal
    between-refresh state, not a deauth. Whether the ``refresh_token`` itself is
    still valid is server-side and opaque, so this local-only, side-effect-free
    check only asks whether a credential is *configured* — not whether it would
    authenticate. A revoked/truly-expired session can therefore still surface as
    available and fail at run time; catching that needs a network probe, which
    is out of scope here.

    :param auth_path: Path to the Codex ``auth.json``.
    :returns: ``True`` when the file parses and contains a credential field;
        ``False`` when it is missing, malformed, or carries no credential.
    """
    try:
        raw = auth_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False

    for key in ("OPENAI_API_KEY", "personal_access_token"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return True
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        for field in ("access_token", "refresh_token"):
            value = tokens.get(field)
            if isinstance(value, str) and value.strip():
                return True
    return False


def _codex_auth_unavailable_reason() -> str | None:
    """
    Return why local Codex is unavailable, or ``None`` when available.

    Readiness must ask the same question the launch resolver answers, not read a
    credential file the launch ignores. :func:`resolve_native_codex_launch`
    routes a Databricks-gateway / provider-configured setup through a Databricks
    profile or a ``model_provider`` override and mints its bearer at run time
    (``databricks auth token`` / a provider auth command) — it never reads
    ``auth.json``. So on such a host ``auth.json`` is legitimately empty, and
    gating on it is a false negative (the launch works). Only when the launch
    defers to Codex's *own* login is ``auth.json`` the credential that decides
    availability, so that is the only case gated on it. This mirrors the
    fail-open the ``claude-sdk`` / ``openai-agents`` gateway harnesses already
    rely on: their gateway token is a runtime mint the daemon can't observe.

    The check stays synchronous, side-effect free, and local: it resolves the
    launch (local config reads) and, only on the defer-to-login path, inspects
    the local auth source. It never runs ``codex login``, a status command, or a
    network probe; any resolver failure fails safe onto the ``auth.json`` check.

    :returns: ``"binary-missing"`` when the CLI is absent, ``"needs-auth"``
        when the launch would defer to Codex's own login but ``auth.json`` is
        missing, malformed, or carries no credential, and ``None`` when a
        provider will route the launch or a login credential is configured.
        Token *validity* (revoked/expired refresh, an unreachable gateway) is
        not judged locally — it surfaces at the first turn via the executor.
    """
    if shutil.which(_DEFAULT_CODEX_COMMAND) is None:
        return _CODEX_AUTH_UNAVAILABLE_BINARY_MISSING
    # ponytail: resolve_native_codex_launch runs once per codex spelling
    # (codex / codex-native / native-codex → 3×) per hello frame; on a host with
    # NO configured provider it also runs ambient detection (a localhost ollama
    # probe + a `claude auth status` subprocess). It's off the event loop and
    # only bites unconfigured hosts — memoize the launch across the map build in
    # configured_harness_map if that cost ever shows up.
    try:
        launch = resolve_native_codex_launch(model=None)
        routes_through_provider = (
            launch.profile is not None or codex_session_meta_model_provider(launch) != "openai"
        )
    except Exception:  # noqa: BLE001 - readiness must never raise; fail onto auth.json.
        _logger.debug("codex readiness: launch resolve failed; using auth.json", exc_info=True)
        routes_through_provider = False
    if routes_through_provider:
        return None
    source = _resolve_codex_auth_source()
    if not _codex_auth_json_has_available_credential(source.auth_path):
        return _CODEX_AUTH_UNAVAILABLE_NEEDS_AUTH
    return None


def _update_startup_progress(
    startup_progress: RunnerStartupProgress | None,
    message: str,
) -> None:
    """
    Show one concise Codex startup milestone when a renderer is active.

    :param startup_progress: Optional progress renderer from
        :func:`runner_startup_progress`.
    :param message: User-facing status text, e.g.
        ``"Starting Codex terminal..."``.
    :returns: None.
    """
    if startup_progress is not None:
        startup_progress.update(message)


@dataclass(frozen=True)
class _ResumeWorkspaceActionOption:
    """
    One selectable action in the Codex cwd-mismatch prompt.

    :param action: Stable action value returned to the caller, e.g.
        ``"switch"``.
    :param label: User-facing action label, e.g.
        ``"Switch working directory to /home/me/repo"``.
    """

    action: str
    label: str


@dataclass
class LaunchedCodexTerminal:
    """
    Terminal resource returned by the Omnigent runner launch path.

    :param terminal_id: Terminal resource id, e.g.
        ``"terminal_codex_main"``.
    :param tmux_socket: Local tmux socket path when the runner exposed
        one, e.g. ``"/tmp/omnigent-terminal-x/tmux.sock"``.
    :param tmux_target: Tmux target when exposed by the runner,
        e.g. ``"main"``.
    """

    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None


@dataclass
class PreparedCodexTerminal:
    """
    Prepared native Codex terminal attachment details.

    :param session_id: Omnigent session/conversation id.
    :param terminal_id: Terminal resource id to attach.
    :param tmux_socket: Local tmux socket path when the runner exposed
        one and it is reachable from this CLI process.
    :param tmux_target: Tmux target for direct local attaches, e.g.
        ``"main"``.
    :param bridge_dir: Native Codex bridge directory.
    :param thread_id: Codex app-server thread id. ``None`` until the
        first attached TUI creates a fresh thread.
    :param app_server_url: App-server transport the TUI, forwarder, and
        initial-turn connect over, e.g. ``"ws://127.0.0.1:9876"``. ``None``
        for runner-owned terminal attaches where the CLI never connects to
        the app-server directly.
    :param app_server: Running app-server process when this wrapper
        invocation owns it. ``None`` for reattached live terminals.
    :param event_client: App-server client already listening for the
        Codex thread. Fresh sessions keep this listener open after it
        observes the TUI-created ``thread/started`` event.
    :param reattached: ``True`` when an existing terminal was reused.
    """

    session_id: str
    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None
    bridge_dir: Path
    thread_id: str | None
    app_server_url: str | None
    app_server: CodexNativeAppServer | None
    event_client: CodexAppServerClient | None
    reattached: bool


def run_codex_native(
    *,
    server: str | None,
    session_id: str | None,
    codex_args: tuple[str, ...],
    resume_picker: bool = False,
    command: str = _DEFAULT_CODEX_COMMAND,
    model: str | None = None,
    prompt: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Launch Codex TUI in an Omnigent terminal.

    :param server: Resolved Omnigent server URL, e.g.
        ``"http://127.0.0.1:8123"``.
    :param session_id: Optional existing Omnigent conversation id,
        e.g. ``"conv_abc123"``.
    :param codex_args: Raw Codex CLI args to pass before ``resume``.
    :param resume_picker: ``True`` runs the Codex-native picker.
    :param command: Codex executable, e.g. ``"codex"``.
    :param model: Optional model id, e.g. ``"gpt-5.4-mini"``.
    :param prompt: Optional first prompt to send after launch.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL after the session is prepared.
    :returns: None after the terminal attach session ends.
    :raises click.ClickException: If setup fails.
    """
    resolved_command = command.strip()
    if not resolved_command:
        raise click.ClickException("Codex command must not be empty.")
    _preflight_local_tools()
    if server is None:
        raise click.ClickException(
            "Codex requires a resolved Omnigent server URL. The CLI should call "
            "_ensure_backend before run_codex_native."
        )
    with TemporaryDirectory(prefix="omnigent-codex-native-") as tmpdir:
        spec_path = _materialize_codex_agent_spec(Path(tmpdir), model=model)
        _run_with_remote_server(
            server.rstrip("/"),
            spec_path,
            session_id=session_id,
            resume_picker=resume_picker,
            codex_args=codex_args,
            model=model,
            prompt=prompt,
            auto_open_conversation=auto_open_conversation,
        )


def _record_launch_for_fresh_session(session_id: str) -> None:
    """
    Persist the wrapper's current cwd as the Codex session launch state.

    :param session_id: Newly created Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :returns: None.
    """
    try:
        write_launch_state(session_id, str(Path.cwd().resolve()))
    except OSError:
        _logger.warning(
            "failed to record codex-native launch state for %s",
            session_id,
            exc_info=True,
        )


def _align_working_directory_with_session(session_id: str) -> None:
    """
    Resolve cwd mismatch before resuming a Codex-native session.

    Native Codex state is workspace-scoped from the user's point of
    view: the app-server and TUI should reopen from the directory
    where the session was created. If client-side launch state is
    present and points at a different existing directory, ask whether
    to switch there before the runner and app-server sample cwd.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: None. Side-effect-only; may change process cwd.
    :raises click.ClickException: If recorded state exists but no
        viable resume directory exists, or if the user cancels.
    """
    state = read_launch_state(session_id)
    if state is None:
        return
    current = Path.cwd().resolve()
    recorded_path = Path(state.working_directory).resolve()
    if current == recorded_path:
        return
    if not recorded_path.is_dir():
        raise click.ClickException(
            f"Session {session_id} was created in {recorded_path}, but that "
            "directory no longer exists. Recreate or move the project back "
            "before resuming Codex."
        )
    action = _prompt_codex_resume_workspace_action(
        recorded_path=recorded_path,
        current=current,
    )
    if action == _RESUME_ACTION_SWITCH:
        _switch_to_recorded_working_directory(recorded_path)
        return
    raise click.ClickException("Resume cancelled.")


def _prompt_codex_resume_workspace_action(
    *,
    recorded_path: Path,
    current: Path,
) -> str:
    """
    Ask how to handle a Codex resume cwd mismatch.

    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :returns: One of ``"switch"`` or ``"cancel"``.
    """
    options = _codex_resume_workspace_action_options(recorded_path=recorded_path)
    click.echo(f"\nSession was started in: {recorded_path}", err=True)
    click.echo(f"Current working directory: {current}", err=True)
    click.echo("Codex resume is workspace-scoped. Choose an action:", err=True)
    for option in options:
        click.echo(f"  {option.action:<6} - {option.label}", err=True)
    return click.prompt(
        "Resume action",
        type=click.Choice([option.action for option in options]),
        default=options[0].action,
        show_choices=True,
        err=True,
    )


def _codex_resume_workspace_action_options(
    *,
    recorded_path: Path,
) -> list[_ResumeWorkspaceActionOption]:
    """
    Build the valid actions for a cwd-mismatched Codex resume.

    :param recorded_path: Recorded launch cwd, already resolved.
    :returns: Action options in display order.
    """
    return [
        _ResumeWorkspaceActionOption(
            action=_RESUME_ACTION_SWITCH,
            label=f"Switch working directory to {recorded_path}",
        ),
        _ResumeWorkspaceActionOption(
            action=_RESUME_ACTION_CANCEL,
            label="Cancel resume",
        ),
    ]


def _switch_to_recorded_working_directory(recorded_path: Path) -> None:
    """
    Switch process cwd to *recorded_path* for Codex resume.

    :param recorded_path: Existing recorded launch cwd.
    :returns: None.
    """
    os.chdir(recorded_path)
    click.echo(f"Switched to {recorded_path}.", err=True)


def _materialize_codex_agent_spec(
    tmpdir: Path,
    *,
    model: str | None,
) -> Path:
    """
    Write the terminal-first agent spec used by ``omnigent codex``.

    :param tmpdir: Temporary directory for the generated YAML file.
    :param model: Optional model id, e.g. ``"gpt-5.4-mini"``.
    :returns: Path to the generated YAML spec.
    """
    yaml_path = tmpdir / "codex-native-ui.yaml"
    executor: dict[str, str] = {"harness": "codex-native"}
    if model is not None:
        executor["model"] = model
    raw: dict[str, Any] = {
        "name": _AGENT_NAME,
        "prompt": (
            "Codex is running in the session terminal. Web UI messages are "
            "forwarded into the same native Codex app-server thread."
        ),
        "executor": executor,
        # Opt the native session into the child-session spawn writes
        # (sys_session_create / sys_session_send / sys_session_close)
        # so the wrapped codex can author agent configs and launch
        # them as sub-agent sessions. The relay derives its advertised
        # tool set from this spec via ToolManager.
        "spawn": True,
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
        # Declare a default shell terminal so the relay advertises the
        # ``sys_terminal_*`` family to the wrapped codex (the relay's
        # gate is a non-empty ``terminals:`` block on this spec). Its
        # command follows the user's ``$SHELL`` (zsh/fish/bash); caller
        # process / no sandbox matches the ``os_env`` stance above — the
        # native CLI already runs unsandboxed on the user's workspace.
        "terminals": native_shell_terminal_spec(),
    }
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return yaml_path


def _run_with_local_server(
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    codex_args: tuple[str, ...],
    command: str,
    model: str | None,
    prompt: str | None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Start a local Omnigent server, launch Codex, and attach to it.

    :param spec_path: Generated Codex wrapper agent spec.
    :param session_id: Optional existing Omnigent session id.
    :param resume_picker: When ``True``, run the Codex-native picker.
    :param codex_args: Raw Codex CLI args.
    :param command: Codex executable to run.
    :param model: Optional Codex model id.
    :param prompt: Optional first prompt.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL after the session is prepared.
    :returns: None.
    """
    from omnigent.chat import (
        _bundle_agent,
        _find_free_port,
        _start_local_server,
        _stop_local_server,
        _wait_for_server,
    )

    port = _find_free_port()
    server_handle = _start_local_server(spec_path, port, ephemeral=False)
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(port, server_handle)
        resolved_session_id = _resolve_session_id_for_resume(
            base_url=base_url,
            headers={},
            session_id=session_id,
            resume_picker=resume_picker,
        )
        if resolved_session_id is None and resume_picker and session_id is None:
            return
        if resolved_session_id is not None:
            _align_working_directory_with_session(resolved_session_id)

        async def _drive() -> None:
            """
            Prepare Codex and attach in a single event loop.

            :returns: None.
            """
            with runner_startup_progress(initial_message="Preparing Codex...") as progress:
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_codex_terminal(
                    base_url=base_url,
                    headers={},
                    session_id=resolved_session_id,
                    runner_id=server_handle.runner_id,
                    session_bundle=bundle,
                    codex_args=codex_args,
                    command=command,
                    model=model,
                    startup_progress=progress,
                )
            if resolved_session_id is None:
                _record_launch_for_fresh_session(prepared.session_id)
            click.echo(f"Web UI: {conversation_url(base_url, prepared.session_id)}", err=True)
            open_conversation_link_if_enabled(
                base_url=base_url,
                conversation_id=prepared.session_id,
                enabled=auto_open_conversation,
                warn=lambda message: click.echo(message, err=True),
            )
            await _attach_with_forwarder(
                base_url=base_url,
                headers={},
                prepared=prepared,
                prompt=prompt,
            )
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="codex",
                    session_id=prepared.session_id,
                )

        asyncio.run(_drive())
    finally:
        _stop_local_server(server_handle)


def _run_with_remote_server(
    base_url: str,
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    codex_args: tuple[str, ...],
    model: str | None,
    prompt: str | None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Launch Codex on an Omnigent server via a daemon-spawned runner.

    :param base_url: Remote Omnigent server base URL, e.g.
        ``"https://example.databricks.com"``.
    :param spec_path: Generated Codex wrapper agent spec.
    :param session_id: Optional existing Omnigent session id.
    :param resume_picker: When ``True``, run the Codex-native picker.
    :param codex_args: Raw Codex CLI args.
    :param model: Optional Codex model id.
    :param prompt: Optional first prompt.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL after the session is prepared.
    :returns: None.
    """
    from omnigent.chat import _bundle_agent, _remote_headers, _server_auth
    from omnigent.cli import _ensure_host_daemon
    from omnigent.host.identity import load_or_create_host_identity

    headers = _remote_headers(server_url=base_url)
    attach_auth = _server_auth(server_url=base_url)
    try:
        resolved_session_id = _resolve_session_id_for_resume(
            base_url=base_url,
            headers=headers,
            session_id=session_id,
            resume_picker=resume_picker,
        )
        if resolved_session_id is None and resume_picker and session_id is None:
            return
        if resolved_session_id is not None:
            _align_working_directory_with_session(resolved_session_id)

        async def _drive() -> None:
            """
            Prepare Codex and attach in a single event loop.

            :returns: None.
            """
            with runner_startup_progress(initial_message="Preparing Codex...") as progress:
                _update_startup_progress(progress, "Connecting to local daemon...")
                _ensure_host_daemon(base_url)
                host_id = load_or_create_host_identity().host_id
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_codex_terminal_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    session_id=resolved_session_id,
                    session_bundle=bundle,
                    codex_args=codex_args,
                    model=model,
                    host_id=host_id,
                    workspace=str(Path.cwd().resolve()),
                    startup_progress=progress,
                )
            if resolved_session_id is None:
                _record_launch_for_fresh_session(prepared.session_id)
            click.echo(f"Web UI: {conversation_url(base_url, prepared.session_id)}", err=True)
            open_conversation_link_if_enabled(
                base_url=base_url,
                conversation_id=prepared.session_id,
                enabled=auto_open_conversation,
                warn=lambda message: click.echo(message, err=True),
            )

            async def _recover() -> None:
                """
                Refresh auth headers before a terminal reattach attempt.

                :returns: None.
                """
                new_headers = _remote_headers(server_url=base_url)
                headers.clear()
                headers.update(new_headers)

            if prompt:
                await _post_initial_prompt(
                    base_url=base_url,
                    headers=headers,
                    session_id=prepared.session_id,
                    prompt=prompt,
                    auth=attach_auth,
                )
            await _attach_terminal_resource(
                base_url=base_url,
                headers=headers,
                prepared=prepared,
                recover=_recover,
            )
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="codex",
                    session_id=prepared.session_id,
                    server=base_url,
                )

        asyncio.run(_drive())
    except httpx.ConnectError as exc:
        raise click.ClickException(
            f"Could not reach the omnigent server at {base_url}. "
            "Confirm the server is running and reachable from here "
            f"(e.g. `curl {base_url}/health`), and that --server is correct."
        ) from exc


async def _prepare_codex_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    codex_args: tuple[str, ...],
    model: str | None,
    host_id: str,
    workspace: str,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedCodexTerminal:
    """
    Create or resume a Codex-native session through a daemon runner.

    The runner owns the Codex app-server, transcript forwarder, and tmux
    terminal. The CLI only persists launch intent, waits for the terminal
    resource, and attaches to it.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricks.com"``.
    :param headers: HTTP auth headers for Omnigent requests.
    :param session_id: Existing session id to resume, or ``None`` for a
        fresh session.
    :param session_bundle: Gzipped Codex wrapper bundle. Required when
        *session_id* is ``None``.
    :param codex_args: User pass-through Codex args, e.g.
        ``("--config", "approval_policy=on-request")``.
    :param model: Optional model override for this launch, e.g.
        ``"gpt-5.4-mini"``.
    :param host_id: Local host daemon id, e.g. ``"host_abc123"``.
    :param workspace: Absolute workspace path for the runner cwd, e.g.
        ``"/Users/me/repo"``.
    :param startup_progress: Optional user-visible progress renderer,
        e.g. a handle from :func:`runner_startup_progress`.
    :returns: Prepared terminal details for attaching.
    :raises click.ClickException: If setup fails.
    """
    persist_args = list(codex_args)
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        reattached = session_id is not None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Codex session requires a session bundle.")
            _update_startup_progress(startup_progress, "Creating Codex session...")
            session_id = await _create_codex_session(
                client,
                session_bundle,
                bridge_id=None,
                terminal_launch_args=persist_args or None,
            )
        else:
            _update_startup_progress(startup_progress, "Loading Codex session...")
            payload = await _fetch_codex_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a codex-native session."
                )
            existing_terminal = await _find_running_codex_terminal(client, session_id)
            if existing_terminal is not None:
                external_session_id = payload.get("external_session_id")
                thread_id = external_session_id if isinstance(external_session_id, str) else None
                if persist_args or model is not None:
                    click.echo(
                        "Ignoring Codex launch args/model for an already-running "
                        "terminal; restart the session terminal to apply them.",
                        err=True,
                    )
                _update_startup_progress(startup_progress, "Codex terminal ready.")
                return PreparedCodexTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    bridge_dir=bridge_dir_for_bridge_id(session_id),
                    thread_id=thread_id,
                    app_server_url=None,
                    app_server=None,
                    event_client=None,
                    reattached=True,
                )
            patch: dict[str, Any] = {}
            if persist_args:
                patch["terminal_launch_args"] = persist_args
            if model is not None:
                patch["model_override"] = model
            if patch:
                _update_startup_progress(startup_progress, "Updating Codex session...")
                resp = await client.patch(
                    f"/v1/sessions/{url_component(session_id)}",
                    json=patch,
                )
                if resp.status_code >= 400:
                    raise click.ClickException(
                        f"Codex session launch config update failed "
                        f"({resp.status_code}): {error_text(resp)}"
                    )

        await wait_for_host_online(client, host_id, timeout_s=_DAEMON_HOST_ONLINE_TIMEOUT_S)
        _update_startup_progress(startup_progress, "Starting runner...")
        runner_id = await launch_or_reuse_daemon_runner(
            client,
            host_id=host_id,
            session_id=session_id,
            workspace=workspace,
        )
        _update_startup_progress(startup_progress, "Waiting for runner...")
        await wait_for_runner_online(client, runner_id, timeout_s=_DAEMON_RUNNER_ONLINE_TIMEOUT_S)
        # Must run AFTER wait_for_runner_online — unregistered runners
        # 400 on replace_runner_id. The daemon bind paths don't route
        # through replace_runner_id, so without this re-bind a stopped
        # session stays stopped.
        await _bind_session_runner(client, session_id, runner_id)
        _update_startup_progress(startup_progress, "Starting Codex terminal...")
        await _ensure_codex_terminal_on_runner(client, session_id)
        terminal = await _wait_for_codex_terminal_ready(
            client,
            session_id,
            timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S,
        )
        _update_startup_progress(startup_progress, "Codex terminal ready.")
    return PreparedCodexTerminal(
        session_id=session_id,
        terminal_id=terminal.terminal_id,
        tmux_socket=terminal.tmux_socket,
        tmux_target=terminal.tmux_target,
        bridge_dir=bridge_dir_for_bridge_id(session_id),
        thread_id=None,
        app_server_url=None,
        app_server=None,
        event_client=None,
        reattached=reattached,
    )


async def _ensure_codex_terminal_on_runner(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """
    Ask the bound runner to ensure the Codex app-server and terminal exist.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :returns: None.
    :raises click.ClickException: If the runner rejects the ensure request.
    """
    resp = await client.post(
        f"/v1/sessions/{url_component(session_id)}/resources/terminals",
        json={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
        timeout=60.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Codex terminal ensure failed ({resp.status_code}): {error_text(resp)}"
        )


async def _wait_for_codex_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> LaunchedCodexTerminal:
    """
    Wait until the runner exposes the Codex terminal resource.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param timeout_s: Max seconds to wait, e.g. ``60.0``.
    :returns: Terminal details including direct tmux attach metadata when
        available.
    :raises click.ClickException: If no terminal appears in time.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        terminal = await _find_running_codex_terminal(client, session_id)
        if terminal is not None:
            return terminal
        await asyncio.sleep(0.2)
    raise click.ClickException(
        f"The runner did not create the Codex terminal for {session_id!r} within {timeout_s:.0f}s."
    )


async def _post_initial_prompt(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    prompt: str,
    auth: httpx.Auth | None,
) -> None:
    """
    Send the first Codex prompt through Omnigent instead of the app-server.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers for Omnigent requests.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param prompt: User prompt text.
    :param auth: Optional refresh-capable HTTP auth for long-lived
        Databricks-backed sessions.
    :returns: None.
    :raises click.ClickException: If Omnigent rejects the prompt.
    """
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        auth=auth,
        timeout=httpx.Timeout(30.0),
    ) as client:
        resp = await client.post(
            f"/v1/sessions/{url_component(session_id)}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            },
        )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Codex initial prompt failed ({resp.status_code}): {error_text(resp)}"
        )


async def _prepare_codex_terminal(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    runner_id: str | None,
    session_bundle: bytes | None,
    codex_args: tuple[str, ...],
    command: str,
    model: str | None,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedCodexTerminal:
    """
    Create/bind a session, start app-server, and launch Codex TUI.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param session_id: Optional existing session id.
    :param runner_id: Runner id to bind.
    :param session_bundle: Gzipped agent bundle for new sessions.
    :param codex_args: Raw Codex CLI args.
    :param command: Codex executable.
    :param model: Optional model id.
    :param startup_progress: Optional user-visible progress renderer,
        e.g. a handle from :func:`runner_startup_progress`.
    :returns: Prepared terminal details.
    """
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        bridge_id: str
        thread_id: str | None = None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Codex session requires a session bundle.")
            _update_startup_progress(startup_progress, "Creating Codex session...")
            bridge_id = secrets.token_urlsafe(24)
            session_id = await _create_codex_session(client, session_bundle, bridge_id=bridge_id)
        else:
            _update_startup_progress(startup_progress, "Loading Codex session...")
            payload = await _fetch_codex_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a codex-native session."
                )
            bridge_id = str(labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY) or session_id)
            existing_terminal = await _find_running_codex_terminal(client, session_id)
            external_session_id = payload.get("external_session_id")
            thread_id = external_session_id if isinstance(external_session_id, str) else None
            if existing_terminal is not None and thread_id is not None:
                reattach_bridge_dir = bridge_dir_for_bridge_id(bridge_id)
                reattach_unix_socket = socket_path_for_bridge_dir(reattach_bridge_dir)
                # The running terminal's real transport lives in its bridge
                # state (``ws://`` for terminals launched by current code).
                # Reattach starts no app-server/forwarder/initial-turn, so
                # app_server_url is unused here, but populate it accurately
                # from bridge state and fall back to the legacy unix path.
                reattach_state = read_bridge_state(reattach_bridge_dir)
                reattach_transport = (
                    reattach_state.socket_path
                    if reattach_state is not None
                    else str(reattach_unix_socket)
                )
                _update_startup_progress(startup_progress, "Codex terminal ready.")
                return PreparedCodexTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    bridge_dir=reattach_bridge_dir,
                    thread_id=thread_id,
                    app_server_url=reattach_transport,
                    app_server=None,
                    event_client=None,
                    reattached=True,
                )
            if thread_id is None:
                raise click.ClickException(
                    f"Conversation {session_id!r} is missing its Codex thread id."
                )

        bridge_dir = prepare_bridge_dir(bridge_id)
        socket_path = socket_path_for_bridge_dir(bridge_dir)
        codex_home = codex_home_for_bridge_dir(bridge_dir)
        clear_bridge_state(bridge_dir)
        # Route across all offerings: a configured provider (configure
        # harness), the Databricks ucode profile, or Codex's own login —
        # so `omnigent codex` honors the provider selection like the
        # in-process codex harness. Resolved before any rollout synthesis
        # so session_meta can name the provider the launch routes through.
        _codex_launch = resolve_native_codex_launch(model=model)
        if thread_id is not None:
            await _ensure_local_codex_resume_rollout(
                client,
                session_id=session_id,
                external_session_id=thread_id,
                codex_home=codex_home,
                workspace=Path.cwd().resolve(),
                model_provider=codex_session_meta_model_provider(_codex_launch),
                codex_path=command,
            )
        # Listen on a loopback WebSocket, mirroring the host-spawned
        # runner (``runner/app.py`` ``_auto_create_codex_terminal``).
        # Codex CLI ``app-server`` only accepts ``stdio://``, ``ws://``,
        # or ``off`` — it dropped ``unix://`` — so a ``unix://`` listen
        # exits immediately and the terminal (and the web-UI Terminal
        # pill) never appears.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _probe:
            _probe.bind(("127.0.0.1", 0))
            codex_ws_url = f"ws://127.0.0.1:{_probe.getsockname()[1]}"
        app_server = build_codex_native_server(
            socket_path=socket_path,
            codex_home=codex_home,
            cwd=Path.cwd(),
            model=_codex_launch.model,
            profile=_codex_launch.profile,
            codex_path=command,
            extra_config_overrides=_codex_launch.config_overrides,
            bridge_dir=bridge_dir,
            ap_server_url=base_url,
            ap_auth_headers=headers,
        )
        app_server.listen_url = codex_ws_url
        event_client: CodexAppServerClient | None = None
        terminal_id: str | None = None
        launched_terminal: LaunchedCodexTerminal | None = None
        try:
            await app_server.start()
            if thread_id is None:
                event_client = client_for_transport(
                    codex_ws_url,
                    client_name="omnigent-codex-native",
                )
                await event_client.connect()
            else:
                await preload_codex_thread_for_resume(codex_ws_url, thread_id)
                write_bridge_state(
                    bridge_dir,
                    CodexNativeBridgeState(
                        session_id=session_id,
                        socket_path=codex_ws_url,
                        thread_id=thread_id,
                        codex_home=str(codex_home),
                    ),
                )
            if runner_id is not None:
                await _bind_session_runner(client, session_id, runner_id)
            _update_startup_progress(startup_progress, "Starting Codex terminal...")
            launched_terminal = await _launch_codex_terminal(
                client,
                session_id,
                codex_args=codex_args,
                command=command,
                thread_id=thread_id,
                remote_url=codex_ws_url,
                env=codex_terminal_env(app_server),
                # Give the --remote TUI the same provider overrides as
                # the app-server so it resolves the Omnigent provider
                # and skips the OpenAI-login onboarding screen.
                config_overrides=tuple(app_server.config_overrides),
            )
            terminal_id = launched_terminal.terminal_id
            _update_startup_progress(startup_progress, "Codex terminal ready.")
        except Exception:
            if terminal_id is not None:
                await _close_codex_terminal(
                    base_url=base_url,
                    headers=headers,
                    session_id=session_id,
                    terminal_id=terminal_id,
                )
            if event_client is not None:
                await event_client.close()
            await app_server.close()
            raise
    if launched_terminal is None:
        raise click.ClickException("Codex terminal was not launched.")
    return PreparedCodexTerminal(
        session_id=session_id,
        terminal_id=launched_terminal.terminal_id,
        tmux_socket=launched_terminal.tmux_socket,
        tmux_target=launched_terminal.tmux_target,
        bridge_dir=bridge_dir,
        thread_id=thread_id,
        app_server_url=codex_ws_url,
        app_server=app_server,
        event_client=event_client,
        reattached=False,
    )


async def _attach_with_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    prepared: PreparedCodexTerminal,
    prompt: str | None,
    recover: Any | None = None,
    auth: httpx.Auth | None = None,
) -> None:
    """
    Attach to the Codex terminal while forwarding app-server events.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param prepared: Prepared terminal details.
    :param prompt: Optional first prompt to send.
    :param recover: Optional reconnect recovery callback.
    :param auth: Optional long-lived HTTP auth for remote sessions.
    :returns: None.
    """
    forwarder: asyncio.Task[None] | None = None
    try:
        if prepared.thread_id is None:
            attach_task = asyncio.create_task(
                _attach_terminal_resource(
                    base_url=base_url,
                    headers=headers,
                    prepared=prepared,
                    recover=recover,
                ),
                name="codex-native-terminal-attach",
            )
            await asyncio.sleep(0)
            try:
                prepared.thread_id = await _initialize_fresh_terminal_thread(
                    base_url=base_url,
                    headers=headers,
                    prepared=prepared,
                )
                if prepared.app_server is not None:
                    forwarder = _start_codex_forwarder(
                        base_url=base_url,
                        headers=headers,
                        prepared=prepared,
                        auth=auth,
                    )
                    if prompt:
                        await _start_initial_turn(
                            prepared.app_server_url,
                            prepared.thread_id,
                            prompt,
                        )
                await attach_task
            except Exception:
                if not attach_task.done():
                    attach_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await attach_task
                raise
        else:
            if prepared.app_server is not None:
                forwarder = _start_codex_forwarder(
                    base_url=base_url,
                    headers=headers,
                    prepared=prepared,
                    auth=auth,
                )
                if prompt:
                    await _start_initial_turn(prepared.app_server_url, prepared.thread_id, prompt)
            await _attach_terminal_resource(
                base_url=base_url,
                headers=headers,
                prepared=prepared,
                recover=recover,
            )
    finally:
        if forwarder is not None:
            forwarder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await forwarder
        if not prepared.reattached:
            active_session_id = (
                _active_codex_session_id(prepared.bridge_dir) or prepared.session_id
            )
            await _close_codex_terminal(
                base_url=base_url,
                headers=headers,
                session_id=active_session_id,
                terminal_id=prepared.terminal_id,
            )
        if prepared.app_server is not None:
            await prepared.app_server.close()


def _start_codex_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    prepared: PreparedCodexTerminal,
    auth: httpx.Auth | None,
) -> asyncio.Task[None]:
    """
    Start the transcript forwarder for a prepared Codex terminal.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param prepared: Prepared terminal details with a known thread id.
    :param auth: Optional long-lived HTTP auth for remote sessions.
    :returns: Running forwarder task.
    :raises click.ClickException: If the Codex thread id is not known.
    """
    if prepared.thread_id is None:
        raise click.ClickException("Codex thread id was not initialized.")
    return asyncio.create_task(
        supervise_forwarder(
            base_url=base_url,
            headers=headers,
            session_id=prepared.session_id,
            bridge_dir=prepared.bridge_dir,
            app_server_url=prepared.app_server_url,
            thread_id=prepared.thread_id,
            client=prepared.event_client,
            auth=auth,
        ),
        name="codex-native-forwarder",
    )


async def _initialize_fresh_terminal_thread(
    *,
    base_url: str,
    headers: dict[str, str],
    prepared: PreparedCodexTerminal,
) -> str:
    """
    Wait for an attached fresh Codex TUI to create its app-server thread.

    Codex terminals can be launched in a tmux pane that waits for the
    first client attach before starting Codex. This preserves web
    terminal sharing while letting Codex query the real attached
    terminal during startup.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param prepared: Prepared terminal details whose ``thread_id`` is
        still ``None``.
    :returns: The Codex app-server thread id, e.g. ``"thread_abc123"``.
    :raises click.ClickException: If no thread-start listener exists.
    """
    if prepared.event_client is None:
        raise click.ClickException("Codex event listener was not initialized.")
    thread_id = await _wait_for_thread_started(prepared.event_client)
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(30.0),
    ) as client:
        await _patch_external_session_id(client, prepared.session_id, thread_id)
    write_bridge_state(
        prepared.bridge_dir,
        CodexNativeBridgeState(
            session_id=prepared.session_id,
            socket_path=prepared.app_server_url,
            thread_id=thread_id,
            codex_home=str(codex_home_for_bridge_dir(prepared.bridge_dir)),
        ),
    )
    return thread_id


async def _attach_terminal_resource(
    *,
    base_url: str,
    headers: dict[str, str],
    prepared: PreparedCodexTerminal,
    recover: Any | None,
) -> None:
    """
    Attach the current terminal to the prepared Omnigent terminal resource.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param prepared: Prepared terminal details.
    :param recover: Optional reconnect recovery callback.
    :returns: None after the attach exits.
    """
    direct_tmux_error = _direct_tmux_unavailable_reason(prepared)
    if direct_tmux_error is None:
        if prepared.tmux_socket is None or prepared.tmux_target is None:
            raise click.ClickException("Codex tmux attach metadata was incomplete.")
        await _attach_direct_tmux(prepared.tmux_socket, prepared.tmux_target)
        return
    if prepared.app_server_url is None:
        raise click.ClickException(
            f"Runner-owned Codex terminal requires direct tmux attach, but {direct_tmux_error}"
        )
    await _attach_with_reconnect(
        attach=attach_local_terminal,
        attach_url=_attach_url(base_url, prepared.session_id, prepared.terminal_id),
        headers=headers,
        recover=recover,
        base_url=base_url,
        session_id=prepared.session_id,
        terminal_id=prepared.terminal_id,
        active_session_id_reader=lambda: _active_codex_session_id(prepared.bridge_dir),
    )


def _active_codex_session_id(bridge_dir: Path) -> str | None:
    """
    Return the active Omnigent session id for a native Codex bridge.

    :param bridge_dir: Native Codex bridge directory.
    :returns: Omnigent session id, e.g. ``"conv_abc123"``, or ``None`` when
        bridge state has not been written yet.
    """
    state = read_bridge_state(bridge_dir)
    return state.session_id if state is not None else None


def _can_attach_direct_tmux(prepared: PreparedCodexTerminal) -> bool:
    """
    Return whether this process can attach to the runner tmux directly.

    :param prepared: Prepared terminal details.
    :returns: ``True`` when the runner exposed a local tmux socket, the
        socket exists on this host, and ``tmux`` is available on PATH.
    """
    return _direct_tmux_unavailable_reason(prepared) is None


def _direct_tmux_unavailable_reason(prepared: PreparedCodexTerminal) -> str | None:
    """
    Explain why this process cannot attach to the runner tmux directly.

    :param prepared: Prepared terminal details.
    :returns: ``None`` when direct tmux attach is available, otherwise a
        human-readable reason for the missing prerequisite.
    """
    if prepared.tmux_socket is None:
        return "the terminal resource did not include a tmux socket path."
    if prepared.tmux_target is None:
        return "the terminal resource did not include a tmux target."
    if not prepared.tmux_socket.exists():
        return f"tmux socket {prepared.tmux_socket} is not reachable from this CLI process."
    if shutil.which("tmux") is None:
        return "tmux is not available on PATH."
    return None


async def _attach_direct_tmux(socket_path: Path, tmux_target: str) -> None:
    """
    Attach the current terminal directly to the runner-owned tmux pane.

    This avoids the local WebSocket + PTY relay used for browser and
    non-local runner attaches. ``TMUX`` is removed from the child
    environment so users who run ``omnigent codex`` inside their own
    tmux session can still attach to Omnigent' private tmux server.

    :param socket_path: Runner tmux socket path.
    :param tmux_target: Tmux target to attach, e.g. ``"main"``.
    :returns: None after the attach process exits.
    """
    env = dict(os.environ)
    env.pop("TMUX", None)
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
    await process.wait()


async def _create_codex_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    bridge_id: str | None,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """
    Create a bundled terminal-first Codex session.

    :param client: HTTP client pointed at AP.
    :param bundle: Gzipped agent bundle.
    :param bridge_id: Opaque bridge id, e.g. ``"bridge_abc123"``.
        ``None`` omits the label so the runner-owned bridge keys by
        session id.
    :param terminal_launch_args: Pass-through Codex CLI args to persist
        for runner-owned terminal launch, e.g.
        ``["--config", "approval_policy=on-request"]``.
    :returns: New Omnigent session id.
    """
    labels = dict(_SESSION_LABELS)
    if bridge_id is not None:
        labels[CODEX_NATIVE_BRIDGE_ID_LABEL_KEY] = bridge_id
    metadata = {
        "labels": labels,
    }
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("codex-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Codex session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    new_session_id = body.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise click.ClickException("Codex session creation response did not include session_id.")
    return new_session_id


async def _fetch_codex_session(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
    """
    Fetch an existing Omnigent session.

    :param client: HTTP client pointed at AP.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :returns: Decoded session payload.
    """
    resp = await client.get(f"/v1/sessions/{url_component(session_id)}")
    if resp.status_code == 404:
        raise click.ClickException(f"Conversation {session_id!r} not found on the server.")
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Failed to fetch conversation {session_id!r} ({resp.status_code}): {error_text(resp)}"
        )
    payload = resp.json()
    if not isinstance(payload, dict):
        raise click.ClickException("Conversation fetch returned non-object JSON.")
    return payload


def _mint_codex_thread_id() -> str:
    """
    Mint a fresh UUIDv7 thread id for a forked Codex clone.

    Codex thread ids are UUIDv7 (time-ordered), e.g.
    ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``. A fork writes the cloned
    rollout under a freshly minted id (rather than reusing the source's)
    so the clone gets its own Omnigent ``external_session_id`` — mirroring how
    claude-native assigns the clone a new transcript uuid. The stdlib has
    no UUIDv7 generator before Python 3.14, so we assemble one per
    RFC 9562 §5.7 (48-bit millisecond timestamp + version + variant +
    random) rather than add a dependency.

    :returns: A UUIDv7 string, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    """
    unix_ms = int(time.time() * 1000)
    value = bytearray(unix_ms.to_bytes(6, "big") + secrets.token_bytes(10))
    value[6] = (value[6] & 0x0F) | 0x70  # version 7 in the high nibble
    value[8] = (value[8] & 0x3F) | 0x80  # RFC 4122 variant (0b10)
    return str(uuid.UUID(bytes=bytes(value)))


def _find_codex_rollout(codex_home: Path, thread_id: str) -> Path | None:
    """
    Find a Codex rollout file by thread id within a ``CODEX_HOME``.

    Codex persists each thread's history as a single append-only JSONL
    rollout at
    ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ISO-ts>-<thread_id>.jsonl``,
    where the trailing ``<thread_id>`` matches the thread's
    ``session_meta.id``. We locate it by that filename suffix.

    :param codex_home: A per-session private ``CODEX_HOME``, e.g.
        ``Path("~/.omnigent/codex-native/<hash>/codex-home")``.
    :param thread_id: Codex thread id / rollout stem, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :returns: Path to the most recent matching rollout, or ``None`` when
        none exists on this host.
    """
    if not _CODEX_THREAD_ID_RE.fullmatch(thread_id):
        return None
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return None
    matches = [p for p in sessions.glob(f"**/rollout-*-{thread_id}.jsonl") if p.is_file()]
    if not matches:
        return None
    matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0]


def _copy_rollout_with_cwd(
    *, source: Path, target: Path, clone_workspace: Path, new_thread_id: str
) -> None:
    """
    Copy a Codex rollout JSONL, rewriting only the structural id/cwd.

    A rollout interleaves *structural* fields (the live thread settings
    Codex reads on resume) with *historical* content (recorded shell
    commands, file paths, messages — facts about what already happened).
    Only two structural fields carry the working directory —
    ``session_meta.payload.cwd`` and each ``turn_context.payload.cwd`` —
    plus the thread id at ``session_meta.payload.id``. Those are rewritten
    to the clone's id / workspace; every other line (and every other
    ``cwd`` mention, which lives inside message/tool bodies) is copied
    verbatim, so the clone's history stays truthful about the source run.

    :param source: Existing source rollout JSONL.
    :param target: Temporary output path (atomically renamed by the
        caller).
    :param clone_workspace: The resolved directory the clone runs in,
        written into the structural ``cwd`` fields, e.g.
        ``Path("/home/me/repo-worktrees/fork")``.
    :param new_thread_id: The clone's thread id, written into
        ``session_meta.payload.id``, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :returns: None.
    :raises click.ClickException: If a rollout line is not valid JSON.
    """
    workspace_text = str(clone_workspace)
    with source.open("r", encoding="utf-8") as src, target.open("w", encoding="utf-8") as dst:
        for line_number, line in enumerate(src, start=1):
            if not line.strip():
                dst.write(line)
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise click.ClickException(
                    f"Cannot clone malformed Codex rollout {source}: "
                    f"line {line_number} is not valid JSON."
                ) from exc
            record_type = record.get("type") if isinstance(record, dict) else None
            if record_type not in ("session_meta", "turn_context"):
                # Historical record — write the original bytes back unchanged
                # so message/tool bodies (and their incidental cwd mentions)
                # are preserved exactly, including whitespace and key order.
                dst.write(line)
                continue
            payload = record.get("payload")
            if isinstance(payload, dict):
                if record_type == "session_meta":
                    if isinstance(payload.get("id"), str):
                        payload["id"] = new_thread_id
                    if isinstance(payload.get("cwd"), str):
                        payload["cwd"] = workspace_text
                elif isinstance(payload.get("cwd"), str):  # turn_context
                    payload["cwd"] = workspace_text
            dst.write(json.dumps(record, separators=(",", ":")) + "\n")


def _clone_codex_rollout(
    *,
    source_session_id: str,
    source_thread_id: str,
    target_thread_id: str,
    clone_codex_home: Path,
    clone_workspace: Path,
) -> Path | None:
    """
    Clone a source Codex rollout into the clone's own ``CODEX_HOME``.

    Used to carry a forked codex-native session's history into the clone.
    Codex's resume reads the rollout from the app-server's ``CODEX_HOME``,
    which is per-session-private (keyed by the conversation id), so the
    source rollout must be copied into the *clone's* ``CODEX_HOME`` under a
    thread id we assign. We rewrite ``session_meta.payload.id`` →
    *target_thread_id* and the two structural ``cwd`` fields →
    *clone_workspace* (see :func:`_copy_rollout_with_cwd`), preserving the
    record order and all historical content. The clone then launches
    ``codex resume <target_thread_id>``. Writing the file ourselves before
    launch (rather than pointing resume at the source's home) is what makes
    the worktree case work and keeps the clone's history isolated from the
    source. This is the codex-native mirror of
    :func:`omnigent.claude_native._clone_claude_transcript`. See
    designs/FORK_SESSION_UX.md.

    :param source_session_id: The SOURCE conversation id, used to locate
        the source's ``CODEX_HOME``, e.g. ``"conv_abc123"``.
    :param source_thread_id: The SOURCE Codex thread id / rollout stem to
        copy from, e.g. ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :param target_thread_id: The thread id to assign the clone's copied
        rollout, e.g. ``"019eaa11-...."``. Must be a safe rollout stem; the
        clone's ``external_session_id`` is set to this so a later relaunch
        resumes it via the normal path.
    :param clone_codex_home: The clone's per-session private ``CODEX_HOME``,
        e.g. ``Path("~/.omnigent/codex-native/<hash>/codex-home")``.
    :param clone_workspace: The resolved directory the clone will run in
        (its worktree or same dir). Written into the structural ``cwd``
        fields. Pass an already-resolved path.
    :returns: Path to the written clone rollout, or ``None`` when the ids
        are unsafe or the source rollout can't be found on this host
        (caller launches fresh in that case).
    :raises click.ClickException: If the source rollout is malformed.
    """
    if not _CODEX_THREAD_ID_RE.fullmatch(source_thread_id):
        return None
    if not _CODEX_THREAD_ID_RE.fullmatch(target_thread_id):
        return None
    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(source_session_id))
    source = _find_codex_rollout(source_home, source_thread_id)
    if source is None:
        return None
    # Preserve the source's ``sessions/<YYYY>/<MM>/<DD>/`` layout; only swap
    # the thread id embedded in the rollout filename so the clone lands in
    # its own CODEX_HOME under the assigned id.
    rel_dir = source.parent.relative_to(source_home)
    target_dir = clone_codex_home / rel_dir
    target = target_dir / source.name.replace(source_thread_id, target_thread_id)
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    try:
        _copy_rollout_with_cwd(
            source=source,
            target=tmp,
            clone_workspace=clone_workspace,
            new_thread_id=target_thread_id,
        )
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return target


async def _ensure_local_codex_resume_rollout(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    external_session_id: str,
    codex_home: Path,
    workspace: Path,
    model_provider: str,
    codex_path: str | None,
) -> Path:
    """
    Ensure Codex has a local rollout JSONL for cold resume.

    Cross-machine resume has the Omnigent conversation and Codex thread id on
    the server, but not necessarily the app-server's local
    ``$CODEX_HOME/sessions/.../rollout-*-<thread>.jsonl`` file. Codex
    ``resume <thread>`` reads that local rollout, so before launching a
    known-thread terminal we synthesize the rollout from committed AP
    items when the local rollout is missing. Existing local rollout files
    are left untouched because Codex treats them as append-only runtime
    state, not a cache that Omnigent should rewrite.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param external_session_id: Codex thread id, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :param codex_home: Per-session private ``CODEX_HOME`` whose
        ``sessions`` directory Codex app-server reads.
    :param workspace: Resolved directory Codex will run in, e.g.
        ``Path("/home/me/repo")``. Pass an already-resolved path so
        structural rollout cwd fields match the terminal cwd.
    :param model_provider: Provider id this session's launch routes through,
        e.g. ``"omnigent_databricks"`` (see
        :func:`omnigent.codex_native_app_server.codex_session_meta_model_provider`).
        Written into ``session_meta`` so codex's thread-store backfill can
        resolve the provider when it indexes the rollout.
    :param codex_path: Codex CLI executable used to stamp the real
        ``cli_version`` into ``session_meta``, e.g. ``"/usr/local/bin/codex"``.
        ``None`` (or an unparseable version probe) falls back to ``"0.0.0"`` —
        codex >= 0.133 requires the field to be *present* to parse the
        rollout, but treats the value as informational, so a flaky probe
        must not cost the carried history.
    :returns: Path to the existing or written rollout.
    :raises click.ClickException: If Omnigent history cannot be fetched or the
        rollout cannot be written, or if the persisted Codex thread id is
        unsafe for use in a rollout filename.
    """
    if not _CODEX_THREAD_ID_RE.fullmatch(external_session_id):
        raise click.ClickException(
            f"Cannot resume Codex session {session_id!r}: persisted thread id "
            f"{external_session_id!r} is not a safe Codex rollout id."
        )
    existing = _find_codex_rollout(codex_home, external_session_id)
    if existing is not None:
        return existing
    target = _codex_resume_rollout_path(codex_home, external_session_id)
    items = await _fetch_all_session_items_for_codex_resume(client, session_id)
    cli_version = None
    if codex_path is not None:
        from omnigent.inner.codex_executor import _codex_cli_version

        version_tuple = await _codex_cli_version(codex_path)
        if version_tuple is not None:
            cli_version = ".".join(str(part) for part in version_tuple)
    records = _codex_rollout_records_from_session_items(
        items,
        session_id=session_id,
        external_session_id=external_session_id,
        cwd=workspace,
        model_provider=model_provider,
        cli_version=cli_version or "0.0.0",
    )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        os.replace(tmp, target)
    except OSError as exc:
        raise click.ClickException(
            f"Failed to write Codex resume rollout {target}: {exc}"
        ) from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return target


def _codex_resume_rollout_path(codex_home: Path, external_session_id: str) -> Path:
    """
    Return the rollout path to write for a Codex cold resume.

    Reuses the most recent existing rollout for the thread when present,
    otherwise creates a date-partitioned path matching Codex's on-disk
    layout.

    :param codex_home: Per-session private ``CODEX_HOME``, e.g.
        ``Path("~/.omnigent/codex-native/x/codex-home")``.
    :param external_session_id: Codex thread id / rollout stem, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :returns: Rollout JSONL path to overwrite or create.
    """
    existing = _find_codex_rollout(codex_home, external_session_id)
    if existing is not None:
        return existing
    now = datetime.now(timezone.utc)
    partition = (
        codex_home / "sessions" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    )
    stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
    return partition / f"rollout-{stamp}-{external_session_id}.jsonl"


async def _fetch_all_session_items_for_codex_resume(
    client: httpx.AsyncClient,
    session_id: str,
) -> list[dict[str, Any]]:
    """
    Fetch committed Omnigent session items in chronological order.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
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


def _codex_rollout_records_from_session_items(
    items: list[dict[str, Any]],
    *,
    session_id: str,
    external_session_id: str,
    cwd: Path,
    model_provider: str,
    cli_version: str,
) -> list[dict[str, Any]]:
    """
    Convert Omnigent session items into Codex rollout JSONL records.

    The generated records follow Codex's rollout shape: one
    ``session_meta`` record, a ``turn_context`` before each Omnigent response
    group, Responses-style ``response_item`` payloads for user, assistant,
    and tool history, and an ``event_msg`` mirror after each user/assistant
    message. All three session_meta extras and the event_msg mirrors are
    load-bearing on codex >= 0.133 (verified against 0.136.0): a
    ``session_meta`` without ``timestamp`` + ``cli_version`` fails rollout
    parse ("does not start with session metadata"), an absent
    ``model_provider`` breaks ``thread/resume`` config load once the
    thread-store backfill indexes the rollout, and without ``event_msg``
    records codex reconstructs zero visible turns — the resume "succeeds"
    but the thread opens empty.

    :param items: Flat Omnigent item dicts in chronological order, e.g.
        ``{"type": "message", "role": "user", "content": [...]}``.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
        Used for deterministic synthetic turn ids.
    :param external_session_id: Codex thread id, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :param cwd: Working directory to write into structural rollout fields,
        e.g. ``Path("/home/me/repo")``.
    :param model_provider: Provider id for ``session_meta.model_provider``,
        e.g. ``"omnigent_databricks"``.
    :param cli_version: Codex CLI version string for
        ``session_meta.cli_version``, e.g. ``"0.136.0"``.
    :returns: Codex rollout record dictionaries.
    """
    timestamp = _codex_rollout_timestamp()
    records: list[dict[str, Any]] = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {
                "id": external_session_id,
                "timestamp": timestamp,
                "cwd": str(cwd),
                "originator": "omnigent",
                "cli_version": cli_version,
                "model_provider": model_provider,
            },
        }
    ]
    seen_turn_ids: set[str] = set()
    interrupted_response_ids = _interrupted_response_ids_from_session_items(items)
    for index, item in enumerate(items):
        if _session_item_response_id(item) in interrupted_response_ids:
            continue
        # Compaction items carry the post-compaction context. Emit a
        # Compacted rollout record and discard all prior records — the
        # replacement_history replaces them.
        if item.get("type") == "compaction":
            compacted_msgs = item.get("compacted_messages")
            if compacted_msgs:
                compacted_record: dict[str, Any] = {
                    "timestamp": timestamp,
                    "type": "compacted",
                    "payload": {
                        "message": item.get("summary", ""),
                        "replacement_history": compacted_msgs,
                    },
                }
                w_id = item.get("window_id")
                if w_id is not None:
                    compacted_record["payload"]["window_id"] = w_id
                # Replace all prior response_item records — the
                # replacement_history is the new context baseline.
                # Keep only session_meta and turn_context records.
                records = [r for r in records if r.get("type") in ("session_meta",)]
                records.append(compacted_record)
                seen_turn_ids.clear()
            continue
        payload = _codex_response_item_from_session_item(item)
        if payload is None:
            continue
        turn_id = _codex_turn_id_for_session_item(
            session_id=session_id,
            external_session_id=external_session_id,
            item=item,
            index=index,
        )
        if turn_id not in seen_turn_ids:
            records.append(
                {
                    "timestamp": timestamp,
                    "type": "turn_context",
                    "payload": {
                        "turn_id": turn_id,
                        "cwd": str(cwd),
                        "approval_policy": "on-request",
                    },
                }
            )
            seen_turn_ids.add(turn_id)
        records.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": payload,
            }
        )
        event_msg = _codex_event_msg_record_for_message(payload, timestamp=timestamp)
        if event_msg is not None:
            records.append(event_msg)
    return records


def _codex_event_msg_record_for_message(
    payload: dict[str, Any],
    *,
    timestamp: str,
) -> dict[str, Any] | None:
    """
    Build the ``event_msg`` mirror record for a message ``response_item``.

    Codex reconstructs a resumed thread's *visible* turns from ``event_msg``
    records (``user_message`` / ``agent_message``), not from the
    ``response_item`` history that feeds the model context. A synthesized
    rollout without these mirrors resumes "successfully" but renders an
    empty thread (zero turns) on codex 0.136.0, so the carried history is
    invisible in the TUI and web UI.

    :param payload: A ``response_item`` payload already emitted into the
        rollout, e.g. ``{"type": "message", "role": "user", "content": [...]}``.
    :param timestamp: Rollout record timestamp, e.g.
        ``"2026-06-12T08:00:00.000Z"``.
    :returns: An ``event_msg`` record for user/assistant messages, or
        ``None`` for tool-call payloads (codex shows those via dedicated
        event types that are not needed for turn reconstruction).
    """
    if payload.get("type") != "message":
        return None
    text = " ".join(
        block.get("text", "") for block in payload.get("content", []) if isinstance(block, dict)
    ).strip()
    if not text:
        return None
    role = payload.get("role")
    if role == "user":
        event_payload: dict[str, Any] = {
            "type": "user_message",
            "message": text,
            "images": [],
            "local_images": [],
            "text_elements": [],
        }
    elif role == "assistant":
        event_payload = {
            "type": "agent_message",
            "message": text,
            "phase": "final_answer",
            "memory_citation": None,
        }
    else:
        return None
    return {"timestamp": timestamp, "type": "event_msg", "payload": event_payload}


def _interrupted_response_ids_from_session_items(items: list[dict[str, Any]]) -> set[str]:
    """
    Return response ids for Omnigent turns that ended interrupted.

    A Codex interrupted turn is persisted in Omnigent as visible transcript text
    plus an ``interrupted`` assistant marker. For native resume, the whole
    response group must be skipped so Codex does not restore the cancelled
    user request, partial assistant answer, or any partial tool history.

    :param items: Flat Omnigent item dicts in chronological order, e.g.
        ``[{"response_id": "codex_turn_123", "interrupted": True}]``.
    :returns: Response ids to exclude from synthesized Codex rollout
        history, e.g. ``{"codex_turn_123"}``.
    """
    response_ids: set[str] = set()
    for item in items:
        if not _is_interrupted_assistant_session_item(item):
            continue
        response_id = _session_item_response_id(item)
        if response_id is not None:
            response_ids.add(response_id)
    return response_ids


def _session_item_response_id(item: dict[str, Any]) -> str | None:
    """
    Extract a non-empty Omnigent response id from a flat item.

    :param item: Flat Omnigent item dict, e.g.
        ``{"response_id": "codex_turn_123"}``.
    :returns: Response id, e.g. ``"codex_turn_123"``, or ``None``.
    """
    response_id = item.get("response_id")
    return response_id if isinstance(response_id, str) and response_id else None


def _codex_response_item_from_session_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """
    Convert one Omnigent item into one Codex ``response_item`` payload.

    :param item: Flat Omnigent item dict, e.g.
        ``{"type": "function_call", "name": "shell", ...}``.
    :returns: Responses-style item payload, or ``None`` for unsupported
        or empty Omnigent items.
    """
    payload = _codex_response_item_payload(item)
    if payload is None:
        return None
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        payload["id"] = item_id
    return payload


def _is_interrupted_assistant_session_item(item: dict[str, Any]) -> bool:
    """
    Return whether an Omnigent item is an interrupted assistant partial.

    Omnigent persists these messages so the web transcript can show the text and
    the interrupted label after refresh. They must not be synthesized back
    into Codex's native rollout during resume, or Codex may treat a partial
    answer as completed history and continue from a cancelled turn.

    :param item: Flat Omnigent item dict, e.g.
        ``{"type": "message", "role": "assistant", "interrupted": True}``.
    :returns: ``True`` for interrupted assistant messages.
    """
    return (
        item.get("type") == "message"
        and item.get("role") == "assistant"
        and item.get("interrupted") is True
    )


def _codex_response_item_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    """
    Convert one supported Omnigent item into a Codex response payload body.

    :param item: Flat Omnigent item dict.
    :returns: Payload without the optional item id, or ``None`` for
        unsupported / empty Omnigent items.
    """
    item_type = item.get("type")
    if item_type == "message":
        return _codex_message_payload_from_session_item(item)
    if item_type == "function_call":
        return _codex_function_call_payload_from_session_item(item)
    if item_type == "function_call_output":
        return _codex_function_call_output_payload_from_session_item(item)
    return None


def _codex_message_payload_from_session_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """
    Convert an Omnigent message item into a Codex message payload.

    :param item: Omnigent message item.
    :returns: Codex message payload, or ``None`` for unsupported roles
        or empty text content.
    """
    role = item.get("role")
    if role == "user":
        api_type = "input_text"
    elif role == "assistant":
        api_type = "output_text"
    else:
        return None
    content = _codex_content_blocks_from_api_content(item.get("content"), api_type=api_type)
    if not content:
        return None
    return {"type": "message", "role": role, "content": content}


def _codex_function_call_payload_from_session_item(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Convert an Omnigent function call item into a Codex function call payload.

    :param item: Omnigent function call item.
    :returns: Codex function call payload, or ``None`` when optional
        routing fields are absent.
    :raises click.ClickException: If the Omnigent item violates required tool
        history fields.
    """
    name = item.get("name")
    call_id = item.get("call_id")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(call_id, str) or not call_id:
        return None
    arguments = item.get("arguments")
    if not isinstance(arguments, str):
        item_id = item.get("id")
        raise click.ClickException(
            "Cannot synthesize Codex resume rollout: Omnigent function_call "
            f"{item_id!r} has non-string arguments."
        )
    return {
        "type": "function_call",
        "name": name,
        "arguments": arguments,
        "call_id": call_id,
    }


def _codex_function_call_output_payload_from_session_item(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Convert an Omnigent function output item into a Codex function output payload.

    :param item: Omnigent function output item.
    :returns: Codex function output payload, or ``None`` when optional
        routing fields are absent.
    :raises click.ClickException: If the Omnigent item violates required tool
        output fields.
    """
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    output = item.get("output")
    if not isinstance(output, str):
        item_id = item.get("id")
        raise click.ClickException(
            "Cannot synthesize Codex resume rollout: Omnigent function_call_output "
            f"{item_id!r} has non-string output."
        )
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }


def _codex_content_blocks_from_api_content(
    content: object,
    *,
    api_type: str,
) -> list[dict[str, Any]]:
    """
    Extract text blocks from an Omnigent content array for Codex rollout items.

    :param content: Omnigent content array, e.g.
        ``[{"type": "input_text", "text": "hello"}]``.
    :param api_type: Omnigent block type to include, e.g.
        ``"input_text"`` or ``"output_text"``.
    :returns: Codex/OpenAI content blocks preserving *api_type*.
    """
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != api_type:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            blocks.append({"type": api_type, "text": text})
    return blocks


def _codex_turn_id_for_session_item(
    *,
    session_id: str,
    external_session_id: str,
    item: dict[str, Any],
    index: int,
) -> str:
    """
    Return a Codex turn id for an Omnigent item.

    Codex-native forwarder stores Omnigent ``response_id`` as
    ``"codex_<turn_id>"`` for mirrored items. When that prefix is not
    present, build a deterministic synthetic turn id from stable inputs.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param external_session_id: Codex thread id, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``.
    :param item: Flat Omnigent item dict.
    :param index: Zero-based fallback item index.
    :returns: Codex turn id, e.g. ``"turn_abc123"``.
    """
    response_id = item.get("response_id")
    if isinstance(response_id, str) and response_id.startswith("codex_"):
        turn_id = response_id.removeprefix("codex_")
        if turn_id:
            return turn_id
    stable = item.get("response_id") or item.get("id") or f"index-{index}"
    return (
        "turn_"
        + uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"omnigent-codex-resume:{session_id}:{external_session_id}:{stable}",
        ).hex
    )


def _codex_rollout_timestamp() -> str:
    """
    Return a UTC timestamp string for synthesized Codex rollout records.

    :returns: ISO-8601 timestamp with ``Z`` suffix, e.g.
        ``"2026-06-08T12:34:56.789Z"``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _patch_external_session_id(
    client: httpx.AsyncClient,
    session_id: str,
    thread_id: str,
) -> None:
    """
    Persist the native Codex thread id on the Omnigent session.

    :param client: HTTP client pointed at AP.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param thread_id: Codex thread id.
    :returns: None.
    """
    resp = await client.patch(
        f"/v1/sessions/{url_component(session_id)}",
        json={"external_session_id": thread_id},
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Codex thread bind failed ({resp.status_code}): {error_text(resp)}"
        )


async def _wait_for_thread_started(client: CodexAppServerClient) -> str:
    """
    Wait for the Codex TUI to create its remote app-server thread.

    Thin CLI-flavoured wrapper over the canonical
    :func:`omnigent.codex_native_forwarder.wait_for_thread_started`
    (shared with the host-spawned runner auto-create), translating its
    plain exceptions into ``click.ClickException`` for the CLI.

    :param client: Connected app-server client listening on the session
        Unix socket.
    :returns: Codex thread id, e.g. ``"019e70d7-1233-7b53-9c76-f1df1f6b1dba"``.
    :raises click.ClickException: If no ``thread/started`` event arrives
        before the startup timeout, or the stream ends first.
    """
    from omnigent.codex_native_forwarder import wait_for_thread_started

    try:
        return await wait_for_thread_started(client, timeout=_CODEX_THREAD_START_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise click.ClickException(
            "Codex TUI did not start a remote app-server thread before "
            f"the {_CODEX_THREAD_START_TIMEOUT_SECONDS:.0f}s timeout."
        ) from exc
    except RuntimeError as exc:
        raise click.ClickException(
            "Codex app-server event stream ended before thread startup."
        ) from exc


async def _start_initial_turn(app_server_url: str, thread_id: str, prompt: str) -> None:
    """
    Submit an initial prompt to a native Codex thread.

    :param app_server_url: App-server transport to connect over, e.g.
        ``"ws://127.0.0.1:9876"``.
    :param thread_id: Codex thread id.
    :param prompt: Prompt text.
    :returns: None.
    """
    client = client_for_transport(app_server_url, client_name="omnigent-codex-native")
    await client.connect()
    try:
        await client.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]},
        )
    finally:
        await client.close()


async def _launch_codex_terminal(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    codex_args: tuple[str, ...],
    command: str,
    thread_id: str | None,
    remote_url: str,
    env: dict[str, str],
    config_overrides: tuple[str, ...] = (),
) -> LaunchedCodexTerminal:
    """
    Launch the server-backed Codex terminal resource.

    :param client: HTTP client pointed at AP.
    :param session_id: Omnigent session id.
    :param codex_args: Raw Codex CLI args.
    :param command: Codex executable.
    :param thread_id: Codex thread id to resume. ``None`` starts a
        fresh remote Codex TUI thread.
    :param remote_url: App-server transport the Codex TUI attaches to
        via ``--remote``, e.g. ``"ws://127.0.0.1:9876"``.
    :param env: Environment overrides for the terminal process.
    :param config_overrides: Codex ``-c`` provider/model overrides to
        apply to the ``--remote`` TUI so it resolves the same provider
        as the app-server (and skips the OpenAI-login onboarding
        screen). See :func:`build_codex_remote_args`. Empty for a plain
        Codex-login launch. E.g.
        ``('model_provider="omnigent_databricks"',)``.
    :returns: Launched terminal resource details.
    """
    terminal_args = build_codex_remote_args(
        codex_args=codex_args,
        thread_id=thread_id,
        remote_url=remote_url,
        config_overrides=config_overrides,
    )
    body = {
        "terminal": _TERMINAL_NAME,
        "session_key": _TERMINAL_SESSION_KEY,
        "spec": {
            "command": command,
            "args": terminal_args,
            "os_env_type": "caller_process",
            "cwd": str(Path.cwd()),
            "env": env,
            "scrollback": _CODEX_TERMINAL_SCROLLBACK_LINES,
            "tmux_allow_passthrough": True,
            "tmux_start_on_attach": True,
        },
    }
    resp = await client.post(
        f"/v1/sessions/{url_component(session_id)}/resources/terminals",
        json=body,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Codex terminal launch failed ({resp.status_code}): {error_text(resp)}"
        )
    payload = resp.json()
    return _launched_codex_terminal_from_payload(payload)


def _launched_codex_terminal_from_payload(payload: object) -> LaunchedCodexTerminal:
    """
    Decode terminal launch metadata returned by the runner.

    :param payload: Decoded terminal resource JSON object, e.g.
        ``{"id": "terminal_codex_main", "metadata": {...}}``.
    :returns: Launched terminal details.
    :raises click.ClickException: If the response omits a valid
        terminal id.
    """
    if not isinstance(payload, dict):
        raise click.ClickException("Codex terminal launch returned non-object JSON.")
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException("Codex terminal launch response did not include terminal id.")
    metadata = payload.get("metadata")
    tmux_socket: Path | None = None
    tmux_target: str | None = None
    if isinstance(metadata, dict):
        raw_socket = metadata.get("tmux_socket")
        raw_target = metadata.get("tmux_target")
        if isinstance(raw_socket, str) and raw_socket:
            tmux_socket = Path(raw_socket)
        if isinstance(raw_target, str) and raw_target:
            tmux_target = raw_target
    return LaunchedCodexTerminal(
        terminal_id=terminal_id,
        tmux_socket=tmux_socket,
        tmux_target=tmux_target,
    )


async def _find_running_codex_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> LaunchedCodexTerminal | None:
    """
    Return the existing running Codex terminal id if present.

    Lookup happens before rebinding an existing session to this
    invocation's local runner. If the previously bound runner is
    offline, the resource route returns an unavailable status; treat
    that as a reattach miss so the caller can bind the current runner
    and cold-resume the Codex thread.

    :param client: HTTP client pointed at AP.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :returns: Terminal details, or ``None`` when absent.
    :raises click.ClickException: If the server rejects the lookup for
        a reason other than "not currently attachable".
    """
    terminal_id = codex_terminal_resource_id()
    resp = await client.get(
        f"/v1/sessions/{url_component(session_id)}"
        f"/resources/terminals/{url_component(terminal_id)}"
    )
    if _codex_terminal_lookup_is_reattach_miss(resp):
        return None
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Failed to fetch Codex terminal ({resp.status_code}): {error_text(resp)}"
        )
    payload = resp.json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("running") is False:
        return None
    return _launched_codex_terminal_from_payload(payload)


def _codex_terminal_lookup_is_reattach_miss(resp: httpx.Response) -> bool:
    """
    Return whether a terminal lookup means "launch a replacement".

    Only missing terminals and explicit runner-unavailable states are
    safe to treat as a reattach miss. Other 409 / 502 / 503 responses
    can indicate real server or infrastructure failures and should
    stay loud.

    :param resp: HTTP response from the terminal resource lookup.
    :returns: ``True`` when Codex should cold-resume into a new
        terminal; ``False`` when the response should be handled by the
        normal status path.
    """
    if resp.status_code == 404:
        return True
    error_code = _response_error_code(resp)
    if error_code == _RUNNER_UNAVAILABLE_ERROR_CODE:
        return True
    message = error_text(resp)
    if resp.status_code == 503 and _runner_offline_message(message):
        return True
    if error_code == _CONFLICT_ERROR_CODE and _UNBOUND_RUNNER_MESSAGE_FRAGMENT in message:
        return True
    if resp.status_code == 409 and _UNBOUND_RUNNER_MESSAGE_FRAGMENT in message:
        return True
    return False


def _response_error_code(resp: httpx.Response) -> str | None:
    """
    Extract a structured Omnigent error code from *resp* if present.

    :param resp: HTTP response from AP.
    :returns: ``error.code`` when the JSON body has one, otherwise
        ``None``.
    """
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


def _runner_offline_message(message: str) -> bool:
    """
    Return whether *message* is the Omnigent stale-runner error shape.

    :param message: Error text extracted from AP, e.g.
        ``"runner 'runner_abc' is offline for conversation 'conv_123'"``.
    :returns: ``True`` when the message specifically names an offline
        runner for the conversation being resumed.
    """
    return message.startswith("runner ") and _RUNNER_OFFLINE_MESSAGE_FRAGMENT in message


async def _close_codex_terminal(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    terminal_id: str,
) -> None:
    """
    Best-effort close of the AP-side Codex terminal resource.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param session_id: Omnigent session id.
    :param terminal_id: Terminal resource id.
    :returns: None.
    """
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(10.0),
        ) as client:
            await client.delete(
                f"/v1/sessions/{url_component(session_id)}"
                f"/resources/terminals/{url_component(terminal_id)}"
            )


def _resolve_session_id_for_resume(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    resume_picker: bool,
) -> str | None:
    """
    Translate resume inputs into a concrete Codex-native session id.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param session_id: Explicit session id, e.g. ``"conv_abc123"``.
    :param resume_picker: ``True`` for bare ``--resume``.
    :returns: Session id, or ``None`` for a fresh session / cancelled
        picker.
    """
    if session_id is not None:
        return session_id
    if not resume_picker:
        return None
    from omnigent_client import OmnigentClient

    from omnigent.repl._resume_picker import pick_conversation_by_wrapper_label_from_sdk

    async def _drive() -> str | None:
        """
        Run the async Codex-native picker.

        :returns: Selected Omnigent session id, or ``None``.
        """
        async with OmnigentClient(
            base_url=base_url,
            headers=headers if headers else None,
        ) as client:
            return await pick_conversation_by_wrapper_label_from_sdk(
                client,
                wrapper_value=_WRAPPER_LABEL_VALUE,
                agent_name=_AGENT_NAME,
            )

    return asyncio.run(_drive())


def codex_terminal_resource_id() -> str:
    """
    Return the deterministic terminal resource id for Codex.

    :returns: Terminal resource id, e.g. ``"terminal_codex_main"``.
    """
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)


def _preflight_local_tools() -> None:
    """
    Verify local executables required by the native Codex wrapper.

    :returns: None.
    :raises click.ClickException: If required tools are missing.
    """
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native Codex wrapper "
            "attaches to the runner-owned Codex tmux terminal."
        )
