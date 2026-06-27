"""Regression tests for the CONTROL_PLANE_REVIEW_FINDINGS fixes + new features.

Covers, all against the real wired app (upstream ``create_app`` +
``attach_control_plane``):

- [P1] fail-closed enforcement (launch authz 503; list-filter empty on error);
- [P1] consumer use-only: multipart bundle upload denied for consumers;
- [P1] contributor management list hides restricted non-audience agents;
- new feature: DELETE own custom (published/template) agent;
- new feature: POST /agents/{id}/test connection check.
"""

from __future__ import annotations

import control_plane.identity as cp_identity
from control_plane.acl_store import AgentAclStore
from omnigent.db.utils import generate_agent_id
from omnigent.server.bundles import bundle_location
from tests.control_plane.conftest import auth_headers, build_valid_bundle

ADMIN = "admin@db.com"
CONTRIB = "carol@db.com"
CONSUMER = "dave@db.com"
AUDIENCE_USER = "bob@db.com"


def _set_groups(group_map, mapping: dict[str, list[str]]) -> None:
    group_map.clear()
    group_map.update(mapping)
    cp_identity.set_group_overrides(group_map)


def _seed_template_agent(app, *, name: str, bundle: bytes | None = None) -> str:
    """Create a built-in (template) agent; optionally back it with a real
    bundle stored in the artifact store and return its id."""
    agent_store = app.state.cp_agent_store
    aid = generate_agent_id()
    if bundle is not None:
        loc = bundle_location(aid, bundle)
        app.state.cp_artifact_store.put(loc, bundle)
    else:
        loc = f"loc/{name}"
    agent_store.create(agent_id=aid, name=name, bundle_location=loc, description="")
    return aid


def _restrict(cp_client, aid: str, *, users: list[str], groups: list[str] | None = None) -> None:
    r = cp_client.patch(
        f"/v1/control-plane/agents/{aid}/visibility",
        headers=auth_headers(ADMIN),
        json={"visibility": "restricted", "audience": {"users": users, "groups": groups or []}},
    )
    assert r.status_code == 200, r.text


# ── Fail closed: launch authz + list filter ──────────────────────


