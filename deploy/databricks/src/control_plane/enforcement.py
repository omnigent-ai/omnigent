"""The single enforcement point — one HTTP middleware in front of the
upstream server.

This is feature 4, and the design's critical decision: UC grants on
agent-spec artifacts do NOT stop a user from calling ``POST /v1/sessions
{agent_id}`` directly, so authorization must sit at the **request layer**,
not in the data layer. We add exactly one ``@app.middleware("http")`` that
every request passes through (registered *after* ``create_app`` returns, so
it is the outermost HTTP middleware). It enforces two things and is a
pass-through for everything else:

1. **Agent-list filtering** — on a successful ``GET /v1/agents`` response,
   drop agents the caller may not see (restricted agents they're not the
   owner / audience of). The caller never learns the agent exists.

2. **Launch authorization** — on any route that binds a *template*
   ``agent_id`` from the request body, if it names a restricted template the
   caller isn't authorized for, reject with 403 *before* the upstream
   handler runs. This covers ``POST /v1/sessions`` (the doc's named bypass)
   **and** the two sibling routes that also accept ``body.agent_id`` —
   ``POST /v1/sessions/{id}/fork`` and ``POST /v1/sessions/{id}/switch-agent``
   — which would otherwise be ungoverned launch paths. The body is JSON-parsed
   regardless of the declared ``Content-Type`` (only ``multipart/form-data``
   bundle uploads are skipped, since they always create a fresh
   session-scoped agent), so the deny can't be slipped past with an
   ``application/*+json`` or absent Content-Type.

Both decisions reuse the exact same predicate the management API uses
(:meth:`AgentAclStore.can_view`), so list-visibility and launch-authz can
never diverge.

The governed paths **fail closed**: if the ACL/identity lookup errors while
deciding a restricted-template launch, the request is rejected with ``503``
rather than allowed through; if agent-list filtering errors, an empty list is
returned rather than the unfiltered upstream payload. For an access-control
layer, favoring availability over correctness is the wrong default. Only
*structural* non-governed cases (multipart uploads, an unparseable/absent
body, a non-list payload) pass through — they carry nothing this layer can or
should authorize. The layer also enforces the design's **use-only consumer**
posture: a multipart ``POST /v1/sessions`` (bring-your-own-agent bundle
upload) is denied for ``consumer``-role callers (contributor+ may upload),
gated by ``OMNIGENT_CP_CONSUMER_UPLOAD`` (default ``deny``, set ``allow`` to
restore the prior behavior).
"""

from __future__ import annotations

import json
import logging
import os
import re

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from control_plane.acl_store import AgentAclStore
from control_plane.roles import RoleResolver

logger = logging.getLogger("omnigent-app.control_plane.enforcement")

_AGENTS_PATH = "/v1/agents"
_SESSIONS_PATH = "/v1/sessions"
# Sibling routes that also bind a template ``agent_id`` from the JSON body
# (``POST /v1/sessions/{id}/fork`` and ``.../switch-agent``). They are not the
# exact ``/v1/sessions`` path, so they must be matched explicitly or they
# become ungoverned launch paths. Trailing slash tolerated.
_AGENT_BIND_SUFFIX_RE = re.compile(r"^/v1/sessions/[^/]+/(?:fork|switch-agent)/?$")
# ``PUT /v1/sessions/{id}/agent`` rewrites the session's bound agent bundle.
# When the session binds a *template* (catalog) agent, that write mutates the
# shared template — so this path needs an owner/admin guard. Anchored with
# ``/?$`` so it does NOT also match ``.../agent/contents``.
_AGENT_UPDATE_RE = re.compile(r"^/v1/sessions/([^/]+)/agent/?$")


def _binds_template_agent(method: str, path: str) -> bool:
    """Whether *method*/*path* is a route that binds a template ``agent_id``
    from the request body, and therefore needs launch authorization.
    """
    if method != "POST":
        return False
    if path == _SESSIONS_PATH or path == _SESSIONS_PATH + "/":
        return True
    return bool(_AGENT_BIND_SUFFIX_RE.match(path))


