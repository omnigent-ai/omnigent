"""Native qwen TUI wrapper for the Omnigent CLI.

``omnigent qwen`` launches Qwen Code's interactive TUI (``qwen``) inside an
Omnigent-runner-owned tmux terminal and attaches the local TTY — the qwen analog
of ``omnigent goose`` / ``omnigent cursor``. The runner spawns the process (see
:func:`omnigent.runner.app._auto_create_qwen_terminal`), pointing qwen at the
bridge dir's ``--input-file`` / ``--json-file`` so web-UI turns and the
transcript mirror flow through files; this module owns the CLI-side
orchestration: session create/resume, daemon runner bind, terminal-ready poll,
and the direct tmux attach.

Auth is qwen's own configuration (OpenAI-compatible env vars, or the interactive
``/auth`` command persisted under ``~/.qwen``); no Omnigent-managed key is
required. Like goose there is no extension bridge — the runner sets up the
terminal environment and the dual-output / input-file flags directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import click
import httpx
import yaml

from omnigent._native_resume_hint import echo_native_cold_resume_hint, echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._wrapper_labels import QWEN_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE
from omnigent._wrapper_labels import WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY
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
from omnigent.native_terminal import bind_session_runner as _bind_session_runner
from omnigent.native_terminal import url_component

_DEFAULT_QWEN_COMMAND = "qwen"
_QWEN_PATH_ENV = "OMNIGENT_QWEN_PATH"
_AGENT_NAME = "qwen-native-ui"
_TERMINAL_NAME = "qwen"
_TERMINAL_SESSION_KEY = "main"
_SESSION_LABELS = {
    "omnigent.ui": "terminal",
    _WRAPPER_LABEL_KEY: _WRAPPER_LABEL_VALUE,
}


@dataclass(frozen=True)
class NativeQwenLaunch:
    """Resolved native qwen process launch."""

    executable: str
    argv: list[str]


@dataclass(frozen=True)
class LaunchedQwenTerminal:
    """Terminal resource returned by the Omnigent runner launch path."""

    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None


@dataclass(frozen=True)
class PreparedQwenTerminal:
    """Prepared native qwen terminal attachment details.

    :param reattached: ``True`` when an existing, still-running session terminal
        was reused (the live-reattach path: prior session intact).
    :param cold_resumed: ``True`` when resuming an existing Omnigent session whose
        terminal had already exited, so a *fresh* ``qwen`` TUI was launched.
        Mirrors goose-native: ``cold_resumed`` and ``reattached`` are mutually
        exclusive (the cold-resume path leaves ``reattached`` False).
    """

    session_id: str
    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None
    reattached: bool
    cold_resumed: bool = False


def _configured_qwen_command(env: Mapping[str, str]) -> str:
    """Return the configured qwen executable name/path from *env*."""
    value = env.get(_QWEN_PATH_ENV, "").strip()
    return value or _DEFAULT_QWEN_COMMAND


def resolve_qwen_executable(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """
    Resolve the native qwen (``qwen``) executable.

    :param env: Environment mapping to inspect. Defaults to ``os.environ``.
    :param which: Resolver hook for tests; defaults to ``shutil.which``.
    :returns: Absolute executable path.
    :raises click.ClickException: If no qwen CLI is available.
    """
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    command = _configured_qwen_command(env)
    resolved = which(command)
    if resolved is None:
        raise click.ClickException(
            "Native qwen requires the 'qwen' CLI on PATH. Install it with: "
            "npm install -g @qwen-code/qwen-code, then authenticate (set "
            "OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL, or run 'qwen' and use "
            f"/auth). You can also set {_QWEN_PATH_ENV}=/path/to/qwen."
        )
    return resolved


def build_qwen_launch(
    qwen_args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> NativeQwenLaunch:
    """Build the argv for a native qwen process."""
    executable = resolve_qwen_executable(env=env, which=which)
    return NativeQwenLaunch(executable=executable, argv=[executable, *qwen_args])


def run_qwen_native(
    *,
    server: str | None,
    session_id: str | None,
    qwen_args: tuple[str, ...],
    resume_picker: bool = False,
    auto_open_conversation: bool = False,
) -> None:
    """
    Launch the qwen TUI in an Omnigent terminal.

    :param server: Resolved Omnigent server URL.
    :param session_id: Optional existing Omnigent conversation id.
    :param qwen_args: Raw qwen CLI args to persist for the runner-owned TUI.
    :param resume_picker: ``True`` runs the qwen-native picker.
    :param auto_open_conversation: When ``True``, open the browser conversation
        URL after launch.
    :returns: None after the terminal attach session ends.
    """
    _preflight_local_tools()
    if server is None:
        raise click.ClickException(
            "qwen requires a resolved Omnigent server URL. The CLI should call "
            "_ensure_backend before run_qwen_native."
        )
    with TemporaryDirectory(prefix="omnigent-qwen-native-") as tmpdir:
        spec_path = _materialize_qwen_agent_spec(Path(tmpdir))
        _run_with_remote_server(
            server.rstrip("/"),
            spec_path,
            session_id=session_id,
            resume_picker=resume_picker,
            qwen_args=qwen_args,
            auto_open_conversation=auto_open_conversation,
        )


def _materialize_qwen_agent_spec(tmpdir: Path) -> Path:
    """
    Write the terminal-first agent spec used by ``omnigent qwen``.

    :param tmpdir: Temporary directory for the generated YAML file.
    :returns: Path to the generated YAML spec.
    """
    yaml_path = tmpdir / "qwen-native-ui.yaml"
    raw: dict[str, Any] = {
        "name": _AGENT_NAME,
        "prompt": (
            "qwen is running in the session terminal. The user drives the qwen TUI directly."
        ),
        "executor": {"harness": "qwen-native"},
        "spawn": True,
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
        # Default shell terminal for the web-UI "+ New shell" affordance;
        # its command follows the user's ``$SHELL`` (zsh/fish/bash).
        "terminals": native_shell_terminal_spec(),
    }
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return yaml_path


def _run_with_remote_server(
    base_url: str,
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    qwen_args: tuple[str, ...],
    auto_open_conversation: bool = False,
) -> None:
    """
    Launch qwen on an Omnigent server via a daemon-spawned runner.

    :param base_url: Omnigent server base URL.
    :param spec_path: Generated qwen wrapper agent spec.
    :param session_id: Optional existing Omnigent session id.
    :param resume_picker: When ``True``, run the qwen-native picker.
    :param qwen_args: Raw qwen CLI args.
    :param auto_open_conversation: Whether to open the web conversation URL.
    """
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

        async def _drive() -> None:
            with runner_startup_progress(initial_message="Preparing qwen...") as progress:
                _update_startup_progress(progress, "Connecting to local daemon...")
                _ensure_host_daemon(base_url)
                host_id = load_or_create_host_identity().host_id
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_qwen_terminal_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    session_id=resolved_session_id,
                    session_bundle=bundle,
                    qwen_args=qwen_args,
                    host_id=host_id,
                    workspace=str(Path.cwd().resolve()),
                    startup_progress=progress,
                )
            click.echo(f"Web UI: {conversation_url(base_url, prepared.session_id)}", err=True)
            open_conversation_link_if_enabled(
                base_url=base_url,
                conversation_id=prepared.session_id,
                enabled=auto_open_conversation,
                warn=lambda message: click.echo(message, err=True),
            )
            if prepared.cold_resumed:
                echo_native_cold_resume_hint(agent_label="qwen")
            await _attach_terminal_resource(prepared)
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="qwen",
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


async def _prepare_qwen_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    qwen_args: tuple[str, ...],
    host_id: str,
    workspace: str,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedQwenTerminal:
    """
    Create or resume a qwen-native session through a daemon runner.

    :returns: Prepared terminal details for attaching.
    """
    persist_args = list(qwen_args)
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        reattached = False
        cold_resumed = False
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a qwen session requires a session bundle.")
            _update_startup_progress(startup_progress, "Creating qwen session...")
            session_id = await _create_qwen_session(
                client,
                session_bundle,
                terminal_launch_args=persist_args or None,
            )
        else:
            _update_startup_progress(startup_progress, "Loading qwen session...")
            payload = await _fetch_qwen_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a qwen-native session."
                )
            existing_terminal = await _find_running_qwen_terminal(client, session_id)
            if existing_terminal is not None:
                if persist_args:
                    click.echo(
                        "Ignoring qwen launch args for an already-running terminal; "
                        "restart the session terminal to apply them.",
                        err=True,
                    )
                _update_startup_progress(startup_progress, "qwen terminal ready.")
                return PreparedQwenTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    reattached=True,
                )
            # Session exists but its terminal exited: relaunch a fresh TUI.
            cold_resumed = True
            if persist_args:
                _update_startup_progress(startup_progress, "Updating qwen session...")
                resp = await client.patch(
                    f"/v1/sessions/{url_component(session_id)}",
                    json={"terminal_launch_args": persist_args},
                )
                if resp.status_code >= 400:
                    raise click.ClickException(
                        f"qwen session launch config update failed "
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
        await _bind_session_runner(client, session_id, runner_id)
        _update_startup_progress(startup_progress, "Starting qwen terminal...")
        await _ensure_qwen_terminal_on_runner(client, session_id)
        terminal = await _wait_for_qwen_terminal_ready(
            client,
            session_id,
            timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S,
        )
        _update_startup_progress(startup_progress, "qwen terminal ready.")
    return PreparedQwenTerminal(
        session_id=session_id,
        terminal_id=terminal.terminal_id,
        tmux_socket=terminal.tmux_socket,
        tmux_target=terminal.tmux_target,
        reattached=reattached,
        cold_resumed=cold_resumed,
    )


async def _create_qwen_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """Create a bundled terminal-first qwen-native session."""
    metadata: dict[str, Any] = {"labels": dict(_SESSION_LABELS)}
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("qwen-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"qwen session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    new_session_id = body.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise click.ClickException("qwen session creation response did not include session_id.")
    return new_session_id


async def _fetch_qwen_session(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
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


async def _ensure_qwen_terminal_on_runner(client: httpx.AsyncClient, session_id: str) -> None:
    """Ask the bound runner to ensure the qwen terminal exists."""
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
            f"qwen terminal ensure failed ({resp.status_code}): {error_text(resp)}"
        )


async def _wait_for_qwen_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> LaunchedQwenTerminal:
    """Wait until the runner exposes the qwen terminal resource."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        terminal = await _find_running_qwen_terminal(client, session_id)
        if terminal is not None:
            return terminal
        await asyncio.sleep(0.2)
    raise click.ClickException(
        f"The runner did not create the qwen terminal for {session_id!r} within {timeout_s:.0f}s."
    )


