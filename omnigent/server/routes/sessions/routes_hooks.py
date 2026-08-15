"""Hook routes: permission requests, policy evaluation, elicitation hooks."""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple

from fastapi import (
    APIRouter,
    Depends,
    Request,
    status,
)
from fastapi.responses import Response

from omnigent.codex_native_elicitation import codex_elicitation_id
from omnigent.entities import Conversation
from omnigent.errors import ElicitationDeclinedError, ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import (
    get_agent_cache,
    get_caps,
    get_policy_store,
    pending_inputs,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.runtime.policies.builder import (
    any_policies_apply,
    build_policy_engine,
)
from omnigent.runtime.policies.engine import PolicyEngine
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
    LEVEL_READ,
    AuthProvider,
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
from omnigent.server.routes._codex_elicitation import parse_codex_elicitation_request
from omnigent.server.routes._content_type import (
    require_json_content_type,
)
from omnigent.server.routes._sessions.common import (
    _EVALUATE_HOOK_ELICITATION_ID_RE,
    _TURN_ACTOR_LABEL,
    _logger,
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import (
    _allow_all_edits_eligible,
    _allow_remember_eligible,
    _build_actor,
    _build_evaluation_context,
    _claude_native_remember_host,
    _client_supplied_hook_elicitation_id,
    _emit_server_routing_decision,
    _forward_session_change_to_runner,
    _get_runner_client,
    _native_ask_gate_lock,
    _publish_policy_denied,
    _structured_ask_user_question,
)
from omnigent.server.routes._sessions.orchestration import (
    _hold_native_ask_gate,
    _publish_and_wait_for_harness_elicitation,
    _spawn_gateway_backed,
    _spawn_native_blocked_notice_forward,
)
from omnigent.server.schemas import (
    ElicitationRequestParams,
)
from omnigent.spec.types import (
    Phase,
    PolicyAction,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore


def _create_route_decision_id(
    session_id: str,
    conversation_store: ConversationStore,
) -> str | None:
    """Return the decision id of a session's create-time routing card.

    The create emits the card but leaves the route-once label unclaimed,
    because the prompt the user finally submits may not be the one it routed.
    When it IS the same prompt, the first-prompt hook claims this decision
    rather than making a second one.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store exposing ``list_items``.
    :returns: The applied ``"session"``-scope decision id, or ``None`` when
        the session has none.
    """
    try:
        page = conversation_store.list_items(session_id, type="routing_decision", order="asc")
    except (OSError, ValueError):
        _logger.warning(
            "route-turn: could not read session=%s routing decisions", session_id, exc_info=True
        )
        return None
    for item in page.data:
        data = item.data
        if getattr(data, "scope", None) == "session" and getattr(data, "applied", False):
            decision_id = getattr(data, "decision_id", None)
            if isinstance(decision_id, str) and decision_id:
                return decision_id
    return None


def register_hooks_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
) -> None:
    """Register the hooks routes on router."""

    @router.post(
        "/sessions/{session_id}/hooks/permission-request",
        # Internal harness callback webhook — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def claude_permission_request_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Claude Code ``PermissionRequest`` HTTP hook endpoint.

        Receives Claude Code's PermissionRequest hook payload (tool
        name + input the user would otherwise see a TUI prompt for),
        publishes a ``response.elicitation_request`` SSE event on the
        session stream so the web UI's :file:`ApprovalCard` renders
        inline, and long-polls until the verdict arrives via the
        session ``approval`` event path.

        Response shape follows Claude Code's PermissionRequest hook
        contract: ``hookSpecificOutput.decision.behavior`` is
        ``"allow"`` or ``"deny"``. On timeout the endpoint returns
        ``200`` with an empty body — Claude Code treats that as
        "defer to the TUI prompt", which matches the wrapper's
        fail-ask contract (UI unreachable / unattended → fall back
        to terminal-side approval).

        Auth: standard session ACL — the wrapper's outbound headers
        (``ap_auth_headers`` in :func:`build_hook_settings`) carry
        the same Bearer token used for every other Omnigent request. For
        local-server mode (no auth provider), unauth'd calls are
        allowed.

        :param request: FastAPI request — body is Claude Code's
            PermissionRequest payload as JSON.
        :param session_id: Omnigent conversation id from the URL path.
        :returns: Claude PermissionRequest hookSpecificOutput JSON,
            or ``200`` with empty body on timeout (fail-ask).
        :raises OmnigentError: 404 if the session doesn't exist,
            400 if the body fails JSON parse or is missing
            ``tool_name``.
        """
        from omnigent.server.routes import sessions as _sf

        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in PermissionRequest hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "PermissionRequest hook body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            raise OmnigentError(
                "PermissionRequest hook body must include a non-empty 'tool_name' string.",
                code=ErrorCode.INVALID_INPUT,
            )
        tool_input = payload.get("tool_input")
        if tool_input is not None and not isinstance(tool_input, dict):
            raise OmnigentError(
                "PermissionRequest hook body 'tool_input' must be an object when present.",
                code=ErrorCode.INVALID_INPUT,
            )
        # Claude Code's PermissionRequest payload carries no
        # ``tool_use_id`` (verified against a real payload — the field
        # is absent, not merely unstable; the id is only minted when the
        # tool call is emitted, AFTER this permission check). And newer
        # builds can write the transcript ``function_call`` (tool_use)
        # before this hook returns — so neither can correlate/resolve the
        # parked request. The parked wait ends on one of three signals: an
        # explicit web verdict, hook disconnect, or the mirrored
        # ``function_call_output`` (tool_result) for this gated tool,
        # which — unlike the tool_use — is written only AFTER the
        # prompt was answered in the TUI. We pass ``tool_name`` /
        # ``tool_input`` below so that result can be correlated back to
        # THIS prompt (see _signal_terminal_resolved_harness_elicitation).
        cwd = payload.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            cwd = None
        permission_mode = payload.get("permission_mode")
        if permission_mode is not None and not isinstance(permission_mode, str):
            permission_mode = None
        elicitation_id = _client_supplied_hook_elicitation_id(payload, session_id)

        try:
            preview_str = json.dumps(tool_input or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            preview_str = repr(tool_input)
        preview_str = preview_str[:1024]

        # ``extra="allow"`` on ElicitationRequestParams permits
        # extra keyword arguments to ride alongside the MCP
        # standard fields. Use it for Claude-native display and
        # correlation hints rather than minting AP-specific fields
        # on the model; strict MCP clients can ignore unknown fields
        # while AP's UI consumes them.
        # ``tool_name`` rides along so the UI can render the
        # permission card with the gated tool name and distinguish
        # simultaneous prompts from different tools.
        extras: dict[str, Any] = {"tool_name": tool_name}
        if cwd is not None:
            extras["cwd"] = cwd
        if permission_mode is not None:
            extras["permission_mode"] = permission_mode
        # The card offers ONE persistent-approval affordance, picked by
        # the gated tool — the two hints below are mutually exclusive
        # (disjoint eligibility), never two buttons competing on one card.
        #
        # Edit tools → "Accept & allow all edits" (switches the session to
        # acceptEdits via setMode). Stamped only for edit-tool prompts
        # under a still-prompting mode — see _allow_all_edits_eligible.
        # The verdict site re-checks the same predicate before honoring it.
        if _allow_all_edits_eligible(tool_name, permission_mode):
            extras["allow_all_edits"] = True
        # Non-edit eligible tools → "don't ask again" (installs a
        # session-scoped allow rule via addRules). Stamped only when the
        # affordance applies — see _allow_remember_eligible.
        # ``remember_scope`` carries the gated tool and, for WebFetch, the
        # request host so the UI can label the button ("… for github.com"
        # vs "… for WebFetch"); the verdict site re-derives the same scope
        # before honoring the flag, never trusting a client-supplied rule.
        if _allow_remember_eligible(tool_name, permission_mode):
            remember_scope: dict[str, Any] = {"tool": tool_name}
            remember_host = _claude_native_remember_host(tool_name, tool_input)
            if remember_host is not None:
                remember_scope["host"] = remember_host
            extras["remember_scope"] = remember_scope
        # When Claude's built-in AskUserQuestion tool is the one
        # needing permission, the PermissionRequest payload
        # already carries the full questions + options structure
        # in ``tool_input``. Surface it as a structured extra so
        # the UI can render an interactive form WITHOUT having to
        # parse the (truncated) ``content_preview`` JSON blob.
        # ``content_preview`` keeps its 1024-char cap for the
        # binary-card fallback; the structured field is the
        # authoritative source the UI consumes when present.
        if tool_name == "AskUserQuestion":
            ask_payload = _structured_ask_user_question(tool_input)
            if ask_payload is not None:
                extras["ask_user_question"] = ask_payload
        # When the gated tool is ExitPlanMode, ride the full
        # ``tool_input`` through verbatim so the UI can render a
        # dedicated plan-review card. ``content_preview`` is
        # hard-capped at 1024 chars — real plans blow well past it —
        # and the input's shape varies across Claude Code builds
        # (``plan`` markdown, ``allowedPrompts``, ...), so no field
        # filtering: every field the hook carried natively reaches
        # the UI. An empty/absent input stamps nothing, leaving the
        # binary-card fallback.
        if tool_name == "ExitPlanMode" and isinstance(tool_input, dict) and tool_input:
            extras["exit_plan_mode"] = tool_input
        params = ElicitationRequestParams(
            mode="form",
            message=f"Claude wants to call **{tool_name}**",
            requestedSchema=None,
            url=None,
            phase="pre_tool_use",
            policy_name="claude_native_permission",
            content_preview=f"{tool_name}({preview_str})",
            **extras,
        )
        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=params,
            timeout_s=_sf._CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S,
            conversation_store=conversation_store,
            # Client-minted stable id so a retry re-parks the same elicitation.
            elicitation_id=elicitation_id,
            # Tool identity lets a mirrored tool result for this gated
            # tool resolve the prompt promptly when the user answers in
            # Claude's TUI instead of the web UI (terminal-resolved
            # fast path). ``tool_input`` is the dict from the payload
            # (or None when absent).
            tool_name=tool_name,
            tool_input=tool_input if isinstance(tool_input, dict) else None,
        )
        if result is None:
            # Disconnect or timeout. Either way Claude is no
            # longer waiting on this response; empty 2xx → Claude
            # defers to its built-in TUI prompt (fail-ask).
            return Response(status_code=status.HTTP_200_OK)

        behavior = "allow" if result.action == "accept" else "deny"
        decision: dict[str, Any] = {"behavior": behavior}
        # A decline can carry feedback typed into the web card (the
        # ExitPlanMode "Reject with feedback" flow). Claude's
        # PermissionRequest decision contract surfaces it via
        # ``decision.message`` — the model sees it as the denial
        # reason, so for a rejected plan Claude stays in plan mode
        # and revises toward the feedback instead of guessing why
        # the plan was refused.
        if behavior == "deny" and isinstance(result.content, dict):
            feedback = result.content.get("feedback")
            if isinstance(feedback, str) and feedback.strip():
                decision["message"] = feedback
        # When the gated tool is AskUserQuestion AND the user accepted
        # with selections, propagate those selections back to Claude
        # via ``decision.updatedInput``. Claude reads
        # ``tool_input.answers`` and skips its TUI picker, returning
        # the supplied selections as the tool result the LLM sees.
        #
        # ``result.content`` is MCP-shaped (a flat ``{[field]: value}``
        # map) — exactly the shape ``tool_input.answers`` expects on
        # AskUserQuestion. Single-select values are strings,
        # multi-select are ``list[str]``; both ride through verbatim.
        if (
            behavior == "allow"
            and tool_name == "AskUserQuestion"
            and isinstance(tool_input, dict)
            and isinstance(result.content, dict)
            and result.content
        ):
            decision["updatedInput"] = {**tool_input, "answers": result.content}
        # "Accept & allow all edits" — the user approved this edit AND
        # asked to auto-accept future edits. Echo a ``setMode`` permission
        # update so Claude Code switches this session into ``acceptEdits``
        # mode, exactly as the native shift+tab toggle does. The
        # ``updatedPermissions`` shape matches the Agent SDK's
        # ``PermissionUpdate`` union (``{type, mode, destination}`` for
        # ``setMode``); ``destination: "session"`` scopes it to this
        # session, so it resets on the next one.
        #
        # Re-check eligibility server-side rather than trusting the
        # client's ``content.allow_all_edits`` flag alone: the flag is
        # only meaningful for the edit-tool / prompting-mode prompts the
        # affordance was offered for. Without this, a client could send
        # the flag on e.g. a Bash prompt and flip the session into
        # ``acceptEdits`` — a mode switch it was never offered.
        if (
            behavior == "allow"
            and isinstance(result.content, dict)
            and result.content.get("allow_all_edits") is True
            and _allow_all_edits_eligible(tool_name, permission_mode)
        ):
            decision["updatedPermissions"] = [
                {
                    "type": "setMode",
                    # The plan card's "Yes, and use auto mode" switches the
                    # session into Claude's ``auto`` mode; the edit-tool
                    # "Accept & allow all edits" keeps the narrower
                    # ``acceptEdits`` (auto-approve edits only).
                    "mode": "auto" if tool_name == "ExitPlanMode" else "acceptEdits",
                    "destination": "session",
                }
            ]
        elif behavior == "allow" and tool_name == "ExitPlanMode":
            # Plan approved WITHOUT auto mode — the card's "Yes,
            # manually approve edits". Pin the session to the prompting
            # ``default`` mode instead of trusting whatever mode
            # Claude's plan-exit restores, so every subsequent edit
            # prompts exactly as the button promised. De-escalation
            # only (most restrictive prompting mode), so no eligibility
            # gate is needed.
            decision["updatedPermissions"] = [
                {"type": "setMode", "mode": "default", "destination": "session"}
            ]
        # "Approve & don't ask again" — the user approved this non-edit
        # tool AND asked to stop prompting for the same scope. Echo an
        # ``addRules`` permission update so Claude Code installs a
        # session-scoped allow rule, exactly as the native TUI's "don't
        # ask again" option does. The shape matches the Agent SDK's
        # ``PermissionUpdate`` union (``addRules``): ``rules`` is a list
        # of ``{toolName, ruleContent?}`` — ``ruleContent`` omitted means
        # the whole tool; ``destination: "session"`` scopes it to this
        # session so it resets on the next one. The claude-native hook
        # forwards this decision verbatim to Claude Code.
        #
        # The host is re-derived server-side from the gated tool's input
        # rather than trusting any client-supplied rule, and gated by the
        # same ``_allow_remember_eligible`` predicate the button was
        # offered under — so a forged ``remember`` flag on an ineligible
        # tool (e.g. an edit tool, which takes the setMode path) can't
        # smuggle in an allow rule. Mutually exclusive with the edit-tool
        # ``allow_all_edits``/ExitPlanMode branches above (disjoint tool
        # sets), so it never overwrites their ``updatedPermissions``.
        if (
            behavior == "allow"
            and isinstance(result.content, dict)
            and result.content.get("remember") is True
            and _allow_remember_eligible(tool_name, permission_mode)
        ):
            rule: dict[str, Any] = {"toolName": tool_name}
            remember_host = _claude_native_remember_host(tool_name, tool_input)
            if remember_host is not None:
                rule["ruleContent"] = f"domain:{remember_host}"
            decision["updatedPermissions"] = [
                {
                    "type": "addRules",
                    "rules": [rule],
                    "behavior": "allow",
                    "destination": "session",
                }
            ]
        body = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision,
            },
        }
        return Response(
            content=json.dumps(body),
            media_type="application/json",
        )

    # ── Proto event-type → validation schema, as ONE total mapping ────
    #
    # This used to be three independent structures: which wire types are
    # accepted, which phases require an object payload, and which phases
    # need a tool name and where it may come from. The docstring on each
    # said "one schema per phase" while the code let you add a wire type to
    # the first table alone — the new phase was then accepted, validated as
    # permissively as possible (``dict | str``), and got no tool-name rule
    # at all, because the other two tables simply had no entry for it and
    # every lookup against them used a permissive default (``in a
    # frozenset`` / ``.get(phase, ())``).
    #
    # A NamedTuple with no defaults on either field makes that impossible:
    # you cannot add a wire type without also deciding, right there, both
    # whether its payload must be an object and where its tool name (if any)
    # comes from. There is deliberately no fallback path for a phase this
    # dict doesn't recognize — the "Unknown event type" branch below is the
    # only way to reach past it, and it 400s rather than silently allowing.
    #
    # ``data`` shape: tool and LLM phases carry a structured payload, so a
    # string there is a malformed call. REQUEST carries prompt text and is
    # the only phase where a bare string is a legitimate wire form.
    #
    # Tool name containers are listed in precedence order because two
    # first-party producers spell it differently: claude-native and the
    # in-process tool dispatch send ``request_data.name``, while the OpenCode
    # plugin sends the tool in ``event.target``. Both are established wire
    # shapes; requiring one spelling 400s the other, and that producer treats
    # any non-2xx as ALLOW — so a stricter guard silently disabled its
    # policies instead of tightening them.
    class _PhaseSchema(NamedTuple):
        phase: Phase
        data_must_be_object: bool
        tool_name_sources: tuple[tuple[str, str], ...]

    _PHASE_SCHEMA_BY_WIRE_TYPE: dict[str, _PhaseSchema] = {
        "PHASE_TOOL_CALL": _PhaseSchema(
            phase=Phase.TOOL_CALL,
            data_must_be_object=True,
            tool_name_sources=(("data", "event.data.name"),),
        ),
        "PHASE_TOOL_RESULT": _PhaseSchema(
            phase=Phase.TOOL_RESULT,
            data_must_be_object=True,
            tool_name_sources=(
                ("request_data", "event.request_data.name"),
                ("target", "event.target"),
            ),
        ),
        "PHASE_LLM_REQUEST": _PhaseSchema(
            phase=Phase.LLM_REQUEST,
            data_must_be_object=True,
            tool_name_sources=(),
        ),
        "PHASE_LLM_RESPONSE": _PhaseSchema(
            phase=Phase.LLM_RESPONSE,
            data_must_be_object=True,
            tool_name_sources=(),
        ),
        # A native session's UserPromptSubmit hook posts the request phase
        # here (the server-level _evaluate_input_policy skips native message
        # events). The prompt text rides in ``event.data.text``, and this is
        # the only phase where ``data`` as a bare string is legitimate.
        "PHASE_REQUEST": _PhaseSchema(
            phase=Phase.REQUEST,
            data_must_be_object=False,
            tool_name_sources=(),
        ),
    }
    _PHASE_TO_PROTO_ACTION: dict[PolicyAction, str] = {
        PolicyAction.ALLOW: "POLICY_ACTION_ALLOW",
        PolicyAction.DENY: "POLICY_ACTION_DENY",
        PolicyAction.ASK: "POLICY_ACTION_ASK",
    }

    # ── POST /sessions/{session_id}/policies/evaluate ─────────────

    @router.post(
        "/sessions/{session_id}/policies/evaluate",
        # Returns EvaluationResponse JSON; no Pydantic model since the
        # proto-style schema is validated manually.
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def evaluate_policy(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Generic policy evaluation endpoint (proto-compatible).

        Accepts an ``EvaluationRequest`` JSON body whose ``event``
        field carries the phase (``PHASE_TOOL_CALL``,
        ``PHASE_TOOL_RESULT``, ``PHASE_LLM_REQUEST``,
        ``PHASE_LLM_RESPONSE``), the event data, and optional
        context. Returns an ``EvaluationResponse`` with the policy
        verdict (``result``), an optional ``reason``, and optional
        ``data`` for content-rewriting policies.

        Used by Claude Code's ``PreToolUse`` and ``PostToolUse``
        command hooks (via ``omnigent.claude_native_hook``) to
        evaluate admin policies on native tool calls. Also usable
        by any client that speaks the proto-compatible JSON schema.

        :param request: FastAPI request — body is the
            ``EvaluationRequest`` JSON envelope.
        :param session_id: Omnigent conversation id from the URL path.
        :returns: ``EvaluationResponse`` JSON with ``result``,
            ``reason``, and optional ``data``.
        :raises OmnigentError: 404 if the session doesn't exist,
            400 if the body is malformed.
        """
        from omnigent.server.routes import sessions as _sf

        user_id = _sf._get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        is_read_only = access.level is not None and access.level < LEVEL_EDIT
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in policy evaluate body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "Policy evaluate body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        event = payload.get("event")
        if not isinstance(event, dict):
            raise OmnigentError(
                "Policy evaluate body must include an 'event' object.",
                code=ErrorCode.INVALID_INPUT,
            )
        event_type = event.get("type")
        if event_type is not None and not isinstance(event_type, str):
            # An unhashable type (list / dict) would raise inside the phase
            # lookup below and surface as a 500 instead of a client error.
            raise OmnigentError(
                f"Policy evaluate 'event.type' must be a string; got {type(event_type).__name__}.",
                code=ErrorCode.INVALID_INPUT,
            )
        schema = _PHASE_SCHEMA_BY_WIRE_TYPE.get(event_type or "")
        if schema is None:
            raise OmnigentError(
                f"Unknown event type: {event_type!r}. "
                f"Expected one of {list(_PHASE_SCHEMA_BY_WIRE_TYPE)}.",
                code=ErrorCode.INVALID_INPUT,
            )
        phase = schema.phase
        # Optional stable re-attach id for hook retries. Validated but not
        # required — absent on non-retrying callers (old hooks, direct API use).
        raw_elicitation_id = payload.get("_omnigent_elicitation_id")
        hook_elicitation_id: str | None = None
        if raw_elicitation_id is not None:
            if not isinstance(raw_elicitation_id, str) or not (
                _EVALUATE_HOOK_ELICITATION_ID_RE.fullmatch(raw_elicitation_id)
            ):
                raise OmnigentError(
                    "Policy evaluate '_omnigent_elicitation_id' must match "
                    "'elicit_evaluate_' + 32 hex chars.",
                    code=ErrorCode.INVALID_INPUT,
                )
            hook_elicitation_id = raw_elicitation_id
        # Validation is driven from the per-phase schema above rather than
        # from a chain of conditionals: rules that live in branches get
        # applied to whichever phase happens to reach that branch. The three
        # rules below are independent and all of them run for every phase.
        #
        # Everything is checked BEFORE normalizing. ``or {}`` turns null,
        # false, 0, "" and [] into an empty object, and a tool phase
        # evaluating with no name gates with ``tool_name=None``, which skips
        # every tool-scoped policy — on a hook that blocks, that fails open.
        raw_data = event.get("data")
        if schema.data_must_be_object:
            # An absent payload is as malformed as a wrongly typed one here:
            # ``None`` normalized to ``{}`` and evaluated as an empty call,
            # which was the original defect's headline vector.
            if not isinstance(raw_data, dict):
                raise OmnigentError(
                    f"Policy evaluate 'event.data' must be an object for "
                    f"{event_type!r}; got {type(raw_data).__name__}.",
                    code=ErrorCode.INVALID_INPUT,
                )
        elif not isinstance(raw_data, (dict, str)):
            # Absent is malformed here too: every first-party producer of this
            # phase sends the prompt text, and ``None`` normalized to ``{}``
            # gates on an empty prompt.
            raise OmnigentError(
                f"Policy evaluate 'event.data' must be an object or string for "
                f"{event_type!r}; got {type(raw_data).__name__}.",
                code=ErrorCode.INVALID_INPUT,
            )
        # A tool-scoped gate cannot run without a tool name, and allowing by
        # default is the wrong direction on a blocking hook. Producers spell
        # the name in different places, so any declared source satisfies the
        # rule; the resolved name is written back to the container the engine
        # reads, so the guard and the gate agree on it.
        name_sources = schema.tool_name_sources
        if name_sources:
            resolved_name: str | None = None
            for container_key, _field_path in name_sources:
                if container_key == "data":
                    candidate = raw_data.get("name") if isinstance(raw_data, dict) else None
                elif container_key == "target":
                    candidate = event.get("target")
                else:
                    container = event.get(container_key)
                    candidate = container.get("name") if isinstance(container, dict) else None
                if isinstance(candidate, str) and candidate:
                    resolved_name = candidate
                    break
            if resolved_name is None:
                paths = " or ".join(repr(path) for _key, path in name_sources)
                raise OmnigentError(
                    f"Policy evaluate requires a non-empty tool name in {paths} "
                    f"for {event_type!r}.",
                    code=ErrorCode.INVALID_INPUT,
                )
            primary_key = name_sources[0][0]
            if primary_key != "data":
                # Normalize onto the container the engine reads, so a producer
                # that names the tool elsewhere gets tool-scoped policies too
                # instead of merely passing validation.
                primary = event.get(primary_key)
                event[primary_key] = (
                    {**primary, "name": resolved_name}
                    if isinstance(primary, dict)
                    else {"name": resolved_name}
                )
        data = raw_data or {}
        raw_event_context = event.get("context")
        if raw_event_context is not None and not isinstance(raw_event_context, dict):
            raise OmnigentError(
                f"Policy evaluate 'event.context' must be an object; got "
                f"{type(raw_event_context).__name__}.",
                code=ErrorCode.INVALID_INPUT,
            )

        # Reuse the row the ACL check already fetched — same point in the
        # request, so no less fresh than reading it again here, one query
        # fewer on the blocking PreToolUse path. Absent for admin callers
        # (who bypass the conversation lookup) and when permissions are
        # disabled, which fall back to their own read.
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise OmnigentError(
                f"Session {session_id!r} not found.",
                code=ErrorCode.NOT_FOUND,
            )
        # Dedup the native request-phase gate. A native session's
        # ``UserPromptSubmit`` hook posts ``PHASE_REQUEST`` here for *every*
        # prompt, but a web-UI prompt was already gated server-side by
        # ``_evaluate_input_policy`` at POST /events (before injection, so no
        # TUI freeze). Re-gating it here would double-prompt the human. A
        # web-UI prompt in flight has a ``pending_inputs`` entry (recorded at
        # dispatch, drained when the forwarder mirrors it back); a prompt
        # typed directly in the TUI has none and never hit POST /events, so it
        # is gated here — the hook is its only request-phase gate. The signal
        # is "is a web prompt in flight", not text correlation (the native
        # transcript gives no reliable id channel — see ``pending_inputs``).
        if phase == Phase.REQUEST and pending_inputs.snapshot_for(session_id):
            return Response(
                content=json.dumps({"result": "POLICY_ACTION_ALLOW"}),
                media_type="application/json",
            )
        agent = agent_store.get(conv.agent_id) if conv.agent_id else None
        if agent is None:
            # No agent — no policies. Return unspecified (pass-through).
            return Response(
                content=json.dumps({"result": "POLICY_ACTION_UNSPECIFIED"}),
                media_type="application/json",
            )

        loaded = _sf.get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.expands_server_env
        )

        _caps = _sf.get_caps()

        # Fast path: if no policies would fire (no agent guardrails, no
        # session policies, no server-wide defaults), skip the engine build
        # entirely. This avoids conversation-store reads for labels/state/usage
        # on every tool call for the common no-policy case. Session policies are
        # LRU-cached so this check is cheap after the first call per session.
        # Users can add policies mid-session — the cache is invalidated on
        # mutation, so newly added policies are visible on the very next call.
        if not any_policies_apply(
            spec=loaded.spec,
            conversation_id=session_id,
            default_policies=_caps.default_policies,
            policy_store=get_policy_store(),
            phase=phase,
            tool_name=data.get("name") if isinstance(data, dict) else None,
        ):
            return Response(
                content=json.dumps({"result": "POLICY_ACTION_ALLOW"}),
                media_type="application/json",
            )

        _host_conn = (
            _caps.policy_llm_connection_factory() if _caps.policy_llm_connection_factory else None
        )

        def _build_engine(preloaded_conv: Conversation | None = None) -> PolicyEngine:
            """
            Build a policy engine for this session from the loaded spec.

            Re-reads persisted ``session_state`` / usage from the store on
            every call: the engine snapshots that state at construction and
            does not re-query it during ``evaluate``, so a fresh build is the
            only way to observe a concurrent sibling's just-recorded approval.

            :param preloaded_conv: The conversation row this handler already
                loaded, passed on the FIRST build only to skip the builder's
                re-read. Rebuilds that must observe concurrent writes (the
                ASK-gate re-evaluation) pass ``None`` for a fresh read.
            :returns: A :class:`PolicyEngine` seeded with the latest
                persisted state for ``session_id``.
            """
            return build_policy_engine(
                spec=loaded.spec,
                conversation_id=session_id,
                conversation_store=conversation_store,
                conversation=preloaded_conv,
                # ``agent`` below was resolved from conv.agent_id; the builder
                # re-reads the row and fails closed if it was rebound since.
                expected_agent_id=agent.id,
                default_policies=_caps.default_policies,
                policy_store=get_policy_store(),
                server_llm=_caps.llm,
                host_connection=_host_conn,
            )

        engine = _build_engine(conv)
        # Use the turn-initiating human's identity (persisted at forward time)
        # so per-user policies gate on the correct actor even when the HTTP
        # caller is the runner's service-account credential.  Falls back to
        # user_id for direct API callers and native-terminal sessions (whose
        # turns go via _dispatch_session_event_to_runner, which does not write
        # this label).
        # Read the actor from the engine's label snapshot, not from the row
        # fetched at the top of this handler: the engine's labels come from a
        # read taken after the agent/spec load, so a turn-actor label written
        # in that window still gates on the right principal. (``agent_id``
        # cannot be treated the same way — it selects the spec the engine is
        # built from, so it is necessarily read first.)
        turn_actor = engine.labels.get(_TURN_ACTOR_LABEL)
        ctx = _build_evaluation_context(
            phase, data, event, actor=_build_actor(turn_actor or user_id)
        )
        result = await engine.evaluate(ctx, read_only=is_read_only)

        # URL-based elicitation for blocking phases: on a TOOL_CALL or
        # LLM_REQUEST ASK, hold the gate server-side rather than
        # returning ASK. Returning ASK makes the native hook emit
        # ``defer``, which a permissive ``permission_mode``
        # (acceptEdits / bypassPermissions) auto-approves — bypassing
        # the human. Instead we publish the approval elicitation, park
        # until the human resolves it via the resolve URL, and collapse
        # to a hard ALLOW / DENY so the caller never sees ASK.
        # TOOL_CALL, LLM_REQUEST, and REQUEST are the phases that can block
        # before the action proceeds (tool dispatch / LLM call / a native
        # session's user prompt via the UserPromptSubmit hook — which has no
        # ASK primitive of its own, so the server resolves ASK here).
        if result.action == PolicyAction.ASK and phase in (
            Phase.TOOL_CALL,
            Phase.LLM_REQUEST,
            Phase.REQUEST,
        ):
            if is_read_only:
                # Read-only callers must not enter the ASK gate — parking
                # creates an elicitation (a server-side mutation). Return
                # the ASK verdict directly so the caller sees the policy
                # decision without mutating the session.
                pass
            else:
                # Serialize concurrent native ASK gates for this (session, policy)
                # so parallel tool calls that all trip the same checkpoint prompt
                # the human once. The first ASK to win the lock parks; on approve
                # it records a checkpoint. Siblings then rebuild the engine and
                # re-evaluate UNDER the lock against that freshly persisted state —
                # an ALLOW (or now-hard DENY) collapses the ASK and falls through
                # without a second prompt. Held across the human wait by design;
                # a declined ASK records nothing, so siblings legitimately re-ask.
                deciding_policy = result.deciding_policy
                assert deciding_policy is not None
                async with _native_ask_gate_lock(session_id, deciding_policy):
                    engine = _build_engine()
                    result = await engine.evaluate(ctx, read_only=is_read_only)
                    if result.action == PolicyAction.ASK and phase in (
                        Phase.TOOL_CALL,
                        Phase.LLM_REQUEST,
                        Phase.REQUEST,
                    ):
                        try:
                            approved = await _hold_native_ask_gate(
                                request,
                                session_id=session_id,
                                phase=phase,
                                data=data,
                                engine=engine,
                                result=result,
                                conversation_store=conversation_store,
                                elicitation_id=hook_elicitation_id,
                            )
                        except ElicitationDeclinedError as exc:
                            # Explicit user decline: interrupt the native
                            # harness BEFORE returning the hook deny so the
                            # Escape key reaches Claude Code's tmux pane first.
                            # By the time the DENY response reaches the hook
                            # subprocess, the abort signal is already queued.
                            # Best-effort: forwarding failures are swallowed.
                            await _forward_session_change_to_runner(
                                session_id,
                                get_server_runner_router(),
                                {"type": "interrupt"},
                            )
                            decline_body = {
                                "result": "POLICY_ACTION_DENY",
                                "reason": exc.args[0] or "Approval was declined.",
                            }
                            return Response(
                                content=json.dumps(decline_body),
                                media_type="application/json",
                            )
                        approval_body: dict[str, Any] = (
                            {"result": "POLICY_ACTION_ALLOW"}
                            if approved
                            else {
                                "result": "POLICY_ACTION_DENY",
                                "reason": result.reason or "Approval was not granted.",
                            }
                        )
                        return Response(
                            content=json.dumps(approval_body),
                            media_type="application/json",
                        )
                # Re-evaluation collapsed the ASK (a sibling's approval recorded
                # the checkpoint) — fall through to the generic ALLOW/DENY handling
                # below with the rebuilt engine and updated result.

        if result.set_labels and not is_read_only:
            engine.apply_label_writes(result.set_labels)

        resp_body: dict[str, Any] = {
            "result": _PHASE_TO_PROTO_ACTION.get(result.action, "POLICY_ACTION_UNSPECIFIED"),
        }
        if result.reason:
            resp_body["reason"] = result.reason
        if result.data is not None:
            resp_body["data"] = result.data
        # A request-phase HARD DENY (no approve option) — surface the reason as a
        # dismissable tmux popup on the native pane. opencode hard-blocks the
        # prompt by its plugin throwing (rendered as a generic error), so this is
        # the clean explanation; the runner dispatch only pops for opencode
        # (claude/codex already show a clean UserPromptSubmit block). Best-effort.
        if result.action == PolicyAction.DENY and phase == Phase.REQUEST and not is_read_only:
            _spawn_native_blocked_notice_forward(
                session_id, result.reason or "Blocked by policy.", result.deciding_policy
            )
        # A tool-call DENY is decided synchronously here, so nothing else on the
        # stream reflects that the native tool was blocked. Publish a positive
        # signal so observers (web UI, capability bench) see the decision rather
        # than infer it from the blocked tool's absence. Observational, so it is
        # not gated on write access.
        if result.action == PolicyAction.DENY and phase == Phase.TOOL_CALL:
            _publish_policy_denied(session_id, result.reason or "Blocked by policy.", phase.value)
        return Response(
            content=json.dumps(resp_body),
            media_type="application/json",
        )

    # ── POST /sessions/{session_id}/hooks/codex-elicitation-request ─

    @router.post(
        "/sessions/{session_id}/hooks/codex-elicitation-request",
        # Internal harness callback webhook — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def codex_elicitation_request_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Codex app-server elicitation request endpoint.

        Receives server-to-client JSON-RPC request envelopes forwarded
        by ``omnigent codex`` (for example
        ``mcpServer/elicitation/request`` and
        ``item/tool/requestUserInput``), publishes the standard
        ``response.elicitation_request`` session event for the web UI,
        then waits for the session-scoped ``approval`` reply. This uses
        the same registry / publish / cleanup path as the Claude-native
        ``PermissionRequest`` hook so pending badges and disconnect
        handling stay consistent across native harnesses.

        :param request: FastAPI request carrying the Codex JSON-RPC
            request envelope.
        :param session_id: Omnigent conversation id from the URL path.
        :returns: Codex JSON-RPC ``result`` payload for the forwarded
            request, or ``200`` with empty body on timeout/disconnect.
        :raises OmnigentError: 404 if the session does not exist,
            400 if the request envelope is malformed or unsupported.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in Codex elicitation hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "Codex elicitation hook body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        codex_request = parse_codex_elicitation_request(payload)
        from omnigent.server.routes import sessions as _sf

        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=codex_request.params,
            timeout_s=_sf._CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S,
            conversation_store=conversation_store,
            elicitation_id=codex_elicitation_id(
                session_id,
                codex_request.method,
                codex_request.request_id,
            ),
        )
        if result is None:
            return Response(status_code=status.HTTP_200_OK)
        if result.action == "decline":
            # Explicit user decline: interrupt Codex before returning the
            # deny response, same as the Claude-native path. The await
            # ensures the abort signal reaches Codex before it processes
            # the decline result and lets the LLM continue.
            await _forward_session_change_to_runner(
                session_id,
                get_server_runner_router(),
                {"type": "interrupt"},
            )
        body = codex_request.build_response(result)
        return Response(
            content=json.dumps(body),
            media_type="application/json",
        )

    # ── POST /sessions/{session_id}/hooks/antigravity-elicitation-request ──

    @router.post(
        "/sessions/{session_id}/hooks/antigravity-elicitation-request",
        # Internal harness callback webhook — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def antigravity_elicitation_request_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Antigravity (agy) elicitation request endpoint.

        Receives ``{"elicitation_id": <str>, "params": <ElicitationRequestParams>}``
        from the interaction bridge (Task 8), which POSTs here when it
        surfaces an agy WAITING interaction for the web UI. Parks the call
        on the shared harness elicitation registry, emits the standard
        ``response.elicitation_request`` SSE event, waits for the session
        ``approval`` verdict, then returns the raw
        :class:`~omnigent.server.schemas.ElicitationResult` so the bridge
        can forward it to agy via ``HandleCascadeUserInteraction``.

        This is intentionally simpler than the Codex hook: the bridge
        (not the endpoint) builds the agy interaction payload via
        ``to_interaction_payload``, so this endpoint only passes back
        the verdict as-is.  The body shape is minimal and symmetric:
        ``elicitation_id`` from the bridge's deterministic id function
        (``agy_elicitation_id``), ``params`` as an
        :class:`~omnigent.server.schemas.ElicitationRequestParams` dict.

        :param request: FastAPI request carrying the agy elicitation body.
        :param session_id: Omnigent conversation id from the URL path.
        :returns: ``ElicitationResult`` JSON on user verdict; ``200`` with
            empty body on timeout/disconnect (bridge interprets as ``None``).
        :raises OmnigentError: 404 if the session does not exist, 400 if
            the request body is malformed.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in antigravity elicitation hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "Antigravity elicitation hook body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        elicitation_id = payload.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            raise OmnigentError(
                "Antigravity elicitation hook body must include a non-empty"
                " 'elicitation_id' string.",
                code=ErrorCode.INVALID_INPUT,
            )
        raw_params = payload.get("params")
        if not isinstance(raw_params, dict):
            raise OmnigentError(
                "Antigravity elicitation hook body must include a 'params' object.",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            params = ElicitationRequestParams.model_validate(raw_params)
        except Exception as exc:
            raise OmnigentError(
                f"Invalid 'params' in antigravity elicitation hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        from omnigent.server.routes import sessions as _sf

        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=params,
            timeout_s=_sf._ANTIGRAVITY_NATIVE_ELICITATION_HOOK_TIMEOUT_S,
            conversation_store=conversation_store,
            elicitation_id=elicitation_id,
        )
        if result is None:
            return Response(status_code=status.HTTP_200_OK)
        if result.action == "decline":
            # Explicit user decline: interrupt the native harness before
            # returning the decline so the abort signal arrives first.
            await _forward_session_change_to_runner(
                session_id,
                get_server_runner_router(),
                {"type": "interrupt"},
            )
        return Response(
            content=result.model_dump_json(),
            media_type="application/json",
        )

    # ── POST /sessions/{session_id}/hooks/cursor-permission-request ─

    @router.post(
        "/sessions/{session_id}/hooks/cursor-permission-request",
        # Internal harness callback webhook — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def cursor_permission_request_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Cursor-native tool-approval hook (TUI → web elicitation).

        Receives a tool-approval prompt detected on the ``cursor-agent`` TUI
        pane by the runner-side mirror
        (:mod:`omnigent.cursor_native_permissions`), publishes the standard
        ``response.elicitation_request`` event for the web UI, then parks for
        the session ``approval`` verdict — the same registry / publish /
        cleanup path as the Codex- and Claude-native hooks, so pending badges
        and disconnect handling stay consistent across native harnesses. An
        empty ``200`` (no web verdict — the prompt was answered in the TUI, or
        the wait timed out) leaves cursor's native prompt authoritative.

        :param request: FastAPI request carrying the detected prompt
            (``elicitation_id`` plus the ``message`` / ``content_preview`` /
            ``operation_type`` to render).
        :param session_id: Omnigent conversation id from the URL path.
        :returns: An ``ElicitationResult`` (``{"action": …}``) on a web
            verdict, or ``200`` with empty body on TUI-resolution / timeout /
            disconnect.
        :raises OmnigentError: 404 if the session does not exist, 400 if the
            body is malformed.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in cursor permission hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "Cursor permission hook body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        elicitation_id = payload.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            raise OmnigentError(
                "Cursor permission hook body must include 'elicitation_id'.",
                code=ErrorCode.INVALID_INPUT,
            )
        message = payload.get("message")
        if not isinstance(message, str) or not message:
            message = "Cursor wants approval to run a tool"
        content_preview = payload.get("content_preview")
        if not isinstance(content_preview, str):
            content_preview = None
        operation_type = payload.get("operation_type")
        if not isinstance(operation_type, str) or not operation_type:
            operation_type = "tool"
        # Structured AskQuestion payload (cursor's multiple-choice tool): when
        # present, stamp it as the ``ask_user_question`` extra so the web UI
        # renders the interactive form from it directly. ``content_preview`` is
        # hard-capped at 1024 chars, which truncates a multi-question payload and
        # breaks the preview-parse fallback — the structured field has no such
        # cap and is the authoritative source the UI consumes when present.
        extras: dict[str, Any] = {}
        ask_user_question = payload.get("ask_user_question")
        if isinstance(ask_user_question, dict) and isinstance(
            ask_user_question.get("questions"), list
        ):
            extras["ask_user_question"] = ask_user_question
        params = ElicitationRequestParams(
            mode="form",
            message=message,
            requestedSchema=None,
            url=None,
            phase="pre_tool_use",
            policy_name="cursor_native_permission",
            content_preview=content_preview,
            **extras,
        )
        from omnigent.server.routes import sessions as _sf

        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=params,
            timeout_s=_sf._CURSOR_NATIVE_PERMISSION_HOOK_TIMEOUT_S,
            conversation_store=conversation_store,
            elicitation_id=elicitation_id,
            tool_name=f"Cursor({operation_type})",
        )
        if result is None:
            return Response(status_code=status.HTTP_200_OK)
        if result.action == "decline":
            # Explicit user decline: interrupt the native harness before
            # returning the decline so the abort signal arrives first.
            await _forward_session_change_to_runner(
                session_id,
                get_server_runner_router(),
                {"type": "interrupt"},
            )
        return Response(
            content=json.dumps(result.model_dump(exclude_none=True)),
            media_type="application/json",
        )

    # ── POST /sessions/{session_id}/hooks/native-permission-request ─

    @router.post(
        "/sessions/{session_id}/hooks/native-permission-request",
        # Internal harness callback webhook — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        dependencies=[Depends(require_json_content_type)],
    )
    async def native_permission_request_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Generic native-TUI tool-approval hook (TUI → web elicitation).

        The vendor-agnostic counterpart of
        :func:`cursor_permission_request_hook`, used by the hermes- and
        goose-native approval mirrors. The runner-side mirror detects the
        vendor's in-terminal approval prompt, POSTs it here, and the server
        publishes ``response.elicitation_request`` and parks for the web verdict
        — the same registry/publish/cleanup path as the cursor/codex/claude
        hooks. An empty ``200`` (TUI answered, or timeout) leaves the vendor's
        native prompt authoritative.

        Unlike the cursor hook, the card label / policy name come from the
        payload (``agent`` / ``policy_name``) so a Hermes or Goose approval is
        labelled as such, not "Cursor".

        :param request: FastAPI request carrying the detected prompt
            (``elicitation_id``, ``message``, ``content_preview``,
            ``operation_type``, optional ``agent`` / ``policy_name``).
        :param session_id: Omnigent conversation id from the URL path.
        :returns: An ``ElicitationResult`` (``{"action": …}``) on a web verdict,
            or ``200`` with empty body on TUI-resolution / timeout / disconnect.
        :raises OmnigentError: 404 if the session does not exist, 400 if the
            body is malformed.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in native permission hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "Native permission hook body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        elicitation_id = payload.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            raise OmnigentError(
                "Native permission hook body must include 'elicitation_id'.",
                code=ErrorCode.INVALID_INPUT,
            )
        agent = payload.get("agent")
        if not isinstance(agent, str) or not agent:
            agent = "Agent"
        message = payload.get("message")
        if not isinstance(message, str) or not message:
            message = f"{agent} wants approval to run a tool"
        content_preview = payload.get("content_preview")
        if not isinstance(content_preview, str):
            content_preview = None
        operation_type = payload.get("operation_type")
        if not isinstance(operation_type, str) or not operation_type:
            operation_type = "tool"
        policy_name = payload.get("policy_name")
        if not isinstance(policy_name, str) or not policy_name:
            policy_name = "native_permission"
        params = ElicitationRequestParams(
            mode="form",
            message=message,
            requestedSchema=None,
            url=None,
            phase="pre_tool_use",
            policy_name=policy_name,
            content_preview=content_preview,
        )
        from omnigent.server.routes import sessions as _sf

        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=params,
            timeout_s=_sf._NATIVE_PERMISSION_HOOK_TIMEOUT_S,
            conversation_store=conversation_store,
            elicitation_id=elicitation_id,
            tool_name=f"{agent}({operation_type})",
        )
        if result is None:
            return Response(status_code=status.HTTP_200_OK)
        if result.action == "decline":
            # Explicit user decline: interrupt the native harness before
            # returning the decline so the abort signal arrives first.
            await _forward_session_change_to_runner(
                session_id,
                get_server_runner_router(),
                {"type": "interrupt"},
            )
        return Response(
            content=json.dumps(result.model_dump(exclude_none=True)),
            media_type="application/json",
        )

    async def _route_subagent_catalog(session_id: str) -> dict[str, list[str]] | None:
        """
        Fetch the session's live model catalog for subagent routing.

        :param session_id: Parent session/conversation id.
        :returns: Worker → servable model ids, or ``None`` when the runner
            is unreachable (callers fall back to the static table).
        """
        from omnigent.server.smart_routing import fetch_runner_models

        try:
            runner_client = await _get_runner_client(
                session_id, runner_router or get_server_runner_router()
            )
            if runner_client is None:
                return None
            return await fetch_runner_models(session_id, runner_client)
        except Exception:
            _logger.debug(
                "route-subagent: live catalog unavailable for session=%s",
                session_id,
                exc_info=True,
            )
            return None

    @router.post(
        "/sessions/{session_id}/hooks/route-subagent",
        # Internal runner relay — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        dependencies=[Depends(require_json_content_type)],
    )
    async def route_subagent_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Decide the model/harness a native subagent spawn may use.

        The runner's loopback router (advertised to harness
        ``PreToolUse`` hooks via ``subagent_router.json``) relays here
        because ``RuntimeCaps.routing_client`` only lives in the server
        process. Request and response follow the frozen route-subagent
        contract; every routed verdict also lands as a
        ``routing_decision`` transcript item.

        The session's subagent-routing switch is two-state and re-read on
        every call (it is togglable mid-session): only an explicit ``"on"``
        routes, and every other session gets its spawn allowed unchanged
        without calling the router. Sessions that start on Smart Routing
        are stamped ``"on"`` at create, so nothing is inherited here.
        Candidate models stay inside the session's own harness family
        unless the session started in auto-harness mode.

        :param request: FastAPI request — body is the route-subagent
            request JSON.
        :param session_id: Parent session/conversation id from the path.
        :returns: The route-subagent decision as JSON.
        :raises OmnigentError: 400 when the body is not a JSON object or
            omits ``harness``.
        """
        from omnigent.runner.subagent_routing import (
            SubagentRouteDecision,
            SubagentRouteRequest,
            auto_harness_session,
            resolve_subagent_route,
            store_persister,
            subagent_routing_enabled,
        )
        from omnigent.server.smart_routing import AUTO_NATIVE_ROUTING_HARNESSES

        user_id = _get_user_id(request, auth_provider)
        # LEVEL_EDIT, like POST /events: a routed verdict mutates the session
        # (a persisted ``routing_decision`` item, and on the sibling route-turn
        # relay a ``model_override`` pin). A read-only viewer must not be able
        # to steer somebody else's spawns.
        await _require_access(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in route-subagent body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "route-subagent body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            route_request = SubagentRouteRequest.from_payload(payload)
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc

        # A relayed spawn is evidence the harness ran its routing hook, but it
        # is deliberately NOT turned into a clear here: the same warning code
        # also carries the spawn-audit verdict ("started on a model the router
        # never approved"), which a relay does not disprove — and every spawn
        # that produces such a verdict is itself relayed, so clearing here
        # wiped exactly the warnings the publisher had just raised (it only
        # re-posts on a transition, so the wipe was permanent). The publisher
        # owns the clear: its next check sees the canary and posts the repair.

        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        parent = None
        if conv is not None and conv.parent_conversation_id is not None:
            parent = await asyncio.to_thread(
                conversation_store.get_conversation, conv.parent_conversation_id
            )
        if conv is None or not subagent_routing_enabled(conv.subagent_routing_override):
            # Allowed unchanged, and deliberately not persisted: an
            # unrouted spawn is not a decision worth a transcript item.
            _logger.info(
                "route-subagent: subagent routing disabled for session=%s harness=%s",
                session_id,
                route_request.harness,
            )
            unrouted = SubagentRouteDecision(
                action="allow",
                rationale="subagent routing disabled for this session",
            )
            return Response(
                content=json.dumps(unrouted.to_payload()),
                media_type="application/json",
            )

        # Only a session started in auto-harness mode may be moved across
        # harness families; everyone else is offered their own family, so a
        # Claude Code session never gets a Codex suggestion.
        cross_harness = auto_harness_session(conv, parent)
        # Which families the spawn may land on decides which router can serve it:
        # off the AI Gateway the built-in judge answers, from the live catalog
        # alone (the static table's databricks-* ids are unreachable there).
        gateway_backed = await _spawn_gateway_backed(
            request,
            conv,
            (AUTO_NATIVE_ROUTING_HARNESSES if cross_harness else (route_request.harness,)),
        )
        # Offer the live catalog: the static table lags model generations, and
        # a pick the workspace serves must not look unservable and get
        # substituted down a tier.
        catalog = await _route_subagent_catalog(session_id)
        decision = await resolve_subagent_route(
            session_id,
            route_request,
            caps=get_caps(),
            catalog=catalog,
            cross_harness=cross_harness,
            gateway_backed=gateway_backed,
            allow_static_fallback=gateway_backed,
            persist=store_persister(session_id, conversation_store),
        )
        return Response(
            content=json.dumps(decision.to_payload()),
            media_type="application/json",
        )

    @router.post(
        "/sessions/{session_id}/hooks/route-turn",
        # Internal runner relay — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        dependencies=[Depends(require_json_content_type)],
    )
    async def route_turn_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Decide the model a session's first real prompt should run on.

        The in-harness sibling of ``route-subagent``: a harness
        ``UserPromptSubmit`` hook relays here (through the runner's
        loopback endpoint, advertised as ``turn_router.json``) because
        ``RuntimeCaps.routing_client`` only lives in the server process.
        It closes the bare-launch gap — a session started with no prompt,
        whose first message is typed straight into the TUI and so is
        invisible to the composer turn gate. Everything else about routing
        is unchanged: same decision seam, same chip, same
        ``model_override`` pin, and that pin is what stops a session from
        ever routing twice.

        :param request: FastAPI request — body is the route-turn request
            JSON.
        :param session_id: Session/conversation id from the path.
        :returns: The route-turn decision as JSON.
        :raises OmnigentError: 400 when the body is not a JSON object or
            omits ``harness`` / ``prompt``.
        """
        from omnigent.runner.turn_routing import (
            TurnRouteRequest,
            decision_scope,
            resolve_turn_route,
        )
        from omnigent.server.routes._sessions.helpers import _resolve_harness
        from omnigent.server.routes._sessions.orchestration import (
            _native_turn_catalog,
            _publish_routed_model,
            _stamp_routing_decision_label,
            _unavailable_routing_card,
        )
        from omnigent.server.smart_routing import route_turn as _route_turn_seam

        user_id = _get_user_id(request, auth_provider)
        # LEVEL_EDIT, like POST /events: this route writes ``model_override``
        # for the rest of the session and persists a decision item. LEVEL_READ
        # let a read-only viewer repin somebody else's model.
        await _require_access(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in route-turn body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "route-turn body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            route_request = TurnRouteRequest.from_payload(payload)
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc

        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        parent = None
        if conv is not None and conv.parent_conversation_id is not None:
            parent = await asyncio.to_thread(
                conversation_store.get_conversation, conv.parent_conversation_id
            )
        runner_client = None
        catalog = None
        if conv is not None:
            try:
                runner_client = await _get_runner_client(
                    session_id, runner_router or get_server_runner_router()
                )
                catalog = await _native_turn_catalog(session_id, conv, runner_client)
            except Exception:
                _logger.debug(
                    "route-turn: live catalog unavailable for session=%s",
                    session_id,
                    exc_info=True,
                )

        # This pane's own family decides which router can serve its first turn.
        # A create off the AI Gateway now succeeds (the built-in judge answers),
        # so this hook must make the same choice the composer path does.
        turn_gateway_backed = (
            await _spawn_gateway_backed(request, conv, (route_request.harness,))
            if conv is not None
            else True
        )

        async def _route(
            harness: str | None, prompt: str
        ) -> tuple[str | None, dict[str, Any] | None]:
            return await _route_turn_seam(
                harness,
                prompt,
                session_id=session_id,
                runner_client=runner_client,
                catalog=catalog,
                gateway_backed=turn_gateway_backed,
                allow_static_fallback=turn_gateway_backed,
            )

        async def _pin(model: str) -> bool:
            try:
                await asyncio.to_thread(
                    conversation_store.update_conversation,
                    session_id,
                    model_override=model,
                )
            except (OSError, ValueError):
                _logger.warning(
                    "route-turn: could not pin model_override for session=%s",
                    session_id,
                    exc_info=True,
                )
                return False
            _publish_routed_model(session_id, model)
            return True

        async def _persist(model: str, verdict: dict[str, Any]) -> None:
            decision_id = await _emit_server_routing_decision(
                session_id,
                conversation_store,
                model,
                verdict,
                scope=decision_scope(),
                harness=route_request.harness,
            )
            await _stamp_routing_decision_label(session_id, conversation_store, decision_id)

        async def _record_decline(cause: str) -> None:
            """Persist the declined chip for a failed routing call.

            The card, not the label: a failure must stay visible without
            claiming the route-once gate, or one outage would make this the
            session's routing decision forever.
            """
            model, verdict = _unavailable_routing_card(cause)
            await _emit_server_routing_decision(
                session_id,
                conversation_store,
                model,
                verdict,
                scope=decision_scope(),
                harness=route_request.harness,
            )

        async def _reuse_create_route() -> bool:
            """Claim the create-time decision as this session's routing decision.

            No second chip: the create's own row already says what was picked,
            and the pane launched on it. Claiming the route-once label is what
            makes this the session's decision, so a later prompt does not ask
            again.

            :returns: ``True`` when the create's decision was claimed.
            """
            decision_id = await asyncio.to_thread(
                _create_route_decision_id, session_id, conversation_store
            )
            if decision_id is None:
                return False
            await _stamp_routing_decision_label(session_id, conversation_store, decision_id)
            return True

        decision = await resolve_turn_route(
            session_id,
            route_request,
            conv=conv,
            parent=parent,
            # A pinned routed parent confines its spawns to its own family, so
            # the pane's own family is not the only one that matters.
            parent_harness=_resolve_harness(parent) if parent is not None else None,
            route_turn=_route,
            reuse_create_route=_reuse_create_route,
            pin=_pin,
            persist=_persist,
            record_decline=_record_decline,
        )
        _logger.info(
            "route-turn: session=%s harness=%s live_model=%s pinned=%s action=%s model=%s",
            session_id,
            route_request.harness,
            route_request.model,
            conv.model_override if conv is not None else None,
            decision.action,
            decision.model,
        )
        # The rationale paraphrases the user's prompt, so it stays off INFO —
        # the same invariant ``omnigent.server.smart_routing`` keeps at each of
        # its own three log sites.
        _logger.debug("route-turn: session=%s rationale=%s", session_id, decision.rationale)
        return Response(
            content=json.dumps(decision.to_payload()),
            media_type="application/json",
        )
