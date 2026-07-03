"""Native OpenCode wrapper agent spec for ``opencode-native-ui``.

Materializes the terminal-first built-in agent the server seeds (parallel
to ``omnigent.codex_native._materialize_codex_agent_spec``). The runner
owns the ``opencode serve`` process and SSE forwarder; this spec just binds
the ``opencode-native`` harness and declares the spawn/terminal surface so
the web UI renders the session terminal-first.

This module also hosts the interactive local ``omnigent opencode`` CLI wrapper
(:func:`run_opencode_native`, the analog of ``omnigent codex`` / ``omnigent pi``):
it ensures a local daemon + runner, creates-or-resumes the ``opencode-native-ui``
session (whose runner auto-creates the ``opencode serve`` + ``opencode attach``
terminal), and attaches this TTY directly to that runner-owned tmux pane — the
same web-UI takeover path, driven from the CLI. The provider/gateway comes from
the runner's ambient env / ``omnigent setup`` config (a profile-bound spec routes
through the Databricks gateway; otherwise OpenAI-/Anthropic-compatible env vars).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import click
import httpx
import yaml

from omnigent._native_resume_hint import echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._wrapper_labels import OPENCODE_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE
from omnigent._wrapper_labels import WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY
from omnigent.conversation_browser import conversation_url, open_conversation_link_if_enabled
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.host.daemon_launch import (
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
from omnigent.native_terminal import bind_session_runner as _bind_session_runner
from omnigent.native_terminal import url_component
from omnigent.opencode_native_state import read_launch_state, write_launch_state

_logger = logging.getLogger(__name__)

# Built-in native-UI agent name (matches the descriptor's
# ``wrapper_agent_name`` and the web native registry).
_AGENT_NAME = "opencode-native-ui"


def _materialize_opencode_agent_spec(
    tmpdir: Path,
    *,
    model: str | None = None,
) -> Path:
    """
    Write the terminal-first agent spec used by the OpenCode native UI.

    :param tmpdir: Temporary directory for the generated YAML file.
    :param model: Optional model id, e.g. ``"anthropic/claude-opus-4"``.
    :returns: Path to the generated YAML spec.
    """
    yaml_path = tmpdir / "opencode-native-ui.yaml"
    executor: dict[str, str] = {"harness": "opencode-native"}
    if model is not None:
        executor["model"] = model
    raw: dict[str, Any] = {
        "name": _AGENT_NAME,
        "prompt": (
            "OpenCode is running in the session terminal. Web UI messages are "
            "forwarded into the same native OpenCode server session."
        ),
        "executor": executor,
        # Opt the native session into the child-session spawn writes
        # (sys_session_create / sys_session_send / sys_session_close) so the
        # wrapped opencode can author agent configs and launch them as
        # sub-agent sessions. The relay derives its advertised tool set from
        # this spec via ToolManager.
        "spawn": True,
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
        # Declare a default shell terminal so the relay advertises the
        # ``sys_terminal_*`` family to the wrapped opencode (the relay's gate
        # is a non-empty ``terminals:`` block on this spec).
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
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return yaml_path


_TERMINAL_NAME = "opencode"
_TERMINAL_SESSION_KEY = "main"
_SESSION_LABELS = {
    "omnigent.ui": "terminal",
    _WRAPPER_LABEL_KEY: _WRAPPER_LABEL_VALUE,
}


@dataclass(frozen=True)
class LaunchedOpenCodeTerminal:
    """Terminal resource returned by the Omnigent runner launch path."""

    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None


@dataclass(frozen=True)
class PreparedOpenCodeTerminal:
    """Prepared native OpenCode terminal attachment details."""

    session_id: str
    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None
    reattached: bool


def opencode_terminal_resource_id() -> str:
    """:returns: The deterministic terminal resource id for OpenCode."""
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)


def _preflight_local_tools() -> None:
    """Verify local executables the native OpenCode wrapper needs."""
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native OpenCode wrapper "
            "attaches to the runner-owned OpenCode tmux terminal."
        )


def run_opencode_native(  # pragma: no cover
    *,
    server: str | None,
    session_id: str | None,
    opencode_args: tuple[str, ...],
    resume_picker: bool = False,
    model: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Launch the OpenCode TUI in an Omnigent terminal (the ``omnigent opencode`` path).

    Mirrors ``omnigent codex`` / ``omnigent pi``: ensure a local daemon + runner,
    create-or-resume the ``opencode-native-ui`` session (the runner auto-creates
    the ``opencode serve`` + ``opencode attach`` terminal), then attach this TTY
    to that runner-owned tmux pane.

    :param server: Resolved Omnigent server URL. ``None`` is an error (the CLI
        must resolve a backend first).
    :param session_id: Optional existing Omnigent conversation id to resume.
    :param opencode_args: Raw ``opencode`` CLI args to persist for the TUI.
    :param resume_picker: When ``True``, run the opencode-native resume picker.
    :param model: Optional model id pinned on the materialized wrapper spec.
    :param auto_open_conversation: Open the browser conversation URL on launch.
    :returns: None after the terminal attach session ends.
    """
    _preflight_local_tools()
    if server is None:
        raise click.ClickException(
            "OpenCode requires a resolved Omnigent server URL. The CLI should resolve "
            "a backend before run_opencode_native."
        )
    with TemporaryDirectory(prefix="omnigent-opencode-native-") as tmpdir:
        spec_path = _materialize_opencode_agent_spec(Path(tmpdir), model=model)
        _run_with_remote_server(
            server.rstrip("/"),
            spec_path,
            session_id=session_id,
            resume_picker=resume_picker,
            opencode_args=opencode_args,
            auto_open_conversation=auto_open_conversation,
        )