def _is_multipart(content_type: str) -> bool:
    """Whether the Content-Type is a multipart form upload.

    Only multipart bodies are skipped by launch authz: the upstream
    ``POST /v1/sessions`` multipart path always creates a fresh
    server-generated session-scoped agent (nothing to authorize). Every
    other Content-Type — ``application/json``, ``application/*+json``, or even
    an absent header — is JSON-parsed by the upstream handlers
    (``await request.json()`` ignores the declared type), so it must be
    inspected.

    The normalization here mirrors the upstream dispatch BYTE-FOR-BYTE
    (``sessions.py``: ``content-type.split(";", 1)[0].lower()`` — note: **no
    ``.strip()``**). The skip set must be *identical* to upstream's: a header
    like ``"multipart/form-data ; boundary=x"`` (whitespace before the ``;``)
    is NOT multipart to upstream (it falls into the JSON branch and binds the
    body ``agent_id``), so we must not skip it either. Stripping here would
    re-open the launch bypass for any whitespace-padded multipart variant.
    """
    media_type = content_type.split(";", 1)[0].lower()
    return media_type == "multipart/form-data"


def _service_unavailable(detail: str) -> JSONResponse:
    """A canonical 503 for a governed path whose authz couldn't be decided.

    Governed launch authz fails *closed*: if the ACL/identity lookup errors
    we reject rather than allow, so a transient infra failure can't expose or
    launch a restricted template.
    """
    return JSONResponse(
        status_code=503,
        content={"error": {"code": "unavailable", "message": detail}},
    )


def _consumer_upload_policy() -> str:
    """Resolve the consumer bundle-upload policy from the environment.

    ``OMNIGENT_CP_CONSUMER_UPLOAD`` = ``deny`` (default) | ``allow``. When
    ``deny``, a ``consumer``-role caller cannot create a session by uploading
    an agent bundle (multipart ``POST /v1/sessions``) — the design's use-only
    posture. ``allow`` restores the prior behavior. Read once at attach time.
    """
    val = (os.environ.get("OMNIGENT_CP_CONSUMER_UPLOAD", "deny") or "deny").strip().lower()
    return "allow" if val == "allow" else "deny"


class _Enforcer:
    """Holds the dependencies the middleware closure needs."""

    def __init__(
        self,
        role_resolver: RoleResolver,
        acl_store: AgentAclStore,
        agent_store,
        conversation_store=None,
    ) -> None:
        self._roles = role_resolver
        self._acl = acl_store
        self._agents = agent_store
        # Used by the template-mutation guard to resolve a session's bound
        # agent (PUT /v1/sessions/{id}/agent). Optional for back-compat.
        self._convs = conversation_store

    def _authorize_launch(self, principal, agent_id: str) -> bool:
        """Whether *principal* may launch *agent_id*.

        Only governs *template* agents (``session_id IS NULL``).
        Session-scoped agents are left to upstream's READ check, which is
        unchanged and still runs. Unknown agents are allowed through so the
        upstream handler produces its own 404 (we don't change error
        semantics for missing agents).
        """
        agent = self._agents.get(agent_id)
        if agent is None or agent.session_id is not None:
            return True
        vis = self._acl.get_visibility(agent_id)
        return AgentAclStore.can_view(
            vis,
            user_id=principal.user_id or "",
            groups=principal.groups,
            is_admin=principal.is_admin,
        )

    def _filter_agents_payload(self, principal, payload: dict) -> dict:
        """Return a copy of a ``GET /v1/agents`` payload with agents the
        caller may not see removed.
        """
        items = payload.get("data")
        if not isinstance(items, list):
            return payload
        ids = [it.get("id") for it in items if isinstance(it, dict) and it.get("id")]
        vis_map = self._acl.get_visibility_map(ids)
        kept = []
        for it in items:
            if not isinstance(it, dict):
                continue
            aid = it.get("id")
            vis = vis_map.get(aid)
            if vis is None:
                kept.append(it)
                continue
            if AgentAclStore.can_view(
                vis,
                user_id=principal.user_id or "",
                groups=principal.groups,
                is_admin=principal.is_admin,
            ):
                kept.append(it)
        new_payload = dict(payload)
        new_payload["data"] = kept
        # Keep pagination fields coherent: the page may now be shorter, but
        # cursors still point at real ids; has_more is preserved as upstream
        # set it (a filtered page can legitimately be empty with more behind).
        return new_payload


