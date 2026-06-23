"""Native Antigravity (agy) TUI wrapper for the Omnigent CLI.

``omnigent antigravity`` treats the Antigravity ``agy`` CLI as a
terminal-first program, mirroring ``omnigent codex`` / ``omnigent claude``.
It creates or binds an Omnigent session, launches ``agy`` in a runner-owned
tmux terminal resource, then attaches the local TTY (directly to the
runner's tmux when same-machine, else over the WebSocket PTY bridge).

Differences from the Codex / Claude wrappers (Phase 1 scope):

* **No separate app-server.** agy self-hosts its local control surface, so
  there is no app-server process to start, no ``--remote`` transport, and no
  thread-init handshake.
* **RPC mirroring (read path) and RPC web-turn delivery (write path).** agy's
  conversation mirrors into the Omnigent chat view via the RPC read driver
  (:mod:`omnigent.antigravity_native_reader`), which polls/streams agy's
  connect-RPC trajectory steps (the read path) — it superseded the transcript-tail
  forwarder, retired in the Task 12 cutover. Web-UI turns are delivered into the
  native agy conversation (the write path) by the native executor
  (:mod:`omnigent.inner.antigravity_native_executor`) over the connect-RPC
  ``SendUserCascadeMessage`` method, which agy records as a real ``USER_INPUT``
  turn — NOT ``SendAgentMessage`` (recorded as a ``SYSTEM_MESSAGE``) and no longer
  by typing into the TUI over tmux send-keys (see the executor module).
* **Per-session identity is minted at cold-start, not assigned at launch.** agy
  mints its own UUID conversation and ignores the launcher's
  ``ANTIGRAVITY_CONVERSATION_ID`` (verified empirically). A fresh launch seeds an
  ``agy_conv_*`` placeholder; the cold-start (:func:`_cold_start_agy_conversation`,
  the runner's equivalent on the web path) then ``StartCascade``s a real id over
  connect-RPC, writes it to bridge state (which the RPC reader binds), and PATCHes
  it onto the session as ``external_session_id``. A resume reads that real id back
  and passes ``--conversation <id>`` to continue agy's actual conversation (see
  :func:`omnigent.antigravity_native_launch.build_agy_launch`).
* **Workspace = the agy terminal cwd.** agy runs tools in its process working
  directory, so the terminal cwd is pinned to the session working dir; no
  ``--add-dir`` is needed.
* **Auth is inherited from ``~/.gemini``** — no credential seeding.

The runner OWNS the agy terminal: binding a runner triggers its idempotent
auto-create of the antigravity terminal (``runner/app.py``
``_auto_create_antigravity_terminal``) for every antigravity-native session. So
the CLI reattaches to that runner-owned terminal after binding rather than
launching its own — a double launch 500s ("already observed as required") and
clobbers the runner's bridge state (breaking web-turn injection). A CLI-side
launch (:func:`_launch_and_record`) remains only as a defensive fallback for the
unexpected case where the runner produces no terminal in the wait window.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import click
import httpx
import yaml

from omnigent._native_resume_hint import echo_native_resume_hint
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._wrapper_labels import (
    ANTIGRAVITY_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import (
    UI_MODE_LABEL_KEY as _UI_MODE_LABEL_KEY,
)
from omnigent._wrapper_labels import (
    UI_MODE_TERMINAL_VALUE as _UI_MODE_TERMINAL_VALUE,
)
from omnigent._wrapper_labels import (
    WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY,
)
from omnigent.antigravity_native_bridge import (
    AGY_PLACEHOLDER_CONVERSATION_PREFIX,
    ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
    AntigravityNativeBridgeState,
    bridge_dir_for_bridge_id,
    clear_bridge_state,
    ensure_agy_onboarding_complete,
    is_placeholder_conversation_id,
    prepare_bridge_dir,
    read_bridge_state,
    update_conversation_id,
    write_bridge_state,
    write_tmux_target,
)
from omnigent.antigravity_native_launch import (
    agy_binary_path,
    build_agy_launch,
    resolve_native_antigravity_launch,
)
from omnigent.antigravity_native_reader import run_reader_with_bridge
from omnigent.antigravity_native_rpc import (
    AntigravityRpcError,
    resolve_cold_start_agy_rpc_port,
    start_cascade,
)
from omnigent.claude_native import (
    _attach_with_reconnect,
    _AttachOutcome,
    attach_local_terminal,
)
from omnigent.claude_native_bridge import url_component
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
    bind_session_runner as _bind_session_runner,
)
from omnigent.native_terminal import (
    terminal_attach_url as _attach_url,
)

_logger = logging.getLogger(__name__)

_AGENT_NAME = "antigravity-native-ui"
_TERMINAL_NAME = "antigravity"
_TERMINAL_SESSION_KEY = "main"
_ANTIGRAVITY_TERMINAL_SCROLLBACK_LINES = 100_000
_SESSION_LABELS = {
    _UI_MODE_LABEL_KEY: _UI_MODE_TERMINAL_VALUE,
    _WRAPPER_LABEL_KEY: _WRAPPER_LABEL_VALUE,
}


@dataclass(frozen=True)
class LaunchedAntigravityTerminal:
    """
    Terminal resource returned by the Omnigent runner launch path.

    :param terminal_id: Terminal resource id, e.g.
        ``"terminal_antigravity_main"``.
    :param tmux_socket: Local tmux socket path when the runner exposed
        one, e.g. ``"/tmp/omnigent-terminal-x/tmux.sock"``.
    :param tmux_target: Tmux target when exposed by the runner,
        e.g. ``"main"``.
    """

    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None


@dataclass(frozen=True)
class PreparedAntigravityTerminal:
    """
    Prepared native Antigravity terminal attachment details.

    :param session_id: Omnigent session/conversation id.
    :param terminal_id: Terminal resource id to attach.
    :param bridge_dir: Native Antigravity bridge directory shared with the
        ``antigravity-native`` harness.
    :param tmux_socket: Local tmux socket path when the runner exposed
        one and it is reachable from this CLI process.
    :param tmux_target: Tmux target for direct local attaches, e.g.
        ``"main"``.
    :param reattached: ``True`` when an existing terminal was reused.
        Drives teardown ownership — a reattached invocation must not
        close the terminal on exit.
    """

    session_id: str
    terminal_id: str
    bridge_dir: Path
    tmux_socket: Path | None
    tmux_target: str | None
    reattached: bool


def run_antigravity_native(
    *,
    server: str | None,
    session_id: str | None,
    antigravity_args: tuple[str, ...] = (),
    resume_picker: bool = False,
    command: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Launch the Antigravity (agy) TUI in an Omnigent terminal and attach.

    :param server: Resolved Omnigent server URL, e.g.
        ``"http://127.0.0.1:8123"``. ``None`` starts a local Omnigent
        server using the existing chat-server machinery.
    :param session_id: Optional existing Omnigent conversation id to
        resume, e.g. ``"conv_abc123"``. ``None`` creates a new bundled
        session.
    :param antigravity_args: Raw pass-through args appended to the ``agy``
        command line after the generated flags.
    :param resume_picker: ``True`` runs the antigravity-native picker once
        the server is reachable; ``False`` keeps the explicit
        ``session_id``-or-fresh behavior.
    :param command: Path to the ``agy`` executable. ``None`` resolves it
        via :func:`agy_binary_path`. Kept off the public CLI surface so
        tests can supply a fake executable.
    :param model: Optional model label passed to agy via ``--model``,
        e.g. ``"gemini-2.5-pro"``. ``None`` lets agy use its default.
    :param permission_mode: Optional Omnigent permission mode, e.g.
        ``"bypassPermissions"``. ``"bypassPermissions"`` maps to agy's
        ``--dangerously-skip-permissions`` (its only pre-emptive control);
        any other value (or ``None``) leaves agy's default ``request-review``
        prompt in place for the attended user — unless the launch is headless,
        in which case the prompt is auto-bypassed so an unattended turn does not
        hang (see
        :func:`omnigent.antigravity_native_launch.should_skip_permissions`).
    :param auto_open_conversation: When ``True``, open the browser
        conversation URL after the session is prepared.
    :returns: None after the terminal attach session ends.
    :raises click.ClickException: If setup, launch, or attach fails.
    """
    resolved_command = (command or agy_binary_path()).strip()
    if not resolved_command:
        raise click.ClickException("Antigravity command must not be empty.")
    _preflight_local_tools()
    # Resolve auth/model config once up front so a missing credential warns
    # before any server work. agy is OAuth-only (subscription), inherited
    # from ~/.gemini — nothing is seeded.
    launch = resolve_native_antigravity_launch(model=model)
    # Detect headless ONCE here (a controlling TTY on stdin+stdout means an
    # interactive client will attach to drive agy's request-review prompt; a
    # non-TTY launch must auto-bypass or the unattended turn hangs forever).
    headless = _launch_is_headless()
    with TemporaryDirectory(prefix="omnigent-antigravity-native-") as tmpdir:
        spec_path = _materialize_antigravity_agent_spec(Path(tmpdir))
        if server is None:
            _run_with_local_server(
                spec_path,
                session_id=session_id,
                resume_picker=resume_picker,
                antigravity_args=antigravity_args,
                command=resolved_command,
                model=launch.model,
                permission_mode=permission_mode,
                headless=headless,
                auto_open_conversation=auto_open_conversation,
            )
        else:
            _run_with_remote_server(
                server.rstrip("/"),
                spec_path,
                session_id=session_id,
                resume_picker=resume_picker,
                antigravity_args=antigravity_args,
                command=resolved_command,
                model=launch.model,
                permission_mode=permission_mode,
                headless=headless,
                auto_open_conversation=auto_open_conversation,
            )


