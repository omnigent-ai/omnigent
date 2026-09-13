"""Native Devin TUI wrapper for the Omnigent CLI."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import click
import httpx
import yaml

from omnigent._platform import resolve_cli_binary
from omnigent._runner_startup import RunnerStartupProgress, runner_startup_progress
from omnigent._wrapper_labels import DEVIN_NATIVE_WRAPPER_VALUE as _WRAPPER_LABEL_VALUE
from omnigent._wrapper_labels import WRAPPER_LABEL_KEY as _WRAPPER_LABEL_KEY
from omnigent.conversation_browser import conversation_url, open_conversation_link_if_enabled
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.devin_native.bridge import DEVIN_EFFORTS
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
from omnigent.native.native_terminal import normalize_extra_args as _normalize_extra_args
from omnigent.native.native_terminal import url_component
from omnigent.util.json_types import JsonObject as _JsonObject

_DEFAULT_DEVIN_COMMAND = "devin"
_DEVIN_PATH_ENV = "OMNIGENT_DEVIN_PATH"
_AGENT_NAME = "devin-native-ui"

_TERMINAL_NAME = "devin"
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

_MODEL_LIST_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class NativeDevinLaunch:
    """Resolved native Devin process launch."""

    executable: str
    argv: list[str]


@dataclass(frozen=True)
class LaunchedDevinTerminal:
    """Terminal resource returned by the Omnigent runner launch path."""

    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None


@dataclass(frozen=True)
class PreparedDevinTerminal:
    """Prepared native Devin terminal attachment details."""

    session_id: str
    terminal_id: str
    tmux_socket: Path | None
    tmux_target: str | None
    reattached: bool
    cold_resumed: bool = False


def _configured_devin_command(env: Mapping[str, str]) -> str:
    """Return the configured ``devin`` executable name/path from *env*."""
    value = env.get(_DEVIN_PATH_ENV, "").strip()
    return value or _DEFAULT_DEVIN_COMMAND


def resolve_devin_executable(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Resolve the native Devin (``devin``) executable."""
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    command = _configured_devin_command(env)
    resolved = resolve_cli_binary(command, which=which)
    if resolved is None:
        raise click.ClickException(
            "Native Devin requires the 'devin' CLI on PATH. Install it with "
            "`curl -fsSL https://cli.devin.ai/install.sh | bash`, run `devin auth login`, "
            f"or set {_DEVIN_PATH_ENV}=/path/to/devin."
        )
    return resolved


# ---------------------------------------------------------------------------
# Model + effort discovery
# ---------------------------------------------------------------------------


