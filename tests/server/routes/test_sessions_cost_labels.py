"""Cross-user tests for the advisor-owned ``cost_control.*`` label namespace.

The cost advisor writes the ``cost_control.plan`` conversation label (its
per-turn brain-model verdict, surfaced in the UI), so client-supplied
label writes must not be able to set it: an editor who could PATCH the
verdict could spoof the cost telemetry for a session. These tests drive
the real ``PATCH /v1/sessions/{id}`` and JSON ``POST /v1/sessions``
routes against file-backed SQLite stores with header auth and assert:

- neither an editor (Bob) nor the owner (Alice) can write
  ``cost_control.*`` labels from an ordinary client;
- the session's bound runner CAN, by proving itself with its tunnel
  binding token (token-bound runner id or server allow-list);
- single-user servers skip the gate (local runners may register under
  stable, non-token-bound ids);
- session creation rejects ``cost_control.*`` seeds outright.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.runner.identity import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)

ALICE = "alice@example.com"
BOB = "bob@example.com"

# Label key used as a concrete test target for the cost_control.* namespace
# guard. The full parse/serialize logic was removed with the cost_advisor;
# the namespace protection in sessions.py still applies to this key.
COST_CONTROL_PLAN_LABEL = "cost_control.plan"

# The binding token the test runner presents; its token-bound id is what
# the session's runner_id must equal for the write to be authorized.
_RUNNER_TOKEN = "test-binding-token-abc123"

# A forged v3 verdict body — content is irrelevant to the gate (it rejects
# on the key, before any parsing), but keep it realistic.
_FORGED_PLAN = (
    '{"version":3,"tier":"expensive","model":"databricks-claude-opus-4-8",'
    '"applied":true,"rationale":"forged","turn_anchor":"2026-06-10T00:00:00+00:00"}'
)


@pytest.fixture
def stores(
    db_uri: str,
) -> tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore]:
    """Real file-backed stores backing the routes under test.

    :param db_uri: Per-test SQLite URI from the root conftest.
    :returns: ``(conversation_store, agent_store, permission_store)``.
    """
    return (
        SqlAlchemyConversationStore(db_uri),
        SqlAlchemyAgentStore(db_uri),
        SqlAlchemyPermissionStore(db_uri),
    )


def _install_error_handler(app: FastAPI) -> None:
    """Mirror ``create_app()``'s OmnigentError → HTTP translation.

    :param app: The bare test app mounting only the sessions router.
    """

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        """Translate OmnigentError to its HTTP status."""
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )


def _multi_user_app(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
    *,
    runner_tunnel_tokens: frozenset[str] | None = None,
    artifact_store: LocalArtifactStore | None = None,
) -> FastAPI:
    """Build a multi-user app (header auth + real permission store).

    :param stores: The shared store fixture.
    :param runner_tunnel_tokens: Optional server tunnel-token
        allow-list, e.g. ``frozenset({"pool-token"})``.
    :param artifact_store: Optional artifact store, required only by
        the multipart bundled-create test.
    :returns: A FastAPI app mounting the sessions router at ``/v1``.
    """
    conversation_store, agent_store, permission_store = stores
    app = FastAPI()
    _install_error_handler(app)
    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
            artifact_store=artifact_store,
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=permission_store,
            runner_tunnel_tokens=runner_tunnel_tokens,
        ),
        prefix="/v1",
    )
    return app


def _single_user_app(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> FastAPI:
    """Build a single-user app (no auth provider, no permission store).

    :param stores: The shared store fixture.
    :returns: A FastAPI app mounting the sessions router at ``/v1``.
    """
    conversation_store, agent_store, _ = stores
    app = FastAPI()
    _install_error_handler(app)
    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
        ),
        prefix="/v1",
    )
    return app


def _seed_session(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
    *,
    owner: str | None = ALICE,
    editor: str | None = None,
    runner_id: str | None = None,
) -> str:
    """Create a session-shaped conversation with optional grants/runner.

    :param stores: The shared store fixture.
    :param owner: User granted ``LEVEL_OWNER``, or ``None`` to skip
        grants entirely (single-user app).
    :param editor: Optional user granted ``LEVEL_EDIT``,
        e.g. ``"bob@example.com"``.
    :param runner_id: Optional runner id to bind, e.g. the
        token-bound id of :data:`_RUNNER_TOKEN`.
    :returns: The new session/conversation id.
    """
    conversation_store, agent_store, permission_store = stores
    if agent_store.get("ag_test") is None:
        agent_store.create(
            agent_id="ag_test",
            name="test-agent",
            bundle_location="ag_test/bundle",
        )
    conv = conversation_store.create_conversation(title="advised session", agent_id="ag_test")
    if owner is not None:
        permission_store.ensure_user(owner)
        permission_store.grant(owner, conv.id, LEVEL_OWNER)
    if editor is not None:
        permission_store.ensure_user(editor)
        permission_store.grant(editor, conv.id, LEVEL_EDIT)
    if runner_id is not None:
        conversation_store.replace_runner_id(conv.id, runner_id)
    return conv.id


# ── PATCH: ordinary clients are rejected ─────────────────────────────────────


def test_editor_cannot_patch_cost_control_plan(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> None:
    """Bob (edit access, no runner token) cannot overwrite the plan
    label — the exact attack from the swarm finding: an editor forging
    ``cost_control.plan`` to steer later enforcement/telemetry."""
    conversation_store = stores[0]
    app = _multi_user_app(stores)
    conv_id = _seed_session(stores, editor=BOB)

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"labels": {COST_CONTROL_PLAN_LABEL: _FORGED_PLAN}},
        headers={"X-Forwarded-Email": BOB},
    )
    # 403, not 200-with-drop: the reserved write fails loud.
    assert resp.status_code == 403
    assert "cost_control" in resp.json()["error"]["message"]
    # The forged verdict never reached the store, so readers still see no
    # verdict label.
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    assert COST_CONTROL_PLAN_LABEL not in conv.labels


def test_owner_without_runner_proof_cannot_patch_cost_control_plan(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> None:
    """Even the session OWNER cannot write the namespace from an
    ordinary client: the plan is policy state owned by the runner-side
    advisor, not user preference — an owner forging a permissive plan
    would spoof their cost telemetry."""
    conversation_store = stores[0]
    app = _multi_user_app(stores)
    conv_id = _seed_session(stores)

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"labels": {COST_CONTROL_PLAN_LABEL: _FORGED_PLAN}},
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 403
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    assert COST_CONTROL_PLAN_LABEL not in conv.labels


def test_rejected_reserved_write_leaves_other_fields_untouched(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> None:
    """The gate runs BEFORE any store mutation: a mixed PATCH (title +
    reserved label) must not half-apply — a changed title alongside a
    403 would mean the route mutated state before authorizing."""
    conversation_store = stores[0]
    app = _multi_user_app(stores)
    conv_id = _seed_session(stores)

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={
            "title": "smuggled rename",
            "labels": {COST_CONTROL_PLAN_LABEL: _FORGED_PLAN},
        },
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 403
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    # Title kept its seeded value — nothing was applied pre-rejection.
    assert conv.title == "advised session"
    assert COST_CONTROL_PLAN_LABEL not in conv.labels


def test_wrong_runner_token_is_rejected(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> None:
    """A token bound to a DIFFERENT runner than the session's must not
    authorize the write — otherwise any runner-holding user could forge
    plans on someone else's sessions."""
    conversation_store = stores[0]
    app = _multi_user_app(stores)
    conv_id = _seed_session(stores, runner_id=token_bound_runner_id(_RUNNER_TOKEN))

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"labels": {COST_CONTROL_PLAN_LABEL: _FORGED_PLAN}},
        headers={
            "X-Forwarded-Email": ALICE,
            RUNNER_TUNNEL_TOKEN_HEADER: "some-other-runners-token",
        },
    )
    assert resp.status_code == 403
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    assert COST_CONTROL_PLAN_LABEL not in conv.labels