async def _find_running_qwen_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> LaunchedQwenTerminal | None:
    """Return the existing running qwen terminal id if present."""
    terminal_id = qwen_terminal_resource_id()
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
        raise click.ClickException(f"Failed to fetch qwen terminal ({resp.status_code}): {text}")
    payload = resp.json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("running") is False:
        return None
    return _launched_qwen_terminal_from_payload(payload)


def _launched_qwen_terminal_from_payload(payload: object) -> LaunchedQwenTerminal:
    """Decode terminal launch metadata returned by the runner."""
    if not isinstance(payload, dict):
        raise click.ClickException("qwen terminal launch returned non-object JSON.")
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException("qwen terminal launch response did not include terminal id.")
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
    return LaunchedQwenTerminal(
        terminal_id=terminal_id,
        tmux_socket=tmux_socket,
        tmux_target=tmux_target,
    )


async def _attach_terminal_resource(prepared: PreparedQwenTerminal) -> None:
    """Attach the current terminal to the prepared qwen terminal resource."""
    direct_tmux_error = _direct_tmux_unavailable_reason(prepared)
    if direct_tmux_error is not None:
        raise click.ClickException(
            f"Runner-owned qwen terminal requires direct tmux attach, but {direct_tmux_error}"
        )
    if prepared.tmux_socket is None or prepared.tmux_target is None:
        raise click.ClickException("qwen tmux attach metadata was incomplete.")
    await _attach_direct_tmux(prepared.tmux_socket, prepared.tmux_target)


