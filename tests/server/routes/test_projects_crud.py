"""Tests for the projects CRUD routes (``/v1/projects``).

The projects router is only mounted when ``create_app`` receives a
``project_store``. The standard conftest ``app`` fixture does not supply one, so
these tests build their own app/client that include it.

Two auth setups are exercised:

- **Single-user** (``project_client``) — no auth provider, so the owner scope is
  the reserved ``None``. This is the OSS / local default.
- **Multi-user** (``multi_user_client`` + ``as_user``) — header auth
  (``UnifiedAuthProvider(source="header")``), so each request's owner is the
  ``X-Forwarded-Email`` identity. Used to prove projects are owner-private: one
  user can never see or mutate another's projects.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.entities.project import (
    MANDATE_PROPOSAL_MAX_BYTES,
    MANDATE_PROPOSALS_MAX_PENDING,
    PROJECT_MANDATE_MAX_BYTES,
)
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runtime import user_session_stream
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import _ensure_default_native_agents, create_app
from omnigent.server.auth import UnifiedAuthProvider
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
from tests.server.helpers import build_agent_bundle

ALICE = "alice@example.com"
BOB = "bob@example.com"
PROJECT_AGENT_ID = "187b7cb7ac30abf4debfaa578d052ec6"


@pytest.fixture
def project_agents(db_uri: str, tmp_path: Path) -> SqlAlchemyAgentStore:
    store = SqlAlchemyAgentStore(db_uri)
    bundle_key = f"{PROJECT_AGENT_ID}/bundle"
    LocalArtifactStore(str(tmp_path / "artifacts")).put(
        bundle_key, build_agent_bundle("project-agent")
    )
    store.create(PROJECT_AGENT_ID, "project-agent", bundle_key)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    _ensure_default_native_agents(
        store, artifacts, AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "seed-cache")
    )
    return store


def _as_user(user: str) -> dict[str, str]:
    """Header identifying the requesting user under header auth."""
    return {"X-Forwarded-Email": user}


@pytest.fixture()
def project_app(
    runtime_init: None, db_uri: str, tmp_path: Path, project_agents: SqlAlchemyAgentStore
) -> FastAPI:
    """Build a FastAPI app that includes the project store."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=project_agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        project_store=SqlAlchemyProjectStore(db_uri),
    )


@pytest_asyncio.fixture()
async def project_client(
    project_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the project-enabled app."""
    transport = httpx.ASGITransport(app=project_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_create_project(project_client: httpx.AsyncClient) -> None:
    """Creating a project returns the project object."""
    resp = await project_client.post("/v1/projects", json={"name": "My Project"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "My Project"
    assert body["object"] == "project"
    assert len(body["id"]) == 32
    assert body["updated_at"] is None


async def test_create_trims_and_rejects_empty(project_client: httpx.AsyncClient) -> None:
    """Names are trimmed; empty/whitespace-only names are rejected with 422."""
    resp = await project_client.post("/v1/projects", json={"name": "  Padded  "})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Padded"

    resp = await project_client.post("/v1/projects", json={"name": "   "})
    assert resp.status_code == 422


async def test_create_duplicate_name_conflicts(project_client: httpx.AsyncClient) -> None:
    """Two projects with the same name for one owner returns 409."""
    await project_client.post("/v1/projects", json={"name": "dup"})
    resp = await project_client.post("/v1/projects", json={"name": "dup"})
    assert resp.status_code == 409


async def test_list_projects(project_client: httpx.AsyncClient) -> None:
    """Listing returns the created projects."""
    await project_client.post("/v1/projects", json={"name": "A"})
    await project_client.post("/v1/projects", json={"name": "B"})
    resp = await project_client.get("/v1/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert {p["name"] for p in body["data"]} == {"A", "B"}


async def test_list_projects_pages_in_creation_order(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """A ``limit``/``after`` request pages the caller's own projects without gaps."""
    for name in ("First", "Second", "Third"):
        await multi_user_client.post("/v1/projects", json={"name": name}, headers=_as_user(ALICE))
    bob = await multi_user_client.post("/v1/projects", json={"name": "Bob"}, headers=_as_user(BOB))

    first = await multi_user_client.get(
        "/v1/projects", params={"limit": 2}, headers=_as_user(ALICE)
    )
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page["data"]) == 2
    assert page["has_more"] is True and page["last_id"] == page["data"][-1]["id"]

    rest = await multi_user_client.get(
        "/v1/projects", params={"limit": 2, "after": page["last_id"]}, headers=_as_user(ALICE)
    )
    assert rest.json()["has_more"] is False
    paged = page["data"] + rest.json()["data"]
    assert sorted(p["name"] for p in paged) == ["First", "Second", "Third"]
    # Projects created in the same second fall back to id order.
    keyset = sorted(paged, key=lambda p: (p["created_at"], p["id"]))
    assert [p["id"] for p in paged] == [p["id"] for p in keyset]

    foreign = await multi_user_client.get(
        "/v1/projects", params={"after": bob.json()["id"]}, headers=_as_user(ALICE)
    )
    assert foreign.status_code == 400
    unknown = await multi_user_client.get(
        "/v1/projects", params={"after": "0" * 32}, headers=_as_user(ALICE)
    )
    assert unknown.status_code == 400
    oversized = await multi_user_client.get(
        "/v1/projects", params={"limit": 201}, headers=_as_user(ALICE)
    )
    assert oversized.status_code == 422