def test_editor_can_still_patch_ordinary_labels(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> None:
    """The gate is namespace-scoped: an editor's write of ordinary
    labels still succeeds — over-blocking would regress every existing
    labels client."""
    conversation_store = stores[0]
    app = _multi_user_app(stores)
    conv_id = _seed_session(stores, editor=BOB)

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"labels": {"team": "ml"}},
        headers={"X-Forwarded-Email": BOB},
    )
    assert resp.status_code == 200
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    assert conv.labels["team"] == "ml"


# ── PATCH: the bound runner's write keeps working ────────────────────────────


def test_bound_runner_token_authorizes_plan_write(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> None:
    """The advisor's own persist path: a PATCH carrying the binding
    token whose token-bound id IS the session's runner id succeeds and
    lands the verdict in the store, where readers parse it back. If this
    403'd, the naive-deny trap would be sprung — the feature breaks on
    every multi-user server."""
    conversation_store = stores[0]
    app = _multi_user_app(stores)
    conv_id = _seed_session(stores, runner_id=token_bound_runner_id(_RUNNER_TOKEN))

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"labels": {COST_CONTROL_PLAN_LABEL: _FORGED_PLAN}},
        headers={
            # The runner authenticates as the session's user (Alice) —
            # the token is what proves it's the runner, not the email.
            "X-Forwarded-Email": ALICE,
            RUNNER_TUNNEL_TOKEN_HEADER: _RUNNER_TOKEN,
        },
    )
    assert resp.status_code == 200
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    # The write landed in the store.
    assert conv.labels.get(COST_CONTROL_PLAN_LABEL) == _FORGED_PLAN


