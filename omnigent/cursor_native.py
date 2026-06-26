"""Native Cursor TUI wrapper for the Omnigent CLI.

``omnigent cursor`` launches the Cursor CLI's interactive TUI (``cursor-agent``
with no args) inside an Omnigent-runner-owned tmux terminal and attaches the
local TTY — the cursor analog of ``omnigent codex`` / ``omnigent pi``. The runner
spawns the process (see :func:`omnigent.runner.app._auto_create_cursor_terminal`);
this module owns the CLI-side orchestration: session create/resume, daemon
runner bind, terminal-ready poll, and the direct tmux attach.

Auth is the ambient ``cursor-agent login`` (``$HOME/.cursor``); no API key is
required. Unlike Pi there is no extension bridge — the runner sets up the
terminal environment directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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
from omnigent._wrapper_labels import CURSOR_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE
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

_DEFAULT_CURSOR_COMMAND = "cursor-agent"
_CURSOR_PATH_ENV = "OMNIGENT_CURSOR_PATH"
#: cursor chat ids (used as ``external_session_id``) are canonical UUIDs, e.g.
#: ``0ef42bbf-3b80-4bec-ac39-ca46531cbc47``. This id flows into two untrusted
#: sinks — a filesystem path component (the cursor chat-store dir) and the
#: ``cursor-agent --resume`` argv — so both callers validate against this strict
#: 8-4-4-4-12 shape before use (stricter than codex's loose hex+dash guard,
#: because we know cursor mints full UUIDs). Anything else can never reach a path
#: or argv.
_CURSOR_CHAT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_valid_cursor_chat_id(chat_id: str | None) -> bool:
    """Return whether *chat_id* is a well-formed cursor chat id (UUID shape).

    Used to gate the persisted ``external_session_id`` before it is used as a
    cursor store-path component or passed to ``cursor-agent --resume``.
    """
    return bool(chat_id) and _CURSOR_CHAT_ID_RE.fullmatch(chat_id) is not None


_AGENT_NAME = "cursor-native-ui"
_TERMINAL_NAME = "cursor"
_TERMINAL_SESSION_KEY = "main"
_SESSION_LABELS = {
    "omnigent.ui": "terminal",
    _WRAPPER_LABEL_KEY: _WRAPPER_LABEL_VALUE,
}


@dataclass(frozen=True)
class NativeCursorLaunch:
    """Resolved native Cursor process launch."""

    executable: str
    argv: list[str]


@dataclass(frozen=True)
class LaunchedCursorTerminal:
    """Terminal resource returned by the Omnigent runner launch path."""

    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None


@dataclass(frozen=True)
class PreparedCursorTerminal:
    """Prepared native Cursor terminal attachment details.

    :param reattached: ``True`` when an existing, still-running session
        terminal was reused (the live-reattach path: prior chat is
        intact).
    :param cold_resumed: ``True`` when resuming an existing Omnigent
        session whose terminal had already exited, so a new
        ``cursor-agent`` TUI is launched. The forwarder persisted the
        cursor chat id as ``external_session_id``, so the runner relaunches
        with ``--resume <chatId>`` and the prior conversation is reloaded
        (cursor reuses its chat store across ``--resume``). Distinct from a
        brand-new session (``resolved_session_id is None``) and from a live
        reattach. Drives the cold-resume stderr hint. Note: cursor
        deliberately treats
        ``cold_resumed`` and ``reattached`` as mutually exclusive (the
        cold-resume path leaves ``reattached`` at its ``False`` default)
        - unlike ``claude_native`` which models them independently. This
        is safe because cursor never reads ``reattached`` for teardown
        ownership; do not "fix" the apparent inconsistency.
    """

    session_id: str
    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None
    reattached: bool
    cold_resumed: bool = False
    #: The validated cursor chat id captured for this session, when a cold
    #: resume will actually reload prior turns. ``None`` when no resumable id
    #: was captured (first run, or the forwarder never persisted one), so the
    #: cold resume genuinely starts a fresh chat — drives the honest hint.
    resume_chat_id: str | None = None


def _configured_cursor_command(env: Mapping[str, str]) -> str:
    """Return the configured cursor-agent executable name/path from *env*."""
    value = env.get(_CURSOR_PATH_ENV, "").strip()
    return value or _DEFAULT_CURSOR_COMMAND


def resolve_cursor_executable(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """
    Resolve the native Cursor (``cursor-agent``) executable.

    :param env: Environment mapping to inspect. Defaults to ``os.environ``.
    :param which: Resolver hook for tests; defaults to ``shutil.which``.
    :returns: Absolute executable path.
    :raises click.ClickException: If no cursor-agent CLI is available.
    """
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    command = _configured_cursor_command(env)
    resolved = which(command)
    if resolved is None:
        raise click.ClickException(
            "Native Cursor requires the 'cursor-agent' CLI on PATH. Install it with: "
            "curl https://cursor.com/install -fsS | bash, then run 'cursor-agent login'. "
            f"You can also set {_CURSOR_PATH_ENV}=/path/to/cursor-agent."
        )
    return resolved


def build_cursor_launch(
    cursor_args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> NativeCursorLaunch:
    """Build the argv for a native Cursor process."""
    executable = resolve_cursor_executable(env=env, which=which)
    return NativeCursorLaunch(executable=executable, argv=[executable, *cursor_args])


def _inject_mode_arg(
    cursor_args: tuple[str, ...],
    mode: str | None,
) -> tuple[str, ...]:
    """Return *cursor_args* with ``--mode <mode>`` prepended when appropriate.

    No-op when *mode* is ``None`` or ``--mode`` / ``--plan`` is already present
    in *cursor_args* (user-supplied value wins).
    """
    if mode is None:
        return cursor_args
    if any(arg in ("--mode", "--plan") or arg.startswith("--mode=") for arg in cursor_args):
        return cursor_args
    return ("--mode", mode, *cursor_args)


# Catalog of cursor-agent *base* models, in web-picker display order.
#
# cursor exposes three incompatible model namespaces and the picker must use the
# one shared by all the surfaces we drive:
#
#   * ``cursor-agent models`` / ``--list-models`` flattens (model x effort) into
#     ~80 compound ids (``gpt-5.2-high``, ``claude-4.6-opus-high``) and spells
#     the claude 4.5/4.6 family with reversed word order + a dotted version
#     (``claude-4.6-opus-*`` vs the canonical ``claude-opus-4-6``); 4.7/4.8 use
#     the canonical form.
#   * The interactive ``/model`` picker filters by base id / display name and
#     keeps effort/context on a separate "Tab to modify" axis — typing a
#     compound id like ``claude-4.6-opus-high`` yields "No matches".
#   * The persisted selection (chat ``meta.lastUsedModel`` / ``cli-config``)
#     uses the *base* id (``claude-opus-4-6``, ``gpt-5.2``).
#
# The base-id namespace is the only one that round-trips across all three
# surfaces — each id below selects the right model via ``--model`` (launch),
# ``/model <id>`` (live inject), and is what ``meta.lastUsedModel`` reports back
# (mirror). The list below is DERIVED from ``cursor-agent models`` by
# ``scripts/gen_cursor_models.py``: it strips the effort/thinking suffix to
# recover the base id, applies a small override map for the irregular claude
# spellings, and drops prefix-collision / unoffered tiers (e.g. ``gpt-5.1``
# mis-ranks to "Codex 5.1 Max"). Re-run that script when cursor ships models and
# paste its output between the markers below; review the diff (a new irregular
# claude spelling needs a one-line override and the script warns about it).
#
# >>> generated by scripts/gen_cursor_models.py — do not edit by hand
_CURSOR_BASE_MODELS: list[dict[str, Any]] = [
    {"id": "auto", "displayName": "Auto"},
    {"id": "composer-2.5", "displayName": "Composer 2.5", "isDefault": True},
    {"id": "claude-opus-4-8", "displayName": "Opus 4.8"},
    {"id": "claude-opus-4-7", "displayName": "Opus 4.7"},
    {"id": "claude-opus-4-6", "displayName": "Opus 4.6"},
    {"id": "claude-opus-4-5", "displayName": "Opus 4.5"},
    {"id": "claude-sonnet-4-6", "displayName": "Sonnet 4.6"},
    {"id": "claude-sonnet-4-5", "displayName": "Sonnet 4.5"},
    {"id": "gpt-5.5", "displayName": "GPT-5.5"},
    {"id": "gpt-5.4", "displayName": "GPT-5.4"},
    {"id": "gpt-5.2", "displayName": "GPT-5.2"},
    {"id": "gpt-5.3-codex", "displayName": "Codex 5.3"},
    {"id": "gpt-5.2-codex", "displayName": "Codex 5.2"},
    {"id": "gemini-3.1-pro", "displayName": "Gemini 3.1 Pro"},
]
# <<< generated


def cursor_base_model_options() -> list[dict[str, Any]]:
    """
    Return the curated cursor-agent base-model options for the Web UI picker.

    Each option carries ``id`` (the base model id — see
    :data:`_CURSOR_BASE_MODELS`), ``displayName``, and ``isDefault``/``isCurrent``
    flags. The ids match what ``/model`` accepts and what ``meta.lastUsedModel``
    reports, so the picker selection round-trips through launch, live switch,
    and the terminal→web mirror.

    :returns: Fresh option dicts (callers may mutate); base order preserved.
    """
    return [
        {
            "id": m["id"],
            "displayName": m["displayName"],
            "isDefault": bool(m.get("isDefault", False)),
            "isCurrent": False,
        }
        for m in _CURSOR_BASE_MODELS
    ]


def run_cursor_native(
    *,
    server: str | None,
    session_id: str | None,
    cursor_args: tuple[str, ...],
    resume_picker: bool = False,
    model: str | None = None,
    auto_open_conversation: bool = False,
    mode: str | None = None,
) -> None:
    """
    Launch the Cursor TUI in an Omnigent terminal.

    :param server: Resolved Omnigent server URL.
    :param session_id: Optional existing Omnigent conversation id.
    :param cursor_args: Raw cursor-agent CLI args to persist for the runner-owned TUI.
    :param resume_picker: ``True`` runs the cursor-native picker.
    :param model: Optional Cursor model id persisted as the session
        ``model_override`` (the runner applies it as ``--model``), e.g.
        ``"gpt-5.2"``.
    :param auto_open_conversation: When ``True``, open the browser
        conversation URL after launch.
    :param mode: Optional cursor-agent execution mode (``"plan"`` or ``"ask"``).
        Injected as ``--mode <mode>`` unless already present in *cursor_args*.
    :returns: None after the terminal attach session ends.
    """
    _preflight_local_tools()
    if server is None:
        raise click.ClickException(
            "Cursor requires a resolved Omnigent server URL. The CLI should call "
            "_ensure_backend before run_cursor_native."
        )
    effective_cursor_args = _inject_mode_arg(cursor_args, mode)
    with TemporaryDirectory(prefix="omnigent-cursor-native-") as tmpdir:
        spec_path = _materialize_cursor_agent_spec(Path(tmpdir))
        _run_with_remote_server(
            server.rstrip("/"),
            spec_path,
            session_id=session_id,
            resume_picker=resume_picker,
            cursor_args=effective_cursor_args,
            model=model,
            auto_open_conversation=auto_open_conversation,
        )


def _materialize_cursor_agent_spec(tmpdir: Path) -> Path:
    """
    Write the terminal-first agent spec used by ``omnigent cursor``.

    :param tmpdir: Temporary directory for the generated YAML file.
    :returns: Path to the generated YAML spec.
    """
    yaml_path = tmpdir / "cursor-native-ui.yaml"
    raw: dict[str, Any] = {
        "name": _AGENT_NAME,
        "prompt": (
            "Cursor is running in the session terminal. The user drives the "
            "cursor-agent TUI directly."
        ),
        "executor": {"harness": "cursor-native"},
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
    cursor_args: tuple[str, ...],
    model: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Launch Cursor on an Omnigent server via a daemon-spawned runner.

    :param base_url: Omnigent server base URL.
    :param spec_path: Generated Cursor wrapper agent spec.
    :param session_id: Optional existing Omnigent session id.
    :param resume_picker: When ``True``, run the cursor-native picker.
    :param cursor_args: Raw cursor-agent CLI args.
    :param model: Optional Cursor model id persisted as ``model_override``.
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
            with runner_startup_progress(initial_message="Preparing Cursor...") as progress:
                _update_startup_progress(progress, "Connecting to local daemon...")
                _ensure_host_daemon(base_url)
                host_id = load_or_create_host_identity().host_id
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_cursor_terminal_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    session_id=resolved_session_id,
                    session_bundle=bundle,
                    cursor_args=cursor_args,
                    model=model,
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
                # Only promise restoration when a valid chat id was actually
                # captured: then the runner injects ``--resume <chatId>`` and
                # cursor (which reuses its chat store across ``--resume``)
                # reloads the prior turns. With no captured id the runner injects
                # nothing and cursor starts fresh, so the hint must say so.
                echo_native_cold_resume_hint(
                    agent_label="Cursor",
                    restored=prepared.resume_chat_id is not None,
                )
            await _attach_terminal_resource(prepared)
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="cursor",
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


async def _prepare_cursor_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    cursor_args: tuple[str, ...],
    model: str | None = None,
    host_id: str,
    workspace: str,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedCursorTerminal:
    """
    Create or resume a cursor-native session through a daemon runner.

    :param model: Optional Cursor model id persisted as the session
        ``model_override`` (the runner applies it as ``--model``).
    :returns: Prepared terminal details for attaching.
    """
    persist_args = list(cursor_args)
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        # Resuming an existing session can either reattach to a live
        # terminal (prior chat intact) or, if that terminal has exited,
        # cold-start a fresh TUI. We only know which after probing for a
        # running terminal below, so default both flags off here.
        reattached = False
        cold_resumed = False
        resume_chat_id: str | None = None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Cursor session requires a session bundle.")
            _update_startup_progress(startup_progress, "Creating Cursor session...")
            session_id = await _create_cursor_session(
                client,
                session_bundle,
                terminal_launch_args=persist_args or None,
            )
            # Persist the model pin before the runner binds and launches the
            # TUI (it reads model_override from the snapshot to build --model).
            if model is not None:
                await _patch_cursor_session(client, session_id, {"model_override": model})
        else:
            _update_startup_progress(startup_progress, "Loading Cursor session...")
            payload = await _fetch_cursor_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a cursor-native session."
                )
            existing_terminal = await _find_running_cursor_terminal(client, session_id)
            if existing_terminal is not None:
                if persist_args or model is not None:
                    click.echo(
                        "Ignoring Cursor launch args/model for an already-running terminal; "
                        "restart the session terminal to apply them.",
                        err=True,
                    )
                _update_startup_progress(startup_progress, "Cursor terminal ready.")
                return PreparedCursorTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    reattached=True,
                )
            # Session exists but its terminal has exited. The forwarder
            # persists the cursor chat id as external_session_id, so the
            # runner's _auto_create_cursor_terminal will pass
            # ``--resume <chatId>`` to cursor-agent and the TUI reloads
            # the prior conversation. Flag cold_resumed so the caller can
            # show an honest hint. Mutually exclusive with the reattach
            # path above: we leave reattached at False here (unlike
            # claude_native, which treats cold_resumed/reattached as
            # independent). Safe because cursor never uses reattached for
            # teardown ownership.
            cold_resumed = True
            # The hint below should only promise "prior conversation resumed"
            # when a valid chat id was actually captured; otherwise the runner
            # injects no ``--resume`` and cursor starts fresh. Read it from the
            # same session payload and validate it (the runner re-validates
            # before use; this keeps the user-facing message honest).
            ext = payload.get("external_session_id") if isinstance(payload, dict) else None
            if isinstance(ext, str) and is_valid_cursor_chat_id(ext):
                resume_chat_id = ext
            patch: dict[str, Any] = {}
            if persist_args:
                patch["terminal_launch_args"] = persist_args
            if model is not None:
                patch["model_override"] = model
            if patch:
                _update_startup_progress(startup_progress, "Updating Cursor session...")
                await _patch_cursor_session(client, session_id, patch)

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
        _update_startup_progress(startup_progress, "Starting Cursor terminal...")
        await _ensure_cursor_terminal_on_runner(client, session_id)
        terminal = await _wait_for_cursor_terminal_ready(
            client,
            session_id,
            timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S,
        )
        _update_startup_progress(startup_progress, "Cursor terminal ready.")
    return PreparedCursorTerminal(
        session_id=session_id,
        terminal_id=terminal.terminal_id,
        tmux_socket=terminal.tmux_socket,
        tmux_target=terminal.tmux_target,
        reattached=reattached,
        cold_resumed=cold_resumed,
        resume_chat_id=resume_chat_id,
    )


async def _create_cursor_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """Create a bundled terminal-first cursor-native session."""
    metadata: dict[str, Any] = {"labels": dict(_SESSION_LABELS)}
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("cursor-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Cursor session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    new_session_id = body.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise click.ClickException("Cursor session creation response did not include session_id.")
    return new_session_id


async def _patch_cursor_session(
    client: httpx.AsyncClient,
    session_id: str,
    patch: dict[str, Any],
) -> None:
    """
    PATCH a cursor-native session's persisted launch config.

    :param client: Omnigent server HTTP client.
    :param session_id: Conversation id to update.
    :param patch: Fields to persist, e.g. ``{"model_override": "gpt-5.2"}``.
    :raises click.ClickException: If the server rejects the update.
    """
    resp = await client.patch(
        f"/v1/sessions/{url_component(session_id)}",
        json=patch,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Cursor session launch config update failed ({resp.status_code}): {error_text(resp)}"
        )


async def _fetch_cursor_session(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
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


async def _ensure_cursor_terminal_on_runner(client: httpx.AsyncClient, session_id: str) -> None:
    """Ask the bound runner to ensure the Cursor terminal exists."""
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
            f"Cursor terminal ensure failed ({resp.status_code}): {error_text(resp)}"
        )


async def _wait_for_cursor_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> LaunchedCursorTerminal:
    """Wait until the runner exposes the Cursor terminal resource."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        terminal = await _find_running_cursor_terminal(client, session_id)
        if terminal is not None:
            return terminal
        await asyncio.sleep(0.2)
    raise click.ClickException(
        f"The runner did not create the Cursor terminal for {session_id!r} "
        f"within {timeout_s:.0f}s."
    )


