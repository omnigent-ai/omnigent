"""Tests for filing sessions into first-class projects via the sessions API.

Exercises the two HTTP surfaces added on top of the projects Stage-1 store:

- ``PATCH /v1/sessions/{id}`` with ``project_id`` — move a session into a
  project (non-empty id), unfile it (``""``), leave unchanged (omitted). Filing
  is by project **id** (the client holds it after create/list).
- ``GET /v1/sessions?project=<name>`` — list a project's member sessions by
  **name**, dual-reading the first-class entity and the legacy ``omni_project``
  label; ``?project=`` (empty) lists unfiled sessions. The name→id resolution is
  server-side, so the client passes only the name it renders in the sidebar.

Both are owner-scoped because projects are owner-private: only the session
owner may file it, and only into a project they own. The multi-user tests use
header auth to prove the ownership boundary holds — a caller can neither file a
session into another owner's project nor into a session they only have
edit/read access to.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    UnifiedAuthProvider,
)
from omnigent.server.routes.projects import create_projects_router
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

ALICE = "alice@example.com"
BOB = "bob@example.com"
AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"


def _ensure_agent(db_uri: str) -> None:
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(
            agent_id=AGENT_ID,
            name="test-agent",
            bundle_location=f"{AGENT_ID}/bundle",
        )


# ── Single-user mode (no auth) ───────────────────────────────────────────


def _single_user_app(db_uri: str) -> FastAPI:
    """Build an app (no auth provider) mounting sessions + projects at ``/v1``."""
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    project_store = SqlAlchemyProjectStore(db_uri)
    app.include_router(
        create_sessions_router(
            conversation_store=SqlAlchemyConversationStore(db_uri),
            agent_store=SqlAlchemyAgentStore(db_uri),
            project_store=project_store,
        ),
        prefix="/v1",
    )
    app.include_router(create_projects_router(project_store=project_store), prefix="/v1")
    return app


def test_file_and_unfile_session_single_user(db_uri: str) -> None:
    """PATCH project_id files the session; project_id="" unfiles it."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))

    project = client.post("/v1/projects", json={"name": "Work"}).json()

    # File it (by id — the client holds the id after create).
    resp = client.patch(f"/v1/sessions/{conv.id}", json={"project_id": project["id"]})
    assert resp.status_code == 200
    assert resp.json()["project_id"] == project["id"]

    # Listing by project NAME returns it (dual-read resolves the name → id).
    listed = client.get("/v1/sessions?project=Work")
    assert [s["id"] for s in listed.json()["data"]] == [conv.id]

    # Unfile it.
    resp = client.patch(f"/v1/sessions/{conv.id}", json={"project_id": ""})
    assert resp.status_code == 200
    assert resp.json()["project_id"] is None

    # No longer in the project; now shows under unfiled (project="").
    assert client.get("/v1/sessions?project=Work").json()["data"] == []
    unfiled = client.get("/v1/sessions?project=")
    assert conv.id in [s["id"] for s in unfiled.json()["data"]]


def test_omitting_project_id_leaves_membership_unchanged(db_uri: str) -> None:
    """A PATCH that doesn't mention project_id must not clear the filing."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Work"}).json()
    client.patch(f"/v1/sessions/{conv.id}", json={"project_id": project["id"]})

    # A title-only PATCH leaves project membership intact.
    resp = client.patch(f"/v1/sessions/{conv.id}", json={"title": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["project_id"] == project["id"]


def test_file_into_nonexistent_project_404(db_uri: str) -> None:
    """Filing into a project id that doesn't exist is rejected (404)."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    resp = client.patch(
        f"/v1/sessions/{conv.id}", json={"project_id": "ffffffffffffffffffffffffffffffff"}
    )
    assert resp.status_code == 404


