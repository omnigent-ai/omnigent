"""Control-plane API router — ``/v1/control-plane/*``.

Surfaces the five governed capabilities via API (each also has a UI
affordance in the Admin page). Mounted on the upstream FastAPI app by the
deploy entry point; depends only on the control-plane components plus the
upstream ``agent_store`` / ``conversation_store`` (consumed, not forked).

Authorization is uniform: every handler resolves the caller to a
:class:`~control_plane.roles.ResolvedPrincipal` and gates on role /
capability / per-agent ownership. The same predicates power the
enforcement middleware, so the API and the request layer agree.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from control_plane.acl_store import AgentAclStore, AgentVisibility
from control_plane.audit_store import AuditStore
from control_plane.models import VISIBILITY_ORG, VISIBILITY_RESTRICTED
from control_plane.roles import RoleResolver
from control_plane.usage import UsageReporter
from omnigent.db.utils import builtin_agent_id
from omnigent.server.bundles import bundle_location
from omnigent.stores import AgentStore, ConversationStore

logger = logging.getLogger("omnigent-app.control_plane.routes")


# ── Request models ────────────────────────────────────────────────


class Audience(BaseModel):
    """Audience for a restricted agent."""

    users: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)


class VisibilityPatch(BaseModel):
    """Body for ``PATCH /agents/{id}/visibility``."""

    visibility: str
    audience: Audience = Field(default_factory=Audience)


class PublishRequest(BaseModel):
    """Body for ``POST /agents/publish``."""

    source_session_id: str
    name: str
    description: str | None = None
    visibility: str = VISIBILITY_ORG
    audience: Audience = Field(default_factory=Audience)


def _vis_to_dict(
    vis: AgentVisibility,
    *,
    viewer_can_manage: bool,
    name: str,
    description: str | None,
    created_at: int,
) -> dict:
    """Serialize a visibility record + agent metadata for the API."""
    return {
        "id": vis.agent_id,
        "name": name,
        "description": description,
        "visibility": vis.visibility,
        "audience": {"users": list(vis.audience_users), "groups": list(vis.audience_groups)},
        "owner_id": vis.owner_id,
        "created_at": created_at,
        "viewer_can_manage": viewer_can_manage,
    }


def create_control_plane_router(
    *,
    role_resolver: RoleResolver,
    acl_store: AgentAclStore,
    audit_store: AuditStore,
    usage_reporter: UsageReporter,
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    agent_cache=None,
    artifact_store=None,
) -> APIRouter:
    """Build the ``/v1/control-plane`` router.

    :param role_resolver: Resolves requests to principals.
    :param acl_store: Agent visibility + ACL store.
    :param audit_store: Audit log.
    :param usage_reporter: Per-agent usage read-model.
    :param agent_store: Upstream agent store (catalog + publish).
    :param conversation_store: Upstream conversation store (publish source).
    :param agent_cache: Upstream ``AgentCache`` for the connection-test
        endpoint (loads a bundle to parse its spec). Optional.
    :param artifact_store: Upstream ``ArtifactStore`` for the test endpoint
        (HEAD-checks a bundle artifact exists). Optional.
    :returns: A FastAPI router (mount at prefix ``/v1/control-plane``).
    """
    router = APIRouter()

    def _require_principal(request: Request):
        """Resolve the caller, raising 401 if unauthenticated."""
        principal = role_resolver.resolve(request)
        if principal.user_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return principal

    # ── Roles ─────────────────────────────────────────────────────

    @router.get("/me")
    async def me(request: Request) -> dict:
        """Return the caller's resolved role + capabilities."""
        principal = role_resolver.resolve(request)
        if principal.user_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return principal.to_me_dict()

    # ── Visibility management ─────────────────────────────────────

    @router.get("/agents")
    async def list_agents(request: Request) -> dict:
        """List every template agent with its visibility metadata.

        Admin/contributor only — this is the *management* list (consumers
        use the filtered ``GET /v1/agents`` discovery list).
        """
        principal = _require_principal(request)
        if not (principal.is_admin or principal.role == "contributor"):
            raise HTTPException(status_code=403, detail="Contributor or admin role required")

        # Page through all built-in agents (agent_store.list filters
        # session_id IS NULL). The catalog is small; one large page.
        page = agent_store.list(limit=1000, order="desc")
        agents = list(page.data)
        vis_map = acl_store.get_visibility_map([a.id for a in agents])
        data = []
        for a in agents:
            vis = vis_map[a.id]
            # Filter the management list by the SAME can_view predicate the
            # enforcement middleware uses, so a non-audience contributor can't
            # enumerate restricted agents here that GET /v1/agents hides.
            # Admins short-circuit can_view → True, so they still see all.
            if not AgentAclStore.can_view(
                vis,
                user_id=principal.user_id,
                groups=principal.groups,
                is_admin=principal.is_admin,
            ):
                continue
            can_manage = AgentAclStore.can_manage(
                vis, user_id=principal.user_id, is_admin=principal.is_admin
            )
            data.append(
                _vis_to_dict(
                    vis,
                    viewer_can_manage=can_manage,
                    name=a.name,
                    description=a.description,
                    created_at=a.created_at,
                )
            )
        return {"data": data}

    @router.patch("/agents/{agent_id}/visibility")
    async def set_visibility(agent_id: str, body: VisibilityPatch, request: Request) -> dict:
        """Set an agent's visibility + audience (admin: any; owner: own)."""
        principal = _require_principal(request)
        if body.visibility not in (VISIBILITY_ORG, VISIBILITY_RESTRICTED):
            raise HTTPException(status_code=400, detail=f"invalid visibility {body.visibility!r}")

        agent = agent_store.get(agent_id)
        if agent is None or agent.session_id is not None:
            # Only template (built-in) agents are governed here.
            raise HTTPException(status_code=404, detail="Agent not found")

        current = acl_store.get_visibility(agent_id)
        if not AgentAclStore.can_manage(
            current, user_id=principal.user_id, is_admin=principal.is_admin
        ):
            raise HTTPException(status_code=403, detail="Not the owner or an admin")

        updated = acl_store.set_visibility(
            agent_id,
            body.visibility,
            audience_users=body.audience.users,
            audience_groups=body.audience.groups,
        )
        audit_store.record(
            actor=principal.user_id,
            action="visibility_change",
            agent_id=agent_id,
            detail=(
                f"visibility={updated.visibility} "
                f"users={len(updated.audience_users)} groups={len(updated.audience_groups)}"
            ),
        )
        can_manage = AgentAclStore.can_manage(
            updated, user_id=principal.user_id, is_admin=principal.is_admin
        )
        return _vis_to_dict(
            updated,
            viewer_can_manage=can_manage,
            name=agent.name,
            description=agent.description,
            created_at=agent.created_at,
        )

    @router.delete("/agents/{agent_id}")
    async def delete_agent(agent_id: str, request: Request) -> Response:
        """Delete a template (custom/published) agent — admin: any; owner: own.

        Authorization mirrors ``PATCH visibility`` exactly (per-agent
        ``can_manage``). Removes the upstream agent row *and* its
        control-plane governance rows, and writes an audit entry. 404 for an
        unknown or non-template agent; 403 for a non-owner non-admin.
        """
        principal = _require_principal(request)
        agent = agent_store.get(agent_id)
        if agent is None or agent.session_id is not None:
            # Only template (built-in) agents are governed/deletable here.
            raise HTTPException(status_code=404, detail="Agent not found")
        current = acl_store.get_visibility(agent_id)
        if not AgentAclStore.can_manage(
            current, user_id=principal.user_id, is_admin=principal.is_admin
        ):
            raise HTTPException(status_code=403, detail="Not the owner or an admin")
        # Refuse the delete if any conversation still binds this template.
        # ``conversations.agent_id → agents.id`` is ON DELETE CASCADE, so hard-
        # deleting a referenced template would cascade-delete that session's
        # history. Block with 409 rather than destroy data (no soft-delete
        # column exists; adding one is a core change). A never-launched
        # template has zero references and deletes cleanly.
        ref_count = _conversations_referencing(conversation_store, agent_id)
        if ref_count:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Agent is bound to {ref_count} session(s); cannot delete while "
                    "referenced (would delete session history)."
                ),
            )
        # Capture metadata for the audit row before the row is gone.
        agent_name = agent.name
        owner_id = current.owner_id
        existed = agent_store.delete(agent_id)
        # Always scrub control-plane rows (idempotent), even on a delete race.
        acl_store.delete_agent(agent_id)
        if not existed:
            raise HTTPException(status_code=404, detail="Agent not found")
        audit_store.record(
            actor=principal.user_id,
            action="delete",
            agent_id=agent_id,
            detail=f"name={agent_name} owner={owner_id}",
        )
        return Response(status_code=204)

    @router.post("/agents/{agent_id}/test")
    async def test_agent(agent_id: str, request: Request) -> dict:
        """Quick connectivity / launchability check for a template agent.

        Cheap, no-runner checks that the saved agent is well-formed and
        loadable: (1) the record resolves; (2) its bundle artifact exists
        (HEAD, no download); (3) the bundle loads + spec parses; (4) the spec
        re-validates. Surfaces the resolved harness + model + MCP-server count
        for display. A genuine model/MCP-endpoint ping needs a full runner
        launch and is out of scope here.

        Authorized to anyone who can *view* the agent (owner / audience /
        contributor / admin) so they can self-serve a health check on an agent
        they could launch. 404 unknown/non-template; 403 if not viewable.
        """
        principal = _require_principal(request)
        agent = agent_store.get(agent_id)
        if agent is None or agent.session_id is not None:
            raise HTTPException(status_code=404, detail="Agent not found")
        vis = acl_store.get_visibility(agent_id)
        if not AgentAclStore.can_view(
            vis,
            user_id=principal.user_id,
            groups=principal.groups,
            is_admin=principal.is_admin,
        ):
            raise HTTPException(status_code=403, detail="Not authorized to test this agent")

        checks: list[dict] = []
        harness: str | None = None
        model: str | None = None
        mcp_count: int | None = None

        # 1) Record resolves (we already have it; record for completeness).
        checks.append(
            {"name": "agent_record", "ok": True, "detail": f"id={agent.id} v{agent.version}"}
        )

        # 2) Bundle artifact present (HEAD, no download). Requires the store.
        if artifact_store is None:
            checks.append(
                {
                    "name": "bundle_present",
                    "ok": False,
                    "detail": "artifact store unavailable in this deployment",
                }
            )
            present = False
        else:
            try:
                present = bool(artifact_store.exists(agent.bundle_location))
            except Exception as exc:  # noqa: BLE001
                present = False
                checks.append(
                    {"name": "bundle_present", "ok": False, "detail": f"check failed: {exc}"}
                )
            else:
                checks.append(
                    {
                        "name": "bundle_present",
                        "ok": present,
                        "detail": agent.bundle_location
                        if present
                        else f"missing artifact {agent.bundle_location}",
                    }
                )

        # 3) Bundle loads + spec parses (template ⇒ expand_env=True).
        if present and agent_cache is not None:
            try:
                loaded = agent_cache.load(agent.id, agent.bundle_location, expand_env=True)
                spec = loaded.spec
                harness = spec.executor.harness_kind
                model = spec.executor.model
                mcp_count = len(spec.mcp_servers)
                checks.append(
                    {
                        "name": "bundle_loadable",
                        "ok": True,
                        "detail": f"harness={harness} model={model or 'unset'} "
                        f"mcp_servers={mcp_count}",
                    }
                )
                # 4) Spec re-validates.
                from omnigent.spec import validate

                res = validate(spec)
                checks.append(
                    {
                        "name": "spec_valid",
                        "ok": res.valid,
                        "detail": "valid"
                        if res.valid
                        else "; ".join(f"{e.path}: {e.message}" for e in res.errors[:5]),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                checks.append(
                    {"name": "bundle_loadable", "ok": False, "detail": f"load failed: {exc}"}
                )
        elif present and agent_cache is None:
            checks.append(
                {
                    "name": "bundle_loadable",
                    "ok": False,
                    "detail": "agent cache unavailable in this deployment",
                }
            )

        ok = all(c["ok"] for c in checks)
        return {
            "ok": ok,
            "agent_id": agent_id,
            "harness": harness,
            "model": model,
            "mcp_server_count": mcp_count,
            "checks": checks,
        }

    @router.post("/agents/validate-bundle")
    async def validate_bundle(request: Request) -> dict:
        """Dry-run smoke test for a NOT-yet-created custom agent bundle.

        The composer builds an agent bundle client-side; this validates the
        raw bundle bytes (parse + spec validation) WITHOUT persisting an agent
        or creating a session, so the user gets a ✓/✗ preflight before launch.
        Reuses the same ``validate_agent_bundle`` the multipart create path
        runs. Returns the same ``{ok, checks, harness, model}`` shape as
        ``/agents/{id}/test`` for UI consistency.

        Honest scope: this is spec/bundle validation (does it parse + validate,
        what harness/model does it resolve), NOT a live model/MCP endpoint ping
        — a real ping needs a runner launch.

        Any authenticated caller may validate their own bundle (no agent
        exists yet to authorize against). Accepts multipart ``bundle`` (matching
        the create form) or a raw body.
        """
        principal = _require_principal(request)  # 401 if unauthenticated
        _ = principal
        # Read the bundle bytes from either a multipart "bundle" part or the
        # raw request body.
        bundle_bytes: bytes | None = None
        content_type = request.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() == "multipart/form-data":
            form = await request.form()
            up = form.get("bundle")
            if up is not None and hasattr(up, "read"):
                bundle_bytes = await up.read()
        else:
            bundle_bytes = await request.body()
        if not bundle_bytes:
            raise HTTPException(status_code=400, detail="No bundle provided")

        checks: list[dict] = []
        harness: str | None = None
        model: str | None = None
        mcp_count: int | None = None
        try:
            from omnigent.server.bundles import validate_agent_bundle

            spec = validate_agent_bundle(bundle_bytes)
            harness = spec.executor.harness_kind
            model = spec.executor.model
            mcp_count = len(spec.mcp_servers)
            checks.append(
                {
                    "name": "bundle_valid",
                    "ok": True,
                    "detail": f"harness={harness} model={model or 'unset'} "
                    f"mcp_servers={mcp_count}",
                }
            )
        except Exception as exc:  # noqa: BLE001 — report the validation failure
            checks.append(
                {"name": "bundle_valid", "ok": False, "detail": f"invalid bundle: {exc}"}
            )

        return {
            "ok": all(c["ok"] for c in checks),
            "agent_id": None,
            "harness": harness,
            "model": model,
            "mcp_server_count": mcp_count,
            "checks": checks,
        }

    # ── Delegated registration (publish) ──────────────────────────

    @router.get("/publishable")
    async def publishable(request: Request) -> dict:
        """List the caller's session-scoped agents eligible to publish.

        A session-scoped agent the caller owns can be promoted to a shared
        template. We surface the caller's owned sessions that carry an
        agent.
        """
        principal = _require_principal(request)
        if not principal.can_publish:
            raise HTTPException(status_code=403, detail="Contributor or admin role required")

        # Sessions the caller owns (LEVEL_OWNER) that have a session-scoped
        # agent. Use the usage/owner machinery: enumerate the caller's
        # owned top-level sessions via the permission store path.
        data = _list_publishable_for(principal.user_id, conversation_store, agent_store)
        return {"data": data}

    @router.post("/agents/publish")
    async def publish(body: PublishRequest, request: Request) -> dict:
        """Promote a session-scoped agent into the shared template catalog.

        Reuses the source agent's content-addressed bundle (no re-upload),
        sets owner = caller, applies the requested visibility, and writes
        an audit row. Consumers are denied.
        """
        principal = _require_principal(request)
        if not principal.can_publish:
            raise HTTPException(
                status_code=403, detail="Publishing requires contributor or admin role"
            )
        if body.visibility not in (VISIBILITY_ORG, VISIBILITY_RESTRICTED):
            raise HTTPException(status_code=400, detail=f"invalid visibility {body.visibility!r}")
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")

        # The source must be a session the caller owns, carrying an agent.
        conv = conversation_store.get_conversation(body.source_session_id)
        if conv is None or conv.agent_id is None:
            raise HTTPException(status_code=404, detail="Source session or its agent not found")
        owner = conversation_store.get_session_owner(body.source_session_id)
        if owner is None or owner.strip().lower() != principal.user_id.strip().lower():
            if not principal.is_admin:
                raise HTTPException(status_code=403, detail="You do not own the source session")

        source_agent = agent_store.get(conv.agent_id)
        if source_agent is None:
            raise HTTPException(status_code=404, detail="Source agent not found")

        # Only genuine session-scoped agents may be promoted. Re-validate the
        # invariant the publishable list applies (it skips templates) at the
        # ACTION layer: without this, a caller who can VIEW a restricted
        # template bound to one of their sessions could republish it under a
        # new, caller-chosen visibility — widening a restricted agent.
        if source_agent.session_id is None:
            raise HTTPException(
                status_code=403, detail="Only session-scoped agents may be published"
            )

        # Duplicate template name → 409.
        if agent_store.get_by_name(name) is not None:
            raise HTTPException(status_code=409, detail=f"An agent named {name!r} already exists")

        # Promote: create a NEW template agent. Re-bundle the source bytes
        # under the NEW id so the published agent's content-addressed
        # bundle_location prefix equals its own id — NOT the source's. If we
        # reused source_agent.bundle_location verbatim and the source was a
        # fork/switch clone, its prefix would be the *original* template, and
        # per-agent usage (which keys on the bundle prefix lineage) would
        # misattribute all of this agent's cost to that template. Re-keying
        # makes the published agent its own usage lineage root.
        new_id = builtin_agent_id(name)
        published_location = source_agent.bundle_location
        if artifact_store is not None:
            try:
                data = artifact_store.get(source_agent.bundle_location)
                published_location = bundle_location(new_id, data)
                artifact_store.put(published_location, data)
            except Exception as exc:  # noqa: BLE001 — fall back to reuse on store error
                logger.warning(
                    "publish: could not re-bundle %s under %s (%s); reusing source location",
                    source_agent.bundle_location,
                    new_id,
                    exc,
                )
                published_location = source_agent.bundle_location
        try:
            created = agent_store.create(
                agent_id=new_id,
                name=name,
                bundle_location=published_location,
                description=body.description or source_agent.description,
            )
        except Exception as exc:  # — surface uniqueness/other as 409/500
            logger.warning("publish failed for %s: %s", name, exc, exc_info=True)
            raise HTTPException(
                status_code=409, detail=f"Could not publish {name!r}: {exc}"
            ) from exc

        # Stamp ownership + visibility. If this fails after the agent row
        # committed, the agent would be left ownerless and (defaulting to)
        # org-visible — a fail-open governance gap. Compensate by deleting the
        # just-created agent + any partial cp_* rows before surfacing the error.
        try:
            acl_store.set_owner(created.id, principal.user_id, visibility=body.visibility)
            if body.visibility == VISIBILITY_RESTRICTED:
                acl_store.set_visibility(
                    created.id,
                    VISIBILITY_RESTRICTED,
                    audience_users=body.audience.users,
                    audience_groups=body.audience.groups,
                    owner_id=principal.user_id,
                )
        except Exception as exc:  # noqa: BLE001 — roll back the orphaned template
            logger.warning(
                "publish: governance write failed for %s; rolling back created agent %s (%s)",
                name,
                created.id,
                exc,
                exc_info=True,
            )
            try:
                agent_store.delete(created.id)
            finally:
                acl_store.delete_agent(created.id)
            raise HTTPException(
                status_code=500, detail=f"Could not publish {name!r}: governance write failed"
            ) from exc
        audit_store.record(
            actor=principal.user_id,
            action="publish",
            agent_id=created.id,
            detail=f"name={name} visibility={body.visibility} source={body.source_session_id}",
        )
        return {
            "agent_id": created.id,
            "name": created.name,
            "owner_id": principal.user_id,
            "visibility": body.visibility,
        }

    # ── Usage ──────────────────────────────────────────────────────

    @router.get("/usage")
    async def usage(request: Request, agent_id: str | None = Query(default=None)) -> dict:
        """Per-agent usage / cost report (admin + contributor).

        Rows are filtered to what the caller may view and ``by_user`` is
        redacted for non-owners, so a non-audience contributor can't enumerate
        restricted agents or other users' spend.
        """
        principal = _require_principal(request)
        if not principal.capabilities.get("can_view_usage"):
            raise HTTPException(status_code=403, detail="Contributor or admin role required")
        return usage_reporter.report(
            agent_id=agent_id,
            acl_store=acl_store,
            user_id=principal.user_id,
            groups=principal.groups,
            is_admin=principal.is_admin,
        )

    # ── Audit ──────────────────────────────────────────────────────

    @router.get("/audit")
    async def audit(request: Request, limit: int = Query(default=100, ge=1, le=1000)) -> dict:
        """Recent governed actions (admin only)."""
        principal = _require_principal(request)
        if not principal.is_admin:
            raise HTTPException(status_code=403, detail="Admin role required")
        entries = audit_store.list_recent(limit=limit)
        return {
            "data": [
                {
                    "id": e.id,
                    "ts": e.ts,
                    "actor": e.actor,
                    "action": e.action,
                    "agent_id": e.agent_id,
                    "detail": e.detail,
                }
                for e in entries
            ]
        }

    return router


def _conversations_referencing(conversation_store: ConversationStore, agent_id: str) -> int:
    """Count conversations whose ``agent_id`` binds this template directly.

    This is exactly the set ``ON DELETE CASCADE`` would destroy if the agent
    row were hard-deleted (``conversations.agent_id → agents.id``). Matches the
    raw FK column (not the tasks-join filter ``list_conversations`` uses, and
    not ``agent_name``). Counts only — never hydrates rows. On any store error
    returns a sentinel ``-1`` so the caller can fail safe (treat as referenced).
    """
    from sqlalchemy import func, select

    from omnigent.db.db_models import SqlConversation

    try:
        session_maker = conversation_store._session  # type: ignore[attr-defined]
    except AttributeError:
        return -1
    try:
        with session_maker() as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(SqlConversation)
                    .where(SqlConversation.agent_id == agent_id)
                ).scalar_one()
            )
    except Exception:  # noqa: BLE001 — fail safe: treat an error as "referenced"
        logger.warning(
            "control_plane: reference-count query failed for agent %s; refusing delete",
            agent_id,
            exc_info=True,
        )
        return -1


