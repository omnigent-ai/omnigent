"""Native GitHub Copilot TUI wrapper for the Omnigent CLI."""

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

from omnigent._native_resume_hint import echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._wrapper_labels import COPILOT_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE
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

_DEFAULT_COPILOT_COMMAND = "copilot"
_COPILOT_PATH_ENV = "OMNIGENT_COPILOT_PATH"
_COPILOT_TOKEN_ENV = "GH_TOKEN"
_AGENT_NAME = "copilot-native-ui"
_TERMINAL_NAME = "copilot"
_TERMINAL_SESSION_KEY = "main"
_SESSION_LABELS = {
    "omnigent.ui": "terminal",
    _WRAPPER_LABEL_KEY: _WRAPPER_LABEL_VALUE,
}


@dataclass(frozen=True)
class NativeCopilotLaunch:
    """Resolved native Copilot process launch."""

    executable: str
    argv: list[str]


@dataclass(frozen=True)
class LaunchedCopilotTerminal:
    """Terminal resource returned by the Omnigent runner launch path."""

    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None


@dataclass(frozen=True)
class PreparedCopilotTerminal:
    """Prepared native Copilot terminal attachment details."""

    session_id: str
    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None
    reattached: bool


def _configured_copilot_command(env: Mapping[str, str]) -> str:
    value = env.get(_COPILOT_PATH_ENV, "").strip()
    return value or _DEFAULT_COPILOT_COMMAND


