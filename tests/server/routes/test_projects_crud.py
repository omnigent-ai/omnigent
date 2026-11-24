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
import random
import string
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
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
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
    # Within the byte limit but too random to compress into the stored column.
    noise = "".join(random.Random(3).choices(string.ascii_letters, k=PROJECT_MANDATE_MAX_BYTES))
    refused = await project_client.patch(path, json={"mandate": noise})
    assert refused.status_code == 400
    assert "too large to store" in refused.text
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


async def test_replacement_proposals_show_their_target_apply_on_accept_and_go_stale(
    project_client: httpx.AsyncClient,
) -> None:
    """Agents propose section and whole-text replacements; only fresh ones can be accepted."""
    mandate = "# Rules\n\n## Push\n\nAsk first.\n\n## Git\n\nTrailer."
    created = (
        await project_client.post("/v1/projects", json={"name": "P", "mandate": mandate})
    ).json()
    agent = {ORCHESTRATION_SOURCE_HEADER: created["main_session_id"]}
    base = f"/v1/projects/{created['id']}/mandate/proposals"
    section = await project_client.post(
        base,
        json={"mode": "replace_section", "section": "push", "text": "## Push\n\nPush Verified work."},
        headers=agent,
    )
    assert section.status_code == 200, section.text
    proposal = section.json()["proposal"]
    assert proposal["mode"] == "replace_section" and proposal["section"] == "push"
    assert proposal["current_text"] == "## Push\n\nAsk first." and proposal["stale"] is False
    delete = (
        await project_client.post(
            base, json={"mode": "replace_section", "section": "Git", "text": ""}, headers=agent
        )
    ).json()["proposal"]
    whole = (
        await project_client.post(
            base, json={"mode": "replace_all", "text": "# Rules\n\nAll new."}, headers=agent
        )
    ).json()["proposal"]
    for bad in (
        {"mode": "replace_section", "section": "Missing", "text": "x"},
        {"mode": "replace_section", "text": "x"},
        {"mode": "replace_all", "text": " "},
        {"mode": "append", "section": "Git", "text": "x"},
    ):
        assert (await project_client.post(base, json=bad, headers=agent)).status_code == 400, bad
    assert (await project_client.post(f"{base}/{proposal['id']}/accept", headers=agent)).status_code == 403
    accepted = (await project_client.post(f"{base}/{proposal['id']}/accept")).json()
    assert accepted["mandate"] == "# Rules\n\n## Push\n\nPush Verified work.\n\n## Git\n\nTrailer."
    pending = {entry["id"]: entry for entry in accepted["mandate_proposals"]}
    # Editing the Push section leaves the Git deletion fresh but the whole-text one stale.
    assert pending[delete["id"]]["stale"] is False
    assert pending[whole["id"]]["stale"] is True
    stale = await project_client.post(f"{base}/{whole['id']}/accept")
    assert stale.status_code == 409
    assert "changed since this proposal" in stale.text
    final = (await project_client.post(f"{base}/{delete['id']}/accept")).json()
    assert final["mandate"] == "# Rules\n\n## Push\n\nPush Verified work."
    assert [entry["id"] for entry in final["mandate_proposals"]] == [whole["id"]]


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

    # Once landed, a comment leading with another SHA (an upstream tip) keeps the record.
    reapprove = [{"key": "approved", "value": third}, {"key": "risk", "value": "migration"}]
    assert (
        await project_client.patch(path, json={"labels": reapprove}, headers=as_reviewer)
    ).status_code == 200
    landed_stage = stages[2]["id"]  # the stage right after Review
    moved = await project_client.patch(path, json={"stage_id": landed_stage}, headers=as_worker)
    assert moved.status_code == 200, moved.text
    upstream = "d" * 40
    await project_client.post(
        f"{path}/comments", json={"text": f"{upstream} is the new upstream tip"}, headers=as_worker
    )
    await settle()
    kept = (await project_client.get(path)).json()
    assert {"key": "approved", "value": third} in kept["labels"]


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