def test_launch_fails_closed_when_acl_unavailable(cp_client, cp_app, group_map, monkeypatch) -> None:
    """If the ACL lookup raises, a governed launch must 503 (not pass through)."""
    _set_groups(group_map, {})
    sec_id = _seed_template_agent(cp_app, name="secret-agent")
    _restrict(cp_client, sec_id, users=[AUDIENCE_USER])

    # Break the ACL read used by the enforcement middleware's _authorize_launch.
    monkeypatch.setattr(
        AgentAclStore, "get_visibility", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    r = cp_client.post("/v1/sessions", headers=auth_headers(CONSUMER), json={"agent_id": sec_id})
    assert r.status_code == 503, f"governed launch must fail closed, got {r.status_code}: {r.text[:200]}"


def test_list_filter_fails_closed_when_acl_unavailable(cp_client, cp_app, group_map, monkeypatch) -> None:
    """If filtering raises, GET /v1/agents returns an empty list, never the
    unfiltered upstream payload (no leak)."""
    _set_groups(group_map, {})
    _seed_template_agent(cp_app, name="org-agent")
    sec_id = _seed_template_agent(cp_app, name="secret-agent")
    _restrict(cp_client, sec_id, users=[AUDIENCE_USER])

    monkeypatch.setattr(
        AgentAclStore,
        "get_visibility_map",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    r = cp_client.get("/v1/agents?limit=1000", headers=auth_headers(CONSUMER))
    assert r.status_code == 200, r.text
    assert r.json()["data"] == [], "filter failure must yield empty list, not the unfiltered catalog"


def test_unrelated_request_unaffected_by_acl_failure(cp_client, cp_app, group_map, monkeypatch) -> None:
    """Fail-closed is scoped to governed paths — /me still works on ACL error."""
    _set_groups(group_map, {})
    monkeypatch.setattr(
        AgentAclStore, "get_visibility", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db"))
    )
    r = cp_client.get("/v1/control-plane/me", headers=auth_headers(CONSUMER))
    assert r.status_code == 200, r.text


# ── Consumer use-only: multipart upload deny ──────────────────────


def _multipart_create(cp_client, email: str):
    """POST a multipart bundle create as *email*."""
    return cp_client.post(
        "/v1/sessions",
        headers=auth_headers(email),
        files={"bundle": ("agent.tar.gz", build_valid_bundle(), "application/gzip")},
        data={"metadata": "{}"},
    )


def test_consumer_multipart_create_denied_by_default(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {})
    r = _multipart_create(cp_client, CONSUMER)
    assert r.status_code == 403, f"consumer upload must be denied: {r.status_code} {r.text[:200]}"


def test_contributor_multipart_create_not_denied_by_control_plane(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    r = _multipart_create(cp_client, CONTRIB)
    assert r.status_code != 403, f"contributor upload must not be CP-denied: {r.status_code}"


def test_admin_multipart_create_not_denied(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    r = _multipart_create(cp_client, ADMIN)
    assert r.status_code != 403


def test_consumer_json_create_still_open_for_org_agent(cp_client, cp_app, group_map) -> None:
    """The multipart deny must not touch the JSON launch path."""
    _set_groups(group_map, {})
    org_id = _seed_template_agent(cp_app, name="org-agent")
    r = cp_client.post("/v1/sessions", headers=auth_headers(CONSUMER), json={"agent_id": org_id})
    assert r.status_code != 403, f"JSON org-agent launch must stay open: {r.status_code}"


def test_consumer_multipart_allowed_when_policy_allow(db_uri, tmp_path, cp_env, group_map, monkeypatch) -> None:
    """OMNIGENT_CP_CONSUMER_UPLOAD=allow restores upload for consumers."""
    monkeypatch.setenv("OMNIGENT_CP_CONSUMER_UPLOAD", "allow")
    # Build a fresh app so the policy is read at attach time.
    from fastapi.testclient import TestClient

    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from control_plane.config import ControlPlaneConfig
    from control_plane.wiring import attach_control_plane

    _set_groups(group_map, {})
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    auth_provider = UnifiedAuthProvider(source="header", local_single_user=False)
    app = create_app(
        agent_store=agent_store,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=auth_provider,
    )
    attach_control_plane(
        app,
        db_uri=db_uri,
        agent_store=agent_store,
        conversation_store=conversation_store,
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=auth_provider,
        config=ControlPlaneConfig.from_env(),
    )
    client = TestClient(app, raise_server_exceptions=True)
    try:
        r = client.post(
            "/v1/sessions",
            headers=auth_headers(CONSUMER),
            files={"bundle": ("agent.tar.gz", build_valid_bundle(), "application/gzip")},
            data={"metadata": "{}"},
        )
        assert r.status_code != 403, f"policy=allow must not deny consumer upload: {r.status_code}"
    finally:
        client.close()


# ── Contributor management-list leak ──────────────────────────────


def test_management_list_hides_restricted_from_non_audience_contributor(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    org_id = _seed_template_agent(cp_app, name="org-agent")
    sec_id = _seed_template_agent(cp_app, name="secret-agent")
    _restrict(cp_client, sec_id, users=[AUDIENCE_USER])

    r = cp_client.get("/v1/control-plane/agents", headers=auth_headers(CONTRIB))
    assert r.status_code == 200, r.text
    ids = {a["id"] for a in r.json()["data"]}
    assert org_id in ids, "contributor sees org agents in management list"
    assert sec_id not in ids, "non-audience contributor must NOT see restricted agent in mgmt list"


def test_management_list_shows_restricted_to_audience_and_admin(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {AUDIENCE_USER: ["gtm-contributors"]})
    sec_id = _seed_template_agent(cp_app, name="secret-agent")
    _restrict(cp_client, sec_id, users=[AUDIENCE_USER])
    # Audience contributor sees it; admin sees it.
    aud = {a["id"] for a in cp_client.get("/v1/control-plane/agents", headers=auth_headers(AUDIENCE_USER)).json()["data"]}
    assert sec_id in aud
    adm = {a["id"] for a in cp_client.get("/v1/control-plane/agents", headers=auth_headers(ADMIN)).json()["data"]}
    assert sec_id in adm


# ── Delete own custom agent ───────────────────────────────────────


def test_owner_can_delete_own_published_agent(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    aid = _seed_template_agent(cp_app, name="carol-custom")
    # Make CONTRIB the owner via the ACL store the app uses.
    _acl_store(cp_app).set_owner(aid, CONTRIB)

    r = cp_client.delete(f"/v1/control-plane/agents/{aid}", headers=auth_headers(CONTRIB))
    assert r.status_code == 204, r.text
    assert cp_app.state.cp_agent_store.get(aid) is None, "agent row removed"


def test_non_owner_non_admin_cannot_delete(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    aid = _seed_template_agent(cp_app, name="someone-else-custom")
    _acl_store(cp_app).set_owner(aid, "someone-else@db.com")
    r = cp_client.delete(f"/v1/control-plane/agents/{aid}", headers=auth_headers(CONTRIB))
    assert r.status_code == 403, r.text
    assert cp_app.state.cp_agent_store.get(aid) is not None, "agent must still exist"


def test_admin_can_delete_any_agent_and_audit_written(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    aid = _seed_template_agent(cp_app, name="orphan-agent")
    r = cp_client.delete(f"/v1/control-plane/agents/{aid}", headers=auth_headers(ADMIN))
    assert r.status_code == 204, r.text
    assert cp_app.state.cp_agent_store.get(aid) is None
    audit = cp_client.get("/v1/control-plane/audit", headers=auth_headers(ADMIN)).json()["data"]
    assert any(e["action"] == "delete" and e["agent_id"] == aid for e in audit)


def test_delete_unknown_agent_404(cp_client, group_map) -> None:
    _set_groups(group_map, {})
    r = cp_client.delete("/v1/control-plane/agents/ag_nope", headers=auth_headers(ADMIN))
    assert r.status_code == 404


# ── Test-connection endpoint ──────────────────────────────────────


def test_test_endpoint_ok_for_wellformed_agent(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    aid = _seed_template_agent(cp_app, name="good-agent", bundle=build_valid_bundle())
    r = cp_client.post(f"/v1/control-plane/agents/{aid}/test", headers=auth_headers(ADMIN))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body["harness"], body
    names = {c["name"]: c["ok"] for c in body["checks"]}
    assert names.get("bundle_present") and names.get("bundle_loadable") and names.get("spec_valid")


def test_test_endpoint_fails_when_bundle_missing(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    aid = _seed_template_agent(cp_app, name="no-bundle")  # fake loc/ key
    r = cp_client.post(f"/v1/control-plane/agents/{aid}/test", headers=auth_headers(ADMIN))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    present = next(c for c in body["checks"] if c["name"] == "bundle_present")
    assert present["ok"] is False


def test_test_endpoint_fails_for_unloadable_bundle(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    # Artifact present but garbage bytes.
    aid = _seed_template_agent(cp_app, name="garbage", bundle=b"not a tarball")
    r = cp_client.post(f"/v1/control-plane/agents/{aid}/test", headers=auth_headers(ADMIN))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    names = {c["name"]: c["ok"] for c in body["checks"]}
    assert names.get("bundle_present") is True
    assert names.get("bundle_loadable") is False


def test_test_endpoint_unknown_agent_404(cp_client, group_map) -> None:
    _set_groups(group_map, {})
    r = cp_client.post("/v1/control-plane/agents/ag_nope/test", headers=auth_headers(ADMIN))
    assert r.status_code == 404


def test_test_endpoint_forbidden_for_non_audience(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {})
    aid = _seed_template_agent(cp_app, name="secret-agent", bundle=build_valid_bundle())
    _restrict(cp_client, aid, users=[AUDIENCE_USER])
    # Non-audience consumer denied; audience user + admin allowed.
    assert cp_client.post(f"/v1/control-plane/agents/{aid}/test", headers=auth_headers(CONSUMER)).status_code == 403
    assert cp_client.post(f"/v1/control-plane/agents/{aid}/test", headers=auth_headers(AUDIENCE_USER)).status_code != 403
    assert cp_client.post(f"/v1/control-plane/agents/{aid}/test", headers=auth_headers(ADMIN)).status_code != 403


# ── Usage lineage: fork/switch clones roll up to the template ─────


def _stamp_usage(cp_app, session_id: str, *, cost: float, tokens: int) -> None:
    import json as _json

    from sqlalchemy import text

    from omnigent.db.utils import get_or_create_engine

    eng = get_or_create_engine(cp_app.state.cp_db_uri)
    with eng.begin() as conn:
        conn.execute(
            text("UPDATE conversations SET session_usage = :u WHERE id = :i"),
            {"u": _json.dumps({"total_cost_usd": cost, "total_tokens": tokens}), "i": session_id},
        )


def test_usage_rolls_up_fork_clone_to_template(cp_client, cp_app, group_map) -> None:
    """A fork/switch clone (session-scoped agent whose bundle_location prefix
    is a template id) has its usage attributed to the template, not the clone."""
    _set_groups(group_map, {})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    conv_store = cp_app.state.cp_conversation_store

    # Template T with a content-addressed bundle key "T_id/sha".
    t_id = _seed_template_agent(cp_app, name="shared-template", bundle=build_valid_bundle())
    t_agent = cp_app.state.cp_agent_store.get(t_id)
    template_prefix = t_agent.bundle_location.split("/", 1)[0]
    assert template_prefix == t_id

    # A fork clone: session-scoped agent whose bundle_location reuses T's prefix.
    clone_id = generate_agent_id()
    created = conv_store.create_session_with_agent(
        agent_id=clone_id,
        agent_name="shared-template",  # clones keep the source name
        agent_bundle_location=f"{t_id}/{'a' * 64}",  # same template prefix, new sha
        agent_description=None,
    )
    _stamp_usage(cp_app, created.conversation.id, cost=3.5, tokens=700)

    report = cp_client.get("/v1/control-plane/usage", headers=auth_headers(ADMIN)).json()
    rows = {r["agent_id"]: r for r in report["data"]}
    assert t_id in rows, "clone usage must roll up under the template id"
    assert rows[t_id]["total_cost_usd"] == 3.5
    assert clone_id not in rows, "the clone id must NOT appear as its own usage row"


def test_usage_native_session_agent_grouped_standalone(cp_client, cp_app, group_map) -> None:
    """A session-scoped agent whose bundle prefix is NOT a template id keeps
    its own grouping (no false roll-up)."""
    _set_groups(group_map, {})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    conv_store = cp_app.state.cp_conversation_store
    aid = generate_agent_id()
    created = conv_store.create_session_with_agent(
        agent_id=aid,
        agent_name="native-agent",
        agent_bundle_location="not-a-template/abc",  # prefix is not a template id
        agent_description=None,
    )
    _stamp_usage(cp_app, created.conversation.id, cost=1.0, tokens=100)
    report = cp_client.get("/v1/control-plane/usage", headers=auth_headers(ADMIN)).json()
    rows = {r["agent_id"]: r for r in report["data"]}
    assert aid in rows, "native session agent groups under its own id"
    assert rows[aid]["total_cost_usd"] == 1.0


def test_published_from_fork_attributes_usage_to_itself_not_source_template(
    cp_client, cp_app, group_map
) -> None:
    """Regression (review HIGH): publishing an agent whose source is a
    fork/switch clone of template T must re-bundle under the NEW id, so the
    published agent's usage is attributed to ITSELF, not to T.

    Before the fix, publish reused the source bundle_location (prefix=T), so
    `_template_id_for` booked all the published agent's cost against T.
    """
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    conv_store = cp_app.state.cp_conversation_store
    perm_store = cp_app.state.cp_permission_store

    # Template T with a real bundle, so its prefix is a live template id.
    t_id = _seed_template_agent(cp_app, name="source-template", bundle=build_valid_bundle())
    # A real fork clone reuses the template's exact (content-addressed)
    # bundle_location — same artifact, prefix = T's id.
    t_location = cp_app.state.cp_agent_store.get(t_id).bundle_location

    # Contributor owns a session whose agent is a FORK CLONE of T.
    clone_id = generate_agent_id()
    created = conv_store.create_session_with_agent(
        agent_id=clone_id,
        agent_name="forked-from-T",
        agent_bundle_location=t_location,
        agent_description=None,
    )
    src_session = created.conversation.id
    perm_store.ensure_user(CONTRIB)
    perm_store.grant(CONTRIB, src_session, 4)  # LEVEL_OWNER

    # Publish the fork clone into the catalog.
    r = cp_client.post(
        "/v1/control-plane/agents/publish",
        headers=auth_headers(CONTRIB),
        json={"source_session_id": src_session, "name": "published-from-fork", "visibility": "org"},
    )
    assert r.status_code == 200, r.text
    pub_id = r.json()["agent_id"]

    # The published agent's bundle_location prefix must be its OWN id, not T.
    pub_agent = cp_app.state.cp_agent_store.get(pub_id)
    assert pub_agent.bundle_location.split("/", 1)[0] == pub_id, (
        f"published agent must be re-bundled under its own id, got {pub_agent.bundle_location}"
    )

    # Run a session on the PUBLISHED agent and stamp usage.
    pub_session = conv_store.create_session_with_agent(
        agent_id=generate_agent_id(),
        agent_name="published-from-fork",
        agent_bundle_location=f"{pub_id}/{'c' * 64}",  # a launch of the published template
        agent_description=None,
    )
    _stamp_usage(cp_app, pub_session.conversation.id, cost=9.0, tokens=900)

    report = cp_client.get("/v1/control-plane/usage", headers=auth_headers(ADMIN)).json()
    rows = {row["agent_id"]: row for row in report["data"]}
    assert pub_id in rows, "published agent's usage must be attributed to itself"
    assert rows[pub_id]["total_cost_usd"] == 9.0
    # T must NOT be inflated by the published agent's spend.
    assert rows.get(t_id, {}).get("total_cost_usd", 0) == 0, "source template must not be inflated"


# ── Dry-run bundle validation (custom-agent smoke test) ──────────


def test_validate_bundle_ok_for_wellformed_bundle(cp_client, cp_app, group_map) -> None:
    """The composer's smoke test: a valid bundle validates without persisting."""
    _set_groups(group_map, {})
    r = cp_client.post(
        "/v1/control-plane/agents/validate-bundle",
        headers=auth_headers(CONSUMER),
        files={"bundle": ("agent.tar.gz", build_valid_bundle(), "application/gzip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body["harness"], body
    assert body["agent_id"] is None  # nothing persisted
    assert any(c["name"] == "bundle_valid" and c["ok"] for c in body["checks"])


def test_validate_bundle_reports_invalid_bundle(cp_client, cp_app, group_map) -> None:
    """A garbage bundle returns ok=false with a check detail (no 500)."""
    _set_groups(group_map, {})
    r = cp_client.post(
        "/v1/control-plane/agents/validate-bundle",
        headers=auth_headers(CONSUMER),
        files={"bundle": ("agent.tar.gz", b"not a tarball", "application/gzip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert any(c["name"] == "bundle_valid" and not c["ok"] for c in body["checks"])


def test_validate_bundle_persists_nothing(cp_client, cp_app, group_map) -> None:
    """Validation must not create an agent row."""
    _set_groups(group_map, {})
    before = len(cp_app.state.cp_agent_store.list(limit=1000).data)
    cp_client.post(
        "/v1/control-plane/agents/validate-bundle",
        headers=auth_headers(CONSUMER),
        files={"bundle": ("agent.tar.gz", build_valid_bundle(), "application/gzip")},
    )
    after = len(cp_app.state.cp_agent_store.list(limit=1000).data)
    assert after == before, "validate-bundle must not persist an agent"


def test_validate_bundle_requires_auth(cp_client) -> None:
    r = cp_client.post(
        "/v1/control-plane/agents/validate-bundle",
        files={"bundle": ("agent.tar.gz", build_valid_bundle(), "application/gzip")},
    )
    assert r.status_code == 401


# ── P0 #1: template mutation via PUT /v1/sessions/{id}/agent ─────


def _session_bound_to(cp_app, agent_id: str, *, owner: str) -> str:
    """Create a conversation bound directly to *agent_id* (mirrors a JSON
    session-create against a template), owned by *owner*. Returns session id."""
    conv_store = cp_app.state.cp_conversation_store
    perm_store = cp_app.state.cp_permission_store
    conv = conv_store.create_conversation(agent_id=agent_id)
    perm_store.ensure_user(owner)
    perm_store.grant(owner, conv.id, 4)  # LEVEL_OWNER
    return conv.id


def test_template_update_blocked_for_non_owner(cp_client, cp_app, group_map) -> None:
    """A LEVEL_EDIT/owner of a session bound to a TEMPLATE cannot mutate the
    shared template via PUT /v1/sessions/{id}/agent unless they own it."""
    _set_groups(group_map, {})
    tmpl = _seed_template_agent(cp_app, name="org-template")  # owner_id None
    sess = _session_bound_to(cp_app, tmpl, owner=CONSUMER)
    # Consumer owns the SESSION but not the TEMPLATE → middleware 403s the PUT.
    r = cp_client.put(
        f"/v1/sessions/{sess}/agent",
        headers=auth_headers(CONSUMER),
        files={"bundle": ("agent.tar.gz", build_valid_bundle(), "application/gzip")},
        data={"metadata": "{}"},
    )
    assert r.status_code == 403, f"template mutation must be denied: {r.status_code} {r.text[:200]}"


def test_template_update_allowed_for_template_owner(cp_client, cp_app, group_map) -> None:
    """The template's owner is allowed past the control-plane guard (the
    upstream handler may still fail for unrelated reasons, but NOT our 403)."""
    _set_groups(group_map, {})
    tmpl = _seed_template_agent(cp_app, name="carol-template")
    _acl_store(cp_app).set_owner(tmpl, CONTRIB)
    sess = _session_bound_to(cp_app, tmpl, owner=CONTRIB)
    r = cp_client.put(
        f"/v1/sessions/{sess}/agent",
        headers=auth_headers(CONTRIB),
        files={"bundle": ("agent.tar.gz", build_valid_bundle(), "application/gzip")},
        data={"metadata": "{}"},
    )
    assert r.status_code != 403, f"template owner must not be CP-denied: {r.status_code}"


def test_session_scoped_agent_update_not_blocked(cp_client, cp_app, group_map) -> None:
    """The common case — editing a session's OWN session-scoped agent — must
    pass straight through to upstream's LEVEL_EDIT check (no CP 403)."""
    _set_groups(group_map, {})
    from tests.control_plane.test_routes_and_enforcement import _create_owned_session_agent

    sess, _ = _create_owned_session_agent(cp_app, owner=CONSUMER, name="dave-own")
    r = cp_client.put(
        f"/v1/sessions/{sess}/agent",
        headers=auth_headers(CONSUMER),
        files={"bundle": ("agent.tar.gz", build_valid_bundle(), "application/gzip")},
        data={"metadata": "{}"},
    )
    assert r.status_code != 403, f"session-scoped agent update must not be CP-denied: {r.status_code}"


# ── P1 #3: publish widening (template source rejected) ───────────


def test_publish_rejects_template_source(cp_client, cp_app, group_map) -> None:
    """Publishing must reject a source session bound to a TEMPLATE (only
    genuine session-scoped agents may be promoted) — closes the widening hole."""
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    tmpl = _seed_template_agent(cp_app, name="restricted-template")
    _restrict(cp_client, tmpl, users=[CONTRIB])  # contributor can VIEW it
    sess = _session_bound_to(cp_app, tmpl, owner=CONTRIB)
    r = cp_client.post(
        "/v1/control-plane/agents/publish",
        headers=auth_headers(CONTRIB),
        json={"source_session_id": sess, "name": "widened", "visibility": "org"},
    )
    assert r.status_code == 403, f"publish from a template source must be rejected: {r.status_code}"


# ── P1 #4: contributor usage leak (filter + redact) ──────────────


def test_usage_hides_restricted_agents_from_non_audience_contributor(
    cp_client, cp_app, group_map
) -> None:
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    org = _seed_template_agent(cp_app, name="org-agent")
    sec = _seed_template_agent(cp_app, name="secret-agent")
    _restrict(cp_client, sec, users=[AUDIENCE_USER])  # CONTRIB not in audience
    # Stamp usage on a session for each.
    _stamp_usage(cp_app, _session_bound_to(cp_app, org, owner="u1@db.com"), cost=1.0, tokens=10)
    _stamp_usage(cp_app, _session_bound_to(cp_app, sec, owner="u2@db.com"), cost=5.0, tokens=50)

    report = cp_client.get("/v1/control-plane/usage", headers=auth_headers(CONTRIB)).json()
    ids = {row["agent_id"] for row in report["data"]}
    assert org in ids, "contributor sees org-agent usage"
    assert sec not in ids, "non-audience contributor must NOT see restricted-agent usage"
    # Totals must not leak the hidden agent's spend.
    assert report["totals"]["total_cost_usd"] == 1.0


def test_usage_admin_sees_all_with_by_user(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    sec = _seed_template_agent(cp_app, name="secret-agent")
    _restrict(cp_client, sec, users=[AUDIENCE_USER])
    _stamp_usage(cp_app, _session_bound_to(cp_app, sec, owner="u2@db.com"), cost=5.0, tokens=50)
    report = cp_client.get("/v1/control-plane/usage", headers=auth_headers(ADMIN)).json()
    rows = {r["agent_id"]: r for r in report["data"]}
    assert sec in rows, "admin sees restricted-agent usage"
    assert rows[sec]["by_user"], "admin sees the by_user breakdown"


def test_usage_redacts_by_user_for_non_owner_viewer(cp_client, cp_app, group_map) -> None:
    """A contributor who can VIEW an org agent still must not see other users'
    per-user spend (by_user redacted unless owner/admin)."""
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    org = _seed_template_agent(cp_app, name="org-agent")  # owner_id None
    _stamp_usage(cp_app, _session_bound_to(cp_app, org, owner="someone@db.com"), cost=2.0, tokens=20)
    report = cp_client.get("/v1/control-plane/usage", headers=auth_headers(CONTRIB)).json()
    rows = {r["agent_id"]: r for r in report["data"]}
    assert org in rows, "contributor sees the org agent total"
    assert rows[org]["by_user"] == [], "by_user must be redacted for a non-owner viewer"


# ── P1 #5: template hard-delete cascade guard ────────────────────


def test_delete_blocked_when_template_referenced(cp_client, cp_app, group_map) -> None:
    """Deleting a template that a conversation binds must 409 (would cascade-
    delete session history), not destroy data."""
    _set_groups(group_map, {})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    tmpl = _seed_template_agent(cp_app, name="referenced-template")
    _session_bound_to(cp_app, tmpl, owner="u1@db.com")  # one bound session
    r = cp_client.delete(f"/v1/control-plane/agents/{tmpl}", headers=auth_headers(ADMIN))
    assert r.status_code == 409, f"referenced template delete must be blocked: {r.status_code}"
    assert cp_app.state.cp_agent_store.get(tmpl) is not None, "template must still exist"


def test_delete_allowed_for_unreferenced_template(cp_client, cp_app, group_map) -> None:
    """A never-launched template (no bound conversations) deletes cleanly."""
    _set_groups(group_map, {})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    tmpl = _seed_template_agent(cp_app, name="unused-template")
    r = cp_client.delete(f"/v1/control-plane/agents/{tmpl}", headers=auth_headers(ADMIN))
    assert r.status_code == 204, f"unreferenced template should delete: {r.status_code} {r.text[:200]}"
    assert cp_app.state.cp_agent_store.get(tmpl) is None


def _acl_store(cp_app):
    """Build an AgentAclStore over the app's DB (matches the wired one)."""
    from control_plane.acl_store import AgentAclStore as _A

    return _A(cp_app.state.cp_db_uri)
