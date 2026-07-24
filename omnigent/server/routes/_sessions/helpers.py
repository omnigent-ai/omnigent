"""Lower-layer helpers for the sessions routes (call-depth 0-1).

Leaf/near-leaf helpers extracted from ``sessions.py``: SSE item builders,
publishers, persistence, validation, and the runner-forward primitives.
Imports shared state/constants from ``.common``; imported by
``.orchestration`` and by the router in ``sessions.py``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
import urllib.parse
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

import httpx
from fastapi import (
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from omnigent.cost_plan import (
    COST_CONTROL_LABEL_NAMESPACE,
    reserved_cost_control_keys,
)
from omnigent.db.utils import generate_task_id
from omnigent.entities import (
    Agent,
    Conversation,
    ConversationItem,
    ErrorData,
    MessageData,
    NewConversationItem,
    SlashCommandData,
    StoredFile,
    synthesize_conversation_title,
)
from omnigent.entities.conversation import (
    ITEM_TYPE_TO_DATA_CLS,
    parse_item_data,
)
from omnigent.entities.permission import SessionPermission
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_plugins import (
    NativeCodingAgent,
)
from omnigent.native_coding_agents import (
    native_coding_agent_for_harness,
    native_coding_agent_for_wrapper_label,
)
from omnigent.policies.types import (
    EvaluationContext,
    PolicyAction,
)
from omnigent.reasoning_effort import (
    EFFORT_VALUES,
    validate_effort,
)
from omnigent.runner.identity import (
    token_bound_runner_id,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runtime import (
    get_policy_store,
    inflight_text,
    pending_elicitations,
    pending_inputs,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.runtime.tool_output import cap_tool_output
from omnigent.server import presence, session_live_state
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
)
from omnigent.server.host_registry import HostConnection, HostRegistry, RunnerExitReports
from omnigent.server.managed_hosts import (
    ManagedHostLaunch,
    ManagedLaunch,
    ManagedLaunchTracker,
    ManagedSandboxConfig,
    RepoWorkspace,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._host_worktree import CreatedWorktree
from omnigent.server.routes._session_create_validation import (
    validate_existing_host_workspace,
)

# Shared constants, state, and small dataclasses live in the _sessions.common
# leaf module; import them here so this module and its re-exporters see the same
# objects. The mutable caches are shared by reference across the package.
from omnigent.server.routes._sessions.common import *

# Runtime bindings that tests patch on the historical ``sessions`` facade are
# imported from common as facade-delegating proxies (kept out of common's
# ``__all__`` so the star import above never overwrites them). Resolving them
# here means a facade-level monkeypatch is honoured in this module too.
from omnigent.server.routes._sessions.common import (  # noqa: F401
    _ELICITATION_MODE,
    build_policy_engine,
    get_agent_cache,
    get_caps,
    get_server_runner_router,
    session_stream,
    set_server_runner_router,
    user_session_stream,
)
from omnigent.server.schemas import (
    ChildSessionSummary,
    CompletedEvent,
    CreatedSessionResponse,
    ErrorDetail,
    ErrorEvent,
    McpServerStartup,
    ModelUsage,
    NativeModelOption,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    PolicyDeniedEvent,
    ReasoningStartedEvent,
    ReasoningTextDeltaEvent,
    ResponseObject,
    SandboxStatus,
    SessionCollaborationModeEvent,
    SessionCreatedEvent,
    SessionCreateMetadata,
    SessionEventInput,
    SessionGitOptions,
    SessionInputConsumedEvent,
    SessionInputConsumedPayload,
    SessionInterruptedEvent,
    SessionInterruptedPayload,
    SessionListItem,
    SessionMcpStartupEvent,
    SessionModelEvent,
    SessionModelOptionsEvent,
    SessionReasoningEffortEvent,
    SessionResourceListPage,
    SessionResourcePaginatedList,
    SessionSandboxStatusEvent,
    SessionSkillsEvent,
    SessionStatusEvent,
    SessionSupersededEvent,
    SessionTerminalPendingEvent,
    SessionTodosEvent,
    SkillSummary,
    ToolOutputDeltaEvent,
)
from omnigent.session_lifecycle import (
    labels_with_closed_status,
    title_without_closed_marker,
)
from omnigent.spec.types import (
    AgentSpec,
    Phase,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import (
    ConversationNotFoundError,
    NameAlreadyExistsError,
)
from omnigent.stores.host_store import Host, HostStore
from omnigent.stores.permission_store import PermissionStore


def _codex_plan_mode_enabled(mode: str) -> bool:
    """
    Convert a validated Codex collaboration mode kind to the UI-facing flag.

    :param mode: Codex collaboration mode kind, e.g. ``"plan"`` or
        ``"default"``.
    :returns: ``True`` for Plan mode.
    """
    return mode == "plan"


def _publish_collaboration_mode(session_id: str, mode: str) -> None:
    """
    Publish the live collaboration-mode for a session.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param mode: The active collaboration mode string, e.g.
        ``"plan"`` or ``"default"``.
    :returns: None.
    """
    event = SessionCollaborationModeEvent(
        type="session.collaboration_mode",
        conversation_id=session_id,
        mode=mode,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_policy_denied(session_id: str, reason: str, phase: str) -> None:
    """
    Publish a native policy-DENY signal on the session stream.

    A native harness's policy DENY is decided synchronously in the
    ``/policies/evaluate`` hook response, so nothing on the stream otherwise
    reflects that an action was blocked. This surfaces the decision as a
    positive event for observers (web UI, capability bench). Fire-and-forget.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param reason: Deny reason from the deciding policy.
    :param phase: The policy phase the DENY landed on, e.g. ``"tool_call"``.
    :returns: None.
    """
    event = PolicyDeniedEvent(
        type="response.policy_denied",
        conversation_id=session_id,
        reason=reason,
        phase=phase,
    )
    session_stream.publish(session_id, event.model_dump())


def _allow_all_edits_eligible(tool_name: str, permission_mode: str | None) -> bool:
    """
    Whether a claude-native PermissionRequest may offer / honor the
    "Accept & allow all edits" affordance.

    Eligible for file-editing tools under a mode that still prompts,
    and for ``ExitPlanMode`` — accepting a plan with the flag is the
    plan card's "Yes, and use auto mode" option (exit plan mode AND
    switch the session into Claude's ``auto`` mode).
    Already-permissive modes (``acceptEdits`` / ``bypassPermissions``)
    wouldn't prompt at all, so the switch would be inert. Used at BOTH
    the stamp site (drives the UI button) and the verdict site (gates
    the ``setMode`` decision), so the server never honors a
    client-supplied ``allow_all_edits`` flag on a tool/mode the
    affordance was never offered for.

    :param tool_name: The gated tool from Claude's PermissionRequest
        payload, e.g. ``"Edit"`` or ``"Bash"``.
    :param permission_mode: Claude's current permission mode from the
        payload, e.g. ``"default"`` / ``"plan"`` / ``"acceptEdits"`` /
        ``None`` when absent.
    :returns: ``True`` iff the affordance applies.
    """
    return (
        tool_name in _CLAUDE_NATIVE_EDIT_TOOLS or tool_name == "ExitPlanMode"
    ) and permission_mode not in (
        "acceptEdits",
        "bypassPermissions",
    )


def _allow_remember_eligible(tool_name: str, permission_mode: str | None) -> bool:
    """
    Whether a claude-native PermissionRequest may offer / honor the
    persistent "don't ask again" affordance — a session-scoped allow
    rule for the gated tool (WebFetch domain, or tool-wide otherwise).

    This restores native Claude Code parity for NON-edit tools: the
    native TUI lets the user approve a tool/domain once and adds an
    allow rule so same-scope calls stop prompting. The web UI used to
    collapse every prompt into binary Approve/Reject and never wrote a
    rule, so e.g. each WebFetch — even same-domain github.com URLs —
    re-prompted forever.

    Eligible for any tool that ISN'T an edit tool (those take the
    ``acceptEdits`` ``setMode`` path) and isn't one of the tools with a
    bespoke card (see ``_CLAUDE_NATIVE_REMEMBER_INELIGIBLE_TOOLS``),
    under any mode that still prompts. ``bypassPermissions`` never
    prompts (the hook doesn't even fire), so a rule there would be
    inert. Used at BOTH the stamp site (drives the UI button) and the
    verdict site (gates the ``addRules`` decision), so the server never
    honors a client-supplied ``remember`` flag on a tool/mode the
    affordance was never offered for.

    :param tool_name: The gated tool from Claude's PermissionRequest
        payload, e.g. ``"WebFetch"`` or ``"Bash"``.
    :param permission_mode: Claude's current permission mode from the
        payload, e.g. ``"default"`` / ``"plan"`` / ``"acceptEdits"`` /
        ``None`` when absent.
    :returns: ``True`` iff the affordance applies.
    """
    return (
        tool_name not in _CLAUDE_NATIVE_EDIT_TOOLS
        and tool_name not in _CLAUDE_NATIVE_REMEMBER_INELIGIBLE_TOOLS
        and permission_mode != "bypassPermissions"
    )


def _claude_native_remember_host(tool_name: str, tool_input: Any) -> str | None:
    """
    Derive the domain host that a WebFetch "don't ask again" rule should
    scope to, from the gated tool's input.

    For ``WebFetch`` the persistent rule is scoped to the request's
    host (``WebFetch(domain:<host>)`` in Claude rule syntax), so
    approving ``https://github.com/a/b`` stops prompting for
    ``https://github.com/c/d`` too — but not for other domains. Any
    other tool (or a WebFetch with a missing/unparseable URL) returns
    ``None``, which the callers treat as a tool-wide scope.

    Only ``http`` / ``https`` URLs yield a domain scope: WebFetch
    domain permissions are semantically HTTP(S)-oriented, so a
    non-HTTP scheme (``ftp://``, ``file://``, …) falls back to a
    tool-wide rule rather than persisting a ``domain:<host>`` that
    would never match a real fetch.

    :param tool_name: The gated tool from Claude's PermissionRequest
        payload.
    :param tool_input: The tool's input dict (``None``/non-dict tolerated).
    :returns: The lowercased host (no port), bracketed when it is an IPv6
        literal (``[2001:db8::1]``), or ``None`` when no domain scope
        applies.
    """
    if tool_name != "WebFetch" or not isinstance(tool_input, dict):
        return None
    url = tool_input.get("url")
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    host = parsed.hostname
    if not host:
        return None
    # urlparse already lowercases ``hostname`` and strips the port and
    # any userinfo; lower() again makes the documented invariant explicit.
    host = host.lower()
    # urlparse strips the brackets off an IPv6 literal authority
    # (``[2001:db8::1]`` → ``2001:db8::1``), but Claude's
    # ``domain:<host>`` rule grammar is colon-delimited, so a bare
    # colon-laden IPv6 atom persists a broken/inert rule (the user
    # clicks "don't ask again" and keeps getting prompted). A registered
    # domain name can never contain a colon, so a ``:`` here is an
    # unambiguous IPv6 literal — re-bracket it so the emitted rule is
    # ``domain:[2001:db8::1]``.
    if ":" in host:
        return f"[{host}]"
    return host


def _read_state_entry(user_id: str | None, session_id: str) -> tuple[int | None, bool]:
    """
    Read the caller's read-state for one session, for embedding in the
    per-user ``GET /v1/sessions`` list items.

    :param user_id: Authenticated user id, or ``None`` in single-user mode.
    :param session_id: Session/conversation identifier.
    :returns: ``(last_seen, unread)`` — the wall-clock baseline (or ``None``
        when the user has never seen the session) and the explicit-unread flag.
    """
    key = _discovery_key(user_id)
    last_seen = _read_last_seen.get(key, {}).get(session_id)
    unread = session_id in _read_explicit_unread.get(key, set())
    return last_seen, unread


def _set_read_state(user_id: str | None, session_id: str, last_seen: int, unread: bool) -> None:
    """
    Set the caller's read-state for one session.

    :param user_id: Authenticated user id, or ``None`` in single-user mode.
    :param session_id: Session/conversation identifier.
    :param last_seen: Wall-clock baseline in seconds.
    :param unread: Whether the session is explicitly flagged unread.
    """
    key = _discovery_key(user_id)
    _read_last_seen.setdefault(key, {})[session_id] = last_seen
    if unread:
        _read_explicit_unread.setdefault(key, set()).add(session_id)
    else:
        unread_set = _read_explicit_unread.get(key)
        if unread_set is not None:
            unread_set.discard(session_id)


def _prune_session_read_state(session_id: str) -> None:
    """
    Drop a session's read-state from every user's caches.

    Called when a session leaves the default view for good — on delete, and
    on archive (archived sessions are hidden and never show the unread dot).
    This bounds the otherwise-monotonic ``_read_last_seen`` growth to live,
    non-archived sessions. Read-state is a session-level removal (the session
    is gone/archived for everyone), so it clears across all users. Unarchiving
    does NOT restore the prior state — the session reads as seen, which is the
    intended "done with it" semantics of archiving.

    :param session_id: Session/conversation identifier.
    """
    for seen in _read_last_seen.values():
        seen.pop(session_id, None)
    for unread in _read_explicit_unread.values():
        unread.discard(session_id)


def _discovery_key(user_id: str | None) -> str:
    """
    Map an (optional) user id to the :mod:`user_session_stream` channel key.

    :param user_id: Authenticated user id, e.g. ``"alice@example.com"``, or
        ``None`` in single-user / no-auth mode.
    :returns: ``user_id`` when set, else :data:`_SHARED_DISCOVERY_KEY`.
    """
    return user_id if user_id is not None else _SHARED_DISCOVERY_KEY


def _announce_session_added(user_id: str | None, session_id: str) -> None:
    """
    Push a ``session_added`` discovery event to a user's updates streams.

    Called after a session becomes accessible to ``user_id`` (created, forked,
    or shared) so that user's open tabs surface it without a list poll. A no-op
    when the user has no stream connected.

    :param user_id: The user the session is now accessible to (the owner on
        create/fork, the grantee on share), or ``None`` in single-user mode.
    :param session_id: The newly-accessible session id, e.g. ``"conv_abc123"``.
    """
    user_session_stream.publish(
        _discovery_key(user_id), {"type": "session_added", "session_id": session_id}
    )


def announce_hosts_changed(user_id: str | None) -> None:
    """
    Push a ``hosts_changed`` event to a user's session-updates streams.

    Called when a host owned by ``user_id`` connects or disconnects so the
    client invalidates its hosts cache without polling. A no-op when the user
    has no stream connected.

    :param user_id: Owner of the host that changed, or ``None`` in
        single-user mode.
    """
    user_session_stream.publish(_discovery_key(user_id), {"type": "hosts_changed"})


def _native_ask_gate_lock(conversation_id: str, deciding_policy: str) -> asyncio.Lock:
    """
    Return the lock serializing native ASK gates for one (session, policy).

    Concurrent native tool calls that all trip the same ASKing policy must
    prompt the human once, not once each. Callers hold the returned lock
    across the entire human-approval wait and re-evaluate the policy under it;
    the first approval records a checkpoint that collapses the siblings to
    ALLOW. Get-or-create is race-free because there is no ``await`` between the
    lookup and the insert (single event loop).

    :param conversation_id: Omnigent conversation id whose ASK gate is being
        serialized, e.g. ``"conv_abc123"``. Sub-agent native tool calls
        evaluate against the parent conversation id, so they share its lock.
    :param deciding_policy: Name of the policy that produced the ASK verdict,
        e.g. ``"session_cost_guard"``. Distinct policies get distinct locks so
        their approval prompts can surface concurrently.
    :returns: A process-wide :class:`asyncio.Lock` shared by every concurrent
        caller for the same ``(conversation_id, deciding_policy)`` pair.
    """
    key = (conversation_id, deciding_policy)
    lock = _native_ask_gate_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _native_ask_gate_locks[key] = lock
    return lock


async def _poll_request_disconnect(*args: Any, **kwargs: Any) -> None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._poll_request_disconnect(*args, **kwargs)


async def _poll_request_disconnect_impl(request: Request) -> None:
    """
    Resolve once Starlette reports the client closed the connection.

    Long-poll routes that park on a verdict (e.g. the Claude-native
    ``PermissionRequest`` hook) use this to detect that the upstream
    client has hung up — Claude closes its HTTP request when its
    TUI prompt receives an answer first, and without this wait the
    handler would sit out the full timeout to notice.

    Blocks on ``request.receive()`` rather than polling
    ``request.is_disconnected()``. The poll variant runs each check
    inside a pre-cancelled anyio ``CancelScope`` (Starlette's
    non-blocking receive idiom); an external ``Task.cancel()`` that
    lands while that scope is unwinding coalesces with the scope's own
    cancellation and is swallowed with it, so the poller survives its
    cancel and the caller's race cleanup blocks on it forever.
    A blocking receive has no cancel scope in its await chain, so
    cancellation always propagates; it is also cheaper than waking
    twice a second.

    :param request: The active FastAPI :class:`Request`. By the time
        the handler parks, the route has consumed the body, so the
        next receive yields only ``http.disconnect``.
    :returns: None when the disconnect is observed. Cancellation
        propagates: callers that race this against a verdict Future
        cancel the wait once the verdict arrives.
    """
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


def _attachment_disposition(filename: str) -> str:
    """Build a safe ``Content-Disposition: attachment`` header value.

    The filename is user-controlled, so it cannot be interpolated
    into the header verbatim — a quote or newline would let the
    uploader inject header content or break parsing. We emit an
    ASCII-only ``filename`` fallback (with quotes/backslashes/control
    characters stripped) plus an RFC 5987 ``filename*`` parameter that
    percent-encodes the full UTF-8 name for modern browsers.

    :param filename: The stored, user-supplied filename.
    :returns: A ``Content-Disposition`` header value forcing download.
    """
    # ASCII fallback: drop anything outside printable ASCII and the
    # characters that are structurally significant in the header.
    ascii_name = "".join(ch for ch in filename if 0x20 <= ord(ch) < 0x7F and ch not in '"\\')
    if not ascii_name:
        ascii_name = "download"
    encoded = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _stored_file_to_resource(
    session_id: str,
    stored: StoredFile,
) -> dict[str, Any]:
    """Convert a :class:`StoredFile` to a session file resource dict.

    Matches the ``session.resource`` shape with ``type: "file"``
    used by the unified inventory and the session-scoped file
    endpoints.

    :param session_id: Owning session/conversation id.
    :param stored: The stored file entity.
    :returns: JSON-serializable resource dict.
    """
    return {
        "id": stored.id,
        "object": "session.resource",
        "type": "file",
        "session_id": session_id,
        "name": stored.filename,
        "metadata": {
            "filename": stored.filename,
            "bytes": stored.bytes,
            "created_at": stored.created_at,
        },
    }


def _publish_and_persist_resource_event(
    session_id: str,
    event_type: str,
    resource_id: str,
    resource_type: str,
    conversation_store: ConversationStore,
    resource: dict[str, Any] | None = None,
) -> None:
    """Publish an SSE event and persist it as a conversation item.

    Emits the event on the live session stream so connected
    clients see it immediately, and appends a ``resource_event``
    conversation item so reconnecting clients discover it in the
    snapshot.

    :param session_id: Session/conversation identifier.
    :param event_type: SSE event type, e.g.
        ``"session.resource.created"``.
    :param resource_id: Opaque id of the affected resource.
    :param resource_type: Kind of resource, e.g. ``"terminal"``.
    :param conversation_store: Store for persisting the item.
    :param resource: Full resource dict for created events.
    """
    from omnigent.entities.conversation import ResourceEventData

    sse_payload: dict[str, Any] = {"type": event_type}
    if event_type == "session.resource.created":
        sse_payload["resource"] = resource or {}
    else:
        sse_payload["resource_id"] = resource_id
        sse_payload["resource_type"] = resource_type
        sse_payload["session_id"] = session_id

    session_stream.publish(session_id, sse_payload)

    item = NewConversationItem(
        type="resource_event",
        response_id=session_id,
        data=ResourceEventData(
            event_type=event_type,
            resource_id=resource_id,
            resource_type=resource_type,
            resource=resource,
        ),
    )
    try:
        conversation_store.append(session_id, [item])
    except (AttributeError, TypeError, ValueError, RuntimeError):
        _logger.debug(
            "Failed to persist resource event for session=%s",
            session_id,
            exc_info=True,
        )


def _structured_ask_user_question(
    tool_input: Any,
) -> dict[str, Any] | None:
    """
    Build a structured AskUserQuestion payload for the elicitation
    params extras.

    Claude's PermissionRequest payload includes the full tool_input
    when the gated tool is AskUserQuestion. Rather than relying on
    the (truncated) ``content_preview`` JSON-string, we extract the
    questions + options here and ship them as a typed structure the
    UI consumes directly.

    The returned shape is the same one the UI's
    :file:`@/lib/askUserQuestion.ts` produces from its preview
    parser — so the front-end can treat both sources uniformly.

    :param tool_input: The ``tool_input`` field from the
        PermissionRequest payload.
    :returns: ``{"questions": [...]}`` on success, or ``None`` when
        the input doesn't carry a usable AskUserQuestion shape (no
        questions, malformed options, etc.) — caller falls back to
        the binary preview-only render.
    """
    if not isinstance(tool_input, dict):
        return None
    questions_raw = tool_input.get("questions")
    if not isinstance(questions_raw, list) or not questions_raw:
        return None
    questions: list[dict[str, Any]] = []
    for entry in questions_raw:
        if not isinstance(entry, dict):
            continue
        question_text = entry.get("question")
        if not isinstance(question_text, str) or not question_text:
            continue
        options_raw = entry.get("options")
        if not isinstance(options_raw, list):
            continue
        options: list[dict[str, Any]] = []
        for opt in options_raw:
            if isinstance(opt, dict):
                label = opt.get("label")
                if not isinstance(label, str) or not label:
                    continue
                option: dict[str, Any] = {"label": label}
                description = opt.get("description")
                if isinstance(description, str) and description:
                    option["description"] = description
                # ``preview`` is an optional richer snippet some
                # Claude builds attach to an option (rendered as a
                # <pre> below the option list when selected). Ride
                # it through verbatim so the UI can surface it.
                preview = opt.get("preview")
                if isinstance(preview, str) and preview:
                    option["preview"] = preview
                options.append(option)
            elif isinstance(opt, str) and opt:
                options.append({"label": opt})
        if not options:
            continue
        question: dict[str, Any] = {
            "question": question_text,
            "options": options,
            "multiSelect": entry.get("multiSelect") is True,
        }
        header = entry.get("header")
        if isinstance(header, str) and header:
            question["header"] = header
        questions.append(question)
    if not questions:
        return None
    return {"questions": questions}


def _canonical_tool_input(tool_input: dict[str, Any] | None) -> dict[str, Any]:
    """
    Canonicalize a tool input for terminal-resolved correlation.

    The park side records an absent / non-dict input as ``None`` (a
    permission prompt whose hook payload carries no ``tool_input`` — see
    the ``_publish_and_wait_for_harness_elicitation`` call sites), while
    the mirror side normalizes the parsed transcript arguments to ``{}``
    (see :func:`_drive_terminal_resolved_elicitation`). Both mean "no
    input", so collapse them to ``{}`` before comparing — otherwise a
    no-input prompt would never match its own mirrored result (``None ==
    {}`` is ``False``) and, with no count-based fallback, would orphan
    until the hook timeout.

    :param tool_input: Parked or mirrored tool input, e.g.
        ``{"command": "ls"}``, ``{}``, or ``None``.
    :returns: The dict unchanged, or ``{}`` when it is ``None``.
    """
    return tool_input if isinstance(tool_input, dict) else {}


def _signal_terminal_resolved_harness_elicitation(*args: Any, **kwargs: Any) -> None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return _facade._signal_terminal_resolved_harness_elicitation(*args, **kwargs)


def _signal_terminal_resolved_harness_elicitation_impl(
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
) -> None:
    """
    Resolve the parked prompt a mirrored tool result belongs to,
    ending its long-poll promptly.

    Called when the transcript forwarder mirrors a tool result
    (``function_call_output``) for a native session. A tool result is
    only written AFTER the user answered that tool's permission prompt
    in the native terminal — on accept the tool ran and produced output,
    on reject the harness records a rejection result — so its arrival is
    a reliable "the terminal already resolved this" signal.

    Correlation is by exact tool identity, never positional: a result
    resolves a parked prompt only when it has the SAME ``tool_name`` AND
    the SAME ``tool_input`` in the same session. Claude Code's
    ``PermissionRequest`` payload carries no ``tool_use_id`` (the id is
    minted only when the tool call is emitted, after the permission
    check), so ``(tool_name, tool_input)`` is the only correlation signal
    available — and both sides are unmodified JSON round-trips of the
    same input, so exact equality holds whenever they describe the same
    call (absent input and empty input both canonicalize to ``{}`` via
    :func:`_canonical_tool_input`, since the park and mirror sides spell
    "no input" differently — ``None`` vs ``{}``). A non-matching or
    ambiguous result resolves nothing; the web verdict or timeout still
    applies. Exact-only matching is what stops
    one prompt's result from clearing a different prompt: approving
    ``Bash{ls}`` in the web UI un-parks it, and mirroring its own output
    must not then clear a still-pending ``Bash{pwd}`` sibling (an
    unrelated auto-allowed same-named tool's output is harmless for the
    same reason).

    Best-effort and idempotent: a no-op when no parked prompt matches
    (e.g. the web UI already resolved it, the tool needed no permission,
    or it is an unrelated tool). Harness-agnostic by construction —
    keyed on the parked prompt's tool identity, not on a claude-native
    check — so a Codex hook that records ``tool_name`` benefits too.

    :param session_id: Omnigent conversation id whose forwarder mirrored the
        result, e.g. ``"conv_abc123"``.
    :param tool_name: Tool name the result is for, e.g. ``"Bash"``.
    :param tool_input: Tool input the result is for, e.g.
        ``{"command": "ls"}``, or ``None`` if unavailable.
    """
    candidates = [
        parked
        for parked in _harness_parked_elicitations.values()
        if parked.session_id == session_id
        and parked.tool_name == tool_name
        and not parked.resolved_elsewhere.is_set()
    ]
    if not candidates:
        return
    mirrored_input = _canonical_tool_input(tool_input)
    for parked in candidates:
        if _canonical_tool_input(parked.tool_input) == mirrored_input:
            parked.resolved_elsewhere.set()
            return
    # No exact input match. Correlation is exact-only: resolving a
    # same-named-but-different-input prompt here would clear the wrong
    # card, so leave every candidate to its own result / web verdict /
    # timeout. This branch is reached routinely and benignly — e.g. after
    # a sibling prompt was web-approved and un-parked, its mirrored output
    # finds only the still-pending different-input prompt — so it logs at
    # debug, not warning. (A genuine match failing to compare equal would
    # also land here, but is indistinguishable from the benign case inside
    # this call; both inputs are unmodified JSON round-trips, so such drift
    # is not expected.)
    _logger.debug(
        "Mirrored %s result in %s matched no parked prompt by input "
        "(%d same-named prompt(s) pending); leaving them to web verdict/timeout.",
        tool_name,
        session_id,
        len(candidates),
    )


def _client_supplied_hook_elicitation_id(
    payload: dict[str, Any],
    session_id: str,
) -> str | None:
    """
    Validate the hook client's optional re-attach elicitation id.

    The hook mints one stable id per prompt and re-sends it on every
    retry POST, so a severed wait re-parks as the SAME elicitation.
    Client-controlled, so it is constrained to the claude-hook
    namespace and may not collide with another session's parked id.

    :param payload: Parsed PermissionRequest hook body. Reads the
        optional ``_omnigent_elicitation_id`` key.
    :param session_id: Session the hook call is for, e.g.
        ``"conv_abc123"``.
    :returns: The validated id, or ``None`` when the client supplied
        none (the wait mints a random id as before).
    :raises OmnigentError: 400 when the id is malformed or is
        currently parked by a different session.
    """
    raw = payload.get("_omnigent_elicitation_id")
    if raw is None:
        return None
    if not isinstance(raw, str) or not _HOOK_ELICITATION_ID_RE.fullmatch(raw):
        raise OmnigentError(
            "PermissionRequest hook '_omnigent_elicitation_id' must match "
            "'elicit_<harness>_' + 32 hex chars.",
            code=ErrorCode.INVALID_INPUT,
        )
    owner = _harness_elicitation_owners.get(raw)
    if owner is not None and owner != session_id:
        raise OmnigentError(
            "Elicitation id belongs to a different session.",
            code=ErrorCode.INVALID_INPUT,
        )
    return raw


def _consume_pre_resolved_harness_elicitation(
    session_id: str,
    elicitation_id: str,
) -> _PreResolvedHarnessElicitation | None:
    """
    Consume a resolution that arrived before the hook wait registered.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Harness elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    :returns: The consumed tombstone when one matched this session
        (its ``result`` carries the web verdict to honor, or ``None``
        for a terminal-side resolution), or ``None`` when nothing was
        pre-resolved.
    """
    _prune_pre_resolved_harness_elicitations()
    tombstone = _harness_pre_resolved_elicitations.pop(elicitation_id, None)
    if tombstone is None:
        return None
    if tombstone.session_id == session_id:
        return tombstone
    _harness_pre_resolved_elicitations[elicitation_id] = tombstone
    return None


def _prune_pre_resolved_harness_elicitations(now: float | None = None) -> None:
    """
    Prune stale or excess pre-resolved harness elicitation tombstones.

    :param now: Optional wall-clock timestamp from ``time.time()``,
        e.g. ``1710000000.0``. ``None`` reads the current time.
    :returns: None.
    """
    if not _harness_pre_resolved_elicitations:
        return
    # Resolve limits through the facade so a test's monkeypatch of these
    # constants is honored here.
    from omnigent.server.routes import sessions as _facade

    now = time.time() if now is None else now
    expired = [
        elicitation_id
        for elicitation_id, tombstone in _harness_pre_resolved_elicitations.items()
        if now - tombstone.created_at > _facade._HARNESS_PRE_RESOLVED_ELICITATION_TTL_S
    ]
    for elicitation_id in expired:
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)
    overflow = (
        len(_harness_pre_resolved_elicitations)
        - _facade._HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES
    )
    if overflow <= 0:
        return
    oldest = sorted(
        _harness_pre_resolved_elicitations.items(),
        key=lambda item: item[1].created_at,
    )[:overflow]
    for elicitation_id, _tombstone in oldest:
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)


def _signal_harness_elicitation_resolved_by_id(
    session_id: str,
    elicitation_id: str,
) -> None:
    """
    Resolve or pre-resolve one parked harness elicitation by id.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Harness elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    :returns: None.
    :raises OmnigentError: If the id is malformed or belongs to a
        different session.
    """
    if not elicitation_id:
        raise OmnigentError(
            "external_elicitation_resolved requires data.elicitation_id.",
            code=ErrorCode.INVALID_INPUT,
        )
    owner = _harness_elicitation_owners.get(elicitation_id)
    if owner is not None and owner != session_id:
        raise OmnigentError(
            "Elicitation does not belong to this session.",
            code=ErrorCode.INVALID_INPUT,
        )
    _prune_pre_resolved_harness_elicitations()
    parked = _harness_parked_elicitations.get(elicitation_id)
    if parked is None:
        _harness_pre_resolved_elicitations[elicitation_id] = _PreResolvedHarnessElicitation(
            session_id=session_id,
            created_at=time.time(),
        )
        _prune_pre_resolved_harness_elicitations()
        return
    parked.resolved_elsewhere.set()


def _format_sse(event_type: str, data: dict[str, Any]) -> str:
    """
    Format an SSE event string for the wire.

    :param event_type: SSE event name, e.g.
        ``"response.output_text.delta"``.
    :param data: The event payload dict.
    :returns: A formatted SSE message string ending in two newlines.
    """
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _permission_level_from_grants(
    user_id: str | None,
    grants: list[SessionPermission],
    is_admin: bool,
) -> int | None:
    """
    Derive a user's permission level from a pre-fetched list of grants.

    Mirrors :func:`omnigent.server.routes._auth_helpers._get_permission_level_sync`
    but operates on grants already held in memory so callers can batch the
    permission-store query across many sessions at once.

    :param user_id: The authenticated user, or ``None`` for unauthenticated
        requests, e.g. ``"alice@example.com"``.
    :param grants: All grants for the session, as returned by
        ``permission_store.list_for_sessions()[conv_id]``.
    :param is_admin: Whether the user holds the admin flag.  Pass the result
        of a single ``permission_store.is_admin(user_id)`` call made once
        for the whole page rather than repeating it per session.
    :returns: Numeric level (1–4), or ``None`` when permissions are disabled
        or the user is unauthenticated.
    """
    if user_id is None:
        return None
    if is_admin:
        return LEVEL_OWNER
    user_grant = next((g for g in grants if g.user_id == user_id), None)
    if user_grant is not None:
        return user_grant.level
    public_grant = next((g for g in grants if g.user_id == RESERVED_USER_PUBLIC), None)
    if public_grant is not None:
        return public_grant.level
    return None


def _owner_from_grants(grants: list[SessionPermission]) -> str | None:
    """
    Find the session owner from a pre-fetched list of grants.

    Mirrors :func:`omnigent.server.routes._auth_helpers.get_session_owner_id`
    but operates on grants already held in memory so callers can batch the
    permission-store query across many sessions at once.

    :param grants: All grants for the session, as returned by
        ``permission_store.list_for_sessions()[conv_id]``.
    :returns: The ``user_id`` of the first grant whose level is at least
        :data:`LEVEL_OWNER`, or ``None`` if no such grant exists.
    """
    return next((g.user_id for g in grants if g.level >= LEVEL_OWNER), None)


def _session_status_from_cache(
    conversation_id: str,
    db_status: str | None = None,
) -> Literal["idle", "running", "failed"]:
    """
    Map the relay-fed status cache value to a list-item status.

    The cache stores the fine-grained relay status (``"running"``,
    ``"waiting"``, ``"failed"``, ``"idle"``); the list-item shape
    collapses ``"running"``/``"waiting"`` to ``"running"``. A cache
    miss falls back to *db_status* — the row value the tunnel-holding
    replica persisted (``omnigent_conversation_metadata.live_status``) — so a replica
    that does NOT hold this session's runner tunnel still serves the
    real status. No cache entry and no row value presents as ``"idle"``.

    :param conversation_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param db_status: ``Conversation.live_status`` when the caller has
        the row, else ``None``.
    :returns: One of ``"idle"``, ``"running"``, ``"failed"``.
    """
    cached = _session_status_cache.get(conversation_id)
    if cached is None:
        cached = db_status
    if cached in ("running", "waiting"):
        return "running"
    if cached == "failed":
        return "failed"
    return "idle"


def _session_status_with_child_rollup(
    conversation_id: str,
    child_session_ids: list[str],
    db_status: str | None = None,
) -> Literal["idle", "running", "failed"]:
    """
    Map a session's cached status plus direct child activity to list status.

    A parent session should read as ``"running"`` in the sidebar while any
    direct sub-agent child is still ``"running"`` or ``"waiting"``, even if
    the parent runner has already gone idle. This keeps every sidebar row
    honest without mounting a child-session query for each row.

    :param conversation_id: Parent session/conversation identifier,
        e.g. ``"conv_parent123"``.
    :param child_session_ids: Direct sub-agent child conversation ids,
        e.g. ``["conv_child1", "conv_child2"]``.
    :param db_status: The row's persisted ``live_status``, used when the
        local cache has no entry (this replica doesn't hold the runner
        tunnel). The child rollup below stays cache-only — a wrong-pod
        miss there just skips the parent's roll-up spinner, best-effort.
    :returns: One of ``"idle"``, ``"running"``, ``"failed"`` for the
        session-list row.
    """
    own_status = _session_status_from_cache(conversation_id, db_status)
    if own_status == "running":
        return "running"
    # A claude-native session can settle to ``idle`` while background shells
    # keep running; the sticky tally keeps the sidebar spinner lit, matching
    # the in-chat "N background tasks still running" indicator. (``failed``
    # clears the tally, so this never masks a failure.)
    if own_status != "failed" and _session_background_task_count_cache.get(conversation_id, 0) > 0:
        return "running"
    if any(
        _session_status_cache.get(child_id) in ("running", "waiting")
        for child_id in child_session_ids
    ):
        return "running"
    return own_status


async def _collect_descendant_conversation_ids(
    conversation_store: ConversationStore,
    root_id: str,
) -> list[str]:
    """
    Return every sub-agent descendant of ``root_id``, at any depth.

    Walks the tree one level at a time (child, grandchild, and so on),
    batching each level into a single ``list_child_conversation_ids_by_parent``
    call so an N-level tree costs N queries rather than one per node.

    :param conversation_store: Store for child-id lookup.
    :param root_id: Root session/conversation identifier.
    :returns: Descendant ids in breadth-first order. Empty if ``root_id``
        has no sub-agent descendants.
    """
    descendant_ids: list[str] = []
    seen = {root_id}
    frontier = [root_id]
    while frontier:
        child_ids_map = await asyncio.to_thread(
            conversation_store.list_child_conversation_ids_by_parent,
            frontier,
        )
        next_frontier: list[str] = []
        for parent_id in frontier:
            for child_id in child_ids_map.get(parent_id, []):
                if child_id not in seen:
                    seen.add(child_id)
                    descendant_ids.append(child_id)
                    next_frontier.append(child_id)
        frontier = next_frontier
    return descendant_ids


@dataclass(frozen=True)
class SessionLiveness:
    """
    The two honest liveness signals for a single session.

    Returned (keyed by session id) by the server's
    ``_bulk_session_liveness`` / ``_session_liveness`` lookups and
    consumed by the list-item builder, the ``WS /v1/sessions/updates``
    stream, the single-session ``SessionResponse`` snapshot, and
    ``GET /health``. Splitting the old single conflated boolean into
    two fields lets the open-session view distinguish "runner stopped
    but host can relaunch — just send a message" from "host offline —
    reconnect / fork".

    :param runner_online: Strict runner reachability — ``True`` iff a
        runner tunnel is currently registered for this session. This
        is the sole reachability signal: it does **not** fold in
        host-relaunch optimism (a dead runner on a live host reads
        ``False`` here, not ``True``). A session with no runner
        binding (in-process executor / not yet dispatched) reads
        ``True``.
    :param host_online: Whether the session's host tunnel is live
        (status online and fresh within ``HOST_LIVENESS_TTL_S``).
        ``True`` when the session's ``host_id`` is in the online-hosts
        set, ``False`` when a ``host_id`` is set but not online, and
        ``None`` when the session has no ``host_id`` (CLI / local).
        Used only to choose what the open view shows when
        ``runner_online`` is ``False``; never participates in the
        reachability decision.
    :param host_version: Version string from the bound host's
        ``host.hello`` frame, e.g. ``"0.1.0"`` — surfaced in the
        session info popover. ``None`` when the session has no host
        binding, the host is offline, or its version isn't resolvable
        on this replica (the version lives in the in-memory host
        registry, not the hosts table, so a host connected to another
        replica reads ``None`` here).
    """

    runner_online: bool
    host_online: bool | None
    host_version: str | None = None


async def _apply_liveness_to_items(
    items: list[SessionListItem],
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None,
) -> None:
    """
    Attach runner + host liveness to session-list items when a lookup is
    wired.

    Both ``GET /v1/sessions`` and ``WS /v1/sessions/updates`` use this so
    HTTP reconciliation preserves the same ``runner_online`` /
    ``host_online`` fields that push frames patch into the web cache.

    :param items: Session-list rows to annotate.
    :param liveness_lookup: Bulk liveness lookup from session id to a
        :class:`SessionLiveness` pair, e.g.
        ``{"conv_abc123": SessionLiveness(runner_online=True,
        host_online=None)}``. ``None`` means this server cannot compute
        liveness for list rows, in which case both fields are left
        ``None``.
    :returns: ``None``. Mutates ``items`` in place.
    """
    if liveness_lookup is None or not items:
        return
    liveness = await asyncio.to_thread(liveness_lookup, [item.id for item in items])
    for item in items:
        result = liveness[item.id]
        item.runner_online = result.runner_online
        item.host_online = result.host_online
        # A dead runner's parked prompts died with it, but the persisted
        # pending count has no crash-time writer (a runner/host/replica that
        # dies without a graceful resolve never decrements the row) — so an
        # offline runner reads as zero pending rather than lighting a phantom
        # inbox badge over an empty prompt list. Reconciled durably when the
        # runner reconnects (see ``_on_runner_connect``'s pending resync).
        if not result.runner_online:
            item.pending_elicitations_count = 0


def _targeted_elicitation_event(
    event: dict[str, Any],
    *,
    target_session_id: str,
) -> dict[str, Any]:
    """
    Return an elicitation event annotated with its resolution target.

    Child-session elicitations can be mirrored into an ancestor's
    chat stream. The mirrored card is rendered in the ancestor
    conversation, but the harness Future still belongs to the child.
    ``target_session_id`` tells clients which session's resolve URL
    should receive the verdict.

    :param event: Original ``response.elicitation_request`` event,
        e.g. ``{"type": "response.elicitation_request",
        "elicitation_id": "elicit_abc", "params": {...}}``.
    :param target_session_id: Session that owns the parked
        elicitation, e.g. ``"conv_child123"``.
    :returns: A shallow event copy with a copied ``params`` dict
        carrying ``target_session_id``.
    """
    mirrored = dict(event)
    params = event.get("params")
    if isinstance(params, dict):
        mirrored["params"] = {**params, "target_session_id": target_session_id}
    else:
        mirrored["params"] = {"target_session_id": target_session_id}
    return mirrored


def _ancestor_session_ids(
    conv_store: ConversationStore,
    session_id: str,
) -> list[str]:
    """
    Return ancestor session ids for a session, nearest parent first.

    :param conv_store: Store used to read conversation parent links.
    :param session_id: Session to walk upward from, e.g.
        ``"conv_child123"``.
    :returns: Ancestor ids in parent-to-root order. Empty when the
        session is top-level or missing.
    """
    ancestors: list[str] = []
    seen = {session_id}
    current = conv_store.get_conversation(session_id)
    while current is not None and current.parent_conversation_id is not None:
        parent_id = current.parent_conversation_id
        if parent_id in seen:
            break
        ancestors.append(parent_id)
        seen.add(parent_id)
        current = conv_store.get_conversation(parent_id)
    return ancestors


def _publish_elicitation_request_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
    event: dict[str, Any],
) -> None:
    """
    Mirror a child elicitation request into each ancestor stream.

    :param conv_store: Store used to discover ancestor sessions.
    :param session_id: Child session that owns the elicitation,
        e.g. ``"conv_child123"``.
    :param event: Original ``response.elicitation_request`` event.
    """
    mirrored = _targeted_elicitation_event(event, target_session_id=session_id)
    for ancestor_id in _ancestor_session_ids(conv_store, session_id):
        session_stream.publish(ancestor_id, mirrored)


def _publish_elicitation_resolved_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
    elicitation_id: str,
) -> None:
    """
    Mirror an elicitation-resolved event into each ancestor stream.

    :param conv_store: Store used to discover ancestor sessions.
    :param session_id: Child session that owns the elicitation,
        e.g. ``"conv_child123"``.
    :param elicitation_id: Elicitation correlation id, e.g.
        ``"elicit_abc123"``.
    """
    for ancestor_id in _ancestor_session_ids(conv_store, session_id):
        _publish_elicitation_resolved(ancestor_id, elicitation_id)


def _descendant_sessions(
    conv_store: ConversationStore,
    session_id: str,
) -> list[Conversation]:
    """
    Return descendant sub-agent conversations for a session.

    :param conv_store: Store used to list conversations.
    :param session_id: Ancestor session id, e.g. ``"conv_root123"``.
    :returns: Sub-agent conversations below ``session_id``. Empty
        for sessions with no descendants.
    """
    descendants: list[Conversation] = []
    queue: deque[str] = deque([session_id])
    seen = {session_id}
    while queue:
        parent_id = queue.popleft()
        after: str | None = None
        while True:
            page = conv_store.list_conversations(
                kind="sub_agent",
                parent_conversation_id=parent_id,
                limit=100,
                after=after,
            )
            for child in page.data:
                if child.id in seen:
                    continue
                seen.add(child.id)
                descendants.append(child)
                queue.append(child.id)
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
    return descendants


def _pending_elicitation_snapshot_for_session(
    conv_store: ConversationStore,
    conv: Conversation,
) -> list[dict[str, Any]]:
    """
    Return pending elicitation events visible from a session snapshot.

    The current session's own outstanding prompts are returned first.
    Pending prompts from descendant sub-agents are appended with
    ``params.target_session_id`` so a cold-loaded ancestor chat can
    render and resolve child approvals.
    Duplicate ids are skipped because live mirroring also records the
    ancestor copy in the in-memory index.

    The descendant walk costs one ``list_conversations`` query per
    session in the tree, so it is skipped entirely unless some session
    other than ``conv`` has an outstanding prompt in the in-memory
    index (the common case is none anywhere).

    :param conv_store: Store used to list descendant sub-agents.
    :param conv: Session conversation being snapshotted.
    :returns: Pending elicitation event dicts suitable for
        :class:`SessionResponse.pending_elicitations`.
    """
    events = pending_elicitations.snapshot_for(conv.id)
    if not (set(pending_elicitations.pending_session_ids()) - {conv.id}):
        return events
    seen = {
        event.get("elicitation_id")
        for event in events
        if isinstance(event.get("elicitation_id"), str)
    }
    for child in _descendant_sessions(conv_store, conv.id):
        for event in pending_elicitations.snapshot_for(child.id):
            elicitation_id = event.get("elicitation_id")
            if isinstance(elicitation_id, str) and elicitation_id in seen:
                continue
            if isinstance(elicitation_id, str):
                seen.add(elicitation_id)
            events.append(_targeted_elicitation_event(event, target_session_id=child.id))
    return events


def _publish_input_consumed(
    session_id: str,
    item: ConversationItem,
    cleared_pending_id: str | None = None,
) -> None:
    """
    Publish a ``session.input.consumed`` event for a just-persisted
    conversation item.

    Mirrors the wire shape consumers depend on for rendering the
    input (user message bubble, tool-result block, etc.) at the
    moment of acceptance.

    :param session_id: The session/conversation identifier whose
        stream should receive the event.
    :param item: The persisted :class:`ConversationItem` carrying
        the canonical ``id`` / ``type`` / ``data`` fields.
    :param cleared_pending_id: When this message drained a
        :mod:`omnigent.runtime.pending_inputs` entry (native-terminal
        web message mirrored back from the transcript), that entry's
        id, e.g. ``"pending_a1b2c3"`` — so clients drop the optimistic
        bubble by id. ``None`` when nothing was drained.
    """
    if item.type == "message" and isinstance(item.data, MessageData) and item.data.is_meta:
        return
    event = SessionInputConsumedEvent(
        type="session.input.consumed",
        data=SessionInputConsumedPayload(
            item_id=item.id,
            type=item.type,
            data=item.data.model_dump() if item.data is not None else {},
            created_by=item.created_by,
            cleared_pending_id=cleared_pending_id,
        ),
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_compaction_in_progress(session_id: str) -> None:
    """
    Publish the standard compaction progress event to a session stream.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    session_stream.publish(
        session_id,
        {"type": "response.compaction.in_progress"},
    )


def _publish_compaction_completed(session_id: str, total_tokens: int | None) -> None:
    """
    Publish the compaction-finished event to a session stream.

    Emitted after :func:`compact_conversation_now` returns
    successfully. Clients that rendered a spinner on the
    ``response.compaction.in_progress`` event should upgrade it to
    the permanent "Conversation compacted" marker on this event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param total_tokens: Tiktoken estimate of the post-compaction
        context size, e.g. ``8421``. ``None`` when unavailable.
    """
    payload: dict[str, object] = {"type": "response.compaction.completed"}
    if total_tokens is not None:
        payload["total_tokens"] = total_tokens
    session_stream.publish(session_id, payload)


def _publish_compaction_failed(session_id: str) -> None:
    """
    Publish the compaction-failed event to a session stream.

    Emitted when :func:`compact_conversation_now` raises. Clients
    that rendered a spinner on the
    ``response.compaction.in_progress`` event should dismiss it
    without leaving a permanent marker — the conversation history
    was not modified.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    session_stream.publish(session_id, {"type": "response.compaction.failed"})


def _publish_external_assistant_message(
    session_id: str,
    item: ConversationItem,
    *,
    response_id: str,
    agent_name: str,
) -> None:
    """
    Broadcast an assistant message appended outside the task runtime.

    Terminal-backed integrations such as native Claude produce output
    in a live terminal first, then mirror the semantic text into AP.
    There is no ``agent_task`` to watch, so this helper publishes the
    completed output item directly. The browser reducer renders the
    persisted message content from ``response.output_item.done``;
    emitting synthetic text deltas here would duplicate the same
    transcript item when the snapshot path also sees it.

    :param session_id: Session/conversation identifier.
    :param item: Persisted assistant message item.
    :param response_id: Legacy endpoint response id. The persisted
        item already carries this value, so the publisher does not
        need it separately.
    :param agent_name: Legacy endpoint agent/model name. The
        persisted item already carries this value.
    :returns: None.
    """
    del response_id, agent_name
    api_item = item.to_api_dict()
    event = OutputItemDoneEvent(type="response.output_item.done", item=api_item)
    session_stream.publish(session_id, event.model_dump())


def _resolve_llm_model(conv: Conversation | None) -> str | None:
    """
    Resolve the LLM model identifier from a conversation's agent spec.

    Uses the global agent cache to load the parsed spec and read
    ``spec.llm.model``. Returns ``None`` when the conversation has
    no agent binding or the spec cannot be loaded.

    :param conv: The conversation entity, or ``None``.
    :returns: Model string (e.g. ``"databricks-gpt-5-5"``), or
        ``None`` when unavailable.
    """
    if conv is None or conv.agent_id is None:
        return None
    try:
        # Import ``get_agent_cache`` from the runtime at call time so a test
        # patching ``omnigent.runtime.get_agent_cache`` is honored (the
        # module-level name is a facade proxy that bypasses that patch).
        from omnigent.runtime import get_agent_cache

        agent_cache = get_agent_cache()
        # The agent store is injected at app startup; access it
        # through the runtime globals.
        from omnigent.runtime._globals import _agent_store

        if _agent_store is None:
            return None
        agent = _agent_store.get(conv.agent_id)
        if agent is None:
            return None
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
        return loaded.spec.llm.model if loaded.spec.llm else None
    except (KeyError, AttributeError, ValueError, ImportError, OSError, RuntimeError):
        # ``RuntimeError`` covers ``get_agent_cache()`` before the runtime is
        # initialized: this is a best-effort display resolver (now also called
        # on native cost-only broadcasts), so an uninitialized runtime must
        # degrade to "model unknown" — the cost still records, just unattributed.
        return None


def _resolve_harness(*args: Any, **kwargs: Any) -> str | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return _facade._resolve_harness(*args, **kwargs)


def _resolve_harness_impl(conv: Conversation | None) -> str | None:
    """
    Resolve the canonical harness for a conversation's bound agent.

    Mirrors :func:`_resolve_llm_model`: loads the parsed spec via the agent
    cache and returns the executor's harness
    (``executor.config["harness"]``, else ``executor.type``), canonicalized.
    Surfacing this on :class:`SessionResponse` lets the REPL render the
    active credential for the correct provider *family* — anthropic for
    claude-sdk, openai for codex / openai-agents — instead of guessing the
    family from the model string (which is wrong when the agent declares no
    model, e.g. a generic-provider launcher).

    :param conv: The conversation entity, or ``None``.
    :returns: The canonical harness (e.g. ``"openai-agents"`` or
        ``"claude-sdk"``), or ``None`` when unavailable.
    """
    if conv is None:
        return None
    # A persisted per-session override (validated + canonicalized at
    # create) wins over the spec's declared harness, so the snapshot
    # reports what the runner actually spawns.
    if conv.harness_override:
        return conv.harness_override
    if conv.agent_id is None:
        return None
    try:
        from omnigent.harness_aliases import canonicalize_harness
        from omnigent.runtime import get_agent_cache
        from omnigent.runtime._globals import _agent_store

        if _agent_store is None:
            return None
        agent = _agent_store.get(conv.agent_id)
        if agent is None:
            return None
        loaded = get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
        executor = loaded.spec.executor
        # For a bundled-agent head sub-agent, report the HEAD's own harness,
        # not the bundle brain's — `harness` is this session's provider family
        # (a gpt head runs codex, not the claude-sdk brain). Falls back to the
        # brain harness when the head declares none or can't be matched.
        if conv.sub_agent_name:
            sub = next(
                (s for s in loaded.spec.sub_agents if s.name == conv.sub_agent_name),
                None,
            )
            if sub is not None:
                executor = sub.executor
        harness = (
            executor.config.get("harness")
            or loaded.spec.executor.config.get("harness")
            or executor.type
        )
        return canonicalize_harness(harness) or harness
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        return None


def _validated_harness_override(value: str | None, agent: Agent) -> str | None:
    """
    Validate + canonicalize a session-create ``harness_override``.

    Mirrors the CLI's ``--harness`` rules (``_apply_harness_override_to_executor``
    in ``omnigent/chat.py``): the canonical name must be a known bundle
    harness, and the bound agent must be an ``executor.type: omnigent``
    spec — other executor types have no ``config.harness``, so an
    override there would be a silent no-op.

    :param value: The raw override from the request body, e.g. ``"pi"``
        or the ``"openai-agents-sdk"`` alias. ``None`` means no override.
    :param agent: The bound agent row (already fetched by the caller).
    :returns: The canonical harness id, or ``None`` when *value* is.
    :raises OmnigentError: ``invalid_input`` for an unknown harness, a
        non-omnigent executor type, or an unloadable agent bundle.
    """
    if value is None:
        return None
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.runtime import get_agent_cache
    from omnigent.spec._omnigent_compat import (
        OMNIGENT_EXECUTOR_TYPE,
        OMNIGENT_HARNESSES,
    )

    canonical = canonicalize_harness(value) or value
    if canonical not in OMNIGENT_HARNESSES:
        raise OmnigentError(
            f"invalid harness_override: must be one of "
            f"{sorted(OMNIGENT_HARNESSES)}, got {value!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    try:
        loaded = get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
    except (KeyError, AttributeError, ValueError, ImportError, OSError) as exc:
        raise OmnigentError(
            f"harness_override requires a loadable agent spec; "
            f"agent {agent.name!r} failed to load: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    executor_type = loaded.spec.executor.type
    if executor_type != OMNIGENT_EXECUTOR_TYPE:
        raise OmnigentError(
            f"harness_override only applies to executor.type "
            f"{OMNIGENT_EXECUTOR_TYPE!r} agents; agent {agent.name!r} "
            f"declares executor.type {executor_type!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return canonical


def _validated_harness_override_executor_type(agent: Agent) -> None:
    """Validate that *agent* is an ``executor.type: omnigent`` spec.

    Used by the ``"auto"`` harness path to enforce the same executor-type
    gate as :func:`_validated_harness_override` without requiring a concrete
    harness name (the real harness is resolved at first-message time).

    :raises OmnigentError: ``invalid_input`` when the agent is not an
        omnigent executor type or the bundle cannot be loaded.
    """
    from omnigent.runtime import get_agent_cache
    from omnigent.spec._omnigent_compat import OMNIGENT_EXECUTOR_TYPE

    try:
        loaded = get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
    except (KeyError, AttributeError, ValueError, ImportError, OSError) as exc:
        raise OmnigentError(
            f"harness_override 'auto' requires a loadable agent spec; "
            f"agent {agent.name!r} failed to load: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    executor_type = loaded.spec.executor.type
    if executor_type != OMNIGENT_EXECUTOR_TYPE:
        raise OmnigentError(
            f"harness_override 'auto' only applies to executor.type "
            f"{OMNIGENT_EXECUTOR_TYPE!r} agents; agent {agent.name!r} "
            f"declares executor.type {executor_type!r}",
            code=ErrorCode.INVALID_INPUT,
        )


def _utc_day(epoch_seconds: int) -> str:
    """
    Convert a Unix epoch timestamp to its UTC calendar day.

    :param epoch_seconds: Unix epoch seconds, e.g. ``1749081600``.
    :returns: The UTC date as ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
    """
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date().isoformat()


def _record_daily_cost(
    conv: Conversation | None,
    delta_usd: float,
    conversation_store: ConversationStore,
) -> None:
    """
    Add a turn's LLM cost to the session owner's daily rollup.

    A no-op when *delta_usd* is not positive or the session has no
    resolvable owner. Attributes the cost to the session creator
    (:meth:`ConversationStore.get_session_owner`) and buckets it by the
    current UTC day, so a session spanning midnight splits its spend
    across both days. Recorded for every priced turn regardless of
    whether the session runs under a policy — the daily rollup is the
    backing store for the per-user daily cost-budget policy, and is now
    populated universally. (This relies on the conversation store
    implementing the daily-cost methods on every deployment that runs
    this code; the earlier policy gate that kept the managed deployment
    from touching an absent ``user_daily_cost`` table is no longer needed
    now that the managed store backs it.)

    Sub-agent conversations are created without a permission grant (the
    internal runner POST carries no user context), so
    ``get_session_owner(conv.id)`` returns ``None`` for them.  When
    that happens, fall back to the spawn-tree root's owner: every
    conversation carries ``root_conversation_id`` pointing to the
    top-level session that *was* created with user context and therefore
    always has an owner grant.  This ensures relay / SDK sub-agent spend
    is attributed to the same user as the parent rather than silently
    dropped from the daily rollup.

    :param conv: The conversation row for the session, or ``None``
        (a no-op — no owner to attribute to).
    :param delta_usd: The turn's cost in USD; ``<= 0`` is a no-op.
    :param conversation_store: Store for the owner lookup and the
        daily-cost UPSERT.
    """
    if conv is None or delta_usd <= 0:
        return
    owner = conversation_store.get_session_owner(conv.id)
    if owner is None and conv.root_conversation_id != conv.id:
        # Sub-agent: no direct owner grant — fall back to the root session's
        # owner so sub-agent spend is attributed rather than silently dropped.
        owner = conversation_store.get_session_owner(conv.root_conversation_id)
    if owner is None:
        return
    from omnigent.db.utils import now_epoch

    conversation_store.add_daily_cost(owner, _utc_day(now_epoch()), delta_usd)


def _priced_cost_for_display(usage: dict[str, Any]) -> float | None:
    """
    Extract ``total_cost_usd`` for client display, or ``None`` when unpriced.

    The key is present only when a turn was priced, so its absence ("—" in
    the UI) is distinct from a priced ``$0.00``. The cost-budget policy is
    unaffected — it reads the value with a ``0.0`` default.

    :param usage: A conversation's ``session_usage`` dict, e.g.
        ``{"input_tokens": 1200, "total_cost_usd": 0.42}`` (priced) or
        ``{"input_tokens": 1200}`` (unpriced — no cost key).
    :returns: The cumulative cost in USD when priced, else ``None``.
    """
    if "total_cost_usd" not in usage:
        return None
    try:
        return float(usage["total_cost_usd"])
    except (TypeError, ValueError):
        # Defensive: a malformed persisted value must not break the
        # snapshot / SSE emit. Treat it as unpriced.
        return None


def _model_usage_bucket(usage: dict[str, Any], model: str) -> dict[str, float]:
    """
    Get-or-create the per-model usage sub-bucket inside ``usage["by_model"]``.

    The nested ``by_model`` map attributes token/cost usage to the specific
    LLM that produced it, keyed on the raw harness-reported model id (faithful
    and simplest — alias normalization is intentionally deferred). This mutates
    ``usage`` in place, creating ``by_model`` and the per-model dict on first
    use, and returns the model's bucket for the caller to increment / set.

    :param usage: The conversation's mutable ``session_usage`` dict.
    :param model: The raw harness model id, e.g. ``"claude-sonnet-4-6"`` or
        ``"databricks-gpt-5-5"``.
    :returns: The mutable per-model bucket, e.g. ``{"input_tokens": 1200}``.
    """
    by_model = usage.setdefault("by_model", {})
    return by_model.setdefault(model, {})


def _add_model_usage_delta(
    bucket: dict[str, float],
    token_deltas: dict[str, int],
    cost_delta: float | None,
) -> None:
    """
    Add one turn's per-model token/cost deltas into a model bucket (ADD).

    Mirrors the flat-counter increments in :func:`_accumulate_session_usage`
    so the per-model totals stay consistent with the flat totals: every flat
    increment is matched by an increment to exactly one model bucket, so the
    sum of per-model buckets equals the flat total. ``cost_delta`` is added
    only when the turn was priced (``None`` otherwise), preserving the
    "priced ⟺ ``total_cost_usd`` key present" contract at the per-model level.

    :param bucket: The model's mutable bucket from :func:`_model_usage_bucket`.
    :param token_deltas: This turn's per-bucket token counts to add, keyed by
        the same names as :data:`_TOKEN_BREAKDOWN_KEYS`, e.g.
        ``{"input_tokens": 1200, "output_tokens": 340, ...}``.
    :param cost_delta: This turn's priced cost in USD to add, or ``None`` when
        the turn was unpriced (the model's cost key stays absent).
    """
    for key, delta in token_deltas.items():
        bucket[key] = bucket.get(key, 0) + delta
    if cost_delta is not None:
        bucket["total_cost_usd"] = bucket.get("total_cost_usd", 0.0) + cost_delta


def _usage_by_model_for_display(usage: dict[str, Any]) -> dict[str, ModelUsage] | None:
    """
    Project the nested ``by_model`` usage map into typed :class:`ModelUsage`.

    Companion to :func:`_token_breakdown_for_display` for the per-model view:
    reads ``usage["by_model"]`` (the subtree-summed map from
    :func:`load_session_usage`) and builds a ``{model_id: ModelUsage}`` dict
    for the API. Token buckets are coerced to ``int`` and ``total_cost_usd``
    to ``float``; an absent bucket stays ``None`` on the model (so a model
    that was never priced has no cost), and malformed values are skipped.

    :param usage: A subtree-summed usage dict, e.g.
        ``{"input_tokens": 1500, "by_model": {"claude-sonnet-4-6":
        {"input_tokens": 1500, "total_cost_usd": 0.42}}}``.
    :returns: The per-model map, or ``None`` when no per-model usage is
        present (so ``exclude_none`` omits the field entirely).
    """
    by_model = usage.get("by_model")
    if not isinstance(by_model, dict) or not by_model:
        return None
    result: dict[str, ModelUsage] = {}
    for model, bucket in by_model.items():
        if not isinstance(bucket, dict):
            continue
        fields: dict[str, Any] = {}
        for key in _MODEL_TOKEN_KEYS:
            value = bucket.get(key)
            if value is None:
                continue
            try:
                fields[key] = int(value)
            except (TypeError, ValueError):
                continue
        cost = _priced_cost_for_display(bucket)
        if cost is not None:
            fields["total_cost_usd"] = cost
        result[model] = ModelUsage(**fields)
    return result or None


def _coerce_cumulative_field(
    data: dict[str, Any],
    key: str,
    *,
    numeric: bool,
) -> float | int | None:
    """
    Read and validate an optional cumulative usage field from event data.

    :param data: The ``external_session_usage`` event ``data`` dict.
    :param key: Field name, e.g. ``"cumulative_input_tokens"``.
    :param numeric: When ``True`` accept any non-negative number (cost);
        when ``False`` require a non-negative int (token counts).
    :returns: The validated value, or ``None`` when the key is absent.
    :raises OmnigentError: When present but the wrong type / negative.
    """
    value = data.get(key)
    if value is None:
        return None
    ok = (
        isinstance(value, (int, float)) if numeric else isinstance(value, int)
    ) and not isinstance(value, bool)
    if not ok or value < 0:
        raise OmnigentError(
            f"external_session_usage data.{key} must be a non-negative "
            f"{'number' if numeric else 'int'}",
            code=ErrorCode.INVALID_INPUT,
        )
    return value


async def _persist_external_model_change(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> None:
    """
    Persist and broadcast a model switch made inside the terminal.

    Mirrors a ``/model`` change typed into a claude-native session's
    Claude Code pane (or picked via its in-TUI model picker) onto the
    Omnigent session: writes ``model_override`` so the value survives reload
    and publishes a ``session.model`` SSE event so the web picker
    updates live. Unlike the PATCH path
    (:func:`update_session`), this deliberately does NOT forward a
    ``model_change`` back to the runner — the terminal is already on
    the model, so re-injecting ``/model`` would loop.

    No-ops (no write, no event) when the observed model already equals
    the persisted ``model_override`` — the common case on the web→TUI
    round-trip where the web PATCH set the override moments earlier.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id`` (read at the route
        boundary); ``conv.model_override`` is the dedupe baseline.
    :param body: External model-change event body. ``data.model`` must
        be a non-empty string tier alias, e.g. ``"opus"``.
    :param conversation_store: Store used to upsert ``model_override``.
    :raises OmnigentError: If ``data.model`` is missing or not a
        non-empty string.
    """
    raw_model = body.data.get("model")
    if not isinstance(raw_model, str) or not raw_model.strip():
        raise OmnigentError(
            "external_model_change requires data.model to be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    model = raw_model.strip()
    if conv.model_override == model:
        return
    await asyncio.to_thread(
        conversation_store.update_conversation,
        session_id,
        model_override=model,
    )
    event = SessionModelEvent(
        type="session.model",
        conversation_id=session_id,
        model=model,
    )
    session_stream.publish(session_id, event.model_dump())


def _persist_external_model_options(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
) -> None:
    """
    Record the model catalog a native harness's extension reported.

    Sourced from the harness's live model registry (pi-native:
    ``ctx.modelRegistry.getAvailable()``), so it reflects the models the
    harness actually loaded no matter how it authenticated — an
    Omnigent-configured provider OR the harness's own ``/login``. This is why
    the pi picker populates even in the ``/login`` path, where no
    ``models.json`` is written into the bridge dir for a file-read to find.

    Gated to the pi-native wrapper: only :func:`_fetch_model_options` *serves*
    this cache for pi-native, so accepting a push from any other session would
    just leave a stray cache entry alive until teardown. Reject at ingest to
    keep the contract explicit.

    Stores into :data:`_pushed_model_options_cache` (which a browser reload
    does NOT clear — the extension only pushes on session start) and publishes
    ``session.model_options`` so open clients re-read the snapshot. An empty
    list evicts the entry rather than caching nothing.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row whose labels identify the wrapper.
    :param body: External model-options event body. ``data.models`` must be a
        list of ``{"id": str, ...}`` objects.
    :raises OmnigentError: If the session is not pi-native, or ``data.models``
        is missing or malformed.
    """
    if conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY) != _PI_NATIVE_WRAPPER_LABEL_VALUE:
        raise OmnigentError(
            "external_model_options is only accepted for pi-native sessions",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_models = body.data.get("models")
    if not isinstance(raw_models, list):
        raise OmnigentError(
            "external_model_options requires data.models to be a list",
            code=ErrorCode.INVALID_INPUT,
        )
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_models:
        model_id = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        display = raw.get("displayName") if isinstance(raw, dict) else None
        options.append(
            {
                "id": model_id,
                "displayName": display if isinstance(display, str) and display else model_id,
                "isDefault": bool(raw.get("isDefault", False)) if isinstance(raw, dict) else False,
            }
        )
    if options:
        _pushed_model_options_cache[session_id] = options
    else:
        _pushed_model_options_cache.pop(session_id, None)
    _publish_model_options(session_id)


def _validate_external_reasoning_effort(body: SessionEventInput) -> str | None:
    """
    Validate a terminal-observed reasoning-effort payload.

    :param body: External effort-change event body. ``data.reasoning_effort``
        must be present and either ``None`` or a supported effort string, e.g.
        ``"medium"``.
    :returns: Normalized effort string, or ``None`` when the terminal cleared
        to its default effort.
    :raises OmnigentError: If the payload is missing or unsupported.
    """
    if "reasoning_effort" not in body.data:
        raise OmnigentError(
            "external_reasoning_effort_change requires data.reasoning_effort",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_effort = body.data["reasoning_effort"]
    if raw_effort is None:
        return None
    if not isinstance(raw_effort, str) or not raw_effort.strip():
        raise OmnigentError(
            "external_reasoning_effort_change requires data.reasoning_effort "
            "to be a non-empty string or null",
            code=ErrorCode.INVALID_INPUT,
        )
    effort = raw_effort.strip()
    try:
        return validate_effort(effort, "session metadata", EFFORT_VALUES)
    except ValueError as exc:
        raise OmnigentError(
            f"invalid reasoning_effort: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


async def _persist_external_reasoning_effort_change(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> None:
    """
    Persist and broadcast a reasoning-effort switch made inside the terminal.

    Mirrors a native-terminal thinking-level change onto the Omnigent session.
    Unlike the public PATCH path, this deliberately does NOT forward an
    ``effort_change`` back to the runner: the terminal is already on that
    effort, so re-injecting it would loop.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id`` at the route boundary.
    :param body: External effort-change event body.
    :param conversation_store: Store used to update ``reasoning_effort``.
    :returns: None.
    """
    effort = _validate_external_reasoning_effort(body)
    if conv.reasoning_effort == effort:
        return
    await asyncio.to_thread(
        conversation_store.update_conversation,
        session_id,
        reasoning_effort=effort,
        _unset_reasoning_effort=effort is None,
    )
    event = SessionReasoningEffortEvent(
        type="session.reasoning_effort",
        conversation_id=session_id,
        reasoning_effort=effort,
    )
    session_stream.publish(session_id, event.model_dump())


async def _persist_external_codex_collaboration_mode_change(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> None:
    """
    Persist Codex's collaboration mode kind as an internal session label.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id`` at the route boundary.
    :param body: External Codex mode-change event body. ``data.mode`` must be
        ``"default"`` or ``"plan"``.
    :param conversation_store: Store used to upsert the mode label.
    :returns: None.
    :raises OmnigentError: If ``data.mode`` is missing or unsupported.
    """
    raw_mode = body.data.get("mode")
    if not isinstance(raw_mode, str) or not raw_mode.strip():
        raise OmnigentError(
            "external_codex_collaboration_mode_change requires data.mode to be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    mode = raw_mode.strip()
    if mode not in _CODEX_NATIVE_COLLABORATION_MODES:
        raise OmnigentError(
            "external_codex_collaboration_mode_change requires data.mode in "
            f"{sorted(_CODEX_NATIVE_COLLABORATION_MODES)}; got {mode!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    if conv.labels.get(_CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY) == mode:
        return
    await asyncio.to_thread(
        conversation_store.set_labels,
        session_id,
        {_CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY: mode},
    )
    _publish_collaboration_mode(session_id, mode)


def _handle_external_session_todos(
    session_id: str,
    body: SessionEventInput,
) -> None:
    """
    Cache and broadcast a todo-list update from a native forwarder.

    Sent by the claude-native forwarder (from ``TodoWrite``) and the
    codex-native forwarder (from Codex plan updates); the panel is
    harness-agnostic.

    Updates the in-memory ``_session_todos_cache`` so subsequent
    ``GET /v1/sessions/{id}`` snapshot calls can populate the ``todos``
    field without a file read. Then publishes a ``session.todos`` SSE event
    so connected web clients update their todo panel immediately.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param body: The ``external_session_todos`` event body. Must have
        ``data.todos`` as a list of todo dicts, e.g.
        ``[{"content": "Fix bug", "status": "in_progress", "activeForm": "Fixing the bug"}]``.
    :raises OmnigentError: When ``data.todos`` is missing or not a list.
    """
    todos = body.data.get("todos")
    if not isinstance(todos, list):
        raise OmnigentError(
            "external_session_todos requires data.todos to be a list",
            code=ErrorCode.INVALID_INPUT,
        )
    # Filter to well-formed items before caching so that malformed entries
    # from a buggy forwarder version don't persist in the snapshot.  The
    # same filter is applied by sse.ts on the live-event path; keeping the
    # two in sync means the snapshot and live panel always show the same set.
    valid_statuses = {"pending", "in_progress", "completed"}
    validated: list[dict[str, Any]] = [
        t
        for t in todos
        if isinstance(t, dict)
        and isinstance(t.get("content"), str)
        and t.get("status") in valid_statuses
        and isinstance(t.get("activeForm"), str)
    ]
    _session_todos_cache[session_id] = validated
    event = SessionTodosEvent(
        type="session.todos",
        conversation_id=session_id,
        todos=validated,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_external_conversation_item(
    session_id: str,
    item: ConversationItem,
    cleared_pending_id: str | None = None,
) -> None:
    """
    Broadcast a terminal-observed conversation item.

    User messages use ``session.input.consumed`` so the web UI renders
    them exactly like local/composer messages. Assistant/tool-side
    items use ``response.output_item.done`` because they are already
    completed records from Claude's transcript, not token deltas from
    an active Omnigent task.

    :param session_id: Session/conversation identifier.
    :param item: Persisted conversation item.
    :param cleared_pending_id: For a native user message, the id of the
        optimistic pending-input entry the caller drained for it (so
        clients drop that bubble by id), or ``None``. The drain happens
        at the persist site — see :func:`_persist_external_conversation_item`
        — because it also folds the entry's file blocks into the durable
        item before append.
    :returns: None.
    """
    if item.type == "message" and isinstance(item.data, MessageData) and item.data.is_meta:
        return
    if item.type == "message" and isinstance(item.data, MessageData) and item.data.role == "user":
        _publish_input_consumed(session_id, item, cleared_pending_id=cleared_pending_id)
        return
    event = OutputItemDoneEvent(type="response.output_item.done", item=item.to_api_dict())
    session_stream.publish(session_id, event.model_dump())


def _publish_external_output_text_delta(session_id: str, body: SessionEventInput) -> None:
    """
    Broadcast a terminal-observed assistant text delta.

    Terminal-backed integrations can observe streaming output before
    their completed transcript item is available. This publishes the
    standard Responses-style text-delta SSE event without persisting
    anything; the final assistant message is persisted separately when
    the integration posts ``external_conversation_item``.

    The optional ``message_id`` / ``index`` / ``final`` fields are
    carried through when present (claude-native live streaming) and
    omitted otherwise — ``exclude_none`` keeps the wire shape identical
    to in-process task streaming for callers that don't set them.

    :param session_id: Session/conversation identifier.
    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_OUTPUT_TEXT_DELTA_TYPE`.
    :returns: None.
    :raises OmnigentError: If ``data.delta`` is not a string, or any
        provided ``message_id`` / ``index`` / ``final`` has the wrong
        type.
    """
    delta = body.data.get("delta")
    if not isinstance(delta, str):
        raise OmnigentError(
            "external_output_text_delta requires string data.delta",
            code=ErrorCode.INVALID_INPUT,
        )
    message_id = body.data.get("message_id")
    if message_id is not None and not isinstance(message_id, str):
        raise OmnigentError(
            "external_output_text_delta data.message_id must be a string",
            code=ErrorCode.INVALID_INPUT,
        )
    index = body.data.get("index")
    # ``bool`` is an ``int`` subclass; reject it explicitly so a stray
    # boolean index is a loud error rather than a silent 0/1.
    if index is not None and (not isinstance(index, int) or isinstance(index, bool)):
        raise OmnigentError(
            "external_output_text_delta data.index must be an integer",
            code=ErrorCode.INVALID_INPUT,
        )
    final = body.data.get("final")
    if final is not None and not isinstance(final, bool):
        raise OmnigentError(
            "external_output_text_delta data.final must be a boolean",
            code=ErrorCode.INVALID_INPUT,
        )
    event = OutputTextDeltaEvent(
        type="response.output_text.delta",
        delta=delta,
        message_id=message_id,
        index=index,
        final=final,
    )
    session_stream.publish(session_id, event.model_dump(exclude_none=True))


def _publish_external_tool_output_delta(session_id: str, body: SessionEventInput) -> None:
    """Broadcast a terminal-observed function-call output delta.

    :param session_id: Session/conversation identifier.
    :param body: Event body containing string ``call_id`` and ``delta`` values.
    :returns: None.
    :raises OmnigentError: If either required value is missing or not a string.
    """
    call_id = body.data.get("call_id")
    delta = body.data.get("delta")
    if not isinstance(call_id, str) or not call_id:
        raise OmnigentError(
            "external_tool_output_delta requires non-empty string data.call_id",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(delta, str):
        raise OmnigentError(
            "external_tool_output_delta requires string data.delta",
            code=ErrorCode.INVALID_INPUT,
        )
    event = ToolOutputDeltaEvent(
        type="response.function_call_output.delta",
        call_id=call_id,
        delta=delta,
    )
    session_stream.publish(session_id, event.model_dump(exclude_none=True))


def _publish_external_output_reasoning_delta(session_id: str, body: SessionEventInput) -> None:
    """
    Broadcast a terminal-observed reasoning (chain-of-thought) delta.

    The reasoning analogue of :func:`_publish_external_output_text_delta`:
    terminal-backed integrations (the antigravity-native reader) observe a
    streaming ``thinking`` block before the completed assistant item exists. This
    publishes the standard reasoning SSE events the SPA already renders —
    ``response.reasoning.started`` once (when ``data.started`` is true, marking a
    new reasoning block) followed by ``response.reasoning_text.delta`` — without
    persisting anything. Reasoning has no completed conversation item; the block
    is finalized when the assistant message is persisted via
    ``external_conversation_item``.

    :param session_id: Session/conversation identifier.
    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_OUTPUT_REASONING_DELTA_TYPE`.
    :returns: None.
    :raises OmnigentError: If ``data.delta`` is not a string, or ``data.started``
        is provided with a non-boolean type.
    """
    delta = body.data.get("delta")
    if not isinstance(delta, str):
        raise OmnigentError(
            "external_output_reasoning_delta requires string data.delta",
            code=ErrorCode.INVALID_INPUT,
        )
    started = body.data.get("started")
    if started is not None and not isinstance(started, bool):
        raise OmnigentError(
            "external_output_reasoning_delta data.started must be a boolean",
            code=ErrorCode.INVALID_INPUT,
        )
    if started:
        session_stream.publish(
            session_id,
            ReasoningStartedEvent(type="response.reasoning.started").model_dump(exclude_none=True),
        )
    event = ReasoningTextDeltaEvent(type="response.reasoning_text.delta", delta=delta)
    session_stream.publish(session_id, event.model_dump(exclude_none=True))


def _publish_elicitation_resolved(session_id: str, elicitation_id: str) -> None:
    """
    Universal "approval done" signal — single publish drives both
    sidebar (via :func:`pending_elicitations.record_publish` decrement)
    and the chat-side ``ApprovalCard`` flip on every live subscriber.
    Idempotent on duplicate emissions for the same id.

    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Correlation id, e.g. ``"elicit_abc123"``.
    """
    session_stream.publish(
        session_id,
        {
            "type": "response.elicitation_resolved",
            "elicitation_id": elicitation_id,
        },
    )


async def _forward_approval_to_runner(
    session_id: str,
    data: dict[str, Any],
    runner_router: RunnerRouter | None,
) -> None:
    """
    Forward an approval verdict to the session's bound runner.

    Runner-side elicitations (policy approvals parked in the runner's
    ``_pending_approvals`` dict, scaffold dispatch) resolve when the
    canonical ``approval`` event reaches the runner's ``/events``. The
    server↔runner contract stays the ``approval`` event regardless of
    how the verdict arrived at the server (resolve URL or approval
    event). No-op when no runner is bound (in-process setups). HTTP
    errors are logged, not raised — a dead runner must not fail the
    caller's resolution (the server-side Future was already set).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param data: The approval payload to forward verbatim as the
        event ``data``, e.g. ``{"elicitation_id": "elicit_abc",
        "action": "accept"}``.
    :param runner_router: Router used to resolve the bound runner, or
        ``None`` in in-process setups (forward skipped).
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return
    try:
        await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": _APPROVAL_TYPE, "data": data},
            timeout=10.0,
        )
    except (httpx.HTTPError, ConnectionError):
        _logger.exception(
            "Approval forward failed for %r",
            session_id,
        )