def test_explicit_null_project_id_is_rejected(db_uri: str) -> None:
    """A present-but-null project_id is invalid: only "" unfiles, and omitting
    the field leaves membership unchanged — so null must not silently unfile."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Work"}).json()
    client.patch(f"/v1/sessions/{conv.id}", json={"project_id": project["id"]})

    resp = client.patch(f"/v1/sessions/{conv.id}", json={"project_id": None})
    assert resp.status_code == 400
    # Membership is untouched — the invalid PATCH didn't unfile it.
    snap = client.get(f"/v1/sessions/{conv.id}")
    assert snap.json()["project_id"] == project["id"]


def test_unfile_unknown_session_404(db_uri: str) -> None:
    """Unfiling a session with no metadata row is a 404, mirroring the file
    path — a PATCH that changed nothing must not report success."""
    _ensure_agent(db_uri)
    client = TestClient(_single_user_app(db_uri))
    resp = client.patch("/v1/sessions/" + "f" * 32, json={"project_id": ""})
    assert resp.status_code == 404


def test_project_filter_excludes_other_projects(db_uri: str) -> None:
    """?project=<name> returns only that project's members, not another's."""
    _ensure_agent(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    a = conv_store.create_conversation(title="a", agent_id=AGENT_ID)
    b = conv_store.create_conversation(title="b", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    p1 = client.post("/v1/projects", json={"name": "P1"}).json()
    client.post("/v1/projects", json={"name": "P2"})
    client.patch(f"/v1/sessions/{a.id}", json={"project_id": p1["id"]})
    # File b under P2 by name via the legacy label, to prove the dual-read OR
    # branch surfaces label members under the same ?project= filter.
    conv_store.set_labels(b.id, {"omni_project": "P2"})

    assert [s["id"] for s in client.get("/v1/sessions?project=P1").json()["data"]] == [a.id]
    assert [s["id"] for s in client.get("/v1/sessions?project=P2").json()["data"]] == [b.id]


def test_project_filter_dual_reads_label_and_entity(db_uri: str) -> None:
    """Under one ?project=<name>, both a first-class member and a legacy
    label member surface together (the dual-read OR)."""
    _ensure_agent(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    entity_member = conv_store.create_conversation(title="entity", agent_id=AGENT_ID)
    label_member = conv_store.create_conversation(title="label", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Work"}).json()
    client.patch(f"/v1/sessions/{entity_member.id}", json={"project_id": project["id"]})
    conv_store.set_labels(label_member.id, {"omni_project": "Work"})

    listed = client.get("/v1/sessions?project=Work").json()["data"]
    assert {s["id"] for s in listed} == {entity_member.id, label_member.id}


# ── Multi-user mode (header auth) — ownership boundary ───────────────────


def _multi_user_app(db_uri: str) -> FastAPI:
    """Build a header-auth app mounting sessions + projects at ``/v1``."""
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    auth = UnifiedAuthProvider(source="header")
    project_store = SqlAlchemyProjectStore(db_uri)
    app.include_router(
        create_sessions_router(
            conversation_store=SqlAlchemyConversationStore(db_uri),
            agent_store=SqlAlchemyAgentStore(db_uri),
            auth_provider=auth,
            permission_store=SqlAlchemyPermissionStore(db_uri),
            project_store=project_store,
        ),
        prefix="/v1",
    )
    app.include_router(
        create_projects_router(project_store=project_store, auth_provider=auth),
        prefix="/v1",
    )
    return app


def _seed_owned_session(db_uri: str, owner: str, title: str = "s") -> str:
    """Create a session owned by ``owner`` (owner-level grant). Returns its id."""
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title=title, agent_id=AGENT_ID)
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(owner)
    perms.grant(owner, conv.id, LEVEL_OWNER)
    return conv.id


def _hdr(user: str) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


def test_owner_can_file_own_session_into_own_project(db_uri: str) -> None:
    """The happy path under header auth: Alice files her session into her project."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, ALICE)
    client = TestClient(_multi_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Alice"}, headers=_hdr(ALICE)).json()

    resp = client.patch(
        f"/v1/sessions/{conv_id}", json={"project_id": project["id"]}, headers=_hdr(ALICE)
    )
    assert resp.status_code == 200
    assert resp.json()["project_id"] == project["id"]
    listed = client.get("/v1/sessions?project=Alice", headers=_hdr(ALICE))
    assert [s["id"] for s in listed.json()["data"]] == [conv_id]


def test_cannot_file_into_another_owners_project(db_uri: str) -> None:
    """Alice owns the session but the target project is Bob's — rejected 404,
    and the session stays unfiled."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, ALICE)
    client = TestClient(_multi_user_app(db_uri))
    bob_project = client.post("/v1/projects", json={"name": "Bob"}, headers=_hdr(BOB)).json()

    resp = client.patch(
        f"/v1/sessions/{conv_id}",
        json={"project_id": bob_project["id"]},
        headers=_hdr(ALICE),
    )
    assert resp.status_code == 404
    # Still unfiled — the rejected filing had no side effect.
    snap = client.get(f"/v1/sessions/{conv_id}", headers=_hdr(ALICE))
    assert snap.json()["project_id"] is None


def test_editor_cannot_file_shared_session(db_uri: str) -> None:
    """Bob owns the session and shares EDIT with Alice; Alice may not file it
    into her own project — filing is owner-only."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, BOB)
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(ALICE)
    perms.grant(ALICE, conv_id, LEVEL_EDIT)
    client = TestClient(_multi_user_app(db_uri))
    alice_project = client.post("/v1/projects", json={"name": "Alice"}, headers=_hdr(ALICE)).json()

    resp = client.patch(
        f"/v1/sessions/{conv_id}",
        json={"project_id": alice_project["id"]},
        headers=_hdr(ALICE),
    )
    # Edit access is below owner, so the owner-only gate rejects with 403.
    assert resp.status_code == 403


def test_project_listing_is_owner_scoped(db_uri: str) -> None:
    """A session shared to Alice but filed by its owner Bob must not surface in
    Alice's ?project= listing, even when Alice has her own like-named project.
    The name→id resolution is owner-scoped, so "Shared" resolves to Alice's own
    (empty) project, never Bob's."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, BOB)
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(ALICE)
    perms.grant(ALICE, conv_id, LEVEL_EDIT)
    client = TestClient(_multi_user_app(db_uri))
    bob_project = client.post("/v1/projects", json={"name": "Shared"}, headers=_hdr(BOB)).json()
    # Alice owns a distinct project that happens to share the name.
    client.post("/v1/projects", json={"name": "Shared"}, headers=_hdr(ALICE))
    client.patch(
        f"/v1/sessions/{conv_id}",
        json={"project_id": bob_project["id"]},
        headers=_hdr(BOB),
    )

    # Bob sees his member session under his "Shared".
    bob_list = client.get("/v1/sessions?project=Shared", headers=_hdr(BOB))
    assert [s["id"] for s in bob_list.json()["data"]] == [conv_id]

    # Alice, asking for "Shared", resolves to HER project — the shared session
    # (Bob's) does not surface: the listing is owner-scoped.
    alice_list = client.get("/v1/sessions?project=Shared", headers=_hdr(ALICE))
    assert alice_list.json()["data"] == []