def _run_with_remote_server(  # pragma: no cover
    base_url: str,
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    opencode_args: tuple[str, ...],
    auto_open_conversation: bool = False,
) -> None:
    """Launch OpenCode on an Omnigent server via a daemon-spawned runner."""
    from omnigent.chat import _bundle_agent, _remote_headers
    from omnigent.cli import _ensure_host_daemon
    from omnigent.host.identity import load_or_create_host_identity

    headers = _remote_headers(server_url=base_url)
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
            with runner_startup_progress(initial_message="Preparing OpenCode...") as progress:
                _update_startup_progress(progress, "Connecting to local daemon...")
                _ensure_host_daemon(base_url)
                host_id = load_or_create_host_identity().host_id
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_opencode_terminal_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    session_id=resolved_session_id,
                    session_bundle=bundle,
                    opencode_args=opencode_args,
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
            await _attach_terminal_resource(prepared)
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="opencode",
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


async def _prepare_opencode_terminal_via_daemon(  # pragma: no cover
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    opencode_args: tuple[str, ...],
    host_id: str,
    workspace: str,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedOpenCodeTerminal:
    """Create or resume an opencode-native session through a daemon runner."""
    persist_args = list(opencode_args)
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        reattached = session_id is not None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException(
                    "Creating an OpenCode session requires a session bundle."
                )
            _update_startup_progress(startup_progress, "Creating OpenCode session...")
            session_id = await _create_opencode_session(
                client, session_bundle, terminal_launch_args=persist_args or None
            )
        else:
            _update_startup_progress(startup_progress, "Loading OpenCode session...")
            payload = await _fetch_opencode_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not an opencode-native session."
                )
            existing_terminal = await _find_running_opencode_terminal(client, session_id)
            if existing_terminal is not None:
                if persist_args:
                    click.echo(
                        "Ignoring OpenCode launch args for an already-running terminal; "
                        "restart the session terminal to apply them.",
                        err=True,
                    )
                _update_startup_progress(startup_progress, "OpenCode terminal ready.")
                return PreparedOpenCodeTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    reattached=True,
                )
            if persist_args:
                _update_startup_progress(startup_progress, "Updating OpenCode session...")
                resp = await client.patch(
                    f"/v1/sessions/{url_component(session_id)}",
                    json={"terminal_launch_args": persist_args},
                )
                if resp.status_code >= 400:
                    raise click.ClickException(
                        f"OpenCode session launch config update failed "
                        f"({resp.status_code}): {error_text(resp)}"
                    )

        await wait_for_host_online(client, host_id, timeout_s=_DAEMON_HOST_ONLINE_TIMEOUT_S)
        _update_startup_progress(startup_progress, "Starting runner...")
        runner_id = await launch_or_reuse_daemon_runner(
            client, host_id=host_id, session_id=session_id, workspace=workspace
        )
        _update_startup_progress(startup_progress, "Waiting for runner...")
        await wait_for_runner_online(client, runner_id, timeout_s=_DAEMON_RUNNER_ONLINE_TIMEOUT_S)
        await _bind_session_runner(client, session_id, runner_id)
        _update_startup_progress(startup_progress, "Starting OpenCode terminal...")
        await _ensure_opencode_terminal_on_runner(client, session_id)
        terminal = await _wait_for_opencode_terminal_ready(
            client, session_id, timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S
        )
        _update_startup_progress(startup_progress, "OpenCode terminal ready.")
    return PreparedOpenCodeTerminal(
        session_id=session_id,
        terminal_id=terminal.terminal_id,
        tmux_socket=terminal.tmux_socket,
        tmux_target=terminal.tmux_target,
        reattached=reattached,
    )


