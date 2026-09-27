"""OpenCode permission normalization and policy/approval mapping.

OpenCode 2.x emits ``permission.asked`` for every tool call the ask-all
ruleset gates and accepts ``once`` / ``reject`` on
``POST /api/session/{id}/permission/{requestID}/reply``. This module turns a
request into a policy-evaluation input and maps the verdict back onto a
reply; an unmapped verdict yields no auto-reply (fail closed).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from omnigent.util.json_types import JsonObject as _JsonObject

OPENCODE_NATIVE_HARNESS = "opencode-native"


def evaluate_elicitation_id(request_id: str) -> str:
    """
    Derive the ``_omnigent_elicitation_id`` a permission request evaluates under.

    The evaluator stamps it on ``POST /policies/evaluate`` so the parked
    approval card is addressable, and the forwarder posts the same id in
    ``external_elicitation_resolved`` when the TUI answers first.

    :param request_id: OpenCode permission request id, e.g. ``"per_abc"``.
    :returns: ``"elicit_evaluate_<32 hex>"``, stable for *request_id*.
    """
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    return f"elicit_evaluate_{digest}"


# Reply tokens the forwarder sends; ``always`` is never used (see decision_to_reply).
OpenCodeReply = Literal["once", "reject"]

# Omnigent-side normalized decisions used by the forwarder.
PolicyDecision = Literal["allow_once", "allow_always", "reject", "ask"]

_JsonMapping: TypeAlias = Mapping[str, object]


@dataclass(frozen=True)
class OpenCodePermissionRequest:
    """
    A normalized OpenCode ``Permission.Request``.

    :param request_id: Permission request id, e.g. ``"per_..."``.
    :param session_id: OpenCode session id, e.g. ``"ses_..."``.
    :param action: Permission action, e.g. ``"shell"``, ``"edit"``, or an MCP
        tool name such as ``"omnigent_sys_session_list"``.
    :param resources: Resource strings (command text, relative path, URL, pattern).
    :param metadata: Tool-supplied metadata (e.g. ``{"filepath": ..., "diff": ...}``).
    :param source: ``{"type": "tool", "messageID": ..., "id": ...}`` when raised by a tool.
    :param message: Optional message a permission hook attached.
    :param raw: The full payload.
    """

    request_id: str
    session_id: str | None
    action: str | None
    resources: list[str] = field(default_factory=list)
    metadata: _JsonObject = field(default_factory=dict)
    source: _JsonObject | None = None
    message: str | None = None
    raw: _JsonObject = field(default_factory=dict)

    @property
    def tool_call_id(self) -> str | None:
        """:returns: The originating tool call id (``source.id``), if any."""
        value = self.source.get("id") if self.source else None
        return value if isinstance(value, str) and value else None

    @property
    def message_id(self) -> str | None:
        """:returns: The originating assistant message id (``source.messageID``)."""
        value = self.source.get("messageID") if self.source else None
        return value if isinstance(value, str) and value else None


# Cross-stage contract name.
PermissionRequest = OpenCodePermissionRequest


def parse_permission_request(data: _JsonMapping) -> OpenCodePermissionRequest | None:
    """
    Parse a v2 ``permission.asked`` payload.

    :param data: The event ``data`` object (``Permission.Request``).
    :returns: Parsed request, or ``None`` when no ``id`` is present.
    """
    request_id = data.get("id")
    if not isinstance(request_id, str) or not request_id:
        return None
    session_id = data.get("sessionID")
    action = data.get("action")
    resources = data.get("resources")
    metadata = data.get("metadata")
    source = data.get("source")
    message = data.get("message")
    return OpenCodePermissionRequest(
        request_id=request_id,
        session_id=session_id if isinstance(session_id, str) else None,
        action=action if isinstance(action, str) and action else None,
        resources=[item for item in resources if isinstance(item, str)]
        if isinstance(resources, list)
        else [],
        metadata={key: value for key, value in metadata.items() if isinstance(key, str)}
        if isinstance(metadata, Mapping)
        else {},
        source={key: value for key, value in source.items() if isinstance(key, str)}
        if isinstance(source, Mapping)
        else None,
        message=message if isinstance(message, str) and message else None,
        raw=dict(data),
    )


_PATH_ACTIONS = frozenset({"read", "edit", "external_directory"})
_PATTERN_ACTIONS = frozenset({"glob", "grep"})
# Actions whose single resource is the operand, keyed by the argument name policies read.
_SINGLE_RESOURCE_ARGUMENT = {
    "webfetch": "url",
    "websearch": "query",
    "skill": "skill",
    "subagent": "agent",
}


def policy_arguments(request: OpenCodePermissionRequest) -> _JsonObject:
    """
    Build policy ``data.arguments`` for a v2 permission request.

    :param request: The parsed permission request.
    :returns: Action-specific arguments, e.g. ``{"command": "ls"}`` for ``shell``.
    """
    action = request.action or ""
    resources = request.resources
    first = resources[0] if resources else None
    if action == "shell":
        return {"command": "\n".join(resources)} if resources else {}
    if action in _PATH_ACTIONS:
        if first is None:
            return {}
        arguments: _JsonObject = {"path": first}
        if len(resources) > 1:
            arguments["paths"] = list(resources)
        return arguments
    if action in _PATTERN_ACTIONS:
        if first is None:
            return {}
        arguments = {"pattern": first}
        search_path = request.metadata.get("path")
        if isinstance(search_path, str) and search_path:
            arguments["path"] = search_path
        return arguments
    key = _SINGLE_RESOURCE_ARGUMENT.get(action)
    if key is not None:
        return {key: first} if first is not None else {}
    if resources and resources != ["*"]:
        return {"resources": list(resources)}
    return {}


def normalize_for_policy(
    request: OpenCodePermissionRequest,
    *,
    omnigent_session_id: str,
    workspace: str | None,
) -> _JsonObject:
    """
    Build an Omnigent policy-evaluation input from a permission request.

    The shape mirrors what the codex-native policy hook posts to
    ``/v1/sessions/{id}/policies/evaluate`` — an action name plus
    ``arguments`` built per-action from the v2 ``resources`` list, so
    configured policies can reason about the operation.

    :param request: The normalized OpenCode permission request.
    :param omnigent_session_id: Owning Omnigent conversation id.
    :param workspace: Session working directory, when known.
    :returns: A flat dict suitable for policy evaluation.
    """
    arguments = policy_arguments(request)
    command = arguments.get("command")
    path = arguments.get("path")
    url = arguments.get("url")
    return {
        "harness": OPENCODE_NATIVE_HARNESS,
        "action": request.action,
        "arguments": arguments,
        "resources": list(request.resources),
        "command": command if isinstance(command, str) else None,
        "path": path if isinstance(path, str) else None,
        "url": url if isinstance(url, str) else None,
        "working_directory": workspace,
        "opencode_session_id": request.session_id,
        "omnigent_session_id": omnigent_session_id,
        "request_id": request.request_id,
        "tool_call_id": request.tool_call_id,
        "metadata": request.metadata,
    }


def map_verdict_to_decision(verdict: _JsonMapping | None) -> PolicyDecision:
    """
    Map an Omnigent policy verdict onto a normalized decision.

    Recognizes both ``{"decision": "..."}`` and ``{"action": "..."}``
    verdict shapes. Anything unrecognized maps to ``"ask"`` (fail closed:
    the caller must obtain a human decision before replying).

    :param verdict: The policy verdict object, or ``None``.
    :returns: One of ``allow_once`` / ``allow_always`` / ``reject`` / ``ask``.
    """
    if not isinstance(verdict, Mapping):
        return "ask"
    raw = verdict.get("decision") or verdict.get("action") or verdict.get("verdict")
    token = str(raw).strip().lower() if raw is not None else ""
    if token in {"allow_always", "always", "allow-always"}:
        return "allow_always"
    if token in {"allow", "allow_once", "approve", "allowed", "accept"}:
        return "allow_once"
    if token in {"deny", "reject", "block", "blocked", "denied"}:
        return "reject"
    return "ask"


def decision_to_reply(decision: PolicyDecision) -> OpenCodeReply | None:
    """
    Map a normalized decision onto an OpenCode reply token.

    Both ``allow_once`` and ``allow_always`` map to opencode ``"once"`` — the
    forwarder NEVER replies ``"always"``. opencode persists an ``"always"``
    reply into its local ``approved`` ruleset and then auto-allows every future
    matching tool WITHOUT re-emitting ``permission.asked`` (see opencode
    ``permission/index.ts``), which bypasses the Omnigent policy engine and
    breaks live policy changes — e.g. toggling "Require Approval" mid-session
    would never take effect because opencode stopped asking. Replying ``"once"``
    forces opencode to re-ask on every call so the server engine stays
    authoritative; the "always allow" semantics live SERVER-side (the engine
    persists an approved ASK and returns ``allow`` on later evaluations, so the
    forwarder simply replies ``"once"`` again with no card).

    :param decision: One of ``allow_once`` / ``allow_always`` / ``reject``
        / ``ask``.
    :returns: ``"once"`` / ``"reject"``, or ``None`` for ``ask`` (no automatic
        reply — needs a human).
    """
    if decision in ("allow_once", "allow_always"):
        return "once"
    if decision == "reject":
        return "reject"
    return None