def test_allowlisted_pool_token_authorizes_plan_write(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> None:
    """Managed runner pools register under STABLE runner ids, so their
    proof is allow-list membership (the same trust model as the tunnel
    route), not a token-bound id match."""
    conversation_store = stores[0]
    app = _multi_user_app(stores, runner_tunnel_tokens=frozenset({"pool-token"}))
    # Stable id: deliberately NOT derived from any token.
    conv_id = _seed_session(stores, runner_id="runner_stable_pool_1")

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"labels": {COST_CONTROL_PLAN_LABEL: _FORGED_PLAN}},
        headers={
            "X-Forwarded-Email": ALICE,
            RUNNER_TUNNEL_TOKEN_HEADER: "pool-token",
        },
    )
    assert resp.status_code == 200
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    assert COST_CONTROL_PLAN_LABEL in conv.labels


def test_single_user_server_skips_the_gate(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> None:
    """No permission store = single-user mode: the advisor's persist
    must work without any token (local runners may register under a
    stable id unrelated to their token), and there is no second user
    to forge against."""
    conversation_store = stores[0]
    app = _single_user_app(stores)
    conv_id = _seed_session(stores, owner=None)

    resp = TestClient(app).patch(
        f"/v1/sessions/{conv_id}",
        json={"labels": {COST_CONTROL_PLAN_LABEL: _FORGED_PLAN}},
    )
    assert resp.status_code == 200
    conv = conversation_store.get_conversation(conv_id)
    assert conv is not None
    assert COST_CONTROL_PLAN_LABEL in conv.labels


# ── Create: no client may seed the namespace ─────────────────────────────────


def test_create_session_rejects_cost_control_label_seed(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> None:
    """``POST /v1/sessions`` with a ``cost_control.*`` label seed fails
    400: no runner can be bound at create time, so there is no
    legitimate writer — a seeded forged verdict would spoof the cost telemetry
    from turn one."""
    _seed_session(stores)  # ensures ag_test exists
    app = _multi_user_app(stores)

    resp = TestClient(app).post(
        "/v1/sessions",
        json={
            "agent_id": "ag_test",
            "labels": {COST_CONTROL_PLAN_LABEL: _FORGED_PLAN},
        },
        # Sentinel Origin: first-party client past the require_trusted_origin guard.
        headers={"X-Forwarded-Email": ALICE, "Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    assert resp.status_code == 400
    assert "cost_control" in resp.json()["error"]["message"]


def test_bundled_create_rejects_cost_control_label_seed(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
    tmp_path: Path,
) -> None:
    """The multipart bundled-create shape is gated too: its metadata
    carries the same client-supplied ``labels`` and persists them via
    ``create_session_with_agent`` — an ungated second door for the same
    forgery."""
    import json as _json

    from tests.server.helpers import build_agent_bundle

    app = _multi_user_app(stores, artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")))
    bundle = build_agent_bundle(name="test-agent")
    resp = TestClient(app).post(
        "/v1/sessions",
        data={
            "metadata": _json.dumps({"labels": {COST_CONTROL_PLAN_LABEL: _FORGED_PLAN}}),
        },
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        # Sentinel Origin: first-party client past the require_trusted_origin guard.
        headers={"X-Forwarded-Email": ALICE, "Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    assert resp.status_code == 400
    assert "cost_control" in resp.json()["error"]["message"]


def test_create_session_with_ordinary_labels_succeeds(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyAgentStore, SqlAlchemyPermissionStore],
) -> None:
    """Counterpart of the rejection above: ordinary label seeds still
    work, proving the create gate is namespace-scoped too."""
    conversation_store = stores[0]
    _seed_session(stores)  # ensures ag_test exists
    app = _multi_user_app(stores)

    resp = TestClient(app).post(
        "/v1/sessions",
        json={"agent_id": "ag_test", "labels": {"team": "ml"}},
        # Sentinel Origin: first-party client past the require_trusted_origin guard.
        headers={"X-Forwarded-Email": ALICE, "Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    assert resp.status_code == 201
    conv = conversation_store.get_conversation(resp.json()["id"])
    assert conv is not None
    assert conv.labels["team"] == "ml"
