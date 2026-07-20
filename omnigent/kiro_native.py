"""Native Kiro TUI wrapper for the Omnigent CLI."""

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
from omnigent._wrapper_labels import KIRO_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE
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

_DEFAULT_KIRO_COMMAND = "kiro-cli"
_KIRO_PATH_ENV = "OMNIGENT_KIRO_PATH"
_AGENT_NAME = "kiro-native-ui"

# Curated kiro-cli base models for the Web UI picker. Static (like cursor-native,
# the other launch-only-model vendor) rather than runner-discovered: the list is
# global and fixed, not account-scoped. ids match what ``kiro-cli --model`` accepts
# (and ``--list-models`` reports); ``auto`` is kiro's default and a real literal id.
# Refresh by hand from ``kiro-cli chat --list-models --format json`` if Kiro
# ships/renames a model.
_KIRO_BASE_MODELS: list[dict[str, Any]] = [
    {"id": "auto", "displayName": "Auto", "isDefault": True},
    {"id": "claude-sonnet-4.5", "displayName": "Claude Sonnet 4.5"},
    {"id": "claude-sonnet-4", "displayName": "Claude Sonnet 4"},
    {"id": "claude-haiku-4.5", "displayName": "Claude Haiku 4.5"},
    {"id": "deepseek-3.2", "displayName": "DeepSeek V3.2"},
    {"id": "minimax-m2.5", "displayName": "MiniMax M2.5"},
    {"id": "minimax-m2.1", "displayName": "MiniMax M2.1"},
    {"id": "glm-5", "displayName": "GLM-5"},
    {"id": "qwen3-coder-next", "displayName": "Qwen3 Coder Next"},
]


def kiro_base_model_options() -> list[dict[str, Any]]:
    """Return the curated kiro base-model options for the Web UI picker.

    Mirrors :func:`omnigent.cursor_native.cursor_base_model_options`: each option
    carries ``id`` (the value ``kiro-cli --model`` accepts), ``displayName``, and
    ``isDefault``/``isCurrent`` flags. kiro applies the model only at launch, so
    the picked id is persisted as ``model_override`` and consumed by the runner.

    :returns: Fresh option dicts (callers may mutate); base order preserved.
    """
    return [
        {
            "id": m["id"],
            "displayName": m["displayName"],
            "isDefault": bool(m.get("isDefault", False)),
            "isCurrent": False,
        }
        for m in _KIRO_BASE_MODELS
    ]


_TERMINAL_NAME = "kiro"
_TERMINAL_SESSION_KEY = "main"
_TMUX_ATTACH_ENV_ALLOWLIST = (
    "COLORTERM",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "TERM_PROGRAM",
    "TMPDIR",
    "USER",
)
_SESSION_LABELS = {
    "omnigent.ui": "terminal",
    _WRAPPER_LABEL_KEY: _WRAPPER_LABEL_VALUE,
}


@dataclass(frozen=True)
class NativeKiroLaunch:
    """Resolved native Kiro process launch."""

    executable: str
    argv: list[str]


@dataclass(frozen=True)
class LaunchedKiroTerminal:
    """Terminal resource returned by the Omnigent runner launch path."""

    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None


@dataclass(frozen=True)
class PreparedKiroTerminal:
    """Prepared native Kiro terminal attachment details."""

    session_id: str
    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None
    reattached: bool
    cold_resumed: bool = False


def _configured_kiro_command(env: Mapping[str, str]) -> str:
    """Return the configured kiro-cli executable name/path from *env*."""
    value = env.get(_KIRO_PATH_ENV, "").strip()
    return value or _DEFAULT_KIRO_COMMAND


