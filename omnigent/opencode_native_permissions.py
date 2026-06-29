"""OpenCode permission normalization and policy/approval mapping.

OpenCode requests approval for sensitive actions via permission events
(``permission.v2.asked`` over SSE / ``GET /permission``) and accepts a
reply of ``once`` / ``always`` / ``reject`` (``POST
/permission/{requestID}/reply``). This module is the seam between
OpenCode's permission surface and Omnigent's policy/approval model:

1. Normalize a raw permission request into a flat policy-evaluation input.
2. Map an Omnigent policy verdict (allow / allow-always / deny / ask) onto
   an OpenCode reply.
3. Fail closed: an unmapped verdict yields no auto-reply, so the caller
   must obtain a human decision before answering.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

OPENCODE_NATIVE_HARNESS = "opencode-native"

# OpenCode's accepted reply tokens.
OpenCodeReply = Literal["once", "always", "reject"]

# Omnigent-side normalized decisions used by the forwarder.
PolicyDecision = Literal["allow_once", "allow_always", "reject", "ask"]


@dataclass(frozen=True)
class OpenCodePermissionRequest:
    """
    A normalized OpenCode permission request.

    :param request_id: OpenCode permission request id, e.g. ``"per_..."``.
    :param session_id: OpenCode session id the request belongs to.
    :param action: The action/tool name needing approval, e.g.
        ``"bash"`` or ``"edit"``.
    :param resources: Resource descriptors (command/path/url), as given.
    :param metadata: Extra metadata supplied by OpenCode.
    :param source: Where the request originated, when reported.
    :param raw: The full raw payload for forward-compatibility.
    """

    request_id: str
    session_id: str | None
    action: str | None
    resources: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def parse_permission_request(payload: Mapping[str, Any]) -> OpenCodePermissionRequest | None:
    """
    Parse a raw permission payload into :class:`OpenCodePermissionRequest`.

    Accepts BOTH opencode permission event shapes (live-verified against
    1.17.7, which emits v1 ``permission.asked``):

    - **v1** (``permission.asked``): ``{id, sessionID, permission, patterns,
      metadata, always, tool}`` — the tool/category is in ``permission``
      (e.g. ``"bash"``/``"edit"``/``"read"``) and the resources are string
      ``patterns``.
    - **v2** (``permission.v2.asked``): ``{id, sessionID, action, resources,
      save, metadata, source}`` — the category is in ``action``.

    The category MUST be extracted (it becomes the policy-evaluation tool
    name): reading only ``action``/``type`` left v1's ``permission`` field
    unread, so every opencode tool reached the policy engine as the literal
    name ``"permission"`` and matched no tool-name policy (e.g. "Require
    Approval for File & Shell Operations" never fired). Also accepts entries
    from ``GET /permission``.

    :param payload: Raw permission object.
    :returns: Parsed request, or ``None`` when no request id is present.
    """
    request_id = payload.get("id") or payload.get("requestID") or payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    session_id = payload.get("sessionID") or payload.get("session_id")
    # v2 → ``action``; v1 → ``permission`` (the tool category, e.g. "bash").
    action = payload.get("action") or payload.get("type") or payload.get("permission")
    # v2 → ``resources`` (dicts); v1 → ``patterns`` (strings).
    resources = payload.get("resources") or payload.get("patterns")
    metadata = payload.get("metadata")
    source = payload.get("source")
    return OpenCodePermissionRequest(
        request_id=request_id,
        session_id=session_id if isinstance(session_id, str) else None,
        action=action if isinstance(action, str) else None,
        resources=list(resources) if isinstance(resources, list) else [],
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        source=source if isinstance(source, str) else None,
        raw=dict(payload),
    )


def normalize_for_policy(
    request: OpenCodePermissionRequest,
    *,
    omnigent_session_id: str,
    workspace: str | None,
) -> dict[str, Any]:
    """
    Build an Omnigent policy-evaluation input from a permission request.

    The shape mirrors what the codex-native policy hook posts to
    ``/v1/sessions/{id}/policies/evaluate`` — an action name plus the
    concrete command / path / url being acted on, so configured policies
    can reason about the operation.

    :param request: The normalized OpenCode permission request.
    :param omnigent_session_id: Owning Omnigent conversation id.
    :param workspace: Session working directory, when known.
    :returns: A flat dict suitable for policy evaluation.
    """
    command, path, url = _extract_resource_fields(request)
    return {
        "harness": OPENCODE_NATIVE_HARNESS,
        "action": request.action,
        "command": command,
        "path": path,
        "url": url,
        "working_directory": workspace,
        "opencode_session_id": request.session_id,
        "omnigent_session_id": omnigent_session_id,
        "request_id": request.request_id,
        "metadata": request.metadata,
    }


def _extract_resource_fields(
    request: OpenCodePermissionRequest,
) -> tuple[str | None, str | None, str | None]:
    """
    Pull command / path / url out of a permission request's resources.

    :param request: The normalized permission request.
    :returns: ``(command, path, url)``; each ``None`` when not present.
    """
    command: str | None = None
    path: str | None = None
    url: str | None = None
    candidates: list[Mapping[str, Any]] = []
    if isinstance(request.metadata, Mapping):
        candidates.append(request.metadata)
    for resource in request.resources:
        if isinstance(resource, Mapping):
            candidates.append(resource)
    for source in candidates:
        if command is None:
            value = source.get("command")
            command = value if isinstance(value, str) and value else command
        if path is None:
            value = source.get("path") or source.get("filePath") or source.get("file")
            path = value if isinstance(value, str) and value else path
        if url is None:
            value = source.get("url")
            url = value if isinstance(value, str) and value else url
    return command, path, url


def map_verdict_to_decision(verdict: Mapping[str, Any] | None) -> PolicyDecision:
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


def reply_body(reply: OpenCodeReply, *, message: str | None = None) -> dict[str, Any]:
    """
    Build the JSON body for ``POST /permission/{requestID}/reply``.

    :param reply: ``once`` / ``always`` / ``reject``.
    :param message: Optional human-readable note attached to the reply.
    :returns: The reply request body.
    """
    body: dict[str, Any] = {"reply": reply}
    if message is not None:
        body["message"] = message
    return body