async def test_get_project(project_client: httpx.AsyncClient) -> None:
    """A created project can be fetched by id; unknown ids 404."""
    created = (await project_client.post("/v1/projects", json={"name": "X"})).json()
    resp = await project_client.get(f"/v1/projects/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "X"

    missing = await project_client.get(f"/v1/projects/{'0' * 32}")
    assert missing.status_code == 404


async def test_rename_project(project_client: httpx.AsyncClient) -> None:
    """PATCH renames the project and stamps ``updated_at``."""
    created = (await project_client.post("/v1/projects", json={"name": "Old"})).json()
    resp = await project_client.patch(f"/v1/projects/{created['id']}", json={"name": "New"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "New"
    assert body["updated_at"] is not None


async def test_rename_missing_project_404(project_client: httpx.AsyncClient) -> None:
    """Renaming an unknown project returns 404."""
    resp = await project_client.patch(f"/v1/projects/{'0' * 32}", json={"name": "X"})
    assert resp.status_code == 404


async def test_create_defaults_to_empty_config(project_client: httpx.AsyncClient) -> None:
    """A project created without a config field reports an empty config."""
    body = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    assert body["config"] == {}


async def test_create_and_get_roundtrips_config(project_client: httpx.AsyncClient) -> None:
    """A config passed on create round-trips through the create + get responses."""
    cfg = {"workspace": "/work/repo", "icon": "🔧"}
    created = (
        await project_client.post("/v1/projects", json={"name": "Configured", "config": cfg})
    ).json()
    assert created["config"] == cfg
    fetched = (await project_client.get(f"/v1/projects/{created['id']}")).json()
    assert fetched["config"] == cfg


@pytest.mark.parametrize(
    "repository",
    [
        {"url": "https://git.example.com/team/app.git", "branch": "main"},
        # A legacy bare string, and a null, are stored as-is; the composer ignores them.
        "https://git.example.com/team/app.git",
        None,
    ],
    ids=["object", "legacy-string", "null"],
)
async def test_repository_config_is_stored_opaquely(
    project_client: httpx.AsyncClient, repository: object
) -> None:
    """The saved repository round-trips through create, get and rename untouched."""
    config = {"agent_id": "187b7cb7ac30abf4debfaa578d052ec6", "repository": repository}
    created = (
        await project_client.post("/v1/projects", json={"name": "Repo", "config": config})
    ).json()
    assert created["config"] == config
    fetched = (await project_client.get(f"/v1/projects/{created['id']}")).json()
    assert fetched["config"] == config
    renamed = await project_client.patch(f"/v1/projects/{created['id']}", json={"name": "Renamed"})
    assert renamed.json()["config"] == config


async def test_patch_replaces_config(project_client: httpx.AsyncClient) -> None:
    """PATCH with a new config replaces the stored one and stamps updated_at."""
    created = (
        await project_client.post(
            "/v1/projects", json={"name": "P", "config": {"workspace": "/work/old"}}
        )
    ).json()
    resp = await project_client.patch(
        f"/v1/projects/{created['id']}", json={"config": {"host_id": "new", "model": "m"}}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"] == {"host_id": "new", "model": "m"}
    assert body["updated_at"] is not None


async def test_patch_without_config_preserves_it(project_client: httpx.AsyncClient) -> None:
    """A PATCH that omits config (e.g. a rename) leaves the stored config intact."""
    created = (
        await project_client.post(
            "/v1/projects", json={"name": "P", "config": {"workspace": "/work/keep"}}
        )
    ).json()
    renamed = await project_client.patch(f"/v1/projects/{created['id']}", json={"name": "P2"})
    assert renamed.json()["config"] == {"workspace": "/work/keep"}


async def test_patch_empty_config_clears_it(project_client: httpx.AsyncClient) -> None:
    """PATCH with config={} clears the stored defaults (distinct from omitting)."""
    created = (
        await project_client.post(
            "/v1/projects", json={"name": "P", "config": {"workspace": "/work/drop"}}
        )
    ).json()
    resp = await project_client.patch(f"/v1/projects/{created['id']}", json={"config": {}})
    assert resp.json()["config"] == {}


async def test_mandate_round_trips_trims_and_clears(project_client: httpx.AsyncClient) -> None:
    """A mandate is stored trimmed, survives unrelated edits and clears when blank."""
    created = (
        await project_client.post(
            "/v1/projects", json={"name": "P", "mandate": "  Only edit docs/.\n\n- No pushes.  "}
        )
    ).json()
    assert created["mandate"] == "Only edit docs/.\n\n- No pushes."
    path = f"/v1/projects/{created['id']}"
    renamed = (await project_client.patch(path, json={"name": "P2"})).json()
    assert renamed["mandate"] == created["mandate"]
    assert renamed["config"] == {}
    replaced = (await project_client.patch(path, json={"mandate": "Read-only review."})).json()
    assert replaced["mandate"] == "Read-only review."
    assert replaced["updated_at"] is not None
    assert (await project_client.get(path)).json()["mandate"] == "Read-only review."
    assert (await project_client.patch(path, json={"mandate": "  "})).json()["mandate"] is None
    plain = (await project_client.post("/v1/projects", json={"name": "Plain"})).json()
    assert plain["mandate"] is None


async def test_mandate_size_is_bounded(project_client: httpx.AsyncClient) -> None:
    """The limit counts UTF-8 bytes, so a mandate always fits its stored column."""
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    path = f"/v1/projects/{created['id']}"
    fits = "é" * (PROJECT_MANDATE_MAX_BYTES // 2)
    assert (await project_client.patch(path, json={"mandate": fits})).json()["mandate"] == fits
    too_large = await project_client.patch(path, json={"mandate": fits + "é"})
    assert too_large.status_code == 400
    assert "project mandate too large" in too_large.text
    assert (await project_client.get(path)).json()["mandate"] == fits


async def test_agents_cannot_set_or_change_a_mandate(project_client: httpx.AsyncClient) -> None:
    """The sessions a mandate bounds can still rename a project, but not rewrite its mandate."""
    created = (
        await project_client.post("/v1/projects", json={"name": "P", "mandate": "Stay in docs/."})
    ).json()
    agent = {ORCHESTRATION_SOURCE_HEADER: created["main_session_id"]}
    path = f"/v1/projects/{created['id']}"
    refused = await project_client.patch(path, json={"mandate": "Anything goes."}, headers=agent)
    assert refused.status_code == 403
    renamed = await project_client.patch(path, json={"name": "P2"}, headers=agent)
    assert renamed.json()["mandate"] == "Stay in docs/."
    create = await project_client.post(
        "/v1/projects", json={"name": "Q", "mandate": "Mine."}, headers=agent
    )
    assert create.status_code == 403


async def test_session_snapshot_returns_the_mandate_only_on_request(
    project_client: httpx.AsyncClient,
) -> None:
    """Runners opt in to the mandate; ordinary snapshot reads stay unchanged."""
    created = (
        await project_client.post("/v1/projects", json={"name": "P", "mandate": "Stay in docs/."})
    ).json()
    path = f"/v1/sessions/{created['main_session_id']}"
    assert (await project_client.get(path)).json()["project_mandate"] is None
    requested = await project_client.get(
        path, params={"include_items": "false", "include_project_mandate": "true"}
    )
    assert requested.json()["project_mandate"] == "Stay in docs/."
    await project_client.patch(f"/v1/projects/{created['id']}", json={"mandate": "Docs only."})
    requested = await project_client.get(path, params={"include_project_mandate": "true"})
    assert requested.json()["project_mandate"] == "Docs only."


async def test_agent_proposals_wait_for_the_owner_before_reaching_any_session(
    project_client: httpx.AsyncClient,
) -> None:
    """An agent can propose a mandate addition, but only the owner can apply it."""
    created = (
        await project_client.post("/v1/projects", json={"name": "P", "mandate": "Stay in docs/."})
    ).json()
    source = created["main_session_id"]
    agent = {ORCHESTRATION_SOURCE_HEADER: source}
    base = f"/v1/projects/{created['id']}/mandate/proposals"
    proposed = await project_client.post(
        base, json={"text": "  Attach a video to every change.  "}, headers=agent
    )
    assert proposed.status_code == 200
    proposal = proposed.json()["proposal"]
    assert proposal["text"] == "Attach a video to every change."
    assert proposal["session_id"] == source
    project = (await project_client.get(f"/v1/projects/{created['id']}")).json()
    assert project["mandate"] == "Stay in docs/."
    assert [entry["id"] for entry in project["mandate_proposals"]] == [proposal["id"]]
    snapshot = await project_client.get(
        f"/v1/sessions/{source}", params={"include_project_mandate": "true"}
    )
    assert snapshot.json()["project_mandate"] == "Stay in docs/."

    accept = f"{base}/{proposal['id']}/accept"
    assert (await project_client.post(accept, headers=agent)).status_code == 403
    assert (
        await project_client.delete(f"{base}/{proposal['id']}", headers=agent)
    ).status_code == 403
    accepted = (await project_client.post(accept)).json()
    assert accepted["mandate"] == "Stay in docs/.\n\nAttach a video to every change."
    assert accepted["mandate_proposals"] == []
    assert (await project_client.post(accept)).status_code == 404

    dismissed_id = (await project_client.post(base, json={"text": "Skip tests."})).json()[
        "proposal"
    ]["id"]
    dismissed = (await project_client.delete(f"{base}/{dismissed_id}")).json()
    assert dismissed["mandate_proposals"] == []
    assert dismissed["mandate"] == accepted["mandate"]


async def test_mandate_proposals_are_bounded(project_client: httpx.AsyncClient) -> None:
    """Blank, oversized and excess proposals are refused; so is an overflowing accept."""
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    base = f"/v1/projects/{created['id']}/mandate/proposals"
    assert (await project_client.post(base, json={"text": "  "})).status_code == 400
    oversized = "x" * (MANDATE_PROPOSAL_MAX_BYTES + 1)
    assert (await project_client.post(base, json={"text": oversized})).status_code == 400
    ids = [
        (await project_client.post(base, json={"text": f"Rule {index}."})).json()["proposal"]["id"]
        for index in range(MANDATE_PROPOSALS_MAX_PENDING)
    ]
    assert (await project_client.post(base, json={"text": "One more."})).status_code == 409
    await project_client.delete(f"{base}/{ids[0]}")
    full = "y" * (PROJECT_MANDATE_MAX_BYTES - 5)
    await project_client.patch(f"/v1/projects/{created['id']}", json={"mandate": full})
    overflow = await project_client.post(f"{base}/{ids[1]}/accept")
    assert overflow.status_code == 400
    project = (await project_client.get(f"/v1/projects/{created['id']}")).json()
    assert project["mandate"] == full
    assert len(project["mandate_proposals"]) == MANDATE_PROPOSALS_MAX_PENDING - 1
    missing = await project_client.post(
        f"/v1/projects/{uuid.uuid4().hex}/mandate/proposals", json={"text": "Rule."}
    )
    assert missing.status_code == 404


async def test_agents_create_and_edit_their_own_identities_but_not_the_owners(
    project_client: httpx.AsyncClient,
) -> None:
    """Agent-made identities apply at once and stay agent-editable; owner-made ones do not."""
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    source = created["main_session_id"]
    agent = {ORCHESTRATION_SOURCE_HEADER: source}
    identities = f"/v1/projects/{created['id']}/identities"
    pm = await project_client.post(
        identities, json={"name": " PM ", "instructions": "You own the roadmap."}, headers=agent
    )
    assert pm.status_code == 200
    assert (pm.json()["name"], pm.json()["created_by_session_id"]) == ("PM", source)
    for name in ("orchestrator", "Lead"):
        reserved = await project_client.post(
            identities, json={"name": name, "instructions": "x"}, headers=agent
        )
        assert reserved.status_code == 400
    duplicate = await project_client.post(
        identities, json={"name": "pm", "instructions": "x"}, headers=agent
    )
    assert duplicate.status_code == 409
    edited = await project_client.patch(
        f"{identities}/{pm.json()['id']}", json={"instructions": "You own pricing."}, headers=agent
    )
    assert edited.json()["instructions"] == "You own pricing."
    owners = (
        await project_client.post(identities, json={"name": "Ops", "instructions": "Mine."})
    ).json()
    assert owners["created_by_session_id"] is None
    refused = await project_client.patch(
        f"{identities}/{owners['id']}", json={"instructions": "Anything."}, headers=agent
    )
    assert refused.status_code == 403
    deleted = await project_client.delete(f"{identities}/{pm.json()['id']}", headers=agent)
    assert deleted.status_code == 403
    names = [entry["name"] for entry in (await project_client.get(identities)).json()["data"]]
    assert names[0] == "Lead" and sorted(names[1:]) == ["Ops", "PM"]


async def test_only_the_owner_edits_the_leads_charter_and_only_the_lead_reads_it(
    project_client: httpx.AsyncClient,
) -> None:
    """The Lead lists with its charter; agents can't write it, nobody renames or deletes it."""
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    agent = {ORCHESTRATION_SOURCE_HEADER: created["main_session_id"]}
    lead_path = f"/v1/projects/{created['id']}/identities/orchestrator"
    saved = await project_client.patch(lead_path, json={"instructions": "  Batch reviews.  "})
    assert (saved.json()["name"], saved.json()["instructions"]) == ("Lead", "Batch reviews.")
    refused = await project_client.patch(lead_path, json={"instructions": "x"}, headers=agent)
    assert refused.status_code == 403
    renamed = await project_client.patch(lead_path, json={"name": "Boss", "instructions": "x"})
    assert renamed.status_code == 400
    assert (await project_client.delete(lead_path)).status_code == 400
    # A client config replace keeps the server-owned charter.
    await project_client.patch(f"/v1/projects/{created['id']}", json={"config": {"model": "m"}})
    listed = (await project_client.get(f"/v1/projects/{created['id']}/identities")).json()
    assert listed["data"][0]["instructions"] == "Batch reviews."
    params = {"include_project_mandate": "true"}
    snapshot = await project_client.get(
        f"/v1/sessions/{created['main_session_id']}", params=params
    )
    assert snapshot.json()["project_lead_charter"] == "Batch reviews."
    persona = (
        await project_client.post(
            f"/v1/projects/{created['id']}/identities", json={"name": "PM", "instructions": "y"}
        )
    ).json()
    worker = await project_client.get(
        f"/v1/sessions/{persona['active_session_id']}", params=params
    )
    assert worker.json()["project_lead_charter"] is None
    cleared = await project_client.patch(lead_path, json={"instructions": " "})
    assert cleared.json()["instructions"] == ""
    # Its agent, model and effort are the project's defaults; agents can't change them.
    tuned = await project_client.patch(lead_path, json={"model": "m2", "effort": "high"})
    assert (tuned.json()["model"], tuned.json()["effort"]) == ("m2", "high")
    stored = (await project_client.get(f"/v1/projects/{created['id']}")).json()["config"]
    assert (stored["model"], stored["effort"]) == ("m2", "high")
    refused = await project_client.patch(lead_path, json={"model": "m3"}, headers=agent)
    assert refused.status_code == 403
    reset = await project_client.patch(lead_path, json={"effort": ""})
    assert (reset.json()["model"], reset.json()["effort"]) == ("m2", None)


async def test_the_owner_routes_events_to_a_persona_and_can_remove_the_lead(
    project_client: httpx.AsyncClient,
) -> None:
    """Routing defaults to the Lead, follows the owner's choice, then the first persona."""
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    project = f"/v1/projects/{created['id']}"
    lead = {"identity_id": "orchestrator", "name": "Lead"}
    assert created["router"] == lead
    pm = (await project_client.post(f"{project}/identities", json={"name": "PM"})).json()
    qa = (await project_client.post(f"{project}/identities", json={"name": "QA"})).json()
    agent = {ORCHESTRATION_SOURCE_HEADER: created["main_session_id"]}
    refused = await project_client.put(
        f"{project}/routing", json={"router_identity_id": qa["id"]}, headers=agent
    )
    assert refused.status_code == 403

    async def route(body: dict[str, object]) -> dict[str, object]:
        response = await project_client.put(f"{project}/routing", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    chosen = await route({"router_identity_id": qa["id"], "lead_removed": True})
    assert chosen["router"] == {"identity_id": qa["id"], "name": "QA"}
    listed = (await project_client.get(f"{project}/identities")).json()["data"]
    assert sorted((entry["name"], entry["router"]) for entry in listed) == [
        ("PM", False),
        ("QA", True),
    ]
    snapshot = await project_client.get(f"/v1/sessions/{qa['active_session_id']}")
    assert snapshot.json()["project_router"] is True
    lead_session = await project_client.get(f"/v1/sessions/{created['main_session_id']}")
    assert lead_session.json()["project_router"] is False
    # The router coordinates, so it sets `kind`; a worker or the removed Lead cannot.
    ticket = (await project_client.post(f"{project}/tickets", json={"title": "T"})).json()
    kind = {"labels": [{"key": "kind", "value": "fix"}]}
    for session_id, status in (
        (pm["active_session_id"], 403),
        (created["main_session_id"], 403),
        (qa["active_session_id"], 200),
    ):
        patched = await project_client.patch(
            f"{project}/tickets/{ticket['id']}",
            json=kind,
            headers={ORCHESTRATION_SOURCE_HEADER: session_id},
        )
        assert patched.status_code == status, (session_id, patched.text)
    # The router's own comment is for the owner and isn't routed back to it.
    for session_id in (qa["active_session_id"], pm["active_session_id"]):
        posted = await project_client.post(
            f"{project}/tickets/{ticket['id']}/comments",
            json={"text": f"note from {session_id[:8]}"},
            headers={ORCHESTRATION_SOURCE_HEADER: session_id},
        )
        assert posted.status_code == 201, posted.text
    comments = (
        await project_client.get(
            f"{project}/tickets/{ticket['id']}/events", params={"kind": "comment"}
        )
    ).json()["data"]
    assert [
        (event["session_id"], event["payload"]["audience"], event["notification_pending"])
        for event in comments[-2:]
    ] == [
        (qa["active_session_id"], "human", False),
        (pm["active_session_id"], "agents", True),
    ]
    # A client config replace keeps the server-owned routing settings.
    await project_client.patch(project, json={"config": {"model": "m"}})
    assert (await project_client.get(project)).json()["router"]["name"] == "QA"
    first = await route({"router_identity_id": None, "lead_removed": True})
    # Personas created in the same second order by id, so read the listing's first.
    assert first["router"] == {"identity_id": listed[0]["id"], "name": listed[0]["name"]}
    removing = await project_client.put(
        f"{project}/routing", json={"router_identity_id": "orchestrator", "lead_removed": True}
    )
    assert removing.status_code == 400
    restored = await route({"router_identity_id": None, "lead_removed": False})
    assert (restored["router"], restored["lead_removed"]) == (lead, False)


async def test_agents_open_an_identitys_one_session_but_cannot_edit_it(
    project_client: httpx.AsyncClient,
) -> None:
    """Delegating to an owner-made identity reuses the session it started with."""
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    agent = {ORCHESTRATION_SOURCE_HEADER: created["main_session_id"]}
    identities = f"/v1/projects/{created['id']}/identities"
    pm = (
        await project_client.post(identities, json={"name": "PM", "instructions": "Plan."})
    ).json()
    assert pm["active_session_state"] in ("idle", "busy")
    session = f"{identities}/{pm['id']}/session"
    first = await project_client.post(session, json={"expected_session_id": None}, headers=agent)
    assert first.status_code == 200, first.text
    assert first.json() == {"session_id": pm["active_session_id"], "created": False}
    again = (
        await project_client.post(session, json={"expected_session_id": None}, headers=agent)
    ).json()
    assert again == {"session_id": pm["active_session_id"], "created": False}
    snapshot = await project_client.get(
        f"/v1/sessions/{again['session_id']}", params={"include_project_mandate": "true"}
    )
    assert snapshot.json()["project_identity"] == {
        "id": pm["id"],
        "name": "PM",
        "instructions": "Plan.",
    }
    orchestrator = await project_client.post(
        f"{identities}/orchestrator/session", json={}, headers=agent
    )
    assert orchestrator.status_code == 400
    edit = await project_client.patch(
        f"{identities}/{pm['id']}", json={"instructions": "Anything."}, headers=agent
    )
    assert edit.status_code == 403
    assert (
        await project_client.delete(f"{identities}/{pm['id']}", headers=agent)
    ).status_code == 403


async def test_identity_sessions_come_back_after_delete_or_archive(
    project_client: httpx.AsyncClient,
) -> None:
    """A persona always has exactly one live session; listing restores a lost one."""
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    identities = f"/v1/projects/{created['id']}/identities"
    pm = (
        await project_client.post(identities, json={"name": "PM", "instructions": "Plan."})
    ).json()
    first = pm["active_session_id"]
    assert first is not None

    def persona(listing: dict) -> dict:
        return next(entry for entry in listing["data"] if entry["id"] == pm["id"])

    assert (await project_client.delete(f"/v1/sessions/{first}")).status_code == 200
    racing = await asyncio.gather(
        project_client.get(identities),
        project_client.get(identities),
        project_client.post(f"{identities}/{pm['id']}/session", json={}),
    )
    seen = {persona(racing[0].json())["active_session_id"]}
    seen |= {persona(racing[1].json())["active_session_id"], racing[2].json()["session_id"]}
    assert len(seen) == 1 and first not in seen
    restored = persona((await project_client.get(identities)).json())
    second = restored["active_session_id"]
    assert second not in (None, first)
    assert restored["active_session_state"] in ("idle", "busy")
    assert persona((await project_client.get(identities)).json())["active_session_id"] == second

    archived = await project_client.patch(f"/v1/sessions/{second}", json={"archived": True})
    assert archived.status_code == 200
    third = persona((await project_client.get(identities)).json())["active_session_id"]
    assert third not in (None, first, second)
    snapshot = (
        await project_client.get(
            f"/v1/sessions/{third}", params={"include_project_mandate": "true"}
        )
    ).json()
    assert snapshot["project_identity"]["instructions"] == "Plan."
    live = [
        session
        for session in (
            await project_client.get("/v1/sessions", params={"project_id": created["id"]})
        ).json()["data"]
        if session.get("title") == "PM" and not session.get("archived")
    ]
    assert [session["id"] for session in live] == [third]


async def test_a_deleted_orchestrator_comes_back_but_an_archived_one_waits(
    project_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing identities restores a deleted main session; archive and rollover are left alone."""
    from omnigent.server import project_orchestrator_rollover as rollover

    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    identities = f"/v1/projects/{created['id']}/identities"
    first = created["main_session_id"]

    async def orchestrator() -> dict:
        return (await project_client.get(identities)).json()["data"][0]

    assert (await project_client.delete(f"/v1/sessions/{first}")).status_code == 200
    monkeypatch.setitem(rollover._project_rollovers, created["id"], first)
    waiting = await orchestrator()
    assert (waiting["active_session_id"], waiting["active_session_state"]) == (first, "missing")
    monkeypatch.delitem(rollover._project_rollovers, created["id"])

    racing = await asyncio.gather(orchestrator(), orchestrator())
    second = racing[0]["active_session_id"]
    assert second not in (None, first)
    assert {entry["active_session_id"] for entry in racing} == {second}
    assert racing[0]["active_session_state"] == "active"
    project = (await project_client.get(f"/v1/projects/{created['id']}")).json()
    assert project["main_session_id"] == second

    await project_client.patch(f"/v1/sessions/{second}", json={"archived": True})
    archived = await orchestrator()
    assert (archived["active_session_id"], archived["active_session_state"]) == (
        second,
        "archived",
    )


async def test_a_comment_ccing_a_persona_reaches_only_that_personas_session(
    project_client: httpx.AsyncClient, project_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """@PM in a comment is recorded and delivered to PM's session; Designer isn't woken."""
    from omnigent.server.persona_mentions import mention_stable_id
    from omnigent.server.routes import project_tickets

    delivered: list[tuple[str, str, str]] = []

    async def deliver_to_session(app, project, session_id, *, stable_id, text) -> bool:
        delivered.append((session_id, stable_id, text))
        return True

    monkeypatch.setattr(
        project_app.state.project_ticket_notifications,
        "deliver_to_session",
        deliver_to_session,
        raising=False,
    )
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    identities = f"/v1/projects/{created['id']}/identities"
    pm = (await project_client.post(identities, json={"name": "PM", "instructions": "x"})).json()
    await project_client.post(identities, json={"name": "Designer", "instructions": "y"})
    tickets = f"/v1/projects/{created['id']}/tickets"
    ticket = (await project_client.post(tickets, json={"title": "Stuck"})).json()
    posted = await project_client.post(
        f"{tickets}/{ticket['id']}/comments", json={"text": "cc @pm can you scope this?"}
    )
    assert posted.status_code == 201
    await asyncio.gather(*project_tickets._mention_tasks)
    assert [(session, stable) for session, stable, _ in delivered] == [
        (pm["active_session_id"], mention_stable_id(f"{ticket['id']}:1", pm["id"]))
    ]
    assert 'ticket "Stuck"' in delivered[0][2] and "cc @pm can you scope this?" in delivered[0][2]
    events = (
        await project_client.get(f"{tickets}/{ticket['id']}/events", params={"kind": "comment"})
    ).json()["data"]
    assert events[-1]["payload"]["mentions"] == [{"id": pm["id"], "name": "PM"}]


async def test_review_routes_worker_and_reviewer_and_binds_approval_to_a_commit(
    project_client: httpx.AsyncClient, project_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entering Review hands off to the Reviewer; comments route both ways; approval is gated."""
    from omnigent.server.routes import project_tickets

    delivered: list[tuple[str, str]] = []

    async def deliver_to_session(app, project, session_id, *, stable_id, text) -> bool:
        delivered.append((session_id, text))
        return True

    monkeypatch.setattr(
        project_app.state.project_ticket_notifications,
        "deliver_to_session",
        deliver_to_session,
        raising=False,
    )

    async def settle() -> list[tuple[str, str]]:
        await asyncio.gather(*project_tickets._mention_tasks)
        drained = list(delivered)
        delivered.clear()
        return drained

    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    identities = f"/v1/projects/{created['id']}/identities"
    reviewer = (
        await project_client.post(identities, json={"name": "Reviewer", "instructions": "r"})
    ).json()["active_session_id"]
    worker = (
        await project_client.post(identities, json={"name": "Builder", "instructions": "b"})
    ).json()["active_session_id"]
    as_reviewer = {ORCHESTRATION_SOURCE_HEADER: reviewer}
    as_worker = {ORCHESTRATION_SOURCE_HEADER: worker}
    tickets = f"/v1/projects/{created['id']}/tickets"
    stages = (await project_client.get(f"{tickets}/stages")).json()["stages"]
    review_stage = {"id": "code_review", "name": "Code review"}
    staged = await project_client.put(
        f"{tickets}/stages", json={"stages": [*stages[:2], review_stage, *stages[2:]]}
    )
    assert staged.status_code == 200, staged.text
    ticket = (
        await project_client.post(tickets, json={"title": "Fix it", "description": "Done when X"})
    ).json()
    path = f"{tickets}/{ticket['id']}"
    await project_client.post(f"{path}/sessions", json={"session_id": worker}, headers=as_worker)
    first, second, third = "a" * 40, "b" * 40, "c" * 40

    moved = await project_client.patch(
        path,
        json={"stage_id": "code_review", "comment": f"Branch ptb/x at {first}, base main"},
        headers=as_worker,
    )
    assert moved.status_code == 200, moved.text
    [(session, handoff)] = await settle()
    assert session == reviewer and first in handoff and "Done when X" in handoff
    assert "Confirm the ticket's `kind` label (now unset" in handoff

    labels = [{"key": "approved", "value": first}]
    refused = await project_client.patch(path, json={"labels": labels}, headers=as_worker)
    assert refused.status_code == 403
    for key in ("owner-ok", "risk-ok"):
        sign_off = [{"key": key, "value": first}]
        for headers in (as_worker, as_reviewer):
            denied = await project_client.patch(path, json={"labels": sign_off}, headers=headers)
            assert denied.status_code == 403, denied.text
        preset = await project_client.post(
            tickets, json={"title": "Preset", "labels": sign_off}, headers=as_worker
        )
        assert preset.status_code == 403, preset.text
    kind = [{"key": "kind", "value": "fix"}]
    assert (
        await project_client.patch(path, json={"labels": kind}, headers=as_worker)
    ).status_code == 403
    for headers in (as_worker, as_reviewer):
        product = [*kind, {"key": "product-ok", "value": "yes"}]
        denied = await project_client.patch(path, json={"labels": product}, headers=headers)
        assert denied.status_code == 403, denied.text
    kinded = await project_client.patch(path, json={"labels": kind}, headers=as_reviewer)
    assert kinded.status_code == 200, kinded.text
    malformed = [{"key": "approved", "value": "abc"}]
    bad = await project_client.patch(path, json={"labels": malformed}, headers=as_reviewer)
    assert bad.status_code == 400

    finding = "Changes needed. blocking: off-by-one at x.py:3"
    await project_client.post(f"{path}/comments", json={"text": finding}, headers=as_reviewer)
    [(session, to_worker)] = await settle()
    assert session == worker and "off-by-one" in to_worker

    await project_client.post(
        f"{path}/comments", json={"text": f"Fixed in {second}"}, headers=as_worker
    )
    [(session, to_reviewer)] = await settle()
    assert session == reviewer and second in to_reviewer

    approve = [{"key": "approved", "value": second}, {"key": "risk", "value": "migration"}]
    approved = await project_client.patch(path, json={"labels": approve}, headers=as_reviewer)
    assert approved.status_code == 200
    await settle()

    await project_client.post(
        f"{path}/comments", json={"text": f"One more commit {third}"}, headers=as_worker
    )
    await settle()
    after = (await project_client.get(path)).json()
    assert {label["key"]: label.get("value") for label in after["labels"]} == {"risk": "migration"}
    notes = (await project_client.get(f"{path}/events", params={"kind": "comment"})).json()
    assert any("Approval cleared" in event["payload"]["text"] for event in notes["data"])


async def test_delete_project(project_client: httpx.AsyncClient) -> None:
    """DELETE removes the project; a second delete 404s."""
    created = (await project_client.post("/v1/projects", json={"name": "Doomed"})).json()
    resp = await project_client.delete(f"/v1/projects/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    second_delete = await project_client.delete(f"/v1/projects/{created['id']}")
    assert second_delete.status_code == 404


async def test_session_projects_unions_first_class_and_labels(
    project_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """GET /v1/sessions/projects dual-reads: first-class projects (with id,
    even when empty) unioned with label-only projects (id=None), by name."""
    conv_store = SqlAlchemyConversationStore(db_uri)

    # An empty first-class project — invisible to the label path, must appear.
    empty = (await project_client.post("/v1/projects", json={"name": "Empty FC"})).json()
    # A name that exists BOTH as first-class and as a label — one entry, fc id.
    both = (await project_client.post("/v1/projects", json={"name": "Both"})).json()
    conv_both = conv_store.create_conversation()
    conv_store.set_labels(conv_both.id, {"omni_project": "Both"})
    # A label-only project — no first-class row, id=None.
    conv_label = conv_store.create_conversation()
    conv_store.set_labels(conv_label.id, {"omni_project": "Label Only"})

    resp = await project_client.get("/v1/sessions/projects")
    assert resp.status_code == 200
    assert resp.json() == [
        {"id": both["id"], "name": "Both", "icon": None},
        {"id": empty["id"], "name": "Empty FC", "icon": None},
        {"id": None, "name": "Label Only", "icon": None},
    ]


async def test_session_projects_surfaces_config_icon(
    project_client: httpx.AsyncClient,
) -> None:
    """A first-class project's ``config.icon`` surfaces in the sidebar listing
    so the folder can render the emoji; a non-string/absent icon stays None."""
    fire = (
        await project_client.post("/v1/projects", json={"name": "Fire", "config": {"icon": "🔥"}})
    ).json()
    # A config without an icon key surfaces None (default folder glyph).
    plain = (
        await project_client.post(
            "/v1/projects", json={"name": "Plain", "config": {"workspace": "/work/plain"}}
        )
    ).json()

    resp = await project_client.get("/v1/sessions/projects")
    assert resp.status_code == 200
    assert resp.json() == [
        {"id": fire["id"], "name": "Fire", "icon": "🔥"},
        {"id": plain["id"], "name": "Plain", "icon": None},
    ]


# ── Multi-user: projects are owner-private ─────────────────────────────


@pytest.fixture()
def multi_user_app(
    runtime_init: None, db_uri: str, tmp_path: Path, project_agents: SqlAlchemyAgentStore
) -> FastAPI:
    """A project-enabled app with header auth, so each request has an owner."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=project_agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        project_store=SqlAlchemyProjectStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=SqlAlchemyPermissionStore(db_uri),
    )


@pytest_asyncio.fixture()
async def multi_user_client(
    multi_user_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the multi-user (header-auth) app."""
    transport = httpx.ASGITransport(app=multi_user_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_list_scoped_to_requesting_owner(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Each user sees only their own projects."""
    await multi_user_client.post("/v1/projects", json={"name": "Alice A"}, headers=_as_user(ALICE))
    await multi_user_client.post("/v1/projects", json={"name": "Bob B"}, headers=_as_user(BOB))

    alice = (await multi_user_client.get("/v1/projects", headers=_as_user(ALICE))).json()
    bob = (await multi_user_client.get("/v1/projects", headers=_as_user(BOB))).json()
    assert {p["name"] for p in alice["data"]} == {"Alice A"}
    assert {p["name"] for p in bob["data"]} == {"Bob B"}


async def test_same_name_allowed_across_users(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Two users may each own a project with the same name (per-owner uniqueness)."""
    a = await multi_user_client.post(
        "/v1/projects", json={"name": "Shared"}, headers=_as_user(ALICE)
    )
    b = await multi_user_client.post(
        "/v1/projects", json={"name": "Shared"}, headers=_as_user(BOB)
    )
    assert a.status_code == 200
    assert b.status_code == 200


async def test_cannot_get_another_users_project(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Bob's project is 404 (not found), never readable, for Alice."""
    created = (
        await multi_user_client.post(
            "/v1/projects", json={"name": "Bob only"}, headers=_as_user(BOB)
        )
    ).json()
    resp = await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(ALICE))
    assert resp.status_code == 404
    # The owner still sees it.
    assert (
        await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(BOB))
    ).status_code == 200


async def test_cannot_rename_another_users_project(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Alice cannot rename Bob's project (404), and it stays unchanged."""
    created = (
        await multi_user_client.post(
            "/v1/projects", json={"name": "Bob only"}, headers=_as_user(BOB)
        )
    ).json()
    resp = await multi_user_client.patch(
        f"/v1/projects/{created['id']}", json={"name": "Hacked"}, headers=_as_user(ALICE)
    )
    assert resp.status_code == 404
    still = (
        await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(BOB))
    ).json()
    assert still["name"] == "Bob only"


async def test_cannot_delete_another_users_project(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Alice cannot delete Bob's project (404), and it survives."""
    created = (
        await multi_user_client.post(
            "/v1/projects", json={"name": "Bob only"}, headers=_as_user(BOB)
        )
    ).json()
    resp = await multi_user_client.delete(f"/v1/projects/{created['id']}", headers=_as_user(ALICE))
    assert resp.status_code == 404
    assert (
        await multi_user_client.get(f"/v1/projects/{created['id']}", headers=_as_user(BOB))
    ).status_code == 200


# ── Live broadcast of project changes ────────────────────────────────────────
#
# Project mutations must announce on the owner's session-updates discovery
# channel (``user_session_stream``): that push is the only thing that lets the
# owner's OTHER connected clients (another tab, the mobile app) refresh their
# projects cache, so a rename made here shows up there without a reload.


@contextlib.asynccontextmanager
async def _collect_discovery_events(user_key: str) -> AsyncIterator[list[dict]]:
    """Collect every event published on ``user_key``'s discovery channel."""
    events: list[dict] = []

    async def _drain() -> None:
        async for evt in user_session_stream.subscribe(user_key):
            events.append(evt)

    task = asyncio.create_task(_drain())
    # Let the subscriber register before the caller mutates anything.
    await asyncio.sleep(0)
    try:
        yield events
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _wait_for_events(events: list[dict], count: int, *, timeout: float = 2.0) -> None:
    """Wait until ``events`` holds ``count`` items (publish is a queued hop)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(events) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} events, saw {events}")


async def test_project_mutations_announce_projects_changed_to_owner(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """Create, rename, and delete each push ``projects_changed`` to the owner
    — and never to another user's channel (projects are owner-private)."""
    async with (
        _collect_discovery_events(ALICE) as alice_events,
        _collect_discovery_events(BOB) as bob_events,
    ):
        created = await multi_user_client.post(
            "/v1/projects", json={"name": "Live folder"}, headers=_as_user(ALICE)
        )
        assert created.status_code == 200
        await _wait_for_events(alice_events, 2)

        renamed = await multi_user_client.patch(
            f"/v1/projects/{created.json()['id']}",
            json={"name": "Live folder renamed"},
            headers=_as_user(ALICE),
        )
        assert renamed.status_code == 200
        await _wait_for_events(alice_events, 3)

        deleted = await multi_user_client.delete(
            f"/v1/projects/{created.json()['id']}", headers=_as_user(ALICE)
        )
        assert deleted.status_code == 200
        await _wait_for_events(alice_events, 4)

        assert alice_events == [
            {"type": "session_added", "session_id": created.json()["main_session_id"]},
            *[{"type": "projects_changed"}] * 3,
        ]
        # Give any mis-keyed publish a chance to land before asserting silence.
        await asyncio.sleep(0.05)
        assert bob_events == []


async def test_rejected_project_mutation_announces_nothing(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """A 404'd cross-user rename/delete pushes no event on either channel."""
    created = await multi_user_client.post(
        "/v1/projects", json={"name": "Bob private"}, headers=_as_user(BOB)
    )
    assert created.status_code == 200
    async with (
        _collect_discovery_events(ALICE) as alice_events,
        _collect_discovery_events(BOB) as bob_events,
    ):
        rename = await multi_user_client.patch(
            f"/v1/projects/{created.json()['id']}",
            json={"name": "Hijacked"},
            headers=_as_user(ALICE),
        )
        assert rename.status_code == 404
        delete = await multi_user_client.delete(
            f"/v1/projects/{created.json()['id']}", headers=_as_user(ALICE)
        )
        assert delete.status_code == 404
        await asyncio.sleep(0.05)
        assert alice_events == []
        assert bob_events == []


async def test_project_order_round_trip(project_client: httpx.AsyncClient, db_uri: str) -> None:
    """Custom order affects both discovery APIs and survives rename/delete/reset."""

    async def names(path: str) -> list[str]:
        response = await project_client.get(path)
        response.raise_for_status()
        data = response.json()
        return [p["name"] for p in (data if isinstance(data, list) else data["data"])]

    ids = []
    for name in ["BUG", "DOC", "WORK"]:
        response = await project_client.post("/v1/projects", json={"name": name})
        ids.append(response.json()["id"])
    assert (await project_client.get("/v1/projects/order")).json() == {
        "ordered_project_ids": None,
        "sort_mode": "alphabetical",
    }
    ordered = [ids[2], ids[0], ids[1]]
    response = await project_client.put(
        "/v1/projects/order", json={"ordered_project_ids": ordered}
    )
    assert response.status_code == 200
    assert response.json() == {"ordered_project_ids": ordered, "sort_mode": "manual"}
    # A fresh store reads the persisted preference without a users-table column.
    from sqlalchemy import inspect

    from omnigent.db.utils import get_or_create_engine

    assert "project_order" not in {
        column["name"] for column in inspect(get_or_create_engine(db_uri)).get_columns("users")
    }
    assert SqlAlchemyProjectStore(db_uri).get_order_preference(user_id=None) == response.json()
    for path in ["/v1/projects", "/v1/sessions/projects"]:
        assert await names(path) == ["WORK", "BUG", "DOC"]
    await project_client.patch(f"/v1/projects/{ids[2]}", json={"name": "ZZZ"})
    await project_client.post("/v1/projects", json={"name": "AAA"})
    assert await names("/v1/sessions/projects") == ["ZZZ", "BUG", "DOC", "AAA"]
    await project_client.delete(f"/v1/projects/{ids[0]}")
    assert await names("/v1/sessions/projects") == ["ZZZ", "DOC", "AAA"]
    response = await project_client.put("/v1/projects/order", json={"ordered_project_ids": None})
    assert response.status_code == 200
    assert await names("/v1/sessions/projects") == ["AAA", "DOC", "ZZZ"]
    assert response.json() == {"ordered_project_ids": ordered, "sort_mode": "alphabetical"}
    assert (await project_client.get("/v1/projects/order")).json() == response.json()


async def test_project_order_rejects_invalid_payload(project_client: httpx.AsyncClient) -> None:
    """Malformed/duplicate/unknown IDs never overwrite a saved preference."""
    project = (await project_client.post("/v1/projects", json={"name": "A"})).json()
    for ids in [[project["id"], project["id"]], ["missing"], "invalid"]:
        response = await project_client.put(
            "/v1/projects/order", json={"ordered_project_ids": ids}
        )
        assert response.status_code in (400, 422)
    assert (await project_client.put("/v1/projects/order", json={})).status_code == 422
    assert (await project_client.get("/v1/projects/order")).json() == {
        "ordered_project_ids": None,
        "sort_mode": "alphabetical",
    }


@pytest.mark.parametrize("ids", [["a" * 33], ["g" * 32], ["a" * 32] * 10001])
async def test_project_order_rejects_oversized_or_malformed_ids(
    project_client: httpx.AsyncClient, ids: list[str]
) -> None:
    response = await project_client.put("/v1/projects/order", json={"ordered_project_ids": ids})
    assert response.status_code == 422


async def test_corrupt_order_does_not_break_project_discovery(
    project_client: httpx.AsyncClient, db_uri: str
) -> None:
    from sqlalchemy import LargeBinary, bindparam, update

    from omnigent.db.db_models import SqlPreference
    from omnigent.db.utils import get_or_create_engine

    for name in ("Z", "A"):
        (await project_client.post("/v1/projects", json={"name": name})).raise_for_status()
    (
        await project_client.put("/v1/projects/order", json={"ordered_project_ids": []})
    ).raise_for_status()
    with get_or_create_engine(db_uri).begin() as connection:
        connection.execute(
            update(SqlPreference)
            .where(
                SqlPreference.workspace_id == 0,
                SqlPreference.user_id == "local",
                SqlPreference.key == "project_order",
            )
            .values(value=bindparam("raw", type_=LargeBinary)),
            {"raw": b"\x00\x01broken compression"},
        )
    preference = await project_client.get("/v1/projects/order")
    assert preference.status_code == 200
    assert preference.json() == {"sort_mode": "alphabetical", "ordered_project_ids": None}
    for path in ("/v1/projects", "/v1/sessions/projects"):
        response = await project_client.get(path)
        assert response.status_code == 200
        body = response.json()
        assert [p["name"] for p in (body if isinstance(body, list) else body["data"])] == [
            "A",
            "Z",
        ]


async def test_order_endpoints_enforce_owner_and_announce_changes(
    multi_user_client: httpx.AsyncClient,
) -> None:
    client = multi_user_client
    created = await client.post("/v1/projects", headers=_as_user(ALICE), json={"name": "A"})
    project_id = created.json()["id"]
    assert (await client.get("/v1/projects/order")).status_code == 401
    assert (
        await client.put("/v1/projects/order", json={"ordered_project_ids": None})
    ).status_code == 401
    async with (
        _collect_discovery_events(ALICE) as alice_events,
        _collect_discovery_events(BOB) as bob_events,
    ):
        rejected = await client.put(
            "/v1/projects/order",
            headers=_as_user(BOB),
            json={"ordered_project_ids": [project_id]},
        )
        assert rejected.status_code == 400
        assert (await client.get("/v1/projects/order", headers=_as_user(BOB))).json() == {
            "sort_mode": "alphabetical",
            "ordered_project_ids": None,
        }
        for ids in ([project_id], None):
            response = await client.put(
                "/v1/projects/order",
                headers=_as_user(ALICE),
                json={"ordered_project_ids": ids},
            )
            assert response.status_code == 200
        await _wait_for_events(alice_events, 2)
        assert alice_events == [{"type": "projects_changed"}] * 2
        await asyncio.sleep(0.05)
        assert bob_events == []