def resolve_kiro_executable(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Resolve the native Kiro (``kiro-cli``) executable."""
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    command = _configured_kiro_command(env)
    resolved = which(command)
    if resolved is None:
        raise click.ClickException(
            "Native Kiro requires the 'kiro-cli' CLI on PATH. Install and login to "
            f"Kiro, or set {_KIRO_PATH_ENV}=/path/to/kiro-cli."
        )
    return resolved


def build_kiro_launch(
    kiro_args: Sequence[str],
    *,
    model: str | None = None,
    prompt: str | None = None,
    resume_id: str | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> NativeKiroLaunch:
    """Build the argv for a native Kiro TUI process."""
    executable = resolve_kiro_executable(env=env, which=which)
    argv = [executable, "chat", "--tui"]
    if resume_id:
        argv.extend(["--resume-id", resume_id])
    if model:
        argv.extend(["--model", model])
    argv.extend(kiro_args)
    if prompt:
        argv.append(prompt)
    return NativeKiroLaunch(executable=executable, argv=argv)


def run_kiro_native(
    *,
    server: str | None,
    session_id: str | None,
    kiro_args: tuple[str, ...],
    resume_picker: bool = False,
    model: str | None = None,
    prompt: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """Launch the Kiro TUI in an Omnigent terminal."""
    _preflight_local_tools()
    if server is None:
        raise click.ClickException(
            "Kiro requires a resolved Omnigent server URL. The CLI should call "
            "_ensure_backend before run_kiro_native."
        )
    with TemporaryDirectory(prefix="omnigent-kiro-native-") as tmpdir:
        spec_path = _materialize_kiro_agent_spec(Path(tmpdir), model=model)
        _run_with_remote_server(
            server.rstrip("/"),
            spec_path,
            session_id=session_id,
            resume_picker=resume_picker,
            kiro_args=kiro_args,
            model=model,
            prompt=prompt,
            auto_open_conversation=auto_open_conversation,
        )


def _materialize_kiro_agent_spec(tmpdir: Path, *, model: str | None = None) -> Path:
    """Write the terminal-first agent spec used by ``omnigent kiro``."""
    yaml_path = tmpdir / "kiro-native-ui.yaml"
    executor: dict[str, str] = {"harness": "kiro-native"}
    if model:
        executor["model"] = model
    raw: dict[str, Any] = {
        "name": _AGENT_NAME,
        "prompt": (
            "Kiro is running in the session terminal. The user drives the kiro-cli TUI directly."
        ),
        "executor": executor,
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
    kiro_args: tuple[str, ...],
    model: str | None = None,
    prompt: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """Launch Kiro on an Omnigent server via a daemon-spawned runner."""
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
            with runner_startup_progress(initial_message="Preparing Kiro...") as progress:
                _update_startup_progress(progress, "Connecting to local daemon...")
                _ensure_host_daemon(base_url)
                host_id = load_or_create_host_identity().host_id
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_kiro_terminal_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    session_id=resolved_session_id,
                    session_bundle=bundle,
                    kiro_args=kiro_args,
                    model=model,
                    prompt=prompt,
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
                echo_native_cold_resume_hint(agent_label="Kiro")
            await _attach_terminal_resource(prepared)
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="kiro",
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


async def _prepare_kiro_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    kiro_args: tuple[str, ...],
    model: str | None,
    prompt: str | None,
    host_id: str,
    workspace: str,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedKiroTerminal:
    """Create or resume a kiro-native session through a daemon runner."""
    persist_args = list(kiro_args)
    if model:
        persist_args[:0] = ["--model", model]
    if prompt:
        persist_args.append(prompt)
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        reattached = False
        cold_resumed = False
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Kiro session requires a session bundle.")
            _update_startup_progress(startup_progress, "Creating Kiro session...")
            session_id = await _create_kiro_session(
                client,
                session_bundle,
                terminal_launch_args=persist_args or None,
            )
        else:
            _update_startup_progress(startup_progress, "Loading Kiro session...")
            payload = await _fetch_kiro_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a kiro-native session."
                )
            existing_terminal = await _find_running_kiro_terminal(client, session_id)
            if existing_terminal is not None:
                if persist_args:
                    click.echo(
                        "Ignoring Kiro launch args for an already-running terminal; "
                        "restart the session terminal to apply them.",
                        err=True,
                    )
                _update_startup_progress(startup_progress, "Kiro terminal ready.")
                return PreparedKiroTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    reattached=True,
                )
            cold_resumed = True
            if persist_args:
                _update_startup_progress(startup_progress, "Updating Kiro session...")
                resp = await client.patch(
                    f"/v1/sessions/{url_component(session_id)}",
                    json={"terminal_launch_args": persist_args},
                )
                if resp.status_code >= 400:
                    raise click.ClickException(
                        f"Kiro session launch config update failed "
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
        _update_startup_progress(startup_progress, "Starting Kiro terminal...")
        await _ensure_kiro_terminal_on_runner(client, session_id)
        terminal = await _wait_for_kiro_terminal_ready(
            client,
            session_id,
            timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S,
        )
        _update_startup_progress(startup_progress, "Kiro terminal ready.")
    return PreparedKiroTerminal(
        session_id=session_id,
        terminal_id=terminal.terminal_id,
        tmux_socket=terminal.tmux_socket,
        tmux_target=terminal.tmux_target,
        reattached=reattached,
        cold_resumed=cold_resumed,
    )


async def _create_kiro_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """Create a bundled terminal-first kiro-native session."""
    metadata: dict[str, Any] = {"labels": dict(_SESSION_LABELS)}
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("kiro-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Kiro session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    new_session_id = body.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise click.ClickException("Kiro session creation response did not include session_id.")
    return new_session_id


async def _fetch_kiro_session(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
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


async def _ensure_kiro_terminal_on_runner(client: httpx.AsyncClient, session_id: str) -> None:
    """Ask the bound runner to ensure the Kiro terminal exists."""
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
            f"Kiro terminal ensure failed ({resp.status_code}): {error_text(resp)}"
        )


async def _wait_for_kiro_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> LaunchedKiroTerminal:
    """Wait until the runner exposes the Kiro terminal resource."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        terminal = await _find_running_kiro_terminal(client, session_id)
        if terminal is not None:
            return terminal
        await asyncio.sleep(0.2)
    raise click.ClickException(
        f"The runner did not create the Kiro terminal for {session_id!r} within {timeout_s:.0f}s."
    )


async def _find_running_kiro_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> LaunchedKiroTerminal | None:
    """Return the existing running Kiro terminal id if present."""
    terminal_id = kiro_terminal_resource_id()
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
        raise click.ClickException(f"Failed to fetch Kiro terminal ({resp.status_code}): {text}")
    payload = resp.json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("running") is False:
        return None
    return _launched_kiro_terminal_from_payload(payload)


def _launched_kiro_terminal_from_payload(payload: object) -> LaunchedKiroTerminal:
    """Decode terminal launch metadata returned by the runner."""
    if not isinstance(payload, dict):
        raise click.ClickException("Kiro terminal launch returned non-object JSON.")
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException("Kiro terminal launch response did not include terminal id.")
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
    return LaunchedKiroTerminal(
        terminal_id=terminal_id,
        tmux_socket=tmux_socket,
        tmux_target=tmux_target,
    )


async def _attach_terminal_resource(prepared: PreparedKiroTerminal) -> None:
    """Attach the current terminal to the prepared Kiro terminal resource."""
    direct_tmux_error = _direct_tmux_unavailable_reason(prepared)
    if direct_tmux_error is not None:
        raise click.ClickException(
            f"Runner-owned Kiro terminal requires direct tmux attach, but {direct_tmux_error}"
        )
    if prepared.tmux_socket is None or prepared.tmux_target is None:
        raise click.ClickException("Kiro tmux attach metadata was incomplete.")
    await _attach_direct_tmux(prepared.tmux_socket, prepared.tmux_target)


async def _attach_direct_tmux(socket_path: Path, tmux_target: str) -> None:
    """Attach the current terminal directly to the runner-owned tmux pane."""
    process = await asyncio.create_subprocess_exec(
        "tmux",
        "-S",
        str(socket_path),
        "-f",
        os.devnull,
        "attach",
        "-t",
        tmux_target,
        env=_tmux_attach_env(),
    )
    await process.wait()


def _tmux_attach_env() -> dict[str, str]:
    """Return the small local environment needed by ``tmux attach``."""
    return {key: os.environ[key] for key in _TMUX_ATTACH_ENV_ALLOWLIST if os.environ.get(key)}


def _direct_tmux_unavailable_reason(prepared: PreparedKiroTerminal) -> str | None:
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
    """Translate resume inputs into a concrete kiro-native session id."""
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
    """Show one concise Kiro startup milestone when a renderer is active."""
    if startup_progress is not None:
        startup_progress.update(message)


def _preflight_local_tools() -> None:
    """Verify local executables required by the native Kiro wrapper."""
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native Kiro wrapper "
            "attaches to the runner-owned Kiro tmux terminal."
        )


def kiro_terminal_resource_id() -> str:
    """Return the deterministic terminal resource id for Kiro."""
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)
