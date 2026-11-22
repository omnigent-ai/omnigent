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

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    UnifiedAuthProvider,
)
from omnigent.server.routes.projects import create_projects_router
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.server.sharing_settings import (
    PublicSharingMaxLevel,
    PublicSharingPolicyMiddleware,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
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

    # Archived folders resolve first-class membership and exclude live sessions.
    archived = client.patch(f"/v1/sessions/{conv.id}", json={"archived": True})
    assert archived.status_code == 200
    archived_list = client.get("/v1/sessions?project=Work&visibility=archived")
    assert archived_list.status_code == 200
    assert [row["id"] for row in archived_list.json()["data"]] == [conv.id]
    assert archived_list.json()["data"][0]["project_id"] == project["id"]
    assert client.get("/v1/sessions?project=Work&visibility=mine").json()["data"] == []
    assert client.patch(f"/v1/sessions/{conv.id}", json={"archived": False}).status_code == 200

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


def test_patch_response_omits_items(db_uri: str) -> None:
    """PATCH returns a slim snapshot: items stay empty even when the session has some."""
    _ensure_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="s", agent_id=AGENT_ID)
    store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            )
        ],
    )
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Work"}).json()

    resp = client.patch(f"/v1/sessions/{conv.id}", json={"project_id": project["id"]})
    assert resp.status_code == 200
    assert resp.json()["project_id"] == project["id"]
    # PATCH callers use only scalar fields; transcripts come from the items
    # endpoint, so the mutation response skips the expensive items read.
    assert resp.json()["items"] == []

    # The slimming is PATCH-specific — the GET snapshot still carries items.
    assert len(client.get(f"/v1/sessions/{conv.id}").json()["items"]) == 1


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


def test_fork_inherits_source_project(db_uri: str) -> None:
    """A fork of a filed session lands in the same project as its source."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))
    project = client.post("/v1/projects", json={"name": "Work"}).json()
    client.patch(f"/v1/sessions/{conv.id}", json={"project_id": project["id"]})

    resp = client.post(f"/v1/sessions/{conv.id}/fork", json={})
    assert resp.status_code == 201
    fork = resp.json()
    assert fork["project_id"] == project["id"]

    # The project folder lists both the source and the fork.
    listed = client.get("/v1/sessions?project=Work")
    assert {s["id"] for s in listed.json()["data"]} == {conv.id, fork["id"]}


def test_fork_of_unfiled_session_stays_unfiled(db_uri: str) -> None:
    """Forking a session outside any project must not invent a filing."""
    _ensure_agent(db_uri)
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(title="s", agent_id=AGENT_ID)
    client = TestClient(_single_user_app(db_uri))

    resp = client.post(f"/v1/sessions/{conv.id}/fork", json={})
    assert resp.status_code == 201
    assert resp.json()["project_id"] is None


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


def _multi_user_app(
    db_uri: str,
    *,
    artifact_root: Path | None = None,
    project_store: SqlAlchemyProjectStore | None = None,
    file_store: SqlAlchemyFileStore | None = None,
    public_sharing_max_level: PublicSharingMaxLevel = PublicSharingMaxLevel.READ,
) -> FastAPI:
    """Build a header-auth app mounting sessions + projects at ``/v1``.

    :param db_uri: The test database.
    :param artifact_root: Directory for file bytes; ``None`` mounts no file stores.
    :param project_store: The project store to mount, e.g. one that injects a fault.
    :param file_store: The file store to mount with ``artifact_root``, e.g. one
        that injects a fault.
    :param public_sharing_max_level: The server's ceiling on public links, e.g.
        ``PublicSharingMaxLevel.EDIT`` for a server that allows edit links.
    """
    app = FastAPI()
    app.add_middleware(PublicSharingPolicyMiddleware, max_level=lambda: public_sharing_max_level)

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    auth = UnifiedAuthProvider(source="header")
    project_store = project_store or SqlAlchemyProjectStore(db_uri)
    file_stores = (
        {
            "file_store": file_store or SqlAlchemyFileStore(db_uri),
            "artifact_store": LocalArtifactStore(str(artifact_root)),
        }
        if artifact_root is not None
        else {}
    )
    app.include_router(
        create_sessions_router(
            conversation_store=SqlAlchemyConversationStore(db_uri),
            agent_store=SqlAlchemyAgentStore(db_uri),
            auth_provider=auth,
            permission_store=SqlAlchemyPermissionStore(db_uri),
            project_store=project_store,
            **file_stores,
        ),
        prefix="/v1",
    )
    app.include_router(
        create_projects_router(project_store=project_store, auth_provider=auth, **file_stores),
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


def test_fork_of_shared_session_in_foreign_project_stays_unfiled(db_uri: str) -> None:
    """Alice forks Bob's shared session filed in Bob's project. Projects are
    owner-private, so her fork must not carry Bob's project id — a foreign id
    would surface in no folder view of hers."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, BOB)
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(ALICE)
    perms.grant(ALICE, conv_id, LEVEL_EDIT)
    client = TestClient(_multi_user_app(db_uri))
    bob_project = client.post("/v1/projects", json={"name": "Bob"}, headers=_hdr(BOB)).json()
    filed = client.patch(
        f"/v1/sessions/{conv_id}",
        json={"project_id": bob_project["id"]},
        headers=_hdr(BOB),
    )
    assert filed.status_code == 200

    resp = client.post(f"/v1/sessions/{conv_id}/fork", json={}, headers=_hdr(ALICE))
    assert resp.status_code == 201
    assert resp.json()["project_id"] is None