def _parse_external_assistant_message(
    body: SessionEventInput,
) -> tuple[str, str, str]:
    """
    Validate and unpack an external assistant-message event.

    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_ASSISTANT_MESSAGE_TYPE`.
    :returns: ``(agent_name, text, response_id)``.
    :raises OmnigentError: If required fields are missing or
        malformed.
    """
    agent_name = body.data.get("agent")
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise OmnigentError(
            "external_assistant_message requires data.agent",
            code=ErrorCode.INVALID_INPUT,
        )
    text = body.data.get("text")
    if not isinstance(text, str) or not text:
        raise OmnigentError(
            "external_assistant_message requires non-empty data.text",
            code=ErrorCode.INVALID_INPUT,
        )
    response_id = body.data.get("response_id")
    if response_id is None:
        response_id = generate_task_id()
    if not isinstance(response_id, str) or not response_id.strip():
        raise OmnigentError(
            "external_assistant_message data.response_id must be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    return agent_name.strip(), text, response_id.strip()


async def _persist_external_assistant_message(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Persist and broadcast assistant text produced outside Omnigent tasks.

    The event is append-only conversation history. It intentionally
    bypasses the legacy persist path so mirroring a
    Claude terminal response does not create or steer an Omnigent
    agent task.

    :param session_id: Session/conversation identifier.
    :param body: External assistant-message event body.
    :param conversation_store: Store used to append the message.
    :returns: Store-assigned conversation item id.
    """
    agent_name, text, response_id = _parse_external_assistant_message(body)
    item = NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role="assistant",
            agent=agent_name,
            content=[{"type": "output_text", "text": text}],
        ),
    )
    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    persisted = persisted_items[0]
    _publish_external_assistant_message(
        session_id,
        persisted,
        response_id=response_id,
        agent_name=agent_name,
    )
    return persisted.id


def _parse_external_conversation_item(
    body: SessionEventInput,
) -> NewConversationItem:
    """
    Validate and unpack an external conversation-item event.

    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_CONVERSATION_ITEM_TYPE`.
    :returns: A parsed :class:`NewConversationItem` ready to append.
    :raises OmnigentError: If required fields are missing or
        malformed.
    """
    item_type = body.data.get("item_type")
    if not isinstance(item_type, str) or item_type not in ITEM_TYPE_TO_DATA_CLS:
        raise OmnigentError(
            "external_conversation_item requires known data.item_type",
            code=ErrorCode.INVALID_INPUT,
        )
    item_data = body.data.get("item_data")
    if not isinstance(item_data, dict):
        raise OmnigentError(
            "external_conversation_item requires object data.item_data",
            code=ErrorCode.INVALID_INPUT,
        )
    response_id = body.data.get("response_id")
    if response_id is None:
        response_id = generate_task_id()
    if not isinstance(response_id, str) or not response_id.strip():
        raise OmnigentError(
            "external_conversation_item data.response_id must be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    # NOTE: external conversation items are persisted with a random
    # primary key like any other item — there is no server-side dedup.
    # Producers (the claude-native / codex-native forwarders) are
    # responsible for not re-posting records they have already sent;
    # they no longer emit a ``source_id`` dedup key to the server.
    # Cap a native tool result so a multi-MB output isn't persisted + broadcast as one frame.
    if item_type == "function_call_output" and isinstance(item_data.get("output"), str):
        item_data = {**item_data, "output": cap_tool_output(item_data["output"])}
    try:
        data = parse_item_data(item_type, {"type": item_type, **item_data})
    except (ValueError, TypeError) as exc:
        raise OmnigentError(
            f"Invalid data payload for external item type {item_type!r}: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    return NewConversationItem(
        type=item_type,
        response_id=response_id.strip(),
        data=data,
    )


def _find_claude_native_subagent_child(
    conversation_store: ConversationStore,
    parent_id: str,
    subagent_id: str,
) -> Conversation | None:
    """
    Look up an existing claude-native sub-agent child by its Claude-
    side ``subagent_id``.

    Used to make :func:`_persist_external_subagent_start` idempotent:
    the forwarder retries on transient HTTP errors, so two POSTs may
    carry the same ``subagent_id`` for the same physical sub-agent —
    we want both to resolve to the same child Conversation row.

    :param conversation_store: Store to query.
    :param parent_id: Parent (claude-native) conversation id,
        e.g. ``"conv_parent987"``.
    :param subagent_id: Stable Claude-side identifier read from
        ``agent-<id>.meta.json``'s directory name, e.g.
        ``"a5c7effac5a9a35ab"``.
    :returns: The matching child :class:`Conversation`, or ``None``
        when no row has been minted for this sub-agent yet.
    """
    # Page through all children so the lookup isn't capped by result
    # ordering. A parent with > 100 sub-agents would otherwise miss the
    # existing row for an older ``subagent_id`` and fall through to
    # ``create_conversation``, which then trips the
    # ``(parent, title)`` unique constraint instead of returning the
    # existing child id.
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            kind="sub_agent",
            parent_conversation_id=parent_id,
            limit=100,
            after=after,
        )
        for child in page.data:
            if child.labels.get(_CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY) == subagent_id:
                return child
        if not page.has_more or page.last_id is None:
            return None
        after = page.last_id


def _find_subagent_child_by_title(
    conversation_store: ConversationStore,
    parent_id: str,
    title: str,
) -> Conversation | None:
    """
    Look up an existing sub-agent child by its exact title.

    Recovery path for duplicate-title races: when ``create_conversation``
    trips the ``(parent_conversation_id, title)`` unique index but the
    label-based idempotency lookup missed — the original POST crashed
    after creating the row and before ``set_labels`` ran — the row can
    only be found by the title itself. Native sub-agent titles embed the
    stable harness-side id (e.g. ``"Explore:a5c7effac5a9a35ab"``,
    ``"codex-native-ui-subagent:<thread_id>"``), so an exact title match
    under the same parent identifies the same physical sub-agent.

    :param conversation_store: Store to query.
    :param parent_id: Parent conversation id, e.g. ``"conv_parent987"``.
    :param title: Exact child title, e.g. ``"Explore:a5c7effac5a9a35ab"``.
    :returns: Matching child :class:`Conversation`, or ``None`` when no
        row under *parent_id* carries that title.
    """
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            kind="sub_agent",
            parent_conversation_id=parent_id,
            limit=100,
            after=after,
        )
        for child in page.data:
            if child.title == title:
                return child
        if not page.has_more or page.last_id is None:
            return None
        after = page.last_id


def _publish_session_created(
    parent_id: str,
    child_session_id: str,
    agent_id: str | None,
) -> None:
    """
    Emit ``session.created`` on the parent's stream for a child session.

    Clients watching the parent (e.g. the web Subagents rail tab)
    invalidate their ``child_sessions`` cache and re-fetch on this
    event.

    :param parent_id: Parent conversation id, e.g. ``"conv_parent987"``.
    :param child_session_id: The minted (or adopted) child id, e.g.
        ``"conv_child456"``.
    :param agent_id: Agent id stamped on the child (the parent's
        agent), e.g. ``"ag_abc123"``. ``None`` only for legacy parents
        without one.
    """
    event = SessionCreatedEvent(
        type="session.created",
        conversation_id=parent_id,
        child_session_id=child_session_id,
        agent_id=agent_id,
        parent_session_id=parent_id,
    )
    session_stream.publish(parent_id, event.model_dump())


async def _persist_external_subagent_start(
    parent_id: str,
    parent_conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Mint a child :class:`Conversation` row for a claude-native
    sub-agent and emit the parent's ``session.created`` SSE event.

    Claude Code spawns sub-agents internally via its Task tool and
    never POSTs to Omnigent to register them. The forwarder watches the
    parent's on-disk ``subagents/`` directory and calls this handler
    when a new ``.meta.json`` appears. We reuse the parent's
    ``agent_id`` (claude-native sub-agents don't have their own
    omnigent agent), stamp identifying labels, and publish the
    same ``session.created`` event omnigent-spawned children fire
    so the rail's ``child_sessions`` cache invalidates.

    Idempotent: a second POST with the same ``subagent_id`` returns
    the existing child's id without creating a duplicate — via the
    label lookup when the row is fully stamped, or via title-collision
    recovery when an earlier POST died between ``create_conversation``
    and ``set_labels`` (the recovery also re-stamps the labels so the
    row is healed for subsequent deliveries).

    :param parent_id: Parent (claude-native) conversation id,
        e.g. ``"conv_parent987"``.
    :param parent_conv: Pre-fetched parent row — its ``agent_id`` is
        copied onto the child and its labels disambiguate
        claude-native parents from other harnesses.
    :param body: The POST event body. Required ``data`` keys:
        ``subagent_id`` (Claude-side id, e.g. ``"a5c7eff..."``),
        ``agent_type`` (e.g. ``"Explore"``), ``description``
        (free-form, used in the title), ``tool_use_id``
        (e.g. ``"toolu_..."``).
    :param conversation_store: Store used to read existing children
        (for idempotency) and create the new row.
    :returns: The child conversation id, e.g. ``"conv_child456"``.
    :raises OmnigentError: 400 if the payload is missing any of
        the required keys; 400 if the parent has no ``agent_id``
        (claude-native parents always carry one, so this would be
        a corrupted row).
    """
    subagent_id = body.data.get("subagent_id")
    agent_type = body.data.get("agent_type")
    description = body.data.get("description")
    tool_use_id = body.data.get("tool_use_id")
    if not isinstance(subagent_id, str) or not subagent_id:
        raise OmnigentError(
            "external_subagent_start requires non-empty data.subagent_id",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(agent_type, str) or not agent_type:
        raise OmnigentError(
            "external_subagent_start requires non-empty data.agent_type",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(description, str):
        raise OmnigentError(
            "external_subagent_start requires data.description (string)",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise OmnigentError(
            "external_subagent_start requires non-empty data.tool_use_id",
            code=ErrorCode.INVALID_INPUT,
        )
    if parent_conv.agent_id is None:
        # claude-native parents are always created with an agent_id
        # by ``omnigent claude`` (the synthetic Claude bundle).
        # A null agent_id here means we're being called against a
        # legacy / corrupt row — fail loud rather than silently
        # mint a child without a parent agent.
        raise OmnigentError(
            f"parent session {parent_id!r} has no agent_id; cannot "
            "create a claude-native sub-agent child",
            code=ErrorCode.INVALID_INPUT,
        )

    # Idempotency: a forwarder retry with the same subagent_id must
    # resolve to the same child row, not mint a duplicate. The
    # forwarder also persists its own cursor file so this should be
    # rare, but the network is unreliable and the cursor write
    # happens after the POST.
    existing = await asyncio.to_thread(
        _find_claude_native_subagent_child,
        conversation_store,
        parent_id,
        subagent_id,
    )
    if existing is not None:
        return existing.id

    # Title format mirrors omnigent-spawned children
    # (``"{tool}:{session_name}"``) so the rail's split-on-colon
    # parser surfaces the same ``tool`` shape. The ``session_name``
    # half must be unique per parent because the conversation store
    # has a ``(parent_conversation_id, title)`` unique index — using
    # the description here would collide whenever Claude's LLM
    # passes the same agentType + description for parallel
    # sub-agents (which the Task tool does routinely). The
    # ``subagent_id`` is the only stable per-sub-agent identifier
    # in the meta file, so it goes here. The human-readable
    # description is stored as a label below for downstream surfaces
    # that want it; the rail's ``SubagentsPanel`` already hides the
    # ``session_name`` half so the user only sees ``agent_type``.
    title = f"{agent_type}:{subagent_id}"
    labels = {
        _CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
        _CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY: subagent_id,
        _CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY: tool_use_id,
        _CLAUDE_NATIVE_DESCRIPTION_LABEL_KEY: description,
    }

    try:
        child = await asyncio.to_thread(
            conversation_store.create_conversation,
            kind="sub_agent",
            title=title,
            parent_conversation_id=parent_id,
            agent_id=parent_conv.agent_id,
            runner_id=parent_conv.runner_id,
            sub_agent_name=agent_type,
        )
    except NameAlreadyExistsError:
        # The (parent, title) unique index fired: the row already exists
        # but the label-based idempotency lookup above missed it — either
        # a concurrent POST won the insert race, or an earlier POST died
        # after create_conversation and before set_labels, leaving an
        # unlabeled row. Without this recovery every forwarder redelivery
        # 500s on the same collision until the forwarder gives up and
        # parks the sub-agent (it then never appears in the rail). Adopt
        # the existing row and re-stamp its labels (idempotent upsert) so
        # the next delivery takes the fast label-lookup path.
        adopted = await asyncio.to_thread(
            _find_subagent_child_by_title,
            conversation_store,
            parent_id,
            title,
        )
        if adopted is None:
            raise
        await asyncio.to_thread(conversation_store.set_labels, adopted.id, labels)
        # The POST that created this orphan died before reaching the
        # ``session.created`` publish below, so live clients (the web
        # Subagents rail) have never heard about the child — emit it now.
        # In the concurrent-race case the winner also published; a
        # duplicate event is a harmless extra cache invalidation.
        _publish_session_created(parent_id, adopted.id, parent_conv.agent_id)
        return adopted.id
    await asyncio.to_thread(conversation_store.set_labels, child.id, labels)
    _publish_session_created(parent_id, child.id, parent_conv.agent_id)
    return child.id


def _find_codex_native_subagent_child(
    conversation_store: ConversationStore,
    parent_id: str,
    thread_id: str,
) -> Conversation | None:
    """
    Look up an existing Codex-native sub-agent child by its Codex thread id.

    Makes ``_persist_external_codex_subagent_start`` idempotent: when the
    forwarder re-posts because it observed both ``item/started`` and
    ``item/completed`` for the same collab item, the second POST returns
    the existing child row rather than creating a duplicate.

    :param conversation_store: Store to query.
    :param parent_id: Parent codex-native conversation id, e.g.
        ``"conv_parent987"``.
    :param thread_id: Codex child thread id, e.g.
        ``"019e8720-98d7-7b23-ac0a-bfb0eb02e0c9"``.
    :returns: Matching child :class:`Conversation`, or ``None`` when no
        row exists for this thread id.
    """
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            kind="sub_agent",
            parent_conversation_id=parent_id,
            limit=100,
            after=after,
        )
        for child in page.data:
            if child.labels.get(_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY) == thread_id:
                return child
        if not page.has_more or page.last_id is None:
            return None
        after = page.last_id


def _codex_subagent_display_tool(labels: dict[str, str]) -> str:
    """
    Return the UI-facing label for a Codex child session.

    Uses the Codex-assigned nickname when available, then the agent
    role, then ``"Codex"`` as a generic fallback.

    :param labels: Conversation labels from a Codex child row.
    :returns: Display label, e.g. ``"auth-auditor"``.
    """
    nickname = labels.get(_CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY)
    if nickname:
        return nickname
    role = labels.get(_CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY)
    if role:
        return role
    return _CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK


def _is_codex_native_subagent(conv: Conversation) -> bool:
    """
    Return whether a child conversation tracks a Codex internal sub-agent.

    :param conv: Conversation row to inspect.
    :returns: ``True`` when the row carries the codex-native sub-agent
        wrapper label.
    """
    return (
        conv.kind == "sub_agent"
        and conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
        == _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE
    )


def _subagent_delivery_status(
    status: str,
    background_task_count: int | None,
    conv: Conversation,
) -> str:
    """Collapse a sub-agent's background-task ``waiting`` back to ``idle``.

    A claude-native session running as an Omnigent sub-agent relabels its
    ``Stop`` turn-end ``idle`` to ``waiting`` (in the forwarder) when
    background shells linger, purely so its own UI shows a spinner. But the
    sub-agent terminal-delivery branch in ``post_event`` keys off
    ``idle``/``failed``: a ``waiting`` edge would never deliver the child's
    result to the parent, hanging the orchestrator with no follow-up ``Stop``
    to recover. The ``background_task_count`` alone already drives the child's
    spinner at ``idle`` (the in-chat indicator and the sidebar rollup both
    treat a positive tally as working), so for a sub-agent the turn genuinely
    ended — deliver ``idle``. Top-level sessions are returned unchanged so the
    web UI keeps its ``waiting`` shimmer.

    :param status: The incoming external status, e.g. ``"waiting"``.
    :param background_task_count: Parsed background-shell tally, or ``None``.
    :param conv: The conversation the status is for.
    :returns: ``"idle"`` for a non-codex sub-agent's background-task
        ``waiting``; otherwise ``status`` unchanged.
    """
    if (
        status == "waiting"
        and background_task_count is not None
        and background_task_count > 0
        and conv.kind == "sub_agent"
        and not _is_codex_native_subagent(conv)
    ):
        return "idle"
    return status


def _codex_subagent_labels_from_body(
    thread_id: str,
    body: SessionEventInput,
) -> dict[str, str]:
    """
    Build the label dict for a Codex-native sub-agent child row.

    :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param body: Validated ``external_codex_subagent_start`` event body.
    :returns: Labels to upsert on the child conversation row.
    """
    labels: dict[str, str] = {
        _CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
        _CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY: thread_id,
    }
    for data_key, label_key in (
        ("parent_thread_id", _CODEX_NATIVE_SUBAGENT_PARENT_THREAD_ID_LABEL_KEY),
        ("tool_call_id", _CODEX_NATIVE_SUBAGENT_TOOL_CALL_ID_LABEL_KEY),
        ("prompt", _CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY),
        ("agent_nickname", _CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY),
        ("agent_role", _CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY),
    ):
        value = body.data.get(data_key)
        if isinstance(value, str) and value:
            labels[label_key] = value
    return labels


async def _create_and_publish_codex_child(
    parent_id: str,
    parent_conv: Conversation,
    thread_id: str,
    labels: dict[str, str],
    conversation_store: ConversationStore,
) -> str:
    """
    Create a new Codex child Conversation row and publish ``session.created``.

    :param parent_id: Parent codex-native conversation id, e.g.
        ``"conv_parent987"``.
    :param parent_conv: Parent row whose ``agent_id`` and ``runner_id``
        are inherited by the child.
    :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param labels: Labels to stamp on the new child row.
    :param conversation_store: Store used to create the child row.
    :returns: New child conversation id, e.g. ``"conv_child456"``.
    """
    # Stable title so the (parent, title) unique index prevents race-condition
    # duplicate rows when the forwarder retries a failed registration.
    title = f"codex-native-ui-subagent:{thread_id}"
    try:
        child = await asyncio.to_thread(
            conversation_store.create_conversation,
            kind="sub_agent",
            title=title,
            parent_conversation_id=parent_id,
            agent_id=parent_conv.agent_id,
            runner_id=parent_conv.runner_id,
            sub_agent_name=_CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK,
        )
    except NameAlreadyExistsError:
        # A concurrent POST (or a retry that arrived before set_labels ran)
        # already created the row — find it and upsert labels instead.
        existing = await asyncio.to_thread(
            _find_codex_native_subagent_child, conversation_store, parent_id, thread_id
        )
        if existing is None:
            # The thread-id label never landed (the original POST died
            # between create_conversation and set_labels), so the label
            # lookup can't see the row. The title embeds the same thread
            # id and must exist for the unique index to have fired — fall
            # back to it so redelivery heals the unlabeled row instead of
            # permanently 500ing.
            existing = await asyncio.to_thread(
                _find_subagent_child_by_title,
                conversation_store,
                parent_id,
                title,
            )
        if existing is not None:
            await asyncio.to_thread(conversation_store.set_labels, existing.id, labels)
            # An orphaned row's creator died before publishing
            # ``session.created``, so live clients have never heard about
            # this child — emit it now. In the concurrent-race case the
            # winner also published; the duplicate is a harmless extra
            # cache invalidation.
            _publish_session_created(parent_id, existing.id, parent_conv.agent_id)
            return existing.id
        raise
    await asyncio.to_thread(conversation_store.set_labels, child.id, labels)
    _publish_session_created(parent_id, child.id, parent_conv.agent_id)
    return child.id


def _is_kiro_native_session(conv: Conversation) -> bool:
    """Return whether a conversation is backed by the native Kiro terminal."""
    return conv.labels.get("omnigent.wrapper") == "kiro-native-ui"


def _merge_pending_file_blocks(
    item: NewConversationItem,
    pending_content: list[dict[str, Any]],
) -> NewConversationItem:
    """
    Prepend a pending entry's file blocks onto a user-message item.

    The claude-native transcript mirrors a user message back as
    text-only — ``input_image`` / ``input_file`` blocks are dropped. The
    optimistic pending-input entry still carries them (with real
    ``file_id``s, assigned at upload), so we fold them into the durable
    item here. Without it the image renders only on the optimistic
    bubble and vanishes from history on the next reload.

    No-op when the pending entry has no file blocks, or when the item
    already carries file blocks (defensive — a future transcript that
    does include them must not be doubled).

    :param item: The parsed user-message item about to be persisted.
        Its ``data`` is a :class:`MessageData` whose ``content`` is a
        list of block dicts, e.g. ``[{"type": "input_text",
        "text": "hi"}]``.
    :param pending_content: The drained pending entry's content blocks,
        e.g. ``[{"type": "input_image", "file_id": "file_x",
        "filename": "a.png"}, {"type": "input_text", "text": "hi"}]``.
    :returns: A copy of *item* with the file blocks prepended, or *item*
        unchanged when there is nothing to merge.
    """
    if not isinstance(item.data, MessageData):
        return item
    file_blocks = [
        block
        for block in pending_content
        if isinstance(block, dict) and block.get("type") in ("input_image", "input_file")
    ]
    if not file_blocks:
        return item
    already_has_files = any(
        isinstance(block, dict) and block.get("type") in ("input_image", "input_file")
        for block in item.data.content
    )
    if already_has_files:
        return item
    merged_data = item.data.model_copy(update={"content": [*file_blocks, *item.data.content]})
    return item.model_copy(update={"data": merged_data})


def _message_text(content: list[dict[str, Any]]) -> str | None:
    """
    Extract joined text from message content blocks.

    :param content: Message content blocks, e.g.
        ``[{"type": "output_text", "text": "Done"}]``.
    :returns: Joined text from ``text`` / ``input_text`` fields,
        or ``None`` when no text field exists.
    """
    parts: list[str] = []
    found_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            text = block.get("input_text")
        if isinstance(text, str):
            found_text = True
            parts.append(text)
    return "\n".join(parts) if found_text else None


def _latest_assistant_text_from_store(
    conversation_store: ConversationStore,
    session_id: str,
) -> str | None:
    """
    Return the latest persisted assistant message text for a session.

    Native harnesses mirror completed transcript items to the AP
    server, not necessarily to the runner's in-memory history. This
    helper lets Omnigent forward the durable assistant output with the
    terminal-observed idle edge.

    :param conversation_store: Store used to read conversation items.
    :param session_id: Session/conversation id, e.g.
        ``"conv_child123"``.
    :returns: Latest assistant text, or ``None`` when none is
        persisted yet.
    """
    page = conversation_store.list_items(
        session_id,
        limit=_EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT,
        order="desc",
        type="message",
    )
    for item in page.data:
        if not isinstance(item.data, MessageData):
            continue
        if item.data.role != "assistant" or item.data.is_meta:
            continue
        text = _message_text(item.data.content)
        if text is not None:
            return text
    return None


@dataclass(frozen=True)
class _RunnerForwardResult:
    """
    HTTP result from forwarding a session-control event to the runner.

    :param status_code: Runner response status, e.g. ``204``.
    :param body: Runner response body text. Empty string when the runner
        returns no body.
    """

    status_code: int
    body: str


def _require_external_status_forward(
    session_id: str,
    status: str,
    runner_result: _RunnerForwardResult | None,
) -> None:
    """
    Fail loudly when required external status forwarding does not land.

    Terminal native sub-agent completion is delivered to the parent
    runner through this forward. Dropping it would leave the parent
    waiting forever with no inbox result.

    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param status: External status value, e.g. ``"idle"``.
    :param runner_result: HTTP result returned by the runner, or ``None``
        when no runner could be reached.
    :returns: None.
    :raises OmnigentError: If the runner was unavailable or
        rejected the forwarded status.
    """
    if runner_result is None:
        raise OmnigentError(
            f"Could not reach runner to deliver external_session_status "
            f"{status!r} for sub-agent session {session_id!r}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    if runner_result.status_code >= 400:
        detail = runner_result.body[:500]
        suffix = f": {detail}" if detail else ""
        raise OmnigentError(
            f"Runner rejected external_session_status {status!r} for "
            f"sub-agent session {session_id!r} with status "
            f"{runner_result.status_code}{suffix}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )


def _require_collaboration_mode_forward(
    session_id: str,
    enabled: bool,
    runner_result: _RunnerForwardResult | None,
) -> None:
    """
    Fail when a live Codex Plan-mode switch was not applied by the runner.

    Codex Plan mode is a loaded-thread collaboration mode inside Codex
    app-server. Persisting the Omnigent label without a successful runner
    update would make the web UI claim Plan mode while Codex still runs in
    the previous mode, so explicit UI toggles require a confirmed 2xx forward.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param enabled: ``True`` when entering Plan mode; ``False`` when
        returning to Default mode.
    :param runner_result: HTTP result returned by the runner, or ``None``
        when no runner could be reached.
    :returns: None.
    :raises OmnigentError: If no runner was reachable or the runner rejected
        the live Plan-mode update.
    """
    action = "enter Plan mode" if enabled else "exit Plan mode"
    if runner_result is None:
        raise OmnigentError(
            f"Could not {action}: no live Codex runner is available for "
            f"session {session_id!r}. Reconnect the session and try again.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    if not 200 <= runner_result.status_code < 300:
        raise OmnigentError(
            f"Could not {action}: runner returned status "
            f"{runner_result.status_code} for session {session_id!r}. "
            f"Reconnect the session and try again.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )


def _publish_status(
    session_id: str,
    status: str,
    error: ErrorDetail | None = None,
    response_id: str | None = None,
    background_task_count: int | None = None,
) -> None:
    """
    Publish a typed :class:`SessionStatusEvent` to the live stream and
    update the cache the list endpoint reads.

    ``status`` must be one of the literals on
    :class:`SessionStatusEvent` (``idle`` / ``running`` / ``waiting``
    / ``failed``); other values fail Pydantic validation rather than
    silently shipping a non-conforming wire shape (rule 15).

    Every publish site funnels through here so the in-memory
    ``_session_status_cache`` stays coherent with the SSE stream.
    Without this, paths that publish but don't write the cache —
    notably the ``external_session_status`` handler used by the
    claude-native forwarder — leave the sidebar stuck on "idle"
    while the chat itself shows "Working…".

    :param session_id: Session/conversation identifier.
    :param status: New session status value.
    :param error: Failure detail to forward on a ``"failed"``
        transition, e.g. ``ErrorDetail(code="runner_error",
        message="turn setup failed: ...")``. ``None`` for every
        non-failed transition. Carrying it lets clients render a
        terminal error line for SETUP-phase failures that never emit
        a ``response.failed`` event.
    :param response_id: Optional response id for terminal-backed status
        edges, e.g. ``"codex_turn_abc123"``.
    """
    # ``failed`` is sticky against a trailing ``idle``. A turn error is
    # terminal — it must not be silently downgraded to ``idle`` by a
    # follow-on quiescence signal. This matters for claude-native: the
    # turn-error edge comes from the ``StopFailure`` hook (→ ``failed``),
    # but the pane then goes quiet, so the PTY-activity watcher emits a
    # trailing ``idle`` ~1s later. Without this guard that ``idle`` would
    # erase the error state before the user could see it. The next
    # ``running`` edge (new activity) clears ``failed`` normally, so the
    # error persists exactly until the session does real work again. No
    # in-process flow performs a legitimate ``failed`` → ``idle``
    # transition (compaction failure publishes ``running`` → ``idle``, not
    # ``failed``), so this is a safe, harness-agnostic invariant.
    if status == "idle" and _session_status_cache.get(session_id) == "failed":
        # Session stays ``failed`` (terminal); the turn is over, so drop any
        # tracked in-flight response id rather than leaving it for the
        # snapshot to reopen a streaming bubble.
        _session_active_response_cache.pop(session_id, None)
        return
    _session_status_cache[session_id] = status
    # Mirror the transition onto the conversation row (best-effort,
    # deduplicated, off-loop) so replicas that don't hold this session's
    # runner tunnel serve the same sidebar status.
    session_live_state.persist_live_status(session_id, status)
    # Event-driven scheduled-run completion. A terminal edge (idle = the turn
    # completed; failed = it errored/disconnected) flips the conversation's
    # still-``running`` scheduled_task_run to succeeded/failed. This is the
    # primary FU-1 mechanism: the run transitions the instant the turn ends,
    # driven by the same terminal event that persists live_status — no poll.
    # The event's own ``error`` carries the failure classification, so no label
    # re-read is needed (and none of the race that would imply). A no-op for
    # the common case: interactive (non-scheduled) conversations have no
    # running run, and the reverse lookup cheaply returns None. running/waiting
    # edges are skipped entirely so the hot path pays nothing mid-turn.
    if status == "idle":
        session_live_state.persist_scheduled_run_completion(session_id, "succeeded")
    elif status == "failed":
        session_live_state.persist_scheduled_run_completion(
            session_id,
            "failed",
            error_code=error.code if error is not None else None,
            error=error.message if error is not None else None,
        )
    # Track the in-flight response id for snapshot-based reconnect (see
    # _session_active_response_cache). A running/waiting edge that names a
    # turn opens it; any idle/failed edge closes it.
    if status in ("running", "waiting"):
        if response_id is not None:
            _session_active_response_cache[session_id] = response_id
    else:
        _session_active_response_cache.pop(session_id, None)
    # Keep the background-shell tally sticky alongside the status (see the
    # cache's declaration). A ``Stop`` hook reports an authoritative count
    # (``None`` is never sent by it): a positive count sets the tally, and
    # an explicit ``0`` clears it so a finished background shell drops the
    # indicator on the next turn end. ``None`` means "no information" (the
    # trailing PTY-activity ``idle`` carries none) and must NOT wipe the
    # count the Stop hook just published. A new turn or a failure clears it.
    if background_task_count is not None:
        if background_task_count > 0:
            _session_background_task_count_cache[session_id] = background_task_count
        else:
            _session_background_task_count_cache.pop(session_id, None)
    elif status in ("running", "failed"):
        _session_background_task_count_cache.pop(session_id, None)
    event = SessionStatusEvent(
        type="session.status",
        conversation_id=session_id,
        status=status,  # type: ignore[arg-type]
        response_id=response_id,
        error=error,
        background_task_count=background_task_count,
    )
    payload = event.model_dump()
    if response_id is None:
        payload.pop("response_id", None)
    if background_task_count is None:
        payload.pop("background_task_count", None)
    session_stream.publish(session_id, payload)


def _truncate_label(value: str) -> str:
    """Truncate a label value to fit the ``conversation_labels.value`` column.

    Long failure messages (tracebacks, 5xx bodies) overflow the column and
    cause a ``DataError`` that silently drops the error reason. Error messages
    front-load their signal, so keeping the head and appending an ellipsis
    preserves the useful part while flagging that more was dropped. The store
    clamps again as a final guard, but truncating here keeps the marker and
    makes the call site directly testable.

    :param value: The raw string to truncate.
    :returns: ``value`` unchanged if it already fits, else the head trimmed to
        the column width with a trailing ``…`` to signal truncation.
    """
    if len(value) <= _LABEL_VALUE_MAX_LEN:
        return value
    return value[: _LABEL_VALUE_MAX_LEN - 1] + "…"


async def _persist_session_status_error_labels(
    session_id: str,
    error: ErrorDetail | None,
    conversation_store: ConversationStore,
) -> None:
    """
    Persist or clear the reload-visible failure detail for a session status.

    ``session.status`` is an SSE edge, so its ``error`` object disappears on
    reload. Terminal-native sessions can fail before any transcript item is
    written, so store the latest failure detail as runner-owned labels and let
    snapshots project it as ``last_task_error``. Empty string clears stale
    values because the label store is upsert-only.

    :param session_id: Session/conversation identifier.
    :param error: Failure detail from a ``session.status: failed`` edge, or
        ``None`` to clear stale error labels on subsequent activity.
    :param conversation_store: Store used to upsert labels.
    """
    updates = (
        {
            _LAST_TASK_ERROR_CODE_LABEL_KEY: _truncate_label(error.code),
            _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: _truncate_label(error.message),
        }
        if error is not None
        else {
            _LAST_TASK_ERROR_CODE_LABEL_KEY: "",
            _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "",
        }
    )
    try:
        await asyncio.to_thread(conversation_store.set_labels, session_id, updates)
    except Exception:
        _logger.exception(
            "Failed to persist session status error labels for %s",
            session_id,
        )


def _last_task_error_from_labels(labels: Mapping[str, str]) -> dict[str, str] | None:
    """
    Project runner-owned failure labels into the typed API error shape.

    Terminal/native runtimes can fail before they write any transcript item,
    so the session-status relay stores the latest failure as durable labels.
    This helper is the single server-side boundary where those internal labels
    become public ``last_task_error`` data for snapshots and child summaries.

    :param labels: Conversation labels, usually after closed-status projection.
    :returns: ``{"code": "...", "message": "..."}``, or ``None`` when either
        value is absent/cleared.
    """
    raw_error_code = labels.get(_LAST_TASK_ERROR_CODE_LABEL_KEY)
    raw_error_message = labels.get(_LAST_TASK_ERROR_MESSAGE_LABEL_KEY)
    if raw_error_code and raw_error_message:
        return {
            "code": raw_error_code,
            "message": raw_error_message,
        }
    return None


def _publish_terminal_pending(session_id: str, pending: bool) -> None:
    """
    Publish a typed :class:`SessionTerminalPendingEvent` and update the
    cache the snapshot reads.

    Every relay site that changes the terminal-spin-up flag funnels
    through here so the in-memory ``_session_terminal_pending_cache``
    stays coherent with the SSE stream — a client connecting
    mid-spin-up seeds the spinner from the snapshot's
    ``terminal_pending`` field, while already-connected clients update
    live off this event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param pending: ``True`` while the runner is auto-creating the
        terminal; ``False`` once it lands or auto-create fails.
    """
    # Store only ``True`` entries; delete on clear so the cache never
    # accumulates stale ``False`` entries for every terminal-first session
    # that has ever completed spin-up. The snapshot getter uses
    # ``.get(id, False)`` so absent == False.
    if pending:
        _session_terminal_pending_cache[session_id] = True
    else:
        _session_terminal_pending_cache.pop(session_id, None)
    event = SessionTerminalPendingEvent(
        type="session.terminal_pending",
        conversation_id=session_id,
        pending=pending,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_sandbox_status(*args: Any, **kwargs: Any) -> None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return _facade._publish_sandbox_status(*args, **kwargs)


def _publish_sandbox_status_impl(session_id: str, stage: str, error: str | None = None) -> None:
    """
    Publish a typed :class:`SessionSandboxStatusEvent` and update the
    cache the snapshot reads.

    Every stage transition of a managed-sandbox launch funnels through
    here so the in-memory ``_session_sandbox_status_cache`` stays
    coherent with the SSE stream — a client opening the session
    mid-launch seeds its progress indicator from the snapshot's
    ``sandbox_status`` field, while already-connected clients update
    live off this event. Thread-safe (``session_stream.publish`` is a
    thread-safe broadcast and the cache write is a single dict
    assignment), so the launch pipeline may call this from the worker
    thread its sandbox exec steps run on.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param stage: The launch stage just entered, e.g.
        ``"provisioning"`` — one of
        :data:`omnigent.server.schemas.SandboxLaunchStage`.
    :param error: Failure detail when *stage* is ``"failed"``, e.g.
        ``"managed sandbox launch failed: spend limit reached"``.
        ``None`` for non-terminal stages.
    """
    # "ready" evicts: from then on the session looks like any
    # host-bound session and the snapshot carries no launch state.
    # Failures stay cached (mirroring ManagedLaunchTracker retention)
    # so a reload after a dead launch still shows the reason.
    if stage == "ready":
        _session_sandbox_status_cache.pop(session_id, None)
    else:
        _session_sandbox_status_cache[session_id] = SandboxStatus(stage=stage, error=error)
    event = SessionSandboxStatusEvent(
        type="session.sandbox_status",
        conversation_id=session_id,
        stage=stage,
        error=error,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_mcp_startup(session_id: str, servers: dict[str, McpServerStartup]) -> None:
    """
    Publish a typed :class:`SessionMcpStartupEvent` to the live stream.

    Fired when a native forwarder reports harness MCP-server startup
    progress via ``external_mcp_startup``, so the web UI can show
    per-server startup state while the harness boots instead of an
    apparently hung session. Also updates the snapshot cache so a client
    opening the session mid-startup seeds the band from the snapshot's
    ``mcp_startup`` field; a map with nothing left to show — empty, or
    every server ``ready`` — evicts the cache entry, mirroring the web
    store's all-ready clear so a reloading client never seeds a band
    that renders nothing.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param servers: Latest per-server startup map, e.g.
        ``{"safe": McpServerStartup(status="starting", error=None)}``.
    """
    if any(record.status != "ready" for record in servers.values()):
        _session_mcp_startup_cache[session_id] = servers
    else:
        _session_mcp_startup_cache.pop(session_id, None)
    event = SessionMcpStartupEvent(
        type="session.mcp_startup",
        conversation_id=session_id,
        servers=servers,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_runner_skills(session_id: str) -> None:
    """
    Publish a typed :class:`SessionSkillsEvent` to the live stream.

    Fired the moment the background runner-skills fetch
    (:func:`_load_runner_skills`) populates the per-session cache, so a
    connected client can re-read the session snapshot and fill its
    slash-command menu instead of waiting for the next bind. Carries no
    payload beyond the conversation id — it is a "skills resolved,
    re-read the snapshot" nudge; the snapshot's cache-backed ``skills``
    field stays the source of truth.

    No-op when no client is subscribed (``session_stream`` has no
    buffer): a client binding later reads the now-warm snapshot directly.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    event = SessionSkillsEvent(
        type="session.skills",
        conversation_id=session_id,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_model_options(session_id: str) -> None:
    """
    Publish a typed :class:`SessionModelOptionsEvent` to the live stream.

    Fired when a background runner catalog fetch populates the per-session
    model-options cache. Connected clients re-read the session snapshot and
    apply its cache-backed ``model_options`` field.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    event = SessionModelOptionsEvent(
        type="session.model_options",
        conversation_id=session_id,
    )
    session_stream.publish(session_id, event.model_dump())


def _invalidate_runner_backed_snapshot_state(
    session_id: str,
    *,
    cancel_inflight: bool,
) -> None:
    """
    Drop runner-derived session snapshot overlays for one session.

    These fields are discovered from the bound runner (skills and the
    codex-native ``model/list`` catalog), so browser reloads can ask the
    next snapshot to refresh them from the live session instead of serving
    stale AP-process memory. Runner teardown additionally cancels any
    in-flight fetch so a dead runner cannot land a late stale value.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param cancel_inflight: Whether to cancel currently-running fetches.
        Use ``True`` when a runner disconnects; use ``False`` for browser
        refreshes so concurrent page-load callers do not cancel each other.
    """
    _runner_skills_cache.pop(session_id, None)
    if cancel_inflight:
        inflight = _runner_skills_inflight.pop(session_id, None)
        if inflight is not None:
            inflight.cancel()
    _model_options_cache.pop(session_id, None)
    if cancel_inflight:
        codex_inflight = _model_options_inflight.pop(session_id, None)
        if codex_inflight is not None:
            codex_inflight.cancel()


def _publish_changed_files_invalidated(session_id: str, environment_id: str = "default") -> None:
    """
    Publish a coarse filesystem-change invalidation to the live stream.

    The event tells web clients to refetch visible filesystem views
    for the environment instead of polling the tree while a session is
    active. It is intentionally coarse because git-mode workspaces can
    only answer "the working tree changed" cheaply, not per-directory
    deltas.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param environment_id: Environment resource id,
        e.g. ``"default"``.
    """
    session_stream.publish(
        session_id,
        {
            "type": "session.changed_files.invalidated",
            "session_id": session_id,
            "environment_id": environment_id,
        },
    )


def _publish_interrupted(session_id: str, response_id: str | None = None) -> None:
    """
    Publish a ``session.interrupted`` event to the live stream.

    The event is co-emitted with ``response.incomplete`` (reason
    ``"user_interrupt"``) by the runtime cancel handler so off-the-
    shelf Responses parsers still close cleanly. This helper is
    responsible only for the session-level signal — not the
    response-level one.

    :param session_id: The session/conversation identifier whose
        stream should receive the event, e.g. ``"conv_abc123"``.
    :param response_id: Optional response id for terminal-backed
        interrupted turns, e.g. ``"codex_turn_abc123"``.
    """
    event = SessionInterruptedEvent(
        type="session.interrupted",
        data=SessionInterruptedPayload(
            requested_at=int(time.time()),
            response_id=response_id,
        ),
    )
    payload = event.model_dump()
    if response_id is None:
        data = payload.get("data")
        if isinstance(data, dict):
            data.pop("response_id", None)
    session_stream.publish(session_id, payload)


def _publish_session_superseded(session_id: str, target_conversation_id: str) -> None:
    """
    Publish a ``session.superseded`` event to the live stream.

    Emitted when a Claude ``/clear`` rotates a session away (see
    ``_post_clear_supersession`` in
    ``omnigent/claude_native_forwarder.py``): a client actively viewing
    ``session_id`` follows to ``target_conversation_id``. Live-only —
    there is no SSE replay, so a client connecting after the rotation
    relies on the persisted notice message instead.

    :param session_id: The superseded (old) conversation id whose stream
        should receive the event, e.g. ``"conv_old"``.
    :param target_conversation_id: The conversation to redirect to, e.g.
        ``"conv_new"``.
    """
    event = SessionSupersededEvent(
        type="session.superseded",
        conversation_id=session_id,
        target_conversation_id=target_conversation_id,
        reason="clear",
    )
    session_stream.publish(session_id, event.model_dump())
    # Discard any unconsumed pending inputs on the superseded session — notably
    # the ``/clear`` the user typed in the web UI. ``/clear`` is never mirrored
    # back as a committed item (the session rotated away), so its pending entry
    # would otherwise linger forever as a stuck optimistic bubble, re-hydrating
    # from the snapshot on every reload of the old chat. Live viewers already
    # drop the bubble on the ``session.superseded`` event above; this stops it
    # coming back. We deliberately do NOT emit ``session.input.consumed`` (that
    # would commit ``/clear`` as a user message) — the persisted clear notice
    # already explains the rotation, so the input is simply abandoned.
    discarded = 0
    while pending_inputs.resolve_oldest(session_id) is not None:
        discarded += 1
    if discarded:
        _logger.info(
            "Discarded %d unconsumed pending input(s) on superseded session %s",
            discarded,
            session_id,
        )


async def _get_runner_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient | None:
    """Call-time proxy so a ``sessions._get_runner_client`` patch is honored here.

    The real body lives on the ``sessions`` facade (as the ``_impl`` alias); tests
    patch the facade attribute, so sibling callers must resolve it there at call
    time rather than binding the pre-split local copy.
    """
    from omnigent.server.routes import sessions as _facade

    return await _facade._get_runner_client(*args, **kwargs)


async def _get_runner_client_impl(
    session_id: str,
    runner_router: RunnerRouter | None,
) -> httpx.AsyncClient | None:
    """
    Get an HTTP client for the runner bound to a session.

    Uses the ``RunnerRouter`` to resolve the pinned runner. Falls
    back to the in-process runner client for test setups.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_router: The ``RunnerRouter`` instance, or
        ``None`` for in-process setups.
    :returns: An ``httpx.AsyncClient`` pointed at the runner,
        or ``None`` if no runner is available.
    """
    from omnigent.runtime import get_runner_client

    if runner_router is not None:
        try:
            routed = runner_router.client_for_session_resources(
                session_id,
            )
            return routed.client
        except (LookupError, httpx.HTTPError, OmnigentError):
            _logger.debug(
                "No runner bound for session=%s",
                session_id,
            )
            return None
    return cast("httpx.AsyncClient | None", get_runner_client())


async def _query_host_runner_status(
    host_conn: HostConnection,
    host_registry: HostRegistry,
    runner_id: str,
) -> str | None:
    """
    Ask a host whether a runner's process is alive, dead, or unknown.

    The host owns runner-process liveness (it holds the ``Popen``), so it
    can answer the one question the server's tunnel registry cannot: is an
    absent-from-the-tunnel runner still coming (booting) or gone for good
    (stopped, crashed, or lost to a host restart)? Used before the connect
    grace so the dispatch path waits only for a runner that is coming.

    :param host_conn: Live host connection to query.
    :param host_registry: Registry used to enqueue the outbound frame.
    :param runner_id: Runner to ask about, e.g. ``"runner_abc123..."``.
    :returns: ``"alive"``, ``"dead"``, or ``"unknown"`` from the host; or
        ``None`` when the host didn't reply in time, the connection
        dropped, or the host is too old to support the query. ``None``
        means "no authoritative answer" — the caller falls back to the
        plain connect grace, preserving the prior blind-wait behavior.
    """
    from omnigent.host.frames import HostRunnerStatusFrame, encode_host_frame
    from omnigent.server.routes import sessions as _facade

    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, str | None]] = asyncio.get_running_loop().create_future()
    host_conn.pending_runner_status[request_id] = future
    frame = encode_host_frame(HostRunnerStatusFrame(request_id=request_id, runner_id=runner_id))
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError:
            return None
        result = await asyncio.wait_for(
            future,
            timeout=_facade._HOST_RUNNER_STATUS_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:  # noqa: BLE001
        # Defensive: this query only ever *speeds up* the connect grace, so
        # any unexpected failure (e.g. the future resolved with an error)
        # must degrade to "no verdict" and fall back to the wait rather than
        # break the message POST. CancelledError is a BaseException and still
        # propagates, so the race helper's cancel/drain is unaffected.
        _logger.warning(
            "host.runner_status query for runner %s failed; falling back to grace",
            runner_id,
            exc_info=True,
        )
        return None
    finally:
        host_conn.pending_runner_status.pop(request_id, None)
    return result.get("status")


async def _wait_for_runner_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._wait_for_runner_client(*args, **kwargs)


async def _wait_for_runner_client_impl(
    session_id: str,
    runner_router: RunnerRouter | None,
    tunnel_registry: TunnelRegistry | None,
    *,
    runner_id: str | None,
    timeout_s: float,
    runner_exit_reports: RunnerExitReports | None = None,
) -> httpx.AsyncClient | None:
    """
    Wait until a runner connects, then resolve the session's runner client.

    The tunnel registry owns the event-driven "runner connected" signal.
    After that signal fires, this helper intentionally resolves through
    :func:`_get_runner_client` instead of constructing a client directly
    from the registry session: the router re-checks the conversation's
    current ``runner_id`` binding and preserves the existing ownership /
    capability checks.

    When ``runner_exit_reports`` is supplied, the wait also ends the
    moment the daemon reports this runner died (``host.runner_exited``).
    That report is the authoritative "this runner is busted" signal — a
    crashed runner can never connect, so waiting out ``timeout_s`` would
    only delay the caller's failure handling. Returning ``None`` on the
    report (same as a timeout) lets the caller persist the failure the
    instant we are convinced, neither speculatively early nor a full
    timeout late.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_router: The ``RunnerRouter`` instance, or ``None`` for
        in-process test setups.
    :param tunnel_registry: The server's ``TunnelRegistry`` instance, or
        ``None`` in test setups without runner tunnels.
    :param runner_id: Runner id expected to connect, e.g.
        ``"runner_0123456789abcdef"``.
    :param timeout_s: Maximum seconds to wait, e.g. ``3.0``.
    :param runner_exit_reports: Crash-report store consulted to abort the
        wait early when this runner is reported dead. ``None`` keeps the
        plain wait-to-timeout behavior.
    :returns: A runner HTTP client if one becomes available, otherwise
        ``None`` (timed out, or the runner was reported dead).
    """
    if runner_id is None:
        return None
    if tunnel_registry is None:
        return await _get_runner_client(session_id, runner_router)
    if runner_exit_reports is None:
        session = await tunnel_registry.wait_for_runner(runner_id, timeout_s=timeout_s)
        return None if session is None else await _get_runner_client(session_id, runner_router)
    # Race the event-driven connect signal against the crash-report poll;
    # whichever resolves first wins. A report means the runner is busted —
    # stop waiting and let the caller fail the turn now.
    connect_task = asyncio.ensure_future(
        tunnel_registry.wait_for_runner(runner_id, timeout_s=timeout_s)
    )
    try:
        while not connect_task.done():
            if runner_exit_reports.get(runner_id) is not None:
                return None
            await asyncio.wait({connect_task}, timeout=_RUNNER_CONVICTION_POLL_S)
    finally:
        if not connect_task.done():
            connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await connect_task
    session = connect_task.result()
    return None if session is None else await _get_runner_client(session_id, runner_router)


async def _validate_session_workspace(
    *,
    user_id: str | None,
    host_id: str,
    workspace: str | None,
    agent: Any,
    agent_cache: AgentCache | None,
    request: Request,
) -> str:
    """
    Validate a session's workspace against the agent's os_env boundary.

    Wraps the seven-step validation in
    :mod:`omnigent.server.routes._workspace_validation` and
    raises :class:`OmnigentError` on failure so the route layer
    converts the error into a 400 response with a clear message.
    See ``designs/SESSION_WORKSPACE_SELECTION.md`` for the full
    semantic spec.

    The caller's host ownership is checked BEFORE the ``host.stat``
    round-trip the validation performs, so a non-owner never reaches
    another user's host (raises 403/404 via ``resolve_host_owner``).

    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``, or ``None`` when auth is disabled.
    :param host_id: Stable host id, e.g. ``"host_a1b2c3d4..."``.
    :param workspace: Absolute path supplied by the caller, e.g.
        ``"/Users/corey/universe/src/foo"``. ``None`` is rejected
        with the "workspace required when host_id is set" message.
    :param agent: The agent the session binds to. Used to load the
        bundle and read ``os_env.cwd`` for boundary computation.
    :param agent_cache: Cache for loading parsed agent specs from
        bundle storage. Required because session-create needs the
        spec; ``None`` is treated as a server config error.
    :param request: FastAPI request; ``request.app.state``
        carries the host registry and host store.
    :returns: The canonicalized workspace path that should be
        stored on the session row, e.g.
        ``"/Users/corey/universe/src/foo"`` (realpath; symlinks
        already resolved by the host).
    :raises OmnigentError: With ``ErrorCode.INVALID_INPUT`` on
        any validation failure (offline host, missing path,
        outside boundary, missing subdir). With
        ``ErrorCode.INTERNAL_ERROR`` if ``agent_cache`` is unset.
    """
    return await validate_existing_host_workspace(
        user_id=user_id,
        host_id=host_id,
        workspace=workspace,
        agent=agent,
        agent_cache=agent_cache,
        host_store=getattr(request.app.state, "host_store", None),
        host_registry=getattr(request.app.state, "host_registry", None),
    )


@dataclass
class _HostLaunchAttempt:
    """
    Outcome of a relaunch ``host.launch_runner`` round-trip.

    :param runner_id: The token-bound runner id minted for this attempt,
        e.g. ``"runner_token_abc123..."``. Always set (the binding is
        rotated before the frame is sent), even when the host refused.
    :param error_code: Structured failure category from the host's result
        frame, e.g. ``"harness_not_configured"``; ``None`` on a successful
        launch, on a timeout waiting for the result, or when the host sent
        no code.
    :param error: Human-readable failure message from the host, e.g.
        ``"harness 'codex' is not configured on host 'laptop' — run
        `omnigent setup` ..."``; ``None`` when there was no error.
    """

    runner_id: str
    error_code: str | None = None
    error: str | None = None


async def _launch_runner_on_host(*args: Any, **kwargs: Any) -> _HostLaunchAttempt:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._launch_runner_on_host(*args, **kwargs)


async def _launch_runner_on_host_impl(
    conv: Conversation,
    conversation_store: ConversationStore,
    host_registry: HostRegistry,
    host_conn: HostConnection,
) -> _HostLaunchAttempt:
    """
    Ask a host to spawn a runner for a session and capture the result.

    Generates a new binding token, writes the runner_id to the session
    row, sends ``host.launch_runner`` (carrying the session's canonical
    harness so the host can refuse an unconfigured one), and waits up to
    :data:`_HOST_LAUNCH_RESULT_TIMEOUT_S` for the host's result frame.
    Does NOT wait for the runner to *connect* — the caller polls for that
    separately; this only captures the spawn/refuse verdict so a
    structured refusal (harness not configured) can be surfaced instead
    of silently timing out as ``RUNNER_UNAVAILABLE``.

    :param conv: The conversation that needs a runner.
    :param conversation_store: Store for updating ``runner_id``.
    :param host_registry: In-memory ``HostRegistry``.
    :param host_conn: The live ``HostConnection`` for the host.
    :returns: The :class:`_HostLaunchAttempt` — the new runner id plus any
        structured refusal from the host.
    """
    from omnigent.host.frames import HostLaunchRunnerFrame, encode_host_frame
    from omnigent.runner.identity import token_bound_runner_id

    binding_token = secrets.token_urlsafe(32)
    new_runner_id = token_bound_runner_id(binding_token)

    await asyncio.to_thread(
        conversation_store.replace_runner_id,
        conv.id,
        new_runner_id,
    )

    # Pull workspace from the session row — populated and validated
    # at session create per designs/SESSION_WORKSPACE_SELECTION.md.
    # The check constraint guarantees workspace is non-NULL when
    # host_id is set, so this assertion is a tripwire for any path
    # that bypassed the validation.
    if conv.workspace is None:  # pragma: no cover — constraint guards
        _logger.error(
            "session %s has host_id=%s but workspace is NULL — schema "
            "constraint should have prevented this",
            conv.id,
            conv.host_id,
        )
        return _HostLaunchAttempt(runner_id=new_runner_id)
    request_id = secrets.token_hex(8)
    launch_future: asyncio.Future[dict[str, str | None]] = (
        asyncio.get_running_loop().create_future()
    )
    host_conn.pending_launches[request_id] = launch_future
    launch_frame = encode_host_frame(
        HostLaunchRunnerFrame(
            request_id=request_id,
            binding_token=binding_token,
            workspace=conv.workspace,
            session_id=conv.id,
            # Canonical harness (see _resolve_harness) so the host runs the
            # same configuration check it does at create-time launch. None
            # (agent not resolvable) skips the host-side check — fail open.
            harness=_resolve_harness(conv),
        )
    )
    try:
        host_registry.send_text(host_conn, launch_frame)
    except ConnectionError:
        host_conn.pending_launches.pop(request_id, None)
        _logger.warning(
            "Host %s connection lost while launching runner for %s",
            conv.host_id,
            conv.id,
        )
        return _HostLaunchAttempt(runner_id=new_runner_id)
    try:
        result = await asyncio.wait_for(
            launch_future,
            timeout=_HOST_LAUNCH_RESULT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        # No result yet — fall through to the caller's connect wait, which
        # preserves the prior fire-and-forget timing for a slow-but-fine host.
        host_conn.pending_launches.pop(request_id, None)
        return _HostLaunchAttempt(runner_id=new_runner_id)
    if result.get("status") == "failed":
        return _HostLaunchAttempt(
            runner_id=new_runner_id,
            error_code=result.get("error_code"),
            error=result.get("error"),
        )
    return _HostLaunchAttempt(runner_id=new_runner_id)


async def cancel_managed_launch_tasks() -> None:
    """
    Cancel and await every in-flight background managed launch.

    Lifespan-teardown hook: without it, a slow provision outlives the
    ASGI shutdown and dies wherever the loop teardown happens to kill
    it. Cancellation is deterministic teardown of the TASK only — an
    already-provisioned sandbox is not terminated here (there is no
    time budget for provider calls during shutdown); its armed launch
    token expires with the provider lifetime cap that also reaps the
    sandbox.

    :returns: None once every task has settled (cancellations and any
        in-flight failures are absorbed via ``return_exceptions``).
    """
    tasks = list(_managed_launch_tasks)
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _provision_managed_sandbox(
    *,
    session_id: str,
    owner: str,
    sandbox_config: ManagedSandboxConfig,
    repo: RepoWorkspace | None,
    tracker: ManagedLaunchTracker,
    host_store: HostStore,
    relaunch_host: Host | None,
) -> ManagedHostLaunch | None:
    """
    Run the provision phase of a background managed launch.

    Dispatches to :func:`relaunch_managed_host` (existing host row)
    or :func:`launch_managed_host` (fresh identity) and converts any
    failure into a settled tracker entry — the background task has no
    caller to raise to.

    :param session_id: Session/conversation identifier.
    :param owner: User the managed host acts for.
    :param sandbox_config: The deployment's sandbox config.
    :param repo: Repository workspace to clone, or ``None``.
    :param tracker: The app's launch tracker (failed here on error).
    :param host_store: Persistent host registrations.
    :param relaunch_host: Existing host row for a relaunch, or
        ``None`` for a first launch.
    :returns: The launch result, or ``None`` when the launch failed
        (the tracker entry is already settled with the reason).
    """
    from omnigent.server.managed_hosts import launch_managed_host, relaunch_managed_host

    def _on_stage(stage: str) -> None:
        """
        Relay a launch-pipeline stage to the session's progress surface.

        Passed into the launch helpers, which may invoke it from the
        worker thread their sandbox exec steps run on —
        :func:`_publish_sandbox_status` is thread-safe.

        :param stage: The stage just entered, e.g. ``"cloning"``.
        """
        _publish_sandbox_status(session_id, stage)

    try:
        if relaunch_host is not None:
            return await relaunch_managed_host(
                config=sandbox_config,
                host=relaunch_host,
                host_store=host_store,
                repo=repo,
                on_stage=_on_stage,
            )
        return await launch_managed_host(
            config=sandbox_config,
            owner=owner,
            host_store=host_store,
            repo=repo,
            on_stage=_on_stage,
        )
    except HTTPException as exc:
        _logger.warning(
            "Managed sandbox launch failed for session %s: %s",
            session_id,
            exc.detail,
        )
        tracker.fail(session_id, str(exc.detail))
        _publish_sandbox_status(session_id, "failed", str(exc.detail))
        return None
    except Exception:
        # Broad on purpose: this is a fire-and-forget task — an
        # unexpected error must settle the tracker (or a waiting
        # message POST hangs until its timeout) and must not escape
        # as an unhandled-task traceback.
        _logger.exception(
            "Managed sandbox launch crashed for session %s",
            session_id,
        )
        tracker.fail(session_id, "internal error during managed sandbox launch")
        _publish_sandbox_status(
            session_id, "failed", "internal error during managed sandbox launch"
        )
        return None


async def _wait_for_managed_runner_tunnel(
    session_id: str,
    runner_id: str,
    tunnel_registry: TunnelRegistry,
    tracker: ManagedLaunchTracker,
) -> bool:
    """
    Wait for a launched managed runner to connect, failing the launch on timeout.

    :param session_id: Session/conversation identifier.
    :param runner_id: Runner id returned by the host launch frame.
    :param tunnel_registry: Runner tunnel registry to wait on.
    :param tracker: Managed launch tracker to settle on failure.
    :returns: ``True`` when the runner connected; ``False`` after publishing
        and retaining a failed launch status.
    """
    from omnigent.server.routes import sessions as _facade

    runner = await tunnel_registry.wait_for_runner(
        runner_id,
        timeout_s=_facade._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
    )
    if runner is not None:
        return True
    reason = "managed runner did not connect after launch"
    tracker.fail(session_id, reason)
    _publish_sandbox_status(session_id, "failed", reason)
    return False


async def _await_settled_managed_launch(launch: ManagedLaunch) -> None:
    """
    Block until a managed launch settles, raising its failure.

    The rendezvous a message POST takes when it races a background
    managed launch (create-time provisioning or a dead-sandbox
    relaunch): resolve as soon as the launch settles, surface the
    recorded reason when it failed, and give up with a clear retry
    hint when the launch outlives the rendezvous budget.

    :param launch: The session's tracker entry.
    :raises OmnigentError: 503 when the launch failed or is still
        running at the timeout.
    """
    from omnigent.server.managed_hosts import MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S

    try:
        await asyncio.wait_for(
            launch.settled.wait(),
            timeout=MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise OmnigentError(
            "The session's managed sandbox is still provisioning; try again shortly",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from None
    if launch.error is not None:
        raise OmnigentError(
            f"The session's managed sandbox failed to launch: {launch.error}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )


async def _get_runner_client_for_resource_access(
    *args: Any, **kwargs: Any
) -> httpx.AsyncClient | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._get_runner_client_for_resource_access(*args, **kwargs)


async def _get_runner_client_for_resource_access_impl(
    session_id: str,
) -> httpx.AsyncClient | None:
    """Return the authoritative runner client for session resources.

    Requires the session to be bound to a runner via
    ``PATCH /v1/sessions/{id}``; raises ``conflict`` otherwise. If no
    runner router is configured (unit-test/in-process setups), callers
    may fall back to local registries.
    """
    from omnigent.runtime import get_runner_client, get_runner_router

    runner_router = get_runner_router()
    if runner_router is not None:
        routed_runner = runner_router.client_for_session_resources(session_id)
        return routed_runner.client
    return cast("httpx.AsyncClient | None", get_runner_client())


async def _proxy_get_session_resources_to_runner(
    runner_client: httpx.AsyncClient,
    session_id: str,
    resource_type: str | None = None,
) -> SessionResourcePaginatedList:
    """Proxy ``GET /resources`` to the runner with strict validation.

    :param runner_client: HTTP client bound to the session's runner.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param resource_type: Optional ``?type=`` filter forwarded to the
        runner, e.g. ``"environment"``. ``None`` returns all types.
    :returns: The runner's validated resource page.
    :raises HTTPException: 502 on runner failure or malformed response.
    """
    try:
        resp = await runner_client.get(
            f"/v1/sessions/{session_id}/resources",
            # Runner-side list_session_resources applies the type filter.
            params={"type": resource_type} if resource_type else None,
            timeout=10.0,
        )
        if resp.status_code != 200:
            _logger.warning(
                "session resources: runner returned %d for session=%s",
                resp.status_code,
                session_id,
            )
            raise HTTPException(
                status_code=502,
                detail="runner session-resources endpoint failed",
            )

        try:
            body = resp.json()
            if not isinstance(body, dict):
                raise TypeError("response body must be an object")
            page = SessionResourceListPage.model_validate(body)
        except (TypeError, ValueError, ValidationError) as exc:
            _logger.warning(
                "session resources: malformed runner response for session=%s: %s",
                session_id,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail="runner session-resources endpoint returned malformed response",
            ) from exc

        return SessionResourcePaginatedList(
            data=page.data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )
    except HTTPException:
        raise
    except (httpx.HTTPError, ConnectionError) as exc:
        _logger.warning(
            "session resources: runner call failed for session=%s (%s)",
            session_id,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail="runner session-resources endpoint unavailable",
        ) from exc


async def _reset_runner_resources_after_switch(session_id: str) -> None:
    """Best-effort reset of the session's runner-side state after a switch.

    Run as a fire-and-forget background task by the switch-agent route. Calls
    the runner's dedicated ``POST /v1/sessions/{id}/reset-state`` endpoint,
    which closes the cached primary OSEnv + terminals AND drops the
    spec-derived session caches. Two reasons:

    1. **Sandbox correctness.** The primary OSEnv (which backs the web-UI
       filesystem / shell endpoints) is materialized once per session from the
       *original* agent's spec and cached. Closing it AND invalidating the
       spec/snapshot caches forces the next access to re-resolve and
       re-materialize from the NEW agent's spec, so those endpoints run
       under the switched-to agent's ``os_env``/sandbox — not the old one.
       (Agent ``sys_os_*`` tool calls already re-derive os_env per call, and
       native terminals re-evaluate the sandbox gate on respawn; this closes
       the remaining stale path.)
    2. **Terminal rebuild.** A lingering native terminal would otherwise shadow
       the switch-back transcript rebuild (auto-create skips while one exists).

    A dedicated endpoint (rather than ``DELETE /resources``) keeps the
    session-deletion contract untouched — deletion never needs the
    switch-specific cache reset.

    A switch only runs while the session is idle, so closing the env + terminal
    here is safe — unlike doing it inside the next turn's dispatch, which wedges
    that turn. cwd is re-derived from the runner's bound workspace, so the
    working directory / git worktree is preserved (only the sandbox changes;
    a ``fork``/``start_in_scratch`` agent gets a fresh scratch copy). The
    claude-native auto-create gate remains the switch-back safety net if this
    call is lost (runner offline, races).

    :param session_id: Session/conversation id just switched, e.g.
        ``"conv_abc123"``.
    :returns: None.
    """
    try:
        runner_client = await _get_runner_client_for_resource_access(session_id)
        if runner_client is None:
            return
        reset_resp = await runner_client.post(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/reset-state",
            timeout=15.0,
        )
        # httpx only raises on transport errors — a 4xx/5xx reset response
        # still returns. A non-2xx means the runner did NOT close the old
        # env, so it must take the failure path below (suppressing the
        # invalidation publish); HTTPStatusError is an httpx.HTTPError.
        reset_resp.raise_for_status()
    except (httpx.HTTPError, HTTPException, OmnigentError, RuntimeError):
        # Best-effort: a runner hiccup must not break the (already-committed)
        # switch. OmnigentError covers the session-not-runner-bound / runner-
        # offline case raised by _get_runner_client_for_resource_access. The
        # auto-create gate rebuilds on switch-back regardless. No
        # changed-files event on this path either: the runner's env cache is
        # still the OLD agent's, so a triggered refetch would re-serve it —
        # and a lost runner rebuilds from the new spec on relaunch anyway.
        _logger.warning(
            "post-switch runner-resource reset failed for session=%s", session_id, exc_info=True
        )
        return
    # The old agent's cached OSEnv is now closed, so a refetch triggered by
    # this event re-materializes filesystem state from the NEW agent's spec.
    # This is what flips the web Files tab when the switch crosses an
    # os_env boundary (none→some shows it, some→none hides it) — the
    # session.agent_changed event fires before the reset and so cannot
    # carry a trustworthy availability signal.
    _publish_changed_files_invalidated(session_id)


def _native_coding_agent_for_session(conv: Conversation) -> NativeCodingAgent | None:
    """
    Resolve native terminal metadata for a session, by wrapper label OR harness.

    Two independent signals identify a native session, because native message
    handling must NOT be coupled to the terminal-first presentation labels:

    * the ``omnigent.wrapper`` presentation label — set for the built-in
      terminal-first wrapper sessions (``omnigent claude`` / ``omnigent
      codex``); resolved directly and cheaply here (short-circuits the harness
      load below); and
    * the bound agent's RESOLVED harness — for a CUSTOM agent that declares a
      native harness (e.g. a user ``polly`` orchestrator with
      ``executor.harness: codex-native``) but is intentionally CHAT-first, so
      it carries no wrapper label. Its runner still runs a native transcript
      forwarder (the single writer for the conversation), so its web messages
      must take the same native single-writer path — else the inbound user
      message is persisted AP-side AND mirrored by the forwarder, landing
      twice. Resolved via :func:`_resolve_harness` (honors a per-session
      ``harness_override``), independent of the presentation labels; SDK
      harnesses resolve to ``None``.

    :param conv: Conversation row for the target session.
    :returns: The :class:`NativeCodingAgent` for the session's harness, or
        ``None`` when it is not a native terminal harness.
    """
    wrapper = conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
    native_agent = native_coding_agent_for_wrapper_label(wrapper)
    if native_agent is not None:
        return native_agent
    return native_coding_agent_for_harness(_resolve_harness(conv))


def _native_terminal_name_for_harness(harness: str) -> str:
    """
    Return the runner terminal resource name for a native harness.

    :param harness: Native harness identifier, e.g. ``"codex-native"``.
    :returns: Terminal resource name, e.g. ``"codex"``.
    :raises OmnigentError: If *harness* is not a supported native
        terminal harness.
    """
    native_agent = native_coding_agent_for_harness(harness)
    if native_agent is not None:
        return native_agent.terminal_name
    raise OmnigentError(
        "Unsupported native terminal session",
        code=ErrorCode.INVALID_INPUT,
    )


def _native_terminal_failure_from_runner_response(
    resp: httpx.Response,
    *,
    display_name: str,
) -> ErrorData:
    """
    Convert a failed runner terminal-ensure response into durable error data.

    The runner's terminal ensure endpoint must return structured
    ``{"error": {"code": ..., "message": ...}}`` for definitive startup
    failures (for example a missing native CLI). Preserve that message
    exactly so the transcript shows the real cause. If the runner returns
    an opaque framework 500 body such as ``"Internal Server Error"``,
    surface an explicit malformed-runner-response error instead of
    inventing a native terminal cause.

    :param resp: Non-2xx response from
        ``POST /v1/sessions/{id}/resources/terminals``.
    :param display_name: Human-readable runtime name, e.g. ``"Codex"``.
    :returns: Error data suitable for a persisted ``type="error"``
        conversation item.
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        raw_error = body.get("error")
        if isinstance(raw_error, dict):
            raw_code = raw_error.get("code")
            raw_message = raw_error.get("message")
            if (
                isinstance(raw_code, str)
                and raw_code.strip()
                and isinstance(raw_message, str)
                and raw_message.strip()
            ):
                return ErrorData(
                    source="execution",
                    code=raw_code,
                    message=raw_message,
                )
    return ErrorData(
        source="execution",
        code=_NATIVE_TERMINAL_ENSURE_FAILED_CODE,
        message=(
            f"Native {display_name} terminal ensure failed with malformed "
            f"runner response (HTTP {resp.status_code})."
        ),
    )


def _native_terminal_ensure_transport_error(
    exc: httpx.HTTPError | ConnectionError,
    *,
    display_name: str,
) -> ErrorData:
    """
    Convert runner transport failure during native terminal ensure.

    The message path has exactly one preflight path for native terminal
    readiness. If that path cannot reach the runner, fail the user turn
    explicitly instead of falling back to the old forward-and-wait path.

    :param exc: Transport exception from the ensure request, e.g.
        ``httpx.ConnectError("connection refused")`` or the bare
        ``ConnectionError("tunnel closed before request completed")``
        that ``WSTunnelTransport`` raises on tunnel close.
    :param display_name: Human-readable runtime name, e.g. ``"Codex"``.
    :returns: Error data suitable for a persisted ``type="error"``
        conversation item.
    """
    detail = str(exc).strip()
    message = f"Native {display_name} terminal ensure request failed."
    if detail:
        message = f"{message} {detail}"
    return ErrorData(
        source="execution",
        code=_NATIVE_TERMINAL_ENSURE_FAILED_CODE,
        message=message,
    )


@dataclass
class _NativeTerminalEnsureOutcome:
    """
    Result of a native terminal readiness probe.

    :param error: Error data when the runner definitively failed to
        create the terminal (fails the turn with a durable banner), or
        ``None`` when the terminal is ready / the failure was not
        definitive.
    :param policy_notice: Human-readable reason that tool-call policy
        enforcement is NOT active for this session (fail-open — codex too
        old or the hook could not be trusted), or ``None`` when
        enforcement is active. Non-fatal: surfaced once as a durable
        banner, never blocks the turn.
    """

    error: ErrorData | None
    policy_notice: str | None


def _policy_notice_from_ensure_response(resp: httpx.Response) -> str | None:
    """
    Extract a non-fatal policy-disabled notice from a 2xx ensure response.

    The runner attaches ``policy_hook_disabled_reason`` (once) to its
    terminal-ensure success body when the session degraded to no policy
    enforcement. A malformed / non-JSON body is treated as "no notice"
    rather than failing the (successful) readiness probe.

    :param resp: The runner's 2xx ensure response.
    :returns: The reason string, or ``None`` when absent / unparseable.
    """
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    reason = body.get("policy_hook_disabled_reason")
    return reason if isinstance(reason, str) and reason.strip() else None


def _publish_error_event(session_id: str, error: ErrorData) -> None:
    """
    Publish a live ``response.error`` event for a persisted error item.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param error: Durable error payload to mirror into SSE.
    :returns: None.
    """
    event = ErrorEvent(
        type="response.error",
        source=error.source,
        error={"code": error.code, "message": error.message},
    )
    session_stream.publish(session_id, event.model_dump())


async def _persist_native_policy_notice(
    session_id: str,
    conversation_store: ConversationStore,
    reason: str,
) -> None:
    """
    Persist + publish a non-fatal "policy not enforced" banner.

    The runner reports (once, via the terminal-ensure success response)
    that a native codex session started but tool-call policy enforcement
    is inactive (fail-open: codex too old, or the policy hook could not be
    trusted). This records a durable ``type="error"`` banner so the web UI
    shows the degraded-security state across refresh/reconnect, and
    mirrors it as a live ``response.error`` event. Unlike
    :func:`_persist_native_terminal_failure` it does NOT consume the user
    message or mark the turn failed — the terminal is up and the message
    still forwards; this is an advisory notice only.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conversation_store: Store used for the durable append.
    :param reason: Human-readable cause from the runner, e.g. ``"Codex CLI
        0.128.0 is older than 0.129.0; upgrade codex to enforce tool-call
        policies."``.
    :returns: None.
    """
    error = ErrorData(
        source="execution",
        code=_NATIVE_POLICY_NOT_ENFORCED_CODE,
        message=f"Tool-call policy enforcement is not active for this session: {reason}",
    )
    persisted = await _relay_persist_error_once(
        conversation_store,
        session_id,
        NewConversationItem(
            type="error",
            response_id=generate_task_id(),
            data=error,
        ),
    )
    # Mirror to live clients only when newly persisted (the runner's
    # one-shot flag already prevents re-surfacing; this dedups a same-turn
    # retry against an already-recorded notice).
    if persisted == "persisted":
        _publish_error_event(session_id, error)


def _extract_claude_native_runner_failure(resp: httpx.Response) -> str | None:
    """
    Return a harness failure message from a runner SSE response.

    Runner ``POST /v1/sessions/{id}/events`` returns HTTP 200 for a
    syntactically valid harness stream even when the harness emits
    ``response.failed``. Claude-native Omnigent forwarding must treat that
    as failed injection, otherwise the web UI would believe a message
    reached the terminal when ``tmux send-keys`` actually failed.

    :param resp: Completed runner response.
    :returns: Failure message, or ``None`` when no failure event is
        present.
    """
    content_type = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" not in content_type and "response.failed" not in text:
        return None
    for frame in text.split("\n\n"):
        data_lines = [
            line.removeprefix("data:").strip()
            for line in frame.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "response.failed":
            continue
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail")
            if isinstance(message, str) and message:
                return message
            return json.dumps(error, sort_keys=True)
        if isinstance(error, str) and error:
            return error
        return "runner reported response.failed"
    return None


async def _forward_session_change_to_runner(
    *args: Any, **kwargs: Any
) -> _RunnerForwardResult | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._forward_session_change_to_runner(*args, **kwargs)


async def _forward_session_change_to_runner_impl(
    session_id: str,
    runner_router: Any,
    event: dict[str, Any],
) -> _RunnerForwardResult | None:
    """
    Best-effort POST a control event to the bound runner.

    Used for control inputs the runner dispatches by harness in its
    ``/v1/sessions/{id}/events`` handler — claude-native injects the
    corresponding slash command into the tmux pane; other harnesses
    return 204 no-op. Two kinds of caller use this:

    * PATCH-driven harness notifications (``effort_change``,
      ``model_change``) — claude-native injects the slash command,
      other harnesses re-read the persisted value at the next turn
      boundary, so they ignore the return value.
    * Explicit ``compact`` — the caller inspects the returned status
      to decide whether the runner handled the control (claude-native,
      200) or the Omnigent server must run its own in-process compaction
      (204 / no runner). See the ``compact`` branch in
      :func:`post_event`.

    Mirrors the interrupt-forward fallback chain: prefer the per-
    session router binding, fall back to the global runner client
    (in-process / test setups where the router hasn't bound the
    session). When neither resolves to a client, the POST is silently
    skipped — the persisted value on the Omnigent side is the authoritative
    fallback, picked up by the next spawn.

    Non-2xx runner responses (e.g. 503 when the tmux pane isn't
    advertised yet) are logged as warnings so the failure surfaces
    in the Omnigent log — otherwise the POST succeeds at the httpx layer
    and the status would be silently dropped.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param runner_router: The session's ``RunnerRouter`` (may be
        ``None`` in tests / in-process setups).
    :param event: The ``/events`` POST body, e.g.
        ``{"type": "effort_change", "effort": "high"}``,
        ``{"type": "model_change", "model": "claude-opus-4-7"}``, or
        ``{"type": "compact"}``.
    :returns: The runner's HTTP status/body, or ``None`` when no
        runner client could be resolved or the POST failed at the
        transport layer (in both cases the AP-side persisted value /
        operation is the authoritative fallback).
    """
    from omnigent.runtime import get_runner_client

    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return None
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=event,
            timeout=5.0,
        )
    except (httpx.HTTPError, ConnectionError):
        _logger.exception(
            "Session-change forward failed for session=%r type=%r",
            session_id,
            event.get("type"),
        )
        return None
    if resp.status_code >= 400:
        _logger.warning(
            "Session-change forward rejected for session=%s type=%r status=%s body=%s",
            session_id,
            event.get("type"),
            resp.status_code,
            resp.text,
        )
    return _RunnerForwardResult(status_code=resp.status_code, body=resp.text)


async def _stop_session_via_runner(*args: Any, **kwargs: Any) -> bool:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._stop_session_via_runner(*args, **kwargs)


async def _stop_session_via_runner_impl(
    session_id: str,
    runner_router: Any,
) -> bool:
    """
    Forward a ``stop_session`` request to the bound runner, surfacing
    failures to the caller instead of swallowing them.

    Unlike :func:`_forward_session_change_to_runner` (used for
    ``effort_change`` / ``model_change``, where a dropped forward is
    benign — the runner re-reads the persisted value at the next turn),
    a failed ``stop_session`` means the session is *still alive*. The
    web UI's "Stop session" action is destructive and treats a 2xx as
    success (it closes the confirmation dialog), so a swallowed failure
    would tell the user the session stopped when it did not. This
    helper therefore raises on a transport error or non-2xx runner
    response.

    Runner-client resolution mirrors the best-effort helper's fallback
    chain: prefer the per-session router binding, fall back to the
    global runner client (in-process / test setups). When neither
    resolves to a client there is no live runner bound — the session is
    not running on any runner, so the stop is a no-op success and this
    returns ``False`` without raising (the caller uses that to discard
    the turn fence it installed, since no runner means nothing else
    would ever lift it).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param runner_router: The session's ``RunnerRouter`` (may be
        ``None`` in tests / in-process setups).
    :returns: ``True`` if the stop was delivered to a runner (2xx),
        ``False`` if no runner client resolved (nothing forwarded).
    :raises OmnigentError: ``RUNNER_UNAVAILABLE`` (HTTP 503) if the
        runner could not be reached or reported a non-2xx — e.g. the
        claude-native tmux pane is wedged and ``kill_session`` failed.
        The web UI maps this to a visible "stop failed" state rather
        than closing the dialog as if the session stopped.
    """
    from omnigent.runtime import get_runner_client

    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return False
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": _STOP_SESSION_TYPE},
            timeout=5.0,
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        # WSTunnelTransport raises bare ConnectionError on tunnel close.
        raise OmnigentError(
            f"Could not reach the runner to stop session {session_id!r}: {exc}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    if resp.status_code >= 400:
        raise OmnigentError(
            f"Runner failed to stop session {session_id!r} "
            f"(status {resp.status_code}): {resp.text}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    return True


async def _stop_session_host_runner(
    session_id: str,
    host_id: str,
    runner_id: str,
    host_registry: Any,
) -> bool:
    """
    Terminate the host-launched runner backing a host-spawned session.

    "Stop session" on a host-spawned session must end the dedicated runner
    subprocess the host launched for it — there is exactly one runner per
    host-launched session (see ``POST /v1/hosts/{host_id}/runners`` and the
    host-launch branch of session create). Killing the ``claude`` tmux pane
    via :func:`_stop_session_via_runner` is not enough on its own: the
    runner stays connected, so ``GET /health`` keeps reporting
    ``runner_online: true`` for the session and the web UI never shows it as
    disconnected — new messages are accepted and hang on "working" against a
    dead pane.

    Bringing the runner's tunnel down is what flips ``runner_online`` to
    ``false``; ``_on_runner_disconnect`` then marks the session and the web
    UI renders the "Agent disconnected — click to show reconnect command"
    banner, identical to the end state a CLI-launched session reaches when
    its process exits.

    Best-effort by design: the pane is already gone before this runs, so a
    host that is offline, was replaced, or is slow to acknowledge is logged
    and swallowed rather than failing the whole Stop. In the common case —
    the host's ``omnigent host`` tunnel is open while the user drives
    the web UI — the stop is delivered and the runner exits. The runner this
    targets is read from the caller's own (owner-gated) session row, so it
    can only ever stop the runner bound to that session.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param host_id: Owning host identifier from the session row, e.g.
        ``"host_a1b2c3d4..."``.
    :param runner_id: Runner bound to the session, e.g.
        ``"runner_token_abc123..."``.
    :param host_registry: The :class:`HostRegistry` tracking live host
        tunnels on this replica, or ``None`` when host support is not wired
        (in-process / test setups without a host tunnel).
    :returns: ``True`` when the stop was delivered and acknowledged (the
        runner is exiting, so a tunnel drop is expected); ``False`` on any
        best-effort early-out (no host registry, host offline/replaced,
        ack timeout, or host-reported failure) where the runner may keep
        running and no tunnel drop will follow.
    """
    if host_registry is None:
        return False
    conn = host_registry.get(host_id)
    if conn is None:
        _logger.warning(
            "Cannot stop runner %s for session %s: host %s is offline; "
            "the runner may linger online and the session will not show as "
            "disconnected",
            runner_id,
            session_id,
            host_id,
        )
        return False
    from omnigent.host.frames import HostStopRunnerFrame, encode_host_frame

    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, str | None]] = asyncio.get_running_loop().create_future()
    conn.pending_stops[request_id] = future
    stop_frame = encode_host_frame(
        HostStopRunnerFrame(request_id=request_id, runner_id=runner_id),
    )
    try:
        host_registry.send_text(conn, stop_frame)
    except ConnectionError:
        conn.pending_stops.pop(request_id, None)
        _logger.warning(
            "Cannot stop runner %s for session %s: host %s connection was replaced",
            runner_id,
            session_id,
            host_id,
        )
        return False
    try:
        result = await asyncio.wait_for(
            future,
            timeout=_STOP_RUNNER_RESULT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        conn.pending_stops.pop(request_id, None)
        _logger.warning(
            "Host %s did not acknowledge stop of runner %s for session %s",
            host_id,
            runner_id,
            session_id,
        )
        return False
    if result.get("status") == "failed":
        _logger.warning(
            "Host %s failed to stop runner %s for session %s: %s",
            host_id,
            runner_id,
            session_id,
            result.get("error"),
        )
        return False
    return True


def _build_new_item(
    body: SessionEventInput,
    response_id: str,
    created_by: str | None = None,
) -> NewConversationItem:
    """
    Construct a :class:`NewConversationItem` from a POSTed event.

    Validates the data payload via ``parse_item_data`` (the same
    validator the route boundary already invoked) and wraps the
    result with the response_id linkage required by the conversation
    store.

    :param body: Validated event input — guaranteed to be a known
        item type (the route checked ``_ALLOWED_EVENT_TYPES``).
    :param response_id: The task id the new item should be tagged
        with — either the steered active task or a freshly-created
        one.
    :param created_by: Authenticated identity of the actor posting
        the event, recorded for per-message attribution. ``None`` in
        single-user mode.
    :returns: A :class:`NewConversationItem` ready for delivery
        or persistence.
    """
    data = parse_item_data(body.type, {"type": body.type, **body.data})
    return NewConversationItem(
        type=body.type,
        response_id=response_id,
        data=data,
        created_by=created_by,
    )


def _parse_skill_slash_command(body: SessionEventInput) -> tuple[str, str]:
    """
    Validate and unpack a structured skill slash-command event.

    The REPL posts ``type="slash_command"`` for skill invocations.
    Other command kinds are surfaced by terminal transcript bridges
    through ``external_conversation_item`` and are not executable
    session inputs on this route.

    :param body: Validated event input with ``type="slash_command"``
        and data such as ``{"kind": "skill", "name": "grill-me",
        "arguments": "review this plan"}``.
    :returns: ``(skill_name, arguments)`` with whitespace-trimmed
        command name and raw argument text.
    :raises OmnigentError: If the payload is not a skill command
        or is missing a usable skill name / arguments string.
    """
    kind = body.data.get("kind", "skill")
    if kind != "skill":
        raise OmnigentError(
            "slash_command events only support kind='skill'; use the "
            "dedicated control event for built-in commands",
            code=ErrorCode.INVALID_INPUT,
        )
    name = body.data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise OmnigentError(
            "slash_command requires non-empty data.name",
            code=ErrorCode.INVALID_INPUT,
        )
    arguments = body.data.get("arguments", "")
    if not isinstance(arguments, str):
        raise OmnigentError(
            "slash_command data.arguments must be a string",
            code=ErrorCode.INVALID_INPUT,
        )
    return name.strip(), arguments


def _build_skill_slash_command_policy_body(body: SessionEventInput) -> SessionEventInput:
    """
    Build the user-message shape used for input policy evaluation.

    Skill commands inject a hidden meta message containing the full
    skill body, but input guardrails should evaluate the text the user
    actually typed, not the skill instructions maintained by the
    server. This preserves the legacy policy surface of
    ``/<skill> <arguments>`` without making bundled skill content
    policy-sensitive.

    :param body: Validated ``slash_command`` event body with data such
        as ``{"name": "grill-me", "arguments": "review this plan"}``.
    :returns: Synthetic user ``message`` event for policy evaluation.
    :raises OmnigentError: If the slash-command payload is invalid.
    """
    skill_name, arguments = _parse_skill_slash_command(body)
    command_text = f"/{skill_name}" if not arguments else f"/{skill_name} {arguments}"
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": command_text}],
        },
    )


async def _resolve_skill_meta_text_via_runner(
    session_id: str,
    skill_name: str,
    arguments: str,
    runner_client: httpx.AsyncClient,
) -> str:
    """
    Resolve a skill's hidden ``<skill>`` meta text on the bound runner.

    Skill content is runner-owned: the runner reads the ``SKILL.md``
    body and resource files from the skill's directory on its own
    filesystem, so the embedded ``<path>`` and resource listing are
    valid where the harness executes. Wraps
    ``POST /v1/sessions/{id}/skills/resolve``.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param skill_name: Exact skill name to resolve, e.g.
        ``"code-review"``.
    :param arguments: Raw argument string typed after the slash
        command, e.g. ``"review this plan"``. Empty when none.
    :param runner_client: HTTP client pointed at the bound runner.
    :returns: The hidden ``<skill>`` meta text for a single
        ``input_text`` block.
    :raises OmnigentError: If the skill is not exposed for the session
        (the runner 404s with the available list), or the runner is
        unreachable / errors while resolving.
    """
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/skills/resolve",
            json={"name": skill_name, "arguments": arguments},
            timeout=10.0,
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        raise OmnigentError(
            f"Runner unreachable while resolving skill {skill_name!r}: {exc}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if resp.status_code not in (200, 404):
        raise OmnigentError(
            f"Runner failed to resolve skill {skill_name!r}: HTTP {resp.status_code}",
            code=ErrorCode.INTERNAL_ERROR,
        )
    # Parse the body once, guarded: a transport proxy / HTML error page /
    # non-object body must surface as a controlled runner failure, not an
    # uncaught 500.
    try:
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
    except ValueError as exc:
        raise OmnigentError(
            f"Runner returned a malformed skill resolution for {skill_name!r}: {exc}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if resp.status_code == 404:
        available = payload.get("available", [])
        raise OmnigentError(
            f"Skill {skill_name!r} not found. Available skills: {available}",
            code=ErrorCode.INVALID_INPUT,
        )
    meta_text = payload.get("meta_text")
    if not isinstance(meta_text, str):
        raise OmnigentError(
            f"Runner returned malformed skill resolution for {skill_name!r}: missing 'meta_text'",
            code=ErrorCode.INTERNAL_ERROR,
        )
    return meta_text


async def _dispatch_skill_slash_command_to_runner(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    *,
    agent: Agent,
    has_mcp_servers: bool,
    created_by: str | None,
) -> str:
    """
    Persist a skill slash command and forward hidden skill context.

    Skill content is runner-owned: this asks the bound runner to
    resolve the skill (``POST /v1/sessions/{id}/skills/resolve``) into
    its ``<skill>`` meta text, reading the ``SKILL.md`` body and
    resource files from the skill's directory *on the runner* — so the
    embedded ``<path>`` and resource listing are valid where the harness
    executes. The server then persists the result (runner-resolves,
    server-persists). Appends two conversation items with the same
    response id:

    * a visible ``slash_command`` item for the UI transcript;
    * a hidden ``message`` item with ``is_meta=True`` containing the
      full skill instructions for runner history replay.

    Only the hidden message is sent to the runner as input. The visible
    command is published as ``response.output_item.done`` after the
    runner accepts the event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id``.
    :param body: Structured ``slash_command`` event body.
    :param conversation_store: Store used to append both durable
        items.
    :param runner_client: HTTP client pointed at the bound runner.
    :param agent: Agent bound to the conversation.
    :param has_mcp_servers: ``True`` when the agent spec declares MCP
        servers; forwarded unchanged to the runner event.
    :param created_by: Authenticated actor id, e.g.
        ``"alice@example.com"``, or ``None`` in single-user mode.
    :returns: The persisted visible ``slash_command`` item id.
    :raises OmnigentError: If the skill is not exposed for the
        session, or the runner is unreachable while resolving it.
    """
    import uuid

    skill_name, arguments = _parse_skill_slash_command(body)
    meta_text = await _resolve_skill_meta_text_via_runner(
        session_id,
        skill_name,
        arguments,
        runner_client,
    )

    response_id = f"turn_{uuid.uuid4().hex}"
    meta_content = [{"type": "input_text", "text": meta_text}]
    visible_item = NewConversationItem(
        type=_SLASH_COMMAND_TYPE,
        response_id=response_id,
        data=SlashCommandData(
            agent=agent.name,
            kind="skill",
            name=skill_name,
            arguments=arguments,
        ),
        created_by=created_by,
    )
    meta_item = NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role="user",
            content=meta_content,
            is_meta=True,
        ),
        created_by=created_by,
    )
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [visible_item, meta_item],
    )
    visible = persisted_items[0]

    # Mirror the plain-message path's title seeding: a session whose FIRST
    # message is a skill invocation (web landing composer, REPL) would
    # otherwise keep a NULL title and the sidebar falls back to the
    # conversation id. Titled from the typed command ("/debate kafka…"),
    # NOT the hidden meta item — that's the full SKILL.md instruction blob.
    command_text = f"/{skill_name} {arguments}" if arguments else f"/{skill_name}"
    await _seed_missing_title(
        conv,
        [{"type": "input_text", "text": command_text}],
        conversation_store,
    )

    runner_body: dict[str, Any] = {
        "type": "message",
        "role": "user",
        "content": meta_content,
        "agent_id": conv.agent_id,
        "model": agent.name,
        "has_mcp_servers": has_mcp_servers,
        # The forwarded message carries ``meta_content`` — i.e. the
        # META item (persisted_items[1]), not the user-visible item.
        # Hand the runner that id so a cold-cache reload drops the
        # right persisted copy (see _forward_event_to_runner).
        "persisted_item_id": persisted_items[1].id,
    }
    effective_runner_override = (
        body.model_override if body.model_override is not None else conv.model_override
    )
    if effective_runner_override is not None:
        runner_body["model_override"] = effective_runner_override
    # Per-session brain-harness override — create-time only, so no
    # per-event value exists; the persisted column is the source. The
    # "auto" sentinel is resolved to a concrete harness at first-message
    # time and never forwarded verbatim.
    if conv.harness_override is not None and conv.harness_override != "auto":
        runner_body["harness_override"] = conv.harness_override

    try:
        await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=runner_body,
            timeout=_RUNNER_FORWARD_TIMEOUT,
        )
        event = OutputItemDoneEvent(type="response.output_item.done", item=visible.to_api_dict())
        session_stream.publish(session_id, event.model_dump())
    except (httpx.HTTPError, ConnectionError) as exc:
        _logger.exception(
            "Forward of skill slash command failed for session=%s",
            session_id,
        )
        _publish_status(session_id, "idle")
        raise OmnigentError(
            "Runner is unreachable; message was persisted but could not be delivered. "
            "The runner may be restarting — retry or spawn a new session.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    return visible.id


def _title_content_from_item(
    item: NewConversationItem | ConversationItem,
) -> list[dict[str, Any]]:
    """
    Extract title candidate content blocks from a session item.

    User ``message`` items contribute their text. A Skill ``slash_command``
    item (``kind == "skill"``) contributes its typed command, e.g.
    ``"/my-plugin:my-skill ARG-123"`` — a Claude Code native session whose
    first action is a Skill arrives over the transcript bridge as a
    ``slash_command``, not a user ``message``, so without this it stays
    untitled and the sidebar falls back to the generic "Claude Code" label
    (#851). CLI built-ins (``kind == "command"`` — ``/clear``, ``/compact``,
    ``/model``, …) are excluded so a surfaced built-in never becomes the
    session title. Tool results and assistant-shaped messages return an empty
    list so callers leave the conversation title unchanged.

    :param item: The parsed item being persisted, e.g. a user
        ``"message"`` item with input text content.
    :returns: Content blocks that may contribute to a synthesized
        title, e.g. ``[{"type": "input_text", "text": "Hello"}]``.
    """
    if item.type == _SLASH_COMMAND_TYPE:
        # Title a Skill-first session from the typed command; skip surfaced CLI
        # built-ins (kind == "command") which aren't meaningful session topics.
        if not isinstance(item.data, SlashCommandData) or item.data.kind != "skill":
            return []
        command = f"/{item.data.name}"
        arguments = item.data.arguments.strip()
        text = f"{command} {arguments}" if arguments else command
        return [{"type": "input_text", "text": text}]
    if item.type != "message":
        return []
    if not isinstance(item.data, MessageData):
        return []
    if item.data.role != "user":
        return []
    if item.data.is_meta:
        return []
    return item.data.content


async def _seed_missing_title(
    conv: Conversation,
    content: list[dict[str, Any]],
    conversation_store: ConversationStore,
) -> None:
    """
    Set an untitled conversation's title from message content blocks.

    No-op when the conversation already has a title or the blocks
    yield no usable text. Mutates ``conv.title`` in place on success
    so callers holding the row see the persisted value.

    :param conv: The conversation row for the session.
    :param content: Title-candidate blocks, e.g.
        ``[{"type": "input_text", "text": "/debate kafka vs sqs"}]``.
    :param conversation_store: Store used to persist the title.
    :returns: None.
    """
    if conv.title is not None:
        return
    title = synthesize_conversation_title(content)
    if title is None:
        return
    updated = await asyncio.to_thread(
        conversation_store.update_conversation,
        conv.id,
        title=title,
    )
    if updated is not None:
        conv.title = updated.title


async def _seed_missing_title_from_user_message(
    conv: Conversation,
    item: NewConversationItem,
    conversation_store: ConversationStore,
) -> None:
    """
    Set an untitled session's title from a user message.

    The app UI creates sessions with ``initial_items=[]`` and posts
    the first user message through ``POST /v1/sessions/{id}/events``.
    This helper also covers callers that pass initial items to
    ``POST /v1/sessions``. Non-user-message items are ignored, and
    already-titled conversations are left unchanged.

    :param conv: The conversation row for the session.
    :param item: The parsed item being persisted.
    :param conversation_store: Store used to persist the title.
    :returns: None.
    """
    await _seed_missing_title(conv, _title_content_from_item(item), conversation_store)


def _extract_user_text_for_routing(body: SessionEventInput) -> str:
    """Extract plain text from a user message event for the routing judge.

    Concatenates all ``input_text`` blocks in ``body.data["content"]``,
    returning the first 4 000 characters.  Returns ``""`` for non-message
    events or events with no text content.
    """
    content = body.data.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "input_text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
    return " ".join(parts)[:4000]


async def _emit_server_routing_decision(
    session_id: str,
    conversation_store: ConversationStore,
    model: str,
    verdict: dict[str, Any],
    *,
    agent: str | None = None,
) -> None:
    """Persist and publish a ``routing_decision`` transcript chip.

    Called by the server-side routing path before the turn is forwarded
    to the runner.  The chip shows the judge's model pick at turn start
    — the same UX the runner-side advisor produced, but driven entirely
    by the server.

    :param agent: Sub-agent name to include when mirroring a child
        session's routing decision into the parent's transcript.
    """
    import uuid

    rationale = verdict.get("rationale", "")
    applied = verdict.get("applied", True)
    item_data: dict[str, Any] = {
        "model": model,
        "applied": bool(applied),
        "rationale": rationale if isinstance(rationale, str) else "",
    }
    if agent is not None:
        item_data["agent"] = agent
    try:
        parsed_data = parse_item_data("routing_decision", item_data)
    except (ValueError, TypeError):
        _logger.warning("Server routing: failed to parse routing_decision data")
        return

    routing_item = NewConversationItem(
        type="routing_decision",
        response_id=f"routing_{uuid.uuid4().hex}",
        data=parsed_data,
    )
    try:
        persisted = await asyncio.to_thread(conversation_store.append, session_id, [routing_item])
        persisted_id: str | None = persisted[0].id if persisted else None
    except Exception:
        _logger.exception(
            "Server routing: routing_decision persist failed for session=%s",
            session_id,
        )
        persisted_id = None

    # Publish live event so the web UI renders the chip immediately.
    session_stream.publish(
        session_id,
        {
            "type": "response.output_item.done",
            "item": {
                "id": persisted_id,
                "type": "routing_decision",
                **item_data,
            },
        },
    )


@dataclass
class _SessionEventDispatchResult:
    """
    Outcome of forwarding one item-event to the runner.

    :param item_id: Store-assigned id of the AP-persisted item, e.g.
        ``"item_abc123"``. ``None`` for the claude-native message
        bypass, which persists nothing AP-side.
    :param pending_id: Id of the :mod:`omnigent.runtime.pending_inputs`
        entry recorded for a native-terminal web message, e.g.
        ``"pending_a1b2c3"`` — surfaced to the sender so it can adopt
        the id and dedupe against the snapshot. ``None`` for non-native
        events (already persisted, so no separate pending entry).
    """

    item_id: str | None
    pending_id: str | None


def _extract_persistent_item_from_sse(
    event: dict[str, Any],
    response_id: str | None = None,
) -> NewConversationItem | None:
    """
    Extract a persistable conversation item from a runner SSE event.

    Returns a ``NewConversationItem`` for:

    - ``response.output_item.done`` events carrying an assistant
      message, function_call, or function_call_output.
    - ``compaction`` events carrying a conversation summary from
      the runner's compaction system.

    Returns ``None`` for all other events (transient deltas, turn
    lifecycle, compaction progress indicators, etc.).

    :param event: Parsed SSE event dict from the runner stream.
    :param response_id: Turn-scoped id from the most recent
        ``response.in_progress`` event. All items persisted within
        the same turn share this id so the web UI can group them
        into a single bubble and pair function_calls with their
        outputs. Falls back to a fresh uuid when unavailable.
    :returns: A ``NewConversationItem`` ready for
        ``conv_store.append()``, or ``None``.
    """
    import uuid

    evt_type = event.get("type")

    if evt_type == "compaction":
        try:
            data = parse_item_data("compaction", event)
        except (ValueError, TypeError):
            _logger.warning("Failed to parse compaction item from SSE")
            return None

        return NewConversationItem(
            type="compaction",
            response_id=f"compact_{uuid.uuid4().hex}",
            data=data,
        )

    if evt_type != "response.output_item.done":
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type not in ("message", "function_call", "function_call_output"):
        return None
    # Skip transient observed function_call events (status
    # ``in_progress`` / ``action_required``).  Only ``completed``
    # function_calls are durable — the scaffold emits them after
    # the dispatch Future resolves.  Persisting interim statuses
    # creates orphan conversation items whose spinners never
    # resolve in the web UI.
    if item_type == "function_call" and item.get("status") != "completed":
        return None
    try:
        data = parse_item_data(item_type, item)
    except (ValueError, TypeError):
        _logger.warning(
            "Failed to parse persistent item from SSE: %s",
            item_type,
        )
        return None

    return NewConversationItem(
        type=item_type,
        response_id=response_id or f"turn_{uuid.uuid4().hex}",
        data=data,
    )


def _resource_event_item_from_sse(
    session_id: str,
    event: dict[str, Any],
) -> NewConversationItem | None:
    """
    Build a ``resource_event`` conversation item from a runner SSE event.

    The runner emits ``session.resource.created`` /
    ``session.resource.deleted`` when an agent tool
    (``sys_terminal_launch`` / ``sys_terminal_close``) materializes or
    tears down a session resource mid-turn. The relay republishes the
    raw event onto the live ``session_stream`` (so connected clients
    update instantly); this helper produces the durable conversation
    item so a client that reconnects mid-turn rediscovers the resource
    in the snapshot — matching the REST resource path
    (:func:`_publish_and_persist_resource_event`).

    Returns ``None`` for every other event type, and for malformed
    resource events (missing id / type) so a bad frame can't poison
    the relay.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param event: Parsed SSE event dict from the runner stream.
    :returns: A ``resource_event`` :class:`NewConversationItem`, or
        ``None``.
    """
    from omnigent.entities.conversation import ResourceEventData

    evt_type = event.get("type")
    if evt_type == "session.resource.created":
        resource = event.get("resource")
        if not isinstance(resource, dict):
            return None
        resource_id = resource.get("id")
        resource_type = resource.get("type")
    elif evt_type == "session.resource.deleted":
        resource = None
        resource_id = event.get("resource_id")
        resource_type = event.get("resource_type")
    else:
        return None

    # Require non-empty id/type. ``isinstance(x, str)`` alone admits
    # ``""``, which would persist a malformed resource_event item the
    # snapshot can't resolve back to a real resource. Drop the frame
    # instead — the snapshot endpoint stays the source of truth.
    if not resource_id or not isinstance(resource_id, str):
        return None
    if not resource_type or not isinstance(resource_type, str):
        return None

    return NewConversationItem(
        type="resource_event",
        response_id=session_id,
        data=ResourceEventData(
            event_type=evt_type,
            resource_id=resource_id,
            resource_type=resource_type,
            resource=resource,
        ),
    )


def _routing_decision_item_from_sse(
    event: dict[str, Any],
) -> NewConversationItem | None:
    """
    Build a ``routing_decision`` conversation item from a runner SSE event.

    The runner's cost advisor emits a ``response.output_item.done`` with a
    ``routing_decision`` item at the START of an advised turn (the
    intelligent model router's pick). This produces the durable,
    display-only transcript item so the pick survives reload at the right
    position (BEFORE the turn's assistant output); the relay also
    re-publishes a live event carrying the persisted item id so the live
    chip and a turn-start snapshot refetch dedup by the same id (no
    double render).

    Returns ``None`` for every other event, and for a malformed routing
    item (empty model) so a bad frame can't poison the relay.

    :param event: Parsed SSE event dict from the runner stream.
    :returns: A ``routing_decision`` :class:`NewConversationItem`, or
        ``None``.
    """
    if event.get("type") != "response.output_item.done":
        return None
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "routing_decision":
        return None
    try:
        data = parse_item_data("routing_decision", item)
    except (ValueError, TypeError):
        _logger.warning("Failed to parse routing_decision item from SSE")
        return None
    # No turn response_id exists yet (emitted before response.in_progress),
    # so stamp a fresh routing id — the chip renders as its own standalone
    # line at turn start.
    import uuid

    return NewConversationItem(
        type="routing_decision",
        response_id=f"routing_{uuid.uuid4().hex}",
        data=data,
    )


def _error_item_from_sse(
    event: dict[str, Any],
    response_id: str | None = None,
) -> NewConversationItem | None:
    """
    Build a durable ``error`` item from a runner error SSE event.

    The web UI already renders live ``response.error`` and
    ``response.failed`` error payloads as real error banners. This
    helper mirrors turn-scoped payloads into conversation history so the
    banner survives refresh/reconnect.

    A bare ``response.error`` emitted before ``response.in_progress`` is
    a session/startup signal, not a transcript turn. Leaving it live-only
    avoids creating an orphan banner at the top of the transcript; when
    a user sends a message into the failed native terminal, the AP-side
    fast-fail path records that user item and its sibling error in order.

    :param event: Parsed runner SSE event.
    :param response_id: Current response id, e.g. ``"resp_abc123"``.
        ``None`` means no turn is active.
    :returns: A ``type="error"`` item, or ``None`` when the event has
        no structured error payload or is not tied to a turn.
    """
    evt_type = event.get("type")
    raw_error: Any
    source = event.get("source")
    if evt_type == "response.error":
        if response_id is None:
            return None
        raw_error = event.get("error")
    elif evt_type == "response.failed":
        raw_response = event.get("response")
        raw_error = raw_response.get("error") if isinstance(raw_response, dict) else None
        if raw_error is None:
            raw_error = event.get("error")
        source = "execution"
        if response_id is None and isinstance(raw_response, dict):
            raw_response_id = raw_response.get("id")
            if isinstance(raw_response_id, str) and raw_response_id:
                response_id = raw_response_id
    else:
        return None
    if response_id is None:
        return None
    if not isinstance(raw_error, dict):
        return None
    raw_code = raw_error.get("code")
    raw_message = raw_error.get("message")
    if not isinstance(raw_code, str) or not raw_code.strip():
        return None
    if not isinstance(raw_message, str) or not raw_message.strip():
        return None
    if source not in ("llm", "execution", "tool"):
        return None
    return NewConversationItem(
        type="error",
        response_id=response_id,
        data=ErrorData(
            source=source,
            code=raw_code,
            message=raw_message,
        ),
    )


async def _relay_persist_error_once(
    conversation_store: ConversationStore | None,
    session_id: str,
    item: NewConversationItem,
) -> Literal["persisted", "duplicate", "skipped", "failed"]:
    """
    Persist a runner error item unless the same error already exists.

    Native terminal startup can fail again on every runner reconnect.
    Dedupe by the visible payload ``(source, code, message)`` only
    when no user message has appeared since the matching error. That
    suppresses reconnect spam while still recording a new error for a
    user-initiated retry against the same broken terminal.

    :param conversation_store: Store instance, or ``None`` to skip.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param item: The candidate ``type="error"`` item.
    :returns: ``"persisted"`` if this call appended the item,
        ``"duplicate"`` if a matching recent error already exists,
        ``"skipped"`` if no store or non-error item was provided, or
        ``"failed"`` if the store operation failed.
    """
    if conversation_store is None:
        return "skipped"
    if not isinstance(item.data, ErrorData):
        return "skipped"
    try:
        recent = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            limit=20,
            order="desc",
        )
        for existing in recent.data:
            if (
                existing.type == "message"
                and isinstance(existing.data, MessageData)
                and existing.data.role == "user"
            ):
                break
            if existing.type != "error" or not isinstance(existing.data, ErrorData):
                continue
            if (
                existing.data.source == item.data.source
                and existing.data.code == item.data.code
                and existing.data.message == item.data.message
            ):
                return "duplicate"
        await asyncio.to_thread(
            conversation_store.append,
            session_id,
            [item],
        )
        return "persisted"
    except Exception:
        _logger.exception(
            "Relay error persist failed for session=%s",
            session_id,
        )
        return "failed"


async def _relay_persist(
    conversation_store: ConversationStore | None,
    session_id: str,
    item: NewConversationItem,
) -> None:
    """
    Persist a single conversation item from the relay.

    :param conversation_store: Store instance, or ``None`` to skip.
    :param session_id: Session/conversation identifier.
    :param item: The item to persist.
    """
    if conversation_store is None:
        return
    try:
        await asyncio.to_thread(
            conversation_store.append,
            session_id,
            [item],
        )
    except Exception:
        _logger.exception(
            "Relay persist failed for session=%s",
            session_id,
        )


async def _flush_relay_text(
    conversation_store: ConversationStore | None,
    session_id: str,
    text_acc: list[str],
    response_id: str | None,
    model_id: str | None,
) -> None:
    """
    Persist buffered assistant text as a message item and clear the buffer.

    Scaffold harnesses (claude-sdk) stream text deltas with no per-message
    ``output_item.done``, so the relay buffers them. Flushing at each
    text→function_call boundary (not only at ``response.completed``) keeps
    the persisted transcript interleaved — ``[text, tool, text, tool]`` —
    instead of collapsing a turn's narration into one block after its tool
    calls (which renders tools-above-text + run-on text on reload).

    After a confirmed persist the item is also published to the live
    stream as ``response.output_item.done`` (mirroring the native path's
    :func:`_publish_external_conversation_item`). Live clients already
    rendered the text from the deltas; the publish delivers the
    store-assigned item id so they can stamp it onto the streamed block.
    Without it the rendered block stays id-less and every reconnect's
    itemId-keyed reconciliation splices the persisted copy in as a
    duplicate. Clients must dedupe this event by CONTENT, not by
    open-section state: at a mid-turn tool-call boundary the streamed
    text has already been closed/committed client-side (by the
    function_call item or interleaved reasoning) before this publish
    arrives. The web stamps the id onto the matching streamed
    ``text_done`` block in place (web ``chatStore.ts``
    ``pumpStreamEvents``); the TUI consumes a byte-equal committed
    segment (``_repl.py`` ``_TurnProseTracker``).

    The buffer and the in-flight replay are cleared ONLY after the append
    is confirmed: clearing first would let a reconnect during the persist
    ``await`` see neither the (not-yet-committed) message nor the replay,
    dropping the narration — and a swallowed append failure would lose it
    permanently. On failure the buffers are left intact so the text still
    replays and is retried at the next flush / ``response.completed``.

    :param conversation_store: Store to append to, or ``None`` to skip
        persistence (test parsing path).
    :param session_id: Conversation/session id, e.g. ``"conv_abc123"``.
    :param text_acc: Accumulated delta strings; cleared in place on success.
    :param response_id: Turn id so the segment groups with its tool calls.
    :param model_id: Assistant agent label for the message.
    """
    if not text_acc:
        return
    text = "".join(text_acc)
    if not text.strip():
        # Whitespace-only: nothing worth persisting. Drop it so it neither
        # accumulates into the next segment nor replays as an empty bubble.
        text_acc.clear()
        inflight_text.reset_text(session_id)
        return
    if conversation_store is None:
        text_acc.clear()
        return
    import uuid

    try:
        item = NewConversationItem(
            type="message",
            response_id=response_id or f"turn_{uuid.uuid4().hex}",
            data=parse_item_data(
                "message",
                {
                    "type": "message",
                    "role": "assistant",
                    "agent": model_id or "unknown",
                    "content": [{"type": "output_text", "text": text}],
                },
            ),
        )
        persisted = await asyncio.to_thread(conversation_store.append, session_id, [item])
    except Exception:
        # Keep text_acc + the in-flight buffer so the narration isn't lost:
        # it still replays on reconnect and is retried at the next flush.
        _logger.exception(
            "Relay: failed to persist assistant text segment for session=%s",
            session_id,
        )
        return
    # Confirmed persisted — now safe to clear. Synchronous (no await before
    # the next yield), so no reconnect observes the committed message and a
    # stale replay together.
    text_acc.clear()
    inflight_text.reset_text(session_id)
    # Publish the persisted item so live clients learn its store-assigned
    # id and stamp it onto the already-rendered streamed text (see the
    # docstring). Ordered before the boundary item / terminal event the
    # caller publishes next; clients match it back to the streamed text
    # by byte-equal content, not by open-section state.
    done_event = OutputItemDoneEvent(
        type="response.output_item.done",
        item=persisted[0].to_api_dict(),
    )
    session_stream.publish(session_id, done_event.model_dump())


def _compact_lock(session_id: str) -> asyncio.Lock:
    """
    Return the lock serializing explicit compaction for one session.

    Call-time proxy to the facade so a test's ``monkeypatch.setattr`` of
    this name on ``sessions`` is honored by sibling impl callers.
    """
    from omnigent.server.routes import sessions as _facade

    return _facade._compact_lock(session_id)


def _compact_lock_impl(session_id: str) -> asyncio.Lock:
    """
    Return the lock serializing explicit compaction for one session.

    Concurrent ``/compact`` events for the same session must not overlap;
    different sessions get distinct locks so they may compact concurrently.
    Get-or-create is race-free because there is no ``await`` between the
    lookup and the insert (single event loop).

    :param session_id: Session/conversation id being compacted.
    :returns: A process-wide :class:`asyncio.Lock` shared by every concurrent
        caller for the same ``session_id``.
    """
    lock = _COMPACT_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _COMPACT_LOCKS[session_id] = lock
    return lock


async def _run_compact_locked(
    session_id: str,
    conv: Conversation,
    agent_store: AgentStore,
    agent_cache: AgentCache | None,
) -> None:
    """
    Run explicit compaction while holding the per-session compact lock.

    :param session_id: Session/conversation identifier.
    :param conv: Conversation row.
    :param agent_store: Agent store for spec lookup.
    :param agent_cache: Agent cache for bundle loading.
    """
    lock = _compact_lock(session_id)
    async with lock:
        if conv.agent_id is None:
            raise OmnigentError("Session has no agent binding", code=ErrorCode.INTERNAL_ERROR)
        if agent_cache is None:
            raise OmnigentError(
                "Compaction is unavailable: agent cache is not configured",
                code=ErrorCode.INTERNAL_ERROR,
            )
        # Recheck after acquiring — a turn may have started while waiting.
        if _session_status_cache.get(session_id) in ("running", "waiting"):
            raise OmnigentError(
                "Cannot compact while a turn is running; cancel or wait for it to finish first",
                code=ErrorCode.CONFLICT,
            )
        agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
        if agent is None or agent.bundle_location is None:
            raise OmnigentError(
                f"Agent not found: {conv.agent_id!r}",
                code=ErrorCode.NOT_FOUND,
            )
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
        spec = loaded.spec
        if spec.llm is not None:
            llm_config = spec.llm
        elif spec.executor.model is not None:
            from omnigent.spec.types import LLMConfig

            llm_config = LLMConfig(model=spec.executor.model, connection=spec.executor.connection)
        else:
            harness = spec.executor.harness_kind
            raise OmnigentError(
                f"/compact is unavailable for this {harness} session because the agent "
                "does not declare an LLM model for server-side compaction. Configure "
                "`llm.model` or `executor.model`, or use a harness-native compaction "
                "control when one is available.",
                code=ErrorCode.INVALID_INPUT,
            )
        task_id = f"compact_{int(time.time() * 1000)}"
        _publish_status(session_id, "running")
        # compact() publishes its own in_progress / completed SSE events
        # when conversation_id is set — don't double-publish here.
        from omnigent.runtime.workflow import compact_conversation_now

        try:
            await compact_conversation_now(
                task_id=task_id,
                conversation_id=session_id,
                spec=spec,
                llm_config=llm_config,
                tool_schemas=[],
                preserve_recent_window=1,
            )
        except Exception as exc:
            _logger.exception("Explicit session compaction failed for %s", session_id)
            detail = str(exc) or repr(exc)
            _publish_compaction_failed(session_id)
            _publish_status(session_id, "idle")
            raise OmnigentError(
                f"Compaction failed while generating a summary: {detail}",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc
        _publish_status(session_id, "idle")


def _agent_provider_family(agent: Agent) -> str | None:
    """Return the provider family of an agent's harness, or ``None``.

    Loads the agent's spec to read its ``harness_kind`` and maps it to a
    provider family (``"anthropic"`` / ``"openai"``). Returns ``None`` when
    the bundle can't be loaded or the harness is unknown — callers treat
    ``None`` as "can't confirm same family".

    :param agent: The agent whose harness family to resolve.
    :returns: ``"anthropic"`` / ``"openai"``, else ``None``.
    """
    from omnigent.onboarding.provider_config import provider_family_for_harness

    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001 — unloadable bundle → unknown family
        return None
    return provider_family_for_harness(spec.executor.harness_kind)


def _same_provider_family(a: Agent, b: Agent) -> bool:
    """Return whether two agents share a (known) provider family.

    ``False`` when either family is undeterminable, so a fork that can't
    confirm both agents speak the same provider resets model settings and
    skips resuming the source's native session (the runner rebuilds the
    native transcript from Omnigent items instead).

    :param a: First agent (e.g. the fork source's agent).
    :param b: Second agent (e.g. the switch target).
    :returns: ``True`` when both resolve to the same non-``None`` family.
    """
    family_a = _agent_provider_family(a)
    return family_a is not None and family_a == _agent_provider_family(b)


def _agent_is_native(agent: Agent) -> bool:
    """Return whether an agent runs a native CLI harness.

    Loads the agent's spec to read its ``harness_kind``. Native targets run
    a vendor TUI in a terminal (claude-native / codex-native / pi-native /
    cursor-native). This is broader than "can replay fork history" — every
    native harness except cursor-native carries the session-file-rebuild path;
    use ``_agent_carries_native_fork_history`` for that narrower gate. Returns
    ``False`` when the bundle can't be loaded (treated as non-native).

    :param agent: The agent whose harness to classify.
    :returns: ``True`` for a native CLI harness, else ``False``.
    """
    from omnigent.harness_aliases import is_native_harness

    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001 — unloadable bundle → treat as non-native
        return False
    return is_native_harness(spec.executor.harness_kind)


def _agent_carries_native_fork_history(agent: Agent) -> bool:
    """Return whether *agent*'s native harness rebuilds a fork's transcript.

    claude-native / codex-native / pi-native each record a resumable native
    session file that the runner rebuilds from the copied Omnigent items on
    fork/resume, so a fork bound to one of them carries prior history into the
    native CLI. Used by both fork and switch-agent. cursor-native is a native
    CLI but has no resumable session file to rebuild; it carries fork history a
    different way (a text preamble, fork-only — see
    :func:`_agent_carries_cursor_fork_history`), so stamping
    ``carry_history_into_native`` for it here would be a false promise. Returns
    ``False`` when the bundle can't be loaded (treated as non-carrying).

    :param agent: The agent whose harness to classify.
    :returns: ``True`` only for transcript-rebuild native harnesses.
    """
    from omnigent.harness_aliases import canonicalize_harness

    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001 — unloadable bundle → treat as non-carrying
        return False
    return canonicalize_harness(spec.executor.harness_kind) in _FORK_HISTORY_NATIVE_HARNESSES


def _agent_carries_cursor_fork_history(agent: Agent) -> bool:
    """Return whether *agent*'s native harness carries FORK history via preamble.

    Cursor's conversation is server-backed and opencode has no history-import
    API, so neither can seed a local store for a rebuilt resume; instead the
    runner replays prior turns as a text preamble on the fork (cursor: the
    first message; opencode: a ``noReply`` context message). Fork-only —
    switch-agent does not call this, so switching into one still launches fresh.
    Returns ``False`` when the bundle can't be loaded.

    :param agent: The agent whose harness to classify.
    :returns: ``True`` for the cursor-native / opencode-native harnesses.
    """
    from omnigent.harness_aliases import canonicalize_harness

    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001 — unloadable bundle → treat as non-carrying
        return False
    return canonicalize_harness(spec.executor.harness_kind) in _CURSOR_FORK_HISTORY_HARNESSES


def _native_coding_agent_for_agent(agent: Agent) -> NativeCodingAgent | None:
    """
    Return native coding-agent metadata for an agent's harness.

    :param agent: The agent whose bundle should be inspected.
    :returns: Registry metadata for the native TUI harness, or ``None``.
    """
    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001 — unloadable bundle → non-native presentation
        return None
    return native_coding_agent_for_harness(spec.executor.harness_kind)


def _presentation_labels_for_agent(agent: Agent) -> dict[str, str]:
    """Return the Web UI presentation labels for an agent's harness.

    A native-CLI agent runs **terminal-first** (the inline terminal is the
    main view), gated on ``omnigent.ui == "terminal"`` plus the matching
    ``omnigent.wrapper`` value; an SDK agent runs as plain chat (no such
    labels). Used by the fork route so a switched clone's UI mode matches
    the TARGET harness instead of inheriting the source's — otherwise an SDK
    clone of a claude-native session renders a stale interactive terminal.

    :param agent: The agent the fork will bind.
    :returns: ``{ui: terminal, wrapper: <value>}`` for a native agent, or
        ``{}`` for an SDK agent / undeterminable family (chat mode).
    """
    native_agent = _native_coding_agent_for_agent(agent)
    return native_agent.presentation_labels if native_agent is not None else {}


def _load_agent_spec_for_session(*args: Any, **kwargs: Any) -> AgentSpec | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return _facade._load_agent_spec_for_session(*args, **kwargs)


def _load_agent_spec_for_session_impl(
    conv: Conversation,
    agent_store: AgentStore,
) -> AgentSpec | None:
    # Split from _build_policy_engine_from_spec so the caller can run the
    # cheap guardrails/default-policy skip check between the two and avoid
    # paying for engine construction when no policy could fire. Both halves
    # are blocking DB/IO, so each is run under asyncio.to_thread.
    if conv.agent_id is None:
        return None
    agent = agent_store.get(conv.agent_id)
    if agent is None:
        return None
    return (
        get_agent_cache()
        .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
        .spec
    )


def _build_policy_engine_from_spec(*args: Any, **kwargs: Any) -> PolicyEngine:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return _facade._build_policy_engine_from_spec(*args, **kwargs)


def _build_policy_engine_from_spec_impl(
    spec: AgentSpec,
    session_id: str,
    conversation_store: ConversationStore,
) -> PolicyEngine:
    caps = get_caps()
    host_connection = (
        caps.policy_llm_connection_factory() if caps.policy_llm_connection_factory else None
    )
    return build_policy_engine(
        spec=spec,
        conversation_id=session_id,
        conversation_store=conversation_store,
        default_policies=caps.default_policies,
        policy_store=get_policy_store(),
        server_llm=caps.llm,
        host_connection=host_connection,
    )


async def _apply_pending_policy_ask_writes(
    session_id: str,
    conv: Conversation,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    data: dict[str, Any],
) -> None:
    """
    Apply (or drop) policy writes stashed for a relay tool-call ASK.

    Called when an ``approval`` verdict resolves a runner-owned policy
    elicitation (both approval entry points — the ``approval`` event and the
    resolve URL — route here via their callers). On ``accept`` the deciding
    policy's stashed ``state_updates`` / ``set_labels`` are persisted by a
    freshly built engine — exactly what the native ``_hold_native_ask_gate``
    path does inline. On any other verdict (decline / cancel / missing) they
    are dropped (POLICIES.md §7.2: a denied ASK leaves no trace). No-op when
    the elicitation has no stashed writes (the common case — most ASKs and
    all non-policy elicitations).

    :param session_id: Session id that owns the elicitation, e.g.
        ``"conv_abc123"``.
    :param conv: The session conversation, for the agent / spec lookup.
    :param conversation_store: Store the engine persists session state to.
    :param agent_store: Store for the agent spec lookup.
    :param data: The approval payload, carrying ``elicitation_id`` and the
        verdict ``action`` (e.g. ``{"elicitation_id": "elicit_x",
        "action": "accept"}``).
    :returns: None.
    """
    elicitation_id = data.get("elicitation_id", "")
    pending = _pending_policy_ask_writes.get(elicitation_id)
    if pending is None:
        return
    if data.get("action") != "accept":
        # Declined — remove the stashed writes (POLICIES.md §7.2:
        # a denied ASK leaves no trace).
        _pending_policy_ask_writes.pop(elicitation_id, None)
        return
    if pending.from_mcp:
        # MCP entries: the retry path (POST /mcp with requestState)
        # pops and applies the writes itself. Applying here too would
        # double-apply non-idempotent ops (e.g. INCREMENT state
        # updates for cost-budget counters). Leave the entry for the
        # retry path; it owns cleanup.
        return
    # Non-MCP relay path: pop and apply writes here since no retry
    # will arrive.
    _pending_policy_ask_writes.pop(elicitation_id, None)
    # Resolve the agent spec + build the engine off the event loop: the
    # lookup, cold-cache bundle fetch, and engine construction are all
    # blocking DB/IO.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return
    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )
    # The label/state writes hit the DB synchronously too — keep them
    # off the loop.
    if pending.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, pending.set_labels)
    if pending.state_updates:
        await asyncio.to_thread(engine.apply_state_updates, pending.state_updates)


def _build_actor(user_id: str | None) -> dict[str, str] | None:
    """
    Build the ``actor`` dict for :class:`EvaluationContext`.

    Returns ``{"run_as": user_id}`` when the authenticated user is
    known, ``None`` otherwise (tests, legacy callers without auth).

    :param user_id: Authenticated user email from the request,
        e.g. ``"alice@example.com"``. ``None`` when auth is
        disabled or the caller is unauthenticated.
    :returns: Actor dict or ``None``.
    """
    if user_id is None:
        return None
    return {"run_as": user_id}


def _build_evaluation_context(
    phase: Phase,
    data: dict[str, Any] | str,
    event: dict[str, Any],
    *,
    actor: dict[str, str] | None = None,
) -> EvaluationContext:
    """
    Build an :class:`EvaluationContext` from a proto-style event dict.

    Maps the proto ``Event.data`` shape to the internal convention:

    - ``TOOL_CALL``: ``content = {"name": name, "arguments": args}``,
      ``tool_name = name``.
    - ``TOOL_RESULT``: ``content = {"result": result_str}``,
      ``tool_name`` from ``request_data.name``,
      ``request_data`` from the event's ``request_data`` field.
    - ``REQUEST`` / ``RESPONSE``: ``content = str(data)``.

    :param phase: Internal phase enum.
    :param data: ``event.data`` dict from the proto request.
    :param event: Full event dict (for ``request_data``, ``context``).
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: Ready-to-evaluate context.
    """
    # A native hook may stamp the session's live model into the event context
    # (e.g. the codex hook reads it from ``config.toml`` at gate time — the
    # source of truth for an in-TUI ``/model`` selection). When present, this
    # wins over the engine's server-resolved model (see
    # ``PolicyEngine._inject_model``); ``None`` falls back to that resolution.
    raw_context = event.get("context") or {}
    supplied_model = raw_context.get("model")
    hook_model = supplied_model if isinstance(supplied_model, str) and supplied_model else None
    # The harness, when a native hook stamped it (e.g. the codex hook), so
    # policies can tailor messages to the session's model-switch surface
    # (codex-native is terminal-only). Carried through unchanged — the engine
    # neither resolves nor overrides it.
    supplied_harness = raw_context.get("harness")
    hook_harness = (
        supplied_harness if isinstance(supplied_harness, str) and supplied_harness else None
    )
    if phase == Phase.TOOL_CALL:
        tool_name = data.get("name") or ""
        args = data.get("arguments") or {}
        return EvaluationContext(
            phase=phase,
            content={"name": tool_name, "arguments": args},
            tool_name=tool_name or None,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    if phase == Phase.TOOL_RESULT:
        tool_result = data.get("result", "")
        request_data = event.get("request_data")
        tool_name = None
        if isinstance(request_data, dict):
            tool_name = request_data.get("name")
        return EvaluationContext(
            phase=phase,
            content={
                "result": tool_result if isinstance(tool_result, str) else json.dumps(tool_result),
            },
            tool_name=tool_name,
            request_data=request_data,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    # LLM_REQUEST / LLM_RESPONSE — content is the full request/response dict.
    if phase in (Phase.LLM_REQUEST, Phase.LLM_RESPONSE):
        return EvaluationContext(
            phase=phase,
            content=data,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    # REQUEST / RESPONSE — content is the user/assistant text. The wire ``data``
    # is a dict for the native command hooks (``{"text"|"content": ...}``), but
    # may be a bare string — opencode's policy plugin sends the prompt text
    # directly for ``PHASE_REQUEST``. Accept both, and NEVER raise here: a crash
    # 500s the evaluate endpoint, which silently fails the request/result gate
    # OPEN (the exact symptom that let cost-over-budget terminal prompts through).
    if isinstance(data, str):
        text = data
    elif isinstance(data, dict):
        text = data.get("text") or data.get("content") or str(data)
    else:
        text = str(data)
    return EvaluationContext(
        phase=phase,
        content=text if isinstance(text, str) else json.dumps(text),
        actor=actor,
        model=hook_model,
        harness=hook_harness,
    )


def _extract_user_text_from_event(body: SessionEventInput) -> str:
    """
    Extract concatenated text from a user message event body.

    Mirrors the logic in ``workflow._extract_user_text`` but
    operates on the raw ``SessionEventInput.data`` dict rather
    than a parsed ``MessageData`` object.

    :param body: The validated ``message`` event with
        ``role: "user"``.
    :returns: Joined text from ``input_text`` / ``text`` content
        blocks. Empty string if no text blocks found.
    """
    content = body.data.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text") or block.get("input_text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _publish_policy_deny(session_id: str, reason: str) -> None:
    """
    Publish the ``[Denied by policy: ...]`` sentinel on the session stream.

    The sentinel text is a load-bearing contract (the REPL renders it, e2e
    tests assert it, and native harnesses relay it to the model), so it is
    always carried in a ``response.output_text.delta``.

    Input DENY callers also persist the same sentinel as an assistant
    conversation item. This stream publish remains separate so live clients
    still get immediate feedback before the handler returns. Stamping a unique
    ``message_id`` (matching how live streaming text is tagged) routes the
    delta through the web's live-preview path, where it folds into a single
    ``live:<id>`` block rather than a response-scoped stray bubble.

    Safe for the other consumers: the REPL converts any ``output_text.delta``
    to a ``TextDelta`` regardless of ``message_id``; the ``/v1/responses`` API
    surfaces the deny via input-deny synthesis (not session-stream deltas);
    and the only ``message_id``-gated accumulator (``_relay_runner_stream``)
    reads runner-relayed deltas, never this server-published one.

    :param session_id: Session/conversation identifier.
    :param reason: Human-readable deny reason from the policy verdict.
    """
    session_stream.publish(
        session_id,
        {
            "type": "response.output_text.delta",
            "delta": f"[Denied by policy: {reason}]",
            # Unique per deny so two separate denials don't fold into one
            # block; a single delta carries the whole sentinel, so index 0.
            "message_id": f"deny_{secrets.token_hex(8)}",
            "index": 0,
        },
    )


def _publish_input_deny_terminal(session_id: str, conv: Conversation, reason: str) -> None:
    """
    Publish a terminal ``response.completed`` for an INPUT-phase DENY.

    The short-circuit never forwards to a runner, so no runner-relayed
    terminal ``response.*`` event is emitted. SSE consumers that drive a
    turn off the live-tail (the headless ``-p`` client,
    :class:`omnigent_client.SessionsChat.send`) iterate until a
    turn-terminal event arrives and would otherwise block forever. The
    output carries the same sentinel text so the terminal-snapshot fallback
    also surfaces the deny.

    :param session_id: Session/conversation identifier.
    :param conv: Conversation whose agent/model name tags the response.
    :param reason: Human-readable deny reason from the policy verdict.
    """
    sentinel = f"{_DENY_SENTINEL_PREFIX}{reason}]"
    response = ResponseObject(
        id=f"deny_{secrets.token_hex(8)}",
        status="completed",
        model=conv.agent_id or "policy",
        created_at=int(time.time()),
        completed_at=int(time.time()),
        output=[
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": sentinel}],
            }
        ],
    )
    session_stream.publish(
        session_id,
        CompletedEvent(type="response.completed", response=response).model_dump(exclude_none=True),
    )


async def _persist_policy_deny_sentinel(
    session_id: str,
    conv: Conversation,
    reason: str,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
) -> None:
    """
    Persist the ``[Denied by policy: ...]`` sentinel as assistant history.

    INPUT policy DENY returns synchronously and never forwards the user turn
    to a runner, so no downstream stream relay can append the assistant-side
    deny marker. Persisting the same assistant message shape used by OUTPUT
    policy DENY keeps follow-up turns and the items API consistent with the
    streamed deny users already see.

    After persisting, publish the committed item as a
    ``response.output_item.done`` — the same commit event a streamed
    assistant message emits (see :func:`_flush_relay_text`). Without it the
    live deny only exists as the ``_publish_policy_deny`` sentinel delta,
    which the web folds into a provisional ``live:`` preview block that the
    terminal ``response.completed`` sweeps; the deny then reappeared only
    after a refresh re-hydrated the persisted item. Emitting the commit event
    lets the web reconcile the preview into a durable, itemId-keyed block that
    survives the sweep, a reconnect, and a refresh alike.

    :param session_id: Session/conversation identifier.
    :param conv: Conversation whose agent/model name tags the message.
    :param reason: Human-readable deny reason from the policy verdict.
    :param conversation_store: Store for item persistence.
    :param agent_store: Store used to resolve the agent's display name.
    """
    import uuid

    sentinel = f"{_DENY_SENTINEL_PREFIX}{reason}]"
    agent = agent_store.get(conv.agent_id) if conv.agent_id else None
    agent_name = agent.name if agent is not None else conv.agent_id or "policy"
    item = NewConversationItem(
        type="message",
        response_id=f"deny_{uuid.uuid4().hex}",
        data=parse_item_data(
            "message",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": sentinel}],
                "agent": agent_name,
            },
        ),
    )
    persisted = await asyncio.to_thread(conversation_store.append, session_id, [item])
    if persisted:
        done_event = OutputItemDoneEvent(
            type="response.output_item.done",
            item=persisted[0].to_api_dict(),
        )
        session_stream.publish(session_id, done_event.model_dump())


def _extract_assistant_text_from_event(body: SessionEventInput) -> str:
    """
    Extract concatenated text from an assistant message event.

    Mirrors :func:`_extract_user_text_from_event` but for
    assistant messages. Content blocks use ``"text"`` (not
    ``"input_text"``).

    :param body: The validated ``message`` event with
        ``role: "assistant"``.
    :returns: Joined text from content blocks. Empty string if
        no text blocks found.
    """
    content = body.data.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _replace_text_in_message_body(
    body: SessionEventInput,
    replacement: str,
) -> SessionEventInput:
    """
    Return a copy of the message body with all text content
    blocks replaced by *replacement*.

    Used by OUTPUT policy DENY to substitute the deny sentinel
    into the persisted message while preserving non-text content
    blocks (images, etc.) and all other body fields.

    :param body: The original assistant message event.
    :param replacement: The deny sentinel text,
        e.g. ``"[Denied by policy: harmful content]"``.
    :returns: A new body with text blocks replaced.
    """
    content = body.data.get("content") or []
    new_content: list[dict[str, Any]] = []
    replaced = False
    for block in content:
        if isinstance(block, dict) and "text" in block:
            if not replaced:
                new_content.append({"type": "output_text", "text": replacement})
                replaced = True
        else:
            new_content.append(block)
    if not replaced:
        new_content.append({"type": "output_text", "text": replacement})
    new_data = {**body.data, "content": new_content}
    return type(body)(type=body.type, data=new_data)


async def _evaluate_output_policy(
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
    Evaluate an assistant message against OUTPUT phase policies.

    Pure evaluation — does NOT persist the event. Returns
    ``None`` on ALLOW. On DENY, returns a verdict dict with
    ``_denied_body`` — the caller should persist this modified
    body (text replaced with deny sentinel) instead of the
    original.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``message`` event.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW (fall through). Verdict dict
        with ``_denied_body`` on DENY.
    """

    assistant_text = _extract_assistant_text_from_event(body)
    if not assistant_text:
        return None

    # Resolve the agent spec off the event loop (blocking DB + cold-cache
    # bundle fetch). Spec only, so the cheap skip check below runs before
    # the more expensive engine build.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    if not spec.guardrails and not get_caps().default_policies and get_policy_store() is None:
        return None

    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )
    ctx = EvaluationContext(
        phase=Phase.RESPONSE,
        content=assistant_text,
        tool_name=None,
        actor=actor,
    )
    result = await engine.evaluate(ctx)

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    # DENY — build the denied body with sentinel text.
    # The caller persists this modified body instead of the
    # original (Option B).
    if result.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
    reason = result.reason or "Denied by policy"
    sentinel = f"{_DENY_SENTINEL_PREFIX}{reason}]"
    denied_body = _replace_text_in_message_body(body, sentinel)
    return {
        "verdict": "deny",
        "reason": reason,
        "_denied_body": denied_body,
    }


async def _stream_live_events(
    request: Request,
    session_id: str,
    on_subscribed: Callable[[], Awaitable[Iterable[dict[str, Any]]]] | None = None,
    viewer_user_id: str | None = None,
    viewer_idle: bool = False,
    presence_root_id: str | None = None,
) -> AsyncIterator[str]:
    """
    Yield SSE-formatted events from the conversation's live stream.

    Events are delivered live from the moment :func:`session_stream.subscribe`
    is invoked forward — there is no buffer and no replay. Events
    published before this generator subscribed are lost; clients
    reconcile pre-subscribe state via the snapshot endpoint
    (``GET /v1/sessions/{id}``) and dedupe by item id.

    On normal completion (subscribe ends or the disconnect check
    breaks the loop) this generator emits a ``[DONE]`` sentinel so
    well-behaved SSE consumers see a clean stream termination. A
    subscriber-queue overflow instead ends without ``[DONE]`` so clients
    treat it as a dropped transport, reconnect, and reconcile from the
    persisted snapshot.

    ``finally`` is cleanup-only (presence deregistration): yielding
    from ``finally`` during client ``aclose`` / ``GeneratorExit``
    raises ``RuntimeError: async generator ignored GeneratorExit``.
    The subscribe iterator is wrapped in ``contextlib.aclosing`` so
    outer ``aclose`` tears down the pub-sub subscriber slot
    immediately (a bare ``async for`` would defer that to GC).

    Each emitted dict is validated against
    :data:`ServerStreamEvent` at the wire boundary so a runtime
    that publishes an unmodelled ``type`` fails loud rather than
    serializing an unknown event verbatim.

    The subscribe call passes a ``ready_event`` heartbeat plus
    ``heartbeat_interval_s``. The ready heartbeat is yielded
    immediately after the live-tail subscriber slot is registered,
    before any snapshot hook runs, so clients can wait for a
    concrete subscription acknowledgment before posting a fast
    one-shot turn. The interval heartbeat keeps an idle stream
    emitting ``session.heartbeat`` events on a fixed cadence (see
    :data:`_SESSION_STREAM_HEARTBEAT_INTERVAL_S`). Without that,
    a stream that sits between turns has nothing crossing the wire;
    the client's SSE read-timeout and this route's
    ``request.is_disconnected()`` check (only polled on event
    arrival) both lag for minutes after a half-open socket forms
    (e.g. after a laptop sleep). The heartbeat gives both sides a
    regular byte to fire against.

    :param request: The FastAPI request, used to detect disconnect.
    :param session_id: Session/conversation identifier whose stream
        to subscribe to, e.g. ``"conv_abc123"``.
    :param on_subscribed: Optional snapshot-on-connect hook forwarded to
        :func:`session_stream.subscribe`; its events are yielded ahead of
        the live tail so a fresh client sees current resource state
        without polling. ``None`` (default) keeps the pure live-tail
        shape used by callers that reconcile via the snapshot endpoint.
    :param viewer_user_id: Authenticated identity to register in the
        session's presence registry for this stream's lifetime, e.g.
        ``"alice@example.com"``. ``None`` (default, and the reserved
        single-user sentinel mapped via ``attribution_user``) skips
        presence tracking entirely.
    :param viewer_idle: The viewer's connect-time idle flag (tab
        backgrounded), from the route's ``idle`` query param. Ignored
        when *viewer_user_id* is ``None``.
    :param presence_root_id: Root conversation of the streamed
        session's tree (its ``root_conversation_id``), e.g.
        ``"conv_root123"``. Presence is scoped to the tree's root so
        viewers of different agents/sub-agents in one session see
        each other. Required when *viewer_user_id* is set; ignored
        otherwise.
    :returns: An async iterator of SSE message strings.
    :raises ValueError: If *viewer_user_id* is set without
        *presence_root_id* — a per-conversation presence scope would
        silently split a session's viewers per agent.
    """
    # Presence registers before the subscribe loop: the join broadcast
    # fans out to ALREADY-subscribed co-viewers, while this stream
    # learns the full list (self included) from the snapshot-on-connect
    # presence event — full-state events make that ordering race benign.
    presence_token: str | None = None
    if viewer_user_id is not None:
        if presence_root_id is None:
            raise ValueError("presence_root_id is required when viewer_user_id is set")
        presence_token = presence.connect(
            presence_root_id, session_id, viewer_user_id, viewer_idle
        )
    try:
        # ``aclosing`` propagates outer ``aclose`` into ``subscribe``;
        # a bare ``async for`` would leave the subscriber slot until GC.
        async with contextlib.aclosing(
            session_stream.subscribe(
                session_id,
                heartbeat_interval_s=_SESSION_STREAM_HEARTBEAT_INTERVAL_S,
                ready_event={"type": "session.heartbeat"},
                # In-flight text replay must be captured synchronously at slot
                # registration (before ``ready_event`` suspends), not in the
                # async ``on_subscribed`` hook, or window deltas double-render.
                # Resource state stays in ``on_subscribed`` — it needs
                # awaits and is not dedup-sensitive.
                pre_ready_snapshot=lambda: inflight_text.snapshot_for(session_id),
                on_subscribed=on_subscribed,
            )
        ) as live_events:
            async for event in live_events:
                if await request.is_disconnected():
                    break
                event_type = event.get("type")
                if not isinstance(event_type, str):
                    raise ValueError(
                        f"session stream event missing string ``type`` field: {event!r}",
                    )
                validated = _SERVER_STREAM_EVENT_ADAPTER.validate_python(event)
                yield _format_sse(event_type, validated.model_dump())
    except session_stream.SubscriberOverflowError:
        _logger.warning(
            "session stream subscriber overflowed for %s; closing for snapshot reconnect",
            session_id,
        )
    else:
        # Normal completion only — never yield from ``finally`` (aclose /
        # GeneratorExit would raise ``async generator ignored GeneratorExit``).
        yield "data: [DONE]\n\n"
    finally:
        # The non-None checks besides presence_token's are type
        # narrowing only: a minted token implies both were set above.
        if (
            presence_token is not None
            and viewer_user_id is not None
            and presence_root_id is not None
        ):
            presence.disconnect(presence_root_id, viewer_user_id, presence_token)


def _validate_terminal_launch_args(value: list[str] | None) -> list[str] | None:
    """
    Validate per-session native-terminal pass-through args.

    Enforces a flat list of strings within bounded count / length.
    The flat-list shape is the security boundary: there is no key for
    a caller to smuggle internal launch wiring (bridge dir, Omnigent URL,
    auth) through — those stay runner-owned (see
    designs/NATIVE_RUNNER_SERVER_LAUNCH.md).

    :param value: The candidate args, e.g.
        ``["--dangerously-skip-permissions"]``, or ``None`` to leave
        unset / unchanged.
    :returns: The validated list unchanged, or ``None`` when *value*
        is ``None``.
    :raises ValueError: If *value* is not a list of strings, exceeds
        :data:`_MAX_TERMINAL_LAUNCH_ARGS` entries, or any entry
        exceeds :data:`_MAX_TERMINAL_LAUNCH_ARG_LEN` characters.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(arg, str) for arg in value):
        raise ValueError("terminal_launch_args must be a list of strings")
    if len(value) > _MAX_TERMINAL_LAUNCH_ARGS:
        raise ValueError(f"terminal_launch_args exceeds {_MAX_TERMINAL_LAUNCH_ARGS} entries")
    for arg in value:
        if len(arg) > _MAX_TERMINAL_LAUNCH_ARG_LEN:
            raise ValueError(
                f"terminal_launch_args entry exceeds {_MAX_TERMINAL_LAUNCH_ARG_LEN} characters"
            )
    return value


def _validated_cost_control_mode_override(value: str | None) -> str | None:
    """
    Validate a caller-supplied per-session cost-control switch.

    :param value: The candidate value, e.g. ``"on"``, or ``None``
        when the caller did not set / wants to clear the override.
    :returns: The value unchanged when valid, or ``None``.
    :raises OmnigentError: 400 (``invalid_input``) when *value* is
        anything other than ``"on"``, ``"off"``, or ``None``.
    """
    if value is None or value in COST_CONTROL_OVERRIDE_VALUES:
        return value
    raise OmnigentError(
        f"invalid cost_control_mode_override: {value!r} (expected 'on', 'off', or null to clear)",
        code=ErrorCode.INVALID_INPUT,
    )


def _parse_session_create_metadata(metadata: str) -> SessionCreateMetadata:
    """
    Parse the JSON metadata part from bundled session creation.

    :param metadata: Raw JSON string from the multipart form,
        e.g. ``{"title": "debug auth flow"}``.
    :returns: Validated :class:`SessionCreateMetadata`.
    :raises OmnigentError: If the JSON fails the request schema.
    """
    try:
        parsed = SessionCreateMetadata.model_validate_json(metadata)
        reasoning_effort = validate_effort(
            parsed.reasoning_effort,
            "session metadata",
            EFFORT_VALUES,
        )
        # Bounds-check the native-terminal args; raises ValueError
        # (wrapped below) on a malformed or oversized list.
        _validate_terminal_launch_args(parsed.terminal_launch_args)
        return parsed.model_copy(update={"reasoning_effort": reasoning_effort})
    except (ValidationError, ValueError) as exc:
        raise OmnigentError(
            f"invalid session metadata: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


def _multipart_missing_detail(field: str) -> dict[str, Any]:
    """
    Build a FastAPI-style missing multipart field error.

    :param field: Missing form field name, e.g. ``"bundle"``.
    :returns: A validation-detail dict for HTTP 422 responses.
    """
    return {
        "type": "missing",
        "loc": ["body", field],
        "msg": "Field required",
        "input": None,
    }


def _require_host_conn_for_worktree(host_id: str | None, request: Request) -> HostConnection:
    """
    Resolve the live host connection for a worktree operation.

    :param host_id: Target host id from the session request, e.g.
        ``"host_a1b2c3d4..."``. ``None`` is rejected — git worktree
        creation requires a host (the server has no filesystem).
    :param request: FastAPI request carrying ``app.state.host_registry``.
    :returns: The live :class:`HostConnection` for ``host_id``.
    :raises OmnigentError: ``invalid_input`` when ``host_id`` is
        ``None``; ``internal_error`` when no host registry is
        configured; ``conflict`` when the host is offline.
    """
    if host_id is None:
        raise OmnigentError(
            "git worktree creation requires host_id",
            code=ErrorCode.INVALID_INPUT,
        )
    host_registry = getattr(request.app.state, "host_registry", None)
    if host_registry is None:
        # Server misconfiguration, not bad client input — mirror
        # _validate_session_workspace, which also returns internal_error.
        raise OmnigentError(
            "host registry is not configured; cannot create a worktree",
            code=ErrorCode.INTERNAL_ERROR,
        )
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        raise OmnigentError(
            f"host {host_id!r} is offline; reconnect the host and try again",
            code=ErrorCode.CONFLICT,
        )
    return host_conn


async def _create_session_worktree(
    *,
    host_id: str | None,
    source_repo: str | None,
    git: SessionGitOptions,
    request: Request,
) -> CreatedWorktree:
    """
    Create a git worktree on the host for a new session branch.

    Validates the branch name server-side (the host re-validates), then
    proxies ``host.create_worktree``. The returned worktree path
    becomes the session ``workspace``. See
    designs/SESSION_GIT_WORKTREE.md.

    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
        Required (worktree creation needs a host).
    :param source_repo: Canonical path of the picked source repo (the
        boundary-validated workspace), e.g. ``"/Users/alice/myrepo"``.
        ``None`` is a programming error and fails loud.
    :param git: Validated git options (``branch_name``, optional
        ``base_branch``).
    :param request: FastAPI request carrying the host registry.
    :returns: The created worktree's ``worktree_path`` (to store as
        ``workspace``) and ``branch`` (to store as ``git_branch``).
    :raises OmnigentError: ``invalid_input`` for a bad branch name,
        missing source repo, or a host-reported git failure (duplicate
        branch, bad base ref, not a repo); ``conflict`` when the host is
        offline or unresponsive; ``internal_error`` when no host registry
        is configured.
    """
    from omnigent.host.git_worktree import WorktreeError, validate_branch_name
    from omnigent.server.routes._host_worktree import (
        WorktreeHostUnavailableError,
        WorktreeProxyError,
        create_worktree_on_host,
    )

    if source_repo is None:  # pragma: no cover — host_id guarantees a workspace
        raise OmnigentError(
            "git worktree creation requires a source repository workspace",
            code=ErrorCode.INVALID_INPUT,
        )
    try:
        validate_branch_name(git.branch_name)
    except WorktreeError as exc:
        raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc

    host_conn = _require_host_conn_for_worktree(host_id, request)
    host_registry = request.app.state.host_registry
    try:
        return await create_worktree_on_host(
            host_registry=host_registry,
            host_conn=host_conn,
            repo_path=source_repo,
            branch_name=git.branch_name,
            base_branch=git.base_branch,
        )
    except WorktreeHostUnavailableError as exc:
        # Host offline / unresponsive — infra, not user input.
        raise OmnigentError(exc.message, code=ErrorCode.CONFLICT) from exc
    except WorktreeProxyError as exc:
        # Host-reported git failure (dup branch, bad base, not a repo) —
        # user-correctable input.
        raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc


async def _remove_session_worktree_best_effort(
    *,
    host_id: str,
    worktree_path: str,
    branch: str,
    delete_branch: bool,
    request: Request,
    reason: str,
) -> None:
    """
    Best-effort removal of a session's git worktree.

    Used for create-rollback (orphan cleanup) and opt-in session-delete
    cleanup. Never raises — a failure is logged so the caller's primary
    operation still completes.

    :param host_id: Host that owns the worktree, e.g.
        ``"host_a1b2c3d4..."``.
    :param worktree_path: Absolute worktree directory to remove on the
        host, e.g. ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Branch checked out in the worktree, e.g.
        ``"feature/login"``.
    :param delete_branch: When ``True``, also run ``git branch -D``
        after removing the worktree directory.
    :param request: FastAPI request carrying the host registry.
    :param reason: Short label for log lines, e.g.
        ``"create-rollback"`` or ``"session-delete"``.
    """
    from omnigent.server.routes._host_worktree import (
        WorktreeProxyError,
        remove_worktree_on_host,
    )

    host_registry = getattr(request.app.state, "host_registry", None)
    if host_registry is None:
        return
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        _logger.warning(
            "Skipping worktree removal (%s) for %s: host %s offline",
            reason,
            worktree_path,
            host_id,
        )
        return
    try:
        await remove_worktree_on_host(
            host_registry=host_registry,
            host_conn=host_conn,
            worktree_path=worktree_path,
            branch=branch,
            delete_branch=delete_branch,
        )
    except WorktreeProxyError:
        _logger.warning(
            "Best-effort worktree removal (%s) failed for %s",
            reason,
            worktree_path,
            exc_info=True,
        )


def _resolve_subagent_spec(
    *,
    agent: Agent,
    sub_agent_name: str,
    agent_cache: AgentCache | None,
) -> AgentSpec | None:
    """
    Load the parent bundle and resolve a child sub-agent's trusted spec.

    This is the single trusted source for any per-sub-agent launch wiring
    the server derives at create time (terminal-first labels, YOLO
    pass-through args). The spec comes from the server-loaded parent
    bundle — never from caller-supplied request fields — so a caller
    cannot smuggle in launch config a sub-agent's own bundle did not
    declare.

    :param agent: The parent agent row, e.g. the ``polly`` orchestrator,
        whose bundle contains the sub-agent specs.
    :param sub_agent_name: The dispatched sub-agent's name, e.g.
        ``"claude_code"``.
    :param agent_cache: Cache for loading the parsed parent bundle. ``None``
        disables resolution (returns ``None``).
    :returns: The matching child :class:`AgentSpec`, or ``None`` when the
        cache is absent, the bundle fails to load, or no sub-agent matches.
    """
    if agent_cache is None:
        return None
    from omnigent.runtime.workflow import _find_spec_by_name

    try:
        parent_spec = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        ).spec
    except Exception:  # noqa: BLE001 -- create-time resolution is best-effort; never block create.
        # A bundle that fails to load here must not break session
        # creation; the session still works, just without the
        # derived labels / launch args.
        _logger.warning(
            "Could not load bundle for agent %s to resolve sub-agent %r spec",
            agent.id,
            sub_agent_name,
            exc_info=True,
        )
        return None
    return _find_spec_by_name(parent_spec, sub_agent_name)


def _spec_harness(spec: AgentSpec) -> str:
    """
    Return the canonical harness identifier for a resolved spec.

    :param spec: A parsed agent / sub-agent spec.
    :returns: The canonical harness id, e.g. ``"claude-native"`` or
        ``"codex-native"``; falls back to ``executor.type`` when no
        ``harness`` is declared.
    """
    from omnigent.harness_aliases import canonicalize_harness

    harness = spec.executor.config.get("harness") or spec.executor.type
    return canonicalize_harness(harness) or harness


def _spec_config_flag_explicitly_disabled(spec: AgentSpec, key: str) -> bool:
    """
    Return whether an ``executor.config`` flag is explicitly set false.

    The spec parser stringifies every ``executor.config`` value (see
    ``omnigent/spec/parser.py`` — ``{str(k): str(v) ...}``), so a YAML
    ``yolo: false`` arrives here as the string ``"False"``. A naive
    ``not bool(value)`` is wrong: ``bool("False")`` is ``True`` (so a
    naive truthiness test would read ``"False"`` as enabled). This
    compares against the falsey spellings explicitly so only an
    intentional ``false`` / ``False`` counts as disabled — an absent key
    or any other value is NOT disabled.

    Used for opt-OUT semantics: the relevant flag defaults to enabled and
    an explicit ``false`` is the escape hatch (see the codex-native branch
    of :func:`_derive_terminal_launch_args_from_spec`).

    :param spec: A parsed sub-agent spec.
    :param key: The ``executor.config`` key to read, e.g. ``"yolo"``.
    :returns: ``True`` only when the value is the boolean ``False`` or the
        string ``"false"`` (case-insensitive); ``False`` otherwise
        (including when the key is absent).
    """
    value = spec.executor.config.get(key)
    if isinstance(value, bool):
        return value is False
    return isinstance(value, str) and value.strip().lower() == "false"


def _derive_terminal_launch_args_from_spec(sub_spec: AgentSpec) -> list[str] | None:
    """
    Derive native-terminal YOLO pass-through args from a trusted sub-spec.

    polly's native workers (claude-native / codex-native / cursor-native)
    launch in a headless pane where no human can answer an ApprovalCard, so
    every Edit/Write/Bash that prompts stalls the worker. This translates a
    worker bundle's declared full-bypass intent into the per-session
    ``terminal_launch_args`` the runner already appends to the native CLI
    argv:

    - claude-native + ``executor.config.permission_mode`` set ->
      ``["--permission-mode", "<value>"]``. The value is passed through
      verbatim so non-YOLO modes (``acceptEdits``, ``plan``, ...) work too;
      YOLO uses ``bypassPermissions``.
    - codex-native -> ``["--dangerously-bypass-approvals-and-sandbox"]``
      by DEFAULT. A headless codex worker has no human to answer codex's
      approval prompts, and codex's own command sandbox often cannot even
      start (e.g. inside a hardened container), so codex's default
      ``approval_policy=on-request`` + own-sandbox stance stalls the
      worker on its first Edit/Write/Bash. Full bypass is the only
      non-stalling stance for the headless seam (the container / worktree
      is the real boundary, matching claude-native's ``bypassPermissions``
      and the codex-sdk executor's ``approvalPolicy="never"``). An explicit
      ``executor.config.yolo: false`` opts back out for a read-only / must
      -keep-prompting sub-agent. See issue #171.
    - cursor-native -> ``["--yolo"]`` by DEFAULT. Headless cursor workers
      otherwise stall on cursor-agent's in-terminal approval prompts (also
      mirrored as web elicitation cards). ``--yolo`` is cursor-agent's
      don't-ask / full-bypass flag (``--auto-review`` still prompts for
      some calls). An explicit ``executor.config.yolo: false`` opts back
      out. When ``executor.config.permission_mode`` / ``exec_mode`` is set
      to ``auto`` or ``auto-review``, emit ``["--auto-review"]`` instead
      (Smart Auto) so a bundle can choose Claude-style auto without full
      yolo.

    Only those native harnesses are translated; for any other harness
    (e.g. ``claude-sdk`` / ``cursor``, whose bypass is set via the SDK
    ``permissionMode`` / ``auto_review`` spawn path, not a terminal flag)
    this returns ``None`` so no terminal args are set. ``None`` is also
    returned when the relevant field is absent / falsey.

    :param sub_spec: The trusted child sub-agent spec, resolved from the
        server-loaded parent bundle via :func:`_resolve_subagent_spec`.
    :returns: A flat CLI-arg list to store as the child session's
        ``terminal_launch_args``, or ``None`` when nothing should be set.
    :raises ValueError: If a spec-derived argument violates the same
        bounds enforced for request-supplied ``terminal_launch_args``.
    """
    harness = _spec_harness(sub_spec)
    if harness == _CLAUDE_NATIVE_HARNESS:
        permission_mode = sub_spec.executor.config.get("permission_mode")
        if permission_mode:
            return _validate_terminal_launch_args(["--permission-mode", str(permission_mode)])
        return None
    if harness == _CODEX_NATIVE_HARNESS:
        # Headless default: full bypass. The terminal_launch_args set the
        # codex --remote TUI's launch flags, which is what creates the
        # app-server thread and fixes its approval/sandbox stance for the
        # session; the omnigent executor's later turn/start inherits that
        # stance (codex_native_executor.run_turn carries no per-turn
        # approval/sandbox). Without the flag the thread is created at
        # codex's on-request + own-sandbox default and a headless worker
        # stalls. An explicit ``yolo: false`` is the opt-out. See #171.
        if _spec_config_flag_explicitly_disabled(sub_spec, "yolo"):
            return None
        return _validate_terminal_launch_args(["--dangerously-bypass-approvals-and-sandbox"])
    if harness == _CURSOR_NATIVE_HARNESS:
        # Prefer an explicit Smart Auto mode when the bundle asks for it
        # (mirrors Claude's ``permission_mode: auto``), else full --yolo
        # by default so headless polly workers don't stall on mirrored
        # approval cards. ``yolo: false`` is the keep-prompting opt-out.
        mode = (
            sub_spec.executor.config.get("permission_mode")
            or sub_spec.executor.config.get("exec_mode")
            or ""
        )
        mode_norm = str(mode).strip().lower()
        if mode_norm in ("auto", "auto-review"):
            return _validate_terminal_launch_args(["--auto-review"])
        if _spec_config_flag_explicitly_disabled(sub_spec, "yolo"):
            return None
        return _validate_terminal_launch_args(["--yolo"])
    return None


def _native_subagent_wrapper_labels_from_spec(sub_spec: AgentSpec) -> dict[str, str]:
    """
    Resolve terminal-first wrapper labels from an already-loaded sub-spec.

    :param sub_spec: Trusted child sub-agent spec resolved from the
        parent bundle.
    :returns: ``{wrapper_key: value, ui_key: "terminal"}`` for a native
        sub-agent, or ``{}`` when the sub-agent is not native.
    """
    harness = _spec_harness(sub_spec)
    native_agent = native_coding_agent_for_harness(harness)
    if native_agent is not None:
        return {
            _CLAUDE_NATIVE_WRAPPER_LABEL_KEY: native_agent.wrapper_label,
            _CLAUDE_NATIVE_UI_LABEL_KEY: _CLAUDE_NATIVE_UI_LABEL_VALUE,
        }
    return {}


def _reject_reserved_cost_control_label_seed(labels: dict[str, str]) -> None:
    """
    Reject a session-create body that seeds policy-owned labels.

    ``cost_control.*`` is the cost advisor's telemetry namespace and its
    only legitimate writer is the session's bound runner — which cannot
    exist yet at create time, so a seed is always a forgery.

    :param labels: The client-supplied initial labels, e.g.
        ``{"team": "ml"}``.
    :raises OmnigentError: 400 when any ``cost_control.*`` key is
        present.
    """
    reserved = reserved_cost_control_keys(labels)
    if reserved:
        raise OmnigentError(
            f"labels {', '.join(repr(key) for key in reserved)} "
            f"are in the policy-owned {COST_CONTROL_LABEL_NAMESPACE}* "
            "namespace and cannot be set at session creation",
            code=ErrorCode.INVALID_INPUT,
        )


def _reject_server_reserved_label_seed(labels: dict[str, str] | None) -> None:
    """
    Reject a client-supplied label map that touches server-internal keys.

    Keys in this set are written exclusively by server internals and must
    not be client-settable — doing so would let callers forge security-
    critical metadata (e.g. the policy-evaluation actor identity).

    :param labels: The client-supplied label mapping, or ``None``.
    :raises OmnigentError: 400 when any reserved key is present.
    """
    if not labels or _TURN_ACTOR_LABEL not in labels:
        return
    raise OmnigentError(
        f"label {_TURN_ACTOR_LABEL!r} is server-internal and cannot be set by clients",
        code=ErrorCode.INVALID_INPUT,
    )


def _require_cost_control_label_authority(
    *,
    reserved_keys: Sequence[str],
    tunnel_token: str | None,
    bound_runner_id: str | None,
    allowed_tunnel_tokens: frozenset[str] | None,
    multi_user: bool,
) -> None:
    """
    Authorize a label write touching the policy-owned ``cost_control.*`` keys.

    These are the cost advisor's telemetry labels, so ordinary session
    editors must not set them via PATCH; the advisor's persist proves
    itself with the runner tunnel binding token (allow-listed, or bound
    to this session's runner id — the tunnel route's trust model).
    Single-user servers skip the check: loopback runners may register
    under stable ids unrelated to any token, and there is no second
    identity to forge against.

    :param reserved_keys: The ``cost_control.*`` keys the request tries
        to write, e.g. ``("cost_control.plan",)``. Quoted in the error.
    :param tunnel_token: Value of the ``X-Omnigent-Runner-Tunnel-Token``
        request header, or ``None`` when absent.
    :param bound_runner_id: The session's current ``runner_id``, or
        ``None`` when no runner is bound.
    :param allowed_tunnel_tokens: The server's tunnel-token allow-list,
        or ``None`` when not configured.
    :param multi_user: ``True`` when the server enforces per-user
        permissions (a permission store is configured).
    :raises OmnigentError: 403 when the caller presents no acceptable
        runner proof on a multi-user server.
    """
    if not multi_user:
        return
    keys = ", ".join(repr(key) for key in reserved_keys)
    token = (tunnel_token or "").strip()
    if token:
        if allowed_tunnel_tokens is not None and token in allowed_tunnel_tokens:
            return
        if bound_runner_id is not None and token_bound_runner_id(token) == bound_runner_id:
            return
    raise OmnigentError(
        f"labels {keys} are in the policy-owned "
        f"{COST_CONTROL_LABEL_NAMESPACE}* namespace; only the session's "
        "bound runner may write them",
        code=ErrorCode.FORBIDDEN,
    )


def _persist_stored_session_bundle(
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore,
    metadata: SessionCreateMetadata,
    *,
    agent_id: str,
    agent_name: str,
    agent_bundle_location: str,
    agent_description: str | None,
    runner_id: str | None = None,
) -> CreatedSessionResponse:
    """
    Persist database rows for a bundle already written to artifacts.

    :param conversation_store: Store that owns the atomic
        conversation-plus-agent transaction.
    :param artifact_store: Store for deleting the bundle on failure.
    :param metadata: Validated session metadata. A set
        ``parent_session_id`` creates the conversation as a
        sub-agent child of that session.
    :param agent_id: New agent id, e.g. ``"ag_abc123"``.
    :param agent_name: Agent name loaded from the uploaded spec.
    :param agent_bundle_location: Artifact key for the stored bundle.
    :param agent_description: Optional description from the spec.
    :param runner_id: Optional runner binding inherited from the
        parent session, e.g. ``"runner_abc123"``.
    :returns: Response with the new session id.
    :raises OmnigentError: If the agent insert violates integrity
        checks or the parent session no longer exists.
    :raises SQLAlchemyError: If the database transaction fails for
        any non-integrity reason.
    """
    try:
        created = conversation_store.create_session_with_agent(
            agent_id=agent_id,
            agent_name=agent_name,
            agent_bundle_location=agent_bundle_location,
            agent_description=agent_description,
            title=metadata.title,
            labels=metadata.labels,
            reasoning_effort=metadata.reasoning_effort,
            workspace=metadata.workspace,
            terminal_launch_args=metadata.terminal_launch_args,
            parent_conversation_id=metadata.parent_session_id,
            runner_id=runner_id,
        )
    except ConversationNotFoundError as exc:
        # Parent was authorized by the caller but vanished (deleted)
        # before the insert transaction ran.
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        raise OmnigentError(
            str(exc),
            code=ErrorCode.NOT_FOUND,
        ) from exc
    except IntegrityError as exc:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        # Expected integrity failures here are uniqueness collisions:
        # generated agent id, generated conversation id, or
        # agents.session_id. The route maps those to 409.
        raise OmnigentError(
            f"session agent write failed integrity checks: {exc.orig}",
            code=ErrorCode.ALREADY_EXISTS,
        ) from exc
    except SQLAlchemyError:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        raise

    # The create request has no conv id in its URL; stamp the minted id so
    # the create span joins the session's session.id group.
    from omnigent.runtime import telemetry

    telemetry.set_session_id(created.conversation.id)
    return CreatedSessionResponse(
        session_id=created.conversation.id,
        agent_id=agent_id,
        agent_name=agent_name,
    )


def _delete_stored_session_bundle_after_failure(
    artifact_store: ArtifactStore,
    agent_bundle_location: str,
) -> None:
    """
    Delete an uploaded bundle after database creation fails.

    Cleanup failures are logged but suppressed so the original
    exception remains the error seen by callers.

    :param artifact_store: Store that contains the uploaded bundle.
    :param agent_bundle_location: Artifact key to delete, e.g.
        ``"ag_abc123/a1b2c3d4"``.
    :returns: None.
    """
    try:
        artifact_store.delete(agent_bundle_location)
    except Exception:  # noqa: BLE001 - cleanup must not mask the original failure.
        _logger.warning(
            "Failed to delete uploaded session bundle %s after rollback",
            agent_bundle_location,
            exc_info=True,
        )


async def _authorize_bundled_parent_and_inherit_runner(
    parent_session_id: str,
    *,
    user_id: str | None,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> str | None:
    """
    Authorize a bundled create's parent link and resolve runner affinity.

    The caller must have READ access to the parent session
    before inheriting anything, mirroring the JSON create path —
    without this, a forged parent link lets the caller inherit runner
    bindings and parent a session they don't control. On success the
    parent's runner binding is inherited (sub-agent co-location),
    subject to a defense-in-depth ownership check: a runner the
    caller doesn't own is not inherited.

    :param parent_session_id: The requested parent session id,
        e.g. ``"conv_abc123"``.
    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``.
    :param permission_store: Permission store for the access
        check; ``None`` in single-user / no-auth mode.
    :param conversation_store: Store for the parent-conversation read.
    :param runner_router: Router for the runner-ownership check;
        ``None`` skips it.
    :returns: The inherited runner id, or ``None`` when the parent has
        no runner binding or ownership disallows inheritance.
    :raises OmnigentError: 403/404 when the caller may not access the
        parent session.
    """
    await _require_access(
        user_id,
        parent_session_id,
        LEVEL_READ,
        permission_store,
        conversation_store,
    )
    parent_conv = await asyncio.to_thread(
        conversation_store.get_conversation,
        parent_session_id,
    )
    if parent_conv is None:
        return None
    inherited_runner_id = parent_conv.runner_id
    if inherited_runner_id is not None and user_id is not None and runner_router is not None:
        runner_owner = runner_router.runner_owner(inherited_runner_id)
        if runner_owner is not None and runner_owner != user_id:
            return None
    return inherited_runner_id


async def _notify_runner_of_bundled_child(
    session_id: str,
    agent_id: str,
    runner_router: RunnerRouter | None,
) -> None:
    """
    Notify the inherited runner that a bundled child session exists.

    Lets the runner initialize per-session state (inbox queue,
    agent-id cache) before the first forwarded event, mirroring the
    JSON create path's post-create notify. Failures are logged and
    swallowed — the notify is additive and must not fail the create.

    :param session_id: The new child session id, e.g. ``"conv_abc123"``.
    :param agent_id: The child's session-scoped agent id,
        e.g. ``"ag_abc123"``.
    :param runner_router: Router used to resolve the bound runner's
        client; ``None`` falls back to the in-process runner.
    :returns: None.
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return
    try:
        await runner_client.post(
            "/v1/sessions",
            json={
                "session_id": session_id,
                "agent_id": agent_id,
                "sub_agent_name": None,
            },
            timeout=10.0,
        )
    except (httpx.HTTPError, ConnectionError):
        _logger.warning(
            "Failed to notify runner about bundled session %s",
            session_id,
            exc_info=True,
        )


def _registered_runner_id(
    runner_router: RunnerRouter | None,
    raw_runner_id: str,
    *,
    user_id: str | None = None,
) -> str:
    """
    Validate a runner id from ``PATCH /v1/sessions/{id}``.

    When ``user_id`` is provided the function also enforces runner
    ownership: only the user who established the tunnel may
    bind sessions to that runner.

    :param runner_router: Router backed by the live tunnel registry.
        ``None`` means this server cannot bind runners.
    :param raw_runner_id: Runner id from the request body, e.g.
        ``"runner_abc123"``.
    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``. ``None`` skips the ownership
        check (single-user / no-auth mode).
    :returns: Trimmed registered runner id.
    :raises OmnigentError: If the id is empty, the router is
        unavailable, the runner is not registered, or the caller
        does not own the runner.
    """
    runner_id = raw_runner_id.strip()
    if not runner_id:
        raise OmnigentError(
            "runner_id must not be empty",
            code=ErrorCode.INVALID_INPUT,
        )
    if runner_router is None:
        raise OmnigentError(
            "runner router is not configured",
            code=ErrorCode.INTERNAL_ERROR,
        )
    if not runner_router.runner_is_online(runner_id):
        raise OmnigentError(
            f"runner {runner_id!r} is not registered",
            code=ErrorCode.INVALID_INPUT,
        )
    # Enforce runner ownership. A caller must own the runner
    # they are trying to bind to a session.
    if user_id is not None:
        runner_owner = runner_router.runner_owner(runner_id)
        if runner_owner is not None and runner_owner != user_id:
            raise OmnigentError(
                f"runner {runner_id!r} is not owned by the requesting user",
                code=ErrorCode.FORBIDDEN,
            )
    return runner_id


def _latest_message_preview(
    items: list[ConversationItem],
    limit_chars: int = _CHILD_PREVIEW_LIMIT,
) -> str | None:
    """
    Return a single-line text preview from newest-first message items.

    Powers the sub-agent rail row's status line so the user can see what
    the child is saying without opening it. The caller supplies a
    batched newest-first message list for one child; this function joins
    ``input_text`` / ``output_text`` blocks from the first non-meta
    message with text, collapses whitespace, and truncates to
    ``limit_chars``. Hidden meta messages carry durable runner context
    and must never be shown as user-facing previews.

    :param items: Newest-first message items for one conversation.
    :param limit_chars: Max preview length in characters,
        e.g. ``150``.
    :returns: Truncated single-line preview text, e.g.
        ``"I'll search the codebase for references…"``, or ``None``.
    """
    for item in items:
        if not isinstance(item.data, MessageData) or item.data.is_meta:
            continue
        parts: list[str] = []
        for block in item.data.content:
            block_type = block.get("type")
            text = block.get("text")
            if block_type in ("input_text", "output_text") and isinstance(text, str):
                parts.append(text)
        collapsed = " ".join(" ".join(parts).split())
        if not collapsed:
            continue
        if len(collapsed) <= limit_chars:
            return collapsed
        # Trim to one char less than the limit so the trailing ellipsis
        # keeps the field at ``limit_chars`` total.
        return collapsed[: max(0, limit_chars - 1)].rstrip() + "…"
    return None


def _child_session_current_task_status_from_cached_status(status: object) -> str | None:
    """
    Map cached session lifecycle status onto child-summary task status.

    :param status: Cached ``session.status`` value.
    :returns: Public ``ChildSessionSummary.current_task_status`` value.
    """
    if status in ("running", "waiting"):
        return "in_progress"
    if status == "idle":
        return "completed"
    if status == "failed":
        return "failed"
    return None


def _child_session_summary_from_conversation(
    conv: Conversation,
    parent_session_id: str,
    last_message_preview: str | None,
) -> ChildSessionSummary:
    """
    Build a :class:`ChildSessionSummary` from a child conversation.

    Parses the canonical sub-agent title format
    ``"{agent_type}:{session_name}"`` written by
    :func:`omnigent.tools.builtins.spawn._spawn_one`, plus the
    3-segment ``"ui:{agent_name}:{user_label}"`` form written by the
    Web UI "Add agent" flow (surfaced as ``tool={agent_name}`` and
    ``session_name={user_label}``). Tolerates malformed/legacy rows:
    if the title is ``None`` or has no colon, ``tool`` falls back to
    the raw title and ``session_name`` is ``None`` — the row is still
    surfaced so debug views can investigate.

    ``busy`` is derived from the relay-fed ``_session_status_cache``
    (the tasks table has been removed). ``agent_id`` and ``agent_name``
    are read from the conversation row directly.

    :param conv: A child :class:`Conversation` row
        (``kind="sub_agent"``) from
        :meth:`ConversationStore.list_conversations`.
    :param parent_session_id: The parent session id from the
        route, e.g. ``"conv_parent987"``. Passed in rather than
        re-reading from ``conv.parent_conversation_id`` to keep
        the helper indifferent to legacy rows where the FK might
        be missing.
    :param last_message_preview: Preview text derived from a batched
        child-message lookup, or ``None`` when no visible message exists.
    :returns: A populated :class:`ChildSessionSummary`.
    """
    display_title = title_without_closed_marker(conv.title)
    labels = labels_with_closed_status(conv.labels, conv.title)
    tool: str | None
    session_name: str | None
    if _is_codex_native_subagent(conv):
        # Codex-native child: surface the Codex-assigned nickname/role as
        # ``tool`` and the raw thread id as ``session_name`` for correlation.
        tool = _codex_subagent_display_tool(labels)
        session_name = labels.get(_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY)
    elif display_title and ":" in display_title:
        head, _, tail = display_title.partition(":")
        if head == _UI_ADDED_AGENT_TITLE_PREFIX and ":" in tail:
            # User-added agent: "ui:<agent_name>:<user_label>". Surface the
            # bound agent as ``tool`` and the user's label as ``session_name``
            # so the Agents rail renders it like any other child row.
            agent_name, _, user_label = tail.partition(":")
            tool = agent_name
            session_name = user_label
        else:
            tool = head
            session_name = tail
    else:
        tool = display_title or None
        session_name = None

    # Derive busy from the relay-fed cache; tasks table is gone.
    cached_status = _session_status_cache.get(conv.id)
    if cached_status in ("running", "waiting"):
        busy = True
    else:
        busy = False
    last_task_error = _last_task_error_from_labels(labels)
    current_task_status = _child_session_current_task_status_from_cached_status(cached_status)
    if last_task_error is not None:
        current_task_status = "failed"

    # For Codex children, fall back to the prompt label as preview when the
    # real transcript has not arrived yet — avoids synthesizing a user message
    # just so the rail has something to show.
    if last_message_preview is None and _is_codex_native_subagent(conv):
        raw_prompt = labels.get(_CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY)
        if raw_prompt:
            collapsed = " ".join(raw_prompt.split())
            last_message_preview = collapsed[:_CHILD_PREVIEW_LIMIT] or None

    return ChildSessionSummary(
        id=conv.id,
        parent_session_id=parent_session_id,
        title=display_title,
        tool=tool,
        session_name=session_name,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        # agent_id comes from the conversation row; agent_name and task_id
        # are no longer available from the (removed) tasks table.
        agent_id=conv.agent_id,
        agent_name=None,
        current_task_id=None,
        current_task_status=current_task_status,
        busy=busy,
        labels=labels,
        last_task_error=last_task_error,
        last_message_preview=last_message_preview,
        # Surface the sub-agent's parked-elicitation count from the same
        # in-memory index that feeds the sidebar badge, so the Agents
        # rail can flag a child that's awaiting user input.
        pending_elicitations_count=pending_elicitations.count_for(conv.id),
    )


def _mcp_tool_result(rpc_id: int | str | None, text: str) -> Response:
    """
    Wrap a plain-text tool result in a JSON-RPC 2.0 MCP ``tools/call`` response.

    :param rpc_id: The JSON-RPC request id (may be int, str, or ``None``
        for notifications), e.g. ``1``.
    :param text: The tool output text to embed in the ``content`` block.
    :returns: A :class:`Response` with ``Content-Type: application/json``
        carrying the JSON-RPC 2.0 envelope with a single ``text`` content block.
    """
    body = json.dumps(
        {"jsonrpc": "2.0", "id": rpc_id, "result": {"content": [{"type": "text", "text": text}]}}
    )
    return Response(content=body, media_type="application/json")


async def _handle_advise_models_mcp(
    rpc_id: int | str | None,
    conv: Any,
    arguments: dict[str, Any],
    agent_store: Any,
    *,
    session_id: str | None = None,
    runner_router: Any = None,
) -> Response:
    """
    Server-side handler for ``sys_advise_models`` MCP tool calls.

    Intercepts the call before the runner forward because
    ``RuntimeCaps.routing_client`` lives in the server process.

    :param rpc_id: The JSON-RPC request id.
    :param conv: The :class:`Conversation` for this session.
    :param arguments: Parsed tool arguments from the LLM.
    :param agent_store: Store for agent lookup (used to resolve sub-agent harnesses).
    :returns: A JSON-RPC 2.0 ``tools/call`` result response.
    """
    tasks = arguments.get("tasks")
    if not isinstance(tasks, list):
        return _mcp_tool_result(
            rpc_id, json.dumps({"error": "tasks must be a list", "router_on": False})
        )

    caps = get_caps()
    routing_client = caps.routing_client
    if routing_client is None:
        return _mcp_tool_result(rpc_id, json.dumps({"router_on": False, "recommendations": []}))

    from omnigent.model_catalog import spec_harness
    from omnigent.server.smart_routing import fetch_runner_models, infer_models

    # Fetch live model catalog from the runner once; used below to populate
    # per-agent model lists when the caller omits explicit models.
    # Keys are worker names ("self", "claude_code", etc.) as returned by
    # catalog_for_spec.  None when runner is unreachable — falls back to
    # infer_models static table.
    _runner_catalog: dict[str, list[str]] | None = None
    if session_id is not None and runner_router is not None:
        _runner_client = await _get_runner_client(session_id, runner_router)
        if _runner_client is not None:
            _runner_catalog = await fetch_runner_models(session_id, _runner_client)

    # Resolve the parent agent spec to look up sub-agent harnesses.
    spec: Any | None = None
    if conv.agent_id is not None:
        agent_obj = await asyncio.to_thread(agent_store.get, conv.agent_id)
        if agent_obj is not None:
            try:
                spec = (
                    get_agent_cache()
                    .load(
                        agent_obj.id,
                        agent_obj.bundle_location,
                        expand_env=agent_obj.session_id is None,
                    )
                    .spec
                )
            except Exception:  # noqa: BLE001
                _logger.debug(
                    "_handle_advise_models_mcp: failed to load spec for agent=%s", conv.agent_id
                )

    _WORKER_HARNESS: dict[str, str] = {
        "claude_code": "claude-sdk",
        "codex": "codex",
        "pi": "pi",
    }

    def _resolve_harness_for_worker(agent: str) -> str | None:
        if spec is not None:
            sub_agents = getattr(spec, "sub_agents", None) or []
            for sub in sub_agents:
                if getattr(sub, "name", None) == agent:
                    h = spec_harness(sub)
                    if h:
                        return h
                    break
        return _WORKER_HARNESS.get(agent)

    recommendations: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        title = task.get("title", "")
        task_text = task.get("task", "")
        agents_spec = task.get("agents")
        if not isinstance(agents_spec, list) or not agents_spec:
            continue

        # Build harness→models map for the routing client, plus two reverse
        # maps for resolving the chosen agent after the verdict:
        # - harness_to_agent: preferred path when the judge picks a harness
        # - model_to_agent: fallback when harness is absent or unrecognised
        # Insertion order is preserved; first-agent-wins dedup applies when
        # the same model appears in multiple harness lists.
        model_to_agent: dict[str, str] = {}
        harness_to_agent: dict[str, str] = {}
        harness_models: dict[str, list[str]] = {}
        for agent_entry in agents_spec:
            if not isinstance(agent_entry, dict):
                continue
            agent = agent_entry.get("agent", "")
            explicit_models: list[str] | None = agent_entry.get("models")
            if explicit_models is not None and not isinstance(explicit_models, list):
                explicit_models = None
            if explicit_models:
                harness_key = agent  # use agent name as key when models are explicit
                candidates = explicit_models
            else:
                harness_key = _resolve_harness_for_worker(agent) or agent
                # Prefer live runner catalog (worker name or harness key);
                # fall back to static infer_models table.
                candidates = (
                    (_runner_catalog or {}).get(agent)
                    or (_runner_catalog or {}).get(harness_key)
                    or infer_models(harness_key)
                    or []
                )
            if candidates:
                harness_models.setdefault(harness_key, [])
                harness_to_agent.setdefault(harness_key, agent)
                for m in candidates:
                    if m not in model_to_agent:
                        model_to_agent[m] = agent
                        harness_models[harness_key].append(m)

        if not harness_models:
            recommendations.append(
                {"title": title, "agent": None, "model": None, "rationale": "no candidates"}
            )
            continue
        try:
            verdict = await routing_client.route(task_text, harness_models)
        except Exception:  # routing failures must not crash the advisor
            _logger.exception("_handle_advise_models_mcp: route failed task=%r", title)
            verdict = None
        if verdict is None:
            recommendations.append(
                {
                    "title": title,
                    "agent": None,
                    "model": None,
                    "rationale": "router returned no verdict",
                }
            )
        else:
            # Prefer the judge's harness pick; fall back to model ownership.
            chosen_agent = (
                harness_to_agent.get(verdict.harness) if verdict.harness else None
            ) or model_to_agent.get(verdict.model)
            recommendations.append(
                {
                    "title": title,
                    "agent": chosen_agent,
                    "model": verdict.model,
                    "rationale": verdict.rationale,
                }
            )

    return _mcp_tool_result(
        rpc_id, json.dumps({"router_on": True, "recommendations": recommendations})
    )


def _mcp_ok_response(rpc_id: int | str | None, result: dict[str, Any]) -> Response:
    """
    Wrap *result* in a JSON-RPC 2.0 success response.

    :param rpc_id: The JSON-RPC request id (may be int, str, or ``None``
        for notifications), e.g. ``1``.
    :param result: The JSON-serialisable result payload, e.g.
        ``{"tools": [...]}``.
    :returns: A :class:`Response` with ``Content-Type: application/json``
        carrying the JSON-RPC 2.0 envelope.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result})
    return Response(content=body, media_type="application/json")


def _mcp_error_response(
    rpc_id: int | str | None,
    code: int,
    message: str,
) -> Response:
    """
    Wrap an error in a JSON-RPC 2.0 error response.

    :param rpc_id: The JSON-RPC request id. Use ``None`` when the id
        could not be parsed, e.g. ``None``.
    :param code: JSON-RPC error code, e.g. ``-32601`` (method not found)
        or ``-32000`` (application error).
    :param message: Human-readable error description,
        e.g. ``"Method not found: 'unsupported/method'"``.
    :returns: A :class:`Response` with ``Content-Type: application/json``
        carrying the JSON-RPC 2.0 error envelope.
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        }
    )
    return Response(content=body, media_type="application/json")


def _mcp_input_required_response(
    rpc_id: int | str | None,
    elicitation_id: str,
    message: str,
    request_state: str,
    session_id: str | None = None,
) -> Response:
    """
    Return an MCP ``InputRequiredResult`` asking the runner to collect
    user approval before retrying the tool call.

    Follows the Multi Round-Trip Requests (MRTR) spec:
    ``https://modelcontextprotocol.io/specification/draft/basic/utilities/mrtr``.
    The ``elicitation_id`` is used as the key in ``inputRequests`` so the
    runner can identify the approval Future without inspecting the opaque
    ``requestState``. When URL-mode is active and ``session_id`` is
    known, adds ``mode``/``url`` to params.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param elicitation_id: Server-minted elicitation id used both as the
        ``inputRequests`` key and inside the opaque ``requestState``,
        e.g. ``"elicit_abc123"``.
    :param message: Human-readable prompt shown to the user,
        e.g. ``"Allow tool sys_os_shell?"``.
    :param request_state: Opaque state blob the client echoes on retry.
        Contains the ``elicitation_id`` and ``session_id`` so the server
        can verify authenticity on retry without server-side storage.
    :param session_id: Session/conversation id for constructing the
        approval page URL, e.g. ``"conv_abc123"``. ``None`` omits the
        URL (form mode).
    :returns: A :class:`Response` carrying the JSON-RPC 2.0
        ``InputRequiredResult`` envelope.
    """

    params: dict[str, Any] = {
        "message": message,
        "requestedSchema": {
            "type": "object",
            "properties": {"approved": {"type": "boolean"}},
            "required": ["approved"],
        },
    }
    if session_id is not None and _ELICITATION_MODE == "url":
        params["mode"] = "url"
        params["url"] = f"/approve/{session_id}/{elicitation_id}"
    else:
        params["mode"] = "form"

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "resultType": "input_required",
                "inputRequests": {
                    elicitation_id: {
                        "method": "elicitation/create",
                        "params": params,
                    }
                },
                "requestState": request_state,
            },
        }
    )
    return Response(content=body, media_type="application/json")


async def _handle_mcp_tools_list(
    rpc_id: int | str | None,
    session_id: str,
    runner_router: RunnerRouter | None,
) -> Response:
    """
    Handle a ``tools/list`` JSON-RPC request for the MCP proxy endpoint.

    Delegates execution to the runner's ``POST
    /v1/sessions/{id}/mcp/execute`` endpoint so that stdio MCP
    subprocesses spawn on the runner's machine (correct ``cwd``,
    env, and tooling). The Omnigent server's role here is routing only —
    policy evaluation happens in ``tools/call``.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param session_id: The session id whose agent's tools to list,
        e.g. ``"conv_abc123"``.
    :param runner_router: Router used to get an httpx client pointed
        at the session's runner. ``None`` returns an error.
    :returns: A JSON-RPC 2.0 ``tools/list`` result response, or an
        error response when the runner is unavailable.
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        # Fall back to the in-process runner client (local single-user mode).
        from omnigent.runtime import get_runner_client

        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return _mcp_error_response(rpc_id, -32000, f"No runner bound for session {session_id!r}")
    _logger.debug("MCP tools/list: delegating to runner execute for session=%r", session_id)
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/mcp/execute",
            json={"method": "tools/list", "params": {}},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Runner MCP execute failed: %s", exc, exc_info=True)
        return _mcp_error_response(rpc_id, -32000, "Runner MCP execute failed.")

    if "error" in data:
        err = data["error"]
        return _mcp_error_response(
            rpc_id, err.get("code", -32000), err.get("message", "unknown error")
        )

    result = data.get("result", {})
    # schemas are already in OpenAI function-tool format from RunnerMcpManager;
    # convert back to MCP inputSchema format for the tools/list response since
    # ProxyMcpManager on the runner expects MCP-shaped tools/list output.
    schemas: list[dict[str, Any]] = result.get("schemas", [])
    tools = []
    for schema in schemas:
        # schema shape: {"type": "function", "name": "srv__tool",
        #                "description": "...", "parameters": {...}}
        tools.append(
            {
                "name": schema.get("name", ""),
                "description": schema.get("description", ""),
                "inputSchema": schema.get("parameters") or {"type": "object", "properties": {}},
            }
        )

    failures: dict[str, str] = result.get("failures", {})
    for srv, msg in failures.items():
        _logger.warning("runner MCP server %r unavailable: %s", srv, msg)

    _logger.debug(
        "MCP tools/list: session=%r returning %d tools, %d failures",
        session_id,
        len(tools),
        len(failures),
    )
    return _mcp_ok_response(rpc_id, {"tools": tools})


async def _read_upload_capped(file: UploadFile, limit_bytes: int) -> bytes:
    """
    Read an uploaded file into memory, aborting if it exceeds *limit_bytes*.

    Reads in :data:`_UPLOAD_READ_CHUNK_BYTES` chunks and raises HTTP 413 as
    soon as the cap is crossed, so an oversized upload never buffers more
    than one chunk past the limit.

    :param file: The multipart upload.
    :param limit_bytes: Maximum allowed size in bytes.
    :returns: The full file content.
    :raises HTTPException: 413 when the upload exceeds *limit_bytes*.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Attachment exceeds the {limit_bytes // (1024 * 1024)} MB "
                    "limit for this file type."
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _load_runner_skills(
    runner_client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Background single-flight fetch of a session's runner-owned skills.

    Populates :data:`_runner_skills_cache` on success so subsequent
    snapshot polls serve skills without a per-poll runner round-trip. Runs
    off the snapshot's critical path (see :func:`_fetch_runner_skills`).
    Best-effort: transport errors / non-200 / malformed payloads leave the
    cache unset so a later poll retries.

    :param runner_client: HTTP client pointed at the bound runner.
    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    """
    try:
        resp = await runner_client.get(
            f"/v1/sessions/{session_id}/skills",
            timeout=5.0,
        )
    except (httpx.HTTPError, ConnectionError):
        _logger.debug("Runner skills query failed for %s", session_id)
        return
    if resp.status_code != 200:
        return
    try:
        raw = resp.json().get("skills", [])
        skills = [SkillSummary(name=s["name"], description=s["description"]) for s in raw]
    except (ValueError, AttributeError, KeyError, TypeError):
        _logger.debug("Runner skills payload malformed for %s", session_id)
        return
    _runner_skills_cache[session_id] = skills
    # Nudge any subscribed client to re-read the (now-warm) snapshot so
    # its slash-command menu fills without waiting for the next bind.
    _publish_runner_skills(session_id)


def _model_options_from_wire(raw_models: Any) -> list[dict[str, Any]]:
    """
    Validate runner-returned raw native ``model/list`` data.

    :param raw_models: JSON value from the runner's
        ``{"models": [...]}`` response, e.g. a list of model dicts.
    :returns: Raw model options for the session snapshot; malformed rows
        are skipped so one bad provider row cannot blank the picker.
    :raises ValueError: If the payload is not a list.
    """
    if not isinstance(raw_models, list):
        raise ValueError("Native model options payload must be a list")
    options: list[dict[str, Any]] = []
    for raw_model in raw_models:
        # Skip malformed rows instead of discarding the whole catalog: one
        # provider-supplied oddity must not blank the picker for the session.
        if not isinstance(raw_model, dict):
            continue
        try:
            option = NativeModelOption.model_validate(raw_model)
        except ValidationError:
            _logger.debug("Skipping malformed native model option: %r", raw_model)
            continue
        options.append(option.model_dump(exclude_defaults=True, exclude_none=True))
    return options


async def _load_model_options(
    runner_client: httpx.AsyncClient,
    session_id: str,
    path: str,
) -> None:
    """
    Background single-flight fetch of a session's native model catalog.

    :param runner_client: HTTP client pointed at the bound runner.
    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :param path: Runner route to query, e.g.
        ``"/v1/sessions/conv_abc/cursor-model-options"``.
    """
    # Read the retry schedule off the facade so tests patching
    # ``sessions._MODEL_OPTIONS_RETRY_DELAYS_S`` reach this impl.
    import omnigent.server.routes.sessions as _facade

    delays = _facade._MODEL_OPTIONS_RETRY_DELAYS_S
    for attempt in range(len(delays) + 1):
        try:
            resp = await runner_client.get(path, timeout=5.0)
        except (httpx.HTTPError, ConnectionError):
            _logger.debug("Runner model-options query failed for %s", session_id)
            return
        if resp.status_code != 200:
            # 503 means the native backend (Codex app-server bridge / cursor
            # login) is still booting. Keep the background single-flight alive
            # so the web picker fills without a second manual refresh.
            if resp.status_code == 503 and attempt < len(delays):
                await asyncio.sleep(delays[attempt])
                continue
            return
        try:
            options = _model_options_from_wire(resp.json().get("models", []))
        except (ValueError, KeyError, TypeError, ValidationError):
            _logger.debug("Runner model-options payload malformed for %s", session_id)
            return
        if not options:
            # Older runners returned 200 + [] for the same not-ready window.
            # Do not cache that empty catalog; retry, then leave the cache
            # cold so a later snapshot can try again.
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt])
                continue
            return
        _model_options_cache[session_id] = options
        _publish_model_options(session_id)
        return


__all__ = [
    "SessionLiveness",
    "_HostLaunchAttempt",
    "_NativeTerminalEnsureOutcome",
    "_RunnerForwardResult",
    "_SessionEventDispatchResult",
    "_add_model_usage_delta",
    "_agent_carries_cursor_fork_history",
    "_agent_carries_native_fork_history",
    "_agent_is_native",
    "_agent_provider_family",
    "_allow_all_edits_eligible",
    "_allow_remember_eligible",
    "_ancestor_session_ids",
    "_announce_session_added",
    "_apply_liveness_to_items",
    "_apply_pending_policy_ask_writes",
    "_attachment_disposition",
    "_authorize_bundled_parent_and_inherit_runner",
    "_await_settled_managed_launch",
    "_build_actor",
    "_build_evaluation_context",
    "_build_new_item",
    "_build_policy_engine_from_spec",
    "_build_skill_slash_command_policy_body",
    "_canonical_tool_input",
    "_child_session_current_task_status_from_cached_status",
    "_child_session_summary_from_conversation",
    "_claude_native_remember_host",
    "_client_supplied_hook_elicitation_id",
    "_codex_plan_mode_enabled",
    "_codex_subagent_display_tool",
    "_codex_subagent_labels_from_body",
    "_coerce_cumulative_field",
    "_collect_descendant_conversation_ids",
    "_compact_lock",
    "_consume_pre_resolved_harness_elicitation",
    "_create_and_publish_codex_child",
    "_create_session_worktree",
    "_delete_stored_session_bundle_after_failure",
    "_derive_terminal_launch_args_from_spec",
    "_descendant_sessions",
    "_discovery_key",
    "_dispatch_skill_slash_command_to_runner",
    "_emit_server_routing_decision",
    "_error_item_from_sse",
    "_evaluate_output_policy",
    "_extract_assistant_text_from_event",
    "_extract_claude_native_runner_failure",
    "_extract_persistent_item_from_sse",
    "_extract_user_text_for_routing",
    "_extract_user_text_from_event",
    "_find_claude_native_subagent_child",
    "_find_codex_native_subagent_child",
    "_find_subagent_child_by_title",
    "_flush_relay_text",
    "_format_sse",
    "_forward_approval_to_runner",
    "_forward_session_change_to_runner",
    "_get_runner_client",
    "_get_runner_client_for_resource_access",
    "_handle_advise_models_mcp",
    "_handle_external_session_todos",
    "_handle_mcp_tools_list",
    "_invalidate_runner_backed_snapshot_state",
    "_is_codex_native_subagent",
    "_is_kiro_native_session",
    "_last_task_error_from_labels",
    "_latest_assistant_text_from_store",
    "_latest_message_preview",
    "_launch_runner_on_host",
    "_load_agent_spec_for_session",
    "_load_model_options",
    "_load_runner_skills",
    "_mcp_error_response",
    "_mcp_input_required_response",
    "_mcp_ok_response",
    "_mcp_tool_result",
    "_merge_pending_file_blocks",
    "_message_text",
    "_model_options_from_wire",
    "_model_usage_bucket",
    "_multipart_missing_detail",
    "_native_ask_gate_lock",
    "_native_coding_agent_for_agent",
    "_native_coding_agent_for_session",
    "_native_subagent_wrapper_labels_from_spec",
    "_native_terminal_ensure_transport_error",
    "_native_terminal_failure_from_runner_response",
    "_native_terminal_name_for_harness",
    "_notify_runner_of_bundled_child",
    "_owner_from_grants",
    "_parse_external_assistant_message",
    "_parse_external_conversation_item",
    "_parse_session_create_metadata",
    "_parse_skill_slash_command",
    "_pending_elicitation_snapshot_for_session",
    "_permission_level_from_grants",
    "_persist_external_assistant_message",
    "_persist_external_codex_collaboration_mode_change",
    "_persist_external_model_change",
    "_persist_external_model_options",
    "_persist_external_reasoning_effort_change",
    "_persist_external_subagent_start",
    "_persist_native_policy_notice",
    "_persist_policy_deny_sentinel",
    "_persist_session_status_error_labels",
    "_persist_stored_session_bundle",
    "_policy_notice_from_ensure_response",
    "_poll_request_disconnect",
    "_presentation_labels_for_agent",
    "_priced_cost_for_display",
    "_provision_managed_sandbox",
    "_proxy_get_session_resources_to_runner",
    "_prune_pre_resolved_harness_elicitations",
    "_prune_session_read_state",
    "_publish_and_persist_resource_event",
    "_publish_changed_files_invalidated",
    "_publish_collaboration_mode",
    "_publish_compaction_completed",
    "_publish_compaction_failed",
    "_publish_compaction_in_progress",
    "_publish_elicitation_request_to_ancestors",
    "_publish_elicitation_resolved",
    "_publish_elicitation_resolved_to_ancestors",
    "_publish_error_event",
    "_publish_external_assistant_message",
    "_publish_external_conversation_item",
    "_publish_external_output_reasoning_delta",
    "_publish_external_output_text_delta",
    "_publish_external_tool_output_delta",
    "_publish_input_consumed",
    "_publish_input_deny_terminal",
    "_publish_interrupted",
    "_publish_mcp_startup",
    "_publish_model_options",
    "_publish_policy_denied",
    "_publish_policy_deny",
    "_publish_runner_skills",
    "_publish_sandbox_status",
    "_publish_session_created",
    "_publish_session_superseded",
    "_publish_status",
    "_publish_terminal_pending",
    "_query_host_runner_status",
    "_read_state_entry",
    "_read_upload_capped",
    "_record_daily_cost",
    "_registered_runner_id",
    "_reject_reserved_cost_control_label_seed",
    "_reject_server_reserved_label_seed",
    "_relay_persist",
    "_relay_persist_error_once",
    "_remove_session_worktree_best_effort",
    "_replace_text_in_message_body",
    "_require_collaboration_mode_forward",
    "_require_cost_control_label_authority",
    "_require_external_status_forward",
    "_require_host_conn_for_worktree",
    "_reset_runner_resources_after_switch",
    "_resolve_harness",
    "_resolve_llm_model",
    "_resolve_skill_meta_text_via_runner",
    "_resolve_subagent_spec",
    "_resource_event_item_from_sse",
    "_routing_decision_item_from_sse",
    "_run_compact_locked",
    "_same_provider_family",
    "_seed_missing_title",
    "_seed_missing_title_from_user_message",
    "_session_status_from_cache",
    "_session_status_with_child_rollup",
    "_set_read_state",
    "_signal_harness_elicitation_resolved_by_id",
    "_signal_terminal_resolved_harness_elicitation",
    "_spec_config_flag_explicitly_disabled",
    "_spec_harness",
    "_stop_session_host_runner",
    "_stop_session_via_runner",
    "_stored_file_to_resource",
    "_stream_live_events",
    "_structured_ask_user_question",
    "_subagent_delivery_status",
    "_targeted_elicitation_event",
    "_title_content_from_item",
    "_truncate_label",
    "_usage_by_model_for_display",
    "_utc_day",
    "_validate_external_reasoning_effort",
    "_validate_session_workspace",
    "_validate_terminal_launch_args",
    "_validated_cost_control_mode_override",
    "_validated_harness_override",
    "_validated_harness_override_executor_type",
    "_wait_for_managed_runner_tunnel",
    "_wait_for_runner_client",
    "announce_hosts_changed",
    "cancel_managed_launch_tasks",
]
