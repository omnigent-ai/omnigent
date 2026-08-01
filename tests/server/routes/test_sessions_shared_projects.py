"""Regression tests: sessions shared into a project stay on "Shared with me".

Projects are a "My sessions"-only sidebar surface — filing a session into a
project is owner-only. When someone shares a session that happens to carry a
project label, the recipient should see it under "Shared with me", NOT as one
of their own project folders under "My sessions".

The sidebar builds project folders from two owner-scoped server surfaces:

- ``GET /v1/sessions/projects`` — the folder *names*, and
- ``GET /v1/sessions?project=<name>`` — the sessions *inside* a folder.

Both must filter by ownership (an ``owner``-level grant), not mere access, or
a shared session leaks into the recipient's "My sessions" project view. These
tests drive the real routes against file-backed SQLite stores with header auth.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)

ALICE = "alice@example.com"
BOB = "bob@example.com"
PROJECT_LABEL_KEY = "omni_project"


def _multi_user_app(db_uri: str) -> FastAPI:
    """Build a header-auth app mounting the sessions router at ``/v1``."""
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            conversation_store=SqlAlchemyConversationStore(db_uri),
            agent_store=SqlAlchemyAgentStore(db_uri),
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=SqlAlchemyPermissionStore(db_uri),
        ),
        prefix="/v1",
    )
    return app


def _seed_shared_project_session(db_uri: str) -> str:
    """Seed a session owned by Bob, filed under a project, and read-shared to
    Alice. Returns the session id."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    perms = SqlAlchemyPermissionStore(db_uri)
    if agent_store.get("087b7cb7ac30abf4debfaa578d052ec6") is None:
        agent_store.create(
            agent_id="087b7cb7ac30abf4debfaa578d052ec6",
            name="test-agent",
            bundle_location="087b7cb7ac30abf4debfaa578d052ec6/bundle",
        )
    conv = conv_store.create_conversation(
        title="Bob's session", agent_id="087b7cb7ac30abf4debfaa578d052ec6"
    )
    conv_store.set_labels(conv.id, {PROJECT_LABEL_KEY: "Bob Project"})
    for user in (ALICE, BOB):
        perms.ensure_user(user)
    perms.grant(BOB, conv.id, LEVEL_OWNER)
    perms.grant(ALICE, conv.id, LEVEL_READ)
    return conv.id


def test_shared_project_not_listed_as_recipients_own_project(db_uri: str) -> None:
    """A project whose only member is a session shared TO Alice (owned by Bob)
    must not appear in Alice's project list — folders are her own sessions."""
    _seed_shared_project_session(db_uri)
    app = _multi_user_app(db_uri)

    # Bob owns it, so it's his project. Label-only (no first-class row here),
    # so it lists with id=None.
    bob = TestClient(app).get("/v1/sessions/projects", headers={"X-Forwarded-Email": BOB})
    assert bob.status_code == 200
    assert bob.json() == [{"id": None, "name": "Bob Project"}]

    # Alice can access the session, but doesn't own it — no folder for her.
    alice = TestClient(app).get("/v1/sessions/projects", headers={"X-Forwarded-Email": ALICE})
    assert alice.status_code == 200
    assert alice.json() == []


def test_shared_session_excluded_from_recipients_project_folder(db_uri: str) -> None:
    """Fetching a project folder's sessions (``?project=``) as Alice excludes a
    session merely shared with her — it belongs on "Shared with me"."""
    conv_id = _seed_shared_project_session(db_uri)
    app = _multi_user_app(db_uri)

    # Bob's folder holds the session.
    bob = TestClient(app).get(
        "/v1/sessions?project=Bob%20Project", headers={"X-Forwarded-Email": BOB}
    )
    assert bob.status_code == 200
    assert [s["id"] for s in bob.json()["data"]] == [conv_id]

    # Alice's same-named folder is empty — the shared session isn't hers to file.
    alice = TestClient(app).get(
        "/v1/sessions?project=Bob%20Project", headers={"X-Forwarded-Email": ALICE}
    )
    assert alice.status_code == 200
    assert alice.json()["data"] == []


def test_shared_session_still_visible_in_flat_list(db_uri: str) -> None:
    """The fix is scoped to project surfaces: the shared session still shows up
    in Alice's unfiltered session list, where the "Shared with me" tab reads it
    (the frontend splits owned vs. shared by permission_level there)."""
    conv_id = _seed_shared_project_session(db_uri)
    app = _multi_user_app(db_uri)

    resp = TestClient(app).get("/v1/sessions", headers={"X-Forwarded-Email": ALICE})
    assert resp.status_code == 200
    items = {s["id"]: s for s in resp.json()["data"]}
    assert conv_id in items
    # Below LEVEL_OWNER, so the frontend files it under "Shared with me".
    assert items[conv_id]["permission_level"] < LEVEL_OWNER