async def _find_running_cursor_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> LaunchedCursorTerminal | None:
    """Return the existing running Cursor terminal id if present."""
    terminal_id = cursor_terminal_resource_id()
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
        raise click.ClickException(f"Failed to fetch Cursor terminal ({resp.status_code}): {text}")
    payload = resp.json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("running") is False:
        return None
    return _launched_cursor_terminal_from_payload(payload)


def _launched_cursor_terminal_from_payload(payload: object) -> LaunchedCursorTerminal:
    """Decode terminal launch metadata returned by the runner."""
    if not isinstance(payload, dict):
        raise click.ClickException("Cursor terminal launch returned non-object JSON.")
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException("Cursor terminal launch response did not include terminal id.")
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
    return LaunchedCursorTerminal(
        terminal_id=terminal_id,
        tmux_socket=tmux_socket,
        tmux_target=tmux_target,
    )


async def _attach_terminal_resource(prepared: PreparedCursorTerminal) -> None:
    """Attach the current terminal to the prepared Cursor terminal resource."""
    direct_tmux_error = _direct_tmux_unavailable_reason(prepared)
    if direct_tmux_error is not None:
        raise click.ClickException(
            f"Runner-owned Cursor terminal requires direct tmux attach, but {direct_tmux_error}"
        )
    if prepared.tmux_socket is None or prepared.tmux_target is None:
        raise click.ClickException("Cursor tmux attach metadata was incomplete.")
    await _attach_direct_tmux(prepared.tmux_socket, prepared.tmux_target)


async def _attach_direct_tmux(socket_path: Path, tmux_target: str) -> None:
    """Attach the current terminal directly to the runner-owned tmux pane."""
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


def _direct_tmux_unavailable_reason(prepared: PreparedCursorTerminal) -> str | None:
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
    """Translate resume inputs into a concrete cursor-native session id."""
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
    """Show one concise Cursor startup milestone when a renderer is active."""
    if startup_progress is not None:
        startup_progress.update(message)


def _preflight_local_tools() -> None:
    """Verify local executables required by the native Cursor wrapper."""
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native Cursor wrapper "
            "attaches to the runner-owned Cursor tmux terminal."
        )


def cursor_terminal_resource_id() -> str:
    """Return the deterministic terminal resource id for Cursor."""
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)
