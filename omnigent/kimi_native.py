"""Native Kimi TUI wrapper for the Omnigent CLI.

``omnigent kimi`` launches the Kimi CLI's interactive TUI (``kimi``
with no args) inside an Omnigent-runner-owned tmux terminal and attaches the
local TTY — the kimi analog of ``omnigent codex`` / ``omnigent pi``. The runner
spawns the process (see :func:`omnigent.runner.app._auto_create_kimi_terminal`);
this module owns the CLI-side orchestration: session create/resume, daemon
runner bind, terminal-ready poll, and the direct tmux attach.

Auth is the ambient ``kimi login`` (``$HOME/.kimi``); no API key is
required. Unlike Pi there is no extension bridge — the runner sets up the
terminal environment directly.
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
from omnigent._wrapper_labels import KIMI_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE
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

_DEFAULT_KIMI_COMMAND = "kimi"
_KIMI_PATH_ENV = "OMNIGENT_KIMI_PATH"
_AGENT_NAME = "kimi-native-ui"
_TERMINAL_NAME = "kimi"
_TERMINAL_SESSION_KEY = "main"
_SESSION_LABELS = {
    "omnigent.ui": "terminal",
    _WRAPPER_LABEL_KEY: _WRAPPER_LABEL_VALUE,
}


@dataclass(frozen=True)
class NativeKimiLaunch:
    """Resolved native Kimi process launch."""

    executable: str
    argv: list[str]


@dataclass(frozen=True)
class LaunchedKimiTerminal:
    """Terminal resource returned by the Omnigent runner launch path."""

    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None


@dataclass(frozen=True)
class PreparedKimiTerminal:
    """Prepared native Kimi terminal attachment details.

    :param reattached: ``True`` when an existing, still-running session
        terminal was reused (the live-reattach path: prior chat is
        intact).
    :param cold_resumed: ``True`` when resuming an existing Omnigent
        session whose terminal had already exited, so a *fresh*
        ``kimi`` TUI was launched with none of the prior turns.
        Kimi records no resumable chat id, so this is genuinely a new
        chat - distinct from a brand-new session (``resolved_session_id
        is None``) and from a live reattach. Drives the honest
        cold-resume stderr hint. Note: kimi deliberately treats
        ``cold_resumed`` and ``reattached`` as mutually exclusive (the
        cold-resume path leaves ``reattached`` at its ``False`` default)
        - unlike ``claude_native`` which models them independently. This
        is safe because kimi never reads ``reattached`` for teardown
        ownership; do not "fix" the apparent inconsistency.
    """

    session_id: str
    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None
    reattached: bool
    cold_resumed: bool = False


def _configured_kimi_command(env: Mapping[str, str]) -> str:
    """Return the configured kimi executable name/path from *env*."""
    value = env.get(_KIMI_PATH_ENV, "").strip()
    return value or _DEFAULT_KIMI_COMMAND


def resolve_kimi_executable(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """
    Resolve the native Kimi (``kimi``) executable.

    :param env: Environment mapping to inspect. Defaults to ``os.environ``.
    :param which: Resolver hook for tests; defaults to ``shutil.which``.
    :returns: Absolute executable path.
    :raises click.ClickException: If no kimi CLI is available.
    """
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    command = _configured_kimi_command(env)
    resolved = which(command)
    if resolved is None:
        raise click.ClickException(
            "Native Kimi requires the 'kimi' CLI on PATH. Install it with: "
            "curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash, then "
            f"run 'kimi login'. You can also set {_KIMI_PATH_ENV}=/path/to/kimi."
        )
    return resolved


def build_kimi_launch(
    kimi_args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> NativeKimiLaunch:
    """Build the argv for a native Kimi process."""
    executable = resolve_kimi_executable(env=env, which=which)
    return NativeKimiLaunch(executable=executable, argv=[executable, *kimi_args])


def run_kimi_native(
    *,
    server: str | None,
    session_id: str | None,
    kimi_args: tuple[str, ...],
    resume_picker: bool = False,
    auto_open_conversation: bool = False,
) -> None:
    """
    Launch the Kimi TUI in an Omnigent terminal.

    :param server: Resolved Omnigent server URL.
    :param session_id: Optional existing Omnigent conversation id.
    :param kimi_args: Raw kimi CLI args to persist for the runner-owned TUI.
    :param resume_picker: ``True`` runs the kimi-native picker.
    :param auto_open_conversation: When ``True``, open the browser
        conversation URL after launch.
    :returns: None after the terminal attach session ends.
    """
    _preflight_local_tools()
    if server is None:
        raise click.ClickException(
            "Kimi requires a resolved Omnigent server URL. The CLI should call "
            "_ensure_backend before run_kimi_native."
        )
    with TemporaryDirectory(prefix="omnigent-kimi-native-") as tmpdir:
        spec_path = _materialize_kimi_agent_spec(Path(tmpdir))
        _run_with_remote_server(
            server.rstrip("/"),
            spec_path,
            session_id=session_id,
            resume_picker=resume_picker,
            kimi_args=kimi_args,
            auto_open_conversation=auto_open_conversation,
        )


def _materialize_kimi_agent_spec(tmpdir: Path) -> Path:
    """
    Write the terminal-first agent spec used by ``omnigent kimi``.

    :param tmpdir: Temporary directory for the generated YAML file.
    :returns: Path to the generated YAML spec.
    """
    yaml_path = tmpdir / "kimi-native-ui.yaml"
    raw: dict[str, Any] = {
        "name": _AGENT_NAME,
        "prompt": (
            "Kimi is running in the session terminal. The user drives the "
            "kimi TUI directly."
        ),
        "executor": {"harness": "kimi-native"},
        "spawn": True,
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
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


def _run_with_remote_server(
    base_url: str,
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    kimi_args: tuple[str, ...],
    auto_open_conversation: bool = False,
) -> None:
    """
    Launch Kimi on an Omnigent server via a daemon-spawned runner.

    :param base_url: Omnigent server base URL.
    :param spec_path: Generated Kimi wrapper agent spec.
    :param session_id: Optional existing Omnigent session id.
    :param resume_picker: When ``True``, run the kimi-native picker.
    :param kimi_args: Raw kimi CLI args.
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
            with runner_startup_progress(initial_message="Preparing Kimi...") as progress:
                _update_startup_progress(progress, "Connecting to local daemon...")
                _ensure_host_daemon(base_url)
                host_id = load_or_create_host_identity().host_id
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_kimi_terminal_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    session_id=resolved_session_id,
                    session_bundle=bundle,
                    kimi_args=kimi_args,
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
                echo_native_cold_resume_hint(agent_label="Kimi")
            await _attach_terminal_resource(prepared)
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="kimi",
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