def test_fork_keeps_own_project_multi_user(db_uri: str) -> None:
    """Under header auth, Bob's fork of his own filed session stays in his
    project (the ownership check resolves against the forker's id)."""
    _ensure_agent(db_uri)
    conv_id = _seed_owned_session(db_uri, BOB)
    client = TestClient(_multi_user_app(db_uri))
    bob_project = client.post("/v1/projects", json={"name": "Bob"}, headers=_hdr(BOB)).json()
    client.patch(
        f"/v1/sessions/{conv_id}",
        json={"project_id": bob_project["id"]},
        headers=_hdr(BOB),
    )

    resp = client.post(f"/v1/sessions/{conv_id}/fork", json={}, headers=_hdr(BOB))
    assert resp.status_code == 201
    assert resp.json()["project_id"] == bob_project["id"]


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


# ── Project files: shared by the sessions filed in a project ────────────


def _filed_session(db_uri: str, client: TestClient, owner: str, project_id: str) -> str:
    """Create a session owned by ``owner`` and file it into ``project_id``."""
    conv_id = _seed_owned_session(db_uri, owner)
    filed = client.patch(
        f"/v1/sessions/{conv_id}", json={"project_id": project_id}, headers=_hdr(owner)
    )
    assert filed.status_code == 200, filed.text
    return conv_id


