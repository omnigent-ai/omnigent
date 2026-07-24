"""Events, stream, and delete session routes."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import (
    APIRouter,
    Request,
)
from fastapi.responses import StreamingResponse

from omnigent.entities import (
    ErrorData,
    NewConversationItem,
)
from omnigent.entities.conversation import (
    parse_item_data,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE as _HARNESS_NOT_CONFIGURED_ERROR_CODE,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import (
    session_stream,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server import presence
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    LEVEL_READ,
    AuthProvider,
)
from omnigent.server.background_session_titles import (
    BackgroundSessionTitleCoordinator,
    prepare_background_session_title,
)
from omnigent.server.host_registry import HostRegistry, RunnerExitReports
from omnigent.server.routes._auth_helpers import (
    attribution_user as _attribution_user,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import *
from omnigent.server.routes._sessions.common import (
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import *
from omnigent.server.routes._sessions.orchestration import *
from omnigent.server.schemas import (
    ConversationDeleted,
    ElicitationRequestEvent,
    ElicitationRequestParams,
    ErrorDetail,
    McpServerStartup,
    SessionEventInput,
)
from omnigent.session_lifecycle import (
    is_session_closed,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.telemetry import emit as _tel_emit
from omnigent.telemetry.events import SessionDeletedEvent as _TelSessionDeletedEvent
from omnigent.telemetry.events import SessionStoppedEvent as _TelSessionStoppedEvent
from omnigent.telemetry.installation_id import get_installation_id as _get_installation_id
from omnigent.tools.client_specified import parse_client_side_tool_specs


def register_events_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    runner_exit_reports: RunnerExitReports | None = None,
    host_registry: HostRegistry | None = None,
    background_title_coordinator: BackgroundSessionTitleCoordinator | None = None,
) -> None:
    """Register the events, stream, and delete routes on router."""

    @router.post(
        "/sessions/{session_id}/events",
        # Internal event ingestion — hidden from the public API reference.
        include_in_schema=False,
        status_code=202,
        # response_model=None: the body is a small acknowledgement
        # dict, not a domain model.
        response_model=None,
    )
    async def post_event(
        request: Request,
        session_id: str,
        body: SessionEventInput,
    ) -> dict[str, bool | str]:
        """
        Submit a session event (input message, tool output,
        approval, or interrupt).

        Dispatches on ``body.type``:

        - ``"interrupt"`` cancels any active task and publishes a
          ``session.interrupted`` event. Bypasses item persistence.
        - ``"approval"`` resolves an outstanding elicitation
          in-band (see :func:`_dispatch_approval`).
        - ``"external_assistant_message"`` appends and streams an
          assistant message observed outside the Omnigent task runtime,
          without starting or steering a task.
        - ``"external_conversation_item"`` appends and streams a
          completed item observed outside the Omnigent task runtime,
          without starting or steering a task.
        - ``"external_output_text_delta"`` publishes a transient
          ``response.output_text.delta`` event observed outside the
          Omnigent task runtime, without persisting an item or starting /
          steering a task.
        - ``"external_tool_output_delta"`` publishes transient output for
          an in-progress function call without persisting an item.
        - ``"external_output_reasoning_delta"`` publishes a transient
          ``response.reasoning_text.delta`` event (preceded by one
          ``response.reasoning.started`` when ``data.started`` is true)
          observed outside the Omnigent task runtime, without persisting an
          item or starting / steering a task.
        - ``"external_session_interrupted"`` publishes a
          ``session.interrupted`` event observed outside the Omnigent task
          runtime, without persisting an item or starting / steering a
          task.
        - ``"external_elicitation_resolved"`` marks a native
          harness-originated elicitation as resolved elsewhere so
          subscribed clients clear the pending approval card.
        - ``"external_session_status"`` publishes a terminal-observed
          ``session.status`` edge without persisting an item or
          starting/steering a task.
        - ``"external_model_change"`` persists a terminal-observed
          model switch to ``model_override`` and publishes a
          ``session.model`` SSE event so the web picker reflects it.
        - ``"external_model_options"`` records the model catalog a native
          harness's extension reported (its live model registry) into a
          reload-surviving cache and publishes ``session.model_options`` so
          the web picker populates regardless of how the harness authenticated.
        - ``"external_reasoning_effort_change"`` persists a terminal-observed
          thinking-level switch to ``reasoning_effort`` and publishes a
          ``session.reasoning_effort`` SSE event so the web picker reflects it.
        - ``"external_codex_collaboration_mode_change"`` persists the
          Codex app-server collaboration mode kind as an internal session label
          (``omnigent.codex_native.collaboration_mode``).
        - ``"stop_session"`` terminates the live session without
          deleting the conversation (owner-only). Forwarded
          harness-agnostically to the runner, which hard-kills the
          external process for harnesses that have one (claude-native
          kills its tmux pane) and 204s otherwise. Stop is non-sticky:
          it writes no persistent marker, so the next message
          auto-relaunches the session on its (still-online) host via
          the normal message-dispatch relaunch path.
        - ``"message"`` on an ``omnigent claude`` terminal session
          is forwarded to the bound runner for tmux injection only;
          the accepted prompt is persisted later when Claude records
          it in the terminal transcript.
        - Any other (item-typed) event is persisted into
          ``conversation_items`` via the legacy create-or-steer path
          (legacy persist path): if an active
          task is present, the item is delivered into its inbox;
          otherwise a new task is created and started. In both
          cases ``session.input.consumed`` fires with the persisted
          item's id.

        :param session_id: Session/conversation identifier.
        :param body: The validated :class:`SessionEventInput`.
        :returns: ``{"queued": True, "item_id": "..."}`` for
            item-typed events, where ``item_id`` is the persisted
            conversation item id also emitted by
            ``session.input.consumed``; ``{"queued": False}`` for
            control and internal transient events.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        # Validate event type at the route boundary. Anything not in
        # ``_ALLOWED_EVENT_TYPES`` is a client mistake — failing here
        # is far better than silently persisting an item the agent
        # loop will only crash on later when ``parse_item_data`` runs
        # against the payload (rule 15 — fail loud).
        if body.type not in _ALLOWED_EVENT_TYPES:
            raise OmnigentError(
                f"Unknown event type: {body.type!r}. "
                f"Allowed types: {sorted(_ALLOWED_EVENT_TYPES)}",
                code=ErrorCode.INVALID_INPUT,
            )
        # For item types, validate the data payload shape against
        # the item-type's discriminator class. The control types
        # (interrupt, approval) bypass the item-persist path and have
        # their own payload schemas — they skip this check (interrupt
        # has no payload; approval's MCP-shape payload is validated
        # inside ``_dispatch_approval``).
        if body.type not in (
            _INTERRUPT_TYPE,
            _APPROVAL_TYPE,
            _MCP_ELICITATION_TYPE,
            _COMPACT_TYPE,
            _SLASH_COMMAND_TYPE,
            _STOP_SESSION_TYPE,
            _EXTERNAL_ASSISTANT_MESSAGE_TYPE,
            _EXTERNAL_CONVERSATION_ITEM_TYPE,
            _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
            _EXTERNAL_TOOL_OUTPUT_DELTA_TYPE,
            _EXTERNAL_OUTPUT_REASONING_DELTA_TYPE,
            _EXTERNAL_SESSION_INTERRUPTED_TYPE,
            _EXTERNAL_SESSION_SUPERSEDED_TYPE,
            _EXTERNAL_ELICITATION_RESOLVED_TYPE,
            _EXTERNAL_SESSION_STATUS_TYPE,
            _EXTERNAL_SESSION_USAGE_TYPE,
            _EXTERNAL_COMPACTION_STATUS_TYPE,
            _EXTERNAL_MCP_STARTUP_TYPE,
            _EXTERNAL_MODEL_CHANGE_TYPE,
            _EXTERNAL_MODEL_OPTIONS_TYPE,
            _EXTERNAL_REASONING_EFFORT_CHANGE_TYPE,
            _EXTERNAL_SESSION_TODOS_TYPE,
            _EXTERNAL_SUBAGENT_START_TYPE,
            _EXTERNAL_CODEX_SUBAGENT_START_TYPE,
            _EXTERNAL_CODEX_COLLABORATION_MODE_CHANGE_TYPE,
        ):
            try:
                parse_item_data(body.type, {"type": body.type, **body.data})
            except (ValueError, TypeError) as exc:
                raise OmnigentError(
                    f"Invalid data payload for event type {body.type!r}: {exc}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
        # Fail fast on malformed tools at the boundary. The raw dicts
        # (not the parsed objects) are what the runner stores — the
        # parse call is purely a validator.
        if body.tools:
            try:
                parse_client_side_tool_specs(body.tools)
            except ValueError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        # ── Policy evaluation (path-agnostic) ────────────────
        # Evaluate policies BEFORE persistence/runner forwarding so
        # enforcement fires on both paths. On DENY, persist the
        # event (possibly with modified body) through whichever
        # path is active, then return the deny verdict. On ALLOW,
        # fall through to the normal persist/forward path.
        _policy_body = body  # may be replaced by OUTPUT deny
        _actor = _build_actor(user_id)
        # A closed sub-agent session (sys_session_close) rejects new user
        # input — the orchestrator must spawn a fresh session to continue.
        if (
            body.type == "message"
            and body.data.get("role") == "user"
            and is_session_closed(conv.labels, conv.title)
        ):
            raise OmnigentError(
                "Session is closed. Start a new sub-agent session to continue.",
                code=ErrorCode.CONFLICT,
            )
        if (
            body.type == "message"
            and body.data.get("role") == "user"
            and conv.agent_id is not None
        ):
            try:
                _input_verdict = await _evaluate_input_policy(
                    request,
                    session_id,
                    conv,
                    body,
                    conversation_store,
                    agent_store,
                    runner_router,
                    actor=_actor,
                )
            except Exception as _policy_exc:
                # Policy evaluation crashed (e.g. factory misconfigured).
                # Log and treat as DENY so the session doesn't hang on
                # "working" forever. The full cause is logged for admins;
                # the denial reason returned to (and streamed at) the client
                # stays generic so the raw exception text isn't exposed.
                _logger.warning(
                    "Input policy evaluation failed for %s: %s",
                    session_id,
                    _policy_exc,
                    exc_info=True,
                )
                _input_verdict = {
                    "verdict": "deny",
                    "reason": "Denied by policy (policy evaluation error).",
                }
            if _input_verdict is not None:
                # DENY or ASK — don't forward to runner. Publish a
                # deny sentinel on the session stream so the
                # client/REPL sees feedback.
                reason = _input_verdict.get("reason", "Denied by policy")
                _publish_status(session_id, "running")
                _publish_policy_deny(session_id, reason)
                await _persist_policy_deny_sentinel(
                    session_id,
                    conv,
                    reason,
                    conversation_store,
                    agent_store,
                )
                # Terminal response.completed before idle so live-tail
                # consumers (the headless ``-p`` client) unblock.
                _publish_input_deny_terminal(session_id, conv, reason)
                _publish_status(session_id, "idle")
                # Return the same shape the client expects from POST
                # /events so postEvent doesn't throw on an unexpected
                # response body. queued=False signals the event was
                # handled synchronously (denied, not queued for a turn).
                return {"queued": False, "denied": True, "reason": reason}
        elif body.type == _SLASH_COMMAND_TYPE and conv.agent_id is not None:
            _input_verdict = await _evaluate_input_policy(
                request,
                session_id,
                conv,
                _build_skill_slash_command_policy_body(body),
                conversation_store,
                agent_store,
                runner_router,
            )
            if _input_verdict is not None:
                reason = _input_verdict.get("reason", "Denied by policy")
                _publish_status(session_id, "running")
                _publish_policy_deny(session_id, reason)
                await _persist_policy_deny_sentinel(
                    session_id,
                    conv,
                    reason,
                    conversation_store,
                    agent_store,
                )
                # Terminal response.completed before idle (see message branch).
                _publish_input_deny_terminal(session_id, conv, reason)
                _publish_status(session_id, "idle")
                return {"queued": False, "denied": True, "reason": reason}
        elif (
            body.type == "message"
            and body.data.get("role") == "assistant"
            and conv.agent_id is not None
        ):
            _output_verdict = await _evaluate_output_policy(
                session_id,
                conv,
                body,
                conversation_store,
                agent_store,
                runner_router,
                actor=_actor,
            )
            if _output_verdict is not None:
                if _output_verdict.get("_denied_body") is not None:
                    _policy_body = _output_verdict["_denied_body"]
                    body = _policy_body
                # For OUTPUT DENY, fall through to persist the
                # denied body (with sentinel text). The verdict
                # is returned after persistence below.
                if _output_verdict["verdict"] == "deny":
                    pass  # fall through with modified body
                else:
                    return _output_verdict
        elif body.type == "function_call" and body.data.get("evaluate_policy"):
            _tool_verdict = await _evaluate_tool_call_policy(
                session_id,
                conv,
                body,
                conversation_store,
                agent_store,
                runner_router,
                actor=_actor,
            )
            if _tool_verdict is not None:
                return _tool_verdict
            # ALLOW — return explicit verdict so the request does
            # not fall through to the persist-and-forward path.
            # Policy evaluation requests are queries, not items to
            # persist or relay to the harness (which rejects
            # ``function_call`` as an unknown inbound event type).
            return {"verdict": "allow"}

        if body.type == _INTERRUPT_TYPE:
            _publish_interrupted(session_id)
            # Fence the cancelled turn (see _interrupt_fenced_sessions).
            _interrupt_fenced_sessions.add(session_id)
            runner_client = await _get_runner_client(
                session_id,
                runner_router,
            )
            interrupt_delivered = False
            if runner_client is not None:
                try:
                    interrupt_resp = await runner_client.post(
                        f"/v1/sessions/{session_id}/events",
                        json={"type": "interrupt"},
                        timeout=5.0,
                    )
                    interrupt_delivered = interrupt_resp.status_code < 400
                except (httpx.HTTPError, ConnectionError):
                    # WSTunnelTransport raises bare ConnectionError on tunnel close.
                    _logger.exception(
                        "Interrupt forward failed for %r",
                        session_id,
                    )
            if not interrupt_delivered:
                # The turn keeps running and nothing else lifts the fence —
                # remove it so the turn's remaining output isn't dropped.
                _interrupt_fenced_sessions.discard(session_id)
            return {"queued": False}
        if body.type == _STOP_SESSION_TYPE:
            # Terminating the whole session (not just the current turn)
            # is a lifecycle action; require owner access on top of the
            # LEVEL_EDIT gate above so a shared editor can't kill the
            # owner's session.
            await _require_access(
                user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
            )
            # Fence the cancelled turn, same as interrupt.
            _interrupt_fenced_sessions.add(session_id)
            # Harness-agnostic forward: the runner kills the external
            # process for harnesses that have one (claude-native
            # hard-kills its tmux pane) and 204s otherwise. Unlike the
            # best-effort effort/model_change relay, a failed stop means
            # the session is still alive — so this helper RAISES on a
            # non-2xx / unreachable runner (503) rather than swallowing
            # it, letting the web UI show the stop didn't land instead
            # of closing the dialog as if it succeeded.
            try:
                stop_delivered = await _stop_session_via_runner(session_id, runner_router)
            except Exception:
                # Stop didn't land: the turn keeps running, so lift the
                # fence or its remaining output is dropped forever.
                _interrupt_fenced_sessions.discard(session_id)
                raise
            if not stop_delivered:
                # No runner resolved: nothing else lifts the fence (same as interrupt).
                _interrupt_fenced_sessions.discard(session_id)
            # Host-spawned sessions run on a dedicated runner the host
            # launched for this one session. Killing the pane (above) leaves
            # that runner connected, so GET /health keeps reporting
            # runner_online: true and the web UI never shows the session as
            # disconnected — new messages hang on "working" against a dead
            # pane. Stop the runner too so its tunnel drops and the web UI
            # shows the same "Agent disconnected — click to show reconnect
            # command" banner a CLI-launched session reaches on exit. Read
            # host_id / runner_id from the owner-gated session row so we can
            # only ever stop the runner bound to this session.
            stop_conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if stop_conv is not None and stop_conv.host_id and stop_conv.runner_id:
                # Mark the tunnel drop as intentional BEFORE tearing it down so
                # the relay's disconnect handler renders a quiet stopped state
                # rather than "Error · runner_disconnected". Only host-spawned
                # sessions drop the tunnel on Stop; other harnesses leave the
                # runner connected, so there is nothing to suppress for them.
                _intentional_stop_sessions.add(session_id)
                teardown_delivered = await _stop_session_host_runner(
                    session_id,
                    stop_conv.host_id,
                    stop_conv.runner_id,
                    getattr(request.app.state, "host_registry", None),
                )
                if not teardown_delivered:
                    # Best-effort stop did not land (host offline / timeout /
                    # failure): no tunnel drop will follow, so the relay won't
                    # reach the disconnect handler that consumes the marker.
                    # Discard it now so it can't outlive this turn on the
                    # reused per-session relay task and later swallow a genuine
                    # runner_disconnected as a quiet idle.
                    _intentional_stop_sessions.discard(session_id)
            # Stop is non-sticky: no persistent marker is written. The
            # runner tunnel dropping above flips ``runner_online`` to false
            # honestly, and the next message auto-relaunches the session on
            # its (still-online) host via the normal message-dispatch
            # relaunch path below.
            try:
                import hashlib as _hashlib

                _srv_id = _get_installation_id()
                _anon: str | None = None
                if user_id is not None:
                    _salt = f"{_srv_id}:{user_id}" if _srv_id else user_id
                    _anon = _hashlib.sha256(_salt.encode()).hexdigest()[:16]
                _tel_emit(
                    _TelSessionStoppedEvent(
                        session_id=session_id,
                        installation_id=_srv_id,
                        anon_user_id=_anon,
                    )
                )
            except Exception:
                pass
            return {"queued": False}
        if body.type == _APPROVAL_TYPE:
            # Deliver the verdict through the shared resolver: it
            # sets any server-side harness Future (owner-checked),
            # clears the sidebar badge, and forwards
            # to the runner for runner-side (policy) elicitations.
            # The dedicated URL endpoint (``.../elicitations/{eid}/
            # resolve``) routes through the same helper.
            await _resolve_elicitation(session_id, body.data, runner_router, conversation_store)
            # Apply any policy writes deferred by the relay tool-call ASK gate
            # (e.g. a cost-budget checkpoint) now that the verdict is in.
            await _apply_pending_policy_ask_writes(
                session_id, conv, conversation_store, agent_store, body.data
            )
            return {"queued": False}
        if body.type == _MCP_ELICITATION_TYPE:
            # The runner's inline MCP elicitation callback fires when
            # an external MCP server sends ``elicitation/create``
            # during a ``tools/call``. Publish the elicitation as an
            # SSE event (approval card in web UI, y/a/n prompt in
            # REPL) and return the elicitation_id immediately so the
            # runner can park on ``pending_approvals``. The user's
            # verdict arrives later via ``type: "approval"`` →
            # ``_resolve_elicitation`` → ``_forward_approval_to_runner``
            # → runner's ``pending_approvals`` resolves.
            elicit_data = body.data or {}
            elicit_id = f"elicit_{secrets.token_hex(16)}"
            elicit_params = ElicitationRequestParams(
                mode="form",
                message=elicit_data.get("message", ""),
                requestedSchema=elicit_data.get("requestedSchema"),
            )
            event = ElicitationRequestEvent(
                type="response.elicitation_request",
                elicitation_id=elicit_id,
                params=elicit_params,
            )
            _mcp_elicit_payload = event.model_dump()
            from omnigent.server.routes import sessions as _sessions_facade

            _sessions_facade.session_stream.publish(session_id, _mcp_elicit_payload)
            # Mirror the prompt into ancestor streams so a sub-agent MCP
            # elicitation surfaces in the parent (polly) chat with a
            # ``target_session_id`` pointing back at this child. The
            # verdict still arrives via the generic ``approval`` event,
            # which mirrors the resolved signal back up through
            # ``_resolve_elicitation``.
            await asyncio.to_thread(
                _publish_elicitation_request_to_ancestors,
                conversation_store,
                session_id,
                _mcp_elicit_payload,
            )
            return {"queued": False, "elicitation_id": elicit_id}
        if body.type == _COMPACT_TYPE:
            # Unified control dispatch (designs/CLAUDE_NATIVE.md
            # "Control events dispatch on the runner"): forward /compact
            # to the bound runner first, regardless of harness. The
            # runner dispatches by harness — claude-native injects
            # /compact into the tmux pane so Claude Code compacts its
            # own context and returns 200; other harnesses 204 no-op.
            # The Omnigent server stays harness-agnostic: it runs its own
            # in-process compaction only when the runner did NOT handle
            # the control (204 / no runner bound). A 4xx/5xx from the
            # runner (e.g. 503 when the claude-native pane isn't
            # attached) is surfaced as an error rather than silently
            # falling through to AP-side compaction, which would be
            # wrong for a terminal-owned session.
            runner_result = await _forward_session_change_to_runner(
                session_id,
                runner_router,
                {"type": _COMPACT_TYPE},
            )
            if runner_result is not None and runner_result.status_code == 200:
                return {"queued": False}
            if runner_result is not None and runner_result.status_code != 204:
                raise OmnigentError(
                    f"Compaction failed: runner returned {runner_result.status_code}",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            await _run_compact_locked(
                session_id,
                conv,
                agent_store,
                agent_cache,
            )
            return {"queued": False}
        if body.type == "compaction":
            import uuid as _uuid

            item = NewConversationItem(
                type="compaction",
                response_id=f"compact_{_uuid.uuid4().hex}",
                data=parse_item_data("compaction", body.data),
            )
            await asyncio.to_thread(
                conversation_store.append,
                session_id,
                [item],
            )
            return {"queued": True}
        if body.type == _EXTERNAL_ASSISTANT_MESSAGE_TYPE:
            item_id = await _persist_external_assistant_message(
                session_id,
                body,
                conversation_store,
            )
            return {"queued": False, "item_id": item_id}
        if body.type == _EXTERNAL_CONVERSATION_ITEM_TYPE:
            item_id = await _persist_external_conversation_item(
                session_id,
                conv,
                body,
                conversation_store,
                created_by=_attribution_user(user_id),
                background_title_coordinator=background_title_coordinator,
            )
            return {"queued": False, "item_id": item_id}
        if body.type == _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE:
            _publish_external_output_text_delta(session_id, body)
            return {"queued": False}
        if body.type == _EXTERNAL_TOOL_OUTPUT_DELTA_TYPE:
            _publish_external_tool_output_delta(session_id, body)
            return {"queued": False}
        if body.type == _EXTERNAL_OUTPUT_REASONING_DELTA_TYPE:
            _publish_external_output_reasoning_delta(session_id, body)
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_INTERRUPTED_TYPE:
            response_id = body.data.get("response_id")
            if response_id is not None and not isinstance(response_id, str):
                raise OmnigentError(
                    "external_session_interrupted data.response_id must be a string",
                    code=ErrorCode.INVALID_INPUT,
                )
            _publish_interrupted(session_id, response_id=response_id)
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_SUPERSEDED_TYPE:
            target_conversation_id = body.data.get("target_conversation_id")
            if not isinstance(target_conversation_id, str) or not target_conversation_id.strip():
                raise OmnigentError(
                    "external_session_superseded requires a non-empty string "
                    "data.target_conversation_id",
                    code=ErrorCode.INVALID_INPUT,
                )
            _publish_session_superseded(session_id, target_conversation_id.strip())
            return {"queued": False}
        if body.type == _EXTERNAL_ELICITATION_RESOLVED_TYPE:
            elicitation_id = body.data.get("elicitation_id")
            if not isinstance(elicitation_id, str):
                raise OmnigentError(
                    "external_elicitation_resolved requires string data.elicitation_id.",
                    code=ErrorCode.INVALID_INPUT,
                )
            _signal_harness_elicitation_resolved_by_id(session_id, elicitation_id)
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_STATUS_TYPE:
            status = body.data.get("status")
            if status not in _EXTERNAL_SESSION_STATUS_VALUES:
                raise OmnigentError(
                    f"external_session_status requires data.status in "
                    f"{sorted(_EXTERNAL_SESSION_STATUS_VALUES)}; got {status!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            response_id = body.data.get("response_id")
            if response_id is not None and not isinstance(response_id, str):
                raise OmnigentError(
                    "external_session_status data.response_id must be a string",
                    code=ErrorCode.INVALID_INPUT,
                )
            # Surface the failure reason a native forwarder carries so a
            # top-level session sees it on its own status edge and persisted
            # last_task_error, not only the sub-agent parent-inbox path.
            output = body.data.get("output")
            status_error: ErrorDetail | None = None
            if status == "failed" and isinstance(output, str) and output.strip():
                status_error = ErrorDetail(
                    code=(
                        "codex_reauth_required"
                        if body.data.get("reauth_required") is True
                        else "codex_turn_error"
                    ),
                    message=output.strip(),
                )
            if status_error is not None:
                await _persist_session_status_error_labels(
                    session_id, status_error, conversation_store
                )
            elif status == "running":
                await _persist_session_status_error_labels(session_id, None, conversation_store)
            # ``None`` (field absent) = no information; leave the sticky
            # tally untouched (the PTY-activity ``idle`` carries none). An
            # explicit ``0`` from a ``Stop`` hook is authoritative and clears
            # the tally, so a finished background shell drops the indicator.
            raw_bg_count = body.data.get("background_task_count")
            bg_count = (
                raw_bg_count
                if isinstance(raw_bg_count, int)
                and not isinstance(raw_bg_count, bool)
                and raw_bg_count >= 0
                else None
            )
            # A sub-agent's background-task ``waiting`` must deliver as ``idle``
            # so the parent's terminal-delivery branch below fires (otherwise
            # the orchestrator hangs); the tally still drives the child spinner.
            effective_status = _subagent_delivery_status(status, bg_count, conv)
            if effective_status != status:
                status = effective_status
                body.data["status"] = status
            _publish_status(
                session_id,
                status,
                status_error,
                response_id=response_id,
                background_task_count=bg_count,
            )
            forward_body = body.model_dump()
            forward_body["data"] = await _enrich_idle_status_with_subagent_output(
                forward_body["data"], status, session_id, conversation_store
            )
            runner_result = await _forward_session_change_to_runner(
                session_id,
                runner_router,
                forward_body,
            )
            if (
                conv.kind == "sub_agent"
                and status in {"idle", "failed"}
                and not _is_codex_native_subagent(conv)
            ):
                # Codex-internal children are tracked inside the same
                # app-server thread tree; they have no runner inbox entry
                # to forward terminal status to.
                if runner_result is None:
                    # The child's pinned runner_id is stale — its runner was
                    # relaunched under a new id and only the parent was
                    # rebound, so the child points at a dead runner forever and
                    # this terminal status would 503 indefinitely while the
                    # parent hangs waiting for the child's inbox result. Heal
                    # the binding and re-deliver through the parent's live
                    # runner before failing.
                    from omnigent.server.routes import sessions as _sf

                    recovered = await _sf._recover_subagent_status_forward_via_parent(
                        conv,
                        runner_router,
                        getattr(request.app.state, "tunnel_registry", None),
                        conversation_store,
                        forward_body,
                    )
                    if recovered is not None:
                        runner_result = recovered
                _require_external_status_forward(
                    session_id,
                    status,
                    runner_result,
                )
            return {"queued": False}
        if body.type == _EXTERNAL_COMPACTION_STATUS_TYPE:
            # Terminal-observed compaction edge (claude-native forwarder):
            # republish as the standard compaction SSE so the web UI
            # spinner brackets Claude's real terminal compaction. No token
            # count is available here — the context ring is updated
            # separately by external_session_usage — so completed carries
            # total_tokens=None.
            compaction_status = body.data.get("status")
            if compaction_status not in _EXTERNAL_COMPACTION_STATUS_VALUES:
                raise OmnigentError(
                    f"external_compaction_status requires data.status in "
                    f"{sorted(_EXTERNAL_COMPACTION_STATUS_VALUES)}; got {compaction_status!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            if compaction_status == "in_progress":
                _publish_compaction_in_progress(session_id)
            elif compaction_status == "completed":
                _publish_compaction_completed(session_id, None)
            else:
                _publish_compaction_failed(session_id)
            return {"queued": False}
        if body.type == _EXTERNAL_MCP_STARTUP_TYPE:
            # Harness MCP-server startup progress (codex-native forwarder):
            # republish as a ``session.mcp_startup`` SSE so the web UI shows
            # per-server startup state while the harness boots. Malformed
            # entries are rejected at the boundary — a bogus map would only
            # strand the UI's startup band.
            raw_servers = body.data.get("servers")
            if not isinstance(raw_servers, dict):
                raise OmnigentError(
                    "external_mcp_startup requires data.servers to be an object "
                    f"mapping server names to startup records; got {raw_servers!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            mcp_servers: dict[str, McpServerStartup] = {}
            for server_name, record in raw_servers.items():
                record_status = record.get("status") if isinstance(record, dict) else None
                if not (
                    isinstance(server_name, str)
                    and server_name
                    and record_status in _EXTERNAL_MCP_STARTUP_STATUS_VALUES
                ):
                    raise OmnigentError(
                        "external_mcp_startup server records require a status in "
                        f"{sorted(_EXTERNAL_MCP_STARTUP_STATUS_VALUES)}; got "
                        f"{server_name!r}: {record!r}",
                        code=ErrorCode.INVALID_INPUT,
                    )
                record_error = record.get("error")
                mcp_servers[server_name] = McpServerStartup(
                    status=record_status,
                    error=record_error if isinstance(record_error, str) and record_error else None,
                )
            _publish_mcp_startup(session_id, mcp_servers)
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_USAGE_TYPE:
            # Persist the harness-reported cumulative usage so the
            # tool-call cost gate can read the running
            # ``total_cost_usd`` on the next tool call. (Cost budgets
            # now enforce at ``tool_call`` via the PreToolUse hook, not
            # post-hoc here — a logged output cannot be un-logged.)
            await _persist_external_session_usage(
                session_id,
                body,
                conversation_store,
            )
            return {"queued": False}
        if body.type == _EXTERNAL_MODEL_CHANGE_TYPE:
            await _persist_external_model_change(
                session_id,
                conv,
                body,
                conversation_store,
            )
            return {"queued": False}
        if body.type == _EXTERNAL_MODEL_OPTIONS_TYPE:
            _persist_external_model_options(session_id, conv, body)
            return {"queued": False}
        if body.type == _EXTERNAL_REASONING_EFFORT_CHANGE_TYPE:
            await _persist_external_reasoning_effort_change(
                session_id,
                conv,
                body,
                conversation_store,
            )
            return {"queued": False}
        if body.type == _EXTERNAL_CODEX_COLLABORATION_MODE_CHANGE_TYPE:
            await _persist_external_codex_collaboration_mode_change(
                session_id,
                conv,
                body,
                conversation_store,
            )
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_TODOS_TYPE:
            _handle_external_session_todos(session_id, body)
            return {"queued": False}
        if body.type == _EXTERNAL_SUBAGENT_START_TYPE:
            child_id = await _persist_external_subagent_start(
                session_id,
                conv,
                body,
                conversation_store,
            )
            # Returned to the claude-native forwarder so it can address
            # subsequent ``external_conversation_item`` /
            # ``external_session_status`` events to the child id.
            return {"queued": False, "child_session_id": child_id}
        if body.type == _EXTERNAL_CODEX_SUBAGENT_START_TYPE:
            child_id = await _persist_external_codex_subagent_start(
                session_id,
                conv,
                body,
                conversation_store,
            )
            return {"queued": False, "child_session_id": child_id}
        if body.type == "function_call_output":
            # A client-side tool's result tunneling back to a parked turn.
            # The harness scaffold resolves the parked tool Future on a
            # ``tool_result`` event (ToolResultEvent {call_id, output}), so
            # translate the session-API ``function_call_output`` into that
            # wire shape and forward to the bound runner, which relays it
            # verbatim to the parked harness. Mirrors the runner's own
            # dispatch_tool_locally tool_result post; the output here came
            # from the caller (a client-side tool) instead of a local
            # dispatch. ``parse_item_data`` above already validated the
            # payload against ``FunctionCallOutputData`` (call_id: str,
            # output: str), so both fields are present strings. Stale
            # call_ids no-op at the scaffold; the harness re-emits the
            # completed function_call + output on resume, so history is
            # written through the normal stream path (no separate persist).
            runner_client = await _get_runner_client(session_id, runner_router)
            if runner_client is None:
                raise OmnigentError(
                    "No runner bound to this session; cannot deliver the tool result.",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                )
            try:
                await runner_client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "tool_result",
                        "call_id": body.data["call_id"],
                        "output": body.data["output"],
                    },
                    timeout=10.0,
                )
            except (httpx.HTTPError, ConnectionError) as exc:
                # Fail loud (503), not best-effort: unlike the advisory
                # interrupt-forward, a dropped tool_result leaves the parked
                # turn hanging until it times out. Surfacing the failure lets
                # the caller retry the delivery (the scaffold no-ops if a
                # retry double-delivers a now-stale call_id).
                raise OmnigentError(
                    "Failed to deliver the tool result to the session runner.",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                ) from exc
            return {"queued": True, "item_id": body.data["call_id"]}
        # Whether the runner was initially unavailable or was woken below. In
        # that case the session-init handshake may still be racing the first
        # message, even if we reused the original binding instead of launching
        # a replacement.
        _runner_needs_session_init = False
        # Item event (message, function_call_output, etc.).
        if conv.host_id is not None and await _maybe_wake_stale_resumable_managed_sandbox(
            session_id=session_id,
            conv=conv,
            app_state=request.app.state,
            conversation_store=conversation_store,
        ):
            # A resumable managed wake may have re-launched the runner and
            # updated liveness while this handler was holding an old row.
            conv_after_wake = await asyncio.to_thread(
                conversation_store.get_conversation,
                session_id,
            )
            if conv_after_wake is None:
                raise _session_not_found()
            conv = conv_after_wake
            _runner_needs_session_init = True
        runner_client = await _get_runner_client(session_id, runner_router)
        # Managed-launch rendezvous: a ``host_type="managed"`` create
        # returns before the sandbox exists, so the first message (the
        # Web UI auto-sends the composer prompt right after navigate)
        # can land while the background provision is still running.
        # Instead of failing with "no runner bound", wait for the
        # launch to settle: success leaves the session host-bound with
        # its runner tunnel already up (the background task awaits
        # it), failure surfaces the recorded reason.
        if runner_client is None and conv.host_id is None:
            _managed_tracker = getattr(request.app.state, "managed_launches", None)
            _managed_launch = (
                _managed_tracker.get(session_id) if _managed_tracker is not None else None
            )
            if _managed_launch is not None:
                await _await_settled_managed_launch(_managed_launch)
                # The launch bound host_id / workspace / runner_id to
                # the row after this handler's fetch — re-read so the
                # resolution below sees the bound runner.
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if conv is None:
                    raise _session_not_found()
                runner_client = await _get_runner_client(session_id, runner_router)
        if runner_client is None and conv.host_id is not None:
            _tunnel_registry = getattr(request.app.state, "tunnel_registry", None)
            _grace_host_reg = getattr(request.app.state, "host_registry", None)
            _grace_host_conn = (
                _grace_host_reg.get(conv.host_id) if _grace_host_reg is not None else None
            )
            # A just-created host session already has a runner_id before
            # the runner's tunnel is registered. The Web UI can post the
            # first message during that gap; wait briefly for the pinned
            # runner before treating it as dead and replacing it — but end
            # that wait early when the runner is not actually coming. The
            # host owns runner-process liveness (it holds the Popen), so we
            # race a ``host.runner_status`` query against the connect grace:
            # a booting runner connects (or reads "alive") and we forward,
            # while one that was stopped, crashed, or lost to a host restart
            # reads "dead"/"unknown" and cuts the wait short so the relaunch
            # below runs at once. A host that is offline, too old to answer,
            # or slow yields no verdict and the grace runs its normal
            # course, so the query only ever speeds up the cold path.
            from omnigent.server.routes import sessions as _sf

            if conv.runner_id is not None and _sf._HOST_BOUND_RUNNER_CONNECT_GRACE_S > 0:
                _logger.info(
                    "Waiting up to %.1fs for host-bound runner %s to register "
                    "for session %s before relaunch",
                    _sf._HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                    conv.runner_id,
                    session_id,
                )
                if _grace_host_conn is not None:
                    runner_client = await _wait_for_host_bound_runner_client(
                        session_id,
                        runner_router,
                        _tunnel_registry,
                        runner_id=conv.runner_id,
                        timeout_s=_sf._HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                        runner_exit_reports=runner_exit_reports,
                        host_conn=_grace_host_conn,
                        host_registry=_grace_host_reg,
                    )
                else:
                    # Host tunnel absent: no one to query, so this is the
                    # plain connect grace (unchanged pre-existing behavior).
                    runner_client = await _wait_for_runner_client(
                        session_id,
                        runner_router,
                        _tunnel_registry,
                        runner_id=conv.runner_id,
                        timeout_s=_sf._HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                        runner_exit_reports=runner_exit_reports,
                    )
            # Runner is dead or still not spawned for a host-bound
            # session. Ask the host to launch one, then re-fetch the
            # runner client and wait briefly for it to connect before
            # forwarding the message. This is the relaunch path a
            # non-sticky Stop relies on: after Stop drops the runner
            # tunnel, the next message lands here and relaunches the
            # session on its still-online host. Gated only on host
            # presence — if the host is offline this falls through to
            # the RUNNER_UNAVAILABLE raise below, the same as a
            # disconnected CLI session.
            _host_reg = getattr(request.app.state, "host_registry", None)
            if runner_client is None and _host_reg is not None:
                _host_conn = _host_reg.get(conv.host_id)
                if _host_conn is not None:
                    launch_attempt = await _launch_runner_on_host(
                        conv,
                        conversation_store,
                        _host_reg,
                        _host_conn,
                    )
                    if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
                        # The host refused: the agent's harness isn't
                        # configured there. This message was the real
                        # runner-start attempt, so consume it and record a
                        # transcript error (the host's message names the
                        # fix, `omnigent setup`) the web renders as a
                        # banner — instead of timing out into a generic
                        # RUNNER_UNAVAILABLE. The binding stays so a later
                        # message relaunches once setup is done.
                        item_id = await _persist_host_launch_failure_turn(
                            session_id,
                            conv,
                            body,
                            conversation_store,
                            launch_attempt.error,
                            runner_router,
                            created_by=_attribution_user(user_id),
                        )
                        return {"queued": True, "item_id": item_id}
                    relaunched_runner_id = launch_attempt.runner_id
                else:
                    relaunched_runner_id = None
                    # The host tunnel is gone entirely. A managed
                    # host's sandbox is relaunchable — provision a new
                    # generation under the same host identity and ride
                    # it; an external (laptop) host falls through to
                    # the unavailable raise below.
                    if await _maybe_relaunch_managed_sandbox(
                        session_id=session_id,
                        conv=conv,
                        app_state=request.app.state,
                        conversation_store=conversation_store,
                    ):
                        conv_after_relaunch = await asyncio.to_thread(
                            conversation_store.get_conversation, session_id
                        )
                        if conv_after_relaunch is None:
                            raise _session_not_found()
                        conv = conv_after_relaunch
                        runner_client = await _get_runner_client(session_id, runner_router)
            else:
                relaunched_runner_id = None
            if runner_client is None:
                _logger.info(
                    "Waiting up to %.0fs for host %s to spawn a runner for session %s",
                    _HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
                    conv.host_id,
                    session_id,
                )
                runner_client = await _wait_for_runner_client(
                    session_id,
                    runner_router,
                    _tunnel_registry,
                    runner_id=relaunched_runner_id,
                    timeout_s=_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
                    runner_exit_reports=runner_exit_reports,
                )
            if runner_client is None:
                _runner_needs_session_init = False
            else:
                _runner_needs_session_init = True
        if runner_client is None:
            # A native terminal-session message must NOT be silently
            # dropped when no runner is reachable — the runner crashed
            # before connecting (the daemon couldn't bring it up). Persist
            # the user's message together with the runner-failure error so
            # it survives reload and the banner explains why, becoming the
            # AP-server-as-writer failed turn (same shape as a definitive
            # ensure-probe failure). The cause, when known, is the daemon's
            # exit report keyed by this session's runner_id; otherwise a
            # generic unavailable message. This is safe precisely because
            # the harness will never see it (no desync — there is no live
            # harness). Other event types and non-native sessions still
            # raise: their message would replay to a relaunched runner, so
            # persisting now WOULD desync the store from harness state.
            if body.type == "message" and _is_native_terminal_session(conv):
                exit_cause = (
                    runner_exit_reports.get(conv.runner_id)
                    if runner_exit_reports is not None and conv.runner_id is not None
                    else None
                )
                offline_error = ErrorData(
                    source="execution",
                    code="runner_failed_to_start",
                    message=(
                        exit_cause
                        if exit_cause
                        else (
                            "The runner for this session is not available — "
                            "it may have failed to start. See the host logs."
                        )
                    ),
                )
                item_id = await _persist_native_terminal_failure(
                    session_id,
                    conv,
                    body,
                    conversation_store,
                    offline_error,
                    runner_router,
                    created_by=_attribution_user(user_id),
                )
                return {"queued": True, "item_id": item_id}
            # Raise so the Omnigent server doesn't persist an item the
            # harness will never see. Other event paths (interrupt,
            # approval) are best-effort and silently skip when no
            # runner is bound — item events can't, because that
            # would desync conversation store and harness state.
            raise OmnigentError(
                "No runner bound for session",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        refreshed_conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if refreshed_conv is None:
            raise _session_not_found()
        conv = refreshed_conv
        native_terminal_ready = False
        if _runner_needs_session_init:
            # The runner was unavailable when this request began, so its
            # connect callback may still be racing us. Await the handshake
            # so the terminal + transcript forwarder are watching before we
            # inject the message — otherwise a native web message is
            # forwarded into a TUI whose forwarder isn't attached, the
            # round-trip never mirrors back, and the optimistic bubble
            # sticks with no reply (host-restart bug).
            native_terminal_ready = await _ensure_runner_session_initialized(
                session_id,
                conv,
                runner_client,
                conversation_store,
                initializer=getattr(request.app.state, "runner_session_initializer", None),
            )
        await _ensure_runner_relay_ready(
            session_id,
            conv.runner_id,
            runner_client,
            conversation_store,
        )
        _agent = agent_store.get(conv.agent_id) if conv.agent_id else None
        # Determine whether the agent has MCP servers so the runner's
        # proxy_stream handler knows to initialise ProxyMcpManager.
        # agent_cache.load() is O(1) on a warm in-memory cache; the
        # asyncio.to_thread wrapper covers the rare cold-cache path
        # where the bundle is extracted from disk for the first time.
        _has_mcp_servers = False
        if _agent is not None and agent_cache is not None and _agent.bundle_location:
            try:
                _loaded_agent = await asyncio.to_thread(
                    agent_cache.load,
                    _agent.id,
                    _agent.bundle_location,
                )
                _has_mcp_servers = bool(_loaded_agent.spec.mcp_servers)
            except Exception:
                _logger.warning(
                    "Failed to load agent spec for MCP hint for session=%s",
                    session_id,
                    exc_info=True,
                )
        pending_background_title = prepare_background_session_title(
            coordinator=background_title_coordinator,
            conversation=conv,
            event=body,
        )
        if body.type == _SLASH_COMMAND_TYPE:
            if _agent is None:
                raise OmnigentError(
                    f"Session {session_id!r} has no agent; cannot run slash command",
                    code=ErrorCode.INVALID_INPUT,
                )
            item_id = await _dispatch_skill_slash_command_to_runner(
                session_id,
                conv,
                body,
                conversation_store,
                runner_client,
                agent=_agent,
                has_mcp_servers=_has_mcp_servers,
                created_by=_attribution_user(user_id),
            )
            if pending_background_title is not None:
                pending_background_title.schedule()
            return {"queued": True, "item_id": item_id}
        dispatch = await _dispatch_session_event_to_runner(
            session_id,
            conv,
            body,
            conversation_store,
            runner_client,
            agent_name=_agent.name if _agent else None,
            file_store=file_store,
            artifact_store=artifact_store,
            has_mcp_servers=_has_mcp_servers,
            created_by=_attribution_user(user_id),
            runner_router=runner_router,
            native_terminal_ready=native_terminal_ready,
        )
        if pending_background_title is not None:
            pending_background_title.schedule()
        response: dict[str, Any] = {"queued": True}
        if dispatch.item_id is not None:
            response["item_id"] = dispatch.item_id
        # Native-terminal web message: hand back the pending-input id. It
        # identifies the snapshot's replayed bubble on rebind and is the
        # cleared_pending_id the consume event carries to drop it. Clients
        # may adopt it onto their optimistic bubble for id-based dedupe;
        # the first-party web client keeps its client temp id (React-key
        # stability) and relies on stableKey + FIFO instead.
        if dispatch.pending_id is not None:
            response["pending_id"] = dispatch.pending_id
        return response

    # ── GET /sessions/{session_id}/stream ────────────────────────

    # Live-tail only. Clients reconnect via GET /v1/sessions/{id}
    # for snapshot, then open a new stream; events that fire
    # between are deduped client-side by item id (see API.md).
    @router.get(
        "/sessions/{session_id}/stream",
        # response_model=None: returns StreamingResponse, not a model.
        response_model=None,
        # responses=: surface the SSE union to OpenAPI. The
        # ``text/event-stream`` content entry's schema points at the
        # discriminated union so generated clients know what to
        # expect on the wire. ``scripts/dump_openapi.py`` rewrites
        # this in OpenAPI 3.2's ``itemSchema`` form (the OAS 3.2
        # mechanism for typing each item in a sequential stream)
        # before writing ``openapi.json`` to disk.
        responses={
            200: {
                "description": ("SSE stream of :data:`ServerStreamEvent` frames for the session."),
                "content": {
                    "text/event-stream": {
                        "schema": {"$ref": "#/components/schemas/ServerStreamEvent"},
                    },
                },
            },
        },
    )
    async def stream_session(
        request: Request,
        session_id: str,
        idle: bool = False,
    ) -> StreamingResponse:
        """
        Subscribe to the session's live SSE event stream.

        Does NOT replay history; clients reconcile via the snapshot
        endpoint. The generator emits ``[DONE]`` on normal completion
        and uses ``finally`` only for presence cleanup — see
        :func:`_stream_live_events`.

        Holding this stream open registers the caller as a session
        *viewer* (presence): co-viewers' streams receive
        ``session.presence`` events on join/leave/idle edges, and
        this stream's snapshot-on-connect includes the current
        viewer list. Presence is scoped to the session tree's root
        conversation, so viewers of different agents/sub-agents in
        one session see each other. See
        ``omnigent/server/presence.py``.

        :param request: The FastAPI request, used to detect
            disconnect.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param idle: Presence idle flag computed by the web client
            at connect time (tab backgrounded ≥ its debounce). An
            idle *flip* mid-view arrives as a reconnect carrying the
            new value — there is no separate update endpoint.
        :returns: An SSE :class:`StreamingResponse`.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        runner_client = await _get_runner_client(
            session_id,
            runner_router,
        )
        await _ensure_runner_relay_ready(
            session_id,
            conv.runner_id,
            runner_client,
            conversation_store,
        )

        async def _resource_snapshot() -> list[dict[str, Any]]:
            """Gather current resource state to emit as snapshot-on-connect.

            Best-effort: every runner-touching gather is time-boxed and
            guarded so a slow/unavailable runner never blocks the live
            tail. Terminals arrive as ``session.resource.created`` (the
            same shape the web's live handler already consumes); child
            sessions as ``session.child_session.updated``; changed files
            as a single invalidate that triggers a client refetch.

            The in-flight assistant-text replay is NOT read here: it is
            dedup-sensitive and must be captured synchronously at slot
            registration via ``subscribe``'s ``pre_ready_snapshot`` hook,
            before ``ready_event`` suspends. The resource
            gathers below need awaits and are not dedup-sensitive, so they
            stay in this async hook.
            """
            events: list[dict[str, Any]] = []
            try:
                page = await asyncio.to_thread(
                    conversation_store.list_conversations,
                    limit=100,
                    kind="sub_agent",
                    parent_conversation_id=session_id,
                    order="desc",
                    sort_by="created_at",
                )
                summaries = await _child_session_summaries_from_conversations(
                    page.data,
                    session_id,
                    conversation_store,
                )
                for summary in summaries:
                    events.append(
                        {
                            "type": "session.child_session.updated",
                            "conversation_id": session_id,
                            "child_session_id": summary.id,
                            "child": summary.model_dump(mode="json"),
                        }
                    )
            except Exception:
                _logger.debug("snapshot: child sessions failed for %s", session_id, exc_info=True)
            if runner_client is not None:
                try:
                    resp = await asyncio.wait_for(
                        # order=asc: the web cache appends each replayed
                        # ``created`` event, so the replay must arrive in
                        # creation order or the session's own terminal (always
                        # created first) lands behind later agent-launched
                        # ones. limit=1000 (the runner endpoint max) keeps the
                        # oldest-first window from dropping the newest
                        # terminals past the default page of 20.
                        runner_client.get(
                            f"/v1/sessions/{session_id}/resources/terminals",
                            params={"order": "asc", "limit": "1000"},
                        ),
                        timeout=_SNAPSHOT_RUNNER_TIMEOUT_S,
                    )
                    if resp.status_code == 200:
                        for item in resp.json().get("data", []):
                            events.append({"type": "session.resource.created", "resource": item})
                except Exception:
                    _logger.debug("snapshot: terminals failed for %s", session_id, exc_info=True)
            # Tell the client to (re)fetch the changed-files list rather
            # than fetching it here (avoids a second runner round-trip).
            events.append(
                {
                    "type": "session.changed_files.invalidated",
                    "session_id": session_id,
                    "environment_id": "default",
                }
            )
            # Current viewer list (full state, includes this stream's own
            # registration) so a joiner never waits for the next presence
            # edge to learn who's here. Scoped to the session tree's root
            # so a sub-agent page sees viewers of every agent in the tree.
            events.append(presence.snapshot(conv.root_conversation_id, session_id))
            return events

        return StreamingResponse(
            _stream_live_events(
                request,
                session_id,
                _resource_snapshot,
                # Presence tracks distinct human actors only — the reserved
                # single-user "local" sentinel maps to None (no tracking),
                # same as message attribution.
                viewer_user_id=_attribution_user(user_id),
                viewer_idle=idle,
                # Scope presence to the tree's root: sub-agent pages open
                # the CHILD conversation's stream, and per-conversation
                # scoping would hide co-viewers on other agents.
                presence_root_id=conv.root_conversation_id,
            ),
            media_type="text/event-stream",
            headers={
                # Keep intermediaries from buffering the SSE stream:
                # ``X-Accel-Buffering: no`` disables nginx-style response
                # buffering so heartbeats and deltas reach the client as
                # they're written (a buffered proxy can delay the 15s
                # heartbeat past a client/idle timeout), and ``no-cache``
                # keeps the long-lived response out of any shared cache.
                # NOTE: this does NOT defeat the Databricks Apps ingress'
                # hard ~5-min HTTP/2 stream-duration cap — that drop is
                # handled by the client's transparent reconnect.
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ── DELETE /sessions/{session_id} ──────────────────────────────

    @router.delete(
        "/sessions/{session_id}",
        response_model=None,
        responses={200: {"model": ConversationDeleted}},
    )
    async def delete_session(
        request: Request,
        session_id: str,
        delete_branch: bool = False,
    ) -> ConversationDeleted:
        """Delete a session and all associated resources.

        Requires owner-level access. Tears down tasks, runner-side
        resources (environments, terminals), session files, and the
        conversation row.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param delete_branch: Opt-in git cleanup, as a query param
            (``?delete_branch=true``). When ``True`` and the session
            has a server-created worktree (``git_branch`` set), the
            host removes the worktree directory and deletes its branch
            (``git worktree remove --force`` then ``git branch -D``).
            Ignored for sessions with no worktree. Best-effort: a
            cleanup failure does not block the delete. Defaults to
            ``False`` (worktree and branch left untouched). See
            designs/SESSION_GIT_WORKTREE.md.
        :returns: A :class:`ConversationDeleted` confirmation.
        :raises OmnigentError: 404 if no session or no access,
            403 if insufficient permissions.
        """
        user_id = _require_user(request, auth_provider)
        if permission_store is not None and user_id is not None:
            is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
            if not is_admin:
                grant = await asyncio.to_thread(permission_store.get, user_id, session_id)
                if grant is None or grant.level < LEVEL_OWNER:
                    if grant is not None:
                        raise OmnigentError(
                            "Only the session owner can delete this session",
                            code=ErrorCode.FORBIDDEN,
                        )
                    raise OmnigentError(
                        "Conversation not found",
                        code=ErrorCode.NOT_FOUND,
                    )
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise _session_not_found()
        await _best_effort_stop(session_id, conversation_store, runner_router)
        # Runner-side resource cleanup is best-effort: if the bound
        # runner is offline or unbound, the session must still be
        # deletable. Server-owned records (files and conversation row
        # below) live independently of the runner, and runner-side
        # resources are gone with the runner anyway.
        runner_client: httpx.AsyncClient | None = None
        try:
            runner_client = await _get_runner_client_for_resource_access(session_id)
        except OmnigentError as exc:
            _logger.info(
                "Skipping runner-side cleanup for %s; proceeding with server-side delete: %s",
                session_id,
                exc,
            )
        if runner_client is not None:
            try:
                await runner_client.delete(
                    f"/v1/sessions/{session_id}/resources",
                    timeout=10.0,
                )
            except (httpx.HTTPError, ConnectionError):
                _logger.warning(
                    "Runner cleanup failed for %s, falling back",
                    session_id,
                )
        else:
            import contextlib

            from omnigent.runtime import get_terminal_registry

            with contextlib.suppress(RuntimeError):
                await get_terminal_registry().cleanup_conversation(session_id)
        # Session file cleanup.
        if file_store is not None and artifact_store is not None:
            deleted_file_ids = await asyncio.to_thread(
                file_store.delete_all_for_session, session_id
            )
            for fid in deleted_file_ids:
                await asyncio.to_thread(artifact_store.delete, fid)
        # Opt-in git worktree cleanup: only when delete_branch=true and
        # the session has a server-created worktree. Runs after runner
        # teardown; best-effort (designs/SESSION_GIT_WORKTREE.md).
        if (
            delete_branch
            and conv.git_branch is not None
            and conv.workspace is not None
            and conv.host_id is not None
        ):
            await _remove_session_worktree_best_effort(
                host_id=conv.host_id,
                worktree_path=conv.workspace,
                branch=conv.git_branch,
                delete_branch=True,
                request=request,
                reason="session-delete",
            )
        _interrupt_fenced_sessions.discard(session_id)
        _intentional_stop_sessions.discard(session_id)
        deleted = await conversation_store.delete_conversation(session_id)
        if not deleted:
            raise _session_not_found()
        # The session is gone, so is its launch-progress state. Failed
        # launches are retained in the cache for reload visibility while
        # the session exists; without this eviction every deleted
        # failed-launch session would leak one entry for the process
        # lifetime.
        _session_sandbox_status_cache.pop(session_id, None)
        # Same for MCP startup state: failed/cancelled maps are retained
        # for reload visibility while the session exists, so a session
        # whose MCP startup never settled clean would leak its entry.
        _session_mcp_startup_cache.pop(session_id, None)
        # Same for the extension-pushed model catalog: kept across reloads
        # while the session exists (the extension only pushes on start), so a
        # deleted session would otherwise leak its entry for the process life.
        _pushed_model_options_cache.pop(session_id, None)
        # Drop the deleted session's per-user read-state from every user's
        # caches so they don't accumulate orphan entries for the process
        # lifetime.
        _prune_session_read_state(session_id)
        # Same for the tracker's entry — a deleted session's launch can
        # never be rendezvoused again (access checks 404 first), so a
        # retained failure is dead weight. ``finish`` also settles a
        # still-in-flight entry, releasing any parked message POST into
        # its session re-read (which now correctly 404s); the background
        # task's later ``fail`` on the popped entry is a no-op.
        managed_launches_for_delete = getattr(request.app.state, "managed_launches", None)
        if managed_launches_for_delete is not None:
            managed_launches_for_delete.finish(session_id)
        # Managed-host cleanup: when the session's host is backed by a
        # server-provisioned sandbox (host_type="managed"), terminate
        # the sandbox and delete the host row — which also revokes its
        # launch token. Best-effort by design — the provider's lifetime
        # cap reaps stragglers. External (laptop) hosts have no
        # sandbox_id and are never touched.
        host_store_for_managed = getattr(request.app.state, "host_store", None)
        if conv.host_id is not None and host_store_for_managed is not None:
            bound_host = await asyncio.to_thread(host_store_for_managed.get_host, conv.host_id)
            if bound_host is not None and bound_host.sandbox_id is not None:
                from omnigent.server.managed_hosts import terminate_managed_host

                await terminate_managed_host(
                    bound_host,
                    host_store_for_managed,
                    # Supplies the launcher for the provider-side
                    # terminate; None (config removed since launch)
                    # still deletes the row and revokes the token.
                    getattr(request.app.state, "sandbox_config", None),
                )
        try:
            import hashlib as _hashlib
            import time as _time

            _srv_id = _get_installation_id()
            _anon_d: str | None = None
            if user_id is not None:
                _salt_d = f"{_srv_id}:{user_id}" if _srv_id else user_id
                _anon_d = _hashlib.sha256(_salt_d.encode()).hexdigest()[:16]
            _usage = conv.session_usage or {}
            _duration: float | None = None
            with contextlib.suppress(Exception):
                _duration = _time.time() - conv.created_at
            _tel_emit(
                _TelSessionDeletedEvent(
                    session_id=session_id,
                    installation_id=_srv_id,
                    anon_user_id=_anon_d,
                    duration_seconds=_duration,
                    input_tokens=_usage.get("input_tokens"),
                    output_tokens=_usage.get("output_tokens"),
                    total_cost_usd=_usage.get("total_cost_usd"),
                )
            )
        except Exception:
            pass
        return ConversationDeleted(id=session_id)
