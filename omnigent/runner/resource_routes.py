"""Runner routes for a session's resources: environments, terminals, files, and GitHub."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import mimetypes
import os
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from omnigent.harnesses.claude_native.bridge import ClaudeNativeToolRelay
    from omnigent.harnesses.claude_native.main import ClaudeNativeUcodeConfig
    from omnigent.terminals.registry import TerminalListEntry, TerminalRegistry

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from omnigent._platform import normalize_interactive_shells
from omnigent.entities.session_resources import (
    DEFAULT_ENVIRONMENT_ID,
    SessionResourceView,
    resolve_terminal_entry_by_resource_id,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import native_terminal_name
from omnigent.native.native_coding_agents import (
    native_coding_agent_for_agent_name,
    native_coding_agent_for_terminal_name,
)
from omnigent.runner.app_support import (
    _BodyRequest,
    _client_safe_error_detail,
    _CommentRelayBinding,
    _require_full_native_lock_coverage,
    _ResourceType,
    _SpecEntry,
)
from omnigent.runner.native import (
    _COST_POPUP_REPOP_TASKS,
    _REPL_TERMINAL_NAME,
    _REPL_TERMINAL_SESSION_KEY,
    NativeLaunchContext,
    _auto_create_qwen_terminal,
    _auto_create_repl_terminal,
    _claude_native_bridge_id_for_session,
    _codex_ensure_response_with_policy_notice,
    _ensure_native_terminal,
    _is_runner_owned_antigravity_terminal,
    _is_runner_owned_codex_terminal,
    _log_terminal_lookup_miss,
    _publish_tmux_target_for_bridge,
    _resolved_spec_workdir,
    _unwrap_resolved_spec,
)
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    OMNIGENT_REPL_TERMINAL_ROLE,
    QWEN_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.spec.types import AgentSpec
from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket
from omnigent.terminals.ws_common import WS_CLOSE_TERMINAL_NOT_FOUND
from omnigent.util.json_types import JsonObject as _JsonObject

if TYPE_CHECKING:
    from omnigent.entities.environment_filesystem import FilesystemEntry
    from omnigent.runtime.filesystem_registry import FilesystemRegistry

_logger = logging.getLogger("omnigent.runner.app")


class _EnsureCommentRelayStartedFn(Protocol):
    async def __call__(
        self,
        session_id: str,
        *,
        bridge_id: str | None = None,
        explicit_bridge_dir: Path | None = None,
        await_notify: bool = False,
        session_labels: Mapping[str, str] | None = None,
    ) -> None: ...


@dataclasses.dataclass(frozen=True)
class ResourceRoutes:
    """Resource-route helpers the rest of the runner app calls directly."""

    ensure_native_terminal_for_turn: Callable[[str, str | None], Coroutine[Any, Any, None]]
    require_os_env: Callable[[str], Coroutine[Any, Any, AgentSpec | None]]
    resolve_conversation_id: Callable[[str], Coroutine[Any, Any, str | None]]


def register_resource_routes(
    app: FastAPI,
    *,
    _antigravity_terminal_ensure_locks: dict[str, asyncio.Lock],
    _claude_terminal_ensure_locks: dict[str, asyncio.Lock],
    _codex_terminal_ensure_locks: dict[str, asyncio.Lock],
    _cursor_terminal_ensure_locks: dict[str, asyncio.Lock],
    _devin_terminal_ensure_locks: dict[str, asyncio.Lock],
    _discard_comment_relay: Callable[[str, ClaudeNativeToolRelay], None],
    _ensure_comment_relay_started: _EnsureCommentRelayStartedFn,
    _ensure_session_registered: Callable[[str], Coroutine[Any, Any, None]],
    _goose_terminal_ensure_locks: dict[str, asyncio.Lock],
    _hermes_terminal_ensure_locks: dict[str, asyncio.Lock],
    _kimi_terminal_ensure_locks: dict[str, asyncio.Lock],
    _kiro_terminal_ensure_locks: dict[str, asyncio.Lock],
    _native_pane_names: Callable[[str], set[str]],
    _opencode_terminal_ensure_locks: dict[str, asyncio.Lock],
    _pi_terminal_ensure_locks: dict[str, asyncio.Lock],
    _publish_event: Callable[[str, Mapping[str, object]], None],
    _qwen_terminal_ensure_locks: dict[str, asyncio.Lock],
    _record_session_claude_launch_config: Callable[[str, ClaudeNativeUcodeConfig | None], None],
    _repl_terminal_ensure_locks: dict[str, asyncio.Lock],
    _repop_pending_cost_popup_on_attach: Callable[[str, str, str], Coroutine[Any, Any, None]],
    _resolve_session_agent_spec: Callable[[str], Coroutine[Any, Any, AgentSpec | None]],
    _resolve_session_agent_spec_or_none: Callable[[str], Coroutine[Any, Any, AgentSpec | None]],
    _resolve_session_claude_launch_config: Callable[
        [str], Coroutine[Any, Any, ClaudeNativeUcodeConfig | None]
    ],
    _resolve_session_fs_registry: Callable[[str], Coroutine[Any, Any, FilesystemRegistry | None]],
    _resolve_session_spec_entry: Callable[[str], Coroutine[Any, Any, _SpecEntry | None]],
    _resp_to_conv: dict[str, str],
    _search_registry_for_root: Callable[[Path], FilesystemRegistry],
    _session_comment_relays: dict[str, _CommentRelayBinding],
    auth_token_factory: Callable[[], str | None] | None,
    filesystem_registry: FilesystemRegistry | None,
    resource_registry: SessionResourceRegistry,
    server_client: httpx.AsyncClient,
    terminal_registry: TerminalRegistry | None,
) -> ResourceRoutes:
    """Register the ``/v1/sessions/{id}/resources/...`` routes on *app*.

    The keyword arguments are the runner app's shared session state and helpers.
    """
    from omnigent.runtime.filesystem_registry import detect_git_root

    async def _resolve_conversation_id(response_id: str) -> str | None:
        return _resp_to_conv.get(response_id)

    @app.get("/v1/sessions/{session_id}/resources")
    async def list_session_resources(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        type: str | None = Query(default=None),
    ) -> JSONResponse:
        from omnigent.entities.pagination import paginate_in_memory

        spec = await _resolve_session_agent_spec(session_id)
        full = resource_registry.list_resources(
            session_id,
            resource_type=cast(_ResourceType | None, type),
            agent_spec=spec,
        )
        page = paginate_in_memory(
            full.data,
            id_fn=lambda r: r.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [session_resource_view_to_dict(r) for r in page.data]
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "data": data,
                "first_id": page.first_id,
                "last_id": page.last_id,
                "has_more": page.has_more,
            },
        )

    def _build_typed_list_response(
        session_id: str,
        resource_type: _ResourceType,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> JSONResponse:
        from omnigent.entities.pagination import paginate_in_memory

        filtered = resource_registry.list_resources(
            session_id,
            resource_type=resource_type,
        )
        page = paginate_in_memory(
            filtered.data,
            id_fn=lambda r: r.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [session_resource_view_to_dict(r) for r in page.data]
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "data": data,
                "first_id": page.first_id,
                "last_id": page.last_id,
                "has_more": page.has_more,
            },
        )

    @app.get("/v1/sessions/{session_id}/resources/environments")
    async def list_session_environments(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        return _build_typed_list_response(
            session_id,
            "environment",
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    def _environment_reach(root: str, agent_spec: AgentSpec | None) -> dict[str, object]:
        """Describe what the default environment's file browsing can reach.

        ``unconfined`` reports that no OS-level sandbox is applied, so the
        environment's shell already reads anything the runner can and a
        browser may range beyond the listed roots. ``roots`` always names
        the grants the environment's own file tools reach, workspace first,
        so a caller can anchor on the workspace and label the rest.

        :param root: Resolved absolute environment root.
        :param agent_spec: Agent spec for the session, if any.
        :returns: JSON-ready ``{"unconfined": bool, "roots": [...]}``.
        """
        from omnigent.inner.sandbox import (
            ReachableRoot,
            is_unconfined,
            reach_payload,
            reachable_roots,
            resolve_sandbox,
        )

        root_path = Path(root)
        spec_os_env = getattr(agent_spec, "os_env", None) if agent_spec is not None else None
        if spec_os_env is None:
            # No spec to resolve (dev/standalone): report the root alone
            # rather than guessing a wider reach.
            return reach_payload(
                [ReachableRoot(path=root_path, access="write", origin="cwd", kind="tree")],
                unconfined=False,
            )
        policy = resolve_sandbox(spec_os_env, root_path)
        return reach_payload(reachable_roots(root_path, policy), unconfined=is_unconfined(policy))

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}")
    async def get_session_environment(
        session_id: str,
        environment_id: str,
    ) -> JSONResponse:
        agent_spec = await _resolve_session_agent_spec(session_id)
        resource = resource_registry.get_resource(
            session_id,
            environment_id,
        )
        if resource is None or resource.type != "environment":
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"Environment {environment_id!r} not found",
                    }
                },
            )
        content = session_resource_view_to_dict(resource)
        if environment_id == DEFAULT_ENVIRONMENT_ID:
            root = resource_registry.compute_default_env_root(session_id, agent_spec)
            if root is not None:
                raw_metadata = content.get("metadata")
                metadata: dict[str, object] = (
                    dict(cast(Mapping[str, object], raw_metadata))
                    if isinstance(raw_metadata, Mapping)
                    else {}
                )
                metadata["root"] = root
                home = os.path.expanduser("~")
                if os.path.isabs(home):
                    metadata["home"] = home
                metadata["reachable"] = _environment_reach(root, agent_spec)
                content = {**content, "metadata": metadata}
        return JSONResponse(
            status_code=200,
            content=content,
        )

    @app.get("/v1/sessions/{session_id}/resources/terminals")
    async def list_session_terminals(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        return _build_typed_list_response(
            session_id,
            "terminal",
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    @app.post("/v1/sessions/{session_id}/resources/terminals")
    async def create_session_terminal(
        session_id: str,
        request: Request,
    ) -> JSONResponse:
        body = await request.json()
        terminal_name = body.get("terminal")
        session_key = body.get("session_key")
        if not terminal_name or not session_key:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": ("'terminal' and 'session_key' are required"),
                    }
                },
            )

        _ensure_agent = native_coding_agent_for_terminal_name(terminal_name)
        if (
            body.get("ensure_native_terminal")
            and _ensure_agent is not None
            and session_key == "main"
            # antigravity's ensure arm declined to auto-create when the request
            # carried a spec (the CLI-wrapper launch path owns that case).
            and not (terminal_name == "antigravity" and body.get("spec"))
        ):
            # Each native harness contributes only the ensure hooks that differ
            # from the uniform base; a single _ensure_native_terminal call runs
            # them. The 4 uniform harnesses (goose/kiro/hermes/qwen) need only the
            # base context; pi/opencode/cursor/kimi/claude resolve an agent spec
            # via build_context; codex/antigravity add an ownership check (and
            # codex a one-shot policy-notice response wrap).
            _ensure_locks = _require_full_native_lock_coverage(
                {
                    "claude": _claude_terminal_ensure_locks,
                    "codex": _codex_terminal_ensure_locks,
                    "pi": _pi_terminal_ensure_locks,
                    "cursor": _cursor_terminal_ensure_locks,
                    "kiro": _kiro_terminal_ensure_locks,
                    "antigravity": _antigravity_terminal_ensure_locks,
                    "opencode": _opencode_terminal_ensure_locks,
                    "goose": _goose_terminal_ensure_locks,
                    "hermes": _hermes_terminal_ensure_locks,
                    "qwen": _qwen_terminal_ensure_locks,
                    "kimi": _kimi_terminal_ensure_locks,
                    "devin": _devin_terminal_ensure_locks,
                }
            )[_ensure_agent.key]
            persist_resource_event = body.get("persist_resource_event") is not False

            def _publish_ensure_event(event_session_id: str, event: _JsonObject) -> None:
                """Publish terminal readiness, optionally without durable history."""
                event_type = event.get("type")
                if not persist_resource_event and event_type in (
                    "session.resource.created",
                    "session.resource.deleted",
                ):
                    event = {**event, "persist_resource_event": False}
                _publish_event(event_session_id, event)

            _ensure_ctx = NativeLaunchContext(
                session_id=session_id,
                resource_registry=resource_registry,
                publish_event=_publish_ensure_event,
                server_client=server_client,
                event_dispatcher=getattr(app.state, "runner_event_dispatcher", None),
                ensure_comment_relay=_ensure_comment_relay_started,
            )
            _ensure_build: (
                Callable[[NativeLaunchContext], Awaitable[NativeLaunchContext]] | None
            ) = None
            _ensure_is_owned: (
                Callable[[SessionResourceRegistry, SessionResourceView], bool] | None
            ) = None
            _ensure_finalize: Callable[[SessionResourceView], JSONResponse] | None = None
            _ensure_conflict: str | None = None

            if terminal_name == "claude":

                async def _claude_ensure_build(
                    ctx: NativeLaunchContext,
                ) -> NativeLaunchContext:
                    # Resolve the entry, not just the spec: a sub-agent session
                    # must launch against its own bundle dir so --plugin-dir
                    # carries its skills rather than the parent's.
                    claude_entry = await _resolve_session_spec_entry(session_id)
                    claude_agent_spec = _unwrap_resolved_spec(claude_entry)
                    return dataclasses.replace(
                        ctx,
                        agent_spec=claude_entry,
                        bundle_dir=_resolved_spec_workdir(claude_entry),
                        agent_name=getattr(claude_agent_spec, "name", None),
                        skills_filter=getattr(claude_agent_spec, "skills_filter", "all"),
                        auth_token_factory=auth_token_factory,
                        resolve_launch_config=lambda: _resolve_session_claude_launch_config(
                            session_id
                        ),
                        record_launch_config=_record_session_claude_launch_config,
                    )

                _ensure_build = _claude_ensure_build

            elif terminal_name == "codex":

                async def _codex_ensure_build(
                    ctx: NativeLaunchContext,
                ) -> NativeLaunchContext:
                    # Entry, not bare spec: a sub-agent session's CODEX_HOME
                    # skills must come from its own bundle dir.
                    codex_entry = await _resolve_session_spec_entry(session_id)
                    codex_agent_spec = _unwrap_resolved_spec(codex_entry)
                    return dataclasses.replace(
                        ctx,
                        agent_spec=codex_entry,
                        bundle_dir=_resolved_spec_workdir(codex_entry),
                        skills_filter=getattr(codex_agent_spec, "skills_filter", "all"),
                    )

                _ensure_build = _codex_ensure_build
                _ensure_is_owned = _is_runner_owned_codex_terminal
                _ensure_finalize = lambda view: _codex_ensure_response_with_policy_notice(  # noqa: E731
                    session_id, view
                )
                _ensure_conflict = (
                    "Existing codex terminal is not a runner-owned Codex TUI "
                    "and could not be closed."
                )

            elif terminal_name == "antigravity":
                _ensure_is_owned = _is_runner_owned_antigravity_terminal
                _ensure_conflict = (
                    "Existing antigravity terminal is not a runner-owned agy TUI "
                    "and could not be closed."
                )

            elif terminal_name in ("pi", "opencode"):
                # pi/opencode resolve the spec unwrapped — a resolution error
                # surfaces as a terminal-start error (the resolver does not
                # swallow it).
                async def _spec_ensure_build(
                    ctx: NativeLaunchContext,
                ) -> NativeLaunchContext:
                    return dataclasses.replace(
                        ctx, agent_spec=await _resolve_session_agent_spec(session_id)
                    )

                _ensure_build = _spec_ensure_build

            elif terminal_name in ("cursor", "kimi", "devin"):

                async def _spec_or_none_ensure_build(
                    ctx: NativeLaunchContext,
                ) -> NativeLaunchContext:
                    return dataclasses.replace(
                        ctx, agent_spec=await _resolve_session_agent_spec_or_none(session_id)
                    )

                _ensure_build = _spec_or_none_ensure_build

            _ensure_result = await _ensure_native_terminal(
                terminal_name,
                _ensure_ctx,
                ensure_locks=_ensure_locks,
                build_context=_ensure_build,
                is_owned=_ensure_is_owned,
                conflict_message=_ensure_conflict,
                finalize=_ensure_finalize,
            )
            if _ensure_result is not None:
                return _ensure_result

        from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

        cwd_override = body.get("cwd")
        sandbox_override = body.get("sandbox")
        spec = body.get("spec") or {}

        agent_spec = await _resolve_session_agent_spec(session_id)
        agent_os_env = getattr(agent_spec, "os_env", None) if agent_spec is not None else None

        declared_terminal = None
        terminals_map = {}
        if agent_spec is not None:
            terminals_map = getattr(agent_spec, "terminals", None) or {}
            declared_terminal = terminals_map.get(terminal_name)

        if (
            declared_terminal is None
            and native_coding_agent_for_agent_name(getattr(agent_spec, "name", None)) is not None
            and normalize_interactive_shells([terminal_name])
        ):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": ErrorCode.INVALID_INPUT,
                        "message": (
                            f"Shell {terminal_name!r} is not available on this host. "
                            f"Available shells: {list(terminals_map) or 'none'}."
                        ),
                    }
                },
            )

        if declared_terminal is not None:
            from omnigent.tools.builtins.sys_terminal import (
                _materialize_terminal_spec_for_launch,
                _synthesize_parent_os_env,
            )

            default_root = resource_registry.compute_default_env_root(session_id, agent_spec)
            env_spec = _materialize_terminal_spec_for_launch(declared_terminal, default_root)
            agent_os_env = _synthesize_parent_os_env(agent_os_env, default_root)
            cwd_override = cwd_override or spec.get("cwd")
        else:
            spec_cwd = spec.get("cwd")
            if spec_cwd is None or spec_cwd in (".", "./"):
                spec_cwd = resource_registry.compute_default_env_root(session_id, agent_spec)
            env_spec = TerminalEnvSpec(
                os_env=OSEnvSpec(
                    type=spec.get("os_env_type", "caller_process"),
                    cwd=spec_cwd,
                    sandbox=(agent_os_env.sandbox if agent_os_env is not None else None),
                ),
                command=spec.get("command", "bash"),
                args=spec.get("args", []),
                env=spec.get("env", {}),
                scrollback=spec.get("scrollback", 10000),
                tmux_allow_passthrough=bool(spec.get("tmux_allow_passthrough", False)),
                tmux_start_on_attach=bool(spec.get("tmux_start_on_attach", False)),
            )
        bridge_inject = bool(body.get("bridge_inject_dir"))
        bridge_id = session_id
        # Set only when this launch installed the relay, so a failure rolls
        # back what it started and leaves a relay that was already serving
        # the session (or that another path installed meanwhile) alone.
        launched_relay: ClaudeNativeToolRelay | None = None
        if bridge_inject:
            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client,
                session_id=session_id,
            )
            relay_before = _session_comment_relays.get(session_id)
            await _ensure_comment_relay_started(session_id, bridge_id=bridge_id)
            relay_after = _session_comment_relays.get(session_id)
            if relay_after is not None and (
                relay_before is None or relay_before.relay is not relay_after.relay
            ):
                launched_relay = relay_after.relay

        try:
            launch_method = (
                resource_registry.launch_required_terminal
                if bridge_inject
                else resource_registry.launch_auxiliary_terminal
            )
            resource_view = await launch_method(
                session_id=session_id,
                terminal_name=terminal_name,
                session_key=session_key,
                spec=env_spec,
                cwd_override=cwd_override,
                sandbox_override=sandbox_override,
                parent_os_env=agent_os_env,
                resource_role=(CLAUDE_NATIVE_TERMINAL_ROLE if bridge_inject else None),
            )
        except RuntimeError as exc:
            if launched_relay is not None:
                _discard_comment_relay(session_id, launched_relay)
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "terminal_launch_failed",
                        "message": _client_safe_error_detail(exc, context="terminal launch"),
                    }
                },
            )

        if bridge_inject:
            _publish_tmux_target_for_bridge(
                resource_registry=resource_registry,
                session_id=session_id,
                bridge_id=bridge_id,
                terminal_name=terminal_name,
                session_key=session_key,
            )

        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource_view),
        )

    async def _ensure_native_terminal_for_turn(conv_id: str, harness_name: str | None) -> None:
        """Re-create a reaped native pane before forwarding a turn (self-heal).

        The native-pane idle reaper may reclaim an idle pane while a session sits
        between turns. ``NativeServerHarness.run_turn`` forwards into the live
        pane and assumes it exists, so a turn arriving WITHOUT a client handshake
        (a sub-agent or API forward to a long-idle session) would otherwise inject
        into a dead tmux target and lose the message. This re-ensures the pane
        first. Idempotent: a no-op when the harness is not a native CLI harness or
        the pane is already live. Reuses ``create_session_terminal``'s
        ``ensure_native_terminal`` path. Healing restores a live pane, not the
        CLI's in-context history: a harness that records a resumable chat id may
        relaunch with its own ``--resume``, but continuity is best-effort, and a
        harness without one (kimi — exempt from the reaper, so this only fires
        for a crashed pane) always restarts a fresh TUI. Either way the prior
        turns are guaranteed only in the server transcript.

        Detection has two layers: (1) the reaper POPPING the registry entry
        when it reaps (``registry.close()`` -> ``get()`` returns ``None``),
        and (2) an ``is_alive()`` probe when the registry entry exists, catching
        crashed-but-registered panes (tmux killed externally without
        ``close()``). The probe runs only when a turn arrives, not on a
        poll. Every native short-name this can target has a matching
        ``ensure_native_terminal`` branch in ``create_session_terminal``
        (kept in lockstep with ``harness_aliases.NATIVE_HARNESSES``).
        """
        terminal_name = native_terminal_name(harness_name)
        if terminal_name is None:
            return
        terminal_registry = resource_registry.terminal_registry if resource_registry else None
        if terminal_registry is None:
            return
        instance = terminal_registry.get(conv_id, terminal_name, "main")
        if instance is not None:
            if await instance.is_alive():
                return  # pane is registered and alive — nothing to heal
            _logger.info(
                "native pane registered but dead for conv=%s harness=%s; closing stale entry",
                conv_id,
                harness_name,
                extra={"session_id": conv_id},
            )
            # Re-check the registry before closing: a concurrent ensure/recreate
            # path may have already replaced this entry with a live pane between
            # our get() and now.  Only close if the registry still points at the
            # same dead instance we just probed.
            current = terminal_registry.get(conv_id, terminal_name, "main")
            if current is instance:
                try:
                    await terminal_registry.close(conv_id, terminal_name, "main")
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — cleanup is best-effort
                    _logger.warning(
                        "failed to close stale native pane for conv=%s; proceeding to re-create",
                        conv_id,
                        exc_info=True,
                        extra={"session_id": conv_id},
                    )
            else:
                _logger.info(
                    "stale entry already replaced for conv=%s; skipping close",
                    conv_id,
                    extra={"session_id": conv_id},
                )
        _logger.info(
            "native pane missing for conv=%s harness=%s; re-ensuring before turn (#1349)",
            conv_id,
            harness_name,
            extra={"session_id": conv_id},
        )
        try:
            resp = await create_session_terminal(
                conv_id,
                cast(
                    Request,
                    _BodyRequest(
                        {
                            "terminal": terminal_name,
                            "session_key": "main",
                            "ensure_native_terminal": True,
                        }
                    ),
                ),
            )
        except Exception:
            _logger.exception(
                "native pane self-heal failed for conv=%s",
                conv_id,
                extra={"session_id": conv_id},
            )
            return
        status = getattr(resp, "status_code", 200)
        if status >= 400:
            _logger.warning(
                "native pane self-heal returned status %s for conv=%s (%s)",
                status,
                conv_id,
                terminal_name,
                extra={"session_id": conv_id},
            )

    @app.get("/v1/sessions/{session_id}/resources/terminals/{terminal_id}")
    async def get_session_terminal(
        session_id: str,
        terminal_id: str,
    ) -> JSONResponse:
        resource = await resource_registry.get_terminal_resource(
            session_id,
            terminal_id,
        )
        if resource is None:
            _log_terminal_lookup_miss(resource_registry, session_id, terminal_id)
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (f"Terminal {terminal_id!r} not found"),
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource),
        )

    @app.get("/v1/sessions/{session_id}/sign-in-link")
    async def get_session_sign_in_link(session_id: str) -> JSONResponse:
        """
        Return the sign-in prompt a session's terminal is showing right now, if any.

        A launcher wrapper can park a native pane on a device-style sign-in
        (an address to open, often with a code) before the agent runs. The
        address is bound to that launcher process, so a link saved in an
        earlier error card goes stale once the process moves on. The web asks
        here at click time and opens whatever the pane shows now.

        :param session_id: Session/conversation id.
        Only the native agent's own pane is read (see ``_native_pane_names``):
        another terminal in the session must not supply the address.

        :returns: ``{"pending": true, "url", "code", "terminal_id"}`` when the
            running agent pane shows a prompt; ``{"pending": false}`` otherwise.
        """
        from omnigent.harnesses.diagnostics import detect_sign_in_prompt

        registry = resource_registry.terminal_registry
        entries = registry.list_for_conversation(session_id) if registry is not None else []
        pane_names = _native_pane_names(session_id)
        for entry in entries:
            if entry.terminal_name not in pane_names or not entry.instance.running:
                continue
            # Wrapped rows joined: the address is far wider than the pane.
            result = await entry.instance.read(join_wrapped=True)
            screen = result.get("screen") if isinstance(result, dict) else None
            prompt = detect_sign_in_prompt(screen if isinstance(screen, str) else None)
            if prompt is None:
                continue
            return JSONResponse(
                status_code=200,
                content={
                    "pending": True,
                    "url": prompt.url,
                    "code": prompt.code,
                    "terminal_id": terminal_resource_id(entry.terminal_name, entry.session_key),
                },
            )
        return JSONResponse(status_code=200, content={"pending": False, "url": None, "code": None})

    @app.post("/v1/sessions/{session_id}/resources/terminals/{terminal_id}/transfer")
    async def transfer_session_terminal(
        session_id: str,
        terminal_id: str,
        request: Request,
    ) -> JSONResponse:
        body = await request.json()
        target_session_id = body.get("target_session_id") if isinstance(body, dict) else None
        if not isinstance(target_session_id, str) or not target_session_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'target_session_id' is required",
                    }
                },
            )
        try:
            resource = await resource_registry.transfer_terminal(
                source_session_id=session_id,
                target_session_id=target_session_id,
                terminal_id=terminal_id,
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "resource_conflict",
                        "message": _client_safe_error_detail(exc, context="terminal transfer"),
                    }
                },
            )
        if resource is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"Terminal {terminal_id!r} not found",
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource),
        )

    @app.delete("/v1/sessions/{session_id}/resources/terminals/{terminal_id}")
    async def delete_session_terminal(
        session_id: str,
        terminal_id: str,
    ) -> JSONResponse:
        closed = await resource_registry.close_terminal(
            session_id,
            terminal_id,
        )
        if not closed:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (f"Terminal {terminal_id!r} not found"),
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "id": terminal_id,
                "object": "session.resource.deleted",
                "deleted": True,
            },
        )

    async def _recreate_repl_terminal(
        session_id: str, terminal_id: str
    ) -> TerminalListEntry | None:
        if resource_registry is None or resource_registry.terminal_registry is None:
            return None
        registry = resource_registry.terminal_registry
        lock = _repl_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing = registry.get(session_id, _REPL_TERMINAL_NAME, _REPL_TERMINAL_SESSION_KEY)
            if existing is None or not existing.running or not await existing.is_alive():
                await registry.close(session_id, _REPL_TERMINAL_NAME, _REPL_TERMINAL_SESSION_KEY)
                try:
                    repl_agent_spec = await _resolve_session_agent_spec(session_id)
                except OmnigentError:
                    repl_agent_spec = None
                try:
                    await _auto_create_repl_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                        agent_spec=repl_agent_spec,
                    )
                except Exception:
                    _logger.exception(
                        "Failed to recreate omnigent REPL terminal for %s",
                        session_id,
                    )
                    return None
        return resolve_terminal_entry_by_resource_id(session_id, terminal_id, registry)

    async def _recreate_qwen_terminal(
        session_id: str, terminal_id: str
    ) -> TerminalListEntry | None:
        if resource_registry is None or resource_registry.terminal_registry is None:
            return None
        registry = resource_registry.terminal_registry
        lock = _qwen_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing = registry.get(session_id, "qwen", "main")
            if existing is None or not existing.running or not await existing.is_alive():
                await registry.close(session_id, "qwen", "main")
                try:
                    await _auto_create_qwen_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                        ensure_comment_relay=_ensure_comment_relay_started,
                    )
                except Exception:
                    _logger.exception(
                        "Failed to recreate omnigent qwen terminal for %s",
                        session_id,
                    )
                    return None
        return resolve_terminal_entry_by_resource_id(session_id, terminal_id, registry)

    @app.websocket("/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach")
    async def terminal_resource_attach_ws(
        websocket: WebSocket,
        session_id: str,
        terminal_id: str,
        read_only: bool = Query(default=False),
    ) -> None:
        await websocket.accept()
        entry = resolve_terminal_entry_by_resource_id(
            session_id,
            terminal_id,
            terminal_registry,
        )
        terminal_role = (
            resource_registry.terminal_resource_role(session_id, terminal_id)
            if resource_registry is not None
            else None
        )
        if entry is None or not entry.instance.running or not await entry.instance.is_alive():
            if terminal_role == OMNIGENT_REPL_TERMINAL_ROLE:
                entry = await _recreate_repl_terminal(session_id, terminal_id)
            elif terminal_role == QWEN_NATIVE_TERMINAL_ROLE:
                entry = await _recreate_qwen_terminal(session_id, terminal_id)
            else:
                entry = None
            if entry is None:
                await websocket.close(
                    code=WS_CLOSE_TERMINAL_NOT_FOUND,
                    reason="terminal resource not found or not running",
                )
                return
        _repop_task = asyncio.create_task(
            _repop_pending_cost_popup_on_attach(
                session_id,
                str(entry.instance.socket_path),
                entry.instance.tmux_target,
            )
        )
        _COST_POPUP_REPOP_TASKS.add(_repop_task)
        _repop_task.add_done_callback(_COST_POPUP_REPOP_TASKS.discard)
        await bridge_tmux_control_to_websocket(
            websocket,
            socket_path=str(entry.instance.socket_path),
            tmux_target=entry.instance.tmux_target,
            read_only=read_only,
            on_client_interaction=entry.instance.note_client_interaction,
        )

    # Reused by the loopback direct-attach listener (see
    # ``omnigent.runner.direct_attach``): same attach handler served on a
    # token-gated 127.0.0.1 port so a same-machine browser can skip the
    # server relay.
    app.state.terminal_attach_handler = terminal_resource_attach_ws

    async def _require_os_env(session_id: str) -> AgentSpec | None:
        spec = await _resolve_session_agent_spec(session_id)
        if spec is not None and getattr(spec, "os_env", None) is None:
            raise HTTPException(
                status_code=404,
                detail="Session agent has no os_env configured; filesystem API unavailable.",
            )
        return spec

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/filesystem")
    async def list_environment_root(
        session_id: str,
        environment_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        await _require_os_env(session_id)
        return await _fs_list_or_read(
            session_id,
            environment_id,
            "",
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/search")
    async def search_environment_files(
        session_id: str,
        environment_id: str,
        q: str = Query(min_length=1, pattern=r".*\S.*"),
        include: str | None = Query(default=None),
        exclude: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> JSONResponse:
        return await _fs_search(
            session_id,
            environment_id,
            "",
            q=q,
            include=include,
            exclude=exclude,
            limit=limit,
        )

    @app.get(
        "/v1/sessions/{session_id}/resources/environments/{environment_id}/search/{path:path}"
    )
    async def search_environment_files_under(
        session_id: str,
        environment_id: str,
        path: str,
        q: str = Query(min_length=1, pattern=r".*\S.*"),
        include: str | None = Query(default=None),
        exclude: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> JSONResponse:
        """Search under a directory, so results match what the tree shows."""
        return await _fs_search(
            session_id,
            environment_id,
            path,
            q=q,
            include=include,
            exclude=exclude,
            limit=limit,
        )

    async def _fs_search(
        session_id: str,
        environment_id: str,
        path: str,
        *,
        q: str,
        include: str | None,
        exclude: str | None,
        limit: int,
    ) -> JSONResponse:
        import asyncio as _asyncio

        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
            _validate_path,
            index_search,
            merge_entries,
            split_glob_list,
        )

        include_patterns = split_glob_list(include)
        exclude_patterns = split_glob_list(exclude)

        agent_spec = await _require_os_env(session_id)  # also resolves spec
        await _ensure_session_registered(session_id)
        env = resource_registry.resolve_environment(session_id, environment_id, agent_spec)
        fs = CallerProcessFilesystem(env)

        # The budgeted walk is the only source for ignored files (and, until a
        # ``git status`` has run, untracked ones), so it always runs. Alongside
        # it, git's index adds every tracked file in one read however large the
        # repo, and the Changed tab's latest ``git status`` adds untracked files
        # past the budget. Absolute (browse-anywhere) paths have no registry.
        walk = _asyncio.ensure_future(
            fs.search_files(
                q,
                path=path,
                include=include_patterns,
                exclude=exclude_patterns,
                limit=limit,
            )
        )
        indexed: list[FilesystemEntry] | None = None
        try:
            registry = None
            if not fs._absolute(path):
                env_root = fs._resolve("")
                registry = await _resolve_session_fs_registry(session_id)
                if (
                    registry is None
                    or registry.cwd != env_root
                    or registry.git_root != detect_git_root(env_root)
                ):
                    # The session registry watches a different tree than this
                    # walk covers, or a repository boundary moved since it was
                    # built; either way its index would answer for the wrong
                    # files. Consult one rooted where the search actually runs.
                    registry = _search_registry_for_root(env_root)
            if registry is not None:
                indexed = await _asyncio.to_thread(
                    index_search,
                    registry,
                    fs._resolve(path),
                    _validate_path(path) if path else "",
                    q,
                    include=include_patterns,
                    exclude=exclude_patterns,
                    limit=limit,
                )
        except BaseException:
            walk.cancel()
            raise
        entries, truncated = await walk
        if indexed is not None:
            entries = merge_entries(indexed, entries, limit)
        data = [_fs_entry_to_dict(e) for e in entries]
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "base": str(fs._resolve(path)),
                "data": data,
                "has_more": len(entries) >= limit,
                # The scan budget ran out before the tree did, so "no matches"
                # here would be a lie — the caller must be able to say so.
                "truncated": truncated,
            },
        )

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/changes")
    async def list_filesystem_changes(
        session_id: str,
        environment_id: str,  # noqa: ARG001
    ) -> JSONResponse:
        import asyncio as _asyncio

        from omnigent.runtime.filesystem_registry import GitStatusUnavailable

        await _require_os_env(session_id)
        await _ensure_session_registered(session_id)
        session_registry = await _resolve_session_fs_registry(session_id)
        try:
            # ``list_changed_files`` shells out to ``git status`` synchronously,
            # which on a large repo (cold untracked cache) can take seconds.
            # Offload to a thread so it never blocks the event loop — a blocked
            # loop can't answer the server's runner-stream relay probe and the
            # session's first turn 503s with runner_unavailable.
            raw_changes = (
                await _asyncio.to_thread(
                    session_registry.list_changed_files,
                    session_id,
                    limit=10_000,
                )
                if session_registry is not None
                else []
            )
        except GitStatusUnavailable as exc:
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "git_status_failed", "message": exc.reason}},
            )
        data = [
            {
                "object": "session.environment.filesystem.entry",
                "path": rec["path"],
                "name": rec["path"].split("/")[-1],
                "status": rec["status"],
                "bytes": rec.get("bytes"),
                "modified_at": rec.get("modified_at"),
                "lines_added": rec.get("lines_added"),
                "lines_removed": rec.get("lines_removed"),
            }
            for rec in raw_changes
        ]
        return JSONResponse(
            status_code=200,
            content={"object": "list", "data": data, "has_more": False},
        )

    @app.get(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/diff/{relative_path:path}"
    )
    async def read_environment_file_diff(
        session_id: str,
        environment_id: str,
        relative_path: str,
    ) -> JSONResponse:
        agent_spec = await _require_os_env(session_id)
        await _ensure_session_registered(session_id)
        session_registry = await _resolve_session_fs_registry(session_id)

        from omnigent.entities.environment_filesystem import InvalidPath
        from omnigent.runner.environment_filesystem import _validate_path

        try:
            relative_path = _validate_path(relative_path)
        except InvalidPath as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_path",
                        "message": str(exc),
                    }
                },
            )
        if not relative_path:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_path",
                        "message": "Cannot diff the environment root",
                    }
                },
            )

        import asyncio as _asyncio

        from omnigent.runtime.filesystem_registry import GitStatusUnavailable

        try:
            # Offloaded like list_filesystem_changes: get_changed_file shells out
            # to git (status + show) synchronously, so keep it off the loop.
            record = (
                await _asyncio.to_thread(
                    session_registry.get_changed_file, session_id, relative_path
                )
                if session_registry is not None
                else None
            )
        except GitStatusUnavailable as exc:
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "git_status_failed", "message": exc.reason}},
            )
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (
                            f"Path {relative_path!r} is not in the "
                            "changed-files registry for this session"
                        ),
                    }
                },
            )
        is_deleted = record.get("status") == "deleted"

        before: str | None = (
            await _asyncio.to_thread(session_registry.get_baseline, relative_path)
            if session_registry is not None
            else None
        )

        from omnigent.runner.environment_filesystem import CallerProcessFilesystem

        after: str | None = None
        if not is_deleted:
            env = resource_registry.resolve_environment(session_id, environment_id, agent_spec)
            fs = CallerProcessFilesystem(env)
            content = await fs.read(relative_path, limit=None)
            after = content.data.decode(content.encoding or "utf-8", errors="replace")

        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.file_diff",
                "path": relative_path,
                "before": before,
                "after": after,
            },
        )

    # ── GitHub integration (read-only): PR metadata + the PR's files / diff ──
    # The list and patch come from the ``gh`` CLI (the PR's "Files changed");
    # only the per-file expand-context reader uses ``git show``. See
    # omnigent.runner.github_resource. Each shells out synchronously, so it is
    # offloaded to a thread like the changed-files / diff routes above (a blocked
    # loop 503s the session).

    async def _github_workspace_root(session_id: str) -> str:
        """Resolve the workspace root for GitHub routes, or 404 when headless."""
        agent_spec = await _require_os_env(session_id)
        root = resource_registry.compute_default_env_root(session_id, agent_spec)
        if root is None:
            raise HTTPException(
                status_code=404,
                detail="Session has no filesystem; GitHub API unavailable.",
            )
        return root

    async def _github_call(session_id: str, operation: str, **kwargs: Any) -> JSONResponse:
        from omnigent.runner import github_resource

        root = await _github_workspace_root(session_id)
        function = getattr(github_resource, operation)
        try:
            result = await asyncio.to_thread(function, root, session_id=session_id, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(status_code=200, content=result)

    @app.get("/v1/sessions/{session_id}/resources/github")
    async def read_github_info(session_id: str, pr_url: str | None = None) -> JSONResponse:
        return await _github_call(session_id, "github_info", pr_url=pr_url)

    @app.get("/v1/sessions/{session_id}/resources/github/changes")
    async def read_github_changes(session_id: str, pr_url: str | None = None) -> JSONResponse:
        return await _github_call(session_id, "github_changed_files", pr_url=pr_url)

    @app.get("/v1/sessions/{session_id}/resources/github/diff")
    async def read_github_pr_diff(session_id: str, pr_url: str | None = None) -> JSONResponse:
        return await _github_call(session_id, "github_pr_diff", pr_url=pr_url)

    @app.get("/v1/sessions/{session_id}/resources/github/diff/{relative_path:path}")
    async def read_github_file_diff(
        session_id: str,
        relative_path: str,
        base: str | None = None,
        pr_url: str | None = None,
        previous_path: str | None = None,
        head_sha: str | None = None,
        base_sha: str | None = None,
    ) -> JSONResponse:
        if relative_path.startswith("/") or any(
            seg in ("", "..") for seg in relative_path.split("/")
        ):
            raise HTTPException(status_code=400, detail="Invalid path")
        return await _github_call(
            session_id,
            "github_file_diff",
            base=base or "",
            path=relative_path,
            pr_url=pr_url,
            previous_path=previous_path,
            head_sha=head_sha,
            base_sha=base_sha,
        )

    @app.post("/v1/sessions/{session_id}/resources/github/prs")
    async def update_github_pr_route(session_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("url"), str):
            raise HTTPException(status_code=400, detail="Expected a pull request URL")
        return await _github_call(
            session_id, "update_session_pr", url=body["url"], action=body.get("action", "attach")
        )

    @app.post("/v1/sessions/{session_id}/resources/github/preferences")
    async def set_github_preference_route(session_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        return await _github_call(
            session_id,
            "set_github_preference",
            account=body.get("account"),
            remote=body.get("remote"),
            pr_url=body.get("pr_url"),
        )

    @app.get(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def read_or_list_environment_path(
        session_id: str,
        environment_id: str,
        relative_path: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        download: bool = False,
        # ``reach`` admits a workspace symlink whose target lies outside the
        # workspace but within the environment's reach. Only the server sends
        # it, for a caller who may browse that target by absolute path.
        scope: str = Query(default="workspace", pattern="^(workspace|reach)$"),
    ) -> Response:
        await _require_os_env(session_id)
        follow_outward_links = scope == "reach"
        if download:
            return await _fs_download(
                session_id,
                environment_id,
                relative_path,
                follow_outward_links=follow_outward_links,
            )
        return await _fs_list_or_read(
            session_id,
            environment_id,
            relative_path,
            limit=limit,
            after=after,
            before=before,
            order=order,
            follow_outward_links=follow_outward_links,
        )

    @app.put(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def write_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import CallerProcessFilesystem

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        fs = CallerProcessFilesystem(env)
        body = await request.json()
        content_str = body.get("content", "")
        encoding = body.get("encoding", "utf-8")
        create_parents = body.get("create_parents", True)
        content_bytes = content_str.encode(encoding)
        try:
            existing = await fs.read(relative_path, limit=None)
            if existing.encoding and filesystem_registry is not None:
                filesystem_registry.seed_snapshot(
                    relative_path,
                    existing.data.decode(existing.encoding, errors="replace"),
                    session_id=session_id,
                )
        except Exception:  # noqa: BLE001
            pass
        result = await fs.write(
            relative_path,
            content_bytes,
            create_parents=create_parents,
        )
        if filesystem_registry is not None:
            filesystem_registry.record_change(relative_path, result.operation, session_id)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.write_result",
                "operation": result.operation,
                "path": result.path,
                "created": result.created,
                "bytes_written": result.bytes_written,
                "entry": _fs_entry_to_dict(result.entry) if result.entry else None,
            },
        )

    @app.patch(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def edit_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> JSONResponse:
        from omnigent.entities.environment_filesystem import TextEditRequest
        from omnigent.runner.environment_filesystem import CallerProcessFilesystem

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        fs = CallerProcessFilesystem(env)
        try:
            existing = await fs.read(relative_path, limit=None)
            if existing.encoding and filesystem_registry is not None:
                filesystem_registry.seed_snapshot(
                    relative_path,
                    existing.data.decode(existing.encoding, errors="replace"),
                    session_id=session_id,
                )
        except Exception:  # noqa: BLE001
            pass
        body = await request.json()
        edit_req = TextEditRequest(
            old_text=body.get("old_text"),
            new_text=body.get("new_text"),
            replace_all=body.get("replace_all", False),
        )
        result = await fs.edit_text(relative_path, edit_req)
        if filesystem_registry is not None:
            filesystem_registry.record_change(relative_path, result.operation, session_id)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.edit_result",
                "operation": result.operation,
                "path": result.path,
                "replacements": result.replacements,
                "bytes_before": result.bytes_before,
                "bytes_after": result.bytes_after,
                "entry": _fs_entry_to_dict(result.entry) if result.entry else None,
            },
        )

    @app.delete(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def delete_environment_path(
        session_id: str,
        environment_id: str,
        relative_path: str,
        recursive: bool = Query(default=False),
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import CallerProcessFilesystem

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        fs = CallerProcessFilesystem(env)
        result = await fs.delete(relative_path, recursive=recursive)
        if filesystem_registry is not None and result.type == "file":
            filesystem_registry.record_change(relative_path, "deleted", session_id)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.delete_result",
                "operation": result.operation,
                "path": result.path,
                "deleted": result.deleted,
                "type": result.type,
                "bytes_deleted": result.bytes_deleted,
                "entries_deleted": result.entries_deleted,
            },
        )

    async def _fs_download(
        session_id: str,
        environment_id: str,
        path: str,
        *,
        follow_outward_links: bool = False,
    ) -> StreamingResponse:
        """Serve a file's complete bytes as an attachment.

        The read path inlines content in a JSON envelope, so it caps at
        ``_MAX_READ_BYTES``. A download streams from a descriptor and needs
        no cap; ``open_download`` binds that descriptor to what the sandbox
        can read before a byte is served.

        :param session_id: Session identifier.
        :param environment_id: Environment resource id.
        :param path: Path within the environment, or an absolute path.
        :param follow_outward_links: Admit a workspace symlink whose target
            lies outside the workspace but within the environment's reach.
        :returns: The file streamed with ``Content-Disposition: attachment``.
        :raises InvalidPath: If the path names a directory.
        :raises FilesystemPathNotFound: If nothing the caller may see exists
            at the path.
        """
        from omnigent.runner.environment_filesystem import CallerProcessFilesystem

        await _ensure_session_registered(session_id)
        agent_spec = await _resolve_session_agent_spec(session_id)
        env = resource_registry.resolve_environment(session_id, environment_id, agent_spec)
        fs = CallerProcessFilesystem(env, follow_outward_links=follow_outward_links)
        fobj, resolved, size = await fs.open_download(path)

        async def _chunks() -> AsyncIterator[bytes]:
            # Stop at the size announced in Content-Length so a file growing
            # underneath the download cannot overrun the response.
            remaining = size
            try:
                while remaining > 0:
                    chunk = await asyncio.to_thread(fobj.read, min(64 * 1024, remaining))
                    if not chunk:
                        # Ending short of Content-Length would hand the client
                        # a silently incomplete file; abort the transfer instead.
                        raise RuntimeError(f"{resolved.name} shrank during download")
                    remaining -= len(chunk)
                    yield chunk
            finally:
                fobj.close()

        # Same header Starlette's FileResponse builds: a plain quoted filename
        # when it is URL-safe, else the RFC 5987 encoded form.
        quoted = urllib.parse.quote(resolved.name)
        disposition = (
            f'attachment; filename="{resolved.name}"'
            if quoted == resolved.name
            else f"attachment; filename*=utf-8''{quoted}"
        )
        return StreamingResponse(
            _chunks(),
            media_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
            headers={
                "Content-Length": str(size),
                "Content-Disposition": disposition,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _fs_list_or_read(
        session_id: str,
        environment_id: str,
        path: str,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
        follow_outward_links: bool = False,
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import _MAX_READ_BYTES, CallerProcessFilesystem

        await _ensure_session_registered(session_id)
        agent_spec = await _resolve_session_agent_spec(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )

        fs = CallerProcessFilesystem(env, follow_outward_links=follow_outward_links)
        resolved = fs._resolve(path)

        if resolved.is_dir():
            page = await fs.list_dir(
                path,
                limit=limit,
                after=after,
                before=before,
                order=order,
            )
            data = [_fs_entry_to_dict(e) for e in page.data]
            return JSONResponse(
                status_code=200,
                content={
                    "object": "list",
                    # Absolute base the entry paths are relative to. Callers
                    # that only ever browse the workspace can keep ignoring it.
                    "base": str(resolved),
                    "data": data,
                    "first_id": page.first_id,
                    "last_id": page.last_id,
                    "has_more": page.has_more,
                },
            )

        # File previews need every line within the byte cap.
        content = await fs.read(path, max_bytes=_MAX_READ_BYTES)
        content_type_guess, _ = mimetypes.guess_type(path)
        payload: dict[str, object] = {
            "object": "session.environment.filesystem.file_content",
            "path": content.path,
            "content_type": content_type_guess,
            "bytes": content.bytes,
            "truncated": content.truncated,
        }
        if content.encoding:
            payload["encoding"] = content.encoding
            payload["content"] = content.data.decode(content.encoding)
        else:
            import base64

            payload["encoding"] = "base64"
            payload["content"] = base64.b64encode(content.data).decode()
        return JSONResponse(status_code=200, content=payload)

    def _fs_entry_to_dict(entry: FilesystemEntry) -> dict[str, object]:
        from omnigent.runner.environment_filesystem import entry_payload

        return entry_payload(entry)

    return ResourceRoutes(
        ensure_native_terminal_for_turn=_ensure_native_terminal_for_turn,
        require_os_env=_require_os_env,
        resolve_conversation_id=_resolve_conversation_id,
    )