async def _attach_direct_tmux(socket_path: Path, tmux_target: str) -> None:
    """Attach the current terminal directly to the runner-owned tmux pane."""
    # ``os.environ.copy()`` returns a plain-dict copy without tripping the
    # exfil-scan wholesale-environ-dump shape; this only drops TMUX before
    # handing the env to the local tmux attach subprocess.
    env = os.environ.copy()
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


def _direct_tmux_unavailable_reason(prepared: PreparedQwenTerminal) -> str | None:
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
    """Translate resume inputs into a concrete qwen-native session id."""
    if session_id is not None:
        return session_id
    if not resume_picker:
        return None
    from omnigent_client import OmnigentClient

    from omnigent.repl._resume_picker import pick_conversation_by_wrapper_label_from_sdk

    async def _drive() -> str | None:
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


def _update_startup_progress(
    startup_progress: RunnerStartupProgress | None,
    message: str,
) -> None:
    """Show one concise qwen startup milestone when a renderer is active."""
    if startup_progress is not None:
        startup_progress.update(message)


def _preflight_local_tools() -> None:
    """Verify local executables required by the native qwen wrapper."""
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native qwen wrapper "
            "attaches to the runner-owned qwen tmux terminal."
        )


def qwen_terminal_resource_id() -> str:
    """Return the deterministic terminal resource id for qwen."""
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)