def _list_publishable_for(
    user_id: str,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
) -> list[dict]:
    """Return the caller's owned, agent-bearing sessions for publishing.

    Reads the permission store (via the conversation store's engine) for
    the caller's ``LEVEL_OWNER`` sessions, then keeps those whose
    conversation carries a session-scoped agent. Best-effort: any error
    yields an empty list (the publish form just shows nothing to pick).
    """
    from sqlalchemy import select

    from omnigent.db.db_models import SqlConversation, SqlSessionPermission

    try:
        session_maker = conversation_store._session  # type: ignore[attr-defined]
    except AttributeError:
        return []

    out: list[dict] = []
    with session_maker() as session:
        rows = session.execute(
            select(
                SqlConversation.id,
                SqlConversation.agent_id,
                SqlConversation.title,
            )
            .join(
                SqlSessionPermission,
                SqlSessionPermission.conversation_id == SqlConversation.id,
            )
            .where(SqlSessionPermission.user_id == user_id)
            .where(SqlSessionPermission.level >= 4)  # LEVEL_OWNER
            .where(SqlConversation.agent_id.is_not(None))
            .where(SqlConversation.parent_conversation_id.is_(None))
        ).all()
        candidates = [tuple(row) for row in rows]

    for cid, aid, title in candidates:
        agent = agent_store.get(aid)
        # Only session-scoped agents are publishable (session_id set).
        if agent is None or agent.session_id is None:
            continue
        out.append(
            {
                "session_id": cid,
                "agent_id": aid,
                "name": agent.name,
                "title": title,
            }
        )
    return out