def resolve_copilot_executable(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Resolve the native Copilot CLI executable."""
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    command = _configured_copilot_command(env)
    resolved = which(command)
    if resolved is None:
        raise click.ClickException(
            "Native Copilot requires the 'copilot' CLI on PATH. Install it with "
            "'npm install -g @github/copilot' (or the current GitHub package), "
            f"then sign in with GitHub. You can also set {_COPILOT_PATH_ENV}=/path/to/copilot."
        )
    return resolved


def build_copilot_launch(
    copilot_args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> NativeCopilotLaunch:
    """Build the argv for a native Copilot process."""
    executable = resolve_copilot_executable(env=env, which=which)
    return NativeCopilotLaunch(executable=executable, argv=[executable, *copilot_args])


def run_copilot_native(
    *,
    server: str | None,
    session_id: str | None,
    copilot_args: tuple[str, ...],
    resume_picker: bool = False,
    model: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """Launch the Copilot CLI in an Omnigent terminal."""
    _preflight_local_tools()
    if server is None:
        raise click.ClickException(
            "Copilot requires a resolved Omnigent server URL. The CLI should call "
            "_ensure_backend before run_copilot_native."
        )
    from omnigent.chat import _remote_headers

    headers = _remote_headers(server_url=server.rstrip("/"))
    with TemporaryDirectory(prefix="omnigent-copilot-native-") as tmpdir:
        spec_path = _materialize_copilot_agent_spec(Path(tmpdir), model=model)
        resolved_session_id = _resolve_session_id_for_resume(
            base_url=server.rstrip("/"),
            headers=headers,
            session_id=session_id,
            resume_picker=resume_picker,
        )
        if resolved_session_id is None and resume_picker and session_id is None:
            return
        _run_with_remote_server(
            server.rstrip("/"),
            headers=headers,
            spec_path=spec_path,
            session_id=resolved_session_id,
            copilot_args=copilot_args,
            auto_open_conversation=auto_open_conversation,
        )


def _materialize_copilot_agent_spec(tmpdir: Path, *, model: str | None = None) -> Path:
    """Write the terminal-first agent spec used by ``omnigent copilot``."""
    yaml_path = tmpdir / "copilot-native-ui.yaml"
    executor: dict[str, str] = {"harness": "copilot-native"}
    if model:
        executor["model"] = model
    raw: dict[str, Any] = {
        "name": _AGENT_NAME,
        "prompt": (
            "GitHub Copilot is running in the session terminal. The user drives "
            "the Copilot CLI directly."
        ),
        "executor": executor,
        "spawn": True,
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
        "terminals": native_shell_terminal_spec(),
    }
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return yaml_path


def _run_with_remote_server(
    base_url: str,
    headers: dict[str, str],
    spec_path: Path,
    *,
    session_id: str | None,
    copilot_args: tuple[str, ...],
    auto_open_conversation: bool = False,
) -> None:
    from omnigent.chat import _bundle_agent
    from omnigent.cli import _ensure_host_daemon
    from omnigent.host.identity import load_or_create_host_identity

    try:
        resolved_session_id = session_id

        async def _drive() -> None:
            with runner_startup_progress(initial_message="Preparing Copilot...") as progress:
                _update_startup_progress(progress, "Connecting to local daemon...")
                _ensure_host_daemon(base_url)
                host_id = load_or_create_host_identity().host_id
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_copilot_terminal_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    session_id=resolved_session_id,
                    session_bundle=bundle,
                    copilot_args=copilot_args,
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
            await _attach_terminal_resource(prepared)
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="copilot",
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


async def _prepare_copilot_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    copilot_args: tuple[str, ...],
    host_id: str,
    workspace: str,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedCopilotTerminal:
    """Create or resume a copilot-native session through a daemon runner."""
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        reattached = False
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Copilot session requires a session bundle.")
            _update_startup_progress(startup_progress, "Creating Copilot session...")
            session_id = await _create_copilot_session(
                client,
                session_bundle,
                terminal_launch_args=list(copilot_args) or None,
            )
        else:
            _update_startup_progress(startup_progress, "Loading Copilot session...")
            payload = await _fetch_copilot_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a copilot-native session."
                )
            existing_terminal = await _find_running_copilot_terminal(client, session_id)
            if existing_terminal is not None:
                _update_startup_progress(startup_progress, "Copilot terminal ready.")
                return PreparedCopilotTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    reattached=True,
                )
            if copilot_args:
                _update_startup_progress(startup_progress, "Updating Copilot session...")
                resp = await client.patch(
                    f"/v1/sessions/{url_component(session_id)}",
                    json={"terminal_launch_args": list(copilot_args)},
                )
                if resp.status_code >= 400:
                    raise click.ClickException(
                        f"Copilot session launch config update failed "
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
        _update_startup_progress(startup_progress, "Starting Copilot terminal...")
        await _ensure_copilot_terminal_on_runner(client, session_id)
        terminal = await _wait_for_copilot_terminal_ready(
            client,
            session_id,
            timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S,
        )
        _update_startup_progress(startup_progress, "Copilot terminal ready.")
    return PreparedCopilotTerminal(
        session_id=session_id,
        terminal_id=terminal.terminal_id,
        tmux_socket=terminal.tmux_socket,
        tmux_target=terminal.tmux_target,
        reattached=reattached,
    )


async def _create_copilot_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    terminal_launch_args: list[str] | None = None,
) -> str:
    metadata: dict[str, Any] = {"labels": dict(_SESSION_LABELS)}
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("copilot-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Copilot session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    new_session_id = body.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise click.ClickException("Copilot session creation response did not include session_id.")
    return new_session_id


async def _fetch_copilot_session(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
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


async def _ensure_copilot_terminal_on_runner(client: httpx.AsyncClient, session_id: str) -> None:
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
            f"Copilot terminal ensure failed ({resp.status_code}): {error_text(resp)}"
        )


async def _wait_for_copilot_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> LaunchedCopilotTerminal:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        terminal = await _find_running_copilot_terminal(client, session_id)
        if terminal is not None:
            return terminal
        await asyncio.sleep(0.2)
    raise click.ClickException(
        f"The runner did not create the Copilot terminal for {session_id!r} "
        f"within {timeout_s:.0f}s."
    )


async def _find_running_copilot_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> LaunchedCopilotTerminal | None:
    terminal_id = copilot_terminal_resource_id()
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
            f"Failed to fetch Copilot terminal ({resp.status_code}): {text}"
        )
    payload = resp.json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("running") is False:
        return None
    return _launched_copilot_terminal_from_payload(payload)


def _launched_copilot_terminal_from_payload(payload: object) -> LaunchedCopilotTerminal:
    if not isinstance(payload, dict):
        raise click.ClickException("Copilot terminal launch returned non-object JSON.")
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException("Copilot terminal launch response did not include terminal id.")
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
    return LaunchedCopilotTerminal(
        terminal_id=terminal_id,
        tmux_socket=tmux_socket,
        tmux_target=tmux_target,
    )


async def _attach_terminal_resource(prepared: PreparedCopilotTerminal) -> None:
    direct_tmux_error = _direct_tmux_unavailable_reason(prepared)
    if direct_tmux_error is not None:
        raise click.ClickException(
            f"Runner-owned Copilot terminal requires direct tmux attach, but {direct_tmux_error}"
        )
    if prepared.tmux_socket is None or prepared.tmux_target is None:
        raise click.ClickException("Copilot tmux attach metadata was incomplete.")
    await _attach_direct_tmux(prepared.tmux_socket, prepared.tmux_target)


async def _attach_direct_tmux(socket_path: Path, tmux_target: str) -> None:
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


def _direct_tmux_unavailable_reason(prepared: PreparedCopilotTerminal) -> str | None:
    if prepared.tmux_socket is None:
        return "the terminal resource did not include a tmux socket path."
    if prepared.tmux_target is None:
        return "the terminal resource did not include a tmux target."
    if not prepared.tmux_socket.exists():
        return f"tmux socket {prepared.tmux_socket} is not reachable from this CLI process."
    if shutil.which("tmux") is None:
        return "tmux is not available on PATH."
    return None


def copilot_terminal_resource_id() -> str:
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)


def _preflight_local_tools() -> None:
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native Copilot wrapper "
            "attaches to the runner-owned Copilot tmux terminal."
        )


def _update_startup_progress(
    startup_progress: RunnerStartupProgress | None,
    message: str,
) -> None:
    if startup_progress is not None:
        startup_progress.update(message)


def _resolve_session_id_for_resume(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    resume_picker: bool,
) -> str | None:
    """Translate resume inputs into a concrete Copilot-native session id."""
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