def _upload_to_project(client: TestClient, session_id: str, user: str, name: str) -> str:
    """Upload a file from ``session_id``'s agent to its project; return the file id."""
    resp = client.post(
        f"/v1/sessions/{session_id}/resources/project-files",
        files={"file": (name, f"contents of {name}".encode(), "text/plain")},
        headers=_hdr(user),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _project_file_names(client: TestClient, session_id: str, user: str) -> list[str]:
    resp = client.get(f"/v1/sessions/{session_id}/resources/project-files", headers=_hdr(user))
    assert resp.status_code == 200, resp.text
    return [entry["name"] for entry in resp.json()["data"]]


def test_sessions_in_a_project_share_its_files(db_uri: str, tmp_path: Path) -> None:
    """One session's agent stores a project file; another session and the owner read it."""
    _ensure_agent(db_uri)
    client = TestClient(_multi_user_app(db_uri, artifact_root=tmp_path / "artifacts"))
    project = client.post("/v1/projects", json={"name": "Shared"}, headers=_hdr(ALICE)).json()
    writer = _filed_session(db_uri, client, ALICE, project["id"])
    reader = _filed_session(db_uri, client, ALICE, project["id"])

    file_id = _upload_to_project(client, writer, ALICE, "plan.md")

    assert _project_file_names(client, reader, ALICE) == ["plan.md"]
    content = client.get(
        f"/v1/sessions/{reader}/resources/project-files/{file_id}/content", headers=_hdr(ALICE)
    )
    assert content.content == b"contents of plan.md"
    owner_list = client.get(f"/v1/projects/{project['id']}/resources/files", headers=_hdr(ALICE))
    assert [entry["id"] for entry in owner_list.json()["data"]] == [file_id]
    # A project file is not one of the writing session's own files.
    session_files = client.get(f"/v1/sessions/{writer}/resources/files", headers=_hdr(ALICE))
    assert session_files.json()["data"] == []


def test_project_files_stay_inside_their_project(db_uri: str, tmp_path: Path) -> None:
    """Sessions outside the project, and other users, can't reach its files."""
    _ensure_agent(db_uri)
    client = TestClient(_multi_user_app(db_uri, artifact_root=tmp_path / "artifacts"))
    project = client.post("/v1/projects", json={"name": "P"}, headers=_hdr(ALICE)).json()
    other = client.post("/v1/projects", json={"name": "Q"}, headers=_hdr(ALICE)).json()
    member = _filed_session(db_uri, client, ALICE, project["id"])
    file_id = _upload_to_project(client, member, ALICE, "plan.md")
    elsewhere = _filed_session(db_uri, client, ALICE, other["id"])
    unfiled = _seed_owned_session(db_uri, ALICE)

    assert _project_file_names(client, elsewhere, ALICE) == []
    foreign = client.get(
        f"/v1/sessions/{elsewhere}/resources/project-files/{file_id}/content", headers=_hdr(ALICE)
    )
    assert foreign.status_code == 404
    not_filed = client.get(f"/v1/sessions/{unfiled}/resources/project-files", headers=_hdr(ALICE))
    assert not_filed.status_code == 404
    assert "not in a project" in not_filed.text
    other_user = client.get(f"/v1/projects/{project['id']}/resources/files", headers=_hdr(BOB))
    assert other_user.status_code == 404

    unfile = client.patch(f"/v1/sessions/{member}", json={"project_id": ""}, headers=_hdr(ALICE))
    assert unfile.status_code == 200
    moved_out = client.get(f"/v1/sessions/{member}/resources/project-files", headers=_hdr(ALICE))
    assert moved_out.status_code == 404


@pytest.mark.parametrize(("level", "upload_status"), [(LEVEL_EDIT, 201), (LEVEL_READ, 403)])
def test_a_collaborator_reaches_the_project_through_a_shared_session(
    db_uri: str, tmp_path: Path, level: int, upload_status: int
) -> None:
    """A collaborator on a project session reads its project's files, and writes with edit."""
    _ensure_agent(db_uri)
    client = TestClient(_multi_user_app(db_uri, artifact_root=tmp_path / "artifacts"))
    project = client.post("/v1/projects", json={"name": "P"}, headers=_hdr(ALICE)).json()
    shared = _filed_session(db_uri, client, ALICE, project["id"])
    _upload_to_project(client, shared, ALICE, "plan.md")
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(BOB)
    perms.grant(BOB, shared, level)

    assert _project_file_names(client, shared, BOB) == ["plan.md"]
    upload = client.post(
        f"/v1/sessions/{shared}/resources/project-files",
        files={"file": ("notes.md", b"bob's notes", "text/plain")},
        headers=_hdr(BOB),
    )
    assert upload.status_code == upload_status, upload.text


@pytest.mark.parametrize(
    ("link_level", "owner_status"),
    [(LEVEL_READ, 200), (LEVEL_EDIT, 403)],
    ids=["read-link", "edit-link"],
)
def test_a_session_shared_by_link_keeps_its_project_files_private(
    db_uri: str, tmp_path: Path, link_level: int, owner_status: int
) -> None:
    """
    A link to one project session never reaches the project's files.

    Someone with only the link can open the session but not the files. An edit link
    also lets anyone drive the session's agent, whose tool calls reach the server as
    the owner, so that session can't use the files even for the owner. A session
    shared with an editor by name still can.
    """
    _ensure_agent(db_uri)
    client = TestClient(
        _multi_user_app(
            db_uri,
            artifact_root=tmp_path / "artifacts",
            public_sharing_max_level=PublicSharingMaxLevel.EDIT,
        )
    )
    project = client.post("/v1/projects", json={"name": "P"}, headers=_hdr(ALICE)).json()
    linked = _filed_session(db_uri, client, ALICE, project["id"])
    named = _filed_session(db_uri, client, ALICE, project["id"])
    file_id = _upload_to_project(client, named, ALICE, "plan.md")
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.grant(RESERVED_USER_PUBLIC, linked, link_level)
    perms.ensure_user(BOB)
    perms.grant(BOB, named, LEVEL_EDIT)

    session = client.get(f"/v1/sessions/{linked}", headers=_hdr(BOB))
    by_link = client.get(f"/v1/sessions/{linked}/resources/project-files", headers=_hdr(BOB))
    content = client.get(
        f"/v1/sessions/{linked}/resources/project-files/{file_id}/content", headers=_hdr(BOB)
    )
    # What the runner sends for the linked session's agent, whoever started the turn.
    as_owner = client.get(f"/v1/sessions/{linked}/resources/project-files", headers=_hdr(ALICE))

    assert session.status_code == 200, session.text
    assert by_link.status_code == 403, by_link.text
    assert content.status_code == 403, content.text
    assert as_owner.status_code == owner_status, as_owner.text
    assert _project_file_names(client, named, ALICE) == ["plan.md"]
    assert _project_file_names(client, named, BOB) == ["plan.md"]


def test_a_sub_agent_uses_its_root_sessions_project(db_uri: str, tmp_path: Path) -> None:
    """A sub-agent reaches its root's project, even when filed elsewhere itself."""
    _ensure_agent(db_uri)
    client = TestClient(_multi_user_app(db_uri, artifact_root=tmp_path / "artifacts"))
    project = client.post("/v1/projects", json={"name": "P"}, headers=_hdr(ALICE)).json()
    other = client.post("/v1/projects", json={"name": "Q"}, headers=_hdr(ALICE)).json()
    root = _filed_session(db_uri, client, ALICE, project["id"])
    _upload_to_project(client, root, ALICE, "plan.md")
    conversations = SqlAlchemyConversationStore(db_uri)
    child = conversations.create_conversation(
        title="worker", agent_id=AGENT_ID, parent_conversation_id=root
    )
    conversations.set_conversation_project(child.id, other["id"])

    assert _project_file_names(client, child.id, ALICE) == ["plan.md"]
    unfile = client.patch(f"/v1/sessions/{root}", json={"project_id": ""}, headers=_hdr(ALICE))
    assert unfile.status_code == 200, unfile.text
    orphaned = client.get(f"/v1/sessions/{child.id}/resources/project-files", headers=_hdr(ALICE))
    assert orphaned.status_code == 404


def test_deleting_a_project_deletes_its_files(db_uri: str, tmp_path: Path) -> None:
    """The project's rows and bytes go with it; its sessions then reach no files."""
    _ensure_agent(db_uri)
    artifacts = tmp_path / "artifacts"
    client = TestClient(_multi_user_app(db_uri, artifact_root=artifacts))
    project = client.post("/v1/projects", json={"name": "P"}, headers=_hdr(ALICE)).json()
    kept = client.post("/v1/projects", json={"name": "Kept"}, headers=_hdr(ALICE)).json()
    member = _filed_session(db_uri, client, ALICE, project["id"])
    deleted_ids = {_upload_to_project(client, member, ALICE, name) for name in ("a.md", "b.md")}
    kept_id = _upload_to_project(
        client, _filed_session(db_uri, client, ALICE, kept["id"]), ALICE, "k.md"
    )

    resp = client.delete(f"/v1/projects/{project['id']}", headers=_hdr(ALICE))

    assert resp.status_code == 200, resp.text
    file_store = SqlAlchemyFileStore(db_uri)
    assert all(file_store.get(file_id) is None for file_id in deleted_ids)
    assert {path.name for path in artifacts.iterdir()} == {kept_id}
    gone = client.get(f"/v1/sessions/{member}/resources/project-files", headers=_hdr(ALICE))
    assert gone.status_code == 404


def test_an_owner_deletes_a_project_file(db_uri: str, tmp_path: Path) -> None:
    _ensure_agent(db_uri)
    artifacts = tmp_path / "artifacts"
    client = TestClient(_multi_user_app(db_uri, artifact_root=artifacts))
    project = client.post("/v1/projects", json={"name": "P"}, headers=_hdr(ALICE)).json()
    upload = client.post(
        f"/v1/projects/{project['id']}/resources/files",
        files={"file": ("plan.md", b"plan", "text/plain")},
        headers=_hdr(ALICE),
    )
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["id"]

    resp = client.delete(
        f"/v1/projects/{project['id']}/resources/files/{file_id}", headers=_hdr(ALICE)
    )

    assert resp.status_code == 200, resp.text
    assert SqlAlchemyFileStore(db_uri).get(file_id) is None
    assert list(artifacts.iterdir()) == []


class _ProjectDeletedAfterCheck(SqlAlchemyProjectStore):
    """A project store whose project is deleted right after the upload's access check."""

    def __init__(self, db_uri: str) -> None:
        super().__init__(db_uri)
        self.armed = False

    def get(self, project_id: str, *, user_id: str | None):  # type: ignore[no-untyped-def]
        found = super().get(project_id, user_id=user_id)
        if self.armed and found is not None:
            self.armed = False
            # A concurrent project delete lands between the check and the upload's store.
            self.delete(project_id, user_id=user_id)
        return found


def test_an_upload_racing_a_project_delete_leaves_no_file(db_uri: str, tmp_path: Path) -> None:
    """An upload that passed its check before the project was deleted removes itself."""
    _ensure_agent(db_uri)
    artifacts = tmp_path / "artifacts"
    projects = _ProjectDeletedAfterCheck(db_uri)
    client = TestClient(_multi_user_app(db_uri, artifact_root=artifacts, project_store=projects))
    project = client.post("/v1/projects", json={"name": "P"}, headers=_hdr(ALICE)).json()
    projects.armed = True

    resp = client.post(
        f"/v1/projects/{project['id']}/resources/files",
        files={"file": ("plan.md", b"plan", "text/plain")},
        headers=_hdr(ALICE),
    )

    assert resp.status_code == 404, resp.text
    assert SqlAlchemyFileStore(db_uri).list_for_project(project["id"]).data == []
    assert list(artifacts.iterdir()) == []


class _SweepFailsOnce(SqlAlchemyFileStore):
    """
    A file store whose first project sweep commits one batch, then fails.

    Like a database connection lost between two batches: the first file's row
    is gone and its blob key reported, and the rest of the project's files stay.
    """

    def __init__(self, db_uri: str) -> None:
        super().__init__(db_uri)
        self.failures_left = 1

    def delete_all_for_project(
        self, project_id: str, on_orphaned: Callable[[list[str]], None]
    ) -> None:
        if self.failures_left:
            self.failures_left -= 1
            first = self.list_for_project(project_id, limit=1, order="asc").data[0]
            self.delete(first.id, project_id=project_id)
            on_orphaned([first.id])
            raise RuntimeError("database unavailable")
        super().delete_all_for_project(project_id, on_orphaned)


def test_a_failed_file_sweep_keeps_the_project_for_a_retry(db_uri: str, tmp_path: Path) -> None:
    """
    The files go before the project, so a delete that fails between batches can be
    retried, and the file the committed batch removed still loses its bytes.
    """
    _ensure_agent(db_uri)
    artifacts = tmp_path / "artifacts"
    app = _multi_user_app(db_uri, artifact_root=artifacts, file_store=_SweepFailsOnce(db_uri))
    client = TestClient(app, raise_server_exceptions=False)
    project = client.post("/v1/projects", json={"name": "P"}, headers=_hdr(ALICE)).json()
    member = _filed_session(db_uri, client, ALICE, project["id"])
    uploaded = {
        _upload_to_project(client, member, ALICE, "plan.md"),
        _upload_to_project(client, member, ALICE, "notes.md"),
    }

    failed = client.delete(f"/v1/projects/{project['id']}", headers=_hdr(ALICE))
    remaining = {f.id for f in SqlAlchemyFileStore(db_uri).list_for_project(project["id"]).data}

    assert failed.status_code == 500
    assert client.get(f"/v1/projects/{project['id']}", headers=_hdr(ALICE)).status_code == 200
    assert len(remaining) == 1 and remaining < uploaded
    assert {path.name for path in artifacts.iterdir()} == remaining

    retried = client.delete(f"/v1/projects/{project['id']}", headers=_hdr(ALICE))
    assert retried.status_code == 200, retried.text
    assert SqlAlchemyFileStore(db_uri).list_for_project(project["id"]).data == []
    assert list(artifacts.iterdir()) == []


class _SweepAfterTheProjectFailsOnce(SqlAlchemyFileStore):
    """
    A file store whose sweep after the project row fails once.

    An upload that raced the delete committed its row just before that sweep.
    """

    def __init__(self, db_uri: str) -> None:
        super().__init__(db_uri)
        self.sweeps = 0

    def delete_all_for_project(
        self, project_id: str, on_orphaned: Callable[[list[str]], None]
    ) -> None:
        self.sweeps += 1
        if self.sweeps == 2:
            self.create("late.md", 4, project_id=project_id)
            raise RuntimeError("database unavailable")
        super().delete_all_for_project(project_id, on_orphaned)


def test_deleting_a_gone_project_again_clears_what_a_failed_delete_left(
    db_uri: str, tmp_path: Path
) -> None:
    """
    When the sweep after the project row fails, deleting the project again clears the
    files it left; another user's delete never touches a project that still exists.
    """
    _ensure_agent(db_uri)
    artifacts = tmp_path / "artifacts"
    app = _multi_user_app(
        db_uri, artifact_root=artifacts, file_store=_SweepAfterTheProjectFailsOnce(db_uri)
    )
    client = TestClient(app, raise_server_exceptions=False)
    project = client.post("/v1/projects", json={"name": "P"}, headers=_hdr(ALICE)).json()
    member = _filed_session(db_uri, client, ALICE, project["id"])
    _upload_to_project(client, member, ALICE, "plan.md")
    files = SqlAlchemyFileStore(db_uri)

    outsider = client.delete(f"/v1/projects/{project['id']}", headers=_hdr(BOB))
    assert outsider.status_code == 404
    assert [f.filename for f in files.list_for_project(project["id"]).data] == ["plan.md"]

    failed = client.delete(f"/v1/projects/{project['id']}", headers=_hdr(ALICE))
    assert failed.status_code == 500
    assert client.get(f"/v1/projects/{project['id']}", headers=_hdr(ALICE)).status_code == 404
    assert [f.filename for f in files.list_for_project(project["id"]).data] == ["late.md"]
    assert list(artifacts.iterdir()) == []

    retried = client.delete(f"/v1/projects/{project['id']}", headers=_hdr(ALICE))
    assert retried.status_code == 404
    assert files.list_for_project(project["id"]).data == []


@pytest.mark.parametrize("route", ["owner", "session"])
def test_a_project_refuses_files_that_need_local_tools(
    db_uri: str, tmp_path: Path, route: str
) -> None:
    """Archives and documents stay session attachments; a project stores what agents can read."""
    _ensure_agent(db_uri)
    client = TestClient(_multi_user_app(db_uri, artifact_root=tmp_path / "artifacts"))
    project = client.post("/v1/projects", json={"name": "P"}, headers=_hdr(ALICE)).json()
    member = _filed_session(db_uri, client, ALICE, project["id"])
    url = (
        f"/v1/projects/{project['id']}/resources/files"
        if route == "owner"
        else f"/v1/sessions/{member}/resources/project-files"
    )

    resp = client.post(
        url,
        files={"file": ("bundle.zip", b"PK\x03\x04", "application/zip")},
        headers=_hdr(ALICE),
    )

    assert resp.status_code == 415, resp.text
    assert SqlAlchemyFileStore(db_uri).list_for_project(project["id"]).data == []