def _materialize_antigravity_agent_spec(tmpdir: Path) -> Path:
    """
    Write the terminal-first agent spec used by ``omnigent antigravity``.

    :param tmpdir: Temporary directory for the generated YAML file.
    :returns: Path to the generated YAML spec.
    """
    yaml_path = tmpdir / "antigravity-native-ui.yaml"
    raw: dict[str, object] = {
        "name": _AGENT_NAME,
        "prompt": (
            "Antigravity (agy) is running in the session terminal. Web UI "
            "turns are forwarded into the native agy conversation."
        ),
        "executor": {"harness": "antigravity-native"},
        # Opt the native session into the child-session spawn writes so the
        # wrapped agy can author agent configs and launch them as sub-agent
        # sessions. The relay derives its advertised tool set from this spec.
        "spawn": True,
        # Without an ``os_env`` block the runner's filesystem APIs 404 (see
        # ``_require_os_env`` in ``omnigent/runner/app.py``). agy already
        # operates on the user's workspace with full filesystem access, so
        # caller-process / no-sandbox matches reality and enables the web
        # UI's files panel.
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
        # Declare a default shell terminal so the relay advertises the
        # ``sys_terminal_*`` family to the wrapped agy (the relay's gate is
        # a non-empty ``terminals:`` block on this spec).
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


def _run_with_local_server(
    spec_path: Path,
    *,
    session_id: str | None,
    resume_picker: bool,
    antigravity_args: tuple[str, ...],
    command: str,
    model: str | None,
    permission_mode: str | None = None,
    headless: bool = False,
    auto_open_conversation: bool = False,
) -> None:
    """
    Start a local Omnigent server, launch agy, and attach to it.

    :param spec_path: Generated Antigravity wrapper agent spec.
    :param session_id: Optional existing Omnigent session id.
    :param resume_picker: When ``True`` and ``session_id is None``, run the
        picker.
    :param antigravity_args: Raw pass-through agy args.
    :param command: agy executable to run.
    :param model: Optional agy model id.
    :param permission_mode: Optional Omnigent permission mode (e.g.
        ``"bypassPermissions"``) threaded into the agy argv assembly.
    :param headless: ``True`` when no interactive client will attach (forces
        the agy permission-bypass flag so an unattended turn does not hang).
    :param auto_open_conversation: When ``True``, open the browser
        conversation URL after the session is prepared.
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
            # Picker cancelled — exit before creating a session the user declined.
            return

        async def _drive() -> None:
            """
            Prepare agy and attach in a single event loop.

            :returns: None.
            """
            with runner_startup_progress(initial_message="Preparing Antigravity...") as progress:
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_antigravity_terminal(
                    base_url=base_url,
                    headers={},
                    session_id=resolved_session_id,
                    runner_id=server_handle.runner_id,
                    session_bundle=bundle,
                    antigravity_args=antigravity_args,
                    command=command,
                    model=model,
                    permission_mode=permission_mode,
                    headless=headless,
                    startup_progress=progress,
                )
            click.echo(f"Web UI: {conversation_url(base_url, prepared.session_id)}", err=True)
            open_conversation_link_if_enabled(
                base_url=base_url,
                conversation_id=prepared.session_id,
                enabled=auto_open_conversation,
                warn=lambda message: click.echo(message, err=True),
            )
            await _attach_terminal(
                base_url=base_url,
                headers={},
                prepared=prepared,
                recover=None,
            )
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="antigravity",
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
    antigravity_args: tuple[str, ...],
    command: str,
    model: str | None,
    permission_mode: str | None = None,
    headless: bool = False,
    auto_open_conversation: bool = False,
) -> None:
    """
    Launch agy on a remote Omnigent server via a daemon-spawned runner.

    The CLI binds a daemon runner to the session, then launches the agy
    terminal itself (the runner has no agy auto-create branch). Attach
    prefers the runner's tmux when it is local, else the WebSocket PTY
    bridge.

    :param base_url: Remote Omnigent server base URL, e.g.
        ``"https://example.databricks.com"``.
    :param spec_path: Generated Antigravity wrapper agent spec.
    :param session_id: Optional existing Omnigent session id.
    :param resume_picker: When ``True`` and ``session_id is None``, run the
        picker.
    :param antigravity_args: Raw pass-through agy args.
    :param command: agy executable to run.
    :param model: Optional agy model id.
    :param permission_mode: Optional Omnigent permission mode (e.g.
        ``"bypassPermissions"``) threaded into the agy argv assembly.
    :param headless: ``True`` when no interactive client will attach (forces
        the agy permission-bypass flag so an unattended turn does not hang).
    :param auto_open_conversation: When ``True``, open the browser
        conversation URL after the session is prepared.
    :returns: None.
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
            """
            Prepare agy and attach in a single event loop.

            :returns: None.
            """
            with runner_startup_progress(initial_message="Preparing Antigravity...") as progress:
                progress.update("Connecting to local daemon...")
                _ensure_host_daemon(base_url)
                host_id = load_or_create_host_identity().host_id
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_antigravity_terminal_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    session_id=resolved_session_id,
                    session_bundle=bundle,
                    antigravity_args=antigravity_args,
                    command=command,
                    model=model,
                    permission_mode=permission_mode,
                    headless=headless,
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

            async def _recover() -> None:
                """
                Refresh auth headers before a terminal reattach attempt.

                :returns: None.
                """
                new_headers = _remote_headers(server_url=base_url)
                headers.clear()
                headers.update(new_headers)

            await _attach_terminal(
                base_url=base_url,
                headers=headers,
                prepared=prepared,
                recover=_recover,
            )
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="antigravity",
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


async def _prepare_antigravity_terminal(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    runner_id: str | None,
    session_bundle: bytes | None,
    antigravity_args: tuple[str, ...],
    command: str,
    model: str | None,
    permission_mode: str | None = None,
    headless: bool = False,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedAntigravityTerminal:
    """
    Create/bind a session and launch its agy terminal resource.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers; ``{}`` for the local server.
    :param session_id: Optional existing session id.
    :param runner_id: Runner id to bind to the session, or ``None``.
    :param session_bundle: Gzipped agent bundle for new sessions. Required
        when *session_id* is ``None``.
    :param antigravity_args: Raw pass-through agy args.
    :param command: agy executable to run.
    :param model: Optional agy model id.
    :param permission_mode: Optional Omnigent permission mode threaded into the
        agy argv assembly.
    :param headless: ``True`` when no interactive client will attach (forces
        the agy permission-bypass flag).
    :param startup_progress: Optional user-visible progress renderer.
    :returns: Prepared terminal details.
    :raises click.ClickException: If any server operation fails.
    """
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        bridge_id: str
        conversation_id: str
        resume = False
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException(
                    "Creating an Antigravity session requires a session bundle."
                )
            _update_progress(startup_progress, "Creating Antigravity session...")
            bridge_id = _mint_agy_conversation_id()
            conversation_id = bridge_id
            session_id = await _create_antigravity_session(
                client,
                session_bundle,
                bridge_id=bridge_id,
            )
        else:
            _update_progress(startup_progress, "Loading Antigravity session...")
            payload = await _fetch_antigravity_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not an antigravity-native session."
                )
            bridge_id = str(labels.get(ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY) or session_id)
            existing = await _find_running_antigravity_terminal(client, session_id)
            if existing is not None:
                if antigravity_args or model is not None:
                    click.echo(
                        "Ignoring Antigravity launch args/model for an already-running "
                        "terminal; restart the session terminal to apply them.",
                        err=True,
                    )
                _update_progress(startup_progress, "Antigravity terminal ready.")
                return PreparedAntigravityTerminal(
                    session_id=session_id,
                    terminal_id=existing.terminal_id,
                    bridge_dir=bridge_dir_for_bridge_id(bridge_id),
                    tmux_socket=existing.tmux_socket,
                    tmux_target=existing.tmux_target,
                    reattached=True,
                )
            external = payload.get("external_session_id") if isinstance(payload, dict) else None
            conversation_id = external if isinstance(external, str) and external else bridge_id
            resume = isinstance(external, str) and bool(external)

        if runner_id is not None:
            await _bind_session_runner(client, session_id, runner_id)
            # The runner OWNS the antigravity terminal: binding triggers its
            # idempotent auto-create (``runner/app.py``
            # ``_auto_create_antigravity_terminal``), which fires for every
            # antigravity-native session — including on the local server, whose
            # CLI runner subprocess runs the same auto-create. Reattach to that
            # runner-owned terminal instead of launching our own: a redundant
            # ``_launch_and_record`` 500s ("terminal antigravity:main … already
            # observed as required") AND its ``clear_bridge_state`` wipes the bridge
            # state the runner wrote (every web turn then fails "Antigravity native
            # bridge state is missing"), and on ``reattached=False``
            # ``_attach_terminal`` starts a SECOND RPC reader → double-mirror. The
            # pre-bind existing-terminal check above can't catch this — the runner
            # only auto-creates AFTER the bind. A CLI launch stays
            # as a defensive fallback when the runner produced no terminal in the
            # window. Mirrors ``_prepare_antigravity_terminal_via_daemon``.
            autocreated = await _await_runner_antigravity_terminal(
                client, session_id, _RUNNER_TERMINAL_AUTOCREATE_TIMEOUT_S
            )
            if autocreated is not None:
                if antigravity_args or model is not None:
                    click.echo(
                        "Ignoring Antigravity launch args/model for the runner-owned "
                        "terminal; restart the session terminal to apply them.",
                        err=True,
                    )
                _update_progress(startup_progress, "Antigravity terminal ready.")
                return PreparedAntigravityTerminal(
                    session_id=session_id,
                    terminal_id=autocreated.terminal_id,
                    bridge_dir=bridge_dir_for_bridge_id(bridge_id),
                    tmux_socket=autocreated.tmux_socket,
                    tmux_target=autocreated.tmux_target,
                    reattached=True,
                )
        launched = await _launch_and_record(
            client,
            session_id=session_id,
            bridge_id=bridge_id,
            conversation_id=conversation_id,
            resume=resume,
            antigravity_args=antigravity_args,
            command=command,
            model=model,
            permission_mode=permission_mode,
            headless=headless,
            startup_progress=startup_progress,
        )
    return PreparedAntigravityTerminal(
        session_id=session_id,
        terminal_id=launched.terminal_id,
        bridge_dir=bridge_dir_for_bridge_id(bridge_id),
        tmux_socket=launched.tmux_socket,
        tmux_target=launched.tmux_target,
        reattached=False,
    )


# Binding a runner triggers the runner's idempotent auto-create of the
# antigravity terminal (``runner/app.py`` ``_auto_create_antigravity_terminal``),
# which OWNS the terminal for every antigravity-native session. After bind, wait
# this long for that runner-owned terminal to appear before falling back to a
# CLI-side launch — the fallback only fires when the runner produced none.
_RUNNER_TERMINAL_AUTOCREATE_TIMEOUT_S = 20.0
_RUNNER_TERMINAL_POLL_INTERVAL_S = 0.25


async def _await_runner_antigravity_terminal(
    client: httpx.AsyncClient,
    session_id: str,
    timeout_s: float,
) -> LaunchedAntigravityTerminal | None:
    """
    Poll for the runner-auto-created agy terminal after a runner bind.

    The runner auto-creates the antigravity terminal asynchronously once a runner
    binds, so a fresh/cold-resume CLI launch must wait for it and reattach instead
    of launching its own (a double-launch 500s AND ``_launch_and_record``'s
    ``clear_bridge_state`` would wipe the bridge state the runner wrote, breaking
    web-turn injection). Uses ``time.monotonic`` for the deadline.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param timeout_s: Max seconds to wait for the runner-owned terminal.
    :returns: The runner-owned terminal details, or ``None`` if none appeared
        within *timeout_s* (the caller then launches one itself).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        found = await _find_running_antigravity_terminal(client, session_id)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(_RUNNER_TERMINAL_POLL_INTERVAL_S)


async def _prepare_antigravity_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    antigravity_args: tuple[str, ...],
    command: str,
    model: str | None,
    permission_mode: str | None = None,
    headless: bool = False,
    host_id: str,
    workspace: str,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedAntigravityTerminal:
    """
    Create/resolve a session through a daemon runner and attach to agy.

    Binds a daemon-spawned runner to the session, then reattaches to the
    runner-owned agy terminal that the bind auto-creates (the runner owns the
    terminal for every antigravity-native session). Only launches the terminal
    itself (:func:`_launch_and_record`) as a fallback when the runner produced
    none within :data:`_RUNNER_TERMINAL_AUTOCREATE_TIMEOUT_S`.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers for Omnigent requests.
    :param session_id: Existing session id to resume, or ``None`` for a
        fresh session.
    :param session_bundle: Gzipped agent bundle. Required when
        *session_id* is ``None``.
    :param antigravity_args: Raw pass-through agy args.
    :param command: agy executable to run.
    :param model: Optional agy model id.
    :param permission_mode: Optional Omnigent permission mode threaded into the
        agy argv assembly.
    :param headless: ``True`` when no interactive client will attach (forces
        the agy permission-bypass flag).
    :param host_id: Local host daemon id, e.g. ``"host_abc123"``.
    :param workspace: Absolute workspace path for the runner cwd.
    :param startup_progress: Optional user-visible progress renderer.
    :returns: Prepared terminal details for attaching.
    :raises click.ClickException: If setup fails.
    """
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        bridge_id: str
        conversation_id: str
        resume = False
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException(
                    "Creating an Antigravity session requires a session bundle."
                )
            _update_progress(startup_progress, "Creating Antigravity session...")
            bridge_id = _mint_agy_conversation_id()
            conversation_id = bridge_id
            session_id = await _create_antigravity_session(
                client,
                session_bundle,
                bridge_id=bridge_id,
            )
        else:
            _update_progress(startup_progress, "Loading Antigravity session...")
            payload = await _fetch_antigravity_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not an antigravity-native session."
                )
            bridge_id = str(labels.get(ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY) or session_id)
            # Reattach to an already-running runner-owned agy terminal instead of
            # relaunching. Without this the daemon resume path always falls to
            # ``_launch_and_record`` → unconditional ``clear_bridge_state``,
            # which wipes the runner reader's bound ``conversation_id``;
            # and ``reattached=False`` would make teardown close a terminal a
            # different launcher owns. Mirrors the local-server prepare path and
            # ``codex_native`` daemon resume. Runs before host-online/bind: a
            # live terminal means a runner is already serving (the GET reaches
            # it); a cold resume returns ``None`` and falls through to launch.
            existing = await _find_running_antigravity_terminal(client, session_id)
            if existing is not None:
                if antigravity_args or model is not None:
                    click.echo(
                        "Ignoring Antigravity launch args/model for an already-running "
                        "terminal; restart the session terminal to apply them.",
                        err=True,
                    )
                _update_progress(startup_progress, "Antigravity terminal ready.")
                return PreparedAntigravityTerminal(
                    session_id=session_id,
                    terminal_id=existing.terminal_id,
                    bridge_dir=bridge_dir_for_bridge_id(bridge_id),
                    tmux_socket=existing.tmux_socket,
                    tmux_target=existing.tmux_target,
                    reattached=True,
                )
            external = payload.get("external_session_id") if isinstance(payload, dict) else None
            conversation_id = external if isinstance(external, str) and external else bridge_id
            resume = isinstance(external, str) and bool(external)

        await wait_for_host_online(client, host_id, timeout_s=_DAEMON_HOST_ONLINE_TIMEOUT_S)
        _update_progress(startup_progress, "Starting runner...")
        runner_id = await launch_or_reuse_daemon_runner(
            client,
            host_id=host_id,
            session_id=session_id,
            workspace=workspace,
        )
        _update_progress(startup_progress, "Waiting for runner...")
        await wait_for_runner_online(client, runner_id, timeout_s=_DAEMON_RUNNER_ONLINE_TIMEOUT_S)
        # Must run AFTER wait_for_runner_online — unregistered runners reject
        # the bind. Mirrors the Codex/Claude daemon prepare ordering.
        await _bind_session_runner(client, session_id, runner_id)
        # The runner OWNS the antigravity terminal: binding triggers its idempotent
        # auto-create (``runner/app.py`` ``_auto_create_antigravity_terminal``,
        # which fires for every antigravity-native session). Reattach to that
        # runner-owned terminal instead of launching our own. Launching here would
        # (a) 500 ("terminal antigravity:main … already observed as required") and
        # (b) ``_launch_and_record``'s ``clear_bridge_state`` would wipe the bridge
        # state the runner wrote, so every web turn would fail with "Antigravity
        # native bridge state is missing". The pre-bind reattach check can't catch
        # this — the runner only auto-creates AFTER the bind. Falling through to a
        # CLI launch stays as a defensive fallback for the (unexpected) case where
        # the runner produced no terminal in the window.
        autocreated = await _await_runner_antigravity_terminal(
            client, session_id, _RUNNER_TERMINAL_AUTOCREATE_TIMEOUT_S
        )
        if autocreated is not None:
            if antigravity_args or model is not None:
                click.echo(
                    "Ignoring Antigravity launch args/model for the runner-owned "
                    "terminal; restart the session terminal to apply them.",
                    err=True,
                )
            _update_progress(startup_progress, "Antigravity terminal ready.")
            return PreparedAntigravityTerminal(
                session_id=session_id,
                terminal_id=autocreated.terminal_id,
                bridge_dir=bridge_dir_for_bridge_id(bridge_id),
                tmux_socket=autocreated.tmux_socket,
                tmux_target=autocreated.tmux_target,
                reattached=True,
            )
        launched = await _launch_and_record(
            client,
            session_id=session_id,
            bridge_id=bridge_id,
            conversation_id=conversation_id,
            resume=resume,
            antigravity_args=antigravity_args,
            command=command,
            model=model,
            permission_mode=permission_mode,
            headless=headless,
            startup_progress=startup_progress,
        )
    return PreparedAntigravityTerminal(
        session_id=session_id,
        terminal_id=launched.terminal_id,
        bridge_dir=bridge_dir_for_bridge_id(bridge_id),
        tmux_socket=launched.tmux_socket,
        tmux_target=launched.tmux_target,
        reattached=False,
    )


async def _launch_and_record(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_id: str,
    conversation_id: str,
    resume: bool,
    antigravity_args: tuple[str, ...],
    command: str,
    model: str | None,
    permission_mode: str | None = None,
    headless: bool = False,
    startup_progress: RunnerStartupProgress | None,
) -> LaunchedAntigravityTerminal:
    """
    Prepare the bridge, launch the agy terminal, and seed bridge state.

    Builds the agy argv (``--conversation <real-id>`` on resume), POSTs the
    terminal resource, and seeds the shared bridge state the
    ``antigravity-native`` harness reads. agy's real id is NOT captured here: agy
    mints its own UUID and ignores the launcher's id, so a fresh launch seeds an
    ``agy_conv_*`` placeholder that the attach-time cold-start
    (:func:`_cold_start_agy_conversation`) replaces with agy's real cascade id in
    bridge state once agy is live. Seeding a guessed id here would make a later
    resume pass an id agy cannot find.

    No agy process pid is captured here (and there is no ``agy_pid`` field in
    bridge state). The terminal is launched with ``tmux_start_on_attach=True``,
    so at launch the pane runs a ``tmux wait-for`` shell — the agy process does
    not exist until the first client attaches, and there is no pid to record.
    The executor therefore discovers agy's connect-RPC port at injection time by
    enumerating agy processes and validating each against the bridge's
    conversation id via ``GetConversationMetadata`` (see
    :func:`omnigent.antigravity_native_rpc.resolve_language_server_port`). A pid
    fast-path is deliberately omitted: it would never fire (no pid at launch)
    and trusting a recycled pid without the conversation check would risk
    injecting into a different live agy.

    No durable read cursor is seeded: the RPC read driver that mirrors agy's
    conversation (:mod:`omnigent.antigravity_native_reader`) keeps an in-memory
    seen-set only — the transcript forwarder's ``forwarded_steps`` resume cursor
    was retired in the Task 12 cutover.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param bridge_id: Opaque bridge id keying the bridge directory.
    :param conversation_id: On resume, agy's real (discovered) conversation id
        to pass as ``--conversation``. On a fresh launch, the minted
        ``agy_conv_*`` placeholder used only to seed bridge state until the
        cold-start mints agy's real id.
    :param resume: ``True`` to resume an existing agy conversation.
    :param antigravity_args: Raw pass-through agy args.
    :param command: agy executable to run.
    :param model: Optional agy model id.
    :param permission_mode: Optional Omnigent permission mode threaded into the
        agy argv assembly (maps ``"bypassPermissions"`` to the bypass flag).
    :param headless: ``True`` when no interactive client will attach (forces
        the agy permission-bypass flag so an unattended turn does not hang).
    :param startup_progress: Optional user-visible progress renderer.
    :returns: Launched terminal resource details.
    :raises click.ClickException: If the terminal launch fails.
    """
    bridge_dir = prepare_bridge_dir(bridge_id)
    # Clear stale turn/conversation state so a fresh launch rediscovers this run's
    # real agy conversation id instead of binding to the previous run's.
    await asyncio.to_thread(clear_bridge_state, bridge_dir)
    # Pre-accept agy's first-run onboarding wizard (HOME-global) so a headless /
    # detached launch does not hang waiting for a TTY answer. Idempotent and
    # offloaded to a thread (file I/O), mirroring the bridge-state writes below.
    await asyncio.to_thread(ensure_agy_onboarding_complete)
    argv, env_overrides = build_agy_launch(
        conversation_id=conversation_id if resume else None,
        model=model,
        resume=resume,
        permission_mode=permission_mode,
        headless=headless,
        extra_args=antigravity_args,
    )
    _update_progress(startup_progress, "Starting Antigravity terminal...")
    launched = await _launch_antigravity_terminal(
        client,
        session_id,
        argv=argv,
        env=env_overrides,
        command=command,
    )
    # Advertise the tmux pane so a web turn to this CLI-launched session can be
    # bootstrapped into the idle agy TUI by the executor (agy mints its
    # conversation only after it processes a turn; until then connect-RPC has
    # nothing to address). Only when the runner exposed a local pane.
    if launched.tmux_socket is not None and launched.tmux_target is not None:
        await asyncio.to_thread(
            write_tmux_target,
            bridge_dir,
            socket_path=launched.tmux_socket,
            tmux_target=launched.tmux_target,
        )
    # Seed bridge state with the conversation id known so far (the real id on
    # resume; the ``agy_conv_*`` placeholder on a fresh launch — the attach-time
    # cold-start replaces it with agy's real cascade id once agy is live, so the
    # RPC reader binds the real conversation). No durable read cursor is seeded
    # (the reader keeps an in-memory seen-set; the forwarder cursor was retired).
    await asyncio.to_thread(
        write_bridge_state,
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id=session_id,
            conversation_id=conversation_id,
        ),
    )
    _update_progress(startup_progress, "Antigravity terminal ready.")
    return launched


async def _attach_terminal(
    *,
    base_url: str,
    headers: dict[str, str],
    prepared: PreparedAntigravityTerminal,
    recover: Callable[[], Awaitable[None]] | None,
) -> None:
    """
    Attach to the prepared agy terminal, tearing it down on real exit.

    Prefers a direct local tmux attach when the runner shares this host;
    otherwise relays over the WebSocket PTY bridge with reconnect. On a
    real exit (not a tmux detach) the AP-side terminal resource is
    best-effort closed, unless this invocation reattached to a terminal
    another launcher owns.

    **Read/write wiring on the CLI fallback only.** When the runner produced no
    terminal and the CLI launched its own (``prepared.reattached is False``), this
    spawns — for the attach's lifetime, cancelled in ``finally`` — the RPC read
    driver (:func:`omnigent.antigravity_native_reader.run_reader_with_bridge`,
    which mirrors agy's conversation into the Omnigent chat view and surfaces
    WAITING interactions as real-time elicitations) and a one-shot cold-start
    (:func:`_cold_start_agy_conversation`) that mints agy's real cascade id so the
    reader can bind it. These run CONCURRENTLY with the attach because the CLI
    terminal uses ``tmux_start_on_attach=True`` — agy does not exist until this
    attach starts the pane, so cold-start cannot precede it; the cold-start's
    port poll and the reader's discovery poll both wait agy out. When
    ``prepared.reattached is True`` the runner OWNS the terminal and already runs
    its own reader (``runner/app.py`` ``_auto_create_antigravity_terminal``), so
    the CLI starts neither — a second reader would double-mirror every step.

    .. note::
        On this CLI fallback the human attaches to agy's TUI, which shows the
        empty ``>`` banner: the cold-started conversation is a HEADLESS RPC
        cascade that does not surface in the agy TUI (established by the cold-start
        spike). The real conversation is that headless RPC one, driven by the web
        UI through the reader + executor — so the TUI looking empty while web
        turns flow is inherent to the RPC model, not a bug.

        Unlike the retired transcript forwarder, the reader does NOT have
        refresh-capable auth here: it snapshots ``headers`` (the local server has
        none; a remote server's bearer is used for its lifetime — ``recover``
        refreshes the *attach* headers on reconnect but not the reader's client).
        The post-hoc policy audit the forwarder ran is dropped; real-time
        elicitation is the enforcement surface now.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers (mutated in place by ``recover``).
    :param prepared: Prepared terminal details.
    :param recover: Optional async reconnect-recovery callback. ``None``
        disables reconnect (the local-server flow owns the server
        lifecycle and has nothing to reconnect to).
    :returns: None after the attach exits.
    """
    reader: asyncio.Task[None] | None = None
    cold_start: asyncio.Task[None] | None = None
    if not prepared.reattached:
        reader = asyncio.create_task(
            run_reader_with_bridge(
                base_url=base_url,
                headers=headers,
                # The CLI has no refresh-capable auth flow (the bearer in
                # ``headers``, if any, is used as-is); only the runner threads an
                # ``httpx.Auth``.
                auth=None,
                session_id=prepared.session_id,
                bridge_dir=prepared.bridge_dir,
            ),
            name="antigravity-native-rpc-reader",
        )
        # Scope the StartCascade port to THIS session's pane agy so a multi-agy
        # host cannot cross-bind to a foreign agy — but ONLY when the tmux socket
        # exists on THIS host. A remote runner advertises a server-side socket
        # PATH that is not local; running ``tmux -S <remote-path> display-message``
        # against it would fail on every poll (~80 doomed spawns over the budget),
        # so gate on local existence (mirroring ``_can_attach_direct_tmux``) and
        # route the remote case to the no-pane -> candidate fallback instead.
        pane_local = (
            prepared.tmux_socket is not None
            and prepared.tmux_target is not None
            and prepared.tmux_socket.exists()
        )
        cold_start = asyncio.create_task(
            _cold_start_agy_conversation(
                prepared.bridge_dir,
                prepared.session_id,
                base_url=base_url,
                headers=headers,
                tmux_socket=prepared.tmux_socket if pane_local else None,
                tmux_target=prepared.tmux_target if pane_local else None,
            ),
            name="antigravity-native-cold-start",
        )
    outcome = _AttachOutcome.EXITED
    try:
        if _can_attach_direct_tmux(prepared):
            if prepared.tmux_socket is None or prepared.tmux_target is None:
                raise click.ClickException("Antigravity tmux attach metadata was incomplete.")
            outcome = await _attach_direct_tmux(prepared.tmux_socket, prepared.tmux_target)
        else:
            outcome = await _attach_with_reconnect(
                attach=attach_local_terminal,
                attach_url=_attach_url(base_url, prepared.session_id, prepared.terminal_id),
                headers=headers,
                recover=recover,
                base_url=base_url,
                session_id=prepared.session_id,
                terminal_id=prepared.terminal_id,
                close_attach_on_terminal_gone=True,
            )
    finally:
        for task in (cold_start, reader):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if not prepared.reattached and outcome is not _AttachOutcome.DETACHED:
            await _close_antigravity_terminal(
                base_url=base_url,
                headers=headers,
                session_id=prepared.session_id,
                terminal_id=prepared.terminal_id,
            )


# Cold-start port-discovery budget for the CLI fallback. agy's connect-RPC server
# binds its loopback port a moment AFTER the process starts (per-process, BEFORE
# any conversation exists), and on this path agy only starts when the attach opens
# its pane — so the bootstrap polls. The wait is bounded so a never-binding agy
# cannot pin the task; the reader's own discovery keeps polling afterward as a
# fallback. Mirrors the runner's ``_AGY_COLD_START_PORT_TIMEOUT_S``.
_AGY_COLD_START_PORT_TIMEOUT_S = 20.0
_AGY_COLD_START_PORT_POLL_INTERVAL_S = 0.25


async def _agy_cold_start_poll_sleep(seconds: float) -> None:
    """
    Sleep between agy cold-start port-discovery polls.

    Indirection point so tests can stub the poll backoff without patching the
    process-wide ``asyncio.sleep``. Mirrors
    :func:`omnigent.runner.app._agy_cold_start_poll_sleep`.

    :param seconds: Seconds to wait before the next port probe, e.g. ``0.25``.
    :returns: None.
    """
    await asyncio.sleep(seconds)


async def _cold_start_agy_conversation(
    bridge_dir: Path,
    session_id: str,
    *,
    base_url: str,
    headers: dict[str, str],
    tmux_socket: Path | None = None,
    tmux_target: str | None = None,
    timeout_s: float = _AGY_COLD_START_PORT_TIMEOUT_S,
) -> None:
    """
    Cold-start agy's conversation over connect-RPC for the CLI fallback (best-effort).

    The CLI-fallback analogue of the runner's
    :func:`omnigent.runner.app._cold_start_agy_conversation`: once agy is live
    (the attach started its pane), mint a real cascade over ``StartCascade`` and
    write that id into bridge state, replacing the ``agy_conv_*`` placeholder
    :func:`_launch_and_record` seeded — so the RPC reader binds the real
    conversation and web turns resolve, instead of the reader polling the
    placeholder forever. The connect-RPC port is resolved by
    :func:`omnigent.antigravity_native_rpc.resolve_cold_start_agy_rpc_port`:
    scoped to THIS session's own agy via its tmux pane (``tmux_socket`` /
    ``tmux_target``) so a host running several agy instances cannot
    ``StartCascade`` onto a FOREIGN agy (the conversation-ownership check that
    normally disambiguates is not usable yet — no conversation exists). Crucially
    on this CLI path the terminal uses ``tmux_start_on_attach=True``, so agy is
    not ``exec``-ed until the human attaches while this poll runs concurrently:
    until our agy appears in the pane the resolver returns no port and this KEEPS
    POLLING rather than falling back to a candidate (which could be a foreign
    agy). It falls back to the lowest ``Heartbeat``-answering candidate only when
    no local pane was supplied, or once our agy is up but its port is not
    lsof-attributable. This polls that resolver until a port binds, then
    ``StartCascade``s a generated ``uuid4``.

    The cold-started id is also PATCHed onto the Omnigent session as
    ``external_session_id`` (best-effort, mirroring the runner cold-start and
    codex/pi) so a later ``omnigent antigravity --resume`` reads it back and passes
    ``--conversation <id>`` to continue agy's actual conversation — the read-path
    replacement for the retired forwarder's ``_patch_external_session_id``.

    Resume launches already hold agy's real id (seeded as a non-placeholder
    ``conversation_id`` and passed as ``--conversation``), so cold-starting would
    create a second empty conversation and overwrite the resumed id — this no-ops
    when the seeded id is NOT a placeholder (the guard that makes ``--resume``
    actually continue the prior conversation).

    **Best-effort, never raises.** A failure (no state, no port within
    *timeout_s*, ``StartCascade`` erroring, or the PATCH failing) leaves the
    placeholder; the reader's discovery then binds agy's real id once a turn
    creates the conversation. The sync RPC/poll work runs in
    :func:`asyncio.to_thread` so the event loop is never blocked.

    :param bridge_dir: Native Antigravity bridge directory whose ``state.json``
        the real cold-started id is written into.
    :param session_id: Owning session/conversation id (for log correlation and
        the ``external_session_id`` PATCH target).
    :param base_url: Omnigent server base URL for the ``external_session_id``
        PATCH.
    :param headers: HTTP auth headers for the PATCH (the local server has none; a
        remote server's bearer is used as-is).
    :param tmux_socket: This session's tmux socket path, used to scope the
        ``StartCascade`` port to the agy running under this session's pane.
        ``None`` (no local pane) falls back to the candidate scan.
    :param tmux_target: This session's tmux target (e.g. ``"main"``), paired with
        ``tmux_socket`` for the pane-scoped port resolution.
    :param timeout_s: Total seconds to wait for agy's connect-RPC port to bind.
    :returns: None.
    """
    state = await asyncio.to_thread(read_bridge_state, bridge_dir)
    if state is None:
        return
    if not is_placeholder_conversation_id(state.conversation_id):
        # Resume: agy's real id is already seeded (and passed as --conversation);
        # cold-starting would create a second empty conversation and clobber the
        # resumed id, defeating --resume.
        return

    deadline = time.monotonic() + timeout_s
    port: int | None = None
    while True:
        # Scope to THIS session's pane agy (avoids binding a foreign agy on a
        # multi-agy host); falls back to the lowest validated candidate when no
        # local pane is reachable or the pane is not resolvable yet.
        port = await asyncio.to_thread(resolve_cold_start_agy_rpc_port, tmux_socket, tmux_target)
        if port is not None:
            break
        if time.monotonic() >= deadline:
            _logger.warning(
                "Antigravity cold-start: no agy connect-RPC port bound within %.0fs for "
                "session %s; leaving the placeholder for the reader to bind once a turn "
                "creates the conversation.",
                timeout_s,
                session_id,
            )
            return
        await _agy_cold_start_poll_sleep(_AGY_COLD_START_PORT_POLL_INTERVAL_S)

    cascade_id = str(uuid.uuid4())
    try:
        await asyncio.to_thread(start_cascade, port, cascade_id)
    except AntigravityRpcError:
        _logger.warning(
            "Antigravity cold-start: StartCascade failed on port %s for session %s; leaving "
            "the placeholder for the reader to bind.",
            port,
            session_id,
            exc_info=True,
        )
        return
    # Persist the real id (replacing the placeholder) so ``read_bridge_state``
    # returns it and the reader/executor address the cold-started conversation.
    # Offloaded (file I/O).
    await asyncio.to_thread(update_conversation_id, bridge_dir, cascade_id)
    # PATCH the real id onto the session so a later ``--resume`` continues this
    # conversation (best-effort; mirrors the runner cold-start + codex/pi). A
    # short-lived client suffices for this one call.
    try:
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=10.0) as client:
            resp = await client.patch(
                f"/v1/sessions/{url_component(session_id)}",
                json={"external_session_id": cascade_id},
            )
    except httpx.HTTPError:
        _logger.warning(
            "Antigravity cold-start: failed to PATCH external_session_id=%s onto session %s; "
            "the conversation is mirrored but `--resume` will cold-start fresh.",
            cascade_id,
            session_id,
            exc_info=True,
        )
    else:
        if resp.status_code >= 400:
            _logger.warning(
                "Antigravity cold-start: server rejected external_session_id PATCH (%s); "
                "session=%s cascade=%s — the conversation is mirrored but `--resume` will "
                "cold-start fresh.",
                resp.status_code,
                session_id,
                cascade_id,
            )
    _logger.info(
        "Antigravity cold-start: created conversation %s on port %s for session %s",
        cascade_id,
        port,
        session_id,
    )


def _can_attach_direct_tmux(prepared: PreparedAntigravityTerminal) -> bool:
    """
    Return whether this process can attach to the runner tmux directly.

    ``True`` only when the runner advertised a tmux socket + target, the
    socket exists on this host (same machine), and ``tmux`` is on PATH. A
    remote runner's socket won't exist locally, so this returns ``False``
    and the caller falls back to the WebSocket attach. Mirrors
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


async def _attach_direct_tmux(socket_path: Path, tmux_target: str) -> _AttachOutcome:
    """
    Attach the current terminal directly to the runner-owned tmux pane.

    Lower latency than the WebSocket PTY relay because there is no server
    round-trip. ``TMUX`` is dropped from the child environment so a user
    who runs ``omnigent antigravity`` from inside their own tmux can still
    attach to Omnigent's private tmux server. After the attach child
    exits, a ``has-session`` probe distinguishes a user *detach* (session
    still alive) from agy *exiting* (session gone).

    :param socket_path: Runner tmux server socket path.
    :param tmux_target: tmux ``-t`` target to attach, e.g. ``"main"``.
    :returns: :attr:`_AttachOutcome.DETACHED` when the tmux session
        outlives the attach (user detached), else
        :attr:`_AttachOutcome.EXITED`.
    """
    from omnigent.terminals.ws_bridge import _tmux_session_alive

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
    if await _tmux_session_alive(str(socket_path), tmux_target):
        return _AttachOutcome.DETACHED
    return _AttachOutcome.EXITED


async def _create_antigravity_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    bridge_id: str,
) -> str:
    """
    Create a bundled terminal-first Antigravity session.

    Stamps the wrapper + terminal-UI labels and the bridge-id label.
    ``external_session_id`` is left unset here: agy mints its own UUID and ignores
    any id the launcher assigns, so the real id is established at runtime by the
    cold-start (:func:`_cold_start_agy_conversation`), which writes it to bridge
    state AND PATCHes it onto the session as ``external_session_id`` — the
    read-path replacement for the retired forwarder's id capture, so a later
    ``--resume`` continues agy's actual conversation.

    :param client: HTTP client pointed at the Omnigent server.
    :param bundle: Gzipped Antigravity wrapper agent bundle.
    :param bridge_id: Opaque bridge id to write on the session labels.
    :returns: New Omnigent session id, e.g. ``"conv_abc123"``.
    :raises click.ClickException: If creation fails.
    """
    labels = dict(_SESSION_LABELS)
    labels[ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY] = bridge_id
    metadata: dict[str, object] = {"labels": labels}
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("antigravity-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Antigravity session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    new_session_id = body.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise click.ClickException(
            "Antigravity session creation response did not include session_id."
        )
    return new_session_id