def install_enforcement_middleware(
    app,
    *,
    role_resolver: RoleResolver,
    acl_store: AgentAclStore,
    agent_store,
    conversation_store=None,
) -> None:
    """Register the single enforcement middleware on the app.

    Must be called AFTER ``create_app`` returns and BEFORE the first
    request, so the closure is the outermost HTTP middleware.

    :param app: The upstream FastAPI app.
    :param role_resolver: Resolves requests to principals.
    :param acl_store: Agent visibility + ACL store.
    :param agent_store: Upstream agent store (to classify agents).
    :param conversation_store: Upstream conversation store — lets the
        template-mutation guard resolve a session's bound agent.
    """
    enforcer = _Enforcer(role_resolver, acl_store, agent_store, conversation_store)
    consumer_upload = _consumer_upload_policy()

    @app.middleware("http")
    async def control_plane_enforcement(request: Request, call_next):
        path = request.url.path
        method = request.method.upper()

        # ── Consumer use-only: deny bring-your-own-agent uploads ──
        # A multipart POST /v1/sessions uploads an agent bundle and creates a
        # session-scoped agent owned by the caller. The design says consumers
        # are use-only, so deny that for consumer-role callers (contributor+
        # may upload). Only fires on the exact create path + multipart body;
        # the JSON path (launching an existing agent) is untouched here.
        if method == "POST" and (path == _SESSIONS_PATH or path == _SESSIONS_PATH + "/"):
            denied = await _maybe_deny_consumer_upload(request, enforcer, consumer_upload)
            if denied is not None:
                return denied

        # ── Launch authorization on every template-binding route ──
        # session-create, fork, and switch-agent all bind a template
        # agent_id from the JSON body; multipart uploads always create a
        # fresh session-scoped agent (server-generated id), which this layer
        # doesn't govern (skipped inside _maybe_deny_launch).
        if _binds_template_agent(method, path):
            denied = await _maybe_deny_launch(request, enforcer)
            if denied is not None:
                return denied

        # ── Template-mutation guard on session-agent update ────────
        # PUT /v1/sessions/{id}/agent rewrites the bound agent's bundle. If the
        # session binds a TEMPLATE (catalog) agent, only its owner/admin may
        # mutate it; a session-scoped agent (the common case) falls through to
        # upstream's own LEVEL_EDIT check, unchanged.
        if method == "PUT":
            m = _AGENT_UPDATE_RE.match(path)
            if m is not None:
                denied = await _maybe_deny_template_update(request, enforcer, m.group(1))
                if denied is not None:
                    return denied

        response = await call_next(request)

        # ── Agent-list filtering ──────────────────────────────────
        if method == "GET" and path == _AGENTS_PATH and response.status_code == 200:
            filtered = await _maybe_filter_agents(request, response, enforcer)
            if filtered is not None:
                return filtered

        return response


