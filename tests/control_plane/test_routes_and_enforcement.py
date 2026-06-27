"""End-to-end API + enforcement tests for the GTM control plane.

Exercises all five features through the real wired app (upstream
``create_app`` + ``attach_control_plane``) with a ``TestClient`` and
header-injected identity. Emphasis on the denial / authorization paths
the build contract calls out:

- consumer cannot publish;
- a non-audience user cannot list *or* launch a restricted agent;
- a direct ``POST /v1/sessions`` binding a restricted agent is blocked at
  the request layer (the bypass path);
- usage is admin/contributor-only.
"""

from __future__ import annotations

import control_plane.identity as cp_identity
from omnigent.db.utils import builtin_agent_id, generate_agent_id
from tests.control_plane.conftest import auth_headers

ADMIN = "admin@db.com"
CONTRIB = "carol@db.com"
CONSUMER = "dave@db.com"
AUDIENCE_USER = "bob@db.com"


def _seed_template_agent(app, *, name: str, description: str = "") -> str:
    """Create a built-in (template) agent and return its id."""
    agent_store = app.state.cp_agent_store
    aid = generate_agent_id()
    agent_store.create(
        agent_id=aid, name=name, bundle_location=f"loc/{name}", description=description
    )
    return aid


def _set_groups(group_map, mapping: dict[str, list[str]]) -> None:
    group_map.clear()
    group_map.update(mapping)
    cp_identity.set_group_overrides(group_map)


# ── Feature 1: roles via /me ─────────────────────────────────────


def test_me_resolves_three_roles(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)

    assert (
        cp_client.get("/v1/control-plane/me", headers=auth_headers(ADMIN)).json()["role"]
        == "admin"
    )
    assert (
        cp_client.get("/v1/control-plane/me", headers=auth_headers(CONTRIB)).json()["role"]
        == "contributor"
    )
    assert (
        cp_client.get("/v1/control-plane/me", headers=auth_headers(CONSUMER)).json()["role"]
        == "consumer"
    )


def test_me_unauthenticated_401(cp_client) -> None:
    assert cp_client.get("/v1/control-plane/me").status_code == 401


# ── Feature 2: visibility management ─────────────────────────────


