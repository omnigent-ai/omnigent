"""Native Qwen Code wrapper for the Omnigent CLI."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import click
import httpx
import yaml

from omnigent._native_resume_hint import echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
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

_DEFAULT_QWEN_COMMAND = "qwen"
_QWEN_PATH_ENV = "OMNIGENT_QWEN_PATH"
_LEGACY_HARNESS_QWEN_PATH_ENV = "HARNESS_QWEN_PATH"
_AGENT_NAME = "qwen-native-ui"
_TERMINAL_NAME = "qwen"
_TERMINAL_SESSION_KEY = "main"


@dataclass(frozen=True)
class NativeQwenLaunch:
    """Resolved native Qwen process launch."""

    executable: str
    argv: list[str]


def _configured_qwen_command(env: Mapping[str, str]) -> str:
    """Return the configured Qwen executable name/path from *env*."""
    for key in (_QWEN_PATH_ENV, _LEGACY_HARNESS_QWEN_PATH_ENV):
        value = env.get(key, "").strip()
        if value:
            return value
    return _DEFAULT_QWEN_COMMAND


def resolve_qwen_executable(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """
    Resolve the native Qwen executable.

    :param env: Environment mapping; reads ``OMNIGENT_QWEN_PATH`` or
        ``HARNESS_QWEN_PATH`` if present.
    :param which: Optional ``shutil.which`` replacement for testing.
    :returns: The resolved executable path/name.
    """
    if env is None:
        env = dict(os.environ)
    configured = _configured_qwen_command(env)
    if which is None:
        which = shutil.which
    found = which(configured)
    if found is not None:
        return found
    raise click.ClickException(
        f"Qwen Code CLI was not found on PATH. Install it with `npm install -g @qwen/qwen-code` "
        f"or set { _QWEN_PATH_ENV} to the full path."
    )


def resolve_qwen_launch(
    *,
    qwen_args: tuple[str, ...],
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> NativeQwenLaunch:
    """Build the argv for launching Qwen Code."""
    executable = resolve_qwen_executable(env=env, which=which)
    return NativeQwenLaunch(executable=executable, argv=[executable, *qwen_args])


def qwen_terminal_resource_id() -> str:
    """Return the deterministic terminal resource id for Qwen."""
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)


async def run_qwen_native(
    *,
    server: str,
    session_id: str | None,
    resume_picker: bool = False,
    qwen_args: tuple[str, ...] = (),
    auto_open_conversation: bool = False,
    startup_progress: RunnerStartupProgress | None = None,
) -> None:
    """Run a Qwen Code agent through the Omnigent runner.

    :param server: Resolved Omnigent server URL.
    :param session_id: Optional existing Omnigent conversation id.
    :param resume_picker: ``True`` runs the qwen-native picker.
    :param qwen_args: Raw Qwen CLI args to persist for the runner-owned TUI.
    :param auto_open_conversation: When ``True``, open the browser
        conversation URL after launch.
    :param startup_progress: Optional progress renderer for startup milestones.
    :returns: None after the terminal attach session ends.
    """
    _preflight_local_tools()
    if server is None:
        raise click.ClickException(
            "Qwen requires a resolved Omnigent server URL. The CLI should call "
            "_ensure_backend before run_qwen_native."
        )

    with TemporaryDirectory(prefix="omnigent-qwen-native-") as tmpdir:
        spec_path = _materialize_qwen_agent_spec(Path(tmpdir))
        await _run_with_remote_server(
            server.rstrip("/"),
            spec_path,
            session_id=session_id,
            resume_picker=resume_picker,
            qwen_args=qwen_args,
            auto_open_conversation=auto_open_conversation,
            startup_progress=startup_progress,
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
            "Qwen Code is running in the session terminal. Web UI messages are "
            "forwarded into that Qwen process through the native extension bridge."
        ),
        "executor": {"harness": "qwen"},
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


async def _run_with_remote_server(
    base_url: str,
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    qwen_args: tuple[str, ...],
    auto_open_conversation: bool,
    startup_progress: RunnerStartupProgress | None = None,
) -> None:
    """Launch an agent spec through the remote Omnigent runner."""
    headers = {"Content-Type": "application/yaml"}

    with spec_path.open("rb") as fh:
        content = fh.read()

    async with httpx.AsyncClient(timeout=90) as client:
        # Step 1: Ensure host and runner are online
        _update_startup_progress(startup_progress, "ensuring backend")
        await launch_or_reuse_daemon_runner(base_url, client)

        _update_startup_progress(startup_progress, "waiting for host")
        await wait_for_host_online(
            base_url,
            client,
            timeout_s=_DAEMON_HOST_ONLINE_TIMEOUT_S,
            progress=startup_progress,
        )

        _update_startup_progress(startup_progress, "waiting for runner")
        await wait_for_runner_online(
            base_url,
            client,
            timeout_s=_DAEMON_RUNNER_ONLINE_TIMEOUT_S,
            progress=startup_progress,
        )

        # Step 2: Create agent
        _update_startup_progress(startup_progress, "uploading agent spec")
        resp = await client.post(
            f"{base_url}/v1/agents",
            content=content,
            headers=headers,
        )
        resp.raise_for_status()
        agent_id = resp.json()["id"]

        # Step 3: Create session
        _update_startup_progress(startup_progress, "creating session")
        create_resp = await client.post(
            f"{base_url}/v1/agents/{agent_id}/sessions",
            json={"resume": session_id} if session_id else {"resume_latest": True},
        )
        create_resp.raise_for_status()
        session_data = create_resp.json()
        conversation_id = session_data["id"]
        runner_id = session_data.get("runnerId")

        # Step 4: Bind runner to host
        if runner_id:
            _update_startup_progress(startup_progress, "binding runner")
            await _bind_session_runner(
                base_url,
                client,
                conversation_id=conversation_id,
                runner_id=runner_id,
            )

        # Step 5: Launch native terminal
        _update_startup_progress(startup_progress, "launching terminal")
        launch_resp = await client.post(
            f"{base_url}/v1/sessions/{conversation_id}/terminals",
            json={
                "name": _TERMINAL_NAME,
                "sessionKey": _TERMINAL_SESSION_KEY,
                "command": resolve_qwen_launch(qwen_args=qwen_args).argv[0],
                "args": resolve_qwen_launch(qwen_args=qwen_args).argv[1:],
                "cwd": ".",
            },
        )
        launch_resp.raise_for_status()
        terminal_data = launch_resp.json()
        terminal_id = terminal_data["id"]

        # Step 6: Construct conversation URL
        url_path = f"/conversation/{conversation_id}"
        if resume_picker:
            url_path += "?resume=picker"
        conv_url = f"{base_url}{url_path}"

        _update_startup_progress(startup_progress, "attaching terminal")
        try:
            await _attach_terminal_resource(
                base_url,
                client,
                conversation_id=conversation_id,
                terminal_id=terminal_id,
            )
        except click.ClickException as exc:
            echo_native_resume_hint(conv_url)
            raise

        if auto_open_conversation:
            open_conversation_link_if_enabled(conv_url)


async def _attach_terminal_resource(
    base_url: str,
    client: httpx.AsyncClient,
    *,
    conversation_id: str,
    terminal_id: str,
) -> None:
    """Attach to the runner-owned Qwen terminal resource."""
    # For now, use a simple polling approach similar to pi_native
    # Full tmux integration can be added later if needed
    from omnigent.terminals.ws_bridge import bridge_tmux_pty_to_websocket

    # TODO: Implement proper terminal attachment for qwen
    # This is a placeholder that will be fleshed out during implementation
    pass


def _preflight_local_tools() -> None:
    """Verify local executables required by the native Qwen wrapper."""
    if shutil.which("qwen") is None:
        raise click.ClickException(
            "Qwen Code CLI was not found on PATH. Install it with `npm install -g @qwen/qwen-code`"
        )


def _update_startup_progress(
    startup_progress: RunnerStartupProgress | None,
    message: str,
) -> None:
    """Show one concise Qwen startup milestone when a renderer is active."""
    if startup_progress is not None:
        startup_progress.update(message)