async def _maybe_deny_launch(request: Request, enforcer: _Enforcer) -> Response | None:
    """Return a 403 response if the session-create binds a forbidden agent.

    Reads + caches the JSON body so the downstream handler can re-read it
    (Starlette caches ``await request.body()`` on the request, and we
    repopulate the receive channel). Non-JSON / unparseable bodies and any
    error fall through (return ``None``) so the upstream handler runs
    normally.
    """
    # Skip only multipart uploads (fresh session-scoped agent, nothing to
    # authorize). Everything else — application/json, application/*+json, or
    # an absent Content-Type — is JSON-parsed by the upstream handler, so we
    # must inspect it. A substring/suffix test here would let an
    # application/vnd.api+json header bypass the deny.
    content_type = request.headers.get("content-type", "")
    if _is_multipart(content_type):
        return None
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return None
    # Repopulate the receive channel so downstream can read the body again.
    _restore_body(request, raw)
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    agent_id = body.get("agent_id")
    if not agent_id or not isinstance(agent_id, str):
        return None
    # Resolve identity first. RoleResolver.resolve is contractually
    # non-raising, but guard defensively: a resolution failure on a governed
    # launch must fail CLOSED (503), never silently allow.
    try:
        principal = enforcer._roles.resolve(request)
    except Exception:  # noqa: BLE001
        logger.warning(
            "control_plane: principal resolution failed on governed launch; failing closed",
            exc_info=True,
        )
        return _service_unavailable("Authorization service temporarily unavailable.")
    if principal.user_id is None:
        # Let upstream's auth return 401 consistently.
        return None
    # The ACL decision itself fails CLOSED: an ACL/DB read failure for a real
    # template must reject (503), not bypass the only control-plane guard.
    try:
        allowed = enforcer._authorize_launch(principal, agent_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "control_plane: launch authz lookup failed for agent %s; failing closed",
            agent_id,
            exc_info=True,
        )
        return _service_unavailable("Authorization service temporarily unavailable.")
    if allowed:
        return None
    logger.info(
        "control_plane: denied launch of restricted agent %s by %s",
        agent_id,
        principal.user_id,
    )
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "code": "forbidden",
                "message": "You are not authorized to launch this agent.",
            }
        },
    )


async def _maybe_deny_template_update(
    request: Request, enforcer: _Enforcer, session_id: str
) -> Response | None:
    """Return a 403 if a ``PUT /v1/sessions/{id}/agent`` would mutate a
    *template* (catalog) agent the caller doesn't own.

    Session-scoped agents (the common case — a session editing its own agent)
    pass straight through to upstream's ``LEVEL_EDIT`` check, unchanged. Only a
    session bound to a template reaches the manage check, mirroring how
    ``_authorize_launch`` classifies template vs session-scoped. Reads only the
    URL session id + the bound agent — no request body — so no body-restore is
    needed. Fails CLOSED (503) on a lookup error.
    """
    # No conversation_store wired (e.g. older deploy) → can't classify; defer
    # to upstream rather than block.
    if enforcer._convs is None:
        return None
    try:
        principal = enforcer._roles.resolve(request)
    except Exception:  # noqa: BLE001
        logger.warning(
            "control_plane: principal resolution failed on template-update; failing closed",
            exc_info=True,
        )
        return _service_unavailable("Authorization service temporarily unavailable.")
    if principal.user_id is None:
        return None  # let upstream return 401
    try:
        conv = enforcer._convs.get_conversation(session_id)
        if conv is None or conv.agent_id is None:
            return None  # upstream produces its own 404
        agent = enforcer._agents.get(conv.agent_id)
        if agent is None:
            return None  # upstream 404
        if agent.session_id is not None:
            # Session-scoped agent — not a shared template. Upstream's
            # LEVEL_EDIT check governs it; nothing for us to add.
            return None
        vis = enforcer._acl.get_visibility(agent.id)
        allowed = AgentAclStore.can_manage(
            vis, user_id=principal.user_id, is_admin=principal.is_admin
        )
    except Exception:  # noqa: BLE001 — fail closed on the governed mutation
        logger.warning(
            "control_plane: template-update authz lookup failed; failing closed",
            exc_info=True,
        )
        return _service_unavailable("Authorization service temporarily unavailable.")
    if allowed:
        return None
    logger.info(
        "control_plane: denied template mutation of %s via session %s by %s",
        conv.agent_id,
        session_id,
        principal.user_id,
    )
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "code": "forbidden",
                "message": "You are not authorized to modify this template agent.",
            }
        },
    )


