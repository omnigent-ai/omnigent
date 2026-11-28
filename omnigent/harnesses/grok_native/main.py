"""``omnigent grok`` launch path for the grok-native harness.

Materializes the terminal-first built-in agent the server seeds (parallel to
kimi-native), then connects through a daemon runner, which owns the
``grok agent serve`` WebSocket server and its forwarder; this spec just binds
the session to it. The runner auto-creates the serve endpoint + TUI terminal
for the session and this process attaches the TTY to the runner-owned tmux
pane.

See docs/GROK_NATIVE_DESIGN.md for the transport.
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

import click
import httpx
import yaml

from omnigent._platform import resolve_cli_binary
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._wrapper_labels import GROK_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE
from omnigent._wrapper_labels import WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY
from omnigent.conversation_browser import conversation_url, open_conversation_link_if_enabled
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.host.daemon_launch import (
    error_text,
    launch_or_reuse_daemon_runner,
    open_daemon_client,
    wait_for_host_online,
    wait_for_runner_online,
)
from omnigent.native._native_resume_hint import (
    echo_native_cold_resume_hint,
    echo_native_resume_hint,
)
from omnigent.native.native_coding_agents import native_shell_terminal_spec
from omnigent.native.native_terminal import (
    DAEMON_HOST_ONLINE_TIMEOUT_S as _DAEMON_HOST_ONLINE_TIMEOUT_S,
)
from omnigent.native.native_terminal import (
    DAEMON_RUNNER_ONLINE_TIMEOUT_S as _DAEMON_RUNNER_ONLINE_TIMEOUT_S,
)
from omnigent.native.native_terminal import (
    DAEMON_TERMINAL_READY_TIMEOUT_S as _DAEMON_TERMINAL_READY_TIMEOUT_S,
)
from omnigent.native.native_terminal import bind_session_runner as _bind_session_runner
from omnigent.native.native_terminal import (
    normalize_extra_args as _normalize_extra_args,
)
from omnigent.native.native_terminal import url_component
from omnigent.util.json_types import JsonObject as _JsonObject

_DEFAULT_GROK_COMMAND = "grok"
_GROK_PATH_ENV = "OMNIGENT_GROK_PATH"
_AGENT_NAME = "grok-native-ui"
_TERMINAL_NAME = "grok"
_TERMINAL_SESSION_KEY = "main"
_SESSION_LABELS = {
    "omnigent.ui": "terminal",
    _WRAPPER_LABEL_KEY: _WRAPPER_LABEL_VALUE,
}


@dataclass(frozen=True)
class NativeGrokLaunch:
    """Resolved native Grok process launch."""

    executable: str
    argv: list[str]


@dataclass(frozen=True)
class LaunchedGrokTerminal:
    """Terminal resource returned by the Omnigent runner launch path."""

    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None


@dataclass(frozen=True)
class PreparedGrokTerminal:
    """Prepared native Grok terminal attachment details.

    :param reattached: ``True`` when an existing, still-running session
        terminal was reused (the live-reattach path: prior serve process
        and conversation are intact).
    :param cold_resumed: ``True`` when resuming an existing Omnigent
        session whose serve terminal had exited, so a new serve endpoint
        was launched and reloaded the persisted grok session where the
        vendor store has it.
    """

    session_id: str
    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None
    reattached: bool
    cold_resumed: bool = False


def _configured_grok_command(env: Mapping[str, str]) -> str:
    """Return the configured grok executable name/path from *env*."""
    value = env.get(_GROK_PATH_ENV, "").strip()
    return value or _DEFAULT_GROK_COMMAND


def resolve_grok_executable(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """
    Resolve the native Grok Build (``grok``) executable.

    :param env: Environment mapping to inspect. Defaults to ``os.environ``.
    :param which: Resolver hook for tests; defaults to ``shutil.which``.
    :returns: Absolute executable path.
    :raises click.ClickException: If no grok CLI is available.
    """
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    command = _configured_grok_command(env)
    resolved = resolve_cli_binary(command, which=which)
    if resolved is None:
        from omnigent.harnesses.grok_native.credentials import missing_grok_message

        raise click.ClickException(missing_grok_message())
    return resolved


def build_grok_launch(
    grok_args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> NativeGrokLaunch:
    """Build the argv for a native Grok process."""
    executable = resolve_grok_executable(env=env, which=which)
    return NativeGrokLaunch(executable=executable, argv=[executable, *grok_args])


def run_grok_native(
    *,
    server: str | None,
    session_id: str | None,
    extra_args: tuple[str, ...] | None = None,
    grok_args: tuple[str, ...] | None = None,
    resume_picker: bool = False,
    auto_open_conversation: bool = False,
    prompt: str | None = None,
) -> None:
    """
    Launch the Grok native session in an Omnigent terminal.

    :param server: Resolved Omnigent server URL.
    :param session_id: Optional existing Omnigent conversation id.
    :param grok_args: Raw grok CLI args to persist for the runner-owned launch.
    :param resume_picker: ``True`` runs the grok-native picker.
    :param auto_open_conversation: When ``True``, open the browser
        conversation URL after launch.
    :param prompt: Optional initial prompt posted into the new session; when
        set the launch skips the interactive TTY attach so headless callers
        (e.g. ``omni run --harness grok-native -p ...``) can drive a turn.
    :returns: None after the terminal attach session ends.
    """
    grok_args = _normalize_extra_args(
        extra_args=extra_args, legacy_args=grok_args, legacy_param="grok_args"
    )
    _preflight_local_tools()
    if server is None:
        raise click.ClickException(
            "Grok requires a resolved Omnigent server URL. The CLI should call "
            "_ensure_backend before run_grok_native."
        )
    with TemporaryDirectory(prefix="omnigent-grok-native-") as tmpdir:
        spec_path = _materialize_grok_agent_spec(Path(tmpdir))
        _run_with_remote_server(
            server.rstrip("/"),
            spec_path,
            session_id=session_id,
            resume_picker=resume_picker,
            grok_args=grok_args,
            auto_open_conversation=auto_open_conversation,
            prompt=prompt,
        )


def _materialize_grok_agent_spec(tmpdir: Path) -> Path:
    """
    Write the terminal-first agent spec used by ``omnigent grok``.

    :param tmpdir: Temporary directory for the generated YAML file.
    :returns: Path to the generated YAML spec.
    """
    yaml_path = tmpdir / "grok-native-ui.yaml"
    raw: _JsonObject = {
        "name": _AGENT_NAME,
        "prompt": (
            "Grok Build is running as a session-owned agent server. The user "
            "drives it from the session terminal; web-UI messages are "
            "forwarded into the same native Grok session."
        ),
        "executor": {"harness": "grok-native"},
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
    grok_args: tuple[str, ...],
    auto_open_conversation: bool = False,
    prompt: str | None = None,
) -> None:
    """
    Launch Grok on an Omnigent server via a daemon-spawned runner.

    :param base_url: Omnigent server base URL.
    :param spec_path: Generated Grok wrapper agent spec.
    :param session_id: Optional existing Omnigent session id.
    :param resume_picker: When ``True``, run the grok-native picker.
    :param grok_args: Raw grok CLI args.
    :param auto_open_conversation: Whether to open the web conversation URL.
    """
    from omnigent.chat import _bundle_agent, _remote_headers
    from omnigent.cli import _ensure_host_daemon
    from omnigent.host.identity import load_or_create_host_identity

    headers = _remote_headers(server_url=base_url, host_id=None)
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
            with runner_startup_progress(initial_message="Preparing Grok...") as progress:
                _update_startup_progress(progress, "Connecting to local daemon...")
                _ensure_host_daemon(base_url)
                host_id = load_or_create_host_identity().host_id
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_grok_terminal_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    session_id=resolved_session_id,
                    session_bundle=bundle,
                    grok_args=grok_args,
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
                echo_native_cold_resume_hint(agent_label="Grok Build")
            if prompt:
                from omnigent.native.native_terminal import post_initial_prompt

                await post_initial_prompt(
                    base_url=base_url,
                    headers=headers,
                    session_id=prepared.session_id,
                    prompt=prompt,
                    auth=None,
                    harness_name="Grok",
                )
                click.echo("Prompt submitted; watch the session in the web UI.", err=True)
                return
            await _attach_terminal_resource(prepared)
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="grok",
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


async def _prepare_grok_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    grok_args: tuple[str, ...],
    host_id: str,
    workspace: str,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedGrokTerminal:
    """
    Create or resume a grok-native session through a daemon runner.

    :returns: Prepared terminal details for attaching.
    """
    persist_args = list(grok_args)
    timeout = httpx.Timeout(30.0, read=120.0)
    async with open_daemon_client(base_url, headers, host_id, timeout=timeout) as client:
        # Resuming an existing session can either reattach to a live
        # serve terminal (prior session intact) or, if that terminal has
        # exited, cold-start a fresh serve endpoint that reloads the
        # persisted grok session. We only know which after probing for a
        # running terminal below, so default both flags off here.
        reattached = False
        cold_resumed = False
        fresh_session = session_id is None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Grok session requires a session bundle.")
            _update_startup_progress(startup_progress, "Creating Grok session...")
            session_id, _ = await asyncio.gather(
                _create_grok_session(
                    client,
                    session_bundle,
                    terminal_launch_args=persist_args or None,
                ),
                wait_for_host_online(client, host_id, timeout_s=_DAEMON_HOST_ONLINE_TIMEOUT_S),
            )
        else:
            _update_startup_progress(startup_progress, "Loading Grok session...")
            payload = await _fetch_grok_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a grok-native session."
                )
            existing_terminal = await _find_running_grok_terminal(client, session_id)
            if existing_terminal is not None:
                if persist_args:
                    click.echo(
                        "Ignoring Grok launch args for an already-running terminal; "
                        "restart the session terminal to apply them.",
                        err=True,
                    )
                _update_startup_progress(startup_progress, "Grok terminal ready.")
                return PreparedGrokTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    reattached=True,
                )
            cold_resumed = True
            if persist_args:
                _update_startup_progress(startup_progress, "Updating Grok session...")
                resp = await client.patch(
                    f"/v1/sessions/{url_component(session_id)}",
                    json={"terminal_launch_args": persist_args},
                )
                if resp.status_code >= 400:
                    raise click.ClickException(
                        f"Grok session launch config update failed "
                        f"({resp.status_code}): {error_text(resp)}"
                    )

        if not fresh_session:
            await wait_for_host_online(client, host_id, timeout_s=_DAEMON_HOST_ONLINE_TIMEOUT_S)
        _update_startup_progress(startup_progress, "Starting runner...")
        runner_id = await launch_or_reuse_daemon_runner(
            client,
            host_id=host_id,
            session_id=session_id,
            workspace=workspace,
            fresh=fresh_session,
        )
        _update_startup_progress(startup_progress, "Waiting for runner...")
        await wait_for_runner_online(client, runner_id, timeout_s=_DAEMON_RUNNER_ONLINE_TIMEOUT_S)
        await _bind_session_runner(client, session_id, runner_id)
        _update_startup_progress(startup_progress, "Starting Grok terminal...")
        await _ensure_grok_terminal_on_runner(client, session_id)
        terminal = await _wait_for_grok_terminal_ready(
            client,
            session_id,
            timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S,
        )
        _update_startup_progress(startup_progress, "Grok terminal ready.")
    return PreparedGrokTerminal(
        session_id=session_id,
        terminal_id=terminal.terminal_id,
        tmux_socket=terminal.tmux_socket,
        tmux_target=terminal.tmux_target,
        reattached=reattached,
        cold_resumed=cold_resumed,
    )


async def _create_grok_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """Create a bundled terminal-first grok-native session."""
    metadata: _JsonObject = {"labels": dict(_SESSION_LABELS)}
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("grok-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Grok session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    new_session_id = body.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise click.ClickException("Grok session creation response did not include session_id.")
    return new_session_id


async def _fetch_grok_session(client: httpx.AsyncClient, session_id: str) -> _JsonObject:
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


async def _ensure_grok_terminal_on_runner(client: httpx.AsyncClient, session_id: str) -> None:
    """Ask the bound runner to ensure the Grok terminal exists."""
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
            f"Grok terminal ensure failed ({resp.status_code}): {error_text(resp)}"
        )


async def _wait_for_grok_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> LaunchedGrokTerminal:
    """Wait until the runner exposes the Grok terminal resource."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        terminal = await _find_running_grok_terminal(client, session_id)
        if terminal is not None:
            return terminal
        await asyncio.sleep(0.2)
    raise click.ClickException(
        f"The runner did not create the Grok terminal for {session_id!r} within {timeout_s:.0f}s."
    )


