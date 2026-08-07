"""Higher-layer orchestration helpers for the sessions routes (call-depth 2+).

Flows that compose the lower-layer ``.helpers`` primitives: runner relay,
session-event dispatch, native-terminal launch, MCP tool calls, fork/switch.
Imports state/constants from ``.common`` and primitives from ``.helpers``;
imported by the router in ``sessions.py``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, cast

import httpx
from fastapi import (
    HTTPException,
    Request,
)
from fastapi.responses import Response
from pydantic import ValidationError

from omnigent.db.utils import generate_agent_id, generate_task_id
from omnigent.entities import (
    Agent,
    CommentsFingerprint,
    Conversation,
    ConversationItem,
    ErrorData,
    MessageData,
    NewConversationItem,
    ResourceEventData,
)
from omnigent.entities.conversation import (
    FunctionCallData,
    FunctionCallOutputData,
    parse_item_data,
)
from omnigent.entities.permission import SessionPermission
from omnigent.errors import ElicitationDeclinedError, ErrorCode, OmnigentError
from omnigent.harness_availability import (
    HARNESS_BINARY_MISSING,
    HARNESS_NEEDS_AUTH,
    HARNESS_VERSION_TOO_LOW,
)
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE as _HARNESS_NOT_CONFIGURED_ERROR_CODE,
)
from omnigent.host.frames import (
    WORKSPACE_MISSING_ERROR_CODE as _WORKSPACE_MISSING_ERROR_CODE,
)
from omnigent.llms.context_window import resolve_effective_context_window
from omnigent.native_coding_agents import (
    native_coding_agent_for_agent_name,
    native_coding_agent_for_harness,
)
from omnigent.policies.types import (
    ElicitationRequest,
    EvaluationContext,
    PolicyResult,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.session_init_protocol import build_runner_session_init_payload
from omnigent.runner.subagent_routing import (
    ROUTING_DECISION_LABEL_KEY,
    auto_harness_session,
    harness_family,
    subagent_routing_enabled,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runtime import (
    get_policy_store,
    inflight_text,
    pending_elicitations,
    pending_inputs,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import (
    build_elicitation_request_event,
    resolve_ask_timeout,
)
from omnigent.runtime.policies.builder import (
    _sum_subtree_usage,
    ancestor_ids_from_tree,
    load_session_tree,
    load_session_usage,
)
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.runtime.workflow import _find_spec_by_name
from omnigent.server import session_live_state
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_READ,
    local_single_user_enabled,
)
from omnigent.server.background_session_titles import (
    BackgroundSessionTitleCoordinator,
    prepare_background_session_title,
)
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.host_registry import HostConnection, HostRegistry, RunnerExitReports
from omnigent.server.managed_hosts import (
    ManagedHostLaunch,
    ManagedLaunchTracker,
    ManagedSandboxConfig,
    RepoWorkspace,
    host_resume_supported,
    host_sandbox_is_running,
)
from omnigent.server.routes._auth_helpers import (
    attribution_user as _attribution_user,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._session_create_validation import (
    validate_session_agent,
    validate_session_model_metadata,
)

# Shared constants, state, and small dataclasses live in the _sessions.common
# leaf module; import them here so this module and its re-exporters see the same
# objects. The mutable caches are shared by reference across the package.
# Runtime bindings that tests patch on the historical ``sessions`` facade are
# imported from common as facade-delegating proxies. They stay out of common's
# ``__all__`` and the facade's explicit re-exports, preserving its real runtime
# bindings so a facade-level monkeypatch is honoured in this module too.
from omnigent.server.routes._sessions.common import (  # noqa: F401
    _CLAUDE_NATIVE_MESSAGE_TIMEOUT_S,
    _CLAUDE_NATIVE_MODEL,
    _CLAUDE_NATIVE_UI_LABEL_KEY,
    _CLAUDE_NATIVE_UI_LABEL_VALUE,
    _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
    _CODEX_NATIVE_MODEL,
    _CURSOR_NATIVE_WRAPPER_LABEL_VALUE,
    _EXTERNAL_SESSION_STATUS_TYPE,
    _FENCE_EXEMPT_EVENT_TYPES,
    _LAST_CONTEXT_TOKENS_LABEL_KEY,
    _LAST_CONTEXT_WINDOW_LABEL_KEY,
    _MANAGED_RESUMABLE_TUNNEL_STALE_S,
    _MODEL_OPTIONS_ENDPOINT_BY_WRAPPER,
    _MODEL_TOKEN_KEYS,
    _PI_NATIVE_WRAPPER_LABEL_VALUE,
    _RUNNER_FORWARD_TIMEOUT,
    _RUNNER_RELAY_READY_TIMEOUT_S,
    _RUNNER_SESSION_INIT_TIMEOUT_S,
    _SUBAGENT_FORWARD_RECONNECT_WAIT_S,
    _TERMINAL_RESPONSE_EVENT_TYPES,
    _TURN_ACTOR_LABEL,
    _deferred_elicitation_clear_tasks,
    _intentional_stop_sessions,
    _interrupt_fenced_sessions,
    _logger,
    _managed_launch_tasks,
    _MirroredToolCall,
    _model_options_cache,
    _model_options_inflight,
    _model_options_stale,
    _native_popup_forward_tasks,
    _pending_policy_ask_writes,
    _PendingPolicyAskWrites,
    _pushed_model_options_cache,
    _recent_mirrored_tool_calls,
    _RelayHandle,
    _runner_relay_tasks,
    _runner_skills_cache,
    _runner_skills_inflight,
    _session_active_response_cache,
    _session_background_task_count_cache,
    _session_mcp_startup_cache,
    _session_sandbox_status_cache,
    _session_status_cache,
    _session_terminal_pending_cache,
    _session_todos_cache,
    get_caps,
    get_server_runner_router,
    session_stream,
    set_server_runner_router,
)

# Lower-layer helpers (SSE builders, publishers, persistence, runner-forward
# primitives) live in _sessions.helpers.
from omnigent.server.routes._sessions.helpers import (
    SessionLiveness,
    _await_settled_managed_launch,
    _build_new_item,
    _build_policy_engine_from_spec,
    _child_session_summary_from_conversation,
    _codex_subagent_labels_from_body,
    _coerce_cumulative_field,
    _collect_descendant_conversation_ids,
    _consume_pre_resolved_harness_elicitation,
    _create_and_publish_codex_child,
    _create_session_worktree,
    _delete_stored_session_bundle_after_failure,
    _derive_terminal_launch_args_from_spec,
    _emit_server_routing_decision,
    _error_item_from_sse,
    _extract_claude_native_runner_failure,
    _extract_persistent_item_from_sse,
    _extract_user_text_for_routing,
    _extract_user_text_from_event,
    _find_codex_native_subagent_child,
    _flush_relay_text,
    _forward_approval_to_runner,
    _forward_session_change_to_runner,
    _get_runner_client,
    _handle_advise_models_mcp,
    _invalidate_runner_backed_snapshot_state,
    _is_codex_native_subagent,
    _is_kiro_native_session,
    _is_native_interrupt_record,
    _last_task_error_from_labels,
    _latest_assistant_text_from_store,
    _latest_message_preview,
    _launch_runner_on_host,
    _load_agent_spec_for_session,
    _load_model_options,
    _load_model_options_from_host,
    _load_runner_skills,
    _mcp_error_response,
    _mcp_input_required_response,
    _mcp_ok_response,
    _merge_pending_file_blocks,
    _message_text,
    _model_usage_bucket,
    _native_coding_agent_for_session,
    _native_subagent_wrapper_labels_from_spec,
    _native_terminal_ensure_transport_error,
    _native_terminal_failure_from_runner_response,
    _native_terminal_name_for_harness,
    _NativeTerminalEnsureOutcome,
    _owner_from_grants,
    _parse_external_conversation_item,
    _pending_elicitation_snapshot_for_session,
    _permission_level_from_grants,
    _persist_native_policy_notice,
    _persist_session_status_error_labels,
    _persist_stored_session_bundle,
    _policy_notice_from_ensure_response,
    _poll_request_disconnect,
    _priced_cost_for_display,
    _provision_managed_sandbox,
    _prune_pre_resolved_harness_elicitations,
    _publish_elicitation_request_to_ancestors,
    _publish_elicitation_resolved,
    _publish_elicitation_resolved_to_ancestors,
    _publish_error_event,
    _publish_external_conversation_item,
    _publish_input_consumed,
    _publish_sandbox_status,
    _publish_status,
    _publish_terminal_pending,
    _query_host_runner_status,
    _read_state_entry,
    _record_daily_cost,
    _reject_reserved_cost_control_label_seed,
    _reject_server_reserved_label_seed,
    _relay_persist,
    _relay_persist_error_once,
    _remove_session_worktree_best_effort,
    _repl_terminal_ui_labels,
    _require_declared_subagent,
    _require_external_status_forward,
    _resolve_harness,
    _resolve_llm_model,
    _resolve_subagent_spec,
    _resource_event_item_from_sse,
    _routing_decision_item_from_sse,
    _RunnerForwardResult,
    _seed_missing_title_from_user_message,
    _session_status_from_cache,
    _session_status_with_child_rollup,
    _SessionEventDispatchResult,
    _signal_terminal_resolved_harness_elicitation,
    _spec_harness,
    _stop_session_via_runner,
    _usage_by_model_for_display,
    _validate_session_workspace,
    _validate_terminal_launch_args,
    _validated_cost_control_mode_override,
    _validated_harness_override,
    _validated_harness_override_executor_type,
    _validated_spec_smart_routing_harness,
    _validated_subagent_routing_override,
    _wait_for_managed_runner_tunnel,
    _wait_for_runner_client,
)
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.server.schemas import (
    ChildSessionSummary,
    CreatedSessionResponse,
    ElicitationRequestEvent,
    ElicitationRequestParams,
    ElicitationResult,
    ErrorDetail,
    NativeModelOption,
    SessionCreateMetadata,
    SessionCreateRequest,
    SessionEventInput,
    SessionListItem,
    SessionModelEvent,
    SessionResponse,
    SessionStatusEvent,
    SessionUsageEvent,
    SkillSummary,
)
from omnigent.session_lifecycle import (
    labels_with_closed_status,
    title_without_closed_marker,
)
from omnigent.spec.types import (
    AgentSpec,
    Phase,
    PolicyAction,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import (
    PINNED_LABEL_KEY,
    ConversationNotFoundError,
    pinned_label_key,
)
from omnigent.stores.file_store import FileStore
from omnigent.stores.host_store import Host, HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.telemetry import emit as _tel_emit
from omnigent.telemetry.events import SessionCreatedEvent as _TelSessionCreatedEvent
from omnigent.telemetry.installation_id import get_installation_id as _get_installation_id
from omnigent.telemetry.surface import classify_surface as _classify_surface


async def _publish_and_wait_for_harness_elicitation(
    request: Request,
    *,
    session_id: str,
    params: ElicitationRequestParams,
    timeout_s: float,
    conversation_store: ConversationStore | None = None,
    elicitation_id: str | None = None,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> ElicitationResult | None:
    """
    Publish one harness-originated elicitation and wait for web verdict.

    Mirrors the ``omnigent claude`` permission hook contract: the
    hook parks a server-side Future, publishes the standard
    ``response.elicitation_request`` event, waits until the session
    ``approval`` event resolves the Future, and always publishes
    ``response.elicitation_resolved`` when the upstream wait ends.

    The wait ends on the first of three signals: (1) the web verdict
    Future (session ``approval`` event); (2) the terminal-resolved
    Event, set when a mirrored tool result for this gated tool proves
    the prompt was answered in the native TUI (see
    :func:`_signal_terminal_resolved_harness_elicitation`); or (3)
    upstream disconnect / ``timeout_s``. Only (1) yields a verdict;
    (2) and (3) return ``None`` (fail-ask). (1) and (2) publish
    ``response.elicitation_resolved`` immediately; (3) defers it by
    ``_HARNESS_ELICITATION_REPARK_GRACE_S`` and skips it when the
    caller re-parks the same ``elicitation_id`` (hook retries after a
    severed long-poll reuse their id), so a still-blocked prompt's
    card survives the gap. A caller-supplied id likewise re-attaches
    to a verdict that landed during a gap via the pre-resolved
    tombstone, returned at registration time without re-publishing.

    :param request: FastAPI request object so upstream disconnect can
        be detected.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param params: Elicitation params to publish.
    :param timeout_s: Maximum wait in seconds, e.g. ``300.0``.
    :param conversation_store: Optional store used to mirror
        child-session prompts into ancestor streams. ``None`` keeps
        the prompt scoped to ``session_id`` only.
    :param elicitation_id: Optional precomputed correlation id, e.g.
        ``"elicit_codex_abc123"``. ``None`` mints a random id.
    :param tool_name: Gated tool name, e.g. ``"Bash"``, used to
        correlate a mirrored tool result back to this prompt for the
        terminal-resolved fast path. ``None`` (e.g. Codex) disables
        that correlation; the prompt still resolves via web verdict,
        disconnect, or timeout.
    :param tool_input: Gated tool input, e.g. ``{"command": "ls"}``,
        used with ``tool_name`` to disambiguate the result when several
        same-named prompts are parked at once.
    :returns: Web verdict, or ``None`` on terminal-side resolution,
        timeout, or disconnect.
    """
    if elicitation_id is None:
        elicitation_id = f"elicit_{secrets.token_hex(16)}"
    future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
    # ``resolved_elsewhere`` is set when a native-side signal proves the
    # prompt was answered outside the web UI: either a mirrored tool
    # result for this gated tool, or Codex app-server's exact
    # ``serverRequest/resolved`` notification. Raced below so the wait
    # ends promptly without relying on the web verdict or on disconnect
    # detection (unreliable behind the Databricks Apps proxy).
    parked = _ParkedHarnessElicitation(
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        resolved_elsewhere=asyncio.Event(),
    )
    _harness_elicitation_registry[elicitation_id] = future
    _harness_elicitation_owners[elicitation_id] = session_id
    _harness_parked_elicitations[elicitation_id] = parked
    # settled = verdict / terminal-resolved (clear the card now); a
    # severed wait instead defers the clear so a hook retry can re-park.
    published_request = False
    settled = False
    try:
        tombstone = _consume_pre_resolved_harness_elicitation(session_id, elicitation_id)
        if tombstone is not None:
            # Verdict from the un-parked gap; None = terminal answered (fail-ask).
            return tombstone.result
        event = ElicitationRequestEvent(
            type="response.elicitation_request",
            elicitation_id=elicitation_id,
            params=params,
        )
        event_payload = event.model_dump()
        session_stream.publish(session_id, event_payload)
        published_request = True
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_request_to_ancestors,
                conversation_store,
                session_id,
                event_payload,
            )
        disconnect_task = asyncio.create_task(
            _poll_request_disconnect(request),
        )
        resolved_elsewhere_task = asyncio.create_task(parked.resolved_elsewhere.wait())
        race_tasks = (disconnect_task, resolved_elsewhere_task)
        try:
            waiters: set[asyncio.Future[Any]] = {future, *race_tasks}
            done, _pending = await asyncio.wait(
                waiters,
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for race_task in race_tasks:
                if not race_task.done():
                    race_task.cancel()
                    # Bounded: a cancellation swallowed inside the race
                    # task (e.g. coalesced into an anyio cancel-scope
                    # unwind) must not convert this cleanup into
                    # an unbounded wait — that wedged the whole request
                    # for the gate's timeout. ``asyncio.wait`` absorbs
                    # the CancelledError outcome; an unreaped task is
                    # logged and abandoned to die with the request.
                    # Resolve through the facade so a test's
                    # monkeypatch of this constant is honored here.
                    from omnigent.server.routes import sessions as _facade

                    _reaped, still_pending = await asyncio.wait(
                        {race_task},
                        timeout=_facade._RACE_TASK_REAP_TIMEOUT_S,
                    )
                    if still_pending:
                        _logger.warning(
                            "Race task %r for elicitation %s survived its "
                            "cancellation (swallowed cancel); abandoning it.",
                            race_task.get_coro(),
                            elicitation_id,
                        )
        # Only an actual web verdict yields a result; a terminal-side
        # resolution, disconnect, or timeout returns None (fail-ask).
        # Checking ``future in done`` (not ``future.done()``) avoids
        # honoring a verdict that lands in the same tick as a disconnect.
        if future in done and future.exception() is None:
            settled = True
            return future.result()
        settled = parked.resolved_elsewhere.is_set()
        return None
    finally:
        # Pop only our own entries — a hook retry may have re-parked
        # this id with a new future while this wait was unwinding.
        if _harness_elicitation_registry.get(elicitation_id) is future:
            _harness_elicitation_registry.pop(elicitation_id, None)
            _harness_elicitation_owners.pop(elicitation_id, None)
        if _harness_parked_elicitations.get(elicitation_id) is parked:
            _harness_parked_elicitations.pop(elicitation_id, None)
        if published_request and not settled:
            # Severed without an answer — defer the clear (scheduled
            # before any await so handler cancellation can't skip it).
            _schedule_deferred_elicitation_clear(
                session_id,
                elicitation_id,
                conversation_store,
            )
        elif published_request:
            _publish_elicitation_resolved(session_id, elicitation_id)
            if conversation_store is not None:
                await asyncio.to_thread(
                    _publish_elicitation_resolved_to_ancestors,
                    conversation_store,
                    session_id,
                    elicitation_id,
                )


def _schedule_deferred_elicitation_clear(
    session_id: str,
    elicitation_id: str,
    conversation_store: ConversationStore | None,
) -> None:
    """
    Clear one elicitation's approval card after the re-park grace, unless
    a hook retry re-parks the id first.

    A wait severed without an answer (proxy cut, timeout) may still be
    blocked in the native terminal; clearing immediately wiped the only
    surface a headless sub-agent's user can answer from. A hook that
    died for real never re-parks, so the clear still fires after the
    grace and badges don't stick.

    :param session_id: Session that owns the elicitation, e.g.
        ``"conv_abc123"``.
    :param elicitation_id: Correlation id whose card may need clearing,
        e.g. ``"elicit_claude_0f3a..."``.
    :param conversation_store: Store used to mirror the clear into
        ancestor streams, or ``None`` to keep it session-local.
    """

    async def _clear_after_grace() -> None:
        """
        Sleep out the grace, then publish the clear unless re-parked.

        :returns: None.
        """
        # Resolve through the facade so a test's monkeypatch of this
        # constant is honored here.
        from omnigent.server.routes import sessions as _facade

        await asyncio.sleep(_facade._HARNESS_ELICITATION_REPARK_GRACE_S)
        if elicitation_id in _harness_elicitation_registry:
            # Re-parked — the new wait owns the eventual clear.
            return
        _publish_elicitation_resolved(session_id, elicitation_id)
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_resolved_to_ancestors,
                conversation_store,
                session_id,
                elicitation_id,
            )

    task = asyncio.create_task(_clear_after_grace())
    _deferred_elicitation_clear_tasks.add(task)
    task.add_done_callback(_deferred_elicitation_clear_tasks.discard)


async def _best_effort_stop(
    session_id: str,
    conversation_store: ConversationStore,
    runner_router: Any,
) -> None:
    """Stop a running session before a destructive lifecycle action.

    Mirrors the client-side stop-then-archive/delete pattern. A session
    reads as "running" here if it is itself running, has live background
    tasks, or has any sub-agent descendant (child, grandchild, and so on)
    still running or waiting, matching the unbounded depth that
    ``delete_conversation``'s recursive subtree delete already covers.
    Each running descendant must be stopped on its own session id: it
    executes on its own runner, separate from its ancestors', so stopping
    a parent never reaches it. Every stop attempt is independently
    best-effort, so one runner being unreachable does not skip stopping
    the others, and none of this may block the caller from archiving or
    deleting the session.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store for descendant-id lookup.
    :param runner_router: The ``RunnerRouter`` for runner-client
        resolution, or ``None`` in tests / in-process setups.
    """
    try:
        descendant_ids = await _collect_descendant_conversation_ids(conversation_store, session_id)
        status = _session_status_with_child_rollup(session_id, descendant_ids)
        own_status = _session_status_from_cache(session_id)
    except Exception:  # noqa: BLE001
        _logger.debug(
            "Best-effort stop failed for %s; proceeding anyway",
            session_id,
            exc_info=True,
        )
        return

    # Background shells outlive their turn without making the session read as
    # running (the sidebar must not spin for an ended turn), but they are live
    # work on the runner — a destructive action still has to stop them.
    has_background_tasks = (
        own_status != "failed" and _session_background_task_count_cache.get(session_id, 0) > 0
    )
    if status != "running" and not has_background_tasks:
        return

    async def _stop(target_id: str) -> None:
        try:
            await _stop_session_via_runner(target_id, runner_router)
        except Exception:  # noqa: BLE001
            _logger.debug(
                "Best-effort stop failed for %s; proceeding anyway",
                target_id,
                exc_info=True,
            )

    if own_status == "running" or has_background_tasks:
        await _stop(session_id)
    for descendant_id in descendant_ids:
        if _session_status_cache.get(descendant_id) in ("running", "waiting"):
            await _stop(descendant_id)


# Strong references to detached archive stops so the tasks can't be
# garbage-collected mid-stop (asyncio only holds weak refs to tasks).
_detached_stop_tasks: set[asyncio.Task[None]] = set()


async def _archive_stop(
    session_id: str,
    conversation_store: ConversationStore,
    runner_router: Any,
    host_registry: Any,
) -> None:
    """
    Stop an archived session and tear down its host-launched runner.

    Archive carries the whole teardown: it is the one lifecycle action
    with no client-side stop, so both halves run here. Killing the pane
    alone leaves a host-spawned session's dedicated runner connected,
    which keeps ``runner_online`` true and hangs a later message on
    "working" against a dead pane — the failure
    :func:`_stop_session_host_runner` exists to prevent.

    Every step is best-effort: a wedged, offline, or already-stopped
    runner must not leave the session un-archived.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store for descendant and row lookups.
    :param runner_router: The ``RunnerRouter`` for runner-client
        resolution, or ``None`` in tests / in-process setups.
    :param host_registry: The ``HostRegistry`` tracking live host
        tunnels, or ``None`` when host support is not wired.
    """
    # Resolve through the facade so a test's monkeypatch is honored here.
    from omnigent.server.routes import sessions as _facade

    await _facade._best_effort_stop(session_id, conversation_store, runner_router)
    try:
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    except Exception:  # noqa: BLE001
        _logger.debug(
            "Archive host-runner teardown lookup failed for %s",
            session_id,
            exc_info=True,
        )
        return
    if conv is None or not conv.host_id or not conv.runner_id:
        return
    # Mark the tunnel drop intentional BEFORE tearing it down so the relay
    # renders a quiet stopped state rather than "runner_disconnected".
    _intentional_stop_sessions.add(session_id)
    try:
        delivered = await _facade._stop_session_host_runner(
            session_id,
            conv.host_id,
            conv.runner_id,
            host_registry,
        )
    except Exception:  # noqa: BLE001
        _logger.debug(
            "Archive host-runner teardown failed for %s",
            session_id,
            exc_info=True,
        )
        delivered = False
    if not delivered:
        # No tunnel drop will follow, so the marker would outlive this stop
        # and later swallow a genuine runner_disconnected as a quiet idle.
        _intentional_stop_sessions.discard(session_id)


def _spawn_archive_stop(
    session_id: str,
    conversation_store: ConversationStore,
    runner_router: Any,
    host_registry: Any = None,
) -> None:
    """
    Run :func:`_archive_stop` as a retained background task.

    Archiving needs the stop to *happen*, not to have happened before
    the response is written: awaiting it inline held the PATCH for the
    stop's per-runner timeouts (seconds per running session against a
    wedged or asleep runner) even though the archive proceeds
    regardless of the stop's outcome.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store for descendant and row lookups.
    :param runner_router: The ``RunnerRouter`` for runner-client
        resolution, or ``None`` in tests / in-process setups.
    :param host_registry: The ``HostRegistry`` tracking live host
        tunnels, or ``None`` when host support is not wired.
    """
    task = asyncio.create_task(
        _archive_stop(session_id, conversation_store, runner_router, host_registry)
    )
    _detached_stop_tasks.add(task)
    task.add_done_callback(_detached_stop_tasks.discard)


def _labels_for_viewer(labels: dict[str, str], user_id: str | None) -> dict[str, str]:
    """
    Collapse per-user pin keys to the canonical pin label for one viewer.

    Pins are stored per-user as ``omnigent.pinned.<user>`` (see
    :func:`pinned_label_key`). On the wire we present a single canonical
    ``omnigent.pinned`` key — set iff THIS viewer pinned the session — and drop
    every ``omnigent.pinned.*`` key. That keeps the per-user dimension server-
    side (a viewer never sees who else pinned a shared session, and the client
    reads the same bare key it writes) and stops other users' pin keys from
    bloating the payload.

    :param labels: The stored conversation labels.
    :param user_id: The requesting viewer, or ``None`` in single-user mode.
    :returns: A copy with all ``omnigent.pinned.*`` keys removed and the
        canonical ``omnigent.pinned`` key added when the viewer's own pin
        is present.
    """
    my_key = pinned_label_key(user_id)
    my_pin = labels.get(my_key)
    cleaned = {
        k: v
        for k, v in labels.items()
        if k != PINNED_LABEL_KEY and not k.startswith(f"{PINNED_LABEL_KEY}.")
    }
    if my_pin is not None:
        cleaned[PINNED_LABEL_KEY] = my_pin
    return cleaned


def _build_session_list_item(
    conv: Conversation,
    *,
    agent_names_by_id: Mapping[str, str | None],
    grants: list[SessionPermission],
    user_id: str | None,
    user_is_admin: bool,
    permissions_enabled: bool,
    pending_count: int,
    child_session_ids: list[str],
    comments_fingerprint: CommentsFingerprint | None,
) -> SessionListItem:
    """
    Assemble one :class:`SessionListItem` from a conversation row and
    pre-fetched batch data.

    Single source of truth for the list-item shape, shared by the
    ``GET /v1/sessions`` page builder and the ``WS /v1/sessions/updates``
    push stream so the two never drift. The caller is responsible for
    batching the permission grants, agent names, and pending-elicitation
    counts across the whole set and passing the per-conversation slice
    here.

    :param conv: The persisted conversation entity. Must have a
        non-``None`` ``agent_id`` (i.e. be a session, not a plain
        conversation) — the caller filters these out beforehand.
    :param agent_names_by_id: Map from agent id to display name, as
        returned by ``agent_store.get_names()``,
        e.g. ``{"ag_abc": "research-agent"}``.
    :param grants: All permission grants for this conversation, as
        returned by ``permission_store.list_for_sessions()[conv.id]``.
        Empty list when permissions are disabled.
    :param user_id: The authenticated requesting user, or ``None`` when
        unauthenticated / permissions disabled,
        e.g. ``"alice@example.com"``.
    :param user_is_admin: Whether ``user_id`` holds the admin flag, from
        a single ``permission_store.is_admin()`` call made once for the
        whole batch.
    :param permissions_enabled: ``True`` when a permission store is
        wired; gates owner/level population to mirror ``list_sessions``.
    :param pending_count: Number of outstanding elicitations for this
        conversation, from ``pending_elicitations.counts_for()``.
    :param child_session_ids: Direct sub-agent children for this
        conversation, as returned by
        ``conversation_store.list_child_conversation_ids_by_parent()``.
    :param comments_fingerprint: Change-detection summary of this
        conversation's review comments, from
        ``comment_store.get_comments_fingerprints()[conv.id]``. ``None``
        when the conversation has no comments or no comment store is
        wired — emitted as ``comments_count=0`` /
        ``comments_updated_at=None`` so the two states look identical
        on the wire.
    :returns: The assembled :class:`SessionListItem`.
    """
    # ``conv.agent_id`` is guaranteed non-None by the caller (sessions
    # only); assert for the type checker without a runtime branch.
    assert conv.agent_id is not None
    level = _permission_level_from_grants(user_id, grants, user_is_admin)
    owner = _owner_from_grants(grants) if permissions_enabled else None
    # Per-viewer read tracking, embedded so the client hydrates the unread
    # dots straight from the list (no separate fetch). Built per-user here —
    # `user_id` is the requesting caller, never broadcast to other viewers.
    viewer_last_seen, viewer_unread = _read_state_entry(user_id, conv.id)
    return SessionListItem(
        id=conv.id,
        agent_id=conv.agent_id,
        agent_name=agent_names_by_id.get(conv.agent_id),
        status=_session_status_with_child_rollup(conv.id, child_session_ids, conv.live_status),
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        title=title_without_closed_marker(conv.title),
        # Collapse per-user pin keys to the canonical bare key for this viewer
        # (never leak another user's pin key), then add the closed marker.
        labels=labels_with_closed_status(_labels_for_viewer(conv.labels, user_id), conv.title),
        runner_id=conv.runner_id,
        host_id=conv.host_id,
        reasoning_effort=conv.reasoning_effort,
        permission_level=level,
        owner=owner,
        external_session_id=conv.external_session_id,
        # The persisted row count is a CROSS-REPLICA mirror: the replica
        # holding the runner's tunnel writes it, and a replica that doesn't
        # hold it falls back to the row (max() prefers "shows the parked
        # approval" whichever side lags). That fallback only makes sense for
        # a runner-bound session — an unbound session (no runner_id) has no
        # tunnel on any replica, so the local in-memory index is
        # authoritative and the row (an async mirror that lags a resolve's
        # decrement) must not override it. Gating on runner_id keeps the
        # cross-replica fallback where it's needed while making the unbound
        # path index-only and free of the persist-lag race.
        pending_elicitations_count=(
            max(pending_count, conv.pending_elicitation_count or 0)
            if conv.runner_id is not None
            else pending_count
        ),
        workspace=conv.workspace,
        git_branch=conv.git_branch,
        archived=conv.archived,
        comments_count=comments_fingerprint.count if comments_fingerprint else 0,
        comments_updated_at=(
            comments_fingerprint.last_updated_at if comments_fingerprint else None
        ),
        viewer_last_seen=viewer_last_seen,
        viewer_unread=viewer_unread,
        # Transient; set by the store only on a content search. The WS
        # push-stream path leaves it None (no query in flight there).
        search_snippet=conv.search_snippet,
        parent_session_id=conv.parent_conversation_id,
        project_id=conv.project_id,
    )


def _publish_subtree_cost_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
    conv: Conversation | None = None,
) -> None:
    """
    Re-publish each ancestor's subtree-summed cost after a child usage update.

    A sub-agent's spend is persisted on its own child conversation, so an
    ancestor's stored ``session_usage`` doesn't move when the child spends —
    yet the ancestor's displayed "Session cost" reads its own number, so a
    parent's badge would never reflect a running sub-agent. (The policy gate
    already reads the subtree sum via :func:`load_session_usage`; this is the
    display side.) For each ancestor of *session_id*, recompute its subtree
    priced cost and publish a ``session.usage`` event carrying it.

    One tree load serves the whole walk. Every ancestor of this session is in
    the same tree, so both the chain and each ancestor's sum are derived from
    one set of freshly-read rows — previously each ancestor paged the tree
    again, and the chain came from a conversation row that may have been read
    before a concurrent delete/recreate moved this session elsewhere.

    Sync (does store reads + SSE fan-out); call via
    :func:`asyncio.to_thread`, mirroring the elicitation ancestor-publish
    helpers. ``session_stream.publish`` is safe to call from a worker thread.

    :param conv_store: Store used to load the tree.
    :param session_id: The child session whose usage just changed, e.g.
        ``"conv_child123"``.
    :param conv: The child's already-loaded conversation row, when the caller
        holds one. Only its ``root_conversation_id`` is used, and only as a
        hint: :func:`load_session_tree` verifies the tree it names actually
        contains this session and resolves the root itself when it does not.
        The parameter belongs to this function, not to whichever caller first
        needed it, so that no caller can be removed and leave a signature
        behind that its remaining callers already depend on.
    :returns: None.
    """
    tree = load_session_tree(
        session_id,
        conv_store,
        conv.root_conversation_id if conv is not None else None,
    )
    for ancestor_id in ancestor_ids_from_tree(tree, session_id):
        ancestor_usage = _sum_subtree_usage(tree, ancestor_id)
        subtree_cost = _priced_cost_for_display(ancestor_usage)
        usage_by_model = _usage_by_model_for_display(ancestor_usage)
        if subtree_cost is None and usage_by_model is None:
            # Ancestor's subtree has no priced cost or token usage yet —
            # leave its badge showing "—"/its snapshot value rather than
            # emit $0.00.
            continue
        payload: dict[str, Any] = {
            "type": "session.usage",
            "conversation_id": ancestor_id,
        }
        if subtree_cost is not None:
            payload["total_cost_usd"] = subtree_cost
        if usage_by_model is not None:
            payload["usage_by_model"] = usage_by_model
        event = SessionUsageEvent(**payload)
        session_stream.publish(ancestor_id, event.model_dump(exclude_none=True))


def _build_session_response(
    conv: Conversation,
    items: list[ConversationItem],
    status: Literal["idle", "running", "waiting", "failed"],
    permission_level: int | None = None,
    background_task_count: int | None = None,
    llm_model: str | None = None,
    context_window: int | None = None,
    last_total_tokens: int | None = None,
    last_task_error: dict[str, str] | None = None,
    agent_name: str | None = None,
    skills: list[SkillSummary] | None = None,
    runner_online: bool | None = None,
    host_online: bool | None = None,
    host_resumable: bool = False,
    pending_elicitation_events: list[dict[str, Any]] | None = None,
    subtree_usage: dict[str, Any] | None = None,
    model_options: list[dict[str, Any]] | None = None,
    viewer_id: str | None = None,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
) -> SessionResponse:
    """
    Build a :class:`SessionResponse` from store-side entities.

    ``status`` is derived from the conversation's tasks by the
    caller via :func:`_derive_session_lifecycle` — the conversation
    row itself owns no lifecycle column.

    :param conv: The persisted conversation entity.
    :param items: Committed conversation items in chronological
        order, each a :class:`ConversationItem`.
    :param status: Derived session lifecycle status,
        e.g. ``"running"``.
    :param background_task_count: Background shells still running as of the
        last status edge (claude-native), so a reload re-shows "N shells
        still running" even after the session settles to ``"idle"``. ``None``
        when none are tracked.
    :param permission_level: The requesting user's numeric level
        on this session (1=read, 2=edit, 3=manage), or ``None``
        when permissions are disabled.
    :param runner_online: Session-scoped liveness for the bound
        runner/host, e.g. ``False`` for a dead tunneled runner.
        ``None`` when no lookup is wired.
    :param llm_model: The LLM model identifier from the bound
        agent's spec, e.g. ``"anthropic/claude-sonnet-4-6"``.
        ``None`` when not available.
    :param context_window: Context window size in tokens looked up
        from litellm server-side, e.g. ``200_000``. ``None`` when
        the model is not in litellm's registry.
    :param last_total_tokens: Total token count (input + output) from
        the most recently completed task's usage, e.g. ``45231``.
        ``None`` when no task has completed yet. Lets clients seed
        their context-ring on conversation resume without waiting for
        the next ``response.completed`` SSE event.
    :param last_task_error: Error dict from the most recently failed
        task, e.g. ``{"code": "executor_error", "message": "..."}``.
        ``None`` when ``status`` is not ``"failed"`` or the task has
        no stored error.
    :param agent_name: Human-readable agent name, e.g.
        ``"research-agent"``. ``None`` when the agent row is not
        available at snapshot-build time.
    :param skills: Merged skill summaries (bundled + host) for
        the bound agent. ``None`` is treated as the empty list,
        e.g. when the agent spec cannot be loaded.
    :param runner_online: Strict runner reachability — ``True`` iff a
        runner tunnel is currently registered for this session (see
        :class:`SessionLiveness`). ``None`` when the caller has no
        liveness lookup wired (e.g. focused tests), in which case the
        field is omitted from the API projection.
    :param host_online: Whether the session's host tunnel is live, or
        ``None`` when the session has no ``host_id`` or no lookup is
        wired (see :class:`SessionLiveness`). Used only to decide what
        the open view shows when ``runner_online`` is ``False``.
    :param pending_elicitation_events: Optional precomputed
        outstanding elicitation events. ``None`` reads only the
        current session's entries from the pending-elicitations index.
    :param subtree_usage: Precomputed subtree usage dict (this session
        plus its sub-agent descendants, from
        :func:`load_session_usage`), used to display a cost that
        includes sub-agents, e.g. ``{"total_cost_usd": 11.19}``.
        ``None`` falls back to this conversation's own ``session_usage``
        (correct for childless sessions). Passed by the snapshot path;
        other callers omit it.
    :param model_options: Runner-owned native model picker options,
        e.g. ``[{"id": "gpt-5.5", "displayName": "GPT-5.5"}]``.
        ``None`` is treated as ``[]``.
    :param agent_store: Optional store used to resolve the session harness.
    :param agent_cache: Optional cache used to load the session harness spec.
    :returns: The :class:`SessionResponse` for the API.
    :raises OmnigentError: If ``conv.agent_id`` is ``None``.
    """
    if conv.agent_id is None:
        raise OmnigentError(
            "Session has no agent binding",
            code=ErrorCode.INTERNAL_ERROR,
        )
    # Usage to display for this node: the SUBTREE total (this session + its
    # sub-agents) when the caller computed it, else this conversation's own
    # usage. Shared by the cost indicator and the per-model breakdown so
    # both read the same numbers.
    display_usage = subtree_usage if subtree_usage is not None else (conv.session_usage or {})
    # Native-terminal-wrapper sessions (claude-native-ui / codex-native-ui) are
    # always terminal-first: the web UI's Chat/Terminal pill is gated on the
    # ``omnigent.ui = "terminal"`` label. That flag is fully determined by the
    # agent identity, so derive it here from ``agent_name`` rather than relying
    # solely on the stored label — the pill then stays correct even if the
    # stored value is missing or stale. Idempotent: a no-op when already present.
    # Collapse per-user pin keys to the canonical bare key for this viewer, so
    # the snapshot never carries another user's pin key (see _labels_for_viewer).
    labels = labels_with_closed_status(_labels_for_viewer(conv.labels, viewer_id), conv.title)
    if agent_name in (_CLAUDE_NATIVE_MODEL, _CODEX_NATIVE_MODEL):
        labels = {**labels, _CLAUDE_NATIVE_UI_LABEL_KEY: _CLAUDE_NATIVE_UI_LABEL_VALUE}
    return SessionResponse(
        id=conv.id,
        agent_id=conv.agent_id,
        agent_name=agent_name,
        status=status,
        background_task_count=background_task_count,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        title=title_without_closed_marker(conv.title),
        labels=labels,
        runner_id=conv.runner_id,
        host_id=conv.host_id,
        runner_online=runner_online,
        host_online=host_online,
        host_resumable=host_resumable,
        reasoning_effort=conv.reasoning_effort,
        items=items,
        permission_level=permission_level,
        sub_agent_name=conv.sub_agent_name,
        kind=conv.kind,
        parent_session_id=conv.parent_conversation_id,
        root_conversation_id=conv.root_conversation_id,
        llm_model=llm_model,
        harness=_resolve_harness(
            conv,
            agent_store=agent_store,
            agent_cache=agent_cache,
        ),
        model_override=conv.model_override,
        cost_control_mode_override=conv.cost_control_mode_override,
        subagent_routing_override=conv.subagent_routing_override,
        context_window=context_window,
        last_total_tokens=last_total_tokens,
        # Seed the client's cost indicator on resume. Uses the SUBTREE
        # total (this session + its sub-agents) when the caller computed
        # it, so a parent's badge reflects its sub-agents' spend; falls
        # back to this conversation's own usage otherwise. A priced
        # cumulative total, or None (rendered "—") when never priced.
        total_cost_usd=_priced_cost_for_display(display_usage),
        # Per-model breakdown over the same subtree usage. None (omitted)
        # when no per-model usage was recorded.
        usage_by_model=_usage_by_model_for_display(display_usage),
        last_task_error=last_task_error,
        external_session_id=conv.external_session_id,
        terminal_launch_args=conv.terminal_launch_args,
        # Replay outstanding approval prompts into the snapshot.
        # The live SSE stream has no buffer, so a prompt emitted
        # before the user opened this chat would otherwise never
        # render — the UI rebuilds blocks from the snapshot on
        # cold load, then live-tails. Empty list when nothing is
        # outstanding (the common case).
        pending_elicitations=(
            pending_elicitation_events
            if pending_elicitation_events is not None
            else pending_elicitations.snapshot_for(conv.id)
        ),
        # Replay un-consumed web messages on native-terminal sessions
        # so a client that posted then navigated away / rebound re-
        # hydrates the optimistic bubble. Empty for non-native sessions
        # (their message is already persisted into ``items``).
        pending_inputs=pending_inputs.snapshot_for(conv.id),
        workspace=conv.workspace,
        git_branch=conv.git_branch,
        archived=conv.archived,
        # Replay the latest todo list for claude-native sessions.
        # Populated by _handle_external_session_todos; empty list for
        # non-claude-native sessions or before the first poll tick.
        todos=_session_todos_cache.get(conv.id, []),
        skills=skills or [],
        model_options=[
            NativeModelOption.model_validate(option) for option in (model_options or [])
        ],
        # Replay terminal spin-up state so a client connecting while the
        # runner is still creating a terminal-first session's terminal
        # sees the Terminal-pill spinner. Populated by the runner SSE
        # relay; absent (False) for non-terminal-first sessions or once
        # the terminal lands / auto-create fails.
        terminal_pending=_session_terminal_pending_cache.get(conv.id, False),
        # Replay managed-sandbox launch progress so a client opening the
        # session mid-launch (the Web UI navigates here immediately
        # after the non-blocking managed create) sees the provisioning
        # indicator. None for sessions without a managed launch and
        # once the launch succeeds; a failed launch is retained with
        # its reason. Populated by _publish_sandbox_status.
        sandbox_status=_session_sandbox_status_cache.get(conv.id),
        # Replay harness MCP-server startup state (codex-native) so a
        # client opening the session mid-startup sees the startup band.
        mcp_startup=_session_mcp_startup_cache.get(conv.id),
        # In-flight turn id so a mid-turn reconnect can reopen a streaming
        # ``activeResponse`` (the turn-start ``running`` edge that carried it
        # is not replayed on the SSE stream). Populated for native-terminal
        # sessions whose forwarder stamps a turn id; ``None`` otherwise.
        active_response_id=_session_active_response_cache.get(conv.id),
        project_id=conv.project_id,
    )


def _accumulate_session_usage(
    resp_obj: dict[str, Any],
    session_id: str,
    conversation_store: ConversationStore,
) -> float | None:
    """
    Increment the session's cumulative token counters from a
    ``response.completed`` event's usage data.

    Called synchronously from the relay loop. Builds a usage delta from
    the response's ``usage`` field and atomically applies it to the
    persisted ``session_usage`` via a single database transaction
    (``SELECT FOR UPDATE`` on PostgreSQL, SQLite's single-writer lock
    otherwise). This prevents the read-modify-write race that caused
    concurrent relay completions to silently drop each other's cost /
    token deltas (#9). No-op when the response carries no usage data.

    Cost is computed when the model's per-token pricing is
    available from the MLflow catalog (looked up once per call
    from the response's ``model`` field). When the harness instead
    reports an authoritative per-turn ``cost_usd`` (e.g. Copilot's
    AI-credit total), that value is used directly in preference to
    the catalog estimate. The ``total_cost_usd`` key is written
    **only when the turn is priced** (catalog pricing available or a
    harness-reported cost) — an unpriced session leaves it absent
    (its presence is what distinguishes a priced ``$0.00`` from
    "unpriced"; see :func:`_priced_cost_for_display`).

    :param resp_obj: The ``response`` dict from the
        ``response.completed`` SSE event.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conversation_store: Store for reading and writing
        the ``session_usage`` column.
    :returns: The session's cumulative priced cost in USD after this
        update (for the caller to broadcast on a ``session.usage``
        event), or ``None`` when the session is unpriced or carries no
        usage to accumulate.
    """
    usage_obj = resp_obj.get("usage")
    if not isinstance(usage_obj, dict):
        return None
    input_tokens = usage_obj.get("input_tokens", 0)
    output_tokens = usage_obj.get("output_tokens", 0)
    total_tokens = usage_obj.get("total_tokens", 0)
    if not any((input_tokens, output_tokens, total_tokens)):
        return None

    cache_read_input_tokens = usage_obj.get("cache_read_input_tokens", 0)
    cache_creation_input_tokens = usage_obj.get("cache_creation_input_tokens", 0)

    # Load conversation metadata for pricing only (NOT for reading session_usage —
    # the atomic increment_session_usage call below handles that separately to
    # avoid the read-modify-write race).
    conv = conversation_store.get_conversation(session_id)

    # Compute cost delta if pricing is available for the model. Resolve
    # the model to price with, most-specific first:
    #   1. ``usage.model`` — the model the harness actually used this turn.
    #      Relay executors report it; it's the only signal when the spec
    #      pins no ``llm.model`` (a supervisor that delegates / uses the
    #      harness default), so it's what makes those sessions priceable.
    #   2. the session's ``model_override`` (a ``/model`` switch).
    #   3. the agent spec's ``llm.model`` (the static default).
    # The response's top-level ``model`` is the AGENT NAME, not the LLM
    # model, so it is never used here. The ``total_cost_usd`` key is
    # created only on this priced branch, so an unpriced session never
    # gains a (misleading $0.00) cost key.
    cost_delta = 0.0
    priced = False
    # Prefer an authoritative harness-reported cost over the catalog estimate.
    provider_cost = usage_obj.get("cost_usd")
    usage_model = usage_obj.get("model")
    llm_model = (
        usage_model
        if isinstance(usage_model, str) and usage_model
        else (conv.model_override if conv and conv.model_override else _resolve_llm_model(conv))
    )
    if llm_model:
        if isinstance(provider_cost, (int, float)):
            cost_delta = float(provider_cost)
            priced = True
        else:
            from omnigent.llms.context_window import compute_llm_cost, fetch_model_pricing

            pricing = fetch_model_pricing(llm_model)
            priced = pricing is not None
            if pricing is not None:
                # Cache-aware: usage_obj carries cache_read/cache_creation
                # token counts when the harness reports them; compute_llm_cost
                # prices them at their own (cheaper read / pricier write) rates.
                cost_delta = compute_llm_cost(usage_obj, pricing)

    # Build the delta dict and atomically apply it to the persisted
    # session_usage in a single DB transaction (SELECT FOR UPDATE on
    # PostgreSQL; SQLite's exclusive write lock on SQLite). This is the fix
    # for the read-modify-write race that caused concurrent completions to
    # overwrite each other's deltas (#9).
    delta: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
    }
    if priced:
        delta["total_cost_usd"] = cost_delta
    if llm_model:
        # Per-model attribution. Tokens are attributed whenever the model is
        # known — including unpriced turns — so the per-model token view is
        # complete; cost is attributed only when this model's turn was priced
        # (keeping the model's cost key absent otherwise, matching the flat
        # "priced ⟺ key present" contract).
        model_delta: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
        }
        if priced:
            model_delta["total_cost_usd"] = cost_delta
        delta["by_model"] = {llm_model: model_delta}

    new_current = conversation_store.increment_session_usage(session_id, delta)
    # Per-user daily rollup (policy-gated; this is the per-turn delta).
    _record_daily_cost(conv, cost_delta, conversation_store)
    return _priced_cost_for_display(new_current)


def _persist_native_cumulative_usage(
    session_id: str,
    data: dict[str, Any],
    conversation_store: ConversationStore,
) -> float | None:
    """
    Persist cumulative cost / token usage reported by a native harness.

    Unlike the Omnigent relay path (:func:`_accumulate_session_usage`), which adds
    per-response *deltas*, native harnesses (claude-native / codex-native)
    report *cumulative* session usage — so the flat session fields are written
    with SET semantics. The per-model ``by_model`` buckets are the exception:
    they accumulate each report's growth (new - old) attributed to the active
    model, so they stay correct across mid-session model switches (see below).
    The two paths never run for the same session, so they don't conflict.

    Reads explicit cumulative fields from the ``external_session_usage`` event's
    ``data`` (all optional; a no-op when none are present):

    - ``cumulative_cost_usd`` — total session cost for DISPLAY, e.g.
      claude-native forwards Claude Code's own ``cost.total_cost_usd``
      (exact billing; used directly). Stored in ``total_cost_usd``, which
      drives the badge and the per-user daily rollup, so the badge matches
      ``/cost`` in the Claude TUI.
    - ``policy_cost_usd`` — total session cost for ENFORCEMENT (the
      cost-budget gate). claude-native forwards ``max(S, real-time
      transcript estimate)`` here so the gate reflects in-flight sub-agent
      spend while the displayed ``S`` is frozen for the sub-agent's run.
      Stored verbatim in ``policy_cost_usd`` (the policy engine seeds from
      it, falling back to ``total_cost_usd`` when absent). Not fed into the
      daily rollup — that uses the authoritative ``total_cost_usd``.
    - ``cumulative_input_tokens`` / ``cumulative_output_tokens`` — total session
      tokens, e.g. codex-native's ``tokenUsage.total``. When
      ``cumulative_cost_usd`` is absent, cost is computed from these via
      :func:`fetch_model_pricing`.
    - ``cumulative_cache_read_input_tokens`` — the cached portion *included
      in* ``cumulative_input_tokens`` (e.g. codex-native's
      ``tokenUsage.total.cachedInputTokens``). Split out of the input total
      so :func:`compute_llm_cost` prices it at the cache-read rate rather
      than the full input rate. Absent for harnesses that don't report it.
    - ``model`` — LLM model id to price with (e.g. ``"databricks-gpt-5-5"``);
      falls back to the agent spec's model when absent.

    The ``total_cost_usd`` key is written only on the priced branches
    below (exact billing, or token-priced when the model is in the
    catalog), so an unpriced native session leaves it absent — the same
    "priced ⟺ key present" contract the relay path uses. ``policy_cost_usd``
    is written only when the event carries it (claude-native with the
    display/policy split); codex-native and the relay omit it and the
    policy engine falls back to ``total_cost_usd``.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :param data: The ``external_session_usage`` event ``data`` dict.
    :param conversation_store: Store for reading and writing ``session_usage``.
    :returns: The session's cumulative priced cost in USD after this
        update (for the caller to broadcast on a ``session.usage``
        event), or ``None`` when the session is unpriced or no
        cumulative field was present.
    :raises OmnigentError: When a cumulative field is the wrong type.
    """
    cost = _coerce_cumulative_field(data, "cumulative_cost_usd", numeric=True)
    policy_cost = _coerce_cumulative_field(data, "policy_cost_usd", numeric=True)
    cin = _coerce_cumulative_field(data, "cumulative_input_tokens", numeric=False)
    cout = _coerce_cumulative_field(data, "cumulative_output_tokens", numeric=False)
    ccache = _coerce_cumulative_field(data, "cumulative_cache_read_input_tokens", numeric=False)
    if cost is None and policy_cost is None and cin is None and cout is None:
        return None

    conv = conversation_store.get_conversation(session_id)
    current: dict[str, Any] = dict(conv.session_usage) if conv and conv.session_usage else {}
    # Native usage is cumulative (SET semantics), so the per-turn delta
    # for the daily rollup is new_total - old_total. Capture the old
    # cumulative + enforcement costs before the fields below overwrite them.
    # Both are clamped MONOTONIC below (a write may only raise them): the
    # ``external_session_usage`` event is posted with the session owner's own
    # bearer token (the forwarder uses no privileged identity), so a client
    # could otherwise replay it with a falsified low cost to reset the gate's
    # cost to ~0 (disabling the budget DENY/ASK) and drive the daily rollup
    # delta negative (clawing back already-spent budget). Monotonicity makes a
    # downward report a no-op, so the worst a forged post can do is leave the
    # figure unchanged. (See also the runner-token guard on cost_control.*
    # label writes — usage was the missing half.)
    old_cost = float(current.get("total_cost_usd", 0.0) or 0.0)
    old_policy_cost = float(current.get("policy_cost_usd", 0.0) or 0.0)
    # Old cumulative token totals, captured before the overwrites below so
    # per-model attribution can add only this report's growth (new - old).
    old_tokens = {key: int(current.get(key, 0) or 0) for key in _MODEL_TOKEN_KEYS}
    if cin is not None:
        # The reported input total is INCLUSIVE of cached tokens (codex's
        # ``inputTokens`` counts cache reads). Split the cached portion into
        # its own bucket so compute_llm_cost prices it at the cache-read rate;
        # ``input_tokens`` keeps only the non-cached remainder (its contract).
        # Clamp cached to the total so a malformed report never makes
        # ``input_tokens`` negative.
        cached = min(int(ccache), int(cin)) if ccache is not None else 0
        current["cache_read_input_tokens"] = cached
        current["input_tokens"] = int(cin) - cached
    if cout is not None:
        current["output_tokens"] = cout
    if cin is not None or cout is not None:
        # ``total_tokens`` reflects the full input (non-cached + cached) plus
        # output, so the split above doesn't shrink the displayed total.
        current["total_tokens"] = (
            int(current.get("input_tokens", 0))
            + int(current.get("cache_read_input_tokens", 0))
            + int(current.get("output_tokens", 0))
        )

    # Resolve the model for per-model attribution on any broadcast that carries
    # tokens OR a priced cost — both the token-pricing branch and the per-model
    # attribution below need it. A cost-only broadcast must resolve it too:
    # claude-native forwards Claude Code's statusLine total (S) with NO token
    # counts, so gating model resolution on tokens alone dropped that cost from
    # ``by_model`` entirely — the per-model TOKEN USAGE view undercounted the
    # session total by every native (sub-)agent's spend, while the flat
    # ``total_cost_usd`` (and the Session-cost badge) still included it.
    # Priority mirrors the relay path's ``_accumulate_session_usage``: the
    # event's ``model`` (the statusLine's active model, forwarded alongside the
    # cost) wins, then the session's ``model_override`` (the forwarder mirrors
    # in-pane /model switches there), then the agent spec's static model.
    # Computed once out of the pricing-only branch so attribution works even on
    # an unpriced turn. (The agent-cache lookup in ``_resolve_llm_model`` is
    # memoized, so resolving on cost-only polls is cheap.)
    has_tokens = cin is not None or cout is not None
    needs_model = has_tokens or cost is not None
    model_name = (
        (
            data.get("model")
            or (conv.model_override if conv and conv.model_override else None)
            or _resolve_llm_model(conv)
        )
        if needs_model
        else None
    )
    if cost is not None:
        # Monotonic: a reported total below the persisted one is ignored.
        current["total_cost_usd"] = max(old_cost, float(cost))
    elif has_tokens:
        if isinstance(model_name, str) and model_name:
            from omnigent.llms.context_window import compute_llm_cost, fetch_model_pricing

            pricing = fetch_model_pricing(model_name)
            if pricing is not None:
                # SET (cumulative) — price the running token totals.
                # ``current`` carries the cache-read split when the harness
                # reports it (codex-native does), so compute_llm_cost prices
                # cache reads at their own rate; it falls back to the input
                # rate for cache tokens when the catalog omits a cache price
                # (e.g. ``databricks-*`` entries today).
                # Monotonic, like the explicit-cost branch: token totals are
                # also client-SET, so a lowered token report can't drop the
                # priced cost below the persisted figure.
                current["total_cost_usd"] = max(old_cost, compute_llm_cost(current, pricing))

    # ADD this report's growth (new - old) to the active model, not the whole
    # cumulative total, so per-model buckets sum to the flat total across model
    # switches. Clamp >= 0 so a lowered report never claws a bucket back (flat
    # tokens are SET not clamped, so buckets can exceed them then — fail-safe).
    if isinstance(model_name, str) and model_name:
        bucket = _model_usage_bucket(current, model_name)
        for key in _MODEL_TOKEN_KEYS:
            if key in current:
                bucket[key] = int(bucket.get(key, 0)) + max(0, int(current[key]) - old_tokens[key])
        if "total_cost_usd" in current:
            bucket["total_cost_usd"] = float(bucket.get("total_cost_usd", 0.0)) + max(
                0.0, float(current["total_cost_usd"]) - old_cost
            )

    # Enforcement value (claude-native display/policy split). Stored
    # separately from the displayed ``total_cost_usd`` so the gate can read
    # the real-time figure (incl. in-flight sub-agent spend) while the badge
    # shows the frozen statusLine total. Monotonic, like total_cost_usd: this
    # is the value the cost-budget gate actually reads, so a forged low report
    # must never lower it. When an in-flight estimate later resolves below a
    # prior peak the clamp keeps the peak — conservative (the gate errs toward
    # MORE enforcement, never less), which is the safe direction for a budget.
    if policy_cost is not None:
        current["policy_cost_usd"] = max(old_policy_cost, float(policy_cost))

    conversation_store.set_session_usage(session_id, current)
    # Per-user daily rollup. Native reports cumulative totals, so the turn's
    # delta is the increase in cumulative cost. Uses the authoritative
    # ``total_cost_usd`` (= statusLine S), NOT ``policy_cost_usd`` — the
    # daily report must reflect real spend, not the real-time gate estimate.
    new_cost = float(current.get("total_cost_usd", 0.0) or 0.0)
    # Non-negative by the monotonic clamp above; ``max(0.0, ...)`` keeps the
    # daily rollup from ever being clawed back even if that invariant changes.
    _record_daily_cost(conv, max(0.0, new_cost - old_cost), conversation_store)
    return _priced_cost_for_display(current)


async def _persist_external_session_usage(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> int | None:
    """
    Persist and broadcast a token-usage update from a terminal-backed runtime.

    At least one of ``data.context_tokens`` (non-negative int),
    ``data.context_window`` (positive int), or a cumulative usage field
    (:func:`_persist_native_cumulative_usage`) must be present.

    :param session_id: Session/conversation identifier.
    :param body: External session-usage event body.
    :param conversation_store: Store used to upsert the labels.
    :returns: The persisted ``context_tokens`` when present, else ``None``.
    :raises OmnigentError: On missing / malformed fields.
    """
    raw_tokens = body.data.get("context_tokens")
    if raw_tokens is not None and (not isinstance(raw_tokens, int) or raw_tokens < 0):
        raise OmnigentError(
            "external_session_usage data.context_tokens must be a non-negative int",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_window = body.data.get("context_window")
    if raw_window is not None and (not isinstance(raw_window, int) or raw_window <= 0):
        raise OmnigentError(
            "external_session_usage data.context_window must be a positive int",
            code=ErrorCode.INVALID_INPUT,
        )
    _CUMULATIVE_USAGE_KEYS = (
        "cumulative_cost_usd",
        # ``policy_cost_usd`` alone is a valid post: mid-turn the displayed
        # statusLine total (``cumulative_cost_usd``) is frozen, so the
        # forwarder posts only the advancing real-time enforcement cost.
        "policy_cost_usd",
        "cumulative_input_tokens",
        "cumulative_output_tokens",
    )
    has_cumulative = any(body.data.get(k) is not None for k in _CUMULATIVE_USAGE_KEYS)
    if raw_tokens is None and raw_window is None and not has_cumulative:
        raise OmnigentError(
            "external_session_usage requires at least one of "
            "data.context_tokens, data.context_window, or a cumulative usage field",
            code=ErrorCode.INVALID_INPUT,
        )

    # Native harnesses report cumulative cost / tokens (SET semantics) — distinct
    # from the Omnigent relay's per-response accumulation. Persist this session's
    # own cumulative usage (its priced own-cost return is unused — the badge shows
    # the subtree total computed below, not own cost).
    await asyncio.to_thread(
        _persist_native_cumulative_usage,
        session_id,
        body.data,
        conversation_store,
    )

    label_updates: dict[str, str] = {}
    if raw_tokens is not None:
        label_updates[_LAST_CONTEXT_TOKENS_LABEL_KEY] = str(raw_tokens)
    if raw_window is not None:
        label_updates[_LAST_CONTEXT_WINDOW_LABEL_KEY] = str(raw_window)
    await asyncio.to_thread(
        conversation_store.set_labels,
        session_id,
        label_updates,
    )
    # The displayed cost is this session's SUBTREE total (itself + its
    # sub-agents), matching the GET snapshot. A sub-agent persists its spend on
    # its own child conversation, so broadcasting only this session's own cost
    # would drop a parent's badge back to own-cost on every parent flush and
    # hide in-flight sub-agent spend until the next child flush (the badge would
    # oscillate own ⇄ subtree). For a childless session the subtree is just
    # itself, so this equals own cost — one indexed tree query per flush.
    subtree_usage = await asyncio.to_thread(load_session_usage, session_id, conversation_store)
    subtree_cost = _priced_cost_for_display(subtree_usage)
    usage_by_model = _usage_by_model_for_display(subtree_usage)
    # Only include fields that were sent; the client treats absent
    # fields as "no change" so a window-only update doesn't zero tokens.
    # ``total_cost_usd`` is included only when the subtree is priced
    # (``exclude_none`` strips it otherwise) — an unpriced session keeps
    # showing "—" from the snapshot rather than a misleading $0.00.
    event_payload: dict[str, Any] = {
        "type": "session.usage",
        "conversation_id": session_id,
    }
    if raw_tokens is not None:
        event_payload["context_tokens"] = raw_tokens
    if raw_window is not None:
        event_payload["context_window"] = raw_window
    if subtree_cost is not None:
        event_payload["total_cost_usd"] = subtree_cost
    if usage_by_model is not None:
        event_payload["usage_by_model"] = usage_by_model
    event = SessionUsageEvent(**event_payload)
    session_stream.publish(session_id, event.model_dump(exclude_none=True))
    # This session's usage also moves its ANCESTORS' subtree cost (its spend
    # rolls up into every ancestor), so re-publish each ancestor's subtree cost
    # too — otherwise a grandparent's badge wouldn't reflect a deep descendant.
    # No-op for a top-level session (no ancestors). Threaded: it pages the
    # conversation tree per ancestor.
    await asyncio.to_thread(
        _publish_subtree_cost_to_ancestors,
        conversation_store,
        session_id,
    )
    return raw_tokens


async def _persist_model_change_note(
    session_id: str,
    model_override: str | None,
    conversation_store: ConversationStore,
) -> None:
    """
    Append a ``[System: ...]`` transcript note recording a model switch.

    Records a web/REPL ``/model`` change as a user-role system marker
    (the web UI renders ``[System: ...]`` user messages centered + muted
    via ``SystemMessageView``) so the user gets a durable record in the
    conversation that the switch happened — not just a transient composer
    hint. Persisted through the store as append-only history (does NOT
    start an agent turn, unlike the message-post path) and published over
    SSE so connected clients render it live.

    The caller gates this to **non-native** sessions (those WITHOUT an
    ``omnigent.wrapper`` native label, via ``_is_native_terminal_session``)
    and to real ``/model`` commands: claude-native / codex-native manage
    their model through the in-TUI picker / launch flag and must not receive
    an injected AP-side item, and ``silent`` bind-time auto-applies are
    skipped (see the ``live_forward`` guard in ``update_session``). The gate
    keys on ``omnigent.wrapper`` rather than ``omnigent.ui == "terminal"``
    because the latter is also set on chat-first SDK sessions that expose a
    REPL terminal view (e.g. polly / debby), which DO want the note. The note
    is a user-role message, so the agent sees it in history on the next turn —
    consistent with other ``[System: ...]`` markers (timer fired, sub-agent
    done).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param model_override: The new model id, e.g.
        ``"databricks-gpt-5-4"``, or ``None`` when the override was
        cleared back to the agent default.
    :param conversation_store: Store used to append the note item.
    :returns: None.
    """
    text = (
        f"[System: model changed to {model_override}]"
        if model_override is not None
        else "[System: model reset to the agent default]"
    )
    item = NewConversationItem(
        type="message",
        response_id=generate_task_id(),
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": text}],
        ),
    )
    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    _publish_external_conversation_item(session_id, persisted_items[0])


async def _resolve_elicitation(
    session_id: str,
    data: dict[str, Any],
    runner_router: RunnerRouter | None,
    conversation_store: ConversationStore | None = None,
) -> None:
    """
    Resolve one outstanding elicitation from an approval payload.

    Shared by the two entry points that deliver a verdict for a
    parked elicitation: the ``type == "approval"`` branch of
    ``POST /v1/sessions/{id}/events`` and the dedicated
    ``POST /v1/sessions/{id}/elicitations/{eid}/resolve`` URL
    endpoint (URL-based elicitation). Both converge here so
    resolution semantics — server-side harness Future, sidebar
    badge clear, and runner forward — stay identical regardless of
    how the verdict arrived.

    Three effects, in order:

    1. **Server-side harness Future.** Claude-native permission
       hooks (and any other server-parked elicitation) register a
       Future in ``_harness_elicitation_registry``. If one exists
       for this id, is unresolved, and is owned by *this* session
       (cross-user guard), set its result. An
       ownership mismatch silently skips resolution — the runner
       forward below still fires so a runner-side elicitation with
       the same id can reject it on its own terms.
    2. **Sidebar badge clear.** Publish
       ``response.elicitation_resolved`` so every subscribed client
       (other tabs, the REPL TUI) flips its ``ApprovalCard`` and the
       pending-elicitation badge decrements. Idempotent.
    3. **Runner forward.** Runner-side elicitations (policy
       approvals parked in the runner's ``_pending_approvals`` dict)
       resolve when the approval event reaches the runner's
       ``/events``. Forwarded as a canonical ``approval`` event.

    :param session_id: Session/conversation identifier that owns
        the elicitation, e.g. ``"conv_abc123"``.
    :param data: Approval payload carrying the ``elicitation_id``
        correlation key plus the MCP ``ElicitationResult`` fields
        (``action``, optional ``content``), e.g.
        ``{"elicitation_id": "elicit_abc", "action": "accept"}``.
    :param runner_router: Router used to resolve the session's bound
        runner for the forward, or ``None`` in in-process setups
        (the forward is skipped when no runner is bound).
    :param conversation_store: Optional store used to mirror the
        resolved signal into ancestor streams when ``session_id`` is
        a child session. ``None`` keeps the signal scoped locally.
    """
    # Empty-string default is intentional, NOT a fail-loud miss: the
    # resolve-URL caller always supplies the id (it comes from the URL
    # path), but the public ``approval`` event caller may post a
    # malformed body. A missing id degrades gracefully below (no Future
    # matches, no resolved event published) rather than 500-ing the
    # client — the runner forward still fires so the runner can reject.
    elicitation_id = data.get("elicitation_id", "")
    harness_future = _harness_elicitation_registry.get(elicitation_id)
    if harness_future is not None and not harness_future.done():
        # Only the session that owns this elicitation
        # may resolve its server-side Future. A mismatch skips
        # resolution (the runner forward still fires below).
        if _harness_elicitation_owners.get(elicitation_id) == session_id:
            result_payload = {k: v for k, v in data.items() if k != "elicitation_id"}
            try:
                harness_future.set_result(
                    ElicitationResult.model_validate(result_payload),
                )
            except ValidationError:
                _logger.warning(
                    "Invalid approval payload for %r",
                    elicitation_id,
                    exc_info=True,
                )
    elif harness_future is None and isinstance(elicitation_id, str) and elicitation_id:
        # Nothing parked (severed long-poll mid-retry, or a runner-side
        # id that just ages out) — tombstone the verdict so a re-park
        # returns it; consume is session-checked, so no cross-session use.
        result_payload = {k: v for k, v in data.items() if k != "elicitation_id"}
        try:
            pre_resolved = ElicitationResult.model_validate(result_payload)
        except ValidationError:
            pre_resolved = None
        if pre_resolved is not None:
            _prune_pre_resolved_harness_elicitations()
            _harness_pre_resolved_elicitations[elicitation_id] = _PreResolvedHarnessElicitation(
                session_id=session_id,
                created_at=time.time(),
                result=pre_resolved,
            )
            _prune_pre_resolved_harness_elicitations()
    # Wake a currently-parked long-poll via resolved_elsewhere, not only its
    # Future: setting the Future alone races the sever/re-park cycle and the
    # ASK-gated call hangs. Set the event directly; the signal helper's
    # parked-is-None branch would clobber the verdict-carrying tombstone.
    if isinstance(elicitation_id, str) and elicitation_id:
        _parked = _harness_parked_elicitations.get(elicitation_id)
        if _parked is not None and _harness_elicitation_owners.get(elicitation_id) == session_id:
            _parked.resolved_elsewhere.set()

    # Fan-out for every other subscribed client (other tabs, REPL
    # TUI). Idempotent vs. the runner's own ``wait_for_user_approval``
    # finally / harness hook finally — those also publish for the id.
    if isinstance(elicitation_id, str) and elicitation_id:
        _publish_elicitation_resolved(session_id, elicitation_id)
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_resolved_to_ancestors,
                conversation_store,
                session_id,
                elicitation_id,
            )
    # Runner-side elicitations (policy approvals, scaffold dispatch)
    # resolve when the canonical approval event reaches the runner.
    await _forward_approval_to_runner(session_id, data, runner_router)


def _spawn_native_approval_popup_forward(
    session_id: str, elicitation_id: str, message: str, policy_name: str | None = None
) -> None:
    """
    Ask the bound runner to pop a native-terminal modal for a parked ASK.

    Fire-and-forget. Forwards the same ``cost_approval_popup`` control event
    the cost gate uses — the runner dispatch + popup launcher are
    policy-agnostic — so a user working in the native terminal can answer a
    parked tool-policy ASK there, not only in the web ApprovalCard. (Native
    tool-policy ASKs were moved server-side, which took them out of the
    TUI; this puts them back.) The popup resolves the SAME elicitation via
    the same resolve endpoint the web card uses, so whichever surface
    answers first releases the gate. Non-native harnesses 204 no-op on the
    runner.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: The parked elicitation's id, e.g. ``"elicit_x"``.
    :param message: The approval reason shown in the popup.
    :param policy_name: Name of the deciding policy, rendered as the
        popup header so it reflects the actual policy rather than a
        hardcoded cost-budget label. ``None`` falls back to a generic
        header on the runner.
    :returns: None. Fire-and-forget: forwarding failures (runner offline,
        no runner bound) are swallowed by ``_forward_session_change_to_runner``
        and never block the gate — the web ApprovalCard remains the surface.
    """

    async def _forward() -> None:
        await _forward_session_change_to_runner(
            session_id,
            get_server_runner_router(),
            {
                "type": "cost_approval_popup",
                "elicitation_id": elicitation_id,
                "message": message,
                "policy_name": policy_name,
            },
        )

    task = asyncio.create_task(_forward())
    _native_popup_forward_tasks.add(task)
    task.add_done_callback(_native_popup_forward_tasks.discard)


def _spawn_native_blocked_notice_forward(
    session_id: str, message: str, policy_name: str | None = None
) -> None:
    """
    Ask the bound runner to pop an INFORMATIONAL hard-block notice on the pane.

    The request-phase HARD-DENY counterpart of
    :func:`_spawn_native_approval_popup_forward`: no approve/decline (the prompt
    is blocked). opencode can only hard-block a prompt by its policy plugin
    throwing, which opencode renders as a generic "Unexpected server error";
    this forwards the policy reason so the runner can surface it as a dismissable
    tmux popup on the opencode pane. Fire-and-forget; the runner dispatch is
    harness-gated (only ``opencode-native`` pops — claude/codex already show a
    clean ``UserPromptSubmit`` block, so they no-op).

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param message: The block reason shown in the popup.
    :param policy_name: Deciding policy, rendered as the popup header. ``None``
        falls back to a generic header on the runner.
    :returns: None. Forwarding failures (runner offline / none bound) are
        swallowed and never affect the verdict.
    """

    async def _forward() -> None:
        await _forward_session_change_to_runner(
            session_id,
            get_server_runner_router(),
            {
                "type": "policy_blocked_notice",
                "message": message,
                "policy_name": policy_name,
            },
        )

    task = asyncio.create_task(_forward())
    _native_popup_forward_tasks.add(task)
    task.add_done_callback(_native_popup_forward_tasks.discard)


async def _hold_native_ask_gate(*args: Any, **kwargs: Any) -> bool:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._hold_native_ask_gate(*args, **kwargs)


async def _hold_native_ask_gate_impl(
    request: Request,
    *,
    session_id: str,
    phase: Phase,
    data: dict[str, Any],
    engine: PolicyEngine,
    result: PolicyResult,
    conversation_store: ConversationStore,
    elicitation_id: str | None = None,
) -> bool:
    """
    Hold a server-side ASK gate until a human resolves it.

    Publishes a ``response.elicitation_request`` (the web UI / REPL
    render the approve card) and parks a server-side Future via
    :func:`_publish_and_wait_for_harness_elicitation`, exactly as the
    ``PermissionRequest`` hook does. The human approves through the
    elicitation's resolve URL; this collapses the verdict to a single
    boolean the caller maps to ALLOW / DENY.

    Used for any phase whose ASK must be resolved on the server rather
    than by a runner-side ``wait_for_user_approval`` park:
    :attr:`Phase.TOOL_CALL` (the native ``PreToolUse`` hook gate) and
    :attr:`Phase.REQUEST` (the user-message input gate, which has no
    runner in the loop yet — see :func:`_evaluate_input_policy`).

    Unlike the old ASK→``defer`` path, the gate lives on the server,
    so a permissive native ``permission_mode`` (``acceptEdits`` /
    ``bypassPermissions``) cannot skip it — the action stays blocked
    until a real human verdict. Timeout / disconnect fail closed
    (return ``False`` → DENY).

    On approve, the ASK-accumulated ``set_labels`` / ``state_updates``
    are applied (POLICIES.md §7.2: side effects land only on approve);
    a denied / timed-out ASK leaves no trace.

    :param request: FastAPI request, for upstream-disconnect detection
        inside the parking helper.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param phase: Enforcement phase being gated, e.g.
        :attr:`Phase.TOOL_CALL` or :attr:`Phase.REQUEST`.
    :param data: The proto event ``data`` — for a tool call,
        ``{"name": "Bash", "arguments": {"command": "ls"}}``; for a
        request, the user ``message`` body
        (``{"role": "user", "content": [...]}``).
    :param engine: The policy engine, used to resolve the per-policy
        ``ask_timeout`` and to apply approved side effects.
    :param result: The composed ASK :class:`PolicyResult` — carries
        the reason, deciding_policy, and withheld set_labels.
    :param conversation_store: Store used to mirror child-session
        prompts into ancestor streams.
    :param elicitation_id: Optional stable re-attach id from the
        calling hook, e.g. ``"elicit_evaluate_abc123"``. When supplied,
        ``_publish_and_wait_for_harness_elicitation`` re-attaches to the
        existing parked elicitation rather than publishing a new card —
        used by ``POST /policies/evaluate`` retries so a hook retry after
        a transient 5xx / connect-drop does not prompt the human twice.
        ``None`` mints a fresh id (the default for non-retry callers).
    :returns: ``True`` iff a human accepted; ``False`` on cancel /
        timeout / disconnect (fail closed).
    :raises ElicitationDeclinedError: when the human explicitly
        declines (``action == "decline"``). Callers should abort the
        turn rather than continuing with a DENY.
    """
    tool_name = data.get("name")
    tool_input = data.get("arguments")
    params = ElicitationRequestParams(
        mode="form",
        message=result.reason or "Approval required",
        requestedSchema={},
        phase=phase.value,
        policy_name=result.deciding_policy or "unknown",
        content_preview=json.dumps(data)[:1024],
    )
    # Per-policy ``ask_timeout`` override wins over the spec-level default.
    timeout_s = float(resolve_ask_timeout(engine, result))
    # Use the caller-supplied id when present (hook retries re-attach to
    # the same elicitation); otherwise mint a fresh one so we can surface
    # this ASK in the native terminal before parking on the web verdict.
    if elicitation_id is None:
        elicitation_id = f"elicit_{secrets.token_hex(16)}"
    _spawn_native_approval_popup_forward(
        session_id, elicitation_id, params.message, result.deciding_policy
    )
    verdict = await _publish_and_wait_for_harness_elicitation(
        request,
        session_id=session_id,
        params=params,
        timeout_s=timeout_s,
        elicitation_id=elicitation_id,
        conversation_store=conversation_store,
        tool_name=tool_name if isinstance(tool_name, str) else None,
        tool_input=tool_input if isinstance(tool_input, dict) else None,
    )
    # Explicit user decline → raise so callers can abort the turn rather
    # than feeding a DENY message to the LLM and letting it continue.
    if verdict is not None and verdict.action == "decline":
        raise ElicitationDeclinedError(
            result.reason or "",
            policy_name=result.deciding_policy,
        )
    approved = verdict is not None and verdict.action == "accept"
    if approved:
        # POLICIES.md §7.2: writes accumulated by the ASKing policy
        # land only on approve.
        if result.set_labels:
            engine.apply_label_writes(result.set_labels)
        if result.state_updates:
            with contextlib.suppress(ConversationNotFoundError):
                engine.apply_state_updates(result.state_updates)
    return approved


async def _persist_external_codex_subagent_start(
    parent_id: str,
    parent_conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Mint or update a child Conversation for a Codex AgentControl sub-agent.

    Idempotent: repeated POSTs for the same ``thread_id`` return the
    existing child id and upsert any new labels.

    :param parent_id: Parent codex-native conversation id, e.g.
        ``"conv_parent987"``.
    :param parent_conv: Pre-fetched parent row.
    :param body: POST event body with ``data.thread_id`` required.
    :param conversation_store: Store for reading/creating child rows.
    :returns: Child conversation id, e.g. ``"conv_child456"``.
    :raises OmnigentError: If ``thread_id`` is missing or parent has
        no bound agent.
    """
    thread_id = body.data.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise OmnigentError(
            "external_codex_subagent_start requires non-empty data.thread_id",
            code=ErrorCode.INVALID_INPUT,
        )
    if parent_conv.agent_id is None:
        raise OmnigentError(
            f"parent session {parent_id!r} has no agent_id; cannot "
            "create a codex-native sub-agent child",
            code=ErrorCode.INVALID_INPUT,
        )
    existing = await asyncio.to_thread(
        _find_codex_native_subagent_child, conversation_store, parent_id, thread_id
    )
    labels = _codex_subagent_labels_from_body(thread_id, body)
    if existing is not None:
        await asyncio.to_thread(conversation_store.set_labels, existing.id, labels)
        return existing.id
    return await _create_and_publish_codex_child(
        parent_id, parent_conv, thread_id, labels, conversation_store
    )


async def _persist_external_conversation_item(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    created_by: str | None = None,
    background_title_coordinator: BackgroundSessionTitleCoordinator | None = None,
) -> str:
    """
    Persist and broadcast a conversation item produced outside AP.

    This is the transcript bridge path for native Claude. It appends
    user messages, assistant messages, tool calls, and tool results
    without starting or steering the placeholder Omnigent agent.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: Conversation row for title seeding.
    :param body: External item event body.
    :param conversation_store: Store used to append the item.
    :param created_by: Authenticated identity of the actor whose
        request triggered the forwarder POST, e.g.
        ``"alice@example.com"``. Used to attribute user messages typed
        directly in the native terminal (no pending-input entry exists
        for those). ``None`` in single-user / unauthenticated mode —
        no label is stamped in that case.
    :returns: Store-assigned conversation item id.
    """
    item = _parse_external_conversation_item(body)
    # A native user message round-tripping back from the transcript:
    # drain its optimistic pending-input entry (FIFO) and fold the
    # entry's file blocks (image / file) into the item BEFORE persisting.
    # The transcript is text-only, so without this the image is dropped
    # from durable history and disappears on every reload / navigation.
    # The vendor CLI's own interrupt record is exempt: it is synthesized by
    # Claude (not a queued web message) and has no pending entry, so
    # draining for it would hand the queued message's uploads to the marker.
    cleared_pending_id: str | None = None
    skipped_kiro_pending: list[pending_inputs.DrainedInput] = []
    if (
        item.type == "message"
        and isinstance(item.data, MessageData)
        and item.data.role == "user"
        and not item.data.is_meta
        and not _is_native_interrupt_record(item.data)
    ):
        if _is_kiro_native_session(conv):
            text = _message_text(item.data.content) or ""
            matched = pending_inputs.resolve_matching_text(session_id, text)
            drained = matched.matched
            skipped_kiro_pending = matched.skipped
        else:
            drained = pending_inputs.resolve_oldest(session_id)
        if drained is not None:
            cleared_pending_id = drained.pending_id
            item = _merge_pending_file_blocks(item, drained.content)
            # Apply the original sender's identity recorded at POST time.
            # The transcript forwarder is the single writer here and has no
            # auth context, so the persisted item would otherwise have
            # created_by=None, causing session.input.consumed to broadcast
            # without an author — the label would flash in from the optimistic
            # bubble then disappear once the committed item arrived.
            if drained.created_by is not None and item.created_by is None:
                item = item.model_copy(update={"created_by": drained.created_by})
        elif item.created_by is None and created_by is not None:
            # No pending entry — direct terminal input. Fall back to the
            # identity authenticated on the forwarder's own request.
            item = item.model_copy(update={"created_by": created_by})
    for skipped in skipped_kiro_pending:
        await _persist_skipped_kiro_pending_input(
            session_id,
            skipped,
            conversation_store,
        )
    pending_background_title = prepare_background_session_title(
        coordinator=background_title_coordinator,
        conversation=conv,
        event=SessionEventInput(type=item.type, data=item.data.model_dump()),
    )
    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    await _seed_missing_title_from_user_message(conv, item, conversation_store)
    if pending_background_title is not None:
        pending_background_title.schedule()
    persisted = persisted_items[0]
    _publish_external_conversation_item(
        session_id, persisted, cleared_pending_id=cleared_pending_id
    )
    _drive_terminal_resolved_elicitation(session_id, persisted)
    return persisted.id


async def _persist_skipped_kiro_pending_input(
    session_id: str,
    skipped: pending_inputs.DrainedInput,
    conversation_store: ConversationStore,
) -> None:
    """Persist a Kiro web input that never appeared in Kiro's JSONL transcript."""
    turn_id = generate_task_id()
    user_item = NewConversationItem(
        type="message",
        response_id=turn_id,
        data=MessageData(role="user", content=skipped.content),
        created_by=skipped.created_by,
    )
    error = ErrorData(
        source="execution",
        code="kiro_native_prompt_not_recorded",
        message=(
            "Kiro did not accept this web message into its structured session transcript. "
            "The native terminal may have shown the underlying error."
        ),
    )
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [
            user_item,
            NewConversationItem(type="error", response_id=turn_id, data=error),
        ],
    )
    _publish_input_consumed(
        session_id,
        persisted_items[0],
        cleared_pending_id=skipped.pending_id,
    )
    _publish_external_conversation_item(session_id, persisted_items[1])


async def _enrich_idle_status_with_subagent_output(
    data: dict[str, Any],
    status: str,
    session_id: str,
    conversation_store: ConversationStore,
) -> dict[str, Any]:
    """
    Attach a native sub-agent's durable assistant text to an idle status edge.

    Shared by both native sub-agent delivery paths (the codex
    ``external_session_status`` POST handler and the claude-native relay
    forward) so the parent inbox result carries the child's output. Native
    harnesses mirror transcript items to the store, not runner memory, so the
    text is read here and forwarded with the idle edge.

    :param data: The ``external_session_status`` ``data`` to enrich, e.g.
        ``{"status": "idle"}``.
    :param status: Status edge; only ``"idle"`` is enriched.
    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param conversation_store: Store read for the child's assistant text.
    :returns: ``data`` with ``"output"`` added when an idle edge has a
        persisted assistant message; otherwise unchanged.
    """
    if status != "idle":
        return data
    output = await asyncio.to_thread(
        _latest_assistant_text_from_store,
        conversation_store,
        session_id,
    )
    if output is None:
        return data
    return {**data, "output": output}


async def _heal_subagent_runner_binding_via_parent(
    child_conv: Conversation,
    runner_router: RunnerRouter | None,
    tunnel_registry: TunnelRegistry | None,
    conversation_store: ConversationStore,
) -> httpx.AsyncClient | None:
    """
    Repair a sub-agent's stale ``runner_id`` and return its parent's live client.

    A sub-agent child copies its parent's ``runner_id`` once at creation and is
    never repointed when the parent's runner is later relaunched.  This walks up
    the ancestor chain (immediate parent → root) to find the nearest live runner,
    heals the child's DB row to point at that runner, and returns the live
    ``httpx.AsyncClient`` so the caller can proceed as if the binding were always
    current.

    After healing, the caller must re-read the conversation row (the healed
    ``runner_id`` is now in the DB).  Whether session re-initialization is
    needed depends on the harness:

    - **Native-terminal harnesses** (``pi-native``, ``claude-native``): the
      replacement runner does not automatically spawn the child's terminal
      process — the caller must set ``_runner_needs_session_init = True`` so
      ``_ensure_runner_session_initialized`` creates it.
    - **SDK / non-native harnesses**: all active sub-agent sessions are loaded
      in-process by the runner on startup; the parent's live runner already
      holds the child's session state, so ``_runner_needs_session_init``
      should remain ``False`` (calling ``_ensure_runner_session_initialized``
      would be a no-op at best and a spurious timeout at worst).

    :param child_conv: The sub-agent child whose ``runner_id`` may be stale.
    :param runner_router: Router used to resolve runner clients, or ``None`` in
        in-process setups.
    :param tunnel_registry: Runner-tunnel registry used to await the ancestor
        runner's (re)connect, or ``None`` in setups without runner tunnels.
    :param conversation_store: Store used to look up ancestors and persist the
        healed ``runner_id``.
    :returns: The live ``httpx.AsyncClient`` for the nearest live ancestor runner
        after healing, or ``None`` when no live ancestor could be found.
    """
    # Walk the ancestor chain (immediate parent first, then root) to find a
    # live runner.  A single hop covers the common case; two hops cover nested
    # sub-agents where the immediate parent's runner is also stale but the root
    # is still healthy.
    candidate_ids: list[str] = []
    if child_conv.parent_conversation_id and child_conv.parent_conversation_id != child_conv.id:
        candidate_ids.append(child_conv.parent_conversation_id)
    if (
        child_conv.root_conversation_id
        and child_conv.root_conversation_id != child_conv.id
        and child_conv.root_conversation_id not in candidate_ids
    ):
        candidate_ids.append(child_conv.root_conversation_id)
    if not candidate_ids:
        return None

    live_runner_id: str | None = None
    live_client: httpx.AsyncClient | None = None
    for ancestor_id in candidate_ids:
        ancestor = await asyncio.to_thread(conversation_store.get_conversation, ancestor_id)
        if ancestor is None or ancestor.runner_id is None:
            continue
        ancestor_runner_id = ancestor.runner_id
        # Wait briefly for the ancestor's tunnel — covers the reconnect gap
        # right after a relaunch.  When no registry is wired (in-process /
        # tests) fall back to a best-effort direct resolve.
        if tunnel_registry is not None:
            client = await _wait_for_runner_client(
                ancestor_id,
                runner_router,
                tunnel_registry,
                runner_id=ancestor_runner_id,
                timeout_s=_SUBAGENT_FORWARD_RECONNECT_WAIT_S,
            )
        else:
            client = await _get_runner_client(ancestor_id, runner_router)
        if client is not None:
            live_runner_id = ancestor_runner_id
            live_client = client
            break

    if live_runner_id is None or live_client is None:
        return None

    if live_runner_id != child_conv.runner_id:
        # Heal the divergence so this child's row matches the live runner: the
        # next forward resolves directly and a future ``_on_runner_connect``
        # (which rebinds by matching runner_id) can recover it.
        try:
            await asyncio.to_thread(
                conversation_store.replace_runner_id, child_conv.id, live_runner_id
            )
        except ConversationNotFoundError:
            # The child was deleted between reading it and this heal (e.g.
            # removed mid-teardown).  Degrade gracefully rather than surfacing
            # a benign race as an unhandled 500.
            return None

    return live_client


async def _recover_subagent_status_forward_via_parent(
    child_conv: Conversation,
    runner_router: RunnerRouter | None,
    tunnel_registry: TunnelRegistry | None,
    conversation_store: ConversationStore,
    forward_body: dict[str, Any],
) -> _RunnerForwardResult | None:
    """
    Re-deliver a sub-agent terminal status through the parent's live runner.

    A native sub-agent child copies its parent's ``runner_id`` once, at
    creation (``create_conversation(..., runner_id=parent_conv.runner_id)`` —
    see :func:`_persist_external_subagent_start`). It is never repointed when
    the runner is later relaunched under a freshly minted ``runner_id`` (a host
    relaunch after a tunnel drop / server redeploy / crash mints a new binding
    token; only the *parent* conversation is rebound, via the PATCH path on its
    next message). The child then points at a permanently offline ``runner_id``,
    so its terminal ``idle``/``failed`` forward resolves no runner client and
    503s forever (``_forward_session_change_to_runner`` → ``None`` →
    :func:`_require_external_status_forward`). The parent never receives the
    child's inbox result and hangs with no timeout.

    A child always runs on its parent's runner, so the live binding is the
    parent's. This re-resolves the forward through the parent/root
    conversation's *current* ``runner_id``: it waits briefly for that runner's
    tunnel to (re)connect (covering the reconnect gap right after a relaunch),
    heals the child's stale ``runner_id`` so future forwards and
    ``_on_runner_connect`` resolve it correctly, and retries the forward.

    :param child_conv: The sub-agent child conversation whose terminal-status
        forward could not reach its pinned runner.
    :param runner_router: Router used to resolve the bound runner client, or
        ``None`` in in-process setups.
    :param tunnel_registry: Runner-tunnel registry used to await the parent
        runner's (re)connect, or ``None`` in setups without runner tunnels.
    :param conversation_store: Store used to look up the parent and persist the
        child's healed ``runner_id``.
    :param forward_body: The ``external_session_status`` event body to re-POST.
    :returns: The retry's :class:`_RunnerForwardResult` when a live parent
        runner was resolved, or ``None`` when none could be (the caller then
        fails the forward as before).
    """
    client = await _heal_subagent_runner_binding_via_parent(
        child_conv, runner_router, tunnel_registry, conversation_store
    )
    if client is None:
        return None
    return await _forward_session_change_to_runner(
        child_conv.id,
        runner_router,
        forward_body,
    )


def _drive_terminal_resolved_elicitation(session_id: str, persisted: ConversationItem) -> None:
    """
    Feed a mirrored tool item into the terminal-resolved fast path.

    A ``function_call`` records its tool identity by ``call_id`` so the
    matching ``function_call_output`` can be correlated back to a parked
    permission prompt. A ``function_call_output`` means the gated tool
    already ran (or was rejected) in the native terminal, so the prompt
    the web UI may still be showing was resolved there — resolve the
    matching parked prompt now instead of waiting for the hook timeout.
    Other item types are ignored.

    :param session_id: Omnigent conversation id the item was mirrored for,
        e.g. ``"conv_abc123"``.
    :param persisted: The stored conversation item the forwarder just
        mirrored via ``external_conversation_item``.
    """
    data = persisted.data
    if persisted.type == "function_call" and isinstance(data, FunctionCallData):
        try:
            parsed = json.loads(data.arguments) if data.arguments else {}
        except json.JSONDecodeError:
            parsed = {}
        _recent_mirrored_tool_calls[data.call_id] = _MirroredToolCall(
            tool_name=data.name,
            tool_input=parsed if isinstance(parsed, dict) else {},
        )
    elif persisted.type == "function_call_output" and isinstance(data, FunctionCallOutputData):
        identity = _recent_mirrored_tool_calls.get(data.call_id)
        if identity is not None:
            _signal_terminal_resolved_harness_elicitation(
                session_id, identity.tool_name, identity.tool_input
            )


async def _publish_runner_recovered_status(*args: Any, **kwargs: Any) -> None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._publish_runner_recovered_status(*args, **kwargs)


async def _publish_runner_recovered_status_impl(
    session_id: str,
    conversation_store: ConversationStore,
    *,
    require_disconnect_code: bool = False,
) -> None:
    """
    Clear a stale failed session status after runner recovery.

    Native terminal startup failures are sticky against trailing
    ``idle`` PTY-quiescence signals so users can see the error. A
    later runner bind/session-init success is different: it proves AP
    reached a live runner for this session again, so the old failure is
    stale and should not keep the conversation marked failed until the
    next user turn emits ``running``.

    Recovery also clears the durable ``last_task_error`` labels the
    disconnect relay persisted. Those labels survive reload so an
    ongoing disconnect still projects a "Disconnected" pill, but once
    the runner is reachable again the session is healthy and idle — the
    pill must drop without waiting for the next ``running`` edge.

    An explicit rebind/handshake (a PATCH ``/clear`` or ``/switch``, or
    the message-forward session-init) is a user-driven proof the runner
    is live, so it clears any stale ``failed`` state. A *passive* tunnel
    reconnect is weaker: the process merely came back on its own, saying
    nothing about a genuine task error. Callers on that path pass
    ``require_disconnect_code=True`` so only a ``runner_disconnected``
    failure is cleared — a genuine task failure (``response.failed`` / a
    setup error with any other ``last_task_error`` code) survives the
    reconnect, keeping the red "Failed" pill instead of silently flipping
    it back to idle and hiding the error.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conversation_store: Store used to read the persisted error
        code and clear the labels on genuine recovery.
    :param require_disconnect_code: When ``True`` (passive-reconnect
        caller), only clear if the persisted ``last_task_error.code`` is
        ``runner_disconnected``; when ``False`` (default, explicit
        rebind/handshake), clear any stale ``failed`` state. Labels are
        cleared in both cases.
    :returns: None.
    """
    if _session_status_cache.get(session_id) != "failed":
        return
    # A passive reconnect must distinguish a benign runner disconnect
    # from a real task failure: both land the cache on "failed", but only
    # the disconnect persists a ``runner_disconnected`` label. The
    # reconnect proves the runner is reachable again, which invalidates a
    # disconnect failure but says nothing about a genuine task error —
    # leave that one alone. Explicit rebinds skip this guard.
    if require_disconnect_code:
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        last_error = _last_task_error_from_labels(conv.labels) if conv is not None else None
        if last_error is None or last_error.get("code") != "runner_disconnected":
            return
    _session_status_cache[session_id] = "idle"
    session_live_state.persist_live_status(session_id, "idle")
    event = SessionStatusEvent(
        type="session.status",
        conversation_id=session_id,
        status="idle",
        error=None,
    )
    session_stream.publish(session_id, event.model_dump())
    await _persist_session_status_error_labels(session_id, None, conversation_store)


async def _mark_runner_sessions_offline_impl(
    convs: list[Conversation],
    error: ErrorDetail,
    conversation_store: ConversationStore,
    *,
    fail_idle_top_level: bool = False,
) -> None:
    """
    Fail the sessions a departed runner actually interrupted, with the cause.

    A runner going away ends in-flight turns; it does not retroactively
    fail work that already finished. Sub-agents ride their parent's runner,
    so the departure reaches every child bound to it — marking the idle
    ones ``failed`` painted a whole Agents rail red for sub-agents that had
    completed successfully. The runner's absence is already carried by
    liveness (``clear_runner_liveness``), which is what drives the
    reconnect affordances, so a session that was not mid-turn needs no
    status edge at all.

    The sessions that DO get failed carry the cause, persisted as durable
    labels. The cause is what lets the client render a benign
    "Disconnected" instead of a red "Failed", and what lets
    :func:`_publish_runner_recovered_status` clear the state when the
    runner comes back — that helper only clears a failure it can identify
    as a disconnect.

    :param convs: Conversations bound to the departed runner, from
        :meth:`ConversationStore.list_conversations_by_runner_id`.
    :param error: The cause to publish and persist, e.g.
        ``ErrorDetail(code="runner_disconnected", message="...")``.
    :param conversation_store: Store used to persist the error labels.
    :param fail_idle_top_level: When ``True``, also fail an idle top-level
        session. A crash report covers the runner that died *before* it
        could run anything, so its session never reached ``running`` and
        would otherwise sit silent; a sub-agent is spawned by an
        already-live runner and can never be in that state, so idle
        children are skipped either way.
    :returns: None.
    """
    for conv in convs:
        # An intentional teardown (Stop / archive) drops the tunnel on
        # purpose. The relay owns that path — it publishes a quiet idle and
        # consumes the marker — so peek without discarding here.
        if conv.id in _intentional_stop_sessions:
            continue
        # Cache first (this replica holds the runner's tunnel, so it saw the
        # turn edges), falling back to the row for a session whose live state
        # was published before a restart.
        live = _session_status_cache.get(conv.id, conv.live_status)
        interrupted = live in ("running", "waiting")
        dead_on_arrival = fail_idle_top_level and conv.kind != "sub_agent"
        if not interrupted and not dead_on_arrival:
            continue
        _publish_status(conv.id, "failed", error)
        await _persist_session_status_error_labels(conv.id, error, conversation_store)


async def _wait_for_host_bound_runner_client(
    session_id: str,
    runner_router: RunnerRouter | None,
    tunnel_registry: TunnelRegistry | None,
    *,
    runner_id: str,
    timeout_s: float,
    runner_exit_reports: RunnerExitReports | None,
    host_conn: HostConnection,
    host_registry: HostRegistry,
) -> httpx.AsyncClient | None:
    """
    Wait for a host-bound runner to connect, ending early if the host
    reports it already gone.

    Races the connect grace (:func:`_wait_for_runner_client`) against a
    one-shot ``host.runner_status`` query, because they answer different
    questions and either can settle the outcome first:

    * The runner connecting — or a crash report — resolves the wait exactly
      as :func:`_wait_for_runner_client` does. This is ground truth and
      always wins when it lands first.
    * Concurrently, the host — the authoritative owner of runner-process
      liveness — may report the runner ``dead`` or ``unknown`` (stopped,
      crashed, or lost to a host restart). That means it will never
      connect, so the wait ends immediately and the caller relaunches
      without burning the rest of the grace.

    Running the query *alongside* the wait rather than before it is what
    keeps the query strictly a speed-up: a host that is too old to answer,
    slow, or silent (verdict ``None`` / ``"alive"``) never shortcuts the
    wait, so the connect grace runs its normal course with no added
    latency.

    :param session_id: Session/conversation identifier.
    :param runner_router: The ``RunnerRouter`` instance, or ``None``.
    :param tunnel_registry: The server's ``TunnelRegistry``, or ``None``.
    :param runner_id: Runner id expected to connect.
    :param timeout_s: Maximum seconds to wait for the connect.
    :param runner_exit_reports: Crash-report store consulted by the
        connect wait to abort early on a reported death.
    :param host_conn: Live host connection to query for liveness.
    :param host_registry: Registry used to enqueue the query frame.
    :returns: The runner HTTP client if it connected, otherwise ``None``
        (timed out, crash report, or host-confirmed dead/unknown).
    """
    connect_task = asyncio.ensure_future(
        _wait_for_runner_client(
            session_id,
            runner_router,
            tunnel_registry,
            runner_id=runner_id,
            timeout_s=timeout_s,
            runner_exit_reports=runner_exit_reports,
        )
    )
    status_task = asyncio.ensure_future(
        _query_host_runner_status(host_conn, host_registry, runner_id)
    )
    try:
        done, _pending = await asyncio.wait(
            {connect_task, status_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # The connect settling is authoritative (client, timeout, or crash
        # report) — the host's opinion no longer matters once it lands.
        if connect_task in done:
            return connect_task.result()
        # Only the status query has resolved so far.
        if status_task.result() in ("dead", "unknown"):
            # Host confirms the runner will never connect — stop waiting.
            return None
        # No verdict ("alive" or an unavailable/too-old/slow host): let the
        # connect grace run to its natural conclusion.
        return await connect_task
    finally:
        outstanding = [t for t in (connect_task, status_task) if not t.done()]
        for task in outstanding:
            task.cancel()
        if outstanding:
            # Drain the cancelled task(s); return_exceptions swallows the
            # CancelledError so cleanup never masks the real return/raise.
            await asyncio.gather(*outstanding, return_exceptions=True)


async def _run_managed_launch(
    *,
    session_id: str,
    owner: str,
    sandbox_config: ManagedSandboxConfig,
    repo: RepoWorkspace | None,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
    relaunch_host: Host | None = None,
    agent_store: AgentStore | None = None,
    agent_id: str | None = None,
) -> None:
    """
    Provision a managed sandbox for a session in the background.

    The ``host_type="managed"`` create returns before the sandbox
    exists; this task carries the rest of the pipeline: provision the
    sandbox + start the host (:func:`launch_managed_host`), bind the
    host + workspace to the session row, launch a runner on the host,
    and wait for that runner's tunnel so a message POST rendezvousing
    on *tracker* can forward immediately once the launch settles.

    The same pipeline serves a sandbox RELAUNCH (*relaunch_host* set):
    a message arriving for a session whose managed sandbox died kicks
    this task with the existing host row, and
    :func:`relaunch_managed_host` provisions a new sandbox generation
    under the same host identity instead of minting a new one.

    Every exit path settles the tracker entry — success via
    ``finish`` (the session then looks like any host-bound session),
    failure via ``fail`` with the reason a waiting message POST
    reports. A session deleted mid-provision is detected at the bind
    step and the fresh sandbox is torn down.

    Server shutdown cancels this task (the lifespan teardown calls
    :func:`cancel_managed_launch_tasks`); an already-provisioned
    sandbox then leaks until the provider's lifetime cap reaps it
    (the armed launch token expires with the same cap).

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param owner: User the managed host acts for — the session
        creator, e.g. ``"alice@example.com"`` (or the reserved local
        user on auth-disabled servers).
    :param sandbox_config: The deployment's sandbox config.
    :param repo: Parsed repository-URL workspace to clone inside the
        sandbox, or ``None`` for an empty workspace.
    :param tracker: The app's :class:`ManagedLaunchTracker`; this
        session's entry was registered by the caller.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels, used to send the
        launch-runner frame. ``None`` in minimal test wirings.
    :param tunnel_registry: Runner-tunnel registry used to await the
        launched runner's connection. ``None`` in minimal test
        wirings (the rendezvous then settles at frame-send).
    :param relaunch_host: Existing managed host row to relaunch a new
        sandbox generation for, or ``None`` for a first launch (a
        fresh host identity is minted).
    The runner's agent classifier is resolved here rather than by the
    caller, so the read runs on the task that already owns the
    single-flight claim: the request path keeps no ``await`` between
    claiming and spawning, and only the winning caller reaches this
    function, so a losing concurrent message pays nothing for it. It is
    re-derived on every launch through the built-in gate, so there is no
    stored classifier to keep in sync — or to forge.

    :param agent_store: Store the classifier is resolved from, or
        ``None`` (a stripped test wiring) to leave the runner
        unclassified.
    :param agent_id: Agent the session is bound to, resolved through the
        built-in gate into the runner Pod's ``omnigent.ai/agent``
        classifier, or ``None`` to leave it unstamped.
    """
    from omnigent.server.managed_hosts import resolve_managed_agent_label

    agent_name: str | None = None
    if agent_store is not None and agent_id is not None:
        agent_name = await asyncio.to_thread(
            resolve_managed_agent_label,
            agent_store,
            agent_id,
            session_id=session_id,
        )
    managed = await _provision_managed_sandbox(
        session_id=session_id,
        owner=owner,
        sandbox_config=sandbox_config,
        repo=repo,
        tracker=tracker,
        host_store=host_store,
        relaunch_host=relaunch_host,
        agent_name=agent_name,
    )
    if managed is None:
        return
    await _bind_and_launch_managed_runner(
        session_id=session_id,
        managed=managed,
        sandbox_config=sandbox_config,
        tracker=tracker,
        conversation_store=conversation_store,
        host_store=host_store,
        host_registry=host_registry,
        tunnel_registry=tunnel_registry,
    )


async def _bind_and_launch_managed_runner(
    *,
    session_id: str,
    managed: ManagedHostLaunch,
    sandbox_config: ManagedSandboxConfig,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
) -> None:
    """
    Bind a provisioned managed host to its session and launch a runner.

    The bind step doubles as the delete-race detector: a session
    deleted while its sandbox provisioned surfaces here as
    ``ConversationNotFoundError``, and the fresh sandbox is torn down
    (the delete route could not see the host binding yet). Settles
    the tracker on every path.

    :param session_id: Session/conversation identifier.
    :param managed: The provision result (host id + workspace).
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels, used to send the
        launch-runner frame. ``None`` in minimal test wirings.
    :param tunnel_registry: Runner-tunnel registry used to await the
        launched runner's connection. ``None`` in minimal test
        wirings (the rendezvous then settles at frame-send).
    """
    from omnigent.server.managed_hosts import terminate_managed_host

    try:
        conv = await asyncio.to_thread(
            conversation_store.set_host_id,
            session_id,
            managed.host_id,
            managed.workspace,
        )
    except ConversationNotFoundError:
        # The session was deleted while its sandbox provisioned. The
        # delete route couldn't see the host binding yet, so tear the
        # fresh sandbox down here (deleting the host row also revokes
        # its launch token).
        _logger.info(
            "Session %s was deleted during managed provisioning; "
            "terminating fresh sandbox on host %s",
            session_id,
            managed.host_id,
        )
        host = await asyncio.to_thread(host_store.get_host, managed.host_id)
        if host is not None:
            await terminate_managed_host(host, host_store, sandbox_config)
        tracker.fail(session_id, "session was deleted while its sandbox was provisioning")
        _publish_sandbox_status(
            session_id, "failed", "session was deleted while its sandbox was provisioning"
        )
        return
    # Host bound; what remains is launching the runner and waiting
    # for its tunnel.
    _publish_sandbox_status(session_id, "connecting")
    runner_id: str | None = None
    if host_registry is not None:
        host_conn = host_registry.get(managed.host_id)
        if host_conn is not None:
            launch_attempt = await _launch_runner_on_host(
                conv,
                conversation_store,
                host_registry,
                host_conn,
            )
            if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
                # The sandbox image should bake in the harness, but if the
                # host refuses, fail the launch loudly (mirroring the
                # delete-during-provisioning path) rather than waiting out
                # the connect timeout for a runner that will never appear.
                reason = launch_attempt.error or "harness not configured on the sandbox host"
                tracker.fail(session_id, reason)
                _publish_sandbox_status(session_id, "failed", reason)
                return
            runner_id = launch_attempt.runner_id
    if runner_id is not None and tunnel_registry is not None:
        connected = await _wait_for_managed_runner_tunnel(
            session_id,
            runner_id,
            tunnel_registry,
            tracker,
        )
        if not connected:
            return
    tracker.finish(session_id)
    _publish_sandbox_status(session_id, "ready")


async def _maybe_relaunch_managed_sandbox(
    *,
    session_id: str,
    conv: Conversation,
    app_state: Any,
    conversation_store: ConversationStore,
) -> bool:
    """
    Relaunch a dead managed sandbox for a session, if it has one.

    Called from the message-dispatch relaunch path when the session's
    host tunnel is gone. For an external (laptop) host that is the end
    of the line, but a managed host's sandbox is RELAUNCHABLE: the
    host row is durable, so a new sandbox generation can be provisioned
    under the same host identity — "send a message to wake the
    sandbox", mirroring how a message relaunches a dead runner on a
    live host.

    Single-flighted through the app's :class:`ManagedLaunchTracker`:
    the first message kicks the background relaunch, concurrent and
    later messages rendezvous on the same entry (the check-then-begin
    below has no ``await`` between check and begin, so it is atomic on
    the event loop). A previously FAILED attempt's retained entry is
    replaced — every new message retries.

    :param session_id: Session/conversation identifier.
    :param conv: The session row (``host_id`` set; caller guards).
    :param app_state: ``request.app.state`` — supplies the host store,
        sandbox config, tracker, and registries.
    :param conversation_store: Store holding the session row.
    :returns: ``True`` when a relaunch engaged and settled
        successfully (the session row is re-bound; re-resolve the
        runner client). ``False`` when the host is not a managed
        sandbox or managed hosts are not configured — the caller
        falls through to the normal unavailable handling.
    :raises OmnigentError: 503 when the relaunch failed or timed out.
    """
    host_store = getattr(app_state, "host_store", None)
    sandbox_config = getattr(app_state, "sandbox_config", None)
    tracker = getattr(app_state, "managed_launches", None)
    if host_store is None or sandbox_config is None or tracker is None:
        return False
    if conv.host_id is None:
        return False
    host = await asyncio.to_thread(host_store.get_host, conv.host_id)
    if host is None or host.sandbox_provider is None:
        return False
    if await asyncio.to_thread(host_store.is_online, conv.host_id):
        host_registry = getattr(app_state, "host_registry", None)
        host_conn = host_registry.get(conv.host_id) if host_registry is not None else None
        if not (host_resume_supported(host, sandbox_config) and host_conn is None):
            # The host row still reads live (status online with a fresh
            # heartbeat). For non-resumable providers or a live local tunnel,
            # avoid replacing a healthy workspace and let normal unavailable
            # handling surface the transient. Resumable managed hosts are the
            # exception: an idle-paused VM can leave a fresh DB row while this
            # process has no usable tunnel, so the first post-idle message must
            # attempt a wake immediately.
            return False
    launch = tracker.get(session_id)
    if launch is None or launch.settled.is_set():
        # A resumable managed host whose sandbox merely idle-stopped is WOKEN
        # in place (resume: same sandbox + workspace volume) rather than
        # relaunched onto a fresh empty sandbox — same gate the wake itself
        # uses (host_resume_supported). Both run in the background through this
        # same tracker, so the message parks on the rendezvous either way; only
        # the provision step differs.
        if host_resume_supported(host, sandbox_config):
            _kick_managed_wake(
                session_id=session_id,
                conv=conv,
                sandbox_config=sandbox_config,
                tracker=tracker,
                conversation_store=conversation_store,
                host_store=host_store,
                app_state=app_state,
            )
        else:
            _kick_managed_relaunch(
                session_id=session_id,
                conv=conv,
                host=host,
                sandbox_config=sandbox_config,
                tracker=tracker,
                conversation_store=conversation_store,
                host_store=host_store,
                app_state=app_state,
            )
        launch = tracker.get(session_id)
    if launch is not None:
        await _await_settled_managed_launch(launch)
    return True


async def _maybe_wake_stale_resumable_managed_sandbox(
    *,
    session_id: str,
    conv: Conversation,
    app_state: Any,
    conversation_store: ConversationStore,
) -> bool:
    """
    Wake a resumable managed host whose persisted liveness has gone stale.

    Islo idle pause is memory-preserving: the local host/runner WebSocket
    objects can remain registered until their ping loops time out, even though
    the VM is already paused and cannot answer new requests. When the durable
    host-store liveness row is stale, trust it over those in-memory objects,
    drop the stale entries, and route through the normal managed wake path.

    :param session_id: Session/conversation identifier.
    :param conv: Current conversation row.
    :param app_state: ``request.app.state`` — supplies stores and registries.
    :param conversation_store: Store holding the session row.
    :returns: ``True`` when a managed wake ran and settled.
    """
    host_store = getattr(app_state, "host_store", None)
    sandbox_config = getattr(app_state, "sandbox_config", None)
    if host_store is None or sandbox_config is None or conv.host_id is None:
        return False

    host = await asyncio.to_thread(host_store.get_host, conv.host_id)
    if host is None or not host_resume_supported(host, sandbox_config):
        return False
    host_registry = cast(HostRegistry | None, getattr(app_state, "host_registry", None))
    tunnel_registry = cast(TunnelRegistry | None, getattr(app_state, "tunnel_registry", None))
    host_conn = host_registry.get(conv.host_id) if host_registry is not None else None
    host_tunnel_stale = (
        host_conn is not None
        and time.time() - host_conn.last_frame_at >= _MANAGED_RESUMABLE_TUNNEL_STALE_S
    )
    runner_session = (
        tunnel_registry.get(conv.runner_id)
        if tunnel_registry is not None and conv.runner_id is not None
        else None
    )
    runner_tunnel_stale = False
    if runner_session is not None and tunnel_registry is not None:
        runner_idle_s = tunnel_registry.seconds_since_last_frame(runner_session)
        runner_tunnel_stale = (
            runner_idle_s is not None and runner_idle_s >= _MANAGED_RESUMABLE_TUNNEL_STALE_S
        )

    host_row_online = await asyncio.to_thread(host_store.is_online, conv.host_id)
    sandbox_running = await asyncio.to_thread(host_sandbox_is_running, host, sandbox_config)
    if (
        sandbox_running is not False
        and host_row_online
        and host_conn is not None
        and not host_tunnel_stale
        and not runner_tunnel_stale
    ):
        return False

    if host_registry is not None:
        host_registry.deregister(conv.host_id)
    if tunnel_registry is not None and conv.runner_id is not None:
        tunnel_registry.deregister(conv.runner_id)

    _logger.info(
        "Managed host %s for session %s needs wake before reusing tunnels "
        "(host_row_online=%s, sandbox_running=%s, host_tunnel_stale=%s, "
        "runner_tunnel_stale=%s)",
        conv.host_id,
        session_id,
        host_row_online,
        sandbox_running,
        host_tunnel_stale,
        runner_tunnel_stale,
    )
    return await _maybe_relaunch_managed_sandbox(
        session_id=session_id,
        conv=conv,
        app_state=app_state,
        conversation_store=conversation_store,
    )


async def ensure_runner_connected(
    *,
    session_id: str,
    conv: Conversation,
    app_state: Any,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> tuple[httpx.AsyncClient | None, Conversation]:
    """
    Bring a wakeable session's runner online for out-of-band resource access.

    Mirrors the message-dispatch relaunch ladder in ``post_event`` — wake a
    stale resumable managed sandbox, launch a runner on a live host, or
    relaunch a dead managed sandbox — but without any of the message-specific
    side effects (failed-turn persistence, session-init handshake, sub-agent
    binding heal). It answers one question: "is a live runner reachable, and
    if not, can we wake one right now?"

    Used by resource routes (e.g. shell create) so a user acting on a session
    whose runner merely went to sleep transparently reconnects it, instead of
    dead-ending on a 502. Only the wakeable states recover here:

    * runner already connected → returned as-is (the common fast path);
    * host online, pinned runner still booting → wait the connect grace
      (``_HOST_BOUND_RUNNER_CONNECT_GRACE_S``, racing a ``host.runner_status``
      query) before treating it as dead, so a booting runner isn't orphaned
      by an eager relaunch;
    * host online, runner truly dead → ``_launch_runner_on_host`` + connect wait;
    * host tunnel gone but managed/resumable → ``_maybe_relaunch_managed_sandbox``
      / ``_maybe_wake_stale_resumable_managed_sandbox``.

    Non-wakeable states (external host offline, not host-bound with a dead
    runner) return ``(None, conv)`` — the caller keeps its existing
    unavailable handling (the CLI reconnect path owns those).

    :param session_id: Session/conversation identifier.
    :param conv: Current session row.
    :param app_state: ``request.app.state`` — supplies the registries,
        managed-launch tracker, host store, and sandbox config.
    :param conversation_store: Store holding the session row.
    :param runner_router: The ``RunnerRouter``, or ``None`` for in-process.
    :returns: ``(runner_client, conv)`` — the client is ``None`` when no
        runner is reachable and none is wakeable; ``conv`` is re-read after
        any wake/relaunch so the caller sees the rebound row.
    :raises OmnigentError: 503 when a managed relaunch/wake failed or timed
        out (propagated from ``_maybe_relaunch_managed_sandbox``).
    """

    from omnigent.server.routes import sessions as _facade

    async def _reread() -> Conversation:
        refreshed = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        return refreshed if refreshed is not None else conv

    # Fast path: a live runner tunnel is already reachable.
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is not None:
        return runner_client, conv

    tunnel_registry = cast(TunnelRegistry | None, getattr(app_state, "tunnel_registry", None))
    host_registry = cast(HostRegistry | None, getattr(app_state, "host_registry", None))

    # A resumable managed host whose persisted liveness has gone stale wakes
    # in place; re-read the row and re-resolve, since the wake may have
    # relaunched the runner and rebound the session.
    if conv.host_id is not None and await _maybe_wake_stale_resumable_managed_sandbox(
        session_id=session_id,
        conv=conv,
        app_state=app_state,
        conversation_store=conversation_store,
    ):
        conv = await _reread()
        runner_client = await _get_runner_client(session_id, runner_router)
        if runner_client is not None:
            return runner_client, conv

    # Host-bound but no runner: ask the still-online host to spawn one, or
    # relaunch a managed sandbox whose host tunnel is gone. Mirrors the
    # gating in post_event — host presence, then managed relaunch fallback.
    if runner_client is None and conv.host_id is not None:
        host_conn = host_registry.get(conv.host_id) if host_registry is not None else None
        relaunched_runner_id: str | None = None
        if host_conn is not None:
            # A session whose runner is merely booting already has a runner_id
            # before its tunnel registers. Mirror post_event's connect grace:
            # wait briefly for that pinned runner (racing a host.runner_status
            # query, which cuts the wait short if the host reports it truly
            # gone) before relaunching — otherwise we'd spawn a second runner
            # and orphan the one still coming up.
            if (
                conv.runner_id is not None
                and host_registry is not None
                and _facade._HOST_BOUND_RUNNER_CONNECT_GRACE_S > 0
            ):
                runner_client = await _wait_for_host_bound_runner_client(
                    session_id,
                    runner_router,
                    tunnel_registry,
                    runner_id=conv.runner_id,
                    timeout_s=_facade._HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                    runner_exit_reports=getattr(app_state, "runner_exit_reports", None),
                    host_conn=host_conn,
                    host_registry=host_registry,
                )
                if runner_client is not None:
                    return runner_client, await _reread()
            launch_attempt = await _launch_runner_on_host(
                conv,
                conversation_store,
                host_registry,
                host_conn,
            )
            # A harness-not-configured or workspace-missing refusal means the
            # runner will never appear — don't set relaunched_runner_id or the
            # caller will wait out the full connect timeout for nothing.
            # Record the refusal message in runner_exit_reports so the
            # runner_failed_to_start error surfaces the actionable cause
            # rather than the generic "may have failed to start" fallback.
            _fatal_refusal = launch_attempt.error_code in (
                _HARNESS_NOT_CONFIGURED_ERROR_CODE,
                _WORKSPACE_MISSING_ERROR_CODE,
            )
            if _fatal_refusal and launch_attempt.error is not None:
                _rer = getattr(app_state, "runner_exit_reports", None)
                if _rer is not None:
                    _rer.record(
                        launch_attempt.runner_id,
                        launch_attempt.error,
                        owner=None,
                    )
            if not _fatal_refusal:
                relaunched_runner_id = launch_attempt.runner_id
        elif await _maybe_relaunch_managed_sandbox(
            session_id=session_id,
            conv=conv,
            app_state=app_state,
            conversation_store=conversation_store,
        ):
            conv = await _reread()
            runner_client = await _get_runner_client(session_id, runner_router)

        if runner_client is None:
            runner_client = await _wait_for_runner_client(
                session_id,
                runner_router,
                tunnel_registry,
                runner_id=relaunched_runner_id,
                timeout_s=_facade._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
                runner_exit_reports=getattr(app_state, "runner_exit_reports", None),
            )
            conv = await _reread()

    return runner_client, conv


def _kick_managed_relaunch(
    *,
    session_id: str,
    conv: Conversation,
    host: Host,
    sandbox_config: ManagedSandboxConfig,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    app_state: Any,
) -> None:
    """
    Register and spawn the background relaunch for a dead sandbox.

    Recovers the session's create-time repository workspace from its label so
    the fresh generation re-clones it, claims the single-flight tracker entry,
    and schedules :func:`_run_managed_launch` with the existing host row. The
    new Pod re-hits admission, so its ``omnigent.ai/agent`` classifier is
    re-stamped — resolved by the launch task through the same built-in gate the
    initial launch uses, never read from a stored label.

    Stays synchronous so no ``await`` falls between the caller's
    ``tracker.get`` check and the ``tracker.begin`` claim, and none falls
    between that claim and the task that settles it.

    :param session_id: Session/conversation identifier.
    :param conv: The session row (supplies the repo label and bound agent).
    :param host: The dead managed host row to relaunch.
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param app_state: ``request.app.state`` — supplies the registries and the
        agent store the classifier is re-derived from.
    """
    from omnigent.server.managed_hosts import (
        MANAGED_REPO_LABEL_KEY,
        parse_repo_workspace,
    )

    # Re-clone the repository the session was created with so the
    # fresh generation's workspace matches the create-time state.
    # The label holds the raw create-time value, already validated
    # by the create's parse — a parse failure here means the label
    # was tampered with, and the relaunch proceeds with an empty
    # workspace rather than dying.
    repo = None
    raw_repo = conv.labels.get(MANAGED_REPO_LABEL_KEY)
    if raw_repo is not None:
        try:
            repo = parse_repo_workspace(raw_repo)
        except ValueError:
            _logger.warning(
                "Session %s has an unparseable %s label (%r); relaunching with an empty workspace",
                session_id,
                MANAGED_REPO_LABEL_KEY,
                raw_repo,
            )
    _logger.info(
        "Managed sandbox for session %s (host %s) is gone; relaunching a new generation",
        session_id,
        conv.host_id,
    )
    tracker.begin(session_id)
    # Seed the relaunch's progress indicator immediately — the user is
    # typically watching the session page when "wake the sandbox" runs.
    _publish_sandbox_status(session_id, "provisioning")
    agent_store = getattr(app_state, "agent_store", None)
    if agent_store is None:
        _logger.warning(
            "session %s: relaunch has no agent store; runner stays unclassified",
            session_id,
        )
    relaunch_task = asyncio.create_task(
        _run_managed_launch(
            session_id=session_id,
            owner=host.user_id,
            sandbox_config=sandbox_config,
            repo=repo,
            tracker=tracker,
            conversation_store=conversation_store,
            host_store=host_store,
            host_registry=getattr(app_state, "host_registry", None),
            tunnel_registry=getattr(app_state, "tunnel_registry", None),
            relaunch_host=host,
            agent_store=agent_store,
            agent_id=conv.agent_id,
        )
    )
    _managed_launch_tasks.add(relaunch_task)
    relaunch_task.add_done_callback(_managed_launch_tasks.discard)


def _kick_managed_wake(
    *,
    session_id: str,
    conv: Conversation,
    sandbox_config: ManagedSandboxConfig,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    app_state: Any,
) -> None:
    """
    Spawn the background wake for a dormant resumable host.

    Call-time proxy to the facade so a test's ``monkeypatch.setattr`` of this
    name on ``sessions`` is honored by sibling impl callers.
    """
    from omnigent.server.routes import sessions as _facade

    return _facade._kick_managed_wake(
        session_id=session_id,
        conv=conv,
        sandbox_config=sandbox_config,
        tracker=tracker,
        conversation_store=conversation_store,
        host_store=host_store,
        app_state=app_state,
    )


def _kick_managed_wake_impl(
    *,
    session_id: str,
    conv: Conversation,
    sandbox_config: ManagedSandboxConfig,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    app_state: Any,
) -> None:
    """
    Register and spawn the background WAKE for a dormant resumable host.

    Unlike :func:`_kick_managed_relaunch` (which provisions a NEW sandbox and
    re-clones the repo), this resumes the SAME stopped sandbox in place
    (reattaching its persistent volume) — so it does NOT re-bind the session's
    host/workspace. Reuses the launch tracker so a racing message POST parks on
    the rendezvous instead of forwarding into a half-woken host or triggering a
    workspace-destroying relaunch.

    :param session_id: Session/conversation identifier.
    :param conv: The session row bound to the dormant host.
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param app_state: ``request.app.state`` — supplies the registries.
    """
    _logger.info(
        "Managed host %s (session %s) is dormant but resumable; waking in background",
        conv.host_id,
        session_id,
    )
    tracker.begin(session_id)
    # Seed the progress indicator immediately — the user is watching the
    # session page when the wake fires (the composer let them send into a
    # host_asleep session).
    _publish_sandbox_status(session_id, "provisioning")
    wake_task = asyncio.create_task(
        _run_managed_wake(
            session_id=session_id,
            conv=conv,
            sandbox_config=sandbox_config,
            tracker=tracker,
            conversation_store=conversation_store,
            host_store=host_store,
            host_registry=getattr(app_state, "host_registry", None),
            tunnel_registry=getattr(app_state, "tunnel_registry", None),
        )
    )
    _managed_launch_tasks.add(wake_task)
    wake_task.add_done_callback(_managed_launch_tasks.discard)


async def _run_managed_wake(
    *,
    session_id: str,
    conv: Conversation,
    sandbox_config: ManagedSandboxConfig,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
) -> None:
    """
    Wake a dormant resumable managed host in the background, settling the
    tracker so a parked message POST forwards once the host is back.

    Resumes the stopped sandbox in place (:func:`resume_managed_host`: resume +
    re-arm token + re-exec host, preserving the workspace volume — no re-bind),
    then launches a runner on the woken host and waits for its tunnel so a
    rendezvoused message resolves on the first try. The parked send runs the
    session-init handshake (transcript forwarder attach) before forwarding, so
    the first post-wake turn is mirrored + persisted.

    Mirrors :func:`_bind_and_launch_managed_runner` (launch runner + wait
    tunnel + settle) but with a resume instead of a fresh provision + bind.
    Every exit settles the tracker — a failed wake does NOT tear the sandbox
    down (the volume is the user's), it just surfaces the reason to the waiter.

    :param session_id: Session/conversation identifier.
    :param conv: The session row bound to the dormant host.
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker (this session's entry was begun
        by the caller).
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels, used to send the launch-runner
        frame. ``None`` in minimal test wirings.
    :param tunnel_registry: Runner-tunnel registry used to await the launched
        runner's connection. ``None`` in minimal test wirings.
    """
    from omnigent.server.managed_hosts import resume_managed_host
    from omnigent.server.routes import sessions as _facade

    host_id = conv.host_id
    if host_id is None:
        reason = "managed session has no host binding"
        tracker.fail(session_id, reason)
        _publish_sandbox_status(session_id, "failed", reason)
        return

    def _on_stage(stage: str) -> None:
        """Relay the wake's launch-pipeline stage to the session's progress surface.

        Without it the wake shows the single ``"provisioning"`` band that
        ``_kick_managed_wake`` seeded for its whole duration, even while the host
        is already re-execing. resume_managed_host may invoke this from the
        worker thread its ``start_host`` runs on; ``_publish_sandbox_status`` is
        thread-safe.
        """
        _publish_sandbox_status(session_id, stage)

    try:
        # Wake the same sandbox in place; resume_managed_host is single-flight
        # per host and a no-op if it's already online.
        await resume_managed_host(
            host_id, host_store, sandbox_config, force=True, on_stage=_on_stage
        )
        _publish_sandbox_status(session_id, "connecting")
        refreshed = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if refreshed is None:
            tracker.fail(session_id, "session not found after wake")
            return
        runner_id: str | None = None
        host_conn = host_registry.get(host_id) if host_registry is not None else None
        if host_registry is not None and host_conn is None:
            # resume_managed_host waits on cross-replica host-store liveness, not
            # this replica's in-memory tunnel registry — the woken host's tunnel
            # can lag here (or land on another replica). Poll briefly so the runner
            # launches once it reconnects, instead of settling "ready" with no
            # runner; fail clearly if it never shows rather than losing the turn.
            _host_reconnect_deadline = (
                time.monotonic() + _facade._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S
            )
            while host_conn is None and time.monotonic() < _host_reconnect_deadline:
                await asyncio.sleep(0.5)
                host_conn = host_registry.get(host_id)
            if host_conn is None:
                tracker.fail(session_id, "managed host did not reconnect after wake")
                _publish_sandbox_status(
                    session_id, "failed", "managed host did not reconnect after wake"
                )
                return
        if host_conn is not None:
            launch_attempt = await _launch_runner_on_host(
                refreshed,
                conversation_store,
                host_registry,
                host_conn,
            )
            if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
                reason = launch_attempt.error or "harness not configured on the sandbox host"
                tracker.fail(session_id, reason)
                _publish_sandbox_status(session_id, "failed", reason)
                return
            runner_id = launch_attempt.runner_id
        if runner_id is not None and tunnel_registry is not None:
            connected = await _wait_for_managed_runner_tunnel(
                session_id,
                runner_id,
                tunnel_registry,
                tracker,
            )
            if not connected:
                return
        tracker.finish(session_id)
        _publish_sandbox_status(session_id, "ready")
    except HTTPException as exc:
        tracker.fail(session_id, str(exc.detail))
        _publish_sandbox_status(session_id, "failed", str(exc.detail))
    except Exception:  # noqa: BLE001
        # Fire-and-forget task — settle the tracker (else a waiting message
        # POST hangs to its timeout) and never escape as an unhandled-task
        # traceback. A failed wake leaves the sandbox intact for a retry.
        _logger.exception("Managed host wake crashed for session %s", session_id)
        tracker.fail(session_id, "internal error during managed host wake")
        _publish_sandbox_status(session_id, "failed", "internal error during managed host wake")


async def _ensure_runner_session_initialized(
    session_id: str,
    conv: Conversation,
    runner_client: httpx.AsyncClient,
    conversation_store: ConversationStore,
    initializer: RunnerSessionInitializer | None = None,
    *,
    suppress_recovery_turn: bool = False,
) -> bool:
    """
    Drive — and wait for — the runner's session-init handshake.

    Posts ``POST /v1/sessions`` to a freshly (re)launched runner and
    awaits it, so the runner's ``create_session`` completes before the
    caller forwards a message. For a claude-native session that means
    the tmux terminal **and its transcript forwarder are watching**
    before the web message is injected into the TUI — the round-trip
    that promotes the optimistic bubble and streams the reply only
    happens if the forwarder is in place first.

    This closes the host-restart race: today the auto-relaunch /
    resume paths wait only for the runner's *tunnel* to register
    (``runner_client`` becomes non-None), not for the session
    handshake, so the message can be injected before the forwarder
    attaches and is lost. The new / runner-bound paths don't hit this
    because they run the handshake as a distinct step before any
    message (``create_session`` endpoint) or against a from-offset-0
    forwarder.

    Current servers route this and ``_on_runner_connect`` through one
    generation-aware initializer, so both callers await the same response.
    The runner retains its own single-flight as the compatibility backstop for
    older servers and cross-replica delivery.

    Best-effort and matching the create / PATCH handshakes: a transport
    error is logged and swallowed (the relay + ``_on_runner_connect``
    are the backstop), but the *await* — the actual fix — still
    serializes the handshake ahead of the caller's message forward.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for *session_id*; supplies
        ``agent_id`` and ``sub_agent_name`` for the handshake body.
    :param runner_client: Runner client already resolved for
        *session_id* (its tunnel is up).
    :param conversation_store: Store used to clear persisted disconnect
        error labels once the handshake proves the runner recovered.
    :param suppress_recovery_turn: When ``True``, ask the runner not to
        start a crash-recovery turn during ``create_session``.  Must be
        set whenever the caller will forward a message immediately after
        this call: the server persists the message to DB before calling
        session-init, so the runner's history load would otherwise see
        the pending message and start a recovery turn — the subsequent
        forward then arrives to an active turn, is buffered, and is
        processed a second time once the (redundant) recovery turn
        finishes.
    :returns: ``True`` when a current runner explicitly confirmed its native
        terminal is ready; ``False`` for legacy or non-native responses.
    """
    try:
        if initializer is not None:
            resp = await initializer.initialize(
                conv,
                runner_client,
                timeout=_RUNNER_SESSION_INIT_TIMEOUT_S,
                suppress_recovery_turn=suppress_recovery_turn,
            )
        else:
            from omnigent.version import VERSION

            resp = await runner_client.post(
                "/v1/sessions",
                json=build_runner_session_init_payload(
                    conv,
                    server_version=VERSION,
                    suppress_recovery_turn=suppress_recovery_turn,
                ),
                timeout=_RUNNER_SESSION_INIT_TIMEOUT_S,
            )
        # httpx only raises on transport errors; a 4xx/5xx means create_session
        # likely didn't run (terminal + forwarder not set up), so surface it
        # via the same warning path rather than silently forwarding into a
        # half-initialized runner.
        resp.raise_for_status()
        await _publish_runner_recovered_status(session_id, conversation_store)
        try:
            payload = resp.json()
        except ValueError:
            return False
        return bool(
            isinstance(payload, dict)
            and payload.get("session_init_protocol_version") == 2
            and payload.get("terminal_ready") is True
        )
    except (httpx.HTTPError, ConnectionError):
        _logger.warning(
            "Session-init handshake to runner failed for session %s; "
            "forwarding the message anyway",
            session_id,
            exc_info=True,
        )
        return False


def _is_native_terminal_session(conv: Conversation) -> bool:
    """
    Return whether a session's turns are driven by a native terminal harness.

    True for both a built-in terminal-first wrapper (``omnigent.wrapper``
    label) and a custom chat-first agent bound to a native harness — see
    :func:`_native_coding_agent_for_session` for why routing keys on the
    resolved harness, not the presentation labels.

    :param conv: Conversation row for the target session.
    :returns: ``True`` when the session's harness is a native terminal harness.
    """
    return _native_coding_agent_for_session(conv) is not None


def _native_terminal_runtime(conv: Conversation) -> tuple[str, str, str]:
    """
    Return native terminal runtime strings for a native-harness session.

    Resolves by wrapper label OR resolved harness (see
    :func:`_native_coding_agent_for_session`), so a custom chat-first agent on
    a native harness (no wrapper label) resolves too — otherwise it would raise
    ``Unsupported native terminal session`` the moment its first web message
    reached the native dispatch branch.

    :param conv: Conversation row for the target session.
    :returns: ``(display_name, model, harness)``.
    :raises OmnigentError: If the session is not a native terminal harness.
    """
    native_agent = _native_coding_agent_for_session(conv)
    if native_agent is not None:
        return native_agent.display_name, native_agent.agent_name, native_agent.harness
    raise OmnigentError(
        "Unsupported native terminal session",
        code=ErrorCode.INVALID_INPUT,
    )


async def _ensure_native_terminal_ready(
    runner_client: httpx.AsyncClient,
    session_id: str,
    conv: Conversation,
) -> _NativeTerminalEnsureOutcome:
    """
    Ask the runner to create or return the native terminal for a message.

    The runner's explicit ``ensure_native_terminal`` endpoint is the
    authoritative readiness check for native user messages. Any non-2xx
    response or transport failure fails this user turn quickly with a
    durable error item; a 2xx response preserves the normal boot grace
    because the runner has accepted responsibility for terminal startup.

    :param runner_client: HTTP client pointed at the session's runner.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row used to identify the native harness.
    :returns: The probe outcome — a definitive ``error`` when the terminal
        could not start, else ``error=None``.
    """
    display_name, _, harness = _native_terminal_runtime(conv)
    terminal_name = _native_terminal_name_for_harness(harness)
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={
                "terminal": terminal_name,
                "session_key": "main",
                "ensure_native_terminal": True,
            },
            timeout=10.0,
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        # WSTunnelTransport raises bare ConnectionError on tunnel close
        # ("tunnel closed before request completed"); without this clause
        # a runner tunnel drop escaped to the catch-all handler and the
        # web client showed an opaque 500 ``internal_error`` instead of
        # the durable ensure-failure turn error below.
        _logger.warning(
            "%s terminal ensure transport failed for session=%s",
            display_name,
            session_id,
            exc_info=True,
        )
        return _NativeTerminalEnsureOutcome(
            error=_native_terminal_ensure_transport_error(exc, display_name=display_name),
        )
    if resp.status_code < 400:
        return _NativeTerminalEnsureOutcome(
            error=None,
            policy_notice=_policy_notice_from_ensure_response(resp),
        )
    _logger.warning(
        "%s terminal ensure failed definitively for session=%s status=%s body=%s",
        display_name,
        session_id,
        resp.status_code,
        resp.text[:500],
    )
    return _NativeTerminalEnsureOutcome(
        error=_native_terminal_failure_from_runner_response(resp, display_name=display_name),
    )


async def _persist_native_terminal_failure(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    error: ErrorData,
    runner_router: RunnerRouter | None,
    *,
    created_by: str | None,
) -> str:
    """
    Persist a consumed user message and terminal-start error.

    Used when a native terminal definitively cannot start. The AP
    server becomes the writer for this failure turn only: it records
    the user's message so the input is consumed, records a sibling
    ``type="error"`` item so refresh/reconnect can render the banner,
    and publishes the same live error/status events clients already
    understand.

    When the failing session is a native sub-agent, the parent's runner
    is also notified via an ``external_session_status: failed`` forward
    (see :func:`_forward_native_subagent_terminal_failure`). The native
    bypass returns HTTP 200 to the parent's runner ``spawn`` call, so
    without this forward the parent's work entry would stay ``running``
    forever — no harness boots, so no Stop hook ever fires the terminal
    edge the normal completion path relies on.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for the session.
    :param body: Original user message event.
    :param conversation_store: Store used for the durable append.
    :param error: Error data derived from the runner's ensure response.
    :param runner_router: Router used to resolve the (sub-agent's own)
        runner for the parent-wake forward, or ``None`` in
        in-process / test setups where the global client is used.
    :param created_by: Authenticated posting actor, e.g.
        ``"alice@example.com"``; ``None`` in single-user mode.
    :returns: Store-assigned id of the consumed user message item.
    """
    turn_id = generate_task_id()
    user_item = _build_new_item(body, turn_id, created_by=created_by)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [user_item],
    )
    await _seed_missing_title_from_user_message(
        conv,
        user_item,
        conversation_store,
    )
    error_persist_result = await _relay_persist_error_once(
        conversation_store,
        session_id,
        NewConversationItem(
            type="error",
            response_id=turn_id,
            data=error,
        ),
    )
    consumed = persisted_items[0]
    _publish_input_consumed(session_id, consumed)
    if error_persist_result == "persisted":
        _publish_error_event(session_id, error)
    _publish_terminal_pending(session_id, False)
    _publish_status(
        session_id,
        "failed",
        ErrorDetail(code=error.code, message=error.message),
    )
    # A boot failure on a native sub-agent must wake the parent — mirror
    # the normal terminal-status path (publish + forward), gated on
    # ``kind == "sub_agent"`` so top-level native sessions are unaffected.
    await _forward_native_subagent_terminal_failure(
        session_id,
        conv,
        error,
        runner_router,
    )
    return consumed.id


async def _persist_host_launch_failure_turn(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    host_error: str | None,
    runner_router: RunnerRouter | None,
    *,
    created_by: str | None,
) -> str:
    """
    Persist a consumed user message and a host-launch failure error.

    Used when a message arrives for a host-bound session whose runner is
    dead and the host *refuses* to relaunch because the agent's harness
    isn't configured there (the daemon's structured
    ``harness_not_configured`` reply). The message is the real
    runner-start attempt, so — exactly like a native terminal that can't
    boot (:func:`_persist_native_terminal_failure`) — the server records
    the user's message (so the input is consumed, not silently dropped)
    and a sibling ``type="error"`` item carrying the host's message
    (which names the fix, ``omnigent setup``), then publishes the same
    live error/status events the web renders as an error banner. The host
    binding is left intact so a later message relaunches once the user has
    run setup.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for the session.
    :param body: Original user message event.
    :param conversation_store: Store used for the durable append.
    :param host_error: The host's human-readable refusal, e.g.
        ``"harness 'codex' is not configured on host 'laptop' — run
        `omnigent setup` ..."``. ``None`` falls back to a generic
        ``omnigent setup`` pointer so the banner is never empty.
    :param runner_router: Router used to resolve a sub-agent's runner for
        the parent-wake forward, or ``None`` in in-process / test setups.
    :param created_by: Authenticated posting actor, e.g.
        ``"alice@example.com"``; ``None`` in single-user mode.
    :returns: Store-assigned id of the consumed user message item.
    """
    error = ErrorData(
        source="execution",
        # Stable classifier mirroring the host's wire error code, so the
        # web can special-case the banner if it ever wants to.
        code="harness_not_configured",
        message=(
            host_error
            if host_error
            # Defensive fallback: the daemon always sends a message with
            # the code, but the banner must stay actionable if a
            # third-party host omits it.
            else (
                "the agent's harness is not configured on the selected host — run `omnigent setup`"
            )
        ),
    )
    turn_id = generate_task_id()
    user_item = _build_new_item(body, turn_id, created_by=created_by)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [user_item],
    )
    await _seed_missing_title_from_user_message(conv, user_item, conversation_store)
    error_persist_result = await _relay_persist_error_once(
        conversation_store,
        session_id,
        NewConversationItem(type="error", response_id=turn_id, data=error),
    )
    consumed = persisted_items[0]
    _publish_input_consumed(session_id, consumed)
    if error_persist_result == "persisted":
        _publish_error_event(session_id, error)
    _publish_terminal_pending(session_id, False)
    _publish_status(session_id, "failed", ErrorDetail(code=error.code, message=error.message))
    # A host-launched sub-agent that can't configure must wake its parent,
    # the same way a boot failure does — no-ops for top-level sessions.
    await _forward_native_subagent_terminal_failure(session_id, conv, error, runner_router)
    return consumed.id


async def _forward_native_subagent_terminal_failure(
    session_id: str,
    conv: Conversation,
    error: ErrorData,
    runner_router: RunnerRouter | None,
) -> None:
    """
    Wake the parent runner when a native sub-agent fails to boot its terminal.

    Mirrors the terminal-status path's parent-wake (the ``idle`` /
    ``failed`` branch of ``external_session_status`` in
    :func:`post_event`): forward an ``external_session_status: failed``
    edge — carrying the boot error as ``output`` so it lands in the
    parent's inbox — to the sub-agent's own runner, then require the
    forward to land. The runner's ``external_session_status`` handler
    maps ``failed`` to ``mark_subagent_work_terminal(status="failed")``,
    which marks the parent's work entry terminal and wakes the parent.

    No-ops for non-sub-agent sessions and for codex-internal sub-agents
    (tracked inside the same app-server thread tree, with no runner
    inbox entry to forward to — identical to the normal path's
    ``_is_codex_native_subagent`` exclusion).

    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param conv: Conversation row for the sub-agent session.
    :param error: Boot error to relay to the parent as the turn result.
    :param runner_router: Router used to resolve the sub-agent's runner,
        or ``None`` (then the global client is used).
    :returns: None.
    :raises OmnigentError: If the parent's runner could not be reached
        or rejected the forwarded failure status — dropping it would
        strand the parent waiting forever.
    """
    if conv.kind != "sub_agent" or _is_codex_native_subagent(conv):
        return
    forward_body: dict[str, Any] = {
        "type": _EXTERNAL_SESSION_STATUS_TYPE,
        # ``output`` is the parent-inbox result text on a failed edge
        # (runner: ``output or "...turn failed"``); pass the real error.
        "data": {"status": "failed", "output": error.message},
    }
    runner_result = await _forward_session_change_to_runner(
        session_id,
        runner_router,
        forward_body,
    )
    _require_external_status_forward(session_id, "failed", runner_result)


def _build_native_terminal_message_event(
    conv: Conversation,
    body: SessionEventInput,
    model_override: str | None = None,
) -> dict[str, Any]:
    """
    Build the runner event that delivers a web message to a native TUI.

    :param conv: Conversation row for the target session.
    :param body: Validated Sessions API message event, e.g.
        ``{"type": "message", "data": {"role": "user",
        "content": [{"type": "input_text", "text": "Hi"}]}}``.
    :param model_override: Routed model to apply for THIS turn, e.g.
        ``"databricks-claude-sonnet-5"``. Carried in-band on the message
        so the claude-native executor applies ``/model`` and injects the
        message under one lock (no separate racing ``model_change``
        event). ``None`` when routing did not pick a model.
    :returns: Harness ``MessageEvent`` body for the runner-local
        native terminal harness, including ``agent_id`` so the runner
        can resolve the harness spec on the first message.
    :raises OmnigentError: If the event is not a user message.
    """
    display_name, model, harness = _native_terminal_runtime(conv)
    data = parse_item_data(body.type, {"type": body.type, **body.data})
    if not isinstance(data, MessageData) or data.role != "user":
        raise OmnigentError(
            f"{display_name} terminal sessions accept only user message events",
            code=ErrorCode.INVALID_INPUT,
        )
    event: dict[str, Any] = {
        "type": "message",
        "role": "user",
        "content": data.content,
        "model": model,
        "harness": harness,
        # The runner resolves the harness from the agent spec keyed by
        # agent_id; the forwarded ``harness`` hint is ignored on the turn
        # path. Without agent_id, the first message of a freshly
        # host-spawned runner (arriving before POST /v1/sessions caches
        # the spec) falls back to the test-only "runner-test-default"
        # harness and is dropped. Match the non-native forward path,
        # which always includes it.
        "agent_id": conv.agent_id,
    }
    # Ride the routed model in-band as ``model_override`` (extra field the
    # harness MessageEvent forwards into ExecutorConfig.model). The
    # claude-native executor applies the ``/model`` switch and the message
    # inject as ONE locked step, so the switch can't race the message.
    if model_override is not None:
        event["model_override"] = model_override
    return event


async def _forward_native_terminal_message(
    runner_client: httpx.AsyncClient,
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    model_override: str | None = None,
) -> None:
    """
    Forward one Omnigent web-chat message to the native terminal harness.

    The message is intentionally not persisted here. Claude Code
    and Codex record the accepted prompt in their terminal/app-server
    state, and their forwarders later post that terminal-originated
    item back through ``external_conversation_item``.

    :param runner_client: Runner client selected for ``session_id``.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for *session_id*.
    :param body: Sessions API message event to inject.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references in ``input_image`` / ``input_file``
        content blocks.
    :param artifact_store: Optional binary content store for
        fetching file bytes during resolution.
    :param model_override: Routed model to apply for this turn, carried
        in-band on the message so the executor applies ``/model`` and the
        inject under one lock (no separate racing ``model_change``).
        ``None`` when routing did not pick a model.
    :returns: None.
    :raises HTTPException: 502 when the runner or harness rejects
        the injection request.
    """
    display_name, _, _ = _native_terminal_runtime(conv)
    event = _build_native_terminal_message_event(conv, body, model_override=model_override)
    _logger.info(
        "%s terminal message forward starting: session=%s block_types=%s model_override=%s",
        display_name,
        session_id,
        [block.get("type") for block in event.get("content", []) if isinstance(block, dict)]
        if isinstance(event.get("content"), list)
        else type(event.get("content")).__name__,
        event.get("model_override"),
    )
    if (
        file_store is not None
        and artifact_store is not None
        and isinstance(event.get("content"), list)
    ):
        from omnigent.runtime.content_resolver import (
            _resolve_message_content,
        )

        try:
            event["content"] = _resolve_message_content(
                event["content"],
                file_store,
                artifact_store,
                session_id=session_id,
            )
        except (ValueError, KeyError):
            _logger.warning(
                "File reference resolution failed for native session=%s",
                session_id,
                exc_info=True,
            )
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=event,
            timeout=_CLAUDE_NATIVE_MESSAGE_TIMEOUT_S,
        )
        _logger.info(
            "%s terminal message runner response: session=%s status=%s body=%s",
            display_name,
            session_id,
            resp.status_code,
            resp.text[:500],
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        # WSTunnelTransport raises bare ConnectionError on tunnel close;
        # map it to the same 502 as an httpx transport failure so a
        # runner tunnel drop mid-forward doesn't escape as an opaque 500.
        _logger.warning(
            "%s terminal message forward failed for session=%s",
            display_name,
            session_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"{display_name} terminal message delivery failed",
        ) from exc
    if resp.status_code >= 400:
        _logger.warning(
            "%s terminal message forward rejected for session=%s status=%s body=%s",
            display_name,
            session_id,
            resp.status_code,
            resp.text,
        )
        raise HTTPException(
            status_code=502,
            detail=f"{display_name} terminal message delivery failed ({resp.status_code})",
        )
    failure = _extract_claude_native_runner_failure(resp)
    if failure is not None:
        _logger.warning(
            "%s terminal message forward failed in runner SSE for session=%s: %s",
            display_name,
            session_id,
            failure,
        )
        raise HTTPException(
            status_code=502,
            detail=f"{display_name} terminal message delivery failed: {failure}",
        )


async def _persist_session_event(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Persist a user event without forwarding to a runner.

    Used when the runner isn't online yet but the session has a
    ``host_id`` — the message is stored so the runner's crash-
    recovery block picks it up from history when it connects.

    :param session_id: Session/conversation identifier.
    :param body: The validated event input.
    :param conversation_store: Store for item persistence.
    :param agent_name: Agent name for title seeding.
    :returns: The store-assigned item id.
    """
    import uuid

    turn_id = f"turn_{uuid.uuid4().hex}"
    item = _build_new_item(body, turn_id)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [item],
    )
    conv = await asyncio.to_thread(
        conversation_store.get_conversation,
        session_id,
    )
    if conv is not None:
        await _seed_missing_title_from_user_message(
            conv,
            item,
            conversation_store,
        )
    item_id = persisted_items[0].id if persisted_items else turn_id
    _publish_external_conversation_item(session_id, persisted_items[0])
    return item_id


# How long routing waits on a stale catalog refresh. The fetch itself retries
# a booting runner for ~30s, and it is only ever awaited to sharpen a routing
# candidate list — a wait that long would make the whole routing hazard path
# stall on a catalog rather than on the router. Bounding the AWAIT (not the
# task) leaves the single-flight running to fill the cache for the next turn.
_ROUTING_CATALOG_WAIT_S = 3.0


async def _refresh_stale_native_model_options(
    session_id: str,
    runner_client: httpx.AsyncClient | None,
) -> None:
    """
    Replace a pre-launch claude catalog with the runner's launch-exact one.

    The cache can hold a catalog resolved from the session's *host* before
    the terminal existed (``_load_model_options_from_host``), whose family
    aliases carry the workspace default rather than this session's routed
    pin. Such an entry is marked in ``_model_options_stale``; serving it to
    turn routing offers a vocabulary the pane does not have. Once a runner
    is bound its ``claude-model-options`` endpoint reports the pinned
    aliases, so await that fetch before reading the cache. Leaves the stale
    entry in place when there is no runner, the fetch fails, or it outruns
    :data:`_ROUTING_CATALOG_WAIT_S` — a stale vocabulary still beats none,
    and beats holding a prompt while the catalog retries a booting runner.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :param runner_client: HTTP client pointed at the bound runner, or
        ``None`` when no runner is bound.
    """
    if runner_client is None or session_id not in _model_options_stale:
        return
    # This wait was the biggest single term in routing's pre-router latency on
    # a first message, and nothing named it in the logs. Time it, so the ladder
    # above can be sized against a measurement rather than a guess.
    started = time.monotonic()
    inflight = _model_options_inflight.get(session_id)
    if inflight is None:
        endpoint = _MODEL_OPTIONS_ENDPOINT_BY_WRAPPER[_CLAUDE_NATIVE_WRAPPER_LABEL_VALUE]
        inflight = asyncio.create_task(
            _load_model_options(runner_client, session_id, f"/v1/sessions/{session_id}/{endpoint}")
        )
        _model_options_inflight[session_id] = inflight
        inflight.add_done_callback(
            lambda _t, sid=session_id: _model_options_inflight.pop(sid, None)
        )
    await _await_briefly(inflight)
    _logger.info(
        "smart_routing: session=%s stale catalog refresh waited %.3fs (still_stale=%s)",
        session_id,
        time.monotonic() - started,
        session_id in _model_options_stale,
    )


async def _await_briefly(task: asyncio.Task[None]) -> None:
    """
    Wait up to :data:`_ROUTING_CATALOG_WAIT_S` for *task*, then give up on it.

    Shielded, so timing out abandons the wait rather than the fetch: the
    single-flight keeps running and its result lands in the cache for whoever
    reads it next.

    :param task: The in-flight model-options fetch.
    """
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.shield(task), timeout=_ROUTING_CATALOG_WAIT_S)


async def _native_turn_catalog(
    session_id: str,
    conv: Conversation,
    runner_client: httpx.AsyncClient | None = None,
) -> list[str] | None:
    """
    Return the models a native session's terminal can switch onto.

    A claude-native pane switches by ``/model``, which takes its own picker
    vocabulary and nothing else — while the gateway behind it serves far
    more, including generations the pane cannot name. Routing a turn onto
    one of those would be dropped by the executor, so the picker rows are
    the candidate list for this session.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :param conv: Conversation row, read for the wrapper label.
    :param runner_client: HTTP client pointed at the bound runner, used to
        replace a stale pre-launch catalog with the launch-exact one.
        ``None`` serves whatever is cached.
    :returns: Model ids, or ``None`` when the session is not a native
        terminal with a known vocabulary (the caller keeps its own
        candidate resolution).
    """
    if conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY) != _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE:
        return None
    await _refresh_stale_native_model_options(session_id, runner_client)
    options = _model_options_cache.get(session_id)
    if not options:
        return None
    # ``model`` is optional on a picker row and ``model_dump(exclude_none=True)``
    # drops it, so fall back to the row key the same way the host-side reader
    # does — otherwise the turn silently loses its vocabulary constraint.
    models: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        raw = option.get("model") or option.get("id")
        if isinstance(raw, str) and raw and raw not in models:
            models.append(raw)
    return models or None


def _routed_turn_model_spelling(
    session_id: str,
    conv: Conversation,
    model: str,
) -> str | None:
    """
    Translate a routed model into the spelling this pane can switch to.

    A mid-turn switch on a Claude Code pane is typed as ``/model``, which
    takes only this session's own picker vocabulary — its family aliases
    and its one custom slot. An id outside that vocabulary is skipped by
    the executor (fail open, the turn runs on the current model), so it
    must be neither pinned on the row nor recorded as applied. Sessions
    that are not claude-native panes, and panes whose vocabulary is not
    known yet (no picker rows cached), keep the routed id: the launch env
    is the only authority and guessing would be its own inaccuracy.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :param conv: Conversation row, read for the wrapper label.
    :param model: The routed model id, e.g. ``"databricks-claude-opus-4-8"``.
    :returns: The picker-vocabulary spelling to apply (``model`` itself
        when no translation applies), or ``None`` when the pane has no
        spelling for it.
    """
    if conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY) != _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE:
        return model
    options = _model_options_cache.get(session_id)
    if not options:
        return model
    from omnigent.claude_model_vocabulary import (
        claude_model_command_arg,
        model_vocabulary_env,
    )

    env = model_vocabulary_env(options)
    if not env:
        return model
    spelling = claude_model_command_arg(model, env)
    if spelling is None:
        _logger.warning(
            "smart_routing: routed model %s has no spelling the claude-native pane "
            "for session=%s accepts (vocabulary=%s); leaving the session's model alone",
            model,
            session_id,
            sorted(env.values()),
        )
    return spelling


def _unapplied_routed_verdict(model: str, verdict: dict[str, Any]) -> dict[str, Any]:
    """
    Record on a verdict that the terminal cannot switch to *model*.

    :param model: The routed model id the pane has no spelling for.
    :param verdict: Router verdict about to be recorded as a chip.
    :returns: The verdict with ``applied`` cleared and the reason noted.
    """
    rationale = verdict.get("rationale")
    note = f"Not applied: this Claude terminal cannot switch to {model}."
    return {
        **verdict,
        "applied": False,
        "rationale": f"{rationale} {note}" if isinstance(rationale, str) and rationale else note,
    }


#: Placeholder model on a decision card for a routing call that never answered.
#: The same spelling the auto-harness path already uses, so the UI renders one
#: "not routed" outcome however routing failed.
_UNAVAILABLE_ROUTED_MODEL = "unavailable"


def _unavailable_routing_card(reason: str) -> tuple[str, dict[str, Any]]:
    """
    Build the declined ``routing_decision`` card for a routing call that failed.

    Routing is never allowed to block work, but a silent fall-through leaves
    the user with a session that quietly ignored the toggle they turned on.
    This is the visible half of failing open: no model is pinned, and the card
    names the reason.

    :param reason: Why nothing was routed, e.g.
        ``"Routing call failed: router returned HTTP 500"``.
    :returns: ``(model, verdict)`` ready for
        :func:`_emit_server_routing_decision`.
    """
    return _UNAVAILABLE_ROUTED_MODEL, {"rationale": reason, "applied": False}


def _native_pane_harness(conv: Conversation) -> str | None:
    """The native harness a pane actually runs, past the ``"auto"`` sentinel.

    A forced-auto child keeps ``harness_override="auto"`` until its first
    message routes, but its terminal is already up on one CLI. Routing that
    pane off the sentinel offered every family (the sentinel carries none), so
    the pick could be a model the running CLI cannot speak; its wrapper label
    names the real harness.

    :param conv: Conversation row for the native session.
    :returns: The canonical native harness, e.g. ``"claude-native"``, or
        ``None`` when it cannot be resolved.
    """
    harness = _resolve_harness(conv)
    if harness is not None and harness != "auto":
        return harness
    native = _native_coding_agent_for_session(conv)
    return native.harness if native is not None else harness


def _pinned_spawn_family(parent: Conversation | None) -> str | None:
    """Return the family a parent's spawns are confined to, if any.

    Only a Smart Routing parent on a PINNED harness confines them — its
    spawns are routed inside its own family. An auto-harness parent hands
    the family choice to the router, and a parent that routes no spawns at
    all confines nothing.

    :param parent: The parent session's row, or ``None`` for a top-level
        create (nothing to stay in family with).
    :returns: ``"claude"`` / ``"gpt"`` / ``"pi"``, or ``None`` when the
        parent's spawns may land in any family.
    """
    if parent is None or not subagent_routing_enabled(parent.subagent_routing_override):
        return None
    if auto_harness_session(parent):
        return None
    return harness_family(_resolve_harness(parent))


def _reject_out_of_family_child(
    parent: Conversation | None,
    child_harness: str | None,
) -> None:
    """Refuse a pinned parent's create of a child in another family.

    The primary defense for the family rule: a pinned codex Smart Routing
    session must not be able to stand up a claude child at all, rather than
    standing one up and having routing decline it afterwards. The child is
    refused here, at the create, where the caller still sees the reason.

    :param parent: The parent session's row, or ``None`` for a top-level
        create.
    :param child_harness: The harness the child would run, e.g.
        ``"claude-native"``. ``None`` (unresolvable) is allowed through —
        nothing proves it is out of family.
    :raises OmnigentError: 400 ``INVALID_INPUT`` when the child's family
        differs from the family the parent's spawns are confined to.
    """
    family = _pinned_spawn_family(parent)
    child_family = harness_family(child_harness)
    if family is None or child_family is None or family == child_family:
        return
    parent_harness = _resolve_harness(parent)
    raise OmnigentError(
        f"A {parent_harness} session with Smart Routing spawns only agents in its own "
        f"model family; {child_harness} is not one. Pick an agent that runs on "
        f"{parent_harness}, or start the session in Smart Routing (auto) harness mode "
        "to let the router choose the family.",
        code=ErrorCode.INVALID_INPUT,
    )


def _out_of_family_spawn_notice(
    conv: Conversation,
    parent: Conversation | None,
    harness: str | None,
) -> str | None:
    """Why a pinned parent's cross-family spawn is not routed, if it is not.

    The fail-safe behind :func:`_reject_out_of_family_child`, which refuses
    the out-of-family child at its create: a pane that exists anyway (a
    legacy row, an unresolvable harness at create time) still must not be
    routed. Cross-family picks belong to sessions whose harness the router
    owns (Smart Routing / auto): a pinned codex session's spawns stay on
    codex, so a child pane running another family's CLI has no candidate the
    parent's family can serve. Same predicate the in-harness spawn gate uses,
    so "auto" means one thing on both spawn paths.

    :param conv: The child session's row.
    :param parent: The parent session's row, or ``None`` for a top-level
        session (nothing to stay in family with).
    :param harness: The child's resolved harness, e.g. ``"claude-native"``.
    :returns: The rationale for the declined decision, or ``None`` when the
        spawn may be routed.
    """
    if parent is None or auto_harness_session(conv, parent):
        return None
    parent_harness = _resolve_harness(parent)
    parent_family = harness_family(parent_harness)
    child_family = harness_family(harness)
    if parent_family is None or child_family is None or parent_family == child_family:
        return None
    return (
        f"Not routed: a {parent_harness} session's spawns stay in its own model "
        f"family, and this sub-agent runs on {harness}."
    )


def _publish_routed_model(session_id: str, model: str) -> None:
    """
    Publish a ``session.model`` SSE for a router-selected model.

    The model dropdown updates from PATCH responses and ``session.model``
    events only, so a routed session shows the launch model until this
    fires. Mirrors the event ``_persist_external_model_change`` publishes
    for harness-observed switches; if the harness-side apply later fails,
    that mirror corrects this value.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :param model: The model in the spelling this session's picker shows.
        For a switch applied to a running claude-native pane that is the
        pane's own ``/model`` vocabulary — a tier alias such as
        ``"opus"`` (see :func:`_routed_turn_model_spelling`); for a
        session pinned before its harness starts (a routed child) it is
        the catalog id the picker lists, e.g.
        ``"databricks-claude-opus-4-8"``. Either way it must be the value
        the picker can select, since the client sets its selection from it.
    :returns: None.
    """
    event = SessionModelEvent(
        type="session.model",
        conversation_id=session_id,
        model=model,
    )
    session_stream.publish(session_id, event.model_dump())


async def _forward_event_to_runner(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    agent_name: str | None = None,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    has_mcp_servers: bool = False,
    created_by: str | None = None,
    host_store: HostStore | None = None,
) -> str:
    """
    Persist a user event and forward it to the runner.

    The server persists the item to the conversation store
    (invariant I1: persist-before-forward), publishes acknowledgment
    events, then POSTs the event to the runner's
    ``POST /v1/sessions/{id}/events``.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The conversation row for ``session_id``.
    :param body: The validated event input from the client.
    :param conversation_store: Store for item persistence.
    :param runner_client: HTTP client pointed at the runner.
    :param agent_name: Human-readable agent name for the
        ``model`` field on the runner body, e.g. ``"research-agent"``.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references before forwarding.
    :param artifact_store: Optional binary content store for
        resolving ``file_id`` references before forwarding.
    :param has_mcp_servers: ``True`` when the agent spec declares at
        least one MCP server. Forwarded to the runner as the
        ``has_mcp_servers`` hint so ``proxy_stream`` knows to load
        the agent spec and initialise :class:`ProxyMcpManager` for
        this turn. ``False`` by default (agents without MCP servers).
    :param created_by: Authenticated identity of the posting actor,
        recorded on the persisted item for attribution.
    :param host_store: Host registrations, read only to learn whether this
        session's harness is AI-Gateway-backed (which router may route it).
        ``None`` reads as unknown, which counts as backed.
    :returns: The store-assigned id of the persisted item.
    """
    import uuid

    turn_id = f"turn_{uuid.uuid4().hex}"
    item = _build_new_item(body, turn_id, created_by=created_by)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [item],
    )
    await _seed_missing_title_from_user_message(
        conv,
        item,
        conversation_store,
    )
    # Don't publish status="running" or input.consumed here —
    # wait until after the forward to the runner succeeds.
    # Publishing early causes the REPL to start its streaming
    # timer before the turn actually starts, showing a
    # premature "working" phase.

    # Resolve file_id references (input_image, input_file) to
    # inline base64 data: URIs before forwarding. The runner and
    # harness don't have access to the server's file store — the
    # LLM endpoint needs the actual content, not an internal ID.
    forwarded_data = dict(body.data)
    if (
        file_store is not None
        and artifact_store is not None
        and "content" in forwarded_data
        and isinstance(forwarded_data["content"], list)
    ):
        from omnigent.runtime.content_resolver import (
            _resolve_message_content,
        )

        _unresolved = [
            b for b in forwarded_data["content"] if isinstance(b, dict) and "file_id" in b
        ]
        if _unresolved:
            try:
                forwarded_data["content"] = _resolve_message_content(
                    forwarded_data["content"],
                    file_store,
                    artifact_store,
                    session_id=session_id,
                )
                _logger.debug(
                    "Resolved %d file_id block(s) for session=%s before forwarding",
                    len(_unresolved),
                    session_id,
                )
            except (ValueError, KeyError):
                _logger.warning(
                    "File reference resolution failed for session=%s "
                    "(unresolved file_id blocks will reach the runner unresolved — "
                    "runner will attempt fallback resolution)",
                    session_id,
                    exc_info=True,
                )

    # Flatten SessionEventInput {type, data} into the runner's
    # discriminated-union shape {type, ...data_fields}. The runner's
    # POST handler expects the harness event shape, not the
    # session-API wrapper. Include agent_id so the runner can
    # resolve the harness type and spawn environment.
    runner_body: dict[str, Any] = {
        "type": body.type,
        **forwarded_data,
        "agent_id": conv.agent_id,
        # model tags the ResponseObject for REPL rendering.
        # Use the human-readable agent name when available.
        "model": agent_name or conv.agent_id or "",
        # Signal to proxy_stream that it should initialise
        # ProxyMcpManager and fetch MCP tool schemas for this turn.
        # Only included (and only True) when the agent has MCP
        # servers — False/absent saves the runner from a no-op spec
        # load on every turn for agents without MCP servers.
        "has_mcp_servers": has_mcp_servers,
        # Id of the item just persisted for this turn. On a cold runner
        # cache the runner reloads history (which includes this item in
        # PRE-resolution form) and drops it by id, appending its own
        # resolved copy — id-based dedup, not a role/content guess.
        "persisted_item_id": persisted_items[0].id,
    }
    # Persist the turn-initiating actor so /policies/evaluate and MCP
    # tools/call can read it back on any server replica.  Skip system-driven
    # forwards (sub-agent results, parent-wake carry created_by=None) — they
    # must not stomp the in-flight turn's actor.
    # Known gap: a queued message from user B can overwrite this label while
    # user A's turn is still executing tool calls on a shared session.  The
    # runner's _active_turns guard prevents two turns from running on the same
    # session concurrently, but the label is written at server-forward time
    # (before the runner queues the message), not at runner-turn-start time.
    # For the common case (sequential users or single-user sessions) this is
    # correct; strictly concurrent shared-session use is an accepted gap.
    if created_by is not None:
        await asyncio.to_thread(
            conversation_store.set_labels,
            session_id,
            {_TURN_ACTOR_LABEL: created_by},
        )
    # Forward request-supplied client-side tool schemas so non-native
    # harnesses can emit (and tunnel) the caller's tools — the runner
    # merges these into the harness tool list (_merge_request_client_tools).
    # Without this the runner only ever sees the spec's builtin/MCP tools
    # and the model can't invoke client-side Read/Write/Glob/etc.
    if body.tools:
        runner_body["tools"] = body.tools
    # Per-event override wins; fall back to the persisted column so a
    # UI / REPL PATCH applies even when the client doesn't repeat
    # model_override on every event. ``is not None`` over ``or`` per
    # the no-invented-defaults rule.
    effective_runner_override = (
        body.model_override if body.model_override is not None else conv.model_override
    )
    # ── Auto-harness resolution ───────────────────────────────────────
    # When the session was created with harness_override="auto", the real
    # harness + model are determined here on the first message where user
    # text is available.  After resolution the sentinel is replaced with
    # the concrete harness so subsequent turns behave normally.
    # Tracks whether this block ran the router this turn, so the message
    # routing block below doesn't re-route the same message (which would
    # double the judge call, emit two cards, and risk a mismatched pick).
    _auto_resolved_this_turn = False
    # Auto-harness verdict captured for card emission AFTER the runner forward
    # and input.consumed (so the live SSE stream delivers the user bubble
    # before the routing card, matching the message routing path).
    _auto_card_model: str | None = None
    _auto_card_verdict: dict[str, Any] | None = None
    # Set when the auto-harness routing call itself failed. The card still
    # says so, but the route-once label is left unclaimed — the same rule the
    # turn, native-pane and child-spawn paths follow, so one outage at create
    # cannot be the reason this session never routes again.
    _auto_route_failed = False
    # The resolved harness, read back at card-emission time below.
    _auto_harness: str | None = None
    if conv.harness_override == "auto" and body.type == "message":
        from omnigent.server.smart_routing import (
            _AUTO_ROUTING_HARNESSES,
            route_session_harness,
        )

        _auto_text = _extract_user_text_for_routing(body)
        if _auto_text:
            _auto_resolved_this_turn = True
            # The router may land this session on any auto-routing harness, so
            # every one of them must be gateway-backed for the workspace router
            # to serve the pick; otherwise the built-in judge does.
            _auto_backed = _gateway_backed(
                await _session_routing_host(conv, host_store), _AUTO_ROUTING_HARNESSES
            )
            # For a forced-auto child, route against the parent's catalog (full
            # spawnable-worker map) rather than the child's leaf "self" catalog.
            _auto_harness, _auto_model, _auto_verdict, _auto_error = await route_session_harness(
                _auto_text,
                session_id=session_id,
                catalog_session_id=conv.parent_conversation_id,
                runner_client=runner_client,
                gateway_backed=_auto_backed,
                allow_static_fallback=_auto_backed,
            )
            try:
                # Always clear the "auto" sentinel even when routing
                # returned no harness (unavailable/failed) so the branch
                # doesn't re-run on every subsequent turn.
                _conv_updates: dict[str, Any] = (
                    {"harness_override": _auto_harness}
                    if _auto_harness is not None
                    else {"_unset_harness_override": True}
                )
                if _auto_model is not None and effective_runner_override is None:
                    _conv_updates["model_override"] = _auto_model
                    effective_runner_override = _auto_model
                _updated = await asyncio.to_thread(
                    conversation_store.update_conversation,
                    session_id,
                    **_conv_updates,
                )
                if _updated is not None:
                    conv = _updated
            except (OSError, ValueError):
                _logger.warning(
                    "auto-harness: failed to persist resolved harness for session=%s",
                    session_id,
                    exc_info=True,
                )
            # Defer card emission until after input.consumed (see below).
            if _auto_model is not None and _auto_verdict is not None:
                _auto_card_model = _auto_model
                _auto_card_verdict = _auto_verdict
            elif _auto_error is not None:
                # Routing failed — surface why auto-harness fell back to defaults.
                _auto_card_model, _auto_card_verdict = _unavailable_routing_card(_auto_error)
                _auto_route_failed = True
    # ── Server-side intelligent routing ──────────────────────────────
    # When the session toggle is ON and no model has been chosen yet,
    # call the judge LLM on the FIRST message to pick the model for
    # the entire session.  The verdict is persisted as model_override
    # on the conversation so subsequent turns reuse it without another
    # judge call.
    # Route if: this session's own toggle is on (top-level), OR this is a
    # sub-agent and its parent's subagent-routing switch is on — the same switch
    # the native-subagent hook and the child-create path gate on, so one visible
    # knob governs every spawn.
    _parent_routing_on = False
    _parent_conv: Conversation | None = None
    if conv.parent_conversation_id is not None:
        _parent_conv = await asyncio.to_thread(
            conversation_store.get_conversation, conv.parent_conversation_id
        )
        _parent_routing_on = _parent_conv is not None and subagent_routing_enabled(
            _parent_conv.subagent_routing_override
        )
    _routing_enabled = (
        conv.cost_control_mode_override == "on" and conv.parent_conversation_id is None
    ) or _parent_routing_on
    _routed_model: str | None = None
    _routed_harness: str | None = None
    _verdict: dict[str, Any] | None = None
    # Set when the pick has no spelling this session can be switched to, so
    # nothing was pinned and the chip must say so.
    _turn_unapplied = False
    # Set when the routing call itself failed — on either branch below. The
    # chip then carries the "unavailable" placeholder rather than a model,
    # nothing is pinned, and the route-once label is left unclaimed.
    _route_failed = False
    # The model the orchestrator's ``sys_session_send`` asked for, captured
    # before routing overwrites it: when the router picks something else the
    # attempt is recorded on the decision instead of silently vanishing.
    _attempted_override = effective_runner_override
    # A child that already carries a routing decision is done: its harness and
    # model were chosen on its first message and must not move again. Without
    # this the child clause below (which deliberately routes past an
    # orchestrator-supplied model) matched on EVERY message, so a follow-up
    # re-ran the judge and re-persisted ``harness_override`` — a mid-session
    # harness flip, contradicting the create-time-only contract the
    # ``harness_override`` forward below documents. The top-level arm's own
    # once-only gate is ``effective_runner_override is None``; the decision
    # label is the child's equivalent, because a child's model_override can be
    # set by ``sys_session_send`` without anything having routed.
    _child_routed_before = conv.parent_conversation_id is not None and bool(
        conv.labels.get(ROUTING_DECISION_LABEL_KEY)
    )
    # For child sessions, route even when the orchestrator specified a model via
    # sys_session_send (effective_runner_override is already set). Smart routing
    # always wins over the LLM's own model choice when the parent toggle is on.
    _should_route = (
        _routing_enabled
        and body.type == "message"
        # The auto-harness block above already routed this turn (harness +
        # model) — don't re-run the router for the same message.
        and not _auto_resolved_this_turn
        and not _child_routed_before
        and (effective_runner_override is None or conv.parent_conversation_id is not None)
    )
    if _should_route:
        _user_text = _extract_user_text_for_routing(body)
        if _user_text:
            if _parent_routing_on:
                # Child sessions: use route_session_harness to pick both harness
                # and model, overriding whatever the orchestrator specified in
                # sys_session_send.
                from omnigent.server.smart_routing import (
                    AUTO_NATIVE_ROUTING_HARNESSES,
                    route_session_harness,
                )

                # Confine the pick to the harness this CHILD runs. A child that
                # reaches here has one: either its sub-agent spec declares it
                # (a named worker like polly's ``pi``) or the spawn pinned it —
                # an unpinned child of a Smart Routing parent carries the
                # "auto" sentinel and was resolved by the auto block above.
                # Read off the PARENT before, which offered a pi worker the
                # brain's claude family and a claude worker the codex family;
                # under an auto parent it dropped the restriction entirely, so
                # a pi child could be handed a codex verdict it then ran pi on.
                _child_harness = _resolve_harness(conv)
                _child_pinned = _child_harness is not None and _child_harness != "auto"
                _child_family = harness_family(_child_harness) if _child_pinned else None
                # A single-harness candidate set is the honest offer for a
                # pinned child: the family filter alone still admits the other
                # harnesses in that family (a ``codex-native`` child would be
                # offered SDK ``codex``), and it cannot narrow a harness whose
                # family is unknown at all.
                _child_candidates = (_child_harness,) if _child_pinned else None
                # Which harnesses the pick may land on decides whose gateway
                # backing must hold: a cross-harness child can go either way, a
                # pinned one only where it already runs.
                _child_gateway_harnesses = (
                    (_child_harness or "",) if _child_pinned else AUTO_NATIVE_ROUTING_HARNESSES
                )
                _child_host = await _session_routing_host(conv, host_store)
                if _child_host is None and _parent_conv is not None:
                    _child_host = await _session_routing_host(_parent_conv, host_store)
                _child_backed = _gateway_backed(_child_host, _child_gateway_harnesses)
                # Route against the PARENT's catalog: it enumerates the
                # spawnable workers (claude_code/codex/pi) with full model
                # lists, whereas this child's own leaf catalog is "self"-only
                # and would force the static fallback (a smaller/different set).
                _routed_harness, _routed_model, _verdict, _route_err = await route_session_harness(
                    _user_text,
                    session_id=session_id,
                    catalog_session_id=conv.parent_conversation_id,
                    runner_client=runner_client,
                    allowed_family=_child_family,
                    harness_candidates=_child_candidates,
                    gateway_backed=_child_backed,
                    allow_static_fallback=_child_backed,
                )
                if _routed_model is not None:
                    effective_runner_override = _routed_model
                try:
                    _child_updates: dict[str, Any] = {}
                    if _routed_model is not None:
                        _child_updates["model_override"] = _routed_model
                    if _routed_harness is not None:
                        _child_updates["harness_override"] = _routed_harness
                    if _child_updates:
                        await asyncio.to_thread(
                            conversation_store.update_conversation,
                            session_id,
                            **_child_updates,
                        )
                        if _routed_model is not None:
                            # The child's picker lists catalog ids and its
                            # harness starts on this pin, so the catalog id
                            # is the spelling to publish.
                            _publish_routed_model(session_id, _routed_model)
                except (OSError, ValueError):
                    _logger.warning(
                        "smart_routing: failed to persist harness/model for child session=%s",
                        session_id,
                        exc_info=True,
                    )
                if _routed_model is None and _route_err is not None:
                    # ``route_session_harness`` already fails open, so the spawn
                    # runs on whatever the orchestrator asked for — but the
                    # reason was unpacked and dropped, leaving no card and no
                    # way to tell an outage from a router with no opinion. Say
                    # it, on the same placeholder the other paths use. Set last
                    # so nothing above can pin or publish the placeholder.
                    _routed_model, _verdict = _unavailable_routing_card(_route_err)
                    _route_failed = True
            else:
                # Top-level sessions: model-only routing (harness already fixed by spec).
                from omnigent.server.smart_routing import route_turn_or_decline

                _harness = _resolve_harness(conv)
                # A turn cannot change harness, so only this session's own
                # family has to be gateway-backed for the workspace router.
                _turn_backed = _gateway_backed(
                    await _session_routing_host(conv, host_store), (_harness or "",)
                )
                # ``_or_decline``: a routing outage returns an error string
                # rather than raising. Raised here it became a 500 on the
                # events POST, so a router that was merely down cost the user
                # a message that had already been persisted.
                _routed_model, _verdict, _turn_route_err = await route_turn_or_decline(
                    _harness,
                    _user_text,
                    session_id=session_id,
                    runner_client=runner_client,
                    catalog=await _native_turn_catalog(session_id, conv, runner_client),
                    gateway_backed=_turn_backed,
                    allow_static_fallback=_turn_backed,
                )
                if _turn_route_err is not None:
                    # Not routed, and visibly so — the turn runs on the
                    # session's own model with the reason on its card.
                    _routed_model, _verdict = _unavailable_routing_card(_turn_route_err)
                    _route_failed = True
                if _routed_model is not None and not _route_failed:
                    # Whether the session can actually be switched onto the
                    # pick decides everything downstream: an unapplicable
                    # model must not be pinned (the pin disables routing for
                    # every later turn) nor reported as this turn's model.
                    _turn_spelling = _routed_turn_model_spelling(session_id, conv, _routed_model)
                    if _turn_spelling is None:
                        _turn_unapplied = True
                    else:
                        effective_runner_override = _routed_model
                        # Persist as the session's model_override so all
                        # subsequent turns use this model automatically.
                        try:
                            await asyncio.to_thread(
                                conversation_store.update_conversation,
                                session_id,
                                model_override=_routed_model,
                            )
                            _publish_routed_model(session_id, _turn_spelling)
                        except (OSError, ValueError):
                            _logger.warning(
                                "smart_routing: failed to persist model_override "
                                "for session=%s; turn still uses routed model",
                                session_id,
                                exc_info=True,
                            )
    # ────────────────────────────────────────────────────────────────
    if effective_runner_override is not None:
        runner_body["model_override"] = effective_runner_override
    # Per-session brain-harness override — create-time only, so no
    # per-event value exists; the persisted column is the source.
    # _routed_harness is non-None when the child routing path resolved one
    # this turn (conv is not refreshed, so we use the in-flight value).
    _effective_harness = _routed_harness or conv.harness_override
    if _effective_harness is not None and _effective_harness != "auto":
        runner_body["harness_override"] = _effective_harness

    # The runner's sessions-native POST returns 202 immediately
    # and starts the turn as a background task. No streaming
    # response to drain — events flow through GET /stream.
    try:
        await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=runner_body,
            timeout=_RUNNER_FORWARD_TIMEOUT,
        )
        # Publish input.consumed AFTER the forward succeeds —
        # the runner has the message and will start the turn.
        _publish_input_consumed(session_id, persisted_items[0])
        # Emit the routing_decision chip AFTER input.consumed so the
        # live SSE stream delivers the user bubble before the chip —
        # matching the store order (user message was persisted first).
        # Auto-harness card (success or failure) emitted here for the same
        # ordering reason; it was resolved earlier in the turn.
        if _auto_card_model is not None and _auto_card_verdict is not None:
            _auto_decision_id = await _emit_server_routing_decision(
                session_id,
                conversation_store,
                _auto_card_model,
                _auto_card_verdict,
                scope="session",
                harness=_auto_harness,
            )
            if not _auto_route_failed:
                # A failed call is NOT this session's routing decision: the
                # label is the route-once gate, so claiming it would make one
                # create-time outage the reason nothing routes this session
                # again. The card still shows what happened.
                await _stamp_routing_decision_label(
                    session_id, conversation_store, _auto_decision_id
                )
            if conv.parent_conversation_id is not None:
                await _emit_server_routing_decision(
                    conv.parent_conversation_id,
                    conversation_store,
                    _auto_card_model,
                    _auto_card_verdict,
                    agent=agent_name or "",
                    scope="session",
                    harness=_auto_harness,
                    decision_id=_auto_decision_id,
                )
        if _routed_model is not None and _verdict is not None:
            _decision_scope = "child_session" if _parent_routing_on else "turn"
            if _turn_unapplied:
                _verdict = _unapplied_routed_verdict(_routed_model, _verdict)
            # The router wins over the orchestrator's own pick; the attempt is
            # recorded so the UI can show what it overrode. Compared on the bare
            # arm, so another spelling of the same model (a spawn's
            # ``system.ai.glm-5-2`` against the router's ``databricks-glm-5-2``)
            # is not reported as an override.
            from omnigent.server.smart_routing import _bare_id as _bare_model_id

            _overridden = (
                _attempted_override
                if _attempted_override is not None
                and not _route_failed
                and _bare_model_id(_attempted_override) != _bare_model_id(_routed_model)
                else None
            )
            _decision_id = await _emit_server_routing_decision(
                session_id,
                conversation_store,
                _routed_model,
                _verdict,
                scope=_decision_scope,
                harness=_routed_harness or _resolve_harness(conv),
                attempted_override=_overridden,
            )
            if not _route_failed:
                # A failed call is NOT this session's routing decision: the
                # label is the route-once gate, so stamping it would make one
                # outage the reason the session never routes again. The card
                # still shows what happened.
                await _stamp_routing_decision_label(session_id, conversation_store, _decision_id)
            # Mirror the routing decision into the parent session so the
            # orchestrator's transcript also shows which model was chosen
            # for this sub-agent — the decision is otherwise only visible
            # on the child session screen.
            if _parent_routing_on and conv.parent_conversation_id is not None:
                await _emit_server_routing_decision(
                    conv.parent_conversation_id,
                    conversation_store,
                    _routed_model,
                    _verdict,
                    agent=agent_name or "",
                    scope=_decision_scope,
                    harness=_routed_harness or _resolve_harness(conv),
                    decision_id=_decision_id,
                    attempted_override=_overridden,
                )
    except (httpx.HTTPError, ConnectionError) as exc:
        _logger.exception(
            "Forward to runner failed for session=%s",
            session_id,
        )
        _publish_status(session_id, "idle")
        raise OmnigentError(
            "Runner is unreachable; message was persisted but could not be delivered. "
            "The runner may be restarting — retry or spawn a new session.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc

    return persisted_items[0].id


async def _stamp_routing_decision_label(
    session_id: str,
    conversation_store: ConversationStore,
    decision_id: str | None,
) -> None:
    """Record the decision behind a session's pinned model as a label.

    The child-sessions API reads it to render which decision produced a
    sub-agent's routed model, without a new conversation column.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store exposing ``set_labels``.
    :param decision_id: Decision identity, or ``None`` to skip.
    """
    if decision_id is None:
        return
    try:
        await asyncio.to_thread(
            conversation_store.set_labels,
            session_id,
            {ROUTING_DECISION_LABEL_KEY: decision_id},
        )
    except (OSError, ValueError):
        _logger.warning(
            "smart_routing: failed to label routing decision for session=%s",
            session_id,
            exc_info=True,
        )


async def _record_create_route_prompt(
    conv: Conversation,
    conversation_store: ConversationStore,
    prompt: str | None,
) -> Conversation:
    """Record which prompt a create-time route scored, as a fingerprint.

    Read by the in-harness first-prompt hook: the same prompt submitted again
    reuses the create's decision instead of paying a second router call for it
    (:func:`omnigent.runner.turn_routing.create_route_covers_prompt`).

    Best-effort — a label that cannot be written costs the session one extra
    routing call, not its turn.

    :param conv: The created conversation row.
    :param conversation_store: Store exposing ``set_labels``.
    :param prompt: The routed prompt, e.g. the create's
        ``smart_routing_message``. Blank records nothing.
    :returns: The refreshed row, or *conv* when nothing was recorded.
    """
    from omnigent.runner.subagent_routing import CREATE_ROUTE_PROMPT_LABEL_KEY
    from omnigent.runner.turn_routing import create_route_prompt_fingerprint

    fingerprint = create_route_prompt_fingerprint(prompt or "")
    if not fingerprint:
        return conv
    try:
        await asyncio.to_thread(
            conversation_store.set_labels,
            conv.id,
            {CREATE_ROUTE_PROMPT_LABEL_KEY: fingerprint},
        )
    except (OSError, ValueError):
        _logger.warning(
            "smart_routing: failed to record the create-time prompt for session=%s",
            conv.id,
            exc_info=True,
        )
        return conv
    return await asyncio.to_thread(conversation_store.get_conversation, conv.id) or conv


async def _dispatch_session_event_to_runner(*args: Any, **kwargs: Any) -> Any:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._dispatch_session_event_to_runner(*args, **kwargs)


async def _dispatch_session_event_to_runner_impl(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    *,
    agent_name: str | None,
    file_store: FileStore | None,
    artifact_store: ArtifactStore | None,
    has_mcp_servers: bool = False,
    created_by: str | None = None,
    runner_router: RunnerRouter | None = None,
    native_terminal_ready: bool = False,
    host_store: HostStore | None = None,
) -> _SessionEventDispatchResult:
    """
    Forward an item-event to the runner with harness-aware dispatch.

    Callers stay harness-agnostic — the claude-native message bypass
    is encapsulated here. Two dispatch outcomes:

    * **transcript-forwarded native + ``type == "message"``**: web-chat user
      messages on these sessions must NOT be persisted by the AP
      server. The Omnigent would otherwise persist an AP-side copy AND
      let the transcript forwarder mirror the same message back
      (with its own store-assigned item id), so every web-typed
      prompt would land as two items in the chat panel. We forward
      to the bound runner so the native harness types the
      message into tmux; the transcript forwarder becomes the
      single writer for the conversation history. Returns a result
      with ``item_id=None`` (no AP-side persisted item) and a
      ``pending_id`` for the optimistic-bubble index entry.

    * **All other cases**: persist the item AP-side (invariant I1:
      persist-before-forward) and forward via the harness's
      ``/events`` scaffold. Returns the persisted item id and
      ``pending_id=None``.

    The single-writer invariant is the entire reason the bypass
    exists; do NOT collapse the two branches into a single forward
    that always persists. Doing so on a native session causes
    duplicate items in the chat panel as soon as the transcript
    forwarder mirrors the same prompt back.

    The pending-input entry recorded on the native path bridges the
    transcript round-trip: until the forwarder mirrors the message
    back, it lives nowhere durable, so a client that navigates away /
    rebinds would lose the optimistic bubble. The entry is replayed
    into the snapshot and drained when the message persists (see
    :mod:`omnigent.runtime.pending_inputs`). It is rolled back if the
    forward fails, so a never-delivered message leaves no ghost.

    :param session_id: Session/conversation identifier.
    :param conv: Conversation row for *session_id*.
    :param body: Validated event from the client.
    :param conversation_store: Used by the non-native path to
        persist the item.
    :param runner_client: The session's runner client, already
        resolved by the caller via :func:`_get_runner_client`.
    :param agent_name: Human-readable agent name for the
        ``model`` field on non-native forwards.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references before forwarding.
    :param artifact_store: Optional binary store for the same.
    :param has_mcp_servers: ``True`` when the agent spec declares at
        least one MCP server. Forwarded to the runner as the
        ``has_mcp_servers`` hint. ``False`` by default.
    :param created_by: Authenticated identity of the posting actor,
        e.g. ``"alice@example.com"``. On the non-native path it is
        recorded directly on the persisted item. On the claude-native
        bypass the transcript forwarder is the single writer, so
        ``created_by`` is stored in the ``pending_inputs`` entry via
        :func:`omnigent.runtime.pending_inputs.record` and applied
        to the item when the forwarder mirrors it back (see
        :func:`_persist_external_conversation_item`).
    :param runner_router: Router used to resolve the runner for the
        native-terminal parent-wake forward when a sub-agent fails to
        boot (see :func:`_persist_native_terminal_failure`). ``None``
        in in-process / test setups where the global client is used.
    :param native_terminal_ready: A current initialization response already
        proved the terminal and forwarder ready, so the immediate duplicate
        ensure can be skipped.
    :param host_store: Host registrations, read only to learn whether this
        session's harness is AI-Gateway-backed (which router may route it).
        ``None`` reads as unknown, which counts as backed — the same posture
        an older host row gets.
    :returns: A :class:`_SessionEventDispatchResult` carrying the
        persisted item id (non-native) or the pending-input id
        (claude-native message bypass).
    """
    if body.type == "message" and _is_native_terminal_session(conv):
        # Validate before touching the runner. The ensure probe is only
        # for syntactically valid user messages; assistant/system-shaped
        # inputs should still fail locally without creating terminals.
        _build_native_terminal_message_event(conv, body)
        ensure_outcome = (
            _NativeTerminalEnsureOutcome(error=None)
            if native_terminal_ready
            else await _ensure_native_terminal_ready(
                runner_client,
                session_id,
                conv,
            )
        )
        if ensure_outcome.error is not None:
            item_id = await _persist_native_terminal_failure(
                session_id,
                conv,
                body,
                conversation_store,
                ensure_outcome.error,
                runner_router,
                created_by=created_by,
            )
            return _SessionEventDispatchResult(item_id=item_id, pending_id=None)
        if ensure_outcome.policy_notice is not None:
            # Terminal is up but policy enforcement is off (fail-open). Post
            # a durable, non-fatal banner; the user message still forwards.
            await _persist_native_policy_notice(
                session_id,
                conversation_store,
                ensure_outcome.policy_notice,
            )
        # Record the optimistic bubble before forwarding so it's known
        # server-side immediately (replayed into the snapshot). Roll it
        # back on any failure/cancellation so a message the TUI never
        # received doesn't replay as a ghost.
        content = body.data.get("content")
        pending_id: str | None = (
            pending_inputs.record(session_id, content, created_by=created_by)
            if isinstance(content, list) and content
            else None
        )
        # ── Server-side routing for native terminal sessions ────────
        # Same logic as the SDK path in _forward_event_to_runner: if
        # the toggle is on and no model_override is set, call the
        # judge and persist the chosen model on the conversation row.
        # The native CLI reads model_override from the session. A child
        # routes off its parent's subagent-routing switch.
        _native_parent_routing_on = False
        _native_parent_conv: Conversation | None = None
        if conv.parent_conversation_id is not None:
            _native_parent_conv = await asyncio.to_thread(
                conversation_store.get_conversation, conv.parent_conversation_id
            )
            _native_parent_routing_on = (
                _native_parent_conv is not None
                and subagent_routing_enabled(_native_parent_conv.subagent_routing_override)
            )
        _native_routing_enabled = (
            conv.cost_control_mode_override == "on" and conv.parent_conversation_id is None
        ) or _native_parent_routing_on
        _native_routed_model: str | None = None
        _native_verdict: dict[str, Any] | None = None
        # Set when nothing was routed — the call failed, or the family rule
        # below refused a cross-family spawn. The chip then carries the
        # "unavailable" placeholder, nothing is pinned or switched, and the
        # route-once label is left unclaimed.
        _native_route_failed = False
        _native_scope = "child_session" if _native_parent_routing_on else "turn"
        # The routed pick in the spelling this pane accepts, or ``None`` when
        # it has none. Gates the pin AND the in-band switch below: a model the
        # pane cannot take must not become ``model_override``, which would
        # disable routing for every later turn and misattribute usage.
        _native_applied_model: str | None = None
        if _native_routing_enabled and (
            conv.model_override is None or conv.parent_conversation_id is not None
        ):
            from omnigent.server.smart_routing import route_turn_or_decline

            _harness = _native_pane_harness(conv)
            _user_text = _extract_user_text_for_routing(body)
            # Cross-family spawns belong to auto-harness sessions: a pinned
            # session's children stay in its family (the rule the in-harness
            # spawn gate applies via ``candidate_models(cross_harness=False)``).
            # This pane already runs another family's CLI, so the parent's
            # family has no candidate for it and nothing is routed.
            _native_out_of_family = _out_of_family_spawn_notice(
                conv, _native_parent_conv, _harness
            )
            if _user_text and _native_out_of_family is not None:
                _native_routed_model, _native_verdict = _unavailable_routing_card(
                    _native_out_of_family
                )
                _native_route_failed = True
            elif _user_text:
                _native_runner_client = await _get_runner_client(session_id, runner_router)
                # This pane's own family decides which router can serve it: off
                # the gateway the built-in judge routes off the pane's picker
                # vocabulary, which is the only reachable candidate set anyway.
                _native_backed = _gateway_backed(
                    await _session_routing_host(conv, host_store), (_harness or "",)
                )
                # ``_or_decline``: a routing outage must not 500 the message
                # POST. The pane keeps its own model and the card says why.
                (
                    _native_routed_model,
                    _native_verdict,
                    _native_route_err,
                ) = await route_turn_or_decline(
                    _harness,
                    _user_text,
                    session_id=session_id,
                    runner_client=_native_runner_client,
                    catalog=await _native_turn_catalog(session_id, conv, _native_runner_client),
                    gateway_backed=_native_backed,
                    allow_static_fallback=_native_backed,
                )
                if _native_route_err is not None:
                    _native_routed_model, _native_verdict = _unavailable_routing_card(
                        _native_route_err
                    )
                    _native_route_failed = True
                if _native_routed_model is not None and not _native_route_failed:
                    # A pane that already took a routed turn can only be moved
                    # by ``/model``, so the pick must be in its vocabulary —
                    # true of every top-level turn (the pane is up before
                    # routing runs) and of a child from its second spawn on.
                    # On a pane's first routed turn the launch env carries the
                    # id, so no spelling exists to require.
                    _native_pane_routed_before = ROUTING_DECISION_LABEL_KEY in conv.labels
                    _native_applied_model = (
                        _routed_turn_model_spelling(session_id, conv, _native_routed_model)
                        if _native_scope == "turn" or _native_pane_routed_before
                        else _native_routed_model
                    )
                if _native_applied_model is not None:
                    try:
                        await asyncio.to_thread(
                            conversation_store.update_conversation,
                            session_id,
                            model_override=_native_routed_model,
                        )
                        _publish_routed_model(session_id, _native_applied_model)
                    except (OSError, ValueError):
                        _logger.warning(
                            "smart_routing: persist failed for native session=%s",
                            session_id,
                            exc_info=True,
                        )
        # ────────────────────────────────────────────────────────────
        # Forward the message, carrying any routed model in-band. The
        # executor applies ``/model`` and injects the message as ONE step
        # under its pane lock, so the switch can't race the message inject
        # on the tmux pane (the earlier separate ``model_change`` POST did,
        # dropping the first message). model_override alone is applied only
        # at spawn, so the in-band switch is what makes routing take on an
        # already-running pane.
        forwarded = False
        try:
            await _forward_native_terminal_message(
                runner_client,
                session_id,
                conv,
                body,
                file_store=file_store,
                artifact_store=artifact_store,
                # The executor speaks the pane's own vocabulary, so it takes the
                # routed id; ``None`` when the pane has no spelling for it.
                model_override=(
                    _native_routed_model if _native_applied_model is not None else None
                ),
            )
            forwarded = True
        finally:
            if not forwarded and pending_id is not None:
                pending_inputs.resolve(session_id, pending_id)
        # Emit the routing chip AFTER forwarding the message to the
        # terminal so the live SSE stream delivers the user bubble
        # (echoed back by the CLI) before the chip.
        if _native_routed_model is not None and _native_verdict is not None:
            if _native_applied_model is None and not _native_route_failed:
                _native_verdict = _unapplied_routed_verdict(_native_routed_model, _native_verdict)
            _native_decision_id = await _emit_server_routing_decision(
                session_id,
                conversation_store,
                _native_routed_model,
                _native_verdict,
                scope=_native_scope,
                harness=_resolve_harness(conv),
            )
            if not _native_route_failed:
                # The label is the route-once gate, so a failed call must not
                # claim it — one outage would otherwise stop this pane's
                # in-harness hook from ever routing.
                await _stamp_routing_decision_label(
                    session_id, conversation_store, _native_decision_id
                )
            if _native_parent_routing_on and conv.parent_conversation_id is not None:
                await _emit_server_routing_decision(
                    conv.parent_conversation_id,
                    conversation_store,
                    _native_routed_model,
                    _native_verdict,
                    agent=agent_name or "",
                    scope=_native_scope,
                    harness=_resolve_harness(conv),
                    decision_id=_native_decision_id,
                )
        return _SessionEventDispatchResult(item_id=None, pending_id=pending_id)
    item_id = await _forward_event_to_runner(
        session_id,
        conv,
        body,
        conversation_store,
        runner_client,
        agent_name=agent_name,
        file_store=file_store,
        artifact_store=artifact_store,
        has_mcp_servers=has_mcp_servers,
        created_by=created_by,
        host_store=host_store,
    )
    return _SessionEventDispatchResult(item_id=item_id, pending_id=None)


async def _relay_runner_stream(
    session_id: str,
    runner_client: httpx.AsyncClient,
    conversation_store: ConversationStore,
    ready: asyncio.Event | None = None,
) -> None:
    """
    Subscribe to the runner's SSE stream and relay events locally.

    Long-lived background task that opens
    ``GET /v1/sessions/{id}/stream`` on the runner and publishes
    each event to the local ``session_stream`` pub-sub. Also
    updates ``_session_status_cache`` from turn lifecycle events
    and persists conversation items (assistant messages, tool
    calls) to the conversation store as they arrive.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_client: HTTP client pointed at the runner.
    :param conversation_store: Store for persisting conversation
        items extracted from the runner's SSE stream.
    :param ready: Optional event set once the runner stream emits its
        ready heartbeat, proving AP's runner-side no-replay subscriber
        slot is registered. ``None`` is accepted for direct unit tests
        that exercise relay parsing/persistence without asserting on
        startup readiness.
    """
    text_acc: list[str] = []
    current_response_id: str | None = None
    # Model/agent label from the turn header, stamped on text segments
    # flushed at tool-call boundaries (the boundary event carries no model).
    current_model: str | None = None
    # Map tool call_id → response_id so a function_call_output that
    # arrives after a new response.in_progress (different response_id)
    # still pairs with its matching function_call. Without this, the
    # web UI's block stream clears its pending-tool state on the
    # response_id transition and the tool card spinner never resolves.
    tool_call_response_ids: dict[str, str] = {}
    _logger.info("Relay: connecting to runner GET /stream for session=%s", session_id)

    # Read timeout: 3x the runner's session-stream heartbeat interval
    # (15s). Between turns the runner emits ``session.heartbeat`` every
    # 15s to keep proxies from dropping the idle connection. If 3
    # consecutive heartbeats are missed (45s), the connection is likely
    # dead — let the relay exit so ``_ensure_runner_relay`` can restart
    # it on the next ``POST /events``. ``connect`` stays at httpx's
    # default (5s); ``write``/``pool`` are not rate-limiting here.
    _relay_timeout = httpx.Timeout(connect=5.0, read=45.0, write=None, pool=None)
    try:
        async with runner_client.stream(
            "GET",
            f"/v1/sessions/{session_id}/stream",
            timeout=_relay_timeout,
        ) as resp:
            _logger.info("Relay: connected to runner GET /stream for session=%s", session_id)
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, _, buffer = buffer.partition("\n\n")
                    data_line = next(
                        (ln for ln in frame.splitlines() if ln.startswith("data:")),
                        None,
                    )
                    if data_line is None:
                        continue
                    payload = data_line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    evt_type = event.get("type", "")
                    # The runner emits session.status events
                    # directly.
                    # Re-publish via _publish_status so the event
                    # gets the conversation_id field required by
                    # SessionStatusEvent's schema. The cache write
                    # happens inside _publish_status itself.
                    # Runner-emitted keepalive — consumed to reset the
                    # read timeout; not forwarded to the session stream
                    # (the Omnigent subscriber generates its own heartbeats).
                    if evt_type == "session.heartbeat":
                        if ready is not None:
                            ready.set()
                        continue

                    # Stopped turn: drop its trailing response.* output (no
                    # forward, no persist) but keep text_acc — the pre-stop
                    # narration the user watched persists at the terminal flush.
                    if session_id in _interrupt_fenced_sessions:
                        if evt_type == "session.status" and event.get("status") == "running":
                            _interrupt_fenced_sessions.discard(session_id)
                        elif evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                            # Terminal proves the stopped turn is over (completed =
                            # the stop lost the race); process it normally.
                            _interrupt_fenced_sessions.discard(session_id)
                        elif (
                            evt_type.startswith("response.")
                            and evt_type not in _FENCE_EXEMPT_EVENT_TYPES
                        ):
                            continue

                    if evt_type == "session.status":
                        status = event.get("status", "")
                        if status:
                            # Forward the runner's failure detail on a
                            # ``failed`` transition so a SETUP-phase
                            # failure (which never emits response.failed)
                            # surfaces a real error message downstream
                            # instead of ending the turn silently.
                            raw_err = event.get("error")
                            status_error = (
                                ErrorDetail.model_validate(raw_err)
                                if isinstance(raw_err, dict)
                                else None
                            )
                            if status == "failed" and status_error is not None:
                                await _persist_session_status_error_labels(
                                    session_id,
                                    status_error,
                                    conversation_store,
                                )
                            elif status == "running":
                                await _persist_session_status_error_labels(
                                    session_id,
                                    None,
                                    conversation_store,
                                )
                                # A new turn proves the runner is live again, so
                                # a prior Stop that never dropped the tunnel must
                                # not leave the intentional-stop marker to swallow
                                # this turn's genuine disconnect. Fence-independent
                                # (the fence may already be cleared by a terminal
                                # stop event), so it fires on every running edge.
                                _intentional_stop_sessions.discard(session_id)
                            # PTY-activity status is a UI signal only. Terminal
                            # sub-agent delivery rides the Stop/StopFailure hook
                            # via external_session_status (the codex-shared path)
                            # — the PTY idle oscillates on mid-turn lulls and
                            # would deliver a premature, lock-out completion.
                            raw_blocked_on = event.get("blocked_on")
                            _publish_status(
                                session_id,
                                status,
                                status_error,
                                blocked_on=(
                                    raw_blocked_on
                                    if isinstance(raw_blocked_on, str) and raw_blocked_on
                                    else None
                                ),
                            )
                        if status == "running":
                            text_acc.clear()
                        continue

                    # Terminal spin-up status from the runner's auto-create
                    # path. Re-publish via _publish_terminal_pending so the
                    # event carries conversation_id and the cache write
                    # (read by the snapshot) stays coherent with the stream.
                    if evt_type == "session.terminal_pending":
                        # Use ``is True`` (not bool()) so a malformed frame
                        # with a string like ``"false"`` can't strand the
                        # spinner on — the runner always sends a real bool.
                        _publish_terminal_pending(
                            session_id,
                            event.get("pending") is True,
                        )
                        continue

                    # Track the turn's response_id from lifecycle
                    # events so persisted items share one id.
                    if evt_type == "response.in_progress":
                        resp_obj = event.get("response", {})
                        _rid = resp_obj.get("id")
                        if isinstance(_rid, str) and _rid:
                            current_response_id = _rid
                        _model = resp_obj.get("model")
                        if isinstance(_model, str) and _model:
                            current_model = _model

                    # Accumulate response-scoped (scaffold) text deltas for
                    # persistence. Native message-scoped deltas (with a
                    # message_id) persist via their own output_item.done(message),
                    # so buffering them here would double-persist. Guard on
                    # non-empty str (like inflight_text.record_publish) so a
                    # malformed delta can't break the later "".join(text_acc).
                    if evt_type == "response.output_text.delta" and not event.get("message_id"):
                        _delta = event.get("delta")
                        if isinstance(_delta, str) and _delta:
                            text_acc.append(_delta)

                    # Track tool call_id → response_id so a
                    # function_call_output that arrives under a later
                    # response still pairs with its call.  Done
                    # before _extract_persistent_item_from_sse because
                    # the parse may fail (serialization alias mismatch)
                    # while the mapping is still needed for the live
                    # event patch below.
                    _raw_item = event.get("item")
                    _item = _raw_item if isinstance(_raw_item, dict) else {}
                    _item_type = _item.get("type")
                    _item_call_id = _item.get("call_id")
                    _persist_rid: str | None
                    if (
                        _item_type == "function_call"
                        and _item.get("status") == "completed"
                        and isinstance(_item_call_id, str)
                        and current_response_id is not None
                    ):
                        tool_call_response_ids[_item_call_id] = current_response_id

                    # For function_call_output, use the response_id
                    # of the matching function_call so the web UI
                    # pairs them in the same bubble even when a new
                    # response.in_progress has already overwritten
                    # current_response_id.
                    if (
                        _item_type == "function_call_output"
                        and isinstance(_item_call_id, str)
                        and _item_call_id in tool_call_response_ids
                    ):
                        _persist_rid = tool_call_response_ids[_item_call_id]
                    else:
                        _persist_rid = current_response_id

                    # Flush buffered narration as its own message BEFORE the
                    # function_call it preceded, so the transcript interleaves
                    # [text, tool, text, tool] instead of pooling a turn's text
                    # after its tool calls (tools-above-text + run-on on reload).
                    if (
                        _item_type == "function_call"
                        and _item.get("status") == "completed"
                        and text_acc
                    ):
                        await _flush_relay_text(
                            conversation_store,
                            session_id,
                            text_acc,
                            current_response_id,
                            current_model,
                        )

                    conv_item = _extract_persistent_item_from_sse(
                        event,
                        response_id=_persist_rid,
                    )
                    if conv_item is not None:
                        await _relay_persist(
                            conversation_store,
                            session_id,
                            conv_item,
                        )

                    # On ANY terminal event (not just completed), persist the
                    # final text segment: narration streamed before a failure /
                    # cancel must survive reload too, ordered BEFORE the error
                    # item below and before the publish pops the in-flight
                    # replay entry (flush → publish keeps reload == live).
                    # NB: fenced deltas never reached text_acc (the fence's
                    # continue precedes accumulation), so a post-Stop flush
                    # carries pre-stop narration only.
                    if evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                        _resp_obj = event.get("response")
                        _resp_model = (
                            _resp_obj.get("model") if isinstance(_resp_obj, dict) else None
                        )
                        _final_model = (
                            _resp_model
                            if isinstance(_resp_model, str) and _resp_model
                            else current_model
                        )
                        await _flush_relay_text(
                            conversation_store,
                            session_id,
                            text_acc,
                            current_response_id,
                            _final_model,
                        )

                    error_item = _error_item_from_sse(
                        event,
                        response_id=current_response_id,
                    )
                    if error_item is not None:
                        await _relay_persist_error_once(
                            conversation_store,
                            session_id,
                            error_item,
                        )

                    # Persist resource lifecycle events
                    # (session.resource.created / .deleted) emitted by
                    # agent-tool terminal launches/closes so reconnecting
                    # clients rediscover the resource in the snapshot.
                    # The live publish below already updates connected
                    # clients.
                    resource_item = _resource_event_item_from_sse(session_id, event)
                    if resource_item is not None:
                        resource_data = resource_item.data
                        await _relay_persist(
                            conversation_store,
                            session_id,
                            resource_item,
                        )
                        # Self-heal the spin-up flag: a created terminal is
                        # authoritative proof the session is no longer
                        # "starting up", so clear it even if the runner's
                        # auto-create finally was skipped (e.g. hard kill
                        # between launch and clear). Only fire on a real
                        # state change to avoid redundant stream traffic.
                        if (
                            isinstance(resource_data, ResourceEventData)
                            and resource_data.event_type == "session.resource.created"
                            and resource_data.resource_type == "terminal"
                            and _session_terminal_pending_cache.get(session_id, False)
                        ):
                            _publish_terminal_pending(session_id, False)

                    # Intelligent-model-router decision emitted by the runner's
                    # cost advisor at turn start. Persist as a display-only
                    # transcript item (arrival order = BEFORE the assistant
                    # output), then re-publish the live event carrying the
                    # store-assigned id so the live chip and a turn-start
                    # snapshot refetch dedup by the same id. Handled
                    # exclusively here (persist + publish + continue) so the
                    # raw, id-less runner event is not also forwarded below.
                    routing_item = _routing_decision_item_from_sse(event)
                    if routing_item is not None:
                        # Persist failure must NOT suppress the live chip
                        # (the owner's hard requirement: the pick shows the
                        # moment the turn starts). On a store error, log and
                        # still publish the live event — id-less, so a later
                        # snapshot can't dedup it, but a missing reload chip
                        # beats no chip at all.
                        try:
                            persisted = await asyncio.to_thread(
                                conversation_store.append, session_id, [routing_item]
                            )
                            _persisted_id: str | None = persisted[0].id if persisted else None
                        except Exception:  # noqa: BLE001
                            _logger.exception(
                                "Relay: routing_decision persist failed for session=%s; "
                                "publishing the live chip without a durable id",
                                session_id,
                            )
                            _persisted_id = None
                        session_stream.publish(
                            session_id,
                            {
                                **event,
                                "item": {**event["item"], "id": _persisted_id},
                            },
                        )
                        continue

                    # Accumulate LLM token usage from the harness
                    # response so policy callables can read
                    # event["context"]["usage"]["total_cost_usd"].
                    if evt_type == "response.completed":
                        # Persist the turn's usage (cost + token buckets) so
                        # policy callables can read
                        # event["context"]["usage"]["total_cost_usd"] and the
                        # subtree roll-up below sees the new totals.
                        _accumulate_session_usage(
                            event.get("response", {}),
                            session_id,
                            conversation_store,
                        )
                        # Push the server-computed cost AND token breakdown
                        # to the web client's session indicator, rolled up
                        # over the spawn subtree. The session's own event
                        # carries its SUBTREE total (this conversation + its
                        # sub-agents), and each ancestor gets its own subtree
                        # total on its own stream — so a supervisor's badge
                        # includes its sub-agents and a parent updates live
                        # when a relay sub-agent spends. Mirrors the native
                        # path (_persist_external_session_usage); the roll-up
                        # was wired for native only, but relay agents (e.g.
                        # claude-sdk) need it too. Cost is included only when
                        # priced; the token breakdown rides along whenever any
                        # bucket is recorded (so an unpriced session still
                        # surfaces tokens). context_tokens/window already ride
                        # on the response.completed event. Threaded: store
                        # reads + SSE fan-out.
                        _subtree_usage = await asyncio.to_thread(
                            load_session_usage,
                            session_id,
                            conversation_store,
                        )
                        _subtree_cost = _priced_cost_for_display(_subtree_usage)
                        _usage_by_model = _usage_by_model_for_display(_subtree_usage)
                        if _subtree_cost is not None or _usage_by_model is not None:
                            _usage_payload: dict[str, Any] = {
                                "type": "session.usage",
                                "conversation_id": session_id,
                            }
                            if _subtree_cost is not None:
                                _usage_payload["total_cost_usd"] = _subtree_cost
                            if _usage_by_model is not None:
                                _usage_payload["usage_by_model"] = _usage_by_model
                            session_stream.publish(
                                session_id,
                                SessionUsageEvent(**_usage_payload).model_dump(exclude_none=True),
                            )
                            await asyncio.to_thread(
                                _publish_subtree_cost_to_ancestors,
                                conversation_store,
                                session_id,
                            )

                    # Reset the turn-scoped response_id on any
                    # terminal event so it doesn't leak to the
                    # next turn.
                    if evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                        current_response_id = None

                    # Patch the live event's response_id for
                    # function_call_output items whose call_id maps
                    # to a known function_call response_id. This
                    # ensures the web UI's block stream pairs the
                    # tool result with its call in the same bubble.
                    if (
                        evt_type == "response.output_item.done"
                        and isinstance(event.get("item"), dict)
                        and event["item"].get("type") == "function_call_output"
                    ):
                        _live_cid = event["item"].get("call_id")
                        if isinstance(_live_cid, str) and _live_cid in tool_call_response_ids:
                            event = {
                                **event,
                                "item": {
                                    **event["item"],
                                    "response_id": tool_call_response_ids[_live_cid],
                                },
                            }
                    if evt_type == "response.elicitation_request":
                        session_stream.publish(session_id, event)
                        await asyncio.to_thread(
                            _publish_elicitation_request_to_ancestors,
                            conversation_store,
                            session_id,
                            event,
                        )
                        continue
                    if evt_type == "response.elicitation_resolved":
                        session_stream.publish(session_id, event)
                        elicitation_id = event.get("elicitation_id")
                        if isinstance(elicitation_id, str) and elicitation_id:
                            await asyncio.to_thread(
                                _publish_elicitation_resolved_to_ancestors,
                                conversation_store,
                                session_id,
                                elicitation_id,
                            )
                        continue
                    session_stream.publish(session_id, event)

    except (httpx.HTTPError, ConnectionError):
        # WSTunnelTransport raises bare ConnectionError on tunnel
        # close; treat the same as HTTPError so the task exits
        # gracefully instead of leaving an unretrieved exception.
        _logger.warning(
            "Relay: runner transport lost for session=%s",
            session_id,
            exc_info=True,
        )
        if session_id in _intentional_stop_sessions:
            # User clicked Stop: the Stop handler brought this runner's tunnel
            # down on purpose (see _stop_session_host_runner), so the drop is
            # expected — not a failure. Publish a quiet idle and clear any error
            # label so the chat and sidebar settle to a stopped state instead of
            # rendering "Error · runner_disconnected". One-shot: discard the
            # marker so a genuine later disconnect surfaces normally.
            _intentional_stop_sessions.discard(session_id)
            _publish_status(session_id, "idle")
            await _persist_session_status_error_labels(
                session_id,
                None,
                conversation_store,
            )
        else:
            # Publish a failed status so the client's SSE stream sees a
            # clean error event instead of silent truncation (#1114).
            disconnect_error = ErrorDetail(
                code="runner_disconnected",
                message="Runner disconnected unexpectedly.",
            )
            _publish_status(session_id, "failed", disconnect_error)
            # Persist the disconnect cause as durable labels so the
            # distinction survives into snapshots and child-session
            # summaries. Without this the relay-fed cache only carries a
            # generic ``failed`` and ``last_task_error`` is dropped, leaving
            # the UI unable to tell a benign runner disconnect from a real
            # task failure (Option B: render a "Disconnected" pill, not the
            # red "Failed" pill). Cleared on the next ``running`` edge by the
            # session.status handler, exactly like other failure labels.
            await _persist_session_status_error_labels(
                session_id,
                disconnect_error,
                conversation_store,
            )
    except asyncio.CancelledError:
        raise
    finally:
        _logger.info("Relay: task exiting for session=%s", session_id)
        # Drop any in-flight assistant-text entry so a relay that exits
        # WITHOUT a terminal turn event (runner death / tunnel drop
        # mid-turn, or a rebind cancellation) can't strand it forever.
        # Normal turn-ends already clear via record_publish.
        inflight_text.discard(session_id)
        # The intentional-stop marker is consumed by the disconnect handler
        # above on the expected path; discard it here too so a relay that
        # exits some other way (clean [DONE], rebind cancellation) can't
        # leave a stale marker to swallow a later genuine disconnect on the
        # reused per-session relay task.
        _intentional_stop_sessions.discard(session_id)
        # Relay ended (runner dropped/rebound): re-discover runner-backed
        # snapshot overlays next time. Cancel in-flight fetches so they can't
        # land stale values from the dead runner; the model catalog is only
        # marked stale so the picker keeps serving it while the session sleeps.
        _invalidate_runner_backed_snapshot_state(
            session_id, cancel_inflight=True, drop_model_options=False
        )


def _ensure_runner_relay(
    session_id: str,
    runner_id: str | None,
    runner_client: httpx.AsyncClient | None,
    conversation_store: ConversationStore | None = None,
) -> _RelayHandle | None:
    """
    Start (or replace) the SSE relay for ``session_id``.

    No-op when a healthy relay is already bound to ``runner_id``.
    When the bound runner changes (last-write-wins PATCH-rebind),
    the stale relay is cancelled and a fresh one is created
    against the new runner.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_id: Runner id the new relay subscribes to,
        e.g. ``"runner_abc123"``. ``None`` skips relay
        (in-process path with no runner binding).
    :param runner_client: HTTP client pointed at ``runner_id``.
        ``None`` skips relay.
    :param conversation_store: Store for persisting items from
        the runner's SSE stream. ``None`` disables persistence.
    :returns: The active relay handle, or ``None`` when no runner is
        bound.
    """
    if runner_client is None or runner_id is None:
        _logger.info(
            "Relay: skipping for session=%s (runner_client=%s, runner_id=%s)",
            session_id,
            runner_client is not None,
            runner_id,
        )
        return None
    existing = _runner_relay_tasks.get(session_id)
    if existing is not None:
        if existing.runner_id == runner_id and not existing.task.done():
            _logger.info("Relay: reusing existing for session=%s runner=%s", session_id, runner_id)
            return existing  # same runner, healthy task
        _logger.info(
            "Relay: replacing stale for session=%s (old_runner=%s done=%s)",
            session_id,
            existing.runner_id,
            existing.task.done(),
        )
        if not existing.task.done():
            existing.task.cancel()  # stale binding; replace
    else:
        _logger.info("Relay: creating new for session=%s runner=%s", session_id, runner_id)
    ready = asyncio.Event()
    # Runtime callers always supply a store. ``None`` is retained for
    # heartbeat-only relay readiness tests that never emit persistable frames.
    relay_store = cast(ConversationStore, conversation_store)
    task = asyncio.create_task(
        _relay_runner_stream(
            session_id,
            runner_client,
            relay_store,
            ready,
        ),
        name=f"runner-relay-{session_id}",
    )
    handle = _RelayHandle(runner_id=runner_id, task=task, ready=ready)
    _runner_relay_tasks[session_id] = handle

    def _on_done(t: asyncio.Task[None]) -> None:
        # Clear our slot only if it still holds this task — a
        # later rebind may have replaced us.
        current = _runner_relay_tasks.get(session_id)
        if current is not None and current.task is t:
            _runner_relay_tasks.pop(session_id, None)

    task.add_done_callback(_on_done)
    return handle


async def _ensure_runner_relay_ready(*args: Any, **kwargs: Any) -> _RelayHandle | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._ensure_runner_relay_ready(*args, **kwargs)


async def _ensure_runner_relay_ready_impl(
    session_id: str,
    runner_id: str | None,
    runner_client: httpx.AsyncClient | None,
    conversation_store: ConversationStore | None = None,
) -> _RelayHandle | None:
    """
    Start the runner SSE relay and wait for its subscription ack.

    The runner stream has no replay buffer. For item events, Omnigent must
    subscribe to runner output before it forwards the input event; a
    fast harness can otherwise complete before Omnigent is listening, leaving
    the user with an apparently successful empty response.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_id: Runner id the relay should bind to, e.g.
        ``"runner_abc123"``. ``None`` skips relay setup.
    :param runner_client: HTTP client pointed at ``runner_id``.
        ``None`` skips relay setup.
    :param conversation_store: Store for persisting relayed items.
    :returns: The active relay handle, or ``None`` when no runner is
        bound.
    :raises OmnigentError: If the relay cannot observe the
        runner stream's ready heartbeat before the timeout.
    """
    handle = _ensure_runner_relay(
        session_id,
        runner_id,
        runner_client,
        conversation_store,
    )
    if handle is None or handle.ready.is_set():
        return handle
    try:
        await asyncio.wait_for(
            handle.ready.wait(),
            timeout=_RUNNER_RELAY_READY_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        if handle.task.done():
            raise OmnigentError(
                "Runner stream relay exited before becoming ready",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc
        raise OmnigentError(
            "Timed out waiting for runner stream relay to subscribe",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    return handle


async def _register_policy_elicitation(
    session_id: str,
    result: PolicyResult,
    arguments_preview: str,
    conversation_store: ConversationStore,
) -> str:
    """
    Publish an elicitation request event on the session stream.

    Approval state lives on the runner (in-memory
    ``_pending_approvals`` dict). The server just publishes the
    ``response.elicitation_request`` SSE event so the client
    sees the approval prompt, and returns the elicitation_id
    so the runner can key its Future on it.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param result: The :class:`PolicyResult` with action=ASK,
        carrying the reason and deciding_policy fields.
    :param arguments_preview: Truncated argument string for
        the elicitation UI preview (max ~1024 chars).
    :param conversation_store: Store used to mirror child-session
        prompts into ancestor streams.
    :returns: The generated elicitation id,
        e.g. ``"elicit_a1b2c3..."``.
    """
    elicitation_id = f"elicit_{secrets.token_hex(16)}"
    elicitation = ElicitationRequest(
        message=result.reason or "Approval required",
        requested_schema={},
        phase=Phase.TOOL_CALL.value,
        policy_names=result.deciding_policies or ["unknown"],
        content_preview=arguments_preview[:1024],
    )
    # Approval state lives on the runner (in-memory
    # _pending_approvals dict of elicitation_id → Future).
    # The server just publishes the elicitation SSE event and
    # returns the elicitation_id. The runner parks on the
    # Future; the client's approval event is forwarded to the
    # runner which resolves it. No server-side state needed.
    _elicit_event = build_elicitation_request_event(
        elicitation_id, elicitation, session_id=session_id
    )
    session_stream.publish(session_id, _elicit_event)
    await asyncio.to_thread(
        _publish_elicitation_request_to_ancestors,
        conversation_store,
        session_id,
        _elicit_event,
    )
    return elicitation_id


async def _evaluate_tool_call_policy(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    _runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """
    Evaluate a tool call against TOOL_CALL phase policy rules.

    Pure evaluation — does NOT persist the event. Returns
    ``None`` on ALLOW. Returns a verdict dict on DENY or ASK.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``function_call`` event with
        ``evaluate_policy: true``.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW (fall through). Verdict dict
        on DENY/ASK.
    """

    tool_name = body.data.get("name")
    if not tool_name or not isinstance(tool_name, str):
        raise OmnigentError(
            "function_call event with evaluate_policy requires a non-empty 'name' field in data",
            code=ErrorCode.INVALID_INPUT,
        )
    arguments_str = body.data.get("arguments", "{}")

    # Resolve agent spec + build engine off the event loop (blocking
    # DB/IO). Tool-call policy always evaluates (no guardrails skip).
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store, conv
    )

    try:
        args_payload = json.loads(arguments_str)
    except (ValueError, TypeError):
        args_payload = arguments_str

    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": tool_name, "arguments": args_payload},
        tool_name=tool_name,
        actor=actor,
    )
    result = await engine.evaluate(ctx)

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    if result.action == PolicyAction.DENY:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return {
            "verdict": "deny",
            "reason": result.reason or "Denied by policy",
        }

    # ASK — publish elicitation event. Approval state lives
    # on the runner (_pending_approvals dict).
    elicitation_id = await _register_policy_elicitation(
        session_id=session_id,
        result=result,
        arguments_preview=arguments_str,
        conversation_store=conversation_store,
    )
    # The deciding policy's writes (e.g. a cost-budget checkpoint via
    # ``state_updates``) must land ONLY on approve. This relay path returns
    # ``pending`` and the verdict arrives later off-request, so stash them to
    # apply when the matching ``approval`` resolves with accept (see
    # _apply_pending_policy_ask_writes). The native path applies these inline
    # in _hold_native_ask_gate; without this, a relay/non-native session's
    # checkpoint is never recorded and the ASK re-prompts every tool call.
    # Always store an entry even when there are no deferred writes —
    # the MCP retry path checks the pending map to verify the
    # elicitation was genuinely issued by the server.
    _pending_policy_ask_writes[elicitation_id] = _PendingPolicyAskWrites(
        state_updates=result.state_updates,
        set_labels=result.set_labels,
    )
    return {
        "verdict": "pending",
        "elicitation_id": elicitation_id,
        # Spec-resolved approval window; the runner's park honors it.
        "ask_timeout": resolve_ask_timeout(engine, result),
    }


async def _evaluate_input_policy(
    request: Request,
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    _runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
) -> dict[str, Any] | None:
    """
    Evaluate a user message against REQUEST (input) phase policy rules.

    Does not persist the event. On ALLOW returns ``None`` (caller
    forwards the message). On DENY returns a verdict dict (caller does
    NOT forward). On ASK this function **parks for human approval**
    before returning: unlike the ``tool_call`` phase — where the runner
    parks via ``wait_for_user_approval`` — the REQUEST phase has no
    runner in the loop yet (the message hasn't been forwarded), so the
    approval gate must live here. It reuses :func:`_hold_native_ask_gate`
    (the same server-side park the native ``tool_call`` gate uses):
    accept collapses to ALLOW (``None``, forward the message), while
    decline / timeout collapses to a DENY verdict (fail-closed).

    :param request: The active FastAPI request, threaded to
        :func:`_hold_native_ask_gate` for upstream-disconnect detection
        while parked on an ASK.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``message`` event.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param _runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW or an approved ASK (fall through to the
        forward path). A verdict dict ``{"verdict": "deny", "reason":
        ...}`` on DENY or a declined / timed-out ASK.
    """

    user_text = _extract_user_text_from_event(body)
    # A message with a text attachment (e.g. an uploaded CSV) may carry no typed
    # text — those ``input_file`` blocks are decoded below and must not be skipped
    # here. ``content`` being a list is the cheap precondition for that; the
    # actual (blocking) decode is deferred until after the policy-skip check.
    raw_content_blocks = body.data.get("content")
    content_blocks = (
        [block for block in raw_content_blocks if isinstance(block, dict)]
        if isinstance(raw_content_blocks, list)
        else []
    )
    has_content_blocks = bool(content_blocks)
    if not user_text and not has_content_blocks:
        return None

    # Resolve the agent spec off the event loop (blocking DB + cold-cache
    # bundle fetch). Spec only, so the cheap skip check below runs before
    # the more expensive engine build.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    # Skip only when there are no agent guardrails AND no server-wide
    # default policies AND no session policies. Without this, default/
    # session policies (e.g. deny_pii_in_llm_request added via the UI)
    # are silently skipped for agents without a guardrails: YAML block.
    if not spec.guardrails and not get_caps().default_policies and get_policy_store() is None:
        return None

    # Text-like attachments (e.g. an uploaded CSV) arrive as ``input_file`` blocks
    # base64-inlined straight to the model — they are NOT part of ``user_text`` and
    # would otherwise reach the LLM unscanned. Decode their text here so
    # request-phase policies (e.g. deny_pii_in_llm_request) scan the attachment
    # content too. Deferred until after the skip check so a no-policy agent never
    # pays the artifact fetch. Best-effort; only runs when stores are wired.
    attachments: list[dict[str, str]] = []
    if has_content_blocks and file_store is not None and artifact_store is not None:
        from omnigent.runtime.content_resolver import extract_text_attachments

        attachments = await asyncio.to_thread(
            extract_text_attachments,
            content_blocks,
            file_store,
            artifact_store,
            session_id=session_id,
        )
    if not user_text and not attachments:
        return None
    # Structured request content ({"user_content", "attachments"}) so policies
    # can reason about attachments per-file instead of a merged string.
    request_content = {"user_content": user_text, "attachments": attachments}

    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store, conv
    )
    ctx = EvaluationContext(
        phase=Phase.REQUEST,
        content=request_content,
        tool_name=None,
        actor=actor,
    )
    result = await engine.evaluate(ctx)

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    if result.action == PolicyAction.DENY:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return {
            "verdict": "deny",
            "reason": result.reason or "Denied by policy",
        }

    # ASK — park server-side for human approval. The REQUEST phase has no
    # runner-side approval round-trip (the message has not been forwarded to
    # a runner yet, so nothing would park on a "pending" verdict — it would
    # collapse to a silent deny). Hold the gate here exactly like the native
    # tool_call path: _hold_native_ask_gate publishes the approval card,
    # awaits the human verdict on a server-side Future, and applies the
    # deciding policy's writes only on accept (POLICIES.md §7.2). Accept ->
    # ALLOW (fall through to forward the message); decline / timeout ->
    # DENY (fail-closed).
    try:
        approved = await _hold_native_ask_gate(
            request,
            session_id=session_id,
            phase=Phase.REQUEST,
            data=body.data,
            engine=engine,
            result=result,
            conversation_store=conversation_store,
        )
    except ElicitationDeclinedError as exc:
        return {
            "verdict": "deny",
            "reason": exc.args[0] or "Denied by policy",
        }
    if approved:
        return None
    return {
        "verdict": "deny",
        "reason": result.reason or "Denied by policy",
    }


async def _wake_parent_for_blocked_child(
    parent_id: str,
    child: Conversation,
    notice: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> bool:
    """
    Deliver a parent-wake notice when a sub-agent blocks on an approval.

    Posts the ``[System: …]`` notice as a synthetic user message to the
    parent's ``POST /v1/sessions/{id}/events`` — the same path the runner's
    terminal-completion wake uses, so it starts a continuation turn (idle
    parent) or coalesces with pending input (busy parent). Best-effort: a
    missing parent, missing runner, or transport error is logged and swallowed
    (a dropped wake is no worse than the pre-fix no-wake baseline), but the
    *outcome* is reported back so the notifier can release its per-block
    debounce and let a later publish retry rather than silencing the block.

    :param parent_id: Parent session id, e.g. ``\"conv_parent123\"``.
    :param child: The blocked child :class:`Conversation`; used only for its
        label/id in the notice and logs.
    :param notice: The ``[System: …]`` text to inject into the parent.
    :param conversation_store: Used to load the parent :class:`Conversation`
        and persist the synthetic user message item.
    :param runner_router: Router used to resolve the parent's bound
        runner. ``None`` in in-process setups (the runtime singleton is
        consulted as a fallback).
    :returns: ``True`` when the notice was dispatched to the parent's runner;
        ``False`` when delivery could not happen (parent gone, no runner bound,
        or the forward raised a transport error).
    """
    parent_conv = await asyncio.to_thread(conversation_store.get_conversation, parent_id)
    if parent_conv is None:
        # Parent vanished between publish and wake (cascading-delete race).
        _logger.debug(
            "subagent block notifier: parent %s missing; dropping wake for %s",
            parent_id,
            child.id,
        )
        return False
    runner_client = await _get_runner_client(parent_id, runner_router)
    if runner_client is None:
        # WARNING (not DEBUG): an unbound parent is the transient-miss case the
        # notifier retries — surface it rather than burying it as routine.
        _logger.warning(
            "subagent block notifier: no runner bound for parent %s; dropping wake for %s",
            parent_id,
            child.id,
        )
        return False
    # Ensure the parent's SSE relay is live so the wake turn's output is
    # persisted (parity with post_event).
    _ensure_runner_relay(
        parent_id,
        parent_conv.runner_id,
        runner_client,
        conversation_store,
    )
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": notice}],
        },
    )
    try:
        # None args: a system notice carries no agent/files/artifacts; the runner
        # recomputes has_mcp_servers from the parent's cached spec.
        await _dispatch_session_event_to_runner(
            parent_id,
            parent_conv,
            body,
            conversation_store,
            runner_client,
            agent_name=None,
            file_store=None,
            artifact_store=None,
            runner_router=runner_router,
        )
    except (httpx.HTTPError, OmnigentError):
        _logger.warning(
            "subagent block wake POST failed for parent=%s child=%s",
            parent_id,
            child.id,
            exc_info=True,
        )
        return False
    return True


def configure_subagent_block_notifier(
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> Callable[[], None]:
    """
    Install the parent-wake notifier on the elicitation publish path.

    Wires :class:`SubagentBlockNotifier` into
    :mod:`omnigent.runtime.pending_elicitations` so a sub-agent that
    blocks on an approval immediately wakes its immediate parent through
    the same ``/events`` ingest path the runner-side terminal-completion
    wake already uses (see
    :func:`_wake_parent_for_blocked_child`). Top-level sessions (no
    parent) are no-ops; multi-user safety is inherent because the wake
    is delivered to the recorded ``parent_conversation_id`` only, never
    fanned out to collaborators or unrelated sessions.

    :param conversation_store: Store used to resolve a child's
        ``parent_conversation_id`` and to persist the wake message.
    :param runner_router: Router used by the wake to reach the parent's
        bound runner. ``None`` in in-process setups.
    :returns: A callable that uninstalls the observer and cancels any
        in-flight wake futures. Call from the lifespan teardown.
    """
    from omnigent.runtime import pending_elicitations as _pending_elicitations
    from omnigent.runtime.subagent_block_notifier import SubagentBlockNotifier

    loop = asyncio.get_running_loop()

    async def _wake_dispatch(parent_id: str, child: Conversation, notice: str) -> bool:
        """
        Deliver one wake notice (the notifier's injected dispatch).

        :param parent_id: Parent session id.
        :param child: The blocked child :class:`Conversation`.
        :param notice: Pre-formatted ``[System: …]`` text.
        :returns: ``True`` when the notice reached the parent's runner,
            ``False`` when it could not be delivered (so the notifier
            releases the debounce and a re-publish can retry).
        """
        return await _wake_parent_for_blocked_child(
            parent_id,
            child,
            notice,
            conversation_store=conversation_store,
            runner_router=runner_router,
        )

    notifier = SubagentBlockNotifier(
        conversation_store=conversation_store,
        wake_dispatch=_wake_dispatch,
        loop=loop,
    )
    _pending_elicitations.set_elicitation_observer(notifier.observe)

    def _uninstall() -> None:
        """Remove the observer and cancel any outstanding wake futures."""
        _pending_elicitations.set_elicitation_observer(None)
        notifier.close()

    return _uninstall


def _native_subagent_wrapper_labels(
    *,
    agent: Agent,
    sub_agent_name: str,
    agent_cache: AgentCache | None,
) -> dict[str, str]:
    """
    Resolve the terminal-first wrapper labels for a native-harness sub-agent.

    A sub-agent dispatched via ``sys_session_send`` whose own spec uses a
    native terminal harness (``claude-native`` / ``codex-native``) must
    render with the Chat/Terminal pill in the web UI, exactly like a
    top-level ``claude-native-ui`` / ``codex-native-ui`` wrapper session.
    The pill is gated on the conversation's ``omnigent.wrapper`` +
    ``omnigent.ui`` labels (see ``web`` ``TerminalFirstContext``), but
    the sub-agent create path never stamps them. This resolves the child
    sub-agent's spec from the parent bundle and returns the labels to stamp,
    or an empty dict when the sub-agent is not native (e.g. ``claude-sdk``).

    :param agent: The parent agent row, e.g. the ``polly`` orchestrator,
        whose bundle contains the sub-agent specs.
    :param sub_agent_name: The dispatched sub-agent's name, e.g.
        ``"claude_code"``.
    :param agent_cache: Cache for loading the parsed parent bundle. ``None``
        disables resolution (returns an empty dict).
    :returns: ``{wrapper_key: value, ui_key: "terminal"}`` for a native
        sub-agent, or ``{}`` when not native / not resolvable.
    """
    sub_spec = _resolve_subagent_spec(
        agent=agent,
        sub_agent_name=sub_agent_name,
        agent_cache=agent_cache,
    )
    if sub_spec is None:
        return {}
    return _native_subagent_wrapper_labels_from_spec(sub_spec)


# Host readiness values that mean the native CLI cannot launch. A routed
# session landing on a missing or unauthenticated binary would fail to open its
# terminal, so those harnesses are never offered to Smart Routing.
_NATIVE_UNAVAILABLE_READINESS: frozenset[object] = frozenset(
    {False, HARNESS_BINARY_MISSING, HARNESS_NEEDS_AUTH, HARNESS_VERSION_TOO_LOW}
)


def _installed_native_harnesses(host: Host | None) -> list[str]:
    """Return the Smart Routing native harnesses ready to launch on *host*.

    Reads the host's ``configured_harnesses`` readiness map — the same signal
    the picker uses to gate its "needs setup" rows. Fails open (every
    candidate) when the host or its map is absent, so a host that reports
    nothing does not silently disable native Smart Routing.

    :param host: The session's target host, or ``None`` (e.g. a sandbox).
    :returns: Candidate harness ids in :data:`AUTO_NATIVE_ROUTING_HARNESSES`
        order, restricted to the ones that can launch.
    """
    from omnigent.server.smart_routing import AUTO_NATIVE_ROUTING_HARNESSES

    readiness = getattr(host, "configured_harnesses", None) if host is not None else None
    if not readiness:
        return list(AUTO_NATIVE_ROUTING_HARNESSES)
    return [
        harness
        for harness in AUTO_NATIVE_ROUTING_HARNESSES
        if harness in readiness and readiness[harness] not in _NATIVE_UNAVAILABLE_READINESS
    ]


def _ungatewayed_native_harnesses(host: Host | None, harnesses: Sequence[str]) -> list[str]:
    """Which of *harnesses* this host does not back with the workspace AI gateway.

    The external router's picks are gateway catalog ids, so a CLI pointed at
    Bedrock, a personal subscription, or any other provider cannot run one even
    with the binary installed — those harnesses are served by the built-in judge
    instead. Reads the map the host reported on its connect handshake; unknown
    (nothing reported to this replica yet, or no host bound) counts as backed.

    :param host: The session's target host, or ``None`` (e.g. a sandbox).
    :param harnesses: Harness ids to check, e.g. ``("claude-native",)``.
    :returns: The not-backed ids, in the order given.
    """
    from omnigent.gateway_inference import not_gateway_backed
    from omnigent.server.routing_backend import reported_gateway_inference

    return not_gateway_backed(reported_gateway_inference(host), harnesses)


def _gateway_backed(host: Host | None, harnesses: Sequence[str]) -> bool:
    """Whether the external router can serve a call over *harnesses* on *host*.

    :param host: The session's target host, or ``None``.
    :param harnesses: Every harness family the call may land on.
    :returns: ``True`` unless the host explicitly reports one as not backed.
    """
    return not _ungatewayed_native_harnesses(host, harnesses)


def _oss_routing_available(caps: Any = None) -> bool:  # type: ignore[explicit-any]
    """Whether the built-in judge can answer for a harness off the gateway.

    The fallback source: with it configured, an ungatewayed pane still routes
    (with a smaller candidate set), so the refusals below only fire when this
    is unavailable too.

    :param caps: A ``RuntimeCaps``-shaped object. ``None`` reads the globals.
    :returns: ``True`` when a built-in routing client is configured.
    """
    from omnigent.server.routing_backend import backends_from_caps

    if caps is None:
        try:
            from omnigent.runtime._globals import _caps
        except ImportError:
            return False
        caps = _caps
    return backends_from_caps(caps).local is not None


def _external_router_usable(caps: Any = None) -> bool:  # type: ignore[explicit-any]
    """Whether the external ``routes:select`` router can still answer here.

    False for a deployment that configures none, and for one whose router has
    latched a permanent failure (its workspace has no routing API) — the
    surfaces that need a task_v1 menu then take their judge-only shape.

    :param caps: A ``RuntimeCaps``-shaped object. ``None`` reads the globals.
    :returns: ``True`` when an external routing client can serve a call.
    """
    from omnigent.server.routing_backend import backends_from_caps, usable_external

    if caps is None:
        try:
            from omnigent.runtime._globals import _caps
        except ImportError:
            return False
        caps = _caps
    return usable_external(backends_from_caps(caps)) is not None


def _judge_only_harness_note(harness: str, verdict: dict[str, Any]) -> str:
    """Explain on the chip why only the model was routed.

    :param harness: The pane the create kept, e.g. ``"claude-native"``.
    :param verdict: The routing verdict whose rationale is being extended.
    :returns: The rationale to show, with the harness note appended.
    """
    rationale = str(verdict.get("rationale") or "").strip()
    note = (
        f"Harness routing needs the workspace router, which is unavailable here; "
        f"kept {harness} and routed the model with the built-in judge."
    )
    return f"{rationale} {note}".strip()


async def _session_routing_host(
    conv: Conversation,
    host_store: HostStore | None,
) -> Host | None:
    """Read the host a live session is bound to, for the gateway-backing check.

    Never raises and never blocks the turn: no host store, no ``host_id``, or a
    store error all read as "unknown", which counts as gateway-backed.

    :param conv: The session row.
    :param host_store: Persistent host registrations, or ``None``.
    :returns: The host row, or ``None`` when it cannot be read.
    """
    if host_store is None or conv.host_id is None:
        return None
    try:
        return await asyncio.to_thread(host_store.get_host, conv.host_id)
    except Exception:  # noqa: BLE001 — an unreadable host row is just unknown
        _logger.debug(
            "routing: could not read host %r for session=%s", conv.host_id, conv.id, exc_info=True
        )
        return None


async def _spawn_gateway_backed(
    request: Request,
    conv: Conversation,
    harnesses: Sequence[str],
) -> bool:
    """Whether the workspace router can serve a spawn over *harnesses*.

    :param request: Used to reach the app's host store.
    :param conv: The parent session row.
    :param harnesses: Every harness family the spawn may land on.
    :returns: ``True`` unless the host explicitly reports one as not backed.
    """
    host_store = getattr(request.app.state, "host_store", None)
    return _gateway_backed(await _session_routing_host(conv, host_store), harnesses)


def _harness_labels(harnesses: Sequence[str]) -> str:
    """Join *harnesses* into user-facing prose, e.g. ``"Claude Code and Codex"``.

    :param harnesses: Harness ids, e.g. ``("claude-native", "codex-native")``.
    :returns: The display names, comma-joined with a trailing "and".
    """
    labels = []
    for harness in harnesses:
        native = native_coding_agent_for_harness(harness)
        labels.append(native.display_name if native is not None else harness)
    if len(labels) <= 1:
        return labels[0] if labels else ""
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def _ungatewayed_auto_routing_error(ungatewayed: Sequence[str]) -> str:
    """Message for a top-level Smart Routing create no router can serve.

    Both arms are on the menu, so one arm off the gateway takes the AI Gateway's
    router off the table for the whole pick. That is only fatal when the server
    has no built-in router either — otherwise the built-in one answers.

    :param ungatewayed: The arms the host does not back, e.g.
        ``["codex-native"]``.
    :returns: One sentence naming the arms and the way out.
    """
    verb = "is" if len(ungatewayed) == 1 else "are"
    return (
        f"Smart Routing has no router available on this host: {_harness_labels(ungatewayed)} "
        f"{verb} not AI-Gateway-backed, so the workspace router's picks would not be "
        "reachable, and this server has no built-in routing model to fall back on. Pick a "
        "harness directly, configure a server `llm:` block, or point the harness at the "
        "workspace AI Gateway (`omnigent configure harnesses`)."
    )


def _ungatewayed_model_routing_error(harness: str) -> str:
    """Message for a routing-on create no router can serve.

    Only reached when the harness is off the AI Gateway AND the server has no
    built-in routing model — either one alone still routes.

    :param harness: The session's native harness, e.g. ``"codex-native"``.
    :returns: One sentence naming the harness and the way out.
    """
    return (
        f"Smart Routing has no router available for {harness} on this host: "
        f"{_harness_labels([harness])} is not AI-Gateway-backed, so the workspace router's "
        "picks would not be reachable from the pane, and this server has no built-in routing "
        'model to fall back on. Create the session without cost_control_mode_override="on", '
        "configure a server `llm:` block, or point the harness at the workspace AI Gateway "
        "(`omnigent configure harnesses`)."
    )


async def _reject_ungatewayed_model_routing(
    body: SessionCreateRequest,
    request: Request,
    user_id: str | None,
    agent: Agent,
    agent_cache: AgentCache | None,
) -> None:
    """Reject a routing-on create no router can serve.

    A pane off the AI Gateway cannot run the workspace router's picks, but the
    built-in judge names models from the pane's own catalog, so it can. This
    only refuses when neither source is available — otherwise the create
    proceeds and the built-in judge answers.

    Children and sub-agent sessions are routed by the spawn/turn paths in their
    parent's family, so they are left to those gates.

    :param body: The validated create request.
    :param request: Used to reach the app's host store.
    :param user_id: Authenticated caller, or ``None`` when auth is disabled.
    :param agent: The bound agent row.
    :param agent_cache: Cache for loading the agent's parsed spec.
    :returns: None when the create may proceed.
    :raises OmnigentError: 400 when the harness is not AI-Gateway-backed AND
        the server has no built-in routing model to fall back on.
    :raises HTTPException: 404/403 from resolving ``body.host_id``.
    """
    from omnigent.server.smart_routing import AUTO_NATIVE_ROUTING_HARNESSES

    if body.cost_control_mode_override != "on":
        return
    if body.parent_session_id is not None or body.sub_agent_name is not None:
        return
    harness = await asyncio.to_thread(
        _create_resolved_harness, agent, body.harness_override, agent_cache
    )
    if harness not in AUTO_NATIVE_ROUTING_HARNESSES:
        return
    # The built-in judge routes off the pane's own catalog, so it reaches an
    # ungatewayed harness the workspace router cannot. Check it before refusing.
    if _oss_routing_available():
        return
    host = await _routing_host_for_create(body, request, user_id)
    if not _ungatewayed_native_harnesses(host, (harness,)):
        return
    raise OmnigentError(_ungatewayed_model_routing_error(harness), code=ErrorCode.INVALID_INPUT)


# Per-harness budget for the pre-session host model-options round-trip. This
# sits on the session-create path, so it stays well under the picker's own
# 15s ceiling: a slow or silent host degrades to the static table rather than
# stalling the create.
_PRE_SESSION_MODEL_OPTIONS_TIMEOUT_S = 5.0


async def _host_model_options(
    host_conn: HostConnection,
    host_registry: HostRegistry,
    harness: str,
) -> list[str]:
    """Ask a host which models *harness* could launch with right now.

    The pre-launch preview the landing screen's model picker already reads,
    reused as a routing candidate list, plus every other id the harness's
    endpoint serves. The picker names only the newest model of each family, so
    on its own it would drop a frozen arm the workspace still serves
    (``claude-opus-4-8`` once ``claude-opus-5`` ships) and the router's pick
    would substitute onto a generation nobody asked for; a launch takes an
    exact id, so the wider set is genuinely launchable. Hosts answer for the
    harnesses that can resolve a catalog without a running CLI and reject the
    rest, so a failure here is expected and yields no candidates.

    :param host_conn: Live host connection to query.
    :param host_registry: Registry used to enqueue the outbound frame.
    :param harness: Native harness id, e.g. ``"claude-native"``.
    :returns: Model ids the host reported, picker rows first, or an empty list.
    """
    from omnigent.server.routes._host_model_options import request_host_model_options

    try:
        result = await request_host_model_options(
            host_registry=host_registry,
            host_conn=host_conn,
            harness=harness,
            timeout_s=_PRE_SESSION_MODEL_OPTIONS_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 — a timeout or host error just means no candidates
        _logger.debug("host model options unavailable for harness %r", harness, exc_info=True)
        return []
    if result.get("status") != "ok":
        return []
    options = result.get("models")
    models: list[str] = []
    if isinstance(options, list):
        for option in options:
            # Picker rows carry the launchable id in ``model``; ``id`` is the row key.
            raw = option.get("model") or option.get("id") if isinstance(option, dict) else None
            if isinstance(raw, str) and raw and raw not in models:
                models.append(raw)
    routable = result.get("routable_models")
    if isinstance(routable, list):
        for raw in routable:
            if isinstance(raw, str) and raw and raw not in models:
                models.append(raw)
    return models


async def _pre_session_model_catalog(
    request: Request,
    host: Host | None,
    harnesses: Sequence[str],
) -> dict[str, list[str]]:
    """Resolve a routing catalog for *harnesses* before any runner exists.

    A create routes with no session, so the live runner catalog
    (``fetch_runner_models``) is out of reach. The host is: it holds the CLIs and
    already resolves their pre-launch model options for the picker. Harnesses it
    cannot answer for are simply absent, and the static table covers them.

    :param request: Used to reach the app's host registry.
    :param host: The session's target host, or ``None`` (e.g. a sandbox).
    :param harnesses: Candidate native harness ids to ask about.
    :returns: ``{harness: model ids}`` for whatever the host answered.
    """
    host_registry = getattr(request.app.state, "host_registry", None)
    if host is None or host_registry is None:
        return {}
    host_conn = host_registry.get(host.host_id)
    if host_conn is None:
        return {}
    results = await asyncio.gather(
        *(_host_model_options(host_conn, host_registry, harness) for harness in harnesses)
    )
    return {harness: models for harness, models in zip(harnesses, results, strict=True) if models}


async def _routing_host_for_create(
    body: SessionCreateRequest,
    request: Request,
    user_id: str | None,
) -> Host | None:
    """Resolve the create's target host for routing, authorizing ownership first.

    Routing reads the host's harness readiness and pushes model-options frames
    over its live connection, so a foreign ``host_id`` must be rejected before
    anything is read from the host or landed in its owner's connection.

    :param body: The create request; ``host_id`` selects the host.
    :param request: Used to reach the app's host store.
    :param user_id: Authenticated caller, or ``None`` when auth is disabled.
    :returns: The owned host, or ``None`` when the create names none.
    :raises HTTPException: 404 if ``host_id`` is unknown, 403 if it belongs
        to another user.
    """
    from omnigent.server.routes._host_launch import resolve_host_owner

    host_store = getattr(request.app.state, "host_store", None)
    if host_store is None or body.host_id is None:
        return None
    return await asyncio.to_thread(
        resolve_host_owner,
        user_id=user_id,
        host_id=body.host_id,
        host_store=host_store,
    )


def _create_resolved_harness(
    agent: Agent,
    harness_override: str | None,
    agent_cache: AgentCache | None,
) -> str | None:
    """Resolve the harness a create will run on, before any row exists.

    :param agent: The bound agent row.
    :param harness_override: The request's raw ``harness_override``, if any.
    :param agent_cache: Cache for loading the agent's parsed spec.
    :returns: The canonical harness id, or ``None`` when it cannot be resolved.
    """
    from omnigent.harness_aliases import canonicalize_harness

    native_agent = native_coding_agent_for_agent_name(agent.name)
    if native_agent is not None:
        return native_agent.harness
    if harness_override:
        return canonicalize_harness(harness_override) or harness_override
    if agent_cache is None:
        return None
    try:
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        # An unloadable spec just means "harness unknown"; the create's own
        # validation reports it.
        _logger.debug("create-time routing: agent %r failed to load", agent.name, exc_info=True)
        return None
    return canonicalize_harness(_spec_harness(loaded.spec)) or None


def _fixed_native_routing_harness(
    body: SessionCreateRequest,
    agent: Agent,
    agent_cache: AgentCache | None,
) -> str | None:
    """The native harness whose model a create must route now, if any.

    A native terminal launches with the session row and its turns originate in
    the TUI, so the server never sees the first message pre-inference: a session
    pinned to claude-native / codex-native has to route its MODEL during the
    create or not at all. Everything else routes later — an SDK harness on its
    first turn through the server, the ``"auto"`` path on its own create branch.

    :param body: The validated create request.
    :param agent: The bound agent row.
    :param agent_cache: Cache for loading the agent's parsed spec.
    :returns: ``"claude-native"`` / ``"codex-native"`` when create-time model
        routing applies, else ``None``.
    """
    from omnigent.server.smart_routing import AUTO_NATIVE_ROUTING_HARNESSES

    if body.cost_control_mode_override != "on":
        return None
    if not (body.smart_routing_message or "").strip():
        return None
    # The auto path routes harness AND model on its own branch; a child or
    # sub-agent session is routed by the spawn/turn paths, which know the
    # parent's family. Neither may take this branch.
    if body.harness_override == "auto":
        return None
    if body.parent_session_id is not None or body.sub_agent_name is not None:
        return None
    # A client-pinned model wins over the router, exactly as it does per turn.
    if body.model_override is not None:
        return None
    harness = _create_resolved_harness(agent, body.harness_override, agent_cache)
    return harness if harness in AUTO_NATIVE_ROUTING_HARNESSES else None


def _spec_routes_its_own_harness(
    body: SessionCreateRequest,
    agent: Agent,
    agent_cache: AgentCache | None,
) -> bool:
    """Whether this create must hand the brain harness to the router.

    A spec pinning ``executor.config.harness`` also pins the family its
    sub-agents are routed within, so a two-headed agent loses the head that
    lives in the other family. ``smart_routing_harness: auto`` opts the spec out
    of its own pin for a Smart Routing session, taking the same ``"auto"``
    sentinel path the brain-harness picker offers by hand.

    Off unless Smart Routing is on for this create, so a spec carrying the key
    behaves exactly as before with routing off. Deliberately narrow otherwise:
    a client's explicit ``harness_override`` or ``model_override`` is the user's
    own pick and outranks the spec, and a child / named sub-agent create is
    routed by the spawn and turn paths (which read the parent's routing state),
    not here.

    :param body: The validated create request.
    :param agent: The bound agent row.
    :param agent_cache: Cache for loading the agent's parsed spec.
    :returns: ``True`` when ``harness_override`` must become ``"auto"``.
    :raises OmnigentError: ``invalid_input`` when the spec's opt-in value is
        not the ``"auto"`` sentinel, or sits on a non-omnigent executor type.
    """
    if body.cost_control_mode_override != "on":
        return False
    if body.harness_override is not None or body.model_override is not None:
        return False
    if body.parent_session_id is not None or body.sub_agent_name is not None:
        return False
    if agent_cache is None:
        return False
    try:
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        # An unloadable spec just means "no opt-in"; the create's own
        # validation reports it.
        _logger.debug("create-time routing: agent %r failed to load", agent.name, exc_info=True)
        return False
    return _validated_spec_smart_routing_harness(loaded.spec) is not None


def _spawn_pins_its_harness(
    body: SessionCreateRequest,
    agent: Agent,
    agent_cache: AgentCache | None,
) -> bool:
    """Whether this child create already names the harness the child runs on.

    Two pins count, and both come from outside the router: an explicit
    ``harness_override`` on the spawn, and a declared sub-agent whose spec
    carries its own harness (polly's ``pi`` / ``claude_code`` workers). Either
    way the child's CLI is decided before any message is routed, so handing the
    router the whole multi-harness catalog can only produce a verdict the pane
    will not honor.

    :param body: The validated create request.
    :param agent: The parent agent row whose bundle holds the sub-agent specs.
    :param agent_cache: Cache for loading the parsed parent bundle. ``None``
        cannot resolve a sub-agent spec, so only the explicit pin is seen.
    :returns: ``True`` when the child's harness is already chosen.
    """
    if body.harness_override is not None:
        return True
    if not body.sub_agent_name:
        return False
    from omnigent.model_catalog import spec_harness

    sub_spec = _resolve_subagent_spec(
        agent=agent,
        sub_agent_name=body.sub_agent_name,
        agent_cache=agent_cache,
    )
    return sub_spec is not None and spec_harness(sub_spec) is not None


async def _resolve_fixed_native_model_routing(
    body: SessionCreateRequest,
    request: Request,
    user_id: str | None,
    harness: str,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Route the model for a create already pinned to one native harness.

    Candidates come from the host's pre-launch catalog for that single harness
    (:func:`_pre_session_model_catalog`) — no runner exists yet — and the pick is
    constrained to the session's own harness, so routing can only change the
    model. Fails open: an unavailable router or a pick this harness cannot run
    yields no model and a rationale for the routing card.

    :param body: The create request; ``smart_routing_message`` carries the
        routing text and ``host_id`` selects whose CLI catalog is read.
    :param request: Used to reach the app's host store and host registry.
    :param user_id: Authenticated caller, or ``None`` when auth is disabled.
    :param harness: The session's native harness, e.g. ``"claude-native"``.
    :returns: ``(model, verdict, error)``; ``model`` and ``verdict`` are
        ``None`` when nothing should be pinned, and ``error`` then explains why.
    :raises HTTPException: 404 if ``host_id`` is unknown, 403 if it belongs
        to another user.
    """
    from omnigent.server.smart_routing import models_in_family, route_session_harness

    host = await _routing_host_for_create(body, request, user_id)
    # Off the gateway the built-in judge answers, and the static table's
    # ``databricks-*`` ids are unreachable — the host's pre-launch catalog is the
    # only provider-accurate candidate source.
    backed = _gateway_backed(host, (harness,))
    _harness, model, verdict, error = await route_session_harness(
        body.smart_routing_message or "",
        harness_candidates=(harness,),
        catalog=await _pre_session_model_catalog(request, host, (harness,)),
        gateway_backed=backed,
        allow_static_fallback=backed,
    )
    if model is None or verdict is None:
        return None, None, error or "Routing unavailable; using the harness default model."
    if not models_in_family(harness, [model]):
        # With one harness on offer the seam has nothing to redirect an
        # out-of-family pick onto, and it resolves to the only candidate. The
        # launch would pass a ``--model`` this CLI cannot run, so leave the
        # session on its default and say so on the card.
        return (
            None,
            None,
            f"Not applied: this {harness} session cannot run {model}.",
        )
    return model, verdict, None


async def _resolve_native_smart_routing(
    body: SessionCreateRequest,
    request: Request,
    user_id: str | None,
) -> tuple[str | None, str | None, dict[str, Any] | None, str | None]:
    """Route a top-level Smart Routing create onto a native terminal harness.

    The terminal launches as soon as the session row exists, so a native
    session's harness cannot wait for the first message event the way the
    bundle-agent auto path does. This routes ``body.smart_routing_message``
    over the host's installed native CLIs and returns the chosen native
    WRAPPER agent name, which the caller binds instead of the client's
    placeholder — from there the create is byte-identical to a normal native
    create, terminal launch and all, so nothing is launched twice.

    Falls back to the first installed candidate when routing is unavailable, so
    the session still lands on a terminal (with the CLI's own default model).
    Same candidate when only the built-in judge is configured: choosing between
    two panes is the workspace router's job, so a judge-only deployment routes
    the default pane's MODEL and says so on the chip.

    :param body: The create request; ``smart_routing_message`` carries the
        routing text and ``host_id`` selects whose CLIs are on offer.
    Candidate models come from the host's pre-launch catalog
    (:func:`_pre_session_model_catalog`) — no runner exists yet, so the live
    per-session catalog is out of reach and the static table is last resort.

    :param request: Used to reach the app's host store for readiness and the
        host registry for the pre-session model catalog.
    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``, or
        ``None`` when auth is disabled. Host ownership is authorized before
        anything is read from the host or pushed over its connection.
    :returns: ``(wrapper_agent_name, model, verdict, error)``.
        ``wrapper_agent_name`` is ``None`` only when no native CLI is
        installed; ``error`` explains a fallback for the routing card.
    :raises HTTPException: 404 if ``host_id`` is unknown, 403 if it belongs
        to another user.
    """
    from omnigent.server.smart_routing import (
        AUTO_NATIVE_ROUTING_HARNESSES,
        models_in_family,
        route_session_harness,
    )

    host = await _routing_host_for_create(body, request, user_id)
    # Both arms must be gateway-backed before the WORKSPACE router may choose
    # between them: an arm off the gateway cannot run its picks, and the pick is
    # made after the create commits, so there is no safe half-menu. That only
    # ends the create when the built-in judge is unavailable too — it names
    # models from each arm's own pre-launch catalog, which every arm can run.
    ungatewayed = _ungatewayed_native_harnesses(host, AUTO_NATIVE_ROUTING_HARNESSES)
    if ungatewayed and not _oss_routing_available():
        return None, None, None, _ungatewayed_auto_routing_error(ungatewayed)
    backed = not ungatewayed
    installed = _installed_native_harnesses(host)
    if not installed:
        return None, None, None, "No native CLI is installed on this host."

    # Choosing BETWEEN native panes belongs to the workspace router: its menu is
    # what ranks two CLIs against one task, and the pick is baked into the
    # terminal launch. With no external router usable, this create keeps the
    # default pane and routes only its model with the built-in judge — a normal
    # Smart Routing session, minus the harness half — instead of declining.
    judge_only = not _external_router_usable()
    candidates = (installed[0],) if judge_only else installed
    harness, model, verdict, error = await route_session_harness(
        body.smart_routing_message or "",
        harness_candidates=candidates,
        catalog=await _pre_session_model_catalog(request, host, candidates),
        gateway_backed=backed,
        allow_static_fallback=backed,
    )
    if judge_only:
        kept = native_coding_agent_for_harness(candidates[0])
        kept_name = kept.agent_name if kept is not None else None
        if model is None or verdict is None:
            return (
                kept_name,
                None,
                None,
                error or "Routing unavailable; using the default native harness.",
            )
        if not models_in_family(candidates[0], [model]):
            # One pane on offer, so the seam resolves any pick onto it — and an
            # out-of-family model would reach the launch as a ``--model`` this
            # CLI cannot run. Keep the pane, pin nothing, say why.
            return (
                kept_name,
                None,
                None,
                f"Not applied: this {candidates[0]} session cannot run {model}.",
            )
        return (
            kept_name,
            model,
            {**verdict, "rationale": _judge_only_harness_note(candidates[0], verdict)},
            None,
        )
    native_agent = native_coding_agent_for_harness(harness) if harness is not None else None
    if native_agent is None:
        # Routing unavailable (or it named something that is not a native
        # terminal) — land on the first installed CLI so the session still
        # opens a terminal; the CLI keeps its own default model.
        fallback = native_coding_agent_for_harness(installed[0])
        return (
            fallback.agent_name if fallback is not None else None,
            None,
            None,
            error or "Routing unavailable; using the default native harness.",
        )
    return native_agent.agent_name, model, verdict, None


async def _create_session_from_existing_agent(
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    body: SessionCreateRequest,
    request: Request,
    agent_cache: AgentCache | None = None,
    user_id: str | None = None,
    permission_store: PermissionStore | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    background_title_coordinator: BackgroundSessionTitleCoordinator | None = None,
) -> SessionResponse:
    """
    Create a session bound to an already-registered agent.

    This preserves the existing JSON ``POST /v1/sessions`` contract:
    clients that uploaded an agent separately still bind by durable
    ``agent_id`` and receive the full session snapshot.

    :param conversation_store: Store for conversation persistence.
    :param agent_store: Store for agent lookup by durable id.
    :param runner_router: Runner router used to validate any initial
        dispatch triggered by ``initial_items``.
    :param body: Validated JSON create request.
    :param agent_cache: Optional cache for loading parsed agent specs
        from bundles, used to populate ``llm_model`` and
        ``context_window`` in the response.
    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``. Used to authorize parent-session
        and agent ownership and enforce runner
        ownership on parent-session inheritance.
    :param permission_store: Permission store for session-access
        checks. Required for authorization of
        ``parent_session_id`` and session-scoped ``agent_id``.
    :param liveness_lookup: Optional session-scoped liveness lookup
        to populate ``SessionResponse.runner_online``.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references in ``initial_items`` before forwarding
        to the runner.
    :param artifact_store: Optional binary content store for the same.
    :returns: The newly created session snapshot.
    :raises OmnigentError: 404 if no agent matches ``body.agent_id``;
        403/404 if ``parent_session_id`` or session-scoped ``agent_id``
        fails authorization.
    """
    _reject_reserved_cost_control_label_seed(body.labels)
    _reject_server_reserved_label_seed(body.labels)

    agent = await validate_session_agent(
        user_id=user_id,
        agent_id=body.agent_id,
        agent_store=agent_store,
        permission_store=permission_store,
        conversation_store=conversation_store,
    )

    # Top-level Smart Routing: "auto" on a native wrapper agent means the client
    # picked Smart Routing with no bundle agent, and its ``agent_id`` is only a
    # placeholder. Route now (the terminal launches with the row, so there is no
    # first-message event to wait for) and rebind to the wrapper the router
    # chose; the rest of this function then runs as a plain native create.
    _native_smart_routing = (
        body.harness_override == "auto"
        and body.parent_session_id is None
        and native_coding_agent_for_agent_name(agent.name) is not None
    )
    _native_routed_model: str | None = None
    _native_routing_verdict: dict[str, Any] | None = None
    _native_routing_error: str | None = None
    if _native_smart_routing:
        (
            _routed_agent_name,
            _native_routed_model,
            _native_routing_verdict,
            _native_routing_error,
        ) = await _resolve_native_smart_routing(body, request, user_id)
        if _routed_agent_name is None:
            raise OmnigentError(
                _native_routing_error
                or "No native CLI is available for Smart Routing on this host.",
                code=ErrorCode.INVALID_INPUT,
            )
        _routed_agent_row = await asyncio.to_thread(agent_store.get_by_name, _routed_agent_name)
        if _routed_agent_row is None:
            raise OmnigentError(
                f"Native wrapper agent {_routed_agent_name!r} is not registered on this server.",
                code=ErrorCode.INTERNAL_ERROR,
            )
        agent = await validate_session_agent(
            user_id=user_id,
            agent_id=_routed_agent_row.id,
            agent_store=agent_store,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )

    # Routing on a native pane whose CLI is not AI-Gateway-backed can never
    # apply, so an explicit request for it is an error rather than a session
    # that silently ignores it. (The auto path checks both arms itself, above.)
    if not _native_smart_routing:
        await _reject_ungatewayed_model_routing(body, request, user_id, agent, agent_cache)

    # Fixed native harness + Smart Routing on: the harness is the caller's own
    # choice, so only the MODEL is routed — and it has to happen here, since the
    # terminal launches with the row and its turns originate in the TUI (the
    # server never sees the first message pre-inference). Fails open: no pin, a
    # decision card with the reason, and the CLI's default model.
    # A spec that hands its brain harness to the router (``smart_routing_harness:
    # auto``) takes the sentinel path below instead of any create-time pick: the
    # harness is not decided until the first message, so there is no single
    # harness whose model could be routed here.
    _spec_auto_brain = not _native_smart_routing and await asyncio.to_thread(
        _spec_routes_its_own_harness, body, agent, agent_cache
    )
    _fixed_native_harness = (
        None
        if _native_smart_routing or _spec_auto_brain
        else await asyncio.to_thread(_fixed_native_routing_harness, body, agent, agent_cache)
    )
    _fixed_routed_model: str | None = None
    _fixed_routing_verdict: dict[str, Any] | None = None
    _fixed_routing_error: str | None = None
    if _fixed_native_harness is not None:
        (
            _fixed_routed_model,
            _fixed_routing_verdict,
            _fixed_routing_error,
        ) = await _resolve_fixed_native_model_routing(
            body, request, user_id, _fixed_native_harness
        )

    # Authorize parent_session_id before inheriting anything.
    # The caller must own or have READ access to the parent session;
    # otherwise a forged parent link lets them inherit runner
    # bindings and establish a parent-child relationship with a
    # session they don't control.
    if body.parent_session_id is not None:
        await _require_access(
            user_id,
            body.parent_session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )

    # Reject an undeclared sub-agent before persisting the row. Downstream
    # spec swaps are all guarded by ``if ... is not None`` with no
    # ``else``, so a name the parent's spec never declares would leave the
    # parent spec/workdir/harness/instructions in place and boot the child
    # as a parent clone. Fail loud here instead.
    if body.sub_agent_name:
        await asyncio.to_thread(
            _require_declared_subagent,
            agent=agent,
            sub_agent_name=body.sub_agent_name,
            agent_cache=agent_cache,
        )

    # The persisted override reaches a native CLI as a ``--model`` argv
    # element at terminal launch, so reject shell-/flag-shaped values
    # before any row or worktree exists.
    model_override, reasoning_effort = validate_session_model_metadata(
        # Native Smart Routing bakes the routed model into the terminal launch;
        # the client sends none of its own on that path. A fixed-harness routed
        # create does the same, and only ever ran because the client sent none.
        model_override=(
            _native_routed_model
            if _native_smart_routing
            else _fixed_routed_model or body.model_override
        ),
        reasoning_effort=body.reasoning_effort,
    )

    # Validated before any row exists so a bad value never creates an
    # orphan session; None (unset) defers to the spec default.
    cost_control_mode_override = _validated_cost_control_mode_override(
        body.cost_control_mode_override
    )
    subagent_routing_override = _validated_subagent_routing_override(
        body.subagent_routing_override
    )

    # A child of an auto-harness parent whose subagent-routing switch is on is
    # routed regardless of the harness/model the orchestrator chose: force the
    # "auto" sentinel so the first-message routing path picks both harness and
    # model, ignoring the tool's ``agent``/``model`` args. Only applied to
    # omnigent-executor agents (auto requires a swappable brain harness).
    #
    # The gate is the parent's subagent-routing switch — the one routing knob the
    # in-session UI exposes — matching the gate the native-subagent hook applies
    # to in-harness spawns. A session that starts on Smart Routing is stamped
    # "on" at create; a legacy row that predates the stamp carries no switch, so
    # it reads as Default and its children are not forced.
    #
    # A child of a session pinned to one harness family (a plain codex or
    # claude session) must NOT be forced to auto: the sentinel would hand the
    # router the whole multi-harness catalog and stamp the auto marker on the
    # child, so a codex session ends up with claude children. Those children
    # keep the harness they were created with and are routed in-family (see
    # the child-routing call in ``_forward_event_to_runner``).
    #
    # Nor is a spawn that NAMED its harness forced: a declared sub-agent
    # (polly's ``pi``) and an explicit ``harness_override`` both pin the CLI the
    # child boots on, which the sentinel does not move — it only re-decides the
    # row. Forcing them handed the router the whole catalog for a pane already
    # committed to one harness, so a ``pi`` worker could be stamped with an
    # applied codex verdict while running pi, and a ``claude-native`` worker
    # lost its terminal labels (skipped for forced-auto children below). Those
    # children route in their own family instead.
    _force_auto_for_child = False
    _parent_for_routing: Conversation | None = None
    if body.parent_session_id is not None:
        _parent_for_routing = await asyncio.to_thread(
            conversation_store.get_conversation, body.parent_session_id
        )
        if (
            _parent_for_routing is not None
            and subagent_routing_enabled(_parent_for_routing.subagent_routing_override)
            and auto_harness_session(_parent_for_routing)
            and not await asyncio.to_thread(_spawn_pins_its_harness, body, agent, agent_cache)
        ):
            try:
                await asyncio.to_thread(_validated_harness_override_executor_type, agent)
                _force_auto_for_child = True
            except OmnigentError:
                # Non-omnigent agent (e.g. a native wrapper) — can't route
                # harness; leave the orchestrator's choice untouched.
                _force_auto_for_child = False
        # A pinned Smart Routing parent may only spawn inside its own model
        # family, so the child that could never be routed is refused here
        # rather than created and declined afterwards. Auto parents are
        # unaffected (the router owns their family) and so is a parent that
        # routes no spawns — neither resolves the child's harness at all.
        if _pinned_spawn_family(_parent_for_routing) is not None:
            _reject_out_of_family_child(
                _parent_for_routing,
                await asyncio.to_thread(
                    _create_resolved_harness, agent, body.harness_override, agent_cache
                ),
            )

    # A session that starts on Smart Routing routes the subagents it spawns.
    # Stamped here, once, so the spawn gate reads one explicit switch instead
    # of re-deriving it from this session's (or its parent's) routing state.
    # Only "on" is written: unset already means Default, so stamping "off" on
    # every ordinary create would grow the overrides blob and cost a write for
    # no change in behavior. An explicit caller value always wins.
    #
    # Stamped for every routed create, pinned harness included: a pinned codex
    # session's launch installs the same generated ``hooks.json`` spawn gate and
    # routed-spawn tool pre-approvals a routed claude one does (see
    # ``ensure_session_router_quietly``), so the switch has a consumer on every
    # family. What a pinned session does *not* get is cross-family picks — the
    # router is offered its own family only. A plain create is still left unset:
    # nothing routes there, so nothing reads the switch.
    if subagent_routing_override is None and (
        cost_control_mode_override == "on"
        or _native_smart_routing
        or (
            _parent_for_routing is not None
            and subagent_routing_enabled(_parent_for_routing.subagent_routing_override)
        )
    ):
        subagent_routing_override = "on"

    # Validated against the loaded spec (known harness + omnigent
    # executor type) before any row exists, mirroring the CLI's
    # --harness fail-loud rules.
    # "auto" defers harness + model selection to the first-message routing
    # path; validate executor type now but store the sentinel unchanged.
    harness_override: str | None
    if _native_smart_routing:
        # The harness is already decided and the agent rebound, so the row keeps
        # no sentinel: a native wrapper rejects harness_override, and leaving
        # "auto" behind would make the first message re-route an already-running
        # terminal. The auto marker is stamped as a label below instead.
        harness_override = None
    elif _force_auto_for_child or _spec_auto_brain or body.harness_override == "auto":
        await asyncio.to_thread(_validated_harness_override_executor_type, agent)
        harness_override = "auto"
        # Ignore any orchestrator-supplied model; routing picks it. (The spec
        # opt-in never reaches here with a client model, so none is dropped.)
        model_override = None
    else:
        harness_override = await asyncio.to_thread(
            _validated_harness_override, body.harness_override, agent
        )

    # Inherit runner affinity from the parent session so the child
    # is assigned to the same runner (sub-agent co-location).
    inherited_runner_id: str | None = None
    if body.parent_session_id is not None:
        parent_conv = conversation_store.get_conversation(body.parent_session_id)
        if parent_conv is not None:
            inherited_runner_id = parent_conv.runner_id
            # Defense-in-depth: don't inherit a runner the
            # caller doesn't own.
            if (
                inherited_runner_id is not None
                and user_id is not None
                and runner_router is not None
            ):
                runner_owner = runner_router.runner_owner(inherited_runner_id)
                if runner_owner is not None and runner_owner != user_id:
                    inherited_runner_id = None

    # Workspace validation: if the caller is binding to a host,
    # they must also pass a workspace, and the workspace must
    # satisfy the agent's os_env.cwd boundary on that host (per
    # designs/SESSION_WORKSPACE_SELECTION.md). Done before
    # create_conversation so a bad workspace never produces a row.
    # With git worktree creation, the validated path is the source
    # repo; the worktree it produces becomes the stored workspace.
    canonical_workspace: str | None = body.workspace
    if body.host_id is not None:
        canonical_workspace = await _validate_session_workspace(
            user_id=user_id,
            host_id=body.host_id,
            workspace=body.workspace,
            agent=agent,
            agent_cache=agent_cache,
            request=request,
        )

    # Git worktree options (optional). Two modes on body.git:
    #  - create (default): make a worktree; it becomes the stored
    #    workspace and its branch is recorded.
    #  - bind (existing_worktree): workspace already IS the worktree;
    #    record its branch only, create nothing.
    git_branch: str | None = None
    # Set to the created worktree path ONLY when Omnigent creates one.
    # Gates create-rollback: an existing worktree bound via
    # existing_worktree must never be force-removed on failure — it is
    # the user's, not an Omnigent orphan.
    created_worktree_path: str | None = None
    if body.git is not None:
        if body.git.existing_worktree:
            # Starting in a pre-existing worktree: no worktree is created, but
            # record its branch so the sidebar shows it and the opt-in delete
            # flow can offer to remove it. Validate the name (the host never
            # runs git for this path, so the server is the only gate).
            from omnigent.host.git_worktree import WorktreeError, validate_branch_name

            try:
                validate_branch_name(body.git.branch_name)
            except WorktreeError as exc:
                raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc
            git_branch = body.git.branch_name
        else:
            created_worktree = await _create_session_worktree(
                host_id=body.host_id,
                source_repo=canonical_workspace,
                git=body.git,
                request=request,
            )
            canonical_workspace = created_worktree.worktree_path
            git_branch = created_worktree.branch
            created_worktree_path = created_worktree.worktree_path

    # Native-terminal pass-through args.
    #
    # Named sub-agent creates (``body.sub_agent_name`` set) DERIVE these
    # from the trusted, server-loaded sub-spec only — any caller-supplied
    # ``body.terminal_launch_args`` is ignored. This is the YOLO seam:
    # claude-native maps ``permission_mode`` to ``--permission-mode``,
    # codex-native defaults to full bypass
    # (``--dangerously-bypass-approvals-and-sandbox``), and cursor-native
    # defaults to ``--yolo`` so a headless worker can edit/run unattended
    # without stalling on native approval prompts (opt out with
    # ``yolo: false``). A caller cannot inject launch wiring by smuggling
    # args through the spawn body.
    #
    # Sessions that resolve their own agent (top-level sessions and the
    # manual Add Agent child flow where ``sub_agent_name`` is null) keep
    # the validated body args (e.g. ``["--permission-mode",
    # "bypassPermissions"]`` from the web permission-mode selector). The
    # flat-list shape plus this bounds check is the security boundary;
    # mirrors the multipart create + PATCH paths.
    sub_spec: AgentSpec | None = None
    if body.sub_agent_name:
        sub_spec = _resolve_subagent_spec(
            agent=agent,
            sub_agent_name=body.sub_agent_name,
            agent_cache=agent_cache,
        )
        try:
            validated_launch_args = (
                _derive_terminal_launch_args_from_spec(sub_spec) if sub_spec is not None else None
            )
        except ValueError as exc:
            raise OmnigentError(
                f"invalid terminal_launch_args in sub-agent spec: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
    else:
        try:
            validated_launch_args = _validate_terminal_launch_args(body.terminal_launch_args)
        except ValueError as exc:
            raise OmnigentError(
                f"invalid terminal_launch_args: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

    try:
        conv = conversation_store.create_conversation(
            agent_id=agent.id,
            title=body.title,
            parent_conversation_id=body.parent_session_id,
            runner_id=inherited_runner_id,
            kind="sub_agent" if body.parent_session_id else "default",
            sub_agent_name=body.sub_agent_name,
            host_id=body.host_id,
            workspace=canonical_workspace,
            git_branch=git_branch,
            terminal_launch_args=validated_launch_args,
        )
    except Exception:
        # Broad catch is intentional: ANY create_conversation failure
        # (integrity error, name clash, ...) must trigger orphan-worktree
        # cleanup before the error propagates. We re-raise unchanged
        # below, so nothing is swallowed. Gate on created_worktree_path,
        # NOT git_branch: only a worktree Omnigent created here may be
        # force-removed. An existing worktree bound via workspace_branch
        # also sets git_branch but is the user's — never destroy it.
        if (
            created_worktree_path is not None
            and body.host_id is not None
            and git_branch is not None
        ):
            await _remove_session_worktree_best_effort(
                host_id=body.host_id,
                worktree_path=created_worktree_path,
                branch=git_branch,
                delete_branch=True,
                request=request,
                reason="create-rollback",
            )
        raise

    # The create request has no conv id in its URL, so the path-based
    # FastAPI hook can't tag it — stamp the minted id so the create span
    # joins the session's session.id group.
    from omnigent.runtime import telemetry

    telemetry.set_session_id(conv.id)

    if (
        model_override is not None
        or reasoning_effort is not None
        or cost_control_mode_override is not None
        or subagent_routing_override is not None
        or harness_override is not None
    ):
        # ``create_conversation`` has no override params; reuse the
        # PATCH path's store write before the runner reads the snapshot
        # (the first turn / terminal launch happens only after this
        # create returns and the caller posts a message event).
        updated_conv = await asyncio.to_thread(
            conversation_store.update_conversation,
            conv.id,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
            cost_control_mode_override=cost_control_mode_override,
            subagent_routing_override=subagent_routing_override,
            harness_override=harness_override,
        )
        if updated_conv is None:
            raise OmnigentError(
                f"Session {conv.id!r} disappeared while persisting session overrides",
                code=ErrorCode.INTERNAL_ERROR,
            )
        conv = updated_conv
    # Set wrapper labels at creation time if the agent is a native
    # terminal wrapper, so all messages
    # (including early ones sent before the runner connects) take
    # the native path and avoid double-persistence with the
    # transcript forwarder.
    native_agent = native_coding_agent_for_agent_name(agent.name)
    if native_agent is not None:
        _native_labels = dict(body.labels) if body.labels else {}
        _native_labels.update(native_agent.presentation_labels)
        await asyncio.to_thread(conversation_store.set_labels, conv.id, _native_labels)
        updated_conv = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
        if updated_conv is None:
            raise OmnigentError(
                f"Session {conv.id!r} disappeared while setting native labels",
                code=ErrorCode.INTERNAL_ERROR,
            )
        conv = updated_conv
    elif (
        body.sub_agent_name
        and sub_spec is not None
        and not _force_auto_for_child
        and (_sa_labels := _native_subagent_wrapper_labels_from_spec(sub_spec))
    ):
        # A native-harness sub-agent (claude-native / codex-native) must
        # render terminal-first with the Chat/Terminal pill, same as a
        # top-level wrapper session. Merge over any caller-supplied labels.
        # Skipped when forcing auto: the harness is not decided until the
        # first-message router runs, so native terminal labels would be
        # premature (routing may pick a non-native SDK harness).
        _merged = dict(body.labels) if body.labels else {}
        _merged.update(_sa_labels)
        await asyncio.to_thread(conversation_store.set_labels, conv.id, _merged)
        updated_conv = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
        if updated_conv is None:
            raise OmnigentError(
                f"Session {conv.id!r} disappeared while setting sub-agent labels",
                code=ErrorCode.INTERNAL_ERROR,
            )
        conv = updated_conv
    elif (
        body.sub_agent_name is None
        and body.host_id is not None
        and (
            _repl_labels := _repl_terminal_ui_labels(
                agent=agent,
                agent_cache=agent_cache,
                harness_override=harness_override,
            )
        )
    ):
        # The runner stamps this label only once its REPL terminal exists,
        # which leaves the web UI's "Starting up…" window empty; stamping at
        # creation covers the whole launch. Host-bound only: an in-process
        # session has no runner to host a terminal.
        _merged = dict(body.labels) if body.labels else {}
        _merged.update(_repl_labels)
        await asyncio.to_thread(conversation_store.set_labels, conv.id, _merged)
        updated_conv = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
        if updated_conv is None:
            raise OmnigentError(
                f"Session {conv.id!r} disappeared while setting terminal-view labels",
                code=ErrorCode.INTERNAL_ERROR,
            )
        conv = updated_conv
    elif body.labels:
        await asyncio.to_thread(conversation_store.set_labels, conv.id, body.labels)

    if harness_override == "auto" or _native_smart_routing:
        # Routing replaces the "auto" sentinel (at the first message for a
        # bundle agent, at create time for a native one), so record the auto
        # start durably: it is what lets subagent routing offer picks from the
        # other harness family later in the session.
        from omnigent.runner.subagent_routing import AUTO_HARNESS_LABEL_KEY

        await asyncio.to_thread(
            conversation_store.set_labels,
            conv.id,
            {AUTO_HARNESS_LABEL_KEY: "1"},
        )
        updated_conv = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
        if updated_conv is None:
            raise OmnigentError(
                f"Session {conv.id!r} disappeared while setting the auto-harness label",
                code=ErrorCode.INTERNAL_ERROR,
            )
        conv = updated_conv

    if _native_smart_routing:
        # Surface the create-time pick as a transcript card, so the user sees
        # which native harness + model was chosen (or why it fell back).
        _routed_native = native_coding_agent_for_agent_name(agent.name)
        if _native_routed_model is not None and _native_routing_verdict is not None:
            await _emit_server_routing_decision(
                conv.id,
                conversation_store,
                _native_routed_model,
                _native_routing_verdict,
                scope="session",
                harness=_routed_native.harness if _routed_native is not None else None,
            )
            # The same prompt is submitted again inside the harness, where the
            # first-prompt hook would score it a second time for the verdict
            # this session is already pinned to. Fingerprint what was routed so
            # that hook reuses this decision — and an edited prompt still
            # routes on its own.
            conv = await _record_create_route_prompt(
                conv, conversation_store, body.smart_routing_message
            )
        elif _native_routing_error is not None:
            await _emit_server_routing_decision(
                conv.id,
                conversation_store,
                "unavailable",
                {"rationale": _native_routing_error, "applied": False},
                scope="session",
            )
    elif _fixed_native_harness is not None:
        # Same card for a create that routed only the model. The decision label
        # records what pinned the row's model, the way a routed turn does.
        if _fixed_routed_model is not None and _fixed_routing_verdict is not None:
            _fixed_decision_id = await _emit_server_routing_decision(
                conv.id,
                conversation_store,
                _fixed_routed_model,
                _fixed_routing_verdict,
                scope="session",
                harness=_fixed_native_harness,
            )
            await _stamp_routing_decision_label(conv.id, conversation_store, _fixed_decision_id)
            conv = await asyncio.to_thread(conversation_store.get_conversation, conv.id) or conv
        elif _fixed_routing_error is not None:
            await _emit_server_routing_decision(
                conv.id,
                conversation_store,
                "unavailable",
                {"rationale": _fixed_routing_error, "applied": False},
                scope="session",
                harness=_fixed_native_harness,
            )

    # Emit session.created exactly once at creation time.
    # Best-effort: skip if the host opted out via HostHelloFrame.
    try:
        import hashlib as _hashlib

        _hr: HostRegistry | None = getattr(request.app.state, "host_registry", None)
        _host_opted_out = (
            _hr is not None
            and conv.host_id is not None
            and _hr.is_host_telemetry_opted_out(conv.host_id)
        )
        if not _host_opted_out:
            _install_id = _get_installation_id()
            _anon_uid: str | None = None
            if user_id is not None:
                _salt = f"{_install_id}:{user_id}" if _install_id else user_id
                _anon_uid = _hashlib.sha256(_salt.encode()).hexdigest()[:16]
            _client_header = request.headers.get("x-omnigent-client")
            _surface = (
                _client_header
                if _client_header in ("web", "desktop", "ios", "android", "cli")
                else _classify_surface(request.headers.get("user-agent"))
            )
            _host_install_id: str | None = None
            if _hr is not None and conv.host_id is not None:
                _host_install_id = _hr.get_host_installation_id(conv.host_id)
            # Resolve harness directly from the in-scope agent + cache so
            # the result is independent of _globals._agent_store (which is
            # only set when the server is started via the CLI).
            _tel_harness: str | None
            if native_agent is not None:
                _tel_harness = native_agent.harness
            elif conv.harness_override:
                _tel_harness = conv.harness_override
            elif agent_cache is not None:
                _tel_loaded = agent_cache.load(
                    agent.id,
                    agent.bundle_location,
                    expand_env=agent.session_id is None,
                )
                _tel_harness = _spec_harness(_tel_loaded.spec)
            else:
                _tel_harness = None
            # Only log agent name for known built-in orchestrators to avoid
            # leaking user-defined agent names.
            _NAMED_AGENTS = {"polly", "debby"}
            _tel_agent_name = agent.name if agent.name in _NAMED_AGENTS else None
            # Routing state at creation time.
            _tel_routing_on = conv.cost_control_mode_override == "on"
            _tel_emit(
                _TelSessionCreatedEvent(
                    session_id=conv.id,
                    agent_id=agent.id,
                    harness=_tel_harness,
                    surface=_surface,
                    installation_id=_install_id,
                    anon_user_id=_anon_uid,
                    host_installation_id=_host_install_id,
                    is_fork=body.parent_session_id is not None,
                    is_sub_agent=body.sub_agent_name is not None,
                    agent_name=_tel_agent_name,
                    routing_enabled=_tel_routing_on,
                )
            )
    except Exception:  # noqa: BLE001
        pass

    if body.initial_items:
        runner_client = await _get_runner_client(conv.id, runner_router)
        if runner_client is None:
            # No runner bound — persist initial items as history-only
            # seed via the conversation store. No execution fires; the
            # caller is responsible for binding a runner and posting a
            # follow-up event if they want the agent to react.
            # SessionEventInput carries no response_id; this is a
            # pre-execution history seed, so tag all items with a
            # synthetic ``"seed"`` response id. The runner overwrites
            # this on first turn via a normal append path.
            new_items = [
                _build_new_item(item, "seed", created_by=_attribution_user(user_id))
                for item in body.initial_items
            ]
            await asyncio.to_thread(conversation_store.append, conv.id, new_items)
        else:
            await _ensure_runner_relay_ready(
                conv.id,
                conv.runner_id,
                runner_client,
                conversation_store,
            )
            # Dispatch (not a plain forward) so native-terminal sessions take the
            # single-writer bypass — otherwise the forwarder's echo duplicates the kickoff.
            for item in body.initial_items:
                pending_background_title = prepare_background_session_title(
                    coordinator=background_title_coordinator,
                    conversation=conv,
                    event=item,
                )
                await _dispatch_session_event_to_runner(
                    conv.id,
                    conv,
                    item,
                    conversation_store,
                    runner_client,
                    agent_name=agent.name,
                    file_store=file_store,
                    artifact_store=artifact_store,
                    created_by=_attribution_user(user_id),
                    runner_router=runner_router,
                    host_store=getattr(request.app.state, "host_store", None),
                )
                if pending_background_title is not None:
                    pending_background_title.schedule()
    # Re-read rather than reusing the local ``conv``: the label-only branch
    # above and ``_forward_event_to_runner`` can mutate the row after it was
    # built, so a fresh read is what keeps the create response current.
    return await _get_session_snapshot(
        conversation_store,
        conv.id,
        agent_store=agent_store,
        agent_cache=agent_cache,
        liveness_lookup=liveness_lookup,
    )


def _create_session_from_bundle(
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore,
    metadata: SessionCreateMetadata,
    bundle_bytes: bytes,
    runner_id: str | None = None,
) -> CreatedSessionResponse:
    """
    Validate, store, and persist a bundled session request.

    Each upload creates a session-scoped agent row, even when a
    template agent with the same spec name already exists. Agent
    names are user-authored labels, not global content identities:
    reusing a template by name would make a fresh ``omnigent run
    <yaml>`` session execute whatever bundle that template currently
    points at, silently discarding the uploaded bundle and coupling
    unrelated users who chose the same name.

    :param conversation_store: Store that owns the atomic
        conversation-plus-agent transaction.
    :param artifact_store: Store for uploaded bundle bytes.
    :param metadata: Validated session metadata. When
        ``metadata.parent_session_id`` is set (already authorized by
        the caller), the session is created as a sub-agent
        child of that conversation.
    :param bundle_bytes: Raw uploaded ``.tar.gz`` agent bundle.
    :param runner_id: Optional runner binding inherited from the
        parent session (caller-resolved, ownership-checked),
        e.g. ``"runner_abc123"``. ``None`` leaves the session
        unbound.
    :returns: Response with the new session id.
    :raises OmnigentError: If bundle validation or agent insert
        integrity checks fail, or the parent session vanished
        between authorization and insert.
    :raises SQLAlchemyError: If the database transaction fails for
        any non-integrity reason.
    """
    # Enforce the policy-handler allowlist only on a shared /
    # multi-user server. On a trusted single-user/local server,
    # ``omnigent run`` uploads the operator's own bundle through this same
    # path, so custom handlers must keep working (the operator already has
    # code execution — the restriction would add no security there).
    spec = validate_agent_bundle(
        bundle_bytes,
        enforce_handler_allowlist=not local_single_user_enabled(),
    )
    assert spec.name is not None

    agent_id = generate_agent_id()
    agent_bundle_location = bundle_location(agent_id, bundle_bytes)
    try:
        artifact_store.put(agent_bundle_location, bundle_bytes)
    except Exception:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        raise
    return _persist_stored_session_bundle(
        conversation_store,
        artifact_store,
        metadata,
        agent_id=agent_id,
        agent_name=spec.name,
        agent_bundle_location=agent_bundle_location,
        agent_description=spec.description,
        runner_id=runner_id,
    )


async def _child_session_summaries_from_conversations(
    children: list[Conversation],
    parent_session_id: str,
    conv_store: ConversationStore,
) -> list[ChildSessionSummary]:
    """
    Build child summaries with one batched message-preview lookup.

    ``ChildSessionSummary.last_message_preview`` needs the latest visible
    message per child. Loading those by calling ``list_items`` once per
    child blocks the event loop and creates N+1 database traffic. This
    helper reads newest message items for all child ids in a worker
    thread, computes previews in memory, then builds summaries without
    further store access.

    :param children: Child conversation rows from
        ``list_conversations(kind="sub_agent")``.
    :param parent_session_id: Parent session id, e.g. ``"conv_parent987"``.
    :param conv_store: Conversation store used for the batched message read.
    :returns: One :class:`ChildSessionSummary` per input child, preserving
        input order.
    """
    if not children:
        return []
    child_ids = [child.id for child in children]
    message_items_by_child = await asyncio.to_thread(
        conv_store.list_latest_message_items_for_conversations,
        child_ids,
        10,
    )
    previews = {
        child_id: _latest_message_preview(message_items)
        for child_id, message_items in message_items_by_child.items()
    }
    return [
        _child_session_summary_from_conversation(
            child,
            parent_session_id,
            previews.get(child.id),
        )
        for child in children
    ]


async def _handle_mcp_tools_call(
    rpc_id: int | str | None,
    session_id: str,
    params: dict[str, Any],
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
    request: Request | None = None,
) -> Response:
    """
    Handle a ``tools/call`` JSON-RPC request for the MCP proxy endpoint.

    Steps:

    1. Validate the tool name (namespaced like ``github__search`` for MCP
       tools, or bare like ``sys_os_read`` for runner-local tools).
    2. Load session → agent → spec for policy evaluation.
    3. On first call: evaluate TOOL_CALL policy.  On DENY, return error.
       On ASK, emit a ``response.elicitation_request`` SSE event and
       return an MCP ``InputRequiredResult`` so the runner can park for
       user approval and retry per the MRTR spec.
    4. On retry (``requestState`` present in ``params``): verify the
       state, check the user's ``inputResponses``, and proceed if
       approved.
    5. Delegate execution to the runner's ``POST
       /v1/sessions/{id}/mcp/execute`` endpoint via the WS tunnel so
       that stdio MCP subprocesses and runner-local tools execute on the
       runner's machine (correct ``cwd``, environment, and tooling).
    6. Evaluate the TOOL_RESULT policy phase on the returned output;
       replace with a redaction notice on DENY.
    7. Return the result in MCP ``content`` format.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param session_id: The session id, e.g. ``"conv_abc123"``.
    :param params: The JSON-RPC ``params`` object.  On first call,
        contains ``"name"`` and ``"arguments"``.  On retry, also
        contains ``"requestState"`` (opaque blob from the server) and
        ``"inputResponses"`` (user's approval decision), e.g.
        ``{"name": "sys_os_shell", "arguments": {}, "requestState": "...",
        "inputResponses": {"elicit_abc": {"action": "accept"}}}``.
    :param conversation_store: Store for session and label state.
    :param agent_store: Store for agent lookup.
    :param runner_router: Router used to get a tunneled client pointed at
        the session's runner. ``None`` returns an error response.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: A JSON-RPC 2.0 response carrying the tool result as MCP
        ``content`` blocks, an ``InputRequiredResult`` on ASK, or an
        error response when the call is denied, the runner is
        unavailable, or the underlying MCP call fails.
    """

    namespaced_name = params.get("name", "")
    arguments: dict[str, Any] = params.get("arguments") or {}
    request_state_str: str | None = params.get("requestState")
    input_responses: dict[str, Any] = params.get("inputResponses") or {}
    is_retry = request_state_str is not None

    _logger.debug(
        "MCP tools/call: session=%r tool=%r is_retry=%r",
        session_id,
        namespaced_name,
        is_retry,
    )

    if not namespaced_name:
        return _mcp_error_response(rpc_id, -32000, "Missing tool name in tools/call params")

    # Session → agent → spec (needed for policy evaluation on both paths).
    # All three reads — conversation row, agent row, and the cold-cache
    # bundle fetch + spec parse — are blocking IO. Run them off the event
    # loop so an MCP tool call doesn't stall the single-worker server and
    # serialize concurrent requests behind it.
    conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conv is None or conv.agent_id is None:
        return _mcp_error_response(
            rpc_id, -32000, f"Session not found or has no agent: {session_id!r}"
        )

    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return _mcp_error_response(rpc_id, -32000, f"Agent not found: {conv.agent_id!r}")

    # Build the policy engine once — used for both TOOL_CALL (first call
    # only) and TOOL_RESULT (both paths). Engine construction reads
    # session-policy specs and labels from the DB, so keep it off-loop too.
    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store, conv
    )

    if is_retry:
        # ── Retry path: user has responded to the elicitation ────────
        # Verify the opaque requestState.
        try:
            state = json.loads(request_state_str)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return _mcp_error_response(rpc_id, -32000, "Invalid requestState: not valid JSON")
        if state.get("session_id") != session_id:
            # Reject cross-session replay.
            return _mcp_error_response(rpc_id, -32000, "requestState session mismatch")

        # ── Fail-closed: re-evaluate TOOL_CALL policy on retry ──────
        # The original retry path trusted the caller-supplied
        # requestState + inputResponses as proof that "policy ran and
        # the user approved." Because requestState is unsigned JSON
        # and inputResponses is caller-controlled, a forged retry
        # could bypass DENY/ASK gates entirely. Re-evaluating the
        # policy on every retry closes this vector: a DENY'd tool
        # stays denied regardless of what the request body claims.
        retry_ctx = EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": namespaced_name, "arguments": arguments},
            tool_name=namespaced_name,
            actor=actor,
        )
        retry_result = await engine.evaluate(retry_ctx)

        _logger.debug(
            "MCP tools/call retry TOOL_CALL policy: session=%r tool=%r action=%r reason=%r",
            session_id,
            namespaced_name,
            retry_result.action,
            retry_result.reason,
        )

        if retry_result.action == PolicyAction.DENY:
            return _mcp_error_response(
                rpc_id,
                -32000,
                f"Denied by policy: {retry_result.reason or 'no reason given'}",
            )

        if retry_result.action == PolicyAction.ASK:
            # Policy still requires approval — verify the elicitation
            # was genuinely issued by the server (present in the
            # server-side pending map) and that the user approved it.
            elicitation_id_from_state: str = state.get("elicitation_id", "")
            if elicitation_id_from_state not in _pending_policy_ask_writes:
                # The elicitation_id is not in the server-side map.
                # Either it was forged, already consumed, or expired.
                # Check inputResponses: if the caller claims approval
                # for an unrecognised elicitation, reject it.
                approval: dict[str, Any] = input_responses.get(elicitation_id_from_state) or {}
                if approval.get("action") == "accept":
                    # Claimed approval for an elicitation the server
                    # never issued or already consumed — reject.
                    return _mcp_error_response(
                        rpc_id,
                        -32000,
                        "Elicitation not found or already resolved",
                    )
                return _mcp_error_response(rpc_id, -32000, "Tool call denied by user")
            approval = input_responses.get(elicitation_id_from_state) or {}
            if approval.get("action") != "accept":
                return _mcp_error_response(rpc_id, -32000, "Tool call denied by user")
            # Recover any policy-transformed args that were serialised into
            # requestState on the initial ASK — the client re-sends the
            # original arguments which we must not use when a transform was set.
            if state.get("transformed_arguments") is not None:
                arguments = state["transformed_arguments"]
            # Apply the deciding policy's deferred writes now that the
            # user approved (POLICIES.md §7.2: only on accept).
            _pending = _pending_policy_ask_writes.pop(elicitation_id_from_state, None)
            if _pending is not None:
                if _pending.set_labels:
                    await asyncio.to_thread(engine.apply_label_writes, _pending.set_labels)
                if _pending.state_updates:
                    with contextlib.suppress(ConversationNotFoundError):
                        await asyncio.to_thread(engine.apply_state_updates, _pending.state_updates)
        else:
            # ALLOW — policy no longer requires approval (e.g. label
            # state changed between the original ASK and this retry).
            # Recover transformed args if present, then fall through.
            if state.get("transformed_arguments") is not None:
                arguments = state["transformed_arguments"]
        # Fall through to execution.
    else:
        # ── First call: evaluate TOOL_CALL policy ────────────────────
        call_ctx = EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": namespaced_name, "arguments": arguments},
            tool_name=namespaced_name,
            actor=actor,
        )
        call_result = await engine.evaluate(call_ctx)

        _logger.debug(
            "MCP tools/call TOOL_CALL policy: session=%r tool=%r action=%r reason=%r",
            session_id,
            namespaced_name,
            call_result.action,
            call_result.reason,
        )

        if call_result.action == PolicyAction.DENY:
            if call_result.set_labels:
                await asyncio.to_thread(engine.apply_label_writes, call_result.set_labels)
            return _mcp_error_response(
                rpc_id,
                -32000,
                f"Denied by policy: {call_result.reason or 'no reason given'}",
            )

        if call_result.action == PolicyAction.ASK:
            # Emit elicitation SSE event (for REPL approval UI) and return
            # InputRequiredResult per the MCP MRTR spec so the runner can
            # park on the approval Future and retry when the user decides.
            elicitation_id = await _register_policy_elicitation(
                session_id,
                call_result,
                json.dumps(arguments)[:1024],
                conversation_store,
            )
            # Defer the deciding policy's writes (label mutations AND
            # state_updates such as a cost-budget checkpoint) to the
            # approved retry path — POLICIES.md §7.2 lands them only on
            # accept. The approval handler at the top of this function
            # already applies both via ``apply_label_writes`` and
            # ``apply_state_updates``. Mirrors the relay path pattern.
            # Always store an entry even when there are no deferred
            # writes — the retry path checks the pending map to verify
            # the elicitation was genuinely issued by the server. A
            # missing entry causes "Elicitation not found or already
            # resolved" on the retry.
            _pending_policy_ask_writes[elicitation_id] = _PendingPolicyAskWrites(
                state_updates=call_result.state_updates,
                set_labels=call_result.set_labels,
                from_mcp=True,
            )
            request_state_payload: dict[str, Any] = {
                "elicitation_id": elicitation_id,
                "session_id": session_id,
            }
            # If the policy returned transformed args alongside ASK (e.g.
            # PII-redacted arguments), persist them so the retry path can
            # apply them after the user approves — the client re-sends the
            # original arguments, which would silently bypass the transform.
            if call_result.data is not None:
                request_state_payload["transformed_arguments"] = call_result.data
            request_state = json.dumps(request_state_payload)
            return _mcp_input_required_response(
                rpc_id,
                elicitation_id=elicitation_id,
                message=call_result.reason or "Approval required to run this tool",
                request_state=request_state,
                session_id=session_id,
            )
        # ALLOW — apply labels now that we know the action is not ASK.
        if call_result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, call_result.set_labels)
        # If the policy returned transformed arguments (e.g.
        # PII-redacted args), use them instead of the originals.
        if call_result.data is not None:
            arguments = cast("dict[str, object]", call_result.data)

    # ── Server-side sys_advise_models intercept ──────────────────────────
    # After policy evaluation (DENY/ASK handled above); arguments may have
    # been transformed. The advisor runs server-side where routing_client lives.
    if namespaced_name in ("sys_advise_models", "mcp__omnigent__sys_advise_models"):
        return await _handle_advise_models_mcp(
            rpc_id,
            conv,
            arguments,
            agent_store,
            session_id=session_id,
            runner_router=runner_router,
        )

    # ── Execute on the runner via WS tunnel ──────────────────────────
    # The runner owns stdio subprocess spawning (correct machine, cwd,
    # and env). We call its /mcp/execute endpoint through the same WS
    # tunnel the runner already opened to the Omnigent server at startup.
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        from omnigent.runtime import get_runner_client

        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return _mcp_error_response(rpc_id, -32000, f"No runner bound for session {session_id!r}")
    try:
        from omnigent.runner.tool_dispatch import MCP_PROXY_FORWARD_TIMEOUT_S

        exec_resp = await runner_client.post(
            f"/v1/sessions/{session_id}/mcp/execute",
            json={
                "method": "tools/call",
                "params": {"name": namespaced_name, "arguments": arguments},
            },
            # ``sys_session_send`` returns a launch handle immediately; this
            # timeout now protects ordinary runner proxy hangs.
            timeout=MCP_PROXY_FORWARD_TIMEOUT_S,
        )
        exec_resp.raise_for_status()
        exec_data = exec_resp.json()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Runner MCP execute failed: %s", exc, exc_info=True)
        return _mcp_error_response(rpc_id, -32000, "Runner MCP execute failed.")

    if "error" in exec_data:
        err = exec_data["error"]
        return _mcp_error_response(
            rpc_id, err.get("code", -32000), err.get("message", "unknown error")
        )

    # ── MRTR: external MCP server needs user input ───────────────
    # The runner returns ``{"result": {"input_required": {...}}}``
    # when the external MCP server sent an ``InputRequiredResult``.
    # Surface each elicitation to the user via the existing SSE
    # infrastructure, gather responses, then retry on the runner.
    mcp_input_required = exec_data.get("result", {}).get("input_required")
    if mcp_input_required is not None:
        if request is None:
            return _mcp_error_response(
                rpc_id, -32000, "MCP server requires elicitation but no request context available"
            )
        input_requests: dict[str, Any] = mcp_input_required.get("inputRequests") or {}
        mcp_request_state: str = mcp_input_required.get("requestState", "")

        # Gather user responses for each inputRequest.
        elicitation_responses: dict[str, Any] = {}
        for eid, req_entry in input_requests.items():
            req_params = req_entry.get("params", {}) if isinstance(req_entry, dict) else {}
            elicit_params = ElicitationRequestParams(
                mode=cast(Literal["form", "url"], req_params.get("mode", "form")),
                message=req_params.get("message", "Approval required"),
                requestedSchema=cast(
                    Mapping[str, Any] | None,
                    req_params.get("requestedSchema"),
                ),
            )
            elicit_result = await _publish_and_wait_for_harness_elicitation(
                request,
                session_id=session_id,
                params=elicit_params,
                timeout_s=300.0,
                conversation_store=conversation_store,
            )
            if elicit_result is None:
                elicitation_responses[eid] = {"action": "decline"}
            else:
                resp_entry: dict[str, Any] = {"action": elicit_result.action}
                if elicit_result.content is not None:
                    resp_entry["content"] = elicit_result.content
                elicitation_responses[eid] = resp_entry

        # Retry on the runner with the user's inputResponses.
        try:
            retry_resp = await runner_client.post(
                f"/v1/sessions/{session_id}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {
                        "name": namespaced_name,
                        "arguments": arguments,
                        "inputResponses": elicitation_responses,
                        "requestState": mcp_request_state,
                    },
                },
                timeout=MCP_PROXY_FORWARD_TIMEOUT_S,
            )
            retry_resp.raise_for_status()
            exec_data = retry_resp.json()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Runner MCP retry failed: %s", exc, exc_info=True)
            return _mcp_error_response(rpc_id, -32000, "Runner MCP retry failed.")
        if "error" in exec_data:
            err = exec_data["error"]
            return _mcp_error_response(
                rpc_id, err.get("code", -32000), err.get("message", "unknown error")
            )
        # Multi-round MRTR: the server returned yet another
        # InputRequiredResult on the retry. Return an error rather
        # than looping indefinitely — the user can retry the tool.
        if exec_data.get("result", {}).get("input_required") is not None:
            return _mcp_error_response(
                rpc_id,
                -32000,
                "MCP server requires additional elicitation rounds (not yet supported)",
            )

    output: str = exec_data.get("result", {}).get("output", "")
    _logger.debug(
        "MCP tools/call execute: session=%r tool=%r output_len=%d",
        session_id,
        namespaced_name,
        len(output),
    )

    # ── TOOL_RESULT policy ───────────────────────────────────────────
    result_ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content={"result": output},
        tool_name=namespaced_name,
        request_data={"name": namespaced_name, "arguments": arguments},
        actor=actor,
    )
    result_policy = await engine.evaluate(result_ctx)

    if result_policy.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, result_policy.set_labels)

    _logger.debug(
        "MCP tools/call TOOL_RESULT policy: session=%r tool=%r action=%r reason=%r",
        session_id,
        namespaced_name,
        result_policy.action,
        result_policy.reason,
    )

    if result_policy.action == PolicyAction.DENY:
        output = f"[Result suppressed by policy: {result_policy.reason or 'no reason given'}]"
    elif result_policy.data is not None:
        # Policy returned transformed output (e.g. PII-redacted content).
        # The TOOL_RESULT phase contract requires data to be a str; coerce
        # and warn rather than dropping the result if a policy author returns
        # the wrong type (common mistake: returning the full content dict).
        if not isinstance(result_policy.data, str):
            _logger.warning(
                "TOOL_RESULT policy data must be str; got %s — coercing via str()",
                type(result_policy.data).__name__,
            )
        output = (
            result_policy.data if isinstance(result_policy.data, str) else str(result_policy.data)
        )

    return _mcp_ok_response(
        rpc_id,
        {"content": [{"type": "text", "text": output}]},
    )


async def _fetch_runner_skills(
    runner_client: httpx.AsyncClient | None,
    session_id: str,
) -> list[SkillSummary]:
    """
    Fetch a session's merged skills from its bound runner.

    Skills are runner-owned: the runner discovers them against its own
    filesystem (the spec's bundled skills plus host skills under the
    session's workspace and the runner's ``~/.claude/skills/``). The
    server only overlays the result onto the session snapshot (the web
    composer's slash-command menu).
    Best-effort: a missing/unreachable runner, a non-200, or any
    transport error yields an empty list rather than failing the
    snapshot.

    :param runner_client: HTTP client pointed at the bound runner, or
        ``None`` when no runner is bound.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :returns: Skill summaries (name + one-line description) for the
        session, or ``[]`` when unavailable.
    """
    if runner_client is None:
        return []
    cached = _runner_skills_cache.get(session_id)
    if cached is not None:
        return cached
    # Don't await the runner here: this snapshot is polled continuously
    # (incl. mid-turn), and a per-poll runner round-trip pins the runner's
    # event loop and wedges the turn. Kick one background fetch (single-
    # flight) and return ``[]``; a later poll serves the cached result.
    if session_id not in _runner_skills_inflight:
        task = asyncio.create_task(_load_runner_skills(runner_client, session_id))
        _runner_skills_inflight[session_id] = task

        def _clear_skills_inflight(_task: asyncio.Task[None]) -> None:
            _runner_skills_inflight.pop(session_id, None)

        task.add_done_callback(_clear_skills_inflight)
    return []


async def _fetch_model_options(
    runner_client: httpx.AsyncClient | None,
    session_id: str,
    conv: Conversation,
) -> list[dict[str, Any]]:
    """
    Resolve the Web UI model-picker options for a native session.

    Three shapes:

    * **codex-native / cursor-native / kiro-native** — a *live* catalog only
      the bound runner can read from the installed CLI. Like skills, this stays
      off the snapshot hot path: the first snapshot kicks a background fetch
      and returns ``[]``; subsequent snapshots serve the cache. The cache
      outlives the runner: with no runner bound (asleep session) it keeps
      serving, and a stale-marked entry serves while a live re-fetch replaces
      it.
    * **claude-native** — the provider-neutral aliases from the exact launch
      config, refreshed from Databricks before each new terminal starts.
      With no runner bound and a cold cache (server restart while the
      session slept), the session's host resolves a pre-launch preview
      instead — the same source the new-session picker uses.

    :param runner_client: HTTP client pointed at the bound runner, or
        ``None`` when no runner is bound.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: Conversation row whose labels identify the wrapper.
    :returns: Model options, or ``[]`` when the session has no model picker or
        the runner-owned options are not yet available.
    """
    wrapper = conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
    if wrapper == _PI_NATIVE_WRAPPER_LABEL_VALUE:
        # pi-native's catalog is PUSHED by its extension (its live
        # ``ctx.modelRegistry``), not fetched: that reflects the models pi
        # actually loaded regardless of auth path (Omnigent provider OR pi's
        # own ``/login``), so the picker populates even when no ``models.json``
        # is written into the bridge dir. Empty until the extension posts
        # ``external_model_options`` on session start.
        return _pushed_model_options_cache.get(session_id, [])
    endpoint = _MODEL_OPTIONS_ENDPOINT_BY_WRAPPER.get(wrapper or "")
    if endpoint is None:
        return []
    cached = _model_options_cache.get(session_id)
    if runner_client is None:
        # No runner to ask (asleep / stranded): serve the last-fetched
        # catalog so the picker stays usable for offline model changes.
        if cached:
            return cached
        # Cold cache too (e.g. the server restarted while the session
        # slept). claude-native's catalog is also resolvable by the
        # session's host — the same pre-launch source the new-session
        # picker uses — so fill it from there in the background.
        if (
            wrapper == _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE
            and conv.host_id is not None
            and session_id not in _model_options_inflight
        ):
            task = asyncio.create_task(_load_model_options_from_host(session_id, conv.host_id))
            _model_options_inflight[session_id] = task

            def _clear_host_options_inflight(_task: asyncio.Task[None]) -> None:
                _model_options_inflight.pop(session_id, None)

            task.add_done_callback(_clear_host_options_inflight)
        return []
    if cached is not None and session_id not in _model_options_stale:
        return cached
    if session_id not in _model_options_inflight:
        path = f"/v1/sessions/{session_id}/{endpoint}"
        task = asyncio.create_task(_load_model_options(runner_client, session_id, path))
        _model_options_inflight[session_id] = task

        def _clear_runner_options_inflight(_task: asyncio.Task[None]) -> None:
            _model_options_inflight.pop(session_id, None)

        task.add_done_callback(_clear_runner_options_inflight)
    # A stale catalog serves while the re-fetch runs; success publishes
    # ``session.model_options`` so open clients re-read the snapshot.
    return cached or []


async def _get_session_snapshot(
    conv_store: ConversationStore,
    session_id: str,
    permission_level: int | None = None,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
    conversation: Conversation | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    include_items: bool = True,
    runner_exit_reports: RunnerExitReports | None = None,
    refresh_state: bool = False,
    host_store: HostStore | None = None,
    sandbox_config: ManagedSandboxConfig | None = None,
    viewer_id: str | None = None,
) -> SessionResponse:
    """
    Read a full session snapshot from the store.

    Centralizes the create/get response building so both endpoints
    return identical projections. The lifecycle ``status`` is
    derived from the relay-fed ``_session_status_cache`` (the tasks
    table has been removed).

    :param conv_store: The conversation store to read from.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param permission_level: The requesting user's numeric level
        on this session, or ``None`` when permissions are disabled.
    :param agent_store: Optional agent store used to look up the
        bound agent's bundle location. ``None`` in legacy call sites
        that don't yet pass it.
    :param agent_cache: Optional agent cache used to load the parsed
        spec from the bundle (provides ``llm_model`` and
        ``context_window``). ``None`` in legacy call sites.
    :param conversation: The already-fetched conversation row to reuse,
        skipping the ``get_conversation`` read. Pass it when the caller
        just authorized the session (which fetched the same row) so the
        snapshot doesn't re-read it. ``None`` reads it here as before.
    :param liveness_lookup: Bulk session-liveness lookup (the server's
        ``_bulk_session_liveness``) used to populate ``runner_online``
        and ``host_online`` on the snapshot. ``None`` (e.g. focused
        tests) leaves both fields ``None`` so the client falls back to
        its ``/health`` poll.
    :param include_items: When ``False``, skip the committed-items read
        and return ``items=[]``. Callers that hydrate the transcript
        through ``GET /sessions/{id}/items`` (the web chat surface)
        pass ``False`` — the items read is the most expensive step of
        the snapshot build and its result would be discarded.
    :param refresh_state: When ``True``, clear runner-backed snapshot
        overlays for this session before building the response. Browser
        reloads use this so a refresh re-reads current live-session
        capabilities instead of serving stale AP-process caches.
    :returns: The fully populated :class:`SessionResponse`.
    :raises OmnigentError: 404 if no session exists, 500 if the
        underlying conversation has no agent binding
        (see :func:`_build_session_response`).
    """
    conv = conversation
    if conv is None:
        conv = await asyncio.to_thread(conv_store.get_conversation, session_id)
    if conv is None:
        raise _session_not_found()
    # Return the most recent committed items while preserving the
    # SessionResponse contract that ``items`` is chronological. The
    # store's default page is the oldest 100 (``order="asc"``), which
    # makes long-session reconnects appear stale in clients that use the
    # snapshot directly.
    items: list[ConversationItem] = []
    if include_items:
        items_page = await asyncio.to_thread(
            conv_store.list_items,
            conversation_id=session_id,
            limit=100,
            order="desc",
        )
        items = list(reversed(items_page.data))
    # Resolve the bound runner client once — used for live status (on a
    # status-cache miss) and for runner-owned skill discovery below.
    #
    # Prefer the router (multi-runner deployments wire only
    # ``set_runner_router``; the legacy ``get_runner_client`` singleton
    # stays ``None`` there). Fall back to the legacy singleton for
    # single-runner / in-process tests.
    from omnigent.runtime import get_runner_client, get_runner_router

    runner_client: httpx.AsyncClient | None = None
    runner_router = get_runner_router()
    if runner_router is not None:
        try:
            routed = runner_router.client_for_session_resources(session_id)
            runner_client = routed.client
        except (LookupError, httpx.HTTPError, OmnigentError):
            _logger.debug(
                "No runner bound for session=%s on snapshot build",
                session_id,
            )
    if runner_client is None:
        runner_client = get_runner_client()

    if refresh_state:
        wrapper = conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
        # Cursor effort/model changes refresh snapshots; keep its previous
        # options visible until the asynchronous CLI re-fetch replaces them.
        # Other live catalogs retain their existing drop-on-refresh contract.
        _invalidate_runner_backed_snapshot_state(
            session_id,
            cancel_inflight=False,
            drop_model_options=(
                runner_client is not None and wrapper != _CURSOR_NATIVE_WRAPPER_LABEL_VALUE
            ),
        )

    status = _session_status_from_cache(session_id)
    if status == "idle":
        # Cache miss (or truly idle): either the server restarted, or the
        # relay has not yet published the first ``"running"`` event for a
        # freshly bound session (the relay's GET /stream is still in its
        # tunnel handshake). Ask the runner for live status so we don't
        # synthesize a stale ``"idle"`` while a turn is actually in flight.
        # ``_session_status_from_cache`` already collapses the fine-grained
        # relay values (``"waiting"`` → ``"running"``), so the raw cache value
        # is only needed here when it is actually missing (None).
        if _session_status_cache.get(session_id) is None and runner_client is not None:
            try:
                resp = await runner_client.get(
                    f"/v1/sessions/{session_id}",
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    raw = resp.json().get("status", "idle")
                    _session_status_cache[session_id] = raw
                    if raw in ("idle", "running", "waiting", "failed"):
                        session_live_state.persist_live_status(session_id, raw)
                    status = _session_status_from_cache(session_id)
            except (httpx.HTTPError, ConnectionError):
                _logger.debug(
                    "Runner status query failed for %s",
                    session_id,
                )
    # last_total_tokens and last_task_error come from the context-tokens
    # label written by the forwarder (tasks table has been removed).
    last_total_tokens: int | None = None
    last_task_error: dict[str, str] | None = None
    raw_label = conv.labels.get(_LAST_CONTEXT_TOKENS_LABEL_KEY)
    if isinstance(raw_label, str) and raw_label.isdigit():
        last_total_tokens = int(raw_label)
    last_task_error = _last_task_error_from_labels(conv.labels)
    # Runner-crash durability: if the session's bound runner reported an
    # unexpected exit (host.runner_exited → RunnerExitReports), surface the
    # cause as last_task_error so a reload/late-open still renders the error
    # banner — the live session.status:failed push is gone by then. status
    # already reads "failed" from the cache (set by _on_runner_exited). The
    # report is keyed by the CURRENT runner_id, so a successful relaunch
    # (new token-bound runner_id) naturally stops matching. Access is gated
    # by the session-snapshot's own authorization, so the unscoped get is
    # correct here (the report is this session's own runner).
    if runner_exit_reports is not None and conv.runner_id is not None:
        exit_error = runner_exit_reports.get(conv.runner_id)
        if exit_error is not None:
            last_task_error = {"code": "runner_failed_to_start", "message": exit_error}
            status = "failed"
    llm_model: str | None = None
    context_window: int | None = None
    agent_name: str | None = None
    if agent_store is not None and agent_cache is not None and conv.agent_id is not None:
        try:
            agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
            if agent is not None:
                agent_name = agent.name
                if agent.bundle_location is not None:
                    # Offload to a worker thread: on a cold cache this fetches
                    # the bundle from the artifact store and parses the spec —
                    # blocking IO that would otherwise stall the single-worker
                    # event loop on every page-load snapshot.
                    loaded = await asyncio.to_thread(
                        agent_cache.load,
                        agent.id,
                        agent.bundle_location,
                        expand_env=agent.session_id is None,
                    )
                    spec = loaded.spec
                    if conv.sub_agent_name:
                        child_spec = _find_spec_by_name(spec, conv.sub_agent_name)
                        if child_spec is not None:
                            spec = child_spec
                    # Prefer the spec's name over the agent row's: a
                    # switch-created session-scoped clone is named
                    # "<builtin> (switch ag_…)" for row disambiguation,
                    # but clients display agent_name verbatim — the spec
                    # carries the clean identity (e.g. "claude-native-ui").
                    if spec.name:
                        agent_name = spec.name
                    llm_model = spec.executor.model

                    # Size the context ring against whatever the next turn will
                    # actually run, using the SAME resolver the runner uses to
                    # budget compaction. That makes the UI ring and the runner's
                    # compaction trigger a single source of truth — computed by
                    # one function — so they can't drift even though they run in
                    # different processes at different times. (They previously
                    # each inlined this rule and silently fell out of step;
                    # sharing the function removes the manual
                    # sync.) spec.executor.context_window describes only the spec
                    # model, so an active override bypasses it — the resolver
                    # makes that decision from the spec model + override.
                    #
                    # Offload to a worker thread: an active override (or an
                    # undeclared window) can trigger a cache-cold provider
                    # catalog fetch (blocking HTTP / CPU-bound litellm) inside
                    # the resolver, which would otherwise stall the single-worker
                    # event loop and serialize every concurrent snapshot.
                    context_window = await asyncio.to_thread(
                        resolve_effective_context_window,
                        spec.executor.context_window,
                        llm_model,
                        model_override=conv.model_override,
                    )
        except Exception:  # noqa: BLE001
            pass
    # Skills are runner-owned: the bound runner discovers them against its
    # own filesystem (bundled skills + host skills under the session's
    # workspace and ``~/.claude/skills/``) — the host where the harness
    # actually executes and may read a skill's local resource files. The
    # server only overlays the result; best-effort, empty when no runner
    # is bound or it can't be reached.
    skills = await _fetch_runner_skills(runner_client, session_id)
    # Codex model options are also runner-owned: they come from the
    # session's live Codex app-server ``model/list`` response. Best-effort
    # and cache-backed like skills so a snapshot poll cannot wedge the
    # runner while a turn is active.
    model_options = await _fetch_model_options(runner_client, session_id, conv)
    # Dynamic override from the forwarder (real Claude Code window).
    # Only present after the first statusLine tick; before that the
    # spec default applies.
    raw_window_label = conv.labels.get(_LAST_CONTEXT_WINDOW_LABEL_KEY)
    if isinstance(raw_window_label, str) and raw_window_label.isdigit():
        observed = int(raw_window_label)
        if observed > 0:
            context_window = observed
    # Resolve strict runner + host liveness for the open-session view.
    # The lookup hits the conversations + hosts tables, so offload it to
    # a worker thread (mirroring _apply_liveness_to_items). Left None on
    # both fields when no lookup is wired (focused tests).
    runner_online: bool | None = None
    host_online: bool | None = None
    if liveness_lookup is not None:
        liveness = await asyncio.to_thread(liveness_lookup, [session_id])
        result = liveness.get(session_id)
        if result is not None:
            runner_online = result.runner_online
            host_online = result.host_online
    # Subtree usage (this session + its sub-agent descendants) so the
    # displayed cost includes sub-agents — a codex/claude sub-agent's spend
    # is persisted on its own child conversation, not the parent's, so the
    # parent's own session_usage would under-report. Off the event loop
    # because it pages the conversation tree from the store.
    subtree_usage = await asyncio.to_thread(load_session_usage, conv.id, conv_store)
    # Static signal telling the open view a host-bound, host-down session is a
    # resumable managed host it can wake by sending a message, vs a terminal
    # host_offline dead-end. Computed independently of liveness_lookup (the web
    # chat passes include_liveness=False, so host_online is None here and
    # liveness arrives via the poll/stream). One indexed host read, gated to
    # host-bound sessions.
    host_resumable = False
    if host_store is not None and sandbox_config is not None and conv.host_id is not None:
        host_for_resume = await asyncio.to_thread(host_store.get_host, conv.host_id)
        if host_for_resume is not None:
            host_resumable = host_resume_supported(host_for_resume, sandbox_config)
    return _build_session_response(
        conv,
        items,
        status,
        permission_level,
        background_task_count=_session_background_task_count_cache.get(session_id),
        llm_model=llm_model,
        context_window=context_window,
        last_total_tokens=last_total_tokens,
        last_task_error=last_task_error,
        agent_name=agent_name,
        skills=skills,
        model_options=model_options,
        runner_online=runner_online,
        host_online=host_online,
        host_resumable=host_resumable,
        pending_elicitation_events=await asyncio.to_thread(
            _pending_elicitation_snapshot_for_session,
            conv_store,
            conv,
        ),
        subtree_usage=subtree_usage,
        viewer_id=viewer_id,
        agent_store=agent_store,
        agent_cache=agent_cache,
    )


__all__ = [
    "_accumulate_session_usage",
    "_archive_stop",
    "_best_effort_stop",
    "_bind_and_launch_managed_runner",
    "_build_native_terminal_message_event",
    "_build_session_list_item",
    "_build_session_response",
    "_child_session_summaries_from_conversations",
    "_create_session_from_bundle",
    "_create_session_from_existing_agent",
    "_detached_stop_tasks",
    "_dispatch_session_event_to_runner",
    "_drive_terminal_resolved_elicitation",
    "_enrich_idle_status_with_subagent_output",
    "_ensure_native_terminal_ready",
    "_ensure_runner_relay",
    "_ensure_runner_relay_ready",
    "_ensure_runner_session_initialized",
    "_evaluate_input_policy",
    "_evaluate_tool_call_policy",
    "_fetch_model_options",
    "_fetch_runner_skills",
    "_forward_event_to_runner",
    "_forward_native_subagent_terminal_failure",
    "_forward_native_terminal_message",
    "_gateway_backed",
    "_get_session_snapshot",
    "_handle_mcp_tools_call",
    "_heal_subagent_runner_binding_via_parent",
    "_hold_native_ask_gate",
    "_is_native_terminal_session",
    "_kick_managed_relaunch",
    "_kick_managed_wake",
    "_labels_for_viewer",
    "_maybe_relaunch_managed_sandbox",
    "_maybe_wake_stale_resumable_managed_sandbox",
    "_native_subagent_wrapper_labels",
    "_native_terminal_runtime",
    "_persist_external_codex_subagent_start",
    "_persist_external_conversation_item",
    "_persist_external_session_usage",
    "_persist_host_launch_failure_turn",
    "_persist_model_change_note",
    "_persist_native_cumulative_usage",
    "_persist_native_terminal_failure",
    "_persist_session_event",
    "_persist_skipped_kiro_pending_input",
    "_publish_and_wait_for_harness_elicitation",
    "_publish_runner_recovered_status",
    "_publish_subtree_cost_to_ancestors",
    "_recover_subagent_status_forward_via_parent",
    "_register_policy_elicitation",
    "_relay_runner_stream",
    "_resolve_elicitation",
    "_run_managed_launch",
    "_run_managed_wake",
    "_schedule_deferred_elicitation_clear",
    "_spawn_archive_stop",
    "_spawn_gateway_backed",
    "_spawn_native_approval_popup_forward",
    "_spawn_native_blocked_notice_forward",
    "_wait_for_host_bound_runner_client",
    "_wake_parent_for_blocked_child",
    "configure_subagent_block_notifier",
    "ensure_runner_connected",
]