def test_admin_sets_visibility_and_consumer_cannot_list(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {})
    aid = _seed_template_agent(cp_app, name="secret-agent")

    # Consumer denied the management list.
    assert (
        cp_client.get("/v1/control-plane/agents", headers=auth_headers(CONSUMER)).status_code
        == 403
    )

    # Admin restricts the agent.
    r = cp_client.patch(
        f"/v1/control-plane/agents/{aid}/visibility",
        headers=auth_headers(ADMIN),
        json={
            "visibility": "restricted",
            "audience": {"users": [AUDIENCE_USER], "groups": ["fsi"]},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["visibility"] == "restricted"
    assert body["audience"]["users"] == [AUDIENCE_USER]
    assert body["owner_id"] is None  # operator-seeded agent has no owner; admin can still manage


def test_non_owner_non_admin_cannot_patch_visibility(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    aid = _seed_template_agent(cp_app, name="org-agent")
    # Contributor who is not the owner is denied managing someone else's agent.
    r = cp_client.patch(
        f"/v1/control-plane/agents/{aid}/visibility",
        headers=auth_headers(CONTRIB),
        json={"visibility": "restricted", "audience": {"users": [], "groups": []}},
    )
    assert r.status_code == 403, r.text


def test_patch_unknown_agent_404(cp_client, group_map) -> None:
    _set_groups(group_map, {})
    r = cp_client.patch(
        "/v1/control-plane/agents/ag_nope/visibility",
        headers=auth_headers(ADMIN),
        json={"visibility": "org", "audience": {"users": [], "groups": []}},
    )
    assert r.status_code == 404


# ── Feature 4: enforcement — list filtering ──────────────────────


def _visible_agent_ids(cp_client, email: str) -> set[str]:
    resp = cp_client.get("/v1/agents?limit=1000", headers=auth_headers(email))
    assert resp.status_code == 200, resp.text
    return {a["id"] for a in resp.json()["data"]}


def test_restricted_agent_hidden_from_non_audience(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {})
    org_id = _seed_template_agent(cp_app, name="org-agent")
    sec_id = _seed_template_agent(cp_app, name="secret-agent")
    cp_client.patch(
        f"/v1/control-plane/agents/{sec_id}/visibility",
        headers=auth_headers(ADMIN),
        json={
            "visibility": "restricted",
            "audience": {"users": [AUDIENCE_USER], "groups": ["fsi"]},
        },
    )

    consumer_sees = _visible_agent_ids(cp_client, CONSUMER)
    assert org_id in consumer_sees, "everyone sees org agents"
    assert sec_id not in consumer_sees, "non-audience must NOT see restricted agent"

    # Audience user and admin both see it.
    assert sec_id in _visible_agent_ids(cp_client, AUDIENCE_USER)
    assert sec_id in _visible_agent_ids(cp_client, ADMIN)

    # Group member sees it too.
    _set_groups(group_map, {"erin@db.com": ["fsi"]})
    assert sec_id in _visible_agent_ids(cp_client, "erin@db.com")


# ── Feature 4: enforcement — launch authorization (the bypass path) ──


def test_direct_session_create_blocked_for_restricted_agent(cp_client, cp_app, group_map) -> None:
    """The decisive test: a non-audience user calling POST /v1/sessions
    {agent_id} directly is blocked at the request layer with 403, before
    a session is created."""
    _set_groups(group_map, {})
    sec_id = _seed_template_agent(cp_app, name="secret-agent")
    cp_client.patch(
        f"/v1/control-plane/agents/{sec_id}/visibility",
        headers=auth_headers(ADMIN),
        json={"visibility": "restricted", "audience": {"users": [AUDIENCE_USER], "groups": []}},
    )

    r = cp_client.post("/v1/sessions", headers=auth_headers(CONSUMER), json={"agent_id": sec_id})
    assert r.status_code == 403, f"bypass must be blocked, got {r.status_code}: {r.text[:300]}"


def test_direct_session_create_blocked_with_json_suffix_content_type(
    cp_client, cp_app, group_map
) -> None:
    """Regression: the launch deny must not be bypassable by sending an
    ``application/<subtype>+json`` Content-Type. The upstream route parses
    the body as JSON regardless of the declared type, so the middleware must
    inspect every non-multipart body, not just literal ``application/json``.
    """
    _set_groups(group_map, {})
    sec_id = _seed_template_agent(cp_app, name="secret-agent")
    cp_client.patch(
        f"/v1/control-plane/agents/{sec_id}/visibility",
        headers=auth_headers(ADMIN),
        json={"visibility": "restricted", "audience": {"users": [AUDIENCE_USER], "groups": []}},
    )

    import json as _json

    for ct in ("application/vnd.api+json", "application/ld+json", "application/json; charset=utf-8"):
        r = cp_client.post(
            "/v1/sessions",
            headers={**auth_headers(CONSUMER), "content-type": ct},
            content=_json.dumps({"agent_id": sec_id}),
        )
        assert r.status_code == 403, f"+json bypass via {ct!r}: got {r.status_code}: {r.text[:200]}"

    # Whitespace-padded multipart: upstream JSON-parses this (no .strip on its
    # dispatch), so the deny MUST still fire — it must not be skipped as
    # multipart.
    for ct in ("multipart/form-data ; boundary=x", "multipart/form-data\t; boundary=x"):
        r = cp_client.post(
            "/v1/sessions",
            headers={**auth_headers(CONSUMER), "content-type": ct},
            content=_json.dumps({"agent_id": sec_id}),
        )
        assert r.status_code == 403, (
            f"padded-multipart bypass via {ct!r}: got {r.status_code}: {r.text[:200]}"
        )


def test_is_multipart_matches_upstream_dispatch_exactly() -> None:
    """Regression: the middleware's multipart skip-test must mirror the
    upstream dispatch byte-for-byte (split on ';', lower, NO strip). A
    whitespace-padded ``multipart/form-data ; ...`` is NOT multipart to
    upstream (it falls into the JSON branch and binds agent_id), so the
    middleware must not skip it either — otherwise the launch deny is
    bypassable by padding the Content-Type with a space.
    """
    from control_plane.enforcement import _is_multipart

    # Genuine multipart (upstream treats as multipart → skip is correct).
    assert _is_multipart("multipart/form-data") is True
    assert _is_multipart("multipart/form-data; boundary=x") is True
    assert _is_multipart("MULTIPART/FORM-DATA; boundary=x") is True
    # Whitespace BEFORE the ';' — upstream does NOT treat as multipart
    # (no .strip()), so neither may we, or the deny is skipped.
    assert _is_multipart("multipart/form-data ; boundary=x") is False
    assert _is_multipart("multipart/form-data\t; boundary=x") is False
    # JSON / other bodies are never multipart → always inspected.
    assert _is_multipart("application/json") is False
    assert _is_multipart("application/vnd.api+json") is False
    assert _is_multipart("") is False


def test_fork_blocked_for_restricted_target_agent(cp_client, cp_app, group_map) -> None:
    """Regression: POST /v1/sessions/{id}/fork binds ``body.agent_id`` to a
    template, an ungoverned launch path the exact-match middleware missed.
    A non-audience user forking onto a restricted template is blocked at 403.
    """
    _set_groups(group_map, {})
    sec_id = _seed_template_agent(cp_app, name="secret-agent")
    cp_client.patch(
        f"/v1/control-plane/agents/{sec_id}/visibility",
        headers=auth_headers(ADMIN),
        json={"visibility": "restricted", "audience": {"users": [AUDIENCE_USER], "groups": []}},
    )
    # Consumer owns some source session; forking it onto the restricted
    # template must be denied before the fork runs.
    src_session, _ = _create_owned_session_agent(cp_app, owner=CONSUMER, name="dave-src")
    r = cp_client.post(
        f"/v1/sessions/{src_session}/fork",
        headers=auth_headers(CONSUMER),
        json={"agent_id": sec_id},
    )
    assert r.status_code == 403, f"fork bypass must be blocked: {r.status_code} {r.text[:200]}"


def test_switch_agent_blocked_for_restricted_target_agent(cp_client, cp_app, group_map) -> None:
    """Regression: POST /v1/sessions/{id}/switch-agent binds ``body.agent_id``
    to a template — a second ungoverned launch path. Non-audience switch onto
    a restricted template is blocked at 403.
    """
    _set_groups(group_map, {})
    sec_id = _seed_template_agent(cp_app, name="secret-agent")
    cp_client.patch(
        f"/v1/control-plane/agents/{sec_id}/visibility",
        headers=auth_headers(ADMIN),
        json={"visibility": "restricted", "audience": {"users": [AUDIENCE_USER], "groups": []}},
    )
    src_session, _ = _create_owned_session_agent(cp_app, owner=CONSUMER, name="dave-src2")
    r = cp_client.post(
        f"/v1/sessions/{src_session}/switch-agent",
        headers=auth_headers(CONSUMER),
        json={"agent_id": sec_id},
    )
    assert r.status_code == 403, f"switch-agent bypass must be blocked: {r.status_code} {r.text[:200]}"


def test_fork_and_switch_allowed_for_org_agent(cp_client, cp_app, group_map) -> None:
    """An org-visible template is never blocked by the control plane on the
    fork / switch-agent paths (downstream may still fail for unrelated
    reasons, but not with the control-plane 403)."""
    _set_groups(group_map, {})
    org_id = _seed_template_agent(cp_app, name="org-agent")
    src_session, _ = _create_owned_session_agent(cp_app, owner=CONSUMER, name="dave-src3")
    for route in ("fork", "switch-agent"):
        r = cp_client.post(
            f"/v1/sessions/{src_session}/{route}",
            headers=auth_headers(CONSUMER),
            json={"agent_id": org_id},
        )
        assert r.status_code != 403, f"org agent on {route} must not be blocked: {r.status_code}"


def test_org_agent_launch_not_blocked_by_control_plane(cp_client, cp_app, group_map) -> None:
    """An org-visible agent is never blocked by the control plane.

    (The create may still fail downstream for unrelated reasons — e.g. no
    runner — but it must NOT be the control plane's 403.)
    """
    _set_groups(group_map, {})
    org_id = _seed_template_agent(cp_app, name="org-agent")
    r = cp_client.post("/v1/sessions", headers=auth_headers(CONSUMER), json={"agent_id": org_id})
    assert r.status_code != 403, f"org agent must not be blocked: {r.status_code} {r.text[:200]}"


def test_audience_user_launch_allowed(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {})
    sec_id = _seed_template_agent(cp_app, name="secret-agent")
    cp_client.patch(
        f"/v1/control-plane/agents/{sec_id}/visibility",
        headers=auth_headers(ADMIN),
        json={"visibility": "restricted", "audience": {"users": [AUDIENCE_USER], "groups": []}},
    )
    r = cp_client.post(
        "/v1/sessions", headers=auth_headers(AUDIENCE_USER), json={"agent_id": sec_id}
    )
    assert (
        r.status_code != 403
    ), f"audience user must not be blocked: {r.status_code} {r.text[:200]}"


# ── Feature 3: delegated registration (publish) ──────────────────


def _create_owned_session_agent(cp_app, *, owner: str, name: str) -> tuple[str, str]:
    """Create a session-scoped agent owned by *owner*; return (session_id, agent_id)."""
    conv_store = cp_app.state.cp_conversation_store
    perm_store = cp_app.state.cp_permission_store
    agent_id = generate_agent_id()
    created = conv_store.create_session_with_agent(
        agent_id=agent_id,
        agent_name=name,
        agent_bundle_location=f"bundle/{name}",
        agent_description="a private agent",
    )
    session_id = created.conversation.id
    perm_store.ensure_user(owner)
    perm_store.grant(owner, session_id, 4)  # LEVEL_OWNER
    return session_id, agent_id


def test_consumer_cannot_publish(cp_client, cp_app, group_map) -> None:
    """The required denial: consumers cannot publish."""
    _set_groups(group_map, {})
    session_id, _ = _create_owned_session_agent(cp_app, owner=CONSUMER, name="dave-agent")
    r = cp_client.post(
        "/v1/control-plane/agents/publish",
        headers=auth_headers(CONSUMER),
        json={"source_session_id": session_id, "name": "published", "visibility": "org"},
    )
    assert r.status_code == 403, r.text


def test_contributor_publishes_and_audit_written(cp_client, cp_app, group_map) -> None:
    """A contributor publishes a session agent → template catalog; audit row written."""
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    session_id, _ = _create_owned_session_agent(cp_app, owner=CONTRIB, name="carol-agent")

    r = cp_client.post(
        "/v1/control-plane/agents/publish",
        headers=auth_headers(CONTRIB),
        json={
            "source_session_id": session_id,
            "name": "deal-helper",
            "description": "helps with deals",
            "visibility": "restricted",
            "audience": {"users": [AUDIENCE_USER], "groups": []},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "deal-helper"
    assert body["owner_id"] == CONTRIB
    assert body["visibility"] == "restricted"

    # The published agent now exists as a template and is owner-managed.
    new_id = body["agent_id"]
    assert new_id == builtin_agent_id("deal-helper")
    agent = cp_app.state.cp_agent_store.get(new_id)
    assert agent is not None and agent.session_id is None

    # Audit captured the publish (admin-only read).
    audit = cp_client.get("/v1/control-plane/audit", headers=auth_headers(ADMIN)).json()["data"]
    assert any(e["action"] == "publish" and e["agent_id"] == new_id for e in audit)


def test_publish_duplicate_name_conflict(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    # A template named "taken" already exists.
    _seed_template_agent(cp_app, name="taken")
    session_id, _ = _create_owned_session_agent(cp_app, owner=CONTRIB, name="carol-agent")
    r = cp_client.post(
        "/v1/control-plane/agents/publish",
        headers=auth_headers(CONTRIB),
        json={"source_session_id": session_id, "name": "taken", "visibility": "org"},
    )
    assert r.status_code == 409, r.text


def test_publish_requires_ownership(cp_client, cp_app, group_map) -> None:
    """A contributor cannot publish a session they do not own."""
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    session_id, _ = _create_owned_session_agent(
        cp_app, owner="someone-else@db.com", name="not-mine"
    )
    r = cp_client.post(
        "/v1/control-plane/agents/publish",
        headers=auth_headers(CONTRIB),
        json={"source_session_id": session_id, "name": "stolen", "visibility": "org"},
    )
    assert r.status_code in (403, 404), r.text


def test_publishable_lists_owned_session_agents(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    session_id, agent_id = _create_owned_session_agent(cp_app, owner=CONTRIB, name="carol-agent")
    r = cp_client.get("/v1/control-plane/publishable", headers=auth_headers(CONTRIB))
    assert r.status_code == 200, r.text
    ids = {item["session_id"] for item in r.json()["data"]}
    assert session_id in ids


# ── Feature 5: usage ─────────────────────────────────────────────


def test_usage_denied_to_consumer_allowed_to_admin(cp_client, cp_app, group_map) -> None:
    _set_groups(group_map, {CONTRIB: ["gtm-contributors"]})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    assert (
        cp_client.get("/v1/control-plane/usage", headers=auth_headers(CONSUMER)).status_code == 403
    )
    assert cp_client.get("/v1/control-plane/usage", headers=auth_headers(ADMIN)).status_code == 200
    assert (
        cp_client.get("/v1/control-plane/usage", headers=auth_headers(CONTRIB)).status_code == 200
    )


def test_usage_aggregates_by_agent(cp_client, cp_app, group_map) -> None:
    """Usage attributes recorded session cost to the agent + owner."""
    _set_groups(group_map, {})
    cp_app.state.cp_permission_store.ensure_user(ADMIN)
    conv_store = cp_app.state.cp_conversation_store
    perm_store = cp_app.state.cp_permission_store

    # Create a session bound to an agent and stamp some usage on it.
    from omnigent.db.utils import generate_agent_id as _gen

    aid = _gen()
    created = conv_store.create_session_with_agent(
        agent_id=aid,
        agent_name="metered-agent",
        agent_bundle_location="bundle/metered",
        agent_description=None,
    )
    session_id = created.conversation.id
    perm_store.ensure_user(AUDIENCE_USER)
    perm_store.grant(AUDIENCE_USER, session_id, 4)

    # Write session_usage directly (the cost path's persisted shape).
    import json as _json

    from sqlalchemy import text

    from omnigent.db.utils import get_or_create_engine

    eng = get_or_create_engine(cp_app.state.cp_db_uri)
    with eng.begin() as conn:
        conn.execute(
            text("UPDATE conversations SET session_usage = :u WHERE id = :i"),
            {"u": _json.dumps({"total_cost_usd": 4.25, "total_tokens": 1000}), "i": session_id},
        )

    report = cp_client.get("/v1/control-plane/usage", headers=auth_headers(ADMIN)).json()
    rows = {r["agent_id"]: r for r in report["data"]}
    assert aid in rows
    assert rows[aid]["total_cost_usd"] == 4.25
    assert rows[aid]["total_tokens"] == 1000
    assert rows[aid]["session_count"] == 1
    # Attributed to the owner.
    by_user = {u["user_id"]: u for u in rows[aid]["by_user"]}
    assert AUDIENCE_USER in by_user
    assert by_user[AUDIENCE_USER]["cost_usd"] == 4.25
    assert report["totals"]["total_cost_usd"] >= 4.25