async def _fetch_antigravity_session(
    client: httpx.AsyncClient, session_id: str
) -> dict[str, object]:
    """
    Fetch an existing Omnigent session snapshot.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :returns: Decoded session payload.
    :raises click.ClickException: If the lookup fails or returns non-object JSON.
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


async def _launch_antigravity_terminal(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    argv: list[str],
    env: dict[str, str],
    command: str,
) -> LaunchedAntigravityTerminal:
    """
    Launch the server-backed agy terminal resource.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent session id.
    :param argv: Full agy command list from :func:`build_agy_launch`. The
        first element is the agy binary; the rest are its args.
    :param env: Environment overrides for the terminal process from
        :func:`build_agy_launch`.
    :param command: agy executable to run (matches ``argv[0]``).
    :returns: Launched terminal resource details.
    :raises click.ClickException: If the terminal launch fails.
    """
    spec: dict[str, object] = {
        "command": command,
        "args": list(argv[1:]),
        "os_env_type": "caller_process",
        # Pin the terminal cwd to the user's launch directory. This IS agy's
        # workspace: agy runs its tools in the process cwd (verified
        # empirically — without it, tools run in agy's default ``scratch`` dir),
        # so no ``--add-dir`` flag is needed. The runner is local, so
        # ``Path.cwd()`` here equals the runner workspace. See the same comment
        # in ``claude_native._claude_terminal_request``.
        "cwd": str(Path.cwd().resolve()),
        "env": env,
        "scrollback": _ANTIGRAVITY_TERMINAL_SCROLLBACK_LINES,
        "tmux_allow_passthrough": True,
        "tmux_start_on_attach": True,
    }
    body = {
        "terminal": _TERMINAL_NAME,
        "session_key": _TERMINAL_SESSION_KEY,
        "spec": spec,
        # Native-bootstrap allowlist marker only: it lets the server's
        # create-terminal gate admit this undeclared terminal name (see
        # ``omnigent/server/routes/sessions.py`` ``is_native_bootstrap``).
        #
        # Deliberately NOT ``bridge_inject_dir``: on the runner, that marker
        # triggers Claude-native machinery — it starts the Claude comment relay,
        # tags the terminal ``CLAUDE_NATIVE_TERMINAL_ROLE`` (which drives the
        # session's PTY-derived working status), and publishes Claude tmux
        # metadata. None of that is owned by antigravity teardown, and
        # antigravity derives its working status from the RPC reader's
        # ``external_session_status`` edges, not PTY activity.
        # ``ensure_native_terminal`` is allowlisted the same
        # way but the runner's claude/codex ``ensure`` branches are gated on
        # those terminal names, so for ``antigravity`` it falls through to the
        # plain generic launch with no Claude side effects. The antigravity
        # harness reads its bridge dir from its own spawn env
        # (``build_antigravity_native_spawn_env``), so no terminal-launch bridge
        # injection is needed.
        "ensure_native_terminal": True,
    }
    resp = await client.post(
        f"/v1/sessions/{url_component(session_id)}/resources/terminals",
        json=body,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Antigravity terminal launch failed ({resp.status_code}): {error_text(resp)}"
        )
    return _launched_antigravity_terminal_from_payload(resp.json())


def _launched_antigravity_terminal_from_payload(payload: object) -> LaunchedAntigravityTerminal:
    """
    Decode terminal launch metadata returned by the runner.

    :param payload: Decoded terminal resource JSON object, e.g.
        ``{"id": "terminal_antigravity_main", "metadata": {...}}``.
    :returns: Launched terminal details.
    :raises click.ClickException: If the response omits a valid terminal id.
    """
    if not isinstance(payload, dict):
        raise click.ClickException("Antigravity terminal launch returned non-object JSON.")
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException(
            "Antigravity terminal launch response did not include terminal id."
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
    return LaunchedAntigravityTerminal(
        terminal_id=terminal_id,
        tmux_socket=tmux_socket,
        tmux_target=tmux_target,
    )


async def _find_running_antigravity_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> LaunchedAntigravityTerminal | None:
    """
    Return the existing running agy terminal id if present.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :returns: Terminal details, or ``None`` when the wrapper should launch
        a new terminal (missing, stopped, or runner unavailable).
    :raises click.ClickException: If the server rejects the lookup for a
        reason other than "not currently attachable".
    """
    terminal_id = antigravity_terminal_resource_id()
    resp = await client.get(
        f"/v1/sessions/{url_component(session_id)}"
        f"/resources/terminals/{url_component(terminal_id)}"
    )
    if resp.status_code in {404, 409, 502, 503}:
        return None
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Failed to fetch Antigravity terminal ({resp.status_code}): {error_text(resp)}"
        )
    payload = resp.json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("running") is False:
        return None
    return _launched_antigravity_terminal_from_payload(payload)


async def _close_antigravity_terminal(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    terminal_id: str,
) -> None:
    """
    Best-effort close of the AP-side agy terminal resource.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers.
    :param session_id: Omnigent session id.
    :param terminal_id: Terminal resource id.
    :returns: None.
    """
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(10.0),
        ) as client:
            response = await client.delete(
                f"/v1/sessions/{url_component(session_id)}"
                f"/resources/terminals/{url_component(terminal_id)}"
            )
        if response.status_code >= 400:
            _logger.warning(
                "agy terminal close returned %s: session=%s terminal=%s",
                response.status_code,
                session_id,
                terminal_id,
            )
    except (httpx.HTTPError, OSError) as exc:
        # Best-effort teardown: a transport/OS failure must not mask the exit
        # path, but log it so a leaked terminal is diagnosable. A programmer
        # error (e.g. a malformed URL) propagates instead of being silently eaten.
        _logger.warning(
            "agy terminal close failed: session=%s terminal=%s error=%r",
            session_id,
            terminal_id,
            exc,
        )


def _resolve_session_id_for_resume(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    resume_picker: bool,
) -> str | None:
    """
    Translate resume inputs into a concrete antigravity-native session id.

    :param base_url: Omnigent server base URL.
    :param headers: HTTP auth headers; ``{}`` for the local server.
    :param session_id: Explicit session id, e.g. ``"conv_abc123"``.
    :param resume_picker: ``True`` for bare ``--resume``.
    :returns: Session id, or ``None`` for a fresh session / cancelled picker.
    """
    if session_id is not None:
        return session_id
    if not resume_picker:
        return None
    from omnigent_client import OmnigentClient

    from omnigent.repl._resume_picker import pick_conversation_by_wrapper_label_from_sdk

    async def _drive() -> str | None:
        """
        Run the async antigravity-native picker.

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