async def _maybe_deny_consumer_upload(
    request: Request, enforcer: _Enforcer, policy: str
) -> Response | None:
    """Return a 403 if a ``consumer`` uploads an agent bundle (use-only).

    Fires only on a multipart ``POST /v1/sessions`` (bring-your-own-agent
    bundle create) when ``policy == "deny"``. Contributors and admins may
    upload; only the consumer tier is denied, matching the design's use-only
    posture. The JSON create/launch path is never touched here.
    """
    if policy == "allow":
        return None
    content_type = request.headers.get("content-type", "")
    if not _is_multipart(content_type):
        return None
    try:
        principal = enforcer._roles.resolve(request)
    except Exception:  # noqa: BLE001 — fail closed for the governed upload decision
        logger.warning(
            "control_plane: principal resolution failed on upload; failing closed",
            exc_info=True,
        )
        return _service_unavailable("Authorization service temporarily unavailable.")
    if principal.user_id is None:
        # Let upstream's auth return 401 consistently.
        return None
    if principal.role == "consumer":
        logger.info("control_plane: denied bundle upload by consumer %s", principal.user_id)
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "forbidden",
                    "message": "Uploading a session bundle requires the contributor or admin role.",
                }
            },
        )
    return None


async def _maybe_filter_agents(
    request: Request, response: Response, enforcer: _Enforcer
) -> Response | None:
    """Return a filtered copy of a ``GET /v1/agents`` response, or ``None``.

    Reads the streamed response body, filters the agent list, and returns a
    fresh ``JSONResponse`` preserving the status code and (most) headers.

    Fails **closed**: if the body can't be drained or filtering errors, an
    empty list (least privilege) is returned rather than the unfiltered
    upstream payload — a filtering bug must never *widen* what a caller sees.
    Only structural non-list payloads (an error body, a non-dict) are rebuilt
    unchanged, since there is no agent list to leak there.
    """
    try:
        body_bytes = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body_bytes += chunk
    except Exception:  # noqa: BLE001
        # Body already partially consumed and unusable — fail closed with an
        # empty catalog (200 so the UI still renders) rather than a broken body.
        logger.warning(
            "control_plane: failed to read agent-list response; failing closed (empty list)",
            exc_info=True,
        )
        return JSONResponse(status_code=200, content={"data": [], "has_more": False})
    try:
        payload = json.loads(body_bytes)
    except (ValueError, TypeError):
        # Not JSON — rebuild the original response so the body isn't lost.
        return _rebuild_response(response, body_bytes)
    if not isinstance(payload, dict):
        return _rebuild_response(response, body_bytes)
    try:
        principal = enforcer._roles.resolve(request)
        if principal.user_id is None:
            # Reached only if upstream returned 200 to an unauthenticated
            # caller (today it 401s, so this is dead). Fail CLOSED rather than
            # leak the unfiltered list, matching the except branch below — no
            # coupling to the upstream auth invariant.
            return JSONResponse(status_code=200, content={"data": [], "has_more": False})
        new_payload = enforcer._filter_agents_payload(principal, payload)
        return JSONResponse(
            status_code=response.status_code,
            content=new_payload,
            headers=_safe_headers(response),
        )
    except Exception:  # noqa: BLE001
        # Authz/filter failure — fail CLOSED with an empty list, never the
        # unfiltered upstream payload (that would leak restricted agents).
        logger.warning(
            "control_plane: agent-list filtering failed; failing closed (empty list)",
            exc_info=True,
        )
        return JSONResponse(status_code=200, content={"data": [], "has_more": False})


def _restore_body(request: Request, raw: bytes) -> None:
    """Make ``request`` re-readable downstream after we consumed its body."""
    # Starlette caches the body on the request once read; also reset the
    # receive channel so any handler calling .stream()/.body() again works.
    request._body = raw  # type: ignore[attr-defined]

    async def receive() -> dict:
        return {"type": "http.request", "body": raw, "more_body": False}

    request._receive = receive  # type: ignore[attr-defined]


def _safe_headers(response: Response) -> dict[str, str]:
    """Copy headers worth preserving, dropping content-length (recomputed)
    and the streaming-specific transfer-encoding.
    """
    drop = {"content-length", "content-type", "transfer-encoding"}
    return {k: v for k, v in response.headers.items() if k.lower() not in drop}


def _rebuild_response(response: Response, body_bytes: bytes) -> Response:
    """Reconstruct a Response from a drained body iterator (unchanged body)."""
    return Response(
        content=body_bytes,
        status_code=response.status_code,
        headers=_safe_headers(response),
        media_type=response.media_type,
    )