async def _create_opencode_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """Create a bundled terminal-first opencode-native session."""
    metadata: dict[str, Any] = {"labels": dict(_SESSION_LABELS)}
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("opencode-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"OpenCode session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    new_session_id = body.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise click.ClickException(
            "OpenCode session creation response did not include session_id."
        )
    return new_session_id


async def _fetch_opencode_session(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
    """Fetch an existing Omnigent session."""
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


async def _ensure_opencode_terminal_on_runner(client: httpx.AsyncClient, session_id: str) -> None:
    """Ask the bound runner to ensure the OpenCode terminal exists (idempotent)."""
    resp = await client.post(
        f"/v1/sessions/{url_component(session_id)}/resources/terminals",
        json={
            "terminal": _TERMINAL_NAME,
            "session_key": _TERMINAL_SESSION_KEY,
            "ensure_native_terminal": True,
        },
        timeout=60.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"OpenCode terminal ensure failed ({resp.status_code}): {error_text(resp)}"
        )


async def _wait_for_opencode_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> LaunchedOpenCodeTerminal:
    """Wait until the runner exposes the OpenCode terminal resource."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        terminal = await _find_running_opencode_terminal(client, session_id)
        if terminal is not None:
            return terminal
        await asyncio.sleep(0.2)
    raise click.ClickException(
        f"The runner did not create the OpenCode terminal for {session_id!r} "
        f"within {timeout_s:.0f}s."
    )


# --- Resume workspace alignment: record launch cwd; realign it on resume ---

_RESUME_ACTION_SWITCH = "switch"
_RESUME_ACTION_CANCEL = "cancel"


def _record_launch_for_fresh_session(session_id: str) -> None:
    """
    Persist the wrapper's current cwd as the OpenCode session launch state.

    :param session_id: Newly created Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :returns: None.
    """
    try:
        write_launch_state(session_id, str(Path.cwd().resolve()))
    except OSError:
        _logger.warning(
            "failed to record opencode-native launch state for %s",
            session_id,
            exc_info=True,
        )


def _align_working_directory_with_session(session_id: str) -> None:
    """
    Resolve cwd mismatch before resuming an OpenCode-native session.

    Native OpenCode state is workspace-scoped from the user's point of
    view: the runner and ``opencode serve`` should reopen from the
    directory where the session was created. If client-side launch
    state points at a different existing directory, prompt whether to
    switch there before the runner and ``opencode serve`` sample cwd.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: None. Side-effect-only; may change process cwd.
    :raises click.ClickException: If recorded state exists but the
        recorded directory no longer exists, or if the user cancels.
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
            "before resuming OpenCode."
        )
    action = _prompt_opencode_resume_workspace_action(
        recorded_path=recorded_path,
        current=current,
    )
    if action == _RESUME_ACTION_SWITCH:
        os.chdir(recorded_path)
        click.echo(f"Switched to {recorded_path}.", err=True)
        return
    raise click.ClickException("Resume cancelled.")


def _prompt_opencode_resume_workspace_action(
    *,
    recorded_path: Path,
    current: Path,
) -> str:
    """
    Ask how to handle an OpenCode resume cwd mismatch.

    :param recorded_path: Recorded launch cwd, already resolved.
    :param current: Current cwd, already resolved.
    :returns: One of ``"switch"`` or ``"cancel"``.
    """
    click.echo(f"\nSession was started in: {recorded_path}", err=True)
    click.echo(f"Current working directory: {current}", err=True)
    click.echo("OpenCode resume is workspace-scoped. Choose an action:", err=True)
    click.echo(
        f"  {_RESUME_ACTION_SWITCH:<6} - Switch working directory to {recorded_path}", err=True
    )
    click.echo(f"  {_RESUME_ACTION_CANCEL:<6} - Cancel resume", err=True)
    return click.prompt(
        "Resume action",
        type=click.Choice([_RESUME_ACTION_SWITCH, _RESUME_ACTION_CANCEL]),
        default=_RESUME_ACTION_SWITCH,
        show_choices=True,
        err=True,
    )


async def _find_running_opencode_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> LaunchedOpenCodeTerminal | None:
    """Return the existing running OpenCode terminal if present."""
    terminal_id = opencode_terminal_resource_id()
    resp = await client.get(
        f"/v1/sessions/{url_component(session_id)}"
        f"/resources/terminals/{url_component(terminal_id)}"
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        text = error_text(resp)
        if resp.status_code in {409, 503} and (
            "not bound to a runner" in text or "offline" in text
        ):
            return None
        raise click.ClickException(
            f"Failed to fetch OpenCode terminal ({resp.status_code}): {text}"
        )
    payload = resp.json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("running") is False:
        return None
    return _launched_opencode_terminal_from_payload(payload)


def _launched_opencode_terminal_from_payload(payload: object) -> LaunchedOpenCodeTerminal:
    """Decode terminal launch metadata returned by the runner."""
    if not isinstance(payload, dict):
        raise click.ClickException("OpenCode terminal launch returned non-object JSON.")
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException(
            "OpenCode terminal launch response did not include terminal id."
        )
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
    return LaunchedOpenCodeTerminal(
        terminal_id=terminal_id,
        tmux_socket=tmux_socket,
        tmux_target=tmux_target,
    )


async def _attach_terminal_resource(  # pragma: no cover
    prepared: PreparedOpenCodeTerminal,
) -> None:
    """Attach the current terminal to the prepared OpenCode terminal resource."""
    reason = _direct_tmux_unavailable_reason(prepared)
    if reason is not None:
        raise click.ClickException(
            f"Runner-owned OpenCode terminal requires direct tmux attach, but {reason}"
        )
    assert prepared.tmux_socket is not None and prepared.tmux_target is not None
    await _attach_direct_tmux(prepared.tmux_socket, prepared.tmux_target)


async def _attach_direct_tmux(socket_path: Path, tmux_target: str) -> None:  # pragma: no cover
    """Attach the current terminal directly to the runner-owned tmux pane."""
    env = dict(os.environ)
    env.pop("TMUX", None)
    process = await asyncio.create_subprocess_exec(
        "tmux", "-S", str(socket_path), "-f", os.devnull, "attach", "-t", tmux_target, env=env
    )
    await process.wait()


def _direct_tmux_unavailable_reason(prepared: PreparedOpenCodeTerminal) -> str | None:
    """Explain why direct tmux attach is unavailable."""
    if prepared.tmux_socket is None:
        return "the terminal resource did not include a tmux socket path."
    if prepared.tmux_target is None:
        return "the terminal resource did not include a tmux target."
    if not prepared.tmux_socket.exists():
        return f"tmux socket {prepared.tmux_socket} is not reachable from this CLI process."
    if shutil.which("tmux") is None:
        return "tmux is not available on PATH."
    return None


def _resolve_session_id_for_resume(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    resume_picker: bool,
) -> str | None:
    """Translate resume inputs into a concrete opencode-native session id."""
    if session_id is not None:
        return session_id
    if not resume_picker:
        return None
    # Interactive SDK resume picker — exercised manually / via the live host
    # e2e, not unit tests (it opens an OmnigentClient and an arrow-key picker).
    from omnigent_client import OmnigentClient  # pragma: no cover

    from omnigent.repl._resume_picker import pick_conversation_by_wrapper_label_from_sdk

    async def _drive() -> str | None:  # pragma: no cover
        async with OmnigentClient(
            base_url=base_url, headers=headers if headers else None
        ) as client:
            return await pick_conversation_by_wrapper_label_from_sdk(
                client, wrapper_value=_WRAPPER_LABEL_VALUE, agent_name=_AGENT_NAME
            )

    return asyncio.run(_drive())  # pragma: no cover


def _update_startup_progress(
    startup_progress: RunnerStartupProgress | None,
    message: str,
) -> None:
    """Show one concise OpenCode startup milestone when a renderer is active."""
    if startup_progress is not None:
        startup_progress.update(message)