def _mint_agy_conversation_id() -> str:
    """
    Mint a fresh agy conversation id for a new session.

    :returns: An ``"agy_conv_<hex>"`` id, e.g.
        ``"agy_conv_5e1f...".``
    """
    return f"{AGY_PLACEHOLDER_CONVERSATION_PREFIX}{uuid.uuid4().hex}"


def antigravity_terminal_resource_id() -> str:
    """
    Return the deterministic terminal resource id for Antigravity.

    :returns: Terminal resource id, e.g. ``"terminal_antigravity_main"``.
    """
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)


def _update_progress(
    startup_progress: RunnerStartupProgress | None,
    message: str,
) -> None:
    """
    Show one concise startup milestone when a renderer is active.

    :param startup_progress: Optional progress renderer.
    :param message: User-facing status text, e.g.
        ``"Starting Antigravity terminal..."``.
    :returns: None.
    """
    if startup_progress is not None:
        startup_progress.update(message)


def _preflight_local_tools() -> None:
    """
    Verify local executables required by the native Antigravity wrapper.

    :returns: None.
    :raises click.ClickException: If required tools are missing.
    """
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native Antigravity wrapper "
            "attaches to the runner-owned agy tmux terminal."
        )


def _launch_is_headless() -> bool:
    """
    Return whether this agy launch is headless (no interactive client attaches).

    ``omnigent antigravity`` attaches the local TTY to the agy tmux terminal so
    the user drives agy interactively. agy's default ``request-review``
    permission prompt is fine for that attended case, but it would **hang an
    unattended/headless turn forever** waiting for a terminal answer (sandbox /
    autonomous / detached / piped invocation). The standard CLI signal for "an
    interactive client will attach" is a controlling terminal on both stdin and
    stdout; when either is not a TTY (CI, ``nohup``, a pipe, a detached spawn)
    the launch is treated as headless so the caller can auto-bypass agy's prompt
    (see :func:`omnigent.antigravity_native_launch.should_skip_permissions`).

    .. note:: This TTY signal governs ONLY the human-invoked CLI launch path
       (``run_antigravity_native`` → here, the single call site). The
       server-spawned / web-attached path
       (:func:`omnigent.runner.app._auto_create_antigravity_terminal`, the
       claude/codex auto-create analogue) does NOT consult this function — it
       passes ``headless=False`` to ``build_agy_launch`` directly, because the
       web client attaches to the agy pane through the runner tunnel and answers
       agy's ``request-review`` prompt there. **Keep that invariant:** a
       server-spawned launch must never key headlessness on the runner process's
       (absent) controlling TTY, which would conflate "no CLI tty" with "no
       client attached" and silently disable agy's per-tool prompt for a watching
       web user.

    :returns: ``True`` when no interactive terminal is attached (headless).
    """
    try:
        return not (sys.stdin.isatty() and sys.stdout.isatty())
    except (ValueError, OSError):
        # A closed/detached stream raises rather than returning False; treat any
        # such failure as "no interactive client" — the safe, non-hanging choice.
        return True