def _run_devin_models_list(
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = _MODEL_LIST_TIMEOUT_S,
) -> _JsonObject:
    """Return the parsed ``devin models list --format json`` payload."""
    executable = resolve_devin_executable(env=env)
    completed = subprocess.run(
        [executable, "models", "list", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=dict(env) if env is not None else None,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise ValueError("Devin model list must be a JSON object")
    return payload


def _variant_ids(family: Mapping[str, object]) -> list[str]:
    """Return the model_uid list for one family row."""
    variants = family.get("variants")
    if not isinstance(variants, list):
        return []
    ids: list[str] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        uid = variant.get("model_uid")
        if isinstance(uid, str) and uid:
            ids.append(uid)
    return ids


def list_devin_cli_model_options(
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = _MODEL_LIST_TIMEOUT_S,
) -> list[_JsonObject]:
    """Discover Devin picker options from the installed CLI.

    Devin's catalog is two-level: a *family* (``claude-opus-5``) owns *variants*
    whose ids encode the reasoning effort (``claude-opus-5-xhigh``) plus serving
    modifiers (``-fast``, ``-priority``, ``-1m``). Omnigent's picker is also
    two-level — a model list plus an effort list — so this returns one option per
    family and :func:`compose_devin_model` recombines the pair at launch.

    Exposing families rather than the ~400 raw variants keeps the picker usable
    and means a new Devin effort rung needs no Omnigent change.

    :returns: Picker options with ``id``, ``displayName``, ``isDefault`` and,
        where the catalog provides them, ``description``, ``contextWindow`` and
        the supported effort rungs under ``efforts``.
    """
    payload = _run_devin_models_list(env=env, timeout_s=timeout_s)
    families = payload.get("families")
    if not isinstance(families, list):
        raise ValueError("Devin model list must contain a families array")
    raw_default = payload.get("default_model")
    default_id = raw_default.strip() if isinstance(raw_default, str) else None
    options: list[_JsonObject] = []
    for family in families:
        if not isinstance(family, dict):
            continue
        slug = family.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        label = family.get("family_label")
        display_name = label.strip() if isinstance(label, str) and label.strip() else slug
        variants = _variant_ids(family)
        option: _JsonObject = {
            "id": slug,
            "displayName": display_name,
            "isDefault": slug == default_id,
        }
        efforts = [effort for effort in DEVIN_EFFORTS if f"{slug}-{effort}" in variants]
        if efforts:
            option["efforts"] = efforts
        aliases = family.get("aliases")
        if isinstance(aliases, list):
            alias_strings = [a for a in aliases if isinstance(a, str) and a]
            if alias_strings:
                option["aliases"] = alias_strings
        first_variant = next(
            (v for v in family.get("variants", []) if isinstance(v, dict)),
            None,
        )
        if first_variant is not None:
            context_window = first_variant.get("max_context_tokens")
            if isinstance(context_window, int) and context_window > 0:
                option["contextWindow"] = context_window
            cost_summary = first_variant.get("cost_summary")
            if isinstance(cost_summary, str) and cost_summary.strip():
                option["description"] = cost_summary.strip()
        options.append(option)
    if not options:
        raise ValueError("Devin model list did not contain any valid families")
    return options


def compose_devin_model(
    model: str | None,
    effort: str | None,
    *,
    known_variants: Sequence[str] | None = None,
) -> str | None:
    """Combine a Devin family slug and an effort rung into a ``--model`` value.

    Devin has no separate effort flag — effort is a suffix on the model id — so
    an Omnigent (model, effort) pair composes into one variant id. When the
    composed variant is not in *known_variants* the bare family slug is returned
    instead, which Devin resolves to that family's default variant. That keeps a
    mismatched pair (e.g. ``gemini-3.8-flash`` with ``max``) working rather than
    passing Devin an id it would reject.

    :param model: Family slug, alias, or an already-composed variant id.
    :param effort: One of :data:`DEVIN_EFFORTS`, or ``None``.
    :param known_variants: Valid variant ids; ``None`` skips validation and
        trusts the composition.
    :returns: The ``--model`` value, or ``None`` when no model was requested.
    """
    if not model:
        return None
    if not effort:
        return model
    # An id that already ends in an effort rung is a full variant — leave it.
    if any(model.endswith(f"-{rung}") for rung in DEVIN_EFFORTS):
        return model
    composed = f"{model}-{effort}"
    if known_variants is None or composed in known_variants:
        return composed
    return model


def devin_model_variants(
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = _MODEL_LIST_TIMEOUT_S,
) -> list[str]:
    """Return every valid Devin variant id, for :func:`compose_devin_model`."""
    payload = _run_devin_models_list(env=env, timeout_s=timeout_s)
    families = payload.get("families")
    if not isinstance(families, list):
        return []
    variants: list[str] = []
    for family in families:
        if isinstance(family, dict):
            variants.extend(_variant_ids(family))
    return variants


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


def build_devin_launch(
    devin_args: Sequence[str],
    *,
    bridge_dir: Path,
    config_path: Path,
    export_file: Path,
    model: str | None = None,
    permission_mode: str | None = None,
    resume_id: str | None = None,
    sandbox: bool = False,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> NativeDevinLaunch:
    """Build the argv for a native Devin TUI process."""
    del bridge_dir  # Reserved: env carries the bridge dir, not argv.
    from omnigent.harnesses.devin_native.bridge import build_devin_launch_args

    executable = resolve_devin_executable(env=env, which=which)
    args = build_devin_launch_args(
        devin_args,
        config_path=config_path,
        export_path_value=export_file,
        model=model,
        permission_mode=permission_mode,
        resume_id=resume_id,
        sandbox=sandbox,
    )
    return NativeDevinLaunch(executable=executable, argv=[executable, *args])


def run_devin_native(
    *,
    server: str | None,
    session_id: str | None,
    extra_args: tuple[str, ...] | None = None,
    devin_args: tuple[str, ...] | None = None,
    resume_picker: bool = False,
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    sandbox: bool = False,
    prompt: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """Launch the Devin TUI in an Omnigent terminal."""
    devin_args = _normalize_extra_args(
        extra_args=extra_args, legacy_args=devin_args, legacy_param="devin_args"
    )
    _preflight_local_tools()
    if server is None:
        raise click.ClickException(
            "Devin requires a resolved Omnigent server URL. The CLI should call "
            "_ensure_backend before run_devin_native."
        )
    resolved_model = resolve_devin_launch_model(model, effort)
    with TemporaryDirectory(prefix="omnigent-devin-native-") as tmpdir:
        spec_path = _materialize_devin_agent_spec(Path(tmpdir), model=resolved_model)
        _run_with_remote_server(
            server.rstrip("/"),
            spec_path,
            session_id=session_id,
            resume_picker=resume_picker,
            devin_args=devin_args,
            model=resolved_model,
            permission_mode=permission_mode,
            sandbox=sandbox,
            prompt=prompt,
            auto_open_conversation=auto_open_conversation,
        )


def resolve_devin_launch_model(model: str | None, effort: str | None) -> str | None:
    """Compose the launch ``--model`` from a model + effort pair.

    Devin has no ``--effort`` flag: effort is a suffix on the model id, so an
    Omnigent (model, effort) pair has to become one variant id before launch.
    Shared by the CLI (``omnigent devin --model … --effort …``) and the runner
    (which reads both off the session snapshot, so a web New-Chat pick composes
    identically).

    The composition is validated against Devin's real catalog when it is
    reachable. A catalog lookup failure (offline, not logged in, slow) falls back
    to the bare family slug rather than guessing a variant id that might not
    exist — Devin always resolves a family to its own default variant, so the
    launch still succeeds with the user's model, just at default effort.

    :param model: Family slug, alias, or an already-composed variant id.
    :param effort: One of :data:`DEVIN_EFFORTS`, or ``None``.
    :returns: The ``--model`` value, or ``None`` when no model was requested.
    """
    if not model:
        return None
    if not effort:
        return model
    try:
        variants: Sequence[str] | None = devin_model_variants()
    except (subprocess.SubprocessError, OSError, ValueError, click.ClickException):
        variants = None
    if variants is None:
        return model
    return compose_devin_model(model, effort, known_variants=variants)


def _materialize_devin_agent_spec(tmpdir: Path, *, model: str | None = None) -> Path:
    """Write the terminal-first agent spec used by ``omnigent devin``."""
    yaml_path = tmpdir / "devin-native-ui.yaml"
    executor: dict[str, str] = {"harness": "devin-native"}
    if model:
        executor["model"] = model
    raw: _JsonObject = {
        "name": _AGENT_NAME,
        "prompt": (
            "Devin is running in the session terminal. The user drives the devin TUI directly."
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
    devin_args: tuple[str, ...],
    model: str | None = None,
    permission_mode: str | None = None,
    sandbox: bool = False,
    prompt: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """Launch Devin on an Omnigent server via a daemon-spawned runner."""
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
            with runner_startup_progress(initial_message="Preparing Devin...") as progress:
                _update_startup_progress(progress, "Connecting to local daemon...")
                _ensure_host_daemon(base_url)
                host_id = load_or_create_host_identity().host_id
                bundle = None if resolved_session_id is not None else _bundle_agent(spec_path)
                prepared = await _prepare_devin_terminal_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    session_id=resolved_session_id,
                    session_bundle=bundle,
                    devin_args=devin_args,
                    model=model,
                    permission_mode=permission_mode,
                    sandbox=sandbox,
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
                echo_native_cold_resume_hint(agent_label="Devin")
            await _attach_terminal_resource(prepared)
            if resolved_session_id is None:
                echo_native_resume_hint(
                    native_command="devin",
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


async def _prepare_devin_terminal_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str | None,
    session_bundle: bytes | None,
    devin_args: tuple[str, ...],
    model: str | None,
    permission_mode: str | None,
    sandbox: bool,
    prompt: str | None,
    host_id: str,
    workspace: str,
    startup_progress: RunnerStartupProgress | None = None,
) -> PreparedDevinTerminal:
    """Create or resume a devin-native session through a daemon runner."""
    persist_args = list(devin_args)
    if model:
        persist_args[:0] = ["--model", model]
    if permission_mode:
        persist_args[:0] = ["--permission-mode", permission_mode]
    if sandbox:
        persist_args.append("--sandbox")
    if prompt:
        persist_args.extend(["--", prompt])
    timeout = httpx.Timeout(30.0, read=120.0)
    async with open_daemon_client(base_url, headers, host_id, timeout=timeout) as client:
        reattached = False
        cold_resumed = False
        fresh_session = session_id is None
        if session_id is None:
            if session_bundle is None:
                raise click.ClickException("Creating a Devin session requires a session bundle.")
            _update_startup_progress(startup_progress, "Creating Devin session...")
            session_id, _ = await asyncio.gather(
                _create_devin_session(
                    client,
                    session_bundle,
                    terminal_launch_args=persist_args or None,
                ),
                wait_for_host_online(client, host_id, timeout_s=_DAEMON_HOST_ONLINE_TIMEOUT_S),
            )
        else:
            _update_startup_progress(startup_progress, "Loading Devin session...")
            payload = await _fetch_devin_session(client, session_id)
            labels = payload.get("labels") if isinstance(payload, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get(_WRAPPER_LABEL_KEY) != _WRAPPER_LABEL_VALUE
            ):
                raise click.ClickException(
                    f"Conversation {session_id!r} is not a devin-native session."
                )
            existing_terminal = await _find_running_devin_terminal(client, session_id)
            if existing_terminal is not None:
                if persist_args:
                    click.echo(
                        "Ignoring Devin launch args for an already-running terminal; "
                        "restart the session terminal to apply them.",
                        err=True,
                    )
                _update_startup_progress(startup_progress, "Devin terminal ready.")
                return PreparedDevinTerminal(
                    session_id=session_id,
                    terminal_id=existing_terminal.terminal_id,
                    tmux_socket=existing_terminal.tmux_socket,
                    tmux_target=existing_terminal.tmux_target,
                    reattached=True,
                )
            cold_resumed = True
            if persist_args:
                _update_startup_progress(startup_progress, "Updating Devin session...")
                resp = await client.patch(
                    f"/v1/sessions/{url_component(session_id)}",
                    json={"terminal_launch_args": persist_args},
                )
                if resp.status_code >= 400:
                    raise click.ClickException(
                        f"Devin session launch config update failed "
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
        _update_startup_progress(startup_progress, "Starting Devin terminal...")
        await _ensure_devin_terminal_on_runner(client, session_id)
        terminal = await _wait_for_devin_terminal_ready(
            client,
            session_id,
            timeout_s=_DAEMON_TERMINAL_READY_TIMEOUT_S,
        )
        _update_startup_progress(startup_progress, "Devin terminal ready.")
    return PreparedDevinTerminal(
        session_id=session_id,
        terminal_id=terminal.terminal_id,
        tmux_socket=terminal.tmux_socket,
        tmux_target=terminal.tmux_target,
        reattached=reattached,
        cold_resumed=cold_resumed,
    )


async def _create_devin_session(
    client: httpx.AsyncClient,
    bundle: bytes,
    *,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """Create a bundled terminal-first devin-native session."""
    metadata: _JsonObject = {"labels": dict(_SESSION_LABELS)}
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("devin-native-ui.tar.gz", bundle, "application/gzip")},
        timeout=120.0,
    )
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Devin session creation failed ({resp.status_code}): {error_text(resp)}"
        )
    body = resp.json()
    new_session_id = body.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise click.ClickException("Devin session creation response did not include session_id.")
    return new_session_id


async def _fetch_devin_session(client: httpx.AsyncClient, session_id: str) -> _JsonObject:
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


async def _ensure_devin_terminal_on_runner(client: httpx.AsyncClient, session_id: str) -> None:
    """Ask the bound runner to ensure the Devin terminal exists."""
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
            f"Devin terminal ensure failed ({resp.status_code}): {error_text(resp)}"
        )


async def _wait_for_devin_terminal_ready(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float,
) -> LaunchedDevinTerminal:
    """Wait until the runner exposes the Devin terminal resource."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        terminal = await _find_running_devin_terminal(client, session_id)
        if terminal is not None:
            return terminal
        await asyncio.sleep(0.2)
    raise click.ClickException(
        f"The runner did not create the Devin terminal for {session_id!r} within {timeout_s:.0f}s."
    )


async def _find_running_devin_terminal(
    client: httpx.AsyncClient,
    session_id: str,
) -> LaunchedDevinTerminal | None:
    """Return the existing running Devin terminal id if present."""
    terminal_id = devin_terminal_resource_id()
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
        raise click.ClickException(f"Failed to fetch Devin terminal ({resp.status_code}): {text}")
    payload = resp.json()
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("running") is False:
        return None
    return _launched_devin_terminal_from_payload(payload)


def _launched_devin_terminal_from_payload(payload: object) -> LaunchedDevinTerminal:
    """Decode terminal launch metadata returned by the runner."""
    if not isinstance(payload, dict):
        raise click.ClickException("Devin terminal launch returned non-object JSON.")
    terminal_id = payload.get("id")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise click.ClickException("Devin terminal launch response did not include terminal id.")
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
    return LaunchedDevinTerminal(
        terminal_id=terminal_id,
        tmux_socket=tmux_socket,
        tmux_target=tmux_target,
    )


async def _attach_terminal_resource(prepared: PreparedDevinTerminal) -> None:
    """Attach the current terminal to the prepared Devin terminal resource."""
    direct_tmux_error = _direct_tmux_unavailable_reason(prepared)
    if direct_tmux_error is not None:
        raise click.ClickException(
            f"Runner-owned Devin terminal requires direct tmux attach, but {direct_tmux_error}"
        )
    if prepared.tmux_socket is None or prepared.tmux_target is None:
        raise click.ClickException("Devin tmux attach metadata was incomplete.")
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


def _direct_tmux_unavailable_reason(prepared: PreparedDevinTerminal) -> str | None:
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
    """Translate resume inputs into a concrete devin-native session id."""
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
    """Show one concise Devin startup milestone when a renderer is active."""
    if startup_progress is not None:
        startup_progress.update(message)


def _preflight_local_tools() -> None:
    """Verify local executables required by the native Devin wrapper."""
    if shutil.which("tmux") is None:
        raise click.ClickException(
            "tmux was not found on local PATH. The native Devin wrapper "
            "attaches to the runner-owned Devin tmux terminal."
        )


def devin_terminal_resource_id() -> str:
    """Return the deterministic terminal resource id for Devin."""
    return terminal_resource_id(_TERMINAL_NAME, _TERMINAL_SESSION_KEY)