async def _find_running_grok_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> LaunchedGrokTerminal | None:
    """Return the existing running Grok terminal id if present."""
    terminal_id = grok_terminal_resource_id()
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
        raise click.ClickException(f"Failed to fetch Grok terminal ({resp.status_code}): {text}")
    payload = resp.json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("running") is False:
        return None
    return _launched_grok_terminal_from_payload(payload)


def _launched_grok_terminal_from_payload(payload: object) -> LaunchedGrokTerminal:
    """Decode terminal launch metadata returned by the runner."""
    if not isinstance(payload, dict):
        raise click.ClickException("Grok terminal launch returned non-object JSON.")
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException("Grok terminal launch response did not include terminal id.")
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
    return LaunchedGrokTerminal(
        terminal_id=terminal_id,
        tmux_socket=tmux_socket,
        tmux_target=tmux_target,
    )


async def _attach_terminal_resource(prepared: PreparedGrokTerminal) -> None:
    """Attach the current terminal to the prepared Grok terminal resource."""
    direct_tmux_error = _direct_tmux_unavailable_reason(prepared)
    if direct_tmux_error is not None:
        raise click.ClickException(
            f"Runner-owned Grok terminal requires direct tmux attach, but {direct_tmux_error}"
        )
    if prepared.tmux_socket is None or prepared.tmux_target is None:
        raise click.ClickException("Grok tmux attach metadata was incomplete.")
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


def _direct_tmux_unavailable_reason(prepared: PreparedGrokTerminal) -> str | None:
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
    """Translate resume inputs into a concrete grok-native session id."""
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
    """Show one concise Grok startup milestone when a renderer is active."""
    if startup_progress is not None:
        startup_progress.update(message)


def _preflight_local_tools() -> None:
    """Verify local executables required by the native Grok wrapper."""
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native Grok wrapper "
            "attaches to the runner-owned Grok tmux terminal."
        )


def grok_terminal_resource_id() -> str:
    """Return the deterministic terminal resource id for Grok."""
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)


__all__ = [
    "GROK_NATIVE_HARNESS_NAME",
    "LaunchedGrokTerminal",
    "NativeGrokLaunch",
    "PreparedGrokTerminal",
    "build_grok_launch",
    "grok_terminal_resource_id",
    "resolve_grok_executable",
    "run_grok_native",
]

GROK_NATIVE_HARNESS_NAME = "grok-native"