async def _prepare_kimi_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    kimi_args: tuple[str, ...],
    host_id: str,
    workspace: str,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedKimiTerminal:
    """
    Create or resume a kimi-native session through a daemon runner.

    :returns: Prepared terminal details for attaching.
    """
    persist_args = list(kimi_args)
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        # Resuming an existing session can either reattach to a live
        # terminal (prior chat intact) or, if that terminal has exited,
        # cold-start a fresh TUI. We only know which after probing for a
        # running terminal below, so default both flags off here.
        reattached = False
        cold_resumed = False
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Kimi session requires a session bundle.")
            _update_startup_progress(startup_progress, "Creating Kimi session...")
            session_id = await _create_kimi_session(
                client,
                session_bundle,
                terminal_launch_args=persist_args or None,
            )
        else:
            _update_startup_progress(startup_progress, "Loading Kimi session...")
            payload = await _fetch_kimi_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a kimi-native session."
                )
            existing_terminal = await _find_running_kimi_terminal(client, session_id)
            if existing_terminal is not None:
                if persist_args:
                    click.echo(
                        "Ignoring Kimi launch args for an already-running terminal; "
                        "restart the session terminal to apply them.",
                        err=True,
                    )
                _update_startup_progress(startup_progress, "Kimi terminal ready.")
                return PreparedKimiTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    reattached=True,
                )
            # Session exists but its terminal has exited. Kimi records no
            # resumable chat id, so the launch below starts a fresh TUI with
            # no prior turns. Flag it so the caller can say so honestly.
            # Mutually exclusive with the reattach path above: we leave
            # reattached at False here (unlike claude_native, which treats
            # cold_resumed/reattached as independent). Safe because kimi
            # never uses reattached for teardown ownership.
            cold_resumed = True
            if persist_args:
                _update_startup_progress(startup_progress, "Updating Kimi session...")
                resp = await client.patch(
                    f"/v1/sessions/{url_component(session_id)}",
                    json={"terminal_launch_args": persist_args},
                )
                if resp.status_code >= 400:
                    raise click.ClickException(
                        f"Kimi session launch config update failed "
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
        _update_startup_progress(startup_progress, "Starting Kimi terminal...")
        await _ensure_kimi_terminal_on_runner(client, session_id)
        terminal = await _wait_for_kimi_terminal_ready(
            client,
            session_id,
            timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S,
        )
        _update_startup_progress(startup_progress, "Kimi terminal ready.")
    return PreparedKimiTerminal(
        session_id=session_id,
        terminal_id=terminal.terminal_id,
        tmux_socket=terminal.tmux_socket,
        tmux_target=terminal.tmux_target,
        reattached=reattached,
        cold_resumed=cold_resumed,
    )


async def _create_kimi_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """Create a bundled terminal-first kimi-native session."""
    metadata: dict[str, Any] = {"labels": dict(_SESSION_LABELS)}
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("kimi-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Kimi session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    new_session_id = body.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise click.ClickException("Kimi session creation response did not include session_id.")
    return new_session_id


async def _fetch_kimi_session(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
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


async def _ensure_kimi_terminal_on_runner(client: httpx.AsyncClient, session_id: str) -> None:
    """Ask the bound runner to ensure the Kimi terminal exists."""
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
            f"Kimi terminal ensure failed ({resp.status_code}): {error_text(resp)}"
        )


async def _wait_for_kimi_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> LaunchedKimiTerminal:
    """Wait until the runner exposes the Kimi terminal resource."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        terminal = await _find_running_kimi_terminal(client, session_id)
        if terminal is not None:
            return terminal
        await asyncio.sleep(0.2)
    raise click.ClickException(
        f"The runner did not create the Kimi terminal for {session_id!r} "
        f"within {timeout_s:.0f}s."
    )


async def _find_running_kimi_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> LaunchedKimiTerminal | None:
    """Return the existing running Kimi terminal id if present."""
    terminal_id = kimi_terminal_resource_id()
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
        raise click.ClickException(f"Failed to fetch Kimi terminal ({resp.status_code}): {text}")
    payload = resp.json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("running") is False:
        return None
    return _launched_kimi_terminal_from_payload(payload)


def _launched_kimi_terminal_from_payload(payload: object) -> LaunchedKimiTerminal:
    """Decode terminal launch metadata returned by the runner."""
    if not isinstance(payload, dict):
        raise click.ClickException("Kimi terminal launch returned non-object JSON.")
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException("Kimi terminal launch response did not include terminal id.")
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
    return LaunchedKimiTerminal(
        terminal_id=terminal_id,
        tmux_socket=tmux_socket,
        tmux_target=tmux_target,
    )


async def _attach_terminal_resource(prepared: PreparedKimiTerminal) -> None:
    """Attach the current terminal to the prepared Kimi terminal resource."""
    direct_tmux_error = _direct_tmux_unavailable_reason(prepared)
    if direct_tmux_error is not None:
        raise click.ClickException(
            f"Runner-owned Kimi terminal requires direct tmux attach, but {direct_tmux_error}"
        )
    if prepared.tmux_socket is None or prepared.tmux_target is None:
        raise click.ClickException("Kimi tmux attach metadata was incomplete.")
    await _attach_direct_tmux(prepared.tmux_socket, prepared.tmux_target)


async def _attach_direct_tmux(socket_path: Path, tmux_target: str) -> None:
    """Attach the current terminal directly to the runner-owned tmux pane."""
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


def _direct_tmux_unavailable_reason(prepared: PreparedKimiTerminal) -> str | None:
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
    """Translate resume inputs into a concrete kimi-native session id."""
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
    """Show one concise Kimi startup milestone when a renderer is active."""
    if startup_progress is not None:
        startup_progress.update(message)


def _preflight_local_tools() -> None:
    """Verify local executables required by the native Kimi wrapper."""
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native Kimi wrapper "
            "attaches to the runner-owned Kimi tmux terminal."
        )


def kimi_terminal_resource_id() -> str:
    """Return the deterministic terminal resource id for Kimi."""
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)