async def test_only_the_owner_picks_the_agent_of_sessions_that_run_as_them(
    multi_user_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A member keeps model and effort, but not the agent or launch permissions."""
    client = multi_user_client
    owner, member = _as_user(ALICE), _as_user(BOB)
    config = {"agent_id": PROJECT_AGENT_ID, "model": "m1", "effort": "low"}
    project = (
        await client.post("/v1/projects", json={"name": "Shared", "config": config}, headers=owner)
    ).json()
    shared = await client.post(
        f"/v1/projects/{project['id']}/shares", json={"user_id": BOB}, headers=owner
    )
    assert shared.status_code == 200, shared.text
    path = f"/v1/projects/{project['id']}"
    for change in (
        {"agent_id": "another-agent"},
        {"permission_mode": "bypassPermissions"},
        {"terminal_launch_args": ["--permission-mode", "bypassPermissions"]},
        {"harness": "codex"},
    ):
        refused = await client.patch(path, json={"config": {**config, **change}}, headers=member)
        assert refused.status_code == 403, refused.text
    kept = await client.patch(
        path, json={"config": {**config, "model": "m2", "effort": "high"}}, headers=member
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["config"]["agent_id"] == PROJECT_AGENT_ID

    # The Lead's agent is the project's default agent.
    lead = f"{path}/identities/orchestrator"
    refused = await client.patch(lead, json={"agent_id": "another-agent"}, headers=member)
    assert refused.status_code == 403, refused.text
    resent = await client.patch(
        lead, json={"agent_id": PROJECT_AGENT_ID, "effort": "medium"}, headers=member
    )
    assert resent.status_code == 200, resent.text

    persona = SqlAlchemyProjectStore(db_uri).create_identity(
        project["id"],
        uuid.uuid4().hex,
        user_id=ALICE,
        name="PM",
        instructions="x",
        agent_id=PROJECT_AGENT_ID,
    )
    persona_path = f"{path}/identities/{persona.id}"
    for agent_id in ("another-agent", ""):
        refused = await client.patch(persona_path, json={"agent_id": agent_id}, headers=member)
        assert refused.status_code == 403, refused.text
    created = await client.post(
        f"{path}/identities", json={"name": "Ops", "agent_id": "another-agent"}, headers=member
    )
    assert created.status_code == 403, created.text

    allowed = await client.patch(
        path, json={"config": {**config, "agent_id": "owner-choice"}}, headers=owner
    )
    assert allowed.status_code == 200, allowed.text


def _bind_runner(db_uri: str, session_id: str, secret: str) -> dict[str, str]:
    """Bind *session_id* to the runner a tunnel *secret* derives; return that runner's headers."""
    SqlAlchemyConversationStore(db_uri).set_runner_id(session_id, token_bound_runner_id(secret))
    return {ORCHESTRATION_SOURCE_HEADER: session_id, RUNNER_TUNNEL_TOKEN_HEADER: secret}


async def test_the_description_is_written_by_the_owner_or_the_lead_only(
    project_client: httpx.AsyncClient, db_uri: str
) -> None:
    """The charter round-trips trimmed; among agents only the Lead's own runner edits it."""
    created = (
        await project_client.post(
            "/v1/projects", json={"name": "P", "description": "  Ship it.  "}
        )
    ).json()
    assert (created["description"], created["mandate"]) == ("Ship it.", None)
    path = f"/v1/projects/{created['id']}"
    lead_id = created["main_session_id"]
    lead = _bind_runner(db_uri, lead_id, "lead-secret")
    written = await project_client.patch(
        path, json={"description": "Mission: docs.\nOut of scope: code."}, headers=lead
    )
    assert written.status_code == 200
    assert written.json()["description"] == "Mission: docs.\nOut of scope: code."
    # A config replace keeps it, and the status summary returns it.
    await project_client.patch(path, json={"config": {"model": "m"}})
    summary = (await project_client.get(f"{path}/status/summary")).json()
    assert summary["description"] == "Mission: docs.\nOut of scope: code."
    persona = (
        await project_client.post(f"{path}/identities", json={"name": "PM", "instructions": "y"})
    ).json()
    worker = _bind_runner(db_uri, persona["active_session_id"], "worker-secret")
    spoofs = {
        "own session": worker,
        # Names the Lead but presents its own runner's token.
        "Lead header, own token": {**worker, ORCHESTRATION_SOURCE_HEADER: lead_id},
        "Lead header, no token": {ORCHESTRATION_SOURCE_HEADER: lead_id},
        # A runner call that leaves out its source header isn't the owner.
        "token, no source": {RUNNER_TUNNEL_TOKEN_HEADER: "worker-secret"},
    }
    for label, headers in spoofs.items():
        refused = await project_client.patch(path, json={"description": "Mine."}, headers=headers)
        assert refused.status_code == 403, label
    refused = await project_client.post(
        "/v1/projects", json={"name": "Q", "description": "x"}, headers=lead
    )
    assert refused.status_code == 403
    # An oversized description is refused before any other field is written.
    too_large = await project_client.patch(
        path, json={"name": "Renamed", "description": "x" * 8_001}
    )
    assert too_large.status_code == 400
    after = (await project_client.get(path)).json()
    assert (after["name"], after["description"]) == ("P", "Mission: docs.\nOut of scope: code.")
    cleared = await project_client.patch(path, json={"description": " "})
    assert cleared.json()["description"] is None


async def test_the_description_follows_the_lead_across_a_rollover(
    project_client: httpx.AsyncClient, db_uri: str
) -> None:
    """After the Lead hands over, its old session can no longer edit; the new one can."""
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    path = f"/v1/projects/{created['id']}"
    old = _bind_runner(db_uri, created["main_session_id"], "old-lead-secret")
    successor = await project_client.post("/v1/sessions", json={"agent_id": PROJECT_AGENT_ID})
    assert successor.status_code == 201, successor.text
    new_id = successor.json()["id"]
    await project_client.patch(f"/v1/sessions/{new_id}", json={"project_id": created["id"]})
    moved = await project_client.patch(
        path,
        json={"main_session_id": new_id, "expected_main_session_id": created["main_session_id"]},
    )
    assert moved.status_code == 200, moved.text
    stale = await project_client.patch(path, json={"description": "Old."}, headers=old)
    assert stale.status_code == 403
    new = _bind_runner(db_uri, new_id, "new-lead-secret")
    fresh = await project_client.patch(path, json={"description": "New."}, headers=new)
    assert fresh.json()["description"] == "New."


async def test_shared_members_read_the_description_but_cannot_edit_it(
    multi_user_client: httpx.AsyncClient,
) -> None:
    created = (
        await multi_user_client.post(
            "/v1/projects",
            json={"name": "Shared", "description": "Mission."},
            headers=_as_user(ALICE),
        )
    ).json()
    path = f"/v1/projects/{created['id']}"
    await multi_user_client.post(f"{path}/shares", json={"user_id": BOB}, headers=_as_user(ALICE))
    read = await multi_user_client.get(path, headers=_as_user(BOB))
    assert (read.json()["description"], read.json()["shared"]) == ("Mission.", True)
    refused = await multi_user_client.patch(
        path, json={"description": "Bob's."}, headers=_as_user(BOB)
    )
    assert refused.status_code == 403
    renamed = await multi_user_client.patch(path, json={"name": "Shared 2"}, headers=_as_user(BOB))
    assert renamed.json()["description"] == "Mission."


async def test_side_chats_of_the_lead_and_a_persona_carry_the_role_but_not_hold_it(
    project_client: httpx.AsyncClient,
) -> None:
    """A role's side chat learns the role and persona; it never becomes the router or identity."""
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    project = f"/v1/projects/{created['id']}"
    await project_client.patch(f"{project}/identities/orchestrator", json={"instructions": "C."})
    pm = (
        await project_client.post(
            f"{project}/identities", json={"name": "PM", "instructions": "Plan."}
        )
    ).json()
    params = {"include_project_mandate": "true"}
    for source, role in (
        (created["main_session_id"], {"id": "orchestrator", "name": "Lead", "instructions": "C."}),
        (pm["active_session_id"], {"id": pm["id"], "name": "PM", "instructions": "Plan."}),
    ):
        fork = await project_client.post(f"/v1/sessions/{source}/fork", json={"side_chat": True})
        assert fork.status_code == 201, fork.text
        side = (
            await project_client.get(f"/v1/sessions/{fork.json()['id']}", params=params)
        ).json()
        assert side["project_side_chat_role"] == role
        assert (side["project_main_session"], side["project_router"]) == (False, False)
        assert (side["project_identity"], side["project_lead_charter"]) == (None, None)
        bare = (await project_client.get(f"/v1/sessions/{fork.json()['id']}")).json()
        assert "instructions" not in bare["project_side_chat_role"]
    plain_fork = await project_client.post(f"/v1/sessions/{pm['active_session_id']}/fork", json={})
    assert plain_fork.status_code == 201, plain_fork.text
    plain = (await project_client.get(f"/v1/sessions/{plain_fork.json()['id']}")).json()
    assert plain["project_side_chat_role"] is None


async def test_the_server_refuses_coordination_from_a_role_side_chat(
    project_client: httpx.AsyncClient,
) -> None:
    """A side chat of the Lead or a persona gets 403 on coordination writes; reads still work."""
    created = (await project_client.post("/v1/projects", json={"name": "P"})).json()
    project = f"/v1/projects/{created['id']}"
    pm = (
        await project_client.post(
            f"{project}/identities", json={"name": "PM", "instructions": "Plan."}
        )
    ).json()
    lead = {ORCHESTRATION_SOURCE_HEADER: created["main_session_id"]}
    ticket = await project_client.post(f"{project}/tickets", json={"title": "T"}, headers=lead)
    assert ticket.status_code == 201, ticket.text
    ticket_path = f"{project}/tickets/{ticket.json()['id']}"
    for source in (created["main_session_id"], pm["active_session_id"]):
        fork = await project_client.post(f"/v1/sessions/{source}/fork", json={"side_chat": True})
        assert fork.status_code == 201, fork.text
        side = {ORCHESTRATION_SOURCE_HEADER: fork.json()["id"]}
        for method, path, body in (
            ("POST", f"{project}/tickets", {"title": "Mine"}),
            ("PATCH", ticket_path, {"stage_id": "done"}),
            ("PATCH", ticket_path, {"labels": [{"key": "kind", "value": "fix"}]}),
            ("PATCH", project, {"name": "Renamed"}),
            ("POST", f"{project}/identities", {"name": "Spy", "instructions": "x"}),
            ("PATCH", f"{project}/identities/{pm['id']}", {"instructions": "x"}),
            ("POST", f"{project}/identities/{pm['id']}/session", {}),
            ("POST", f"{project}/mandate/proposals", {"text": "More."}),
            ("PATCH", f"/v1/sessions/{created['main_session_id']}", {"title": "x"}),
            ("PUT", f"{project}/kpis", {"kpis": []}),
        ):
            refused = await project_client.request(method, path, json=body, headers=side)
            assert refused.status_code == 403, (method, path, refused.text)
            assert "side chat" in refused.text
        commented = await project_client.patch(ticket_path, json={"comment": "FYI."}, headers=side)
        assert commented.status_code == 200, commented.text
        posted = await project_client.post(
            f"{ticket_path}/comments", json={"text": "Note."}, headers=side
        )
        assert posted.status_code == 201, posted.text
        assert (await project_client.get(f"{project}/tickets", headers=side)).status_code == 200
    # The role's main session and people using the UI keep coordinating.
    moved = await project_client.patch(ticket_path, json={"stage_id": "done"}, headers=lead)
    assert moved.status_code == 200, moved.text
    plain = await project_client.post(f"{project}/tickets", json={"title": "By hand"})
    assert plain.status_code == 201, plain.text


async def test_a_manager_invites_members_but_owner_only_settings_stay_with_the_owner(
    multi_user_client: httpx.AsyncClient,
) -> None:
    """The owner promotes a member to Manager and back; a Manager only adds and removes members."""
    client = multi_user_client
    owner, bob = _as_user(ALICE), _as_user(BOB)
    carol, dan = "carol@example.com", "dan@example.com"
    created = (await client.post("/v1/projects", json={"name": "Managed"}, headers=owner)).json()
    path = f"/v1/projects/{created['id']}"
    shares = f"{path}/shares"
    added = await client.post(shares, json={"user_id": BOB}, headers=owner)
    assert added.json()["role"] == "member"

    # A member neither invites nor promotes themselves.
    assert (await client.post(shares, json={"user_id": carol}, headers=bob)).status_code == 403
    selfie = await client.patch(f"{shares}/{BOB}", json={"role": "manager"}, headers=bob)
    assert selfie.status_code == 403

    promoted = await client.patch(f"{shares}/{BOB}", json={"role": "manager"}, headers=owner)
    assert promoted.json()["role"] == "manager", promoted.text
    listed = (await client.get(shares, headers=bob)).json()
    assert listed["viewer_role"] == "manager"
    assert (await client.get(shares, headers=owner)).json()["viewer_role"] == "owner"

    invited = await client.post(shares, json={"user_id": carol}, headers=bob)
    assert (invited.json()["role"], invited.json()["created_by"]) == ("member", BOB)
    assert (await client.get(shares, headers=_as_user(carol))).json()["viewer_role"] == "member"
    for refused in (
        await client.post(shares, json={"user_id": dan, "role": "manager"}, headers=bob),
        await client.patch(f"{shares}/{carol}", json={"role": "manager"}, headers=bob),
        await client.patch(path, json={"config": {"product_approvers": [BOB]}}, headers=bob),
        await client.delete(path, headers=bob),
        # Agents never change access, even in the owner's own sessions.
        await client.post(
            shares, json={"user_id": dan}, headers={**owner, ORCHESTRATION_SOURCE_HEADER: "x"}
        ),
    ):
        assert refused.status_code in (403, 404), refused.text
    assert (await client.get(path, headers=bob)).status_code == 200

    assert (await client.delete(f"{shares}/{carol}", headers=bob)).status_code == 200
    demoted = await client.patch(f"{shares}/{BOB}", json={"role": "member"}, headers=owner)
    assert demoted.json()["role"] == "member"
    assert (await client.post(shares, json={"user_id": dan}, headers=bob)).status_code == 403
