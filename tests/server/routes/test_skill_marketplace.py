"""The skills marketplace routes and the bundle injection, against a fixture repository.

The fetcher's clone is replaced with a copy of a repository on disk, so no
network and no git; everything else (store, artifact store, routes) is real.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.utils import generate_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.skill_injection import (
    effective_installed_skills,
    skill_allowed,
    with_installed_skills,
)
from omnigent.skills.service import SkillFetcher
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.skill_store.sqlalchemy_store import SqlAlchemySkillStore

ALICE = {"X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-Email": "bob@example.com"}

REPO_URL = "https://github.com/acme/skills.git"


def _write_skill(skills_dir: Path, name: str, description: str) -> None:
    skill = skills_dir / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nDo {name}.\n"
    )
    (skill / "notes.txt").write_text("supporting file\n")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write_skill(root / "skills", "review", "Review a diff")
    _write_skill(root / "skills", "triage", "Triage issues")
    return root


def _fetcher(repo: Path) -> SkillFetcher:
    def clone(url: str, ref: str | None, dest: Path, *, token: str | None = None) -> None:
        shutil.copytree(repo, dest)

    return SkillFetcher(clone=clone)


def _build(
    db_uri: str,
    tmp_path: Path,
    *,
    enabled: bool = True,
    auth: bool = False,
    server_config: dict[str, object] | None = None,
) -> tuple[FastAPI, LocalArtifactStore]:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    flags = FeatureFlags(frozenset({Feature.SKILLS_MARKETPLACE}) if enabled else frozenset())
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        skill_store=SqlAlchemySkillStore(db_uri),
        feature_flags=flags,
        permission_store=SqlAlchemyPermissionStore(db_uri) if auth else None,
        auth_provider=UnifiedAuthProvider(source="header") if auth else None,
        server_config=server_config,
    )
    return app, artifact_store


def _bind_session(db_uri: str, artifact_store: LocalArtifactStore, bundle: bytes) -> str:
    """An agent with *bundle* and a session bound to it; returns the session id."""
    agent_id = generate_agent_id()
    artifact_store.put(f"agents/{agent_id}.tar.gz", bundle)
    SqlAlchemyAgentStore(db_uri).create(agent_id, "bundled", f"agents/{agent_id}.tar.gz")
    return SqlAlchemyConversationStore(db_uri).create_conversation(agent_id=agent_id, title="s").id


@pytest.fixture()
async def market(db_uri: str, tmp_path: Path, repo: Path, runtime_init: None):
    app, artifact_store = _build(db_uri, tmp_path)
    app.state.skill_fetcher = _fetcher(repo)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, app, artifact_store


@pytest.mark.asyncio
async def test_sources_catalog_and_install_round_trip(market) -> None:
    client, app, artifact_store = market

    created = await client.post("/v1/skill-sources", json={"source": "acme/skills"})
    assert created.status_code == 201, created.text
    source = created.json()
    assert source["url"] == REPO_URL and source["name"] == "acme/skills"
    listed = (await client.get("/v1/skill-sources")).json()
    assert [s["id"] for s in listed["data"]] == [source["id"]] and listed["has_more"] is False

    catalog = await client.get(f"/v1/skill-sources/{source['id']}/catalog")
    assert catalog.status_code == 200, catalog.text
    assert [
        (s["name"], s["description"], s["installed_id"]) for s in catalog.json()["skills"]
    ] == [
        ("review", "Review a diff", None),
        ("triage", "Triage issues", None),
    ]

    installed = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["triage"]}
    )
    assert installed.status_code == 201, installed.text
    row = installed.json()["data"][0]
    assert (row["name"], row["source"], row["scope"], row["enabled"]) == (
        "triage",
        "acme/skills",
        "user",
        True,
    )
    assert artifact_store.exists(await _location(app, row["id"]))

    # The catalog now marks it, and the listing and content routes see it.
    catalog = (await client.get(f"/v1/skill-sources/{source['id']}/catalog")).json()
    assert {s["name"]: s["installed_id"] for s in catalog["skills"]} == {
        "review": None,
        "triage": row["id"],
    }
    assert [s["name"] for s in (await client.get("/v1/installed-skills")).json()["data"]] == [
        "triage"
    ]
    content = await client.get(f"/v1/installed-skills/{row['id']}/content")
    assert content.json()["markdown"].startswith("---\nname: triage")

    disabled = await client.patch(f"/v1/installed-skills/{row['id']}", json={"enabled": False})
    assert disabled.json()["enabled"] is False
    updated = await client.post(f"/v1/installed-skills/{row['id']}/update")
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == row["id"]

    assert (await client.delete(f"/v1/installed-skills/{row['id']}")).status_code == 204
    assert (await client.get(f"/v1/installed-skills/{row['id']}/content")).status_code == 404
    assert (await client.delete(f"/v1/skill-sources/{source['id']}")).status_code == 204
    assert (await client.delete(f"/v1/skill-sources/{source['id']}")).status_code == 404


async def _location(app: FastAPI, skill_id: str) -> str:
    return app.state.skill_store.get(skill_id).bundle_location


@pytest.mark.asyncio
async def test_preview_from_url_and_bad_sources(market) -> None:
    client, _app, _ = market
    preview = await client.post(
        "/v1/skill-catalog", json={"source": "https://github.com/acme/skills"}
    )
    assert preview.status_code == 200
    assert [s["name"] for s in preview.json()["skills"]] == ["review", "triage"]

    rejected = await client.post(
        "/v1/skill-catalog", json={"source": "https://gitlab.com/acme/skills"}
    )
    assert rejected.status_code == 400
    assert "Only GitHub" in rejected.json()["error"]["message"]

    missing = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["nope"]}
    )
    assert missing.status_code == 400
    assert "No such skill" in missing.json()["error"]["message"]


@pytest.mark.asyncio
async def test_lists_paginate_with_a_continuation_cursor(market) -> None:
    client, _app, _ = market
    installed = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["review", "triage"]}
    )
    assert installed.status_code == 201
    first = (await client.get("/v1/installed-skills?limit=1")).json()
    assert len(first["data"]) == 1 and first["has_more"] is True
    second = (await client.get(f"/v1/installed-skills?limit=1&after={first['last_id']}")).json()
    assert len(second["data"]) == 1 and second["has_more"] is False
    assert {first["data"][0]["name"], second["data"][0]["name"]} == {"review", "triage"}
    assert (await client.get("/v1/installed-skills?after=" + "0" * 32)).status_code == 400
    assert (await client.get("/v1/installed-skills?limit=0")).status_code == 422


@pytest.mark.asyncio
async def test_oversized_metadata_is_rejected_before_anything_is_written(
    db_uri: str, tmp_path: Path, repo: Path, runtime_init: None
) -> None:
    _write_skill(repo / "skills", "wordy", "é" * 1025)
    app, _artifacts = _build(db_uri, tmp_path)
    app.state.skill_fetcher = _fetcher(repo)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # The catalog never offers it, and asking for it by name is refused.
        catalog = (await client.post("/v1/skill-catalog", json={"source": "acme/skills"})).json()
        assert "wordy" not in [s["name"] for s in catalog["skills"]]
        res = await client.post(
            "/v1/installed-skills", json={"source": "acme/skills", "names": ["wordy"]}
        )
        assert res.status_code == 400
        assert (await client.get("/v1/installed-skills")).json()["data"] == []
    assert not list((tmp_path / "artifacts").rglob("*.tar.gz"))


@pytest.mark.asyncio
async def test_feature_off_hides_routes_discovery_and_delivery_for_existing_installs(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    # Install while the feature is on, then boot without it: the rows stay, but
    # nothing reads them.
    store = SqlAlchemySkillStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    artifacts.put("skills/review.tar.gz", _tar({"SKILL.md": b"---\nname: review\n---\n"}))
    store.install(
        name="review",
        display_name=None,
        description="d",
        source_url=REPO_URL,
        ref=None,
        commit=None,
        path="skills/review",
        scope="org",
        owner_user_id=None,
        bundle_location="skills/review.tar.gz",
        installed_by=None,
    )
    session_id = _bind_session(db_uri, artifacts, _tar({"config.yaml": b"name: bundled\n"}))

    for enabled in (True, False):
        app, _ = _build(db_uri, tmp_path, enabled=enabled)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/v1/info")).json()["features"][
                "skills_marketplace"
            ] is enabled
            assert (await client.get("/v1/installed-skills")).status_code == (
                200 if enabled else 404
            )
            contents = await client.get(f"/v1/sessions/{session_id}/agent/contents")
            assert contents.status_code == 200
            assert ("skills/review/SKILL.md" in _names(contents.content)) is enabled
        assert (app.state.skill_store is not None) is enabled


@pytest.mark.asyncio
async def test_served_bundle_route_carries_installed_skills(
    market, db_uri: str, tmp_path: Path
) -> None:
    client, _app, artifact_store = market
    bundle = _tar(
        {"config.yaml": b"name: bundled\n", "skills/own/SKILL.md": b"---\nname: own\n---\n"}
    )
    session_id = _bind_session(db_uri, artifact_store, bundle)
    res = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["triage"]}
    )
    assert res.status_code == 201

    contents = await client.get(f"/v1/sessions/{session_id}/agent/contents")
    assert contents.status_code == 200
    assert _names(contents.content) == [
        "config.yaml",
        "skills/own/SKILL.md",
        "skills/triage/SKILL.md",
        "skills/triage/notes.txt",
    ]


@pytest.mark.asyncio
async def test_authorization_boundaries(
    db_uri: str, tmp_path: Path, repo: Path, runtime_init: None
) -> None:
    app, _ = _build(db_uri, tmp_path, auth=True)
    app.state.skill_fetcher = _fetcher(repo)
    carol = {"X-Forwarded-Email": "carol@example.com"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        SqlAlchemyPermissionStore(db_uri).ensure_user("alice@example.com", is_admin=True)

        # Sources: admin only.
        denied = await client.post(
            "/v1/skill-sources", json={"source": "acme/skills"}, headers=BOB
        )
        assert denied.status_code == 403
        created = await client.post(
            "/v1/skill-sources", json={"source": "acme/skills"}, headers=ALICE
        )
        assert created.status_code == 201
        source_id = created.json()["id"]
        assert (
            await client.delete(f"/v1/skill-sources/{source_id}", headers=BOB)
        ).status_code == 403

        # Org installs: admin only; a user installs for themselves.
        org_body = {"source": "acme/skills", "names": ["review"], "scope": "org"}
        assert (
            await client.post("/v1/installed-skills", json=org_body, headers=BOB)
        ).status_code == 403
        mine = await client.post(
            "/v1/installed-skills",
            json={"source": "acme/skills", "names": ["review"]},
            headers=BOB,
        )
        assert mine.status_code == 201
        bob_skill = mine.json()["data"][0]["id"]
        shared = await client.post(
            "/v1/installed-skills",
            json={"source": "acme/skills", "names": ["triage"], "scope": "org"},
            headers=ALICE,
        )
        assert shared.status_code == 201
        org_skill = shared.json()["data"][0]["id"]

        async def names(headers: dict[str, str]) -> set[str]:
            res = await client.get("/v1/installed-skills", headers=headers)
            return {s["name"] for s in res.json()["data"]}

        # Visibility: own plus the org's; a third user sees the org's only and cannot reach Bob's.
        assert await names(BOB) == {"review", "triage"}
        assert await names(carol) == {"triage"}
        assert (
            await client.get(f"/v1/installed-skills/{bob_skill}/content", headers=carol)
        ).status_code == 404
        patch = {"enabled": False}
        assert (
            await client.patch(f"/v1/installed-skills/{bob_skill}", json=patch, headers=carol)
        ).status_code == 404

        # Mutating an org skill: admin only, even though every user can see it.
        assert (
            await client.patch(f"/v1/installed-skills/{org_skill}", json=patch, headers=BOB)
        ).status_code == 403
        assert (
            await client.post(f"/v1/installed-skills/{org_skill}/update", headers=BOB)
        ).status_code == 403
        assert (
            await client.delete(f"/v1/installed-skills/{org_skill}", headers=BOB)
        ).status_code == 403
        assert (
            await client.patch(f"/v1/installed-skills/{org_skill}", json=patch, headers=ALICE)
        ).status_code == 200
        # An admin may manage another user's install by id.
        assert (
            await client.delete(f"/v1/installed-skills/{bob_skill}", headers=ALICE)
        ).status_code == 204


@pytest.mark.asyncio
async def test_routes_are_absent_when_the_feature_is_off(
    db_uri: str, tmp_path: Path, runtime_init: None
) -> None:
    app, _ = _build(db_uri, tmp_path, enabled=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/v1/installed-skills")).status_code == 404
        info = (await client.get("/v1/info")).json()
        assert info["features"]["skills_marketplace"] is False


# ── bundle injection ──


def _tar(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _names(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        return sorted(tf.getnames())


def test_served_bundle_carries_the_owners_enabled_skills(db_uri: str, tmp_path: Path) -> None:
    store = SqlAlchemySkillStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    bundle = _tar({"config.yaml": b"name: a\n", "skills/own/SKILL.md": b"---\nname: own\n---\n"})

    def install(name: str, owner: str | None, scope: str = "user", enabled: bool = True) -> None:
        artifacts.put(
            f"skills/{name}.tar.gz", _tar({"SKILL.md": f"---\nname: {name}\n---\n".encode()})
        )
        row = store.install(
            name=name,
            display_name=None,
            description=name,
            source_url=REPO_URL,
            ref=None,
            commit=None,
            path=f"skills/{name}",
            scope=scope,
            owner_user_id=owner,
            bundle_location=f"skills/{name}.tar.gz",
            installed_by=owner,
        )
        if not enabled:
            store.set_enabled(row.id, False)

    install("mine", "alice")
    install("shared", None, scope="org")
    install("theirs", "bob")
    install("off", "alice", enabled=False)
    install("own", "alice")  # collides with the bundle's own skill; the bundle wins
    install("deep", "alice")  # collides with a namespaced bundle skill; the bundle wins too
    bundle = _tar(
        {
            "config.yaml": b"name: a\n",
            "skills/own/SKILL.md": b"---\nname: own\n---\n",
            "skills/team/deep/SKILL.md": b"---\nname: deep\n---\n",
        }
    )

    merged = with_installed_skills(
        bundle, owner_id="alice", skill_store=store, artifact_store=artifacts
    )
    assert _names(merged) == [
        "config.yaml",
        "skills/mine/SKILL.md",
        "skills/own/SKILL.md",
        "skills/shared/SKILL.md",
        "skills/team/deep/SKILL.md",
    ]
    # An unreadable tarball drops only that skill.
    artifacts.put("skills/shared.tar.gz", b"not a tarball")
    merged = with_installed_skills(
        bundle, owner_id="alice", skill_store=store, artifact_store=artifacts
    )
    assert "skills/shared/SKILL.md" not in _names(merged) and "skills/mine/SKILL.md" in _names(
        merged
    )


class _Spec:
    def __init__(self, skills_filter):
        self.skills_filter = skills_filter


def test_skill_allowed_honours_the_spec_filter() -> None:
    assert skill_allowed(None, "x")
    assert skill_allowed(_Spec("all"), "x")
    assert not skill_allowed(_Spec("none"), "x")
    assert skill_allowed(_Spec(["x"]), "x") and not skill_allowed(_Spec(["y"]), "x")


@pytest.mark.parametrize("org_first", [True, False])
def test_a_users_install_wins_over_the_orgs_whatever_the_order(
    db_uri: str, org_first: bool
) -> None:
    store = SqlAlchemySkillStore(db_uri)

    def install(scope: str) -> None:
        store.install(
            name="review",
            display_name=None,
            description=f"{scope} copy",
            source_url=REPO_URL,
            ref=None,
            commit=None,
            path="skills/review",
            scope=scope,
            owner_user_id="alice" if scope == "user" else None,
            bundle_location=f"skills/{scope}.tar.gz",
            installed_by="alice",
        )

    for scope in ("org", "user") if org_first else ("user", "org"):
        install(scope)

    effective = effective_installed_skills(store, "alice")
    assert [(s.name, s.scope, s.description) for s in effective] == [
        ("review", "user", "user copy")
    ]
    # Someone without their own copy gets the org's.
    assert [s.scope for s in effective_installed_skills(store, "bob")] == ["org"]


@pytest.mark.asyncio
async def test_server_policy_binds_sources_url_installs_and_delivery(
    db_uri: str, tmp_path: Path, repo: Path, runtime_init: None
) -> None:
    # Single-user mode is all-admin, so every refusal below is one an admin gets.
    config = {
        "skills_marketplace": {
            "sources": [{"url": "https://github.com/acme/skills", "name": "Acme"}],
            "allowed_sources": ["github.com/acme/*"],
            "allow_url_install": False,
        }
    }
    app, artifact_store = _build(db_uri, tmp_path, server_config=config)
    app.state.skill_fetcher = _fetcher(repo)
    session_id = _bind_session(db_uri, artifact_store, _tar({"config.yaml": b"name: bundled\n"}))
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        assert (await client.get("/v1/info")).json()["skills_marketplace"] == {
            "url_install": False
        }

        # The config's source is seeded at startup, labelled, and not removable here.
        listed = (await client.get("/v1/skill-sources")).json()["data"]
        assert [(s["url"], s["name"], s["managed"]) for s in listed] == [(REPO_URL, "Acme", True)]
        source_id = listed[0]["id"]
        assert (await client.delete(f"/v1/skill-sources/{source_id}")).status_code == 403
        assert (await client.get(f"/v1/skill-sources/{source_id}/catalog")).status_code == 200

        # allowed_sources binds the admin too.
        refused = await client.post("/v1/skill-sources", json={"source": "other/skills"})
        assert refused.status_code == 403 and "not an allowed skill source" in refused.text

        # URL installs off: previewing or installing needs a listed source.
        preview = await client.post("/v1/skill-catalog", json={"source": "acme/more"})
        assert preview.status_code == 403 and "disabled on this server" in preview.text
        unlisted = {"source": "acme/more", "names": ["review"]}
        assert (await client.post("/v1/installed-skills", json=unlisted)).status_code == 403
        installed = await client.post(
            "/v1/installed-skills", json={"source": "acme/skills", "names": ["review"]}
        )
        assert installed.status_code == 201, installed.text
        skill_id = installed.json()["data"][0]["id"]
        assert (await client.post(f"/v1/installed-skills/{skill_id}/update")).status_code == 200
        contents = await client.get(f"/v1/sessions/{session_id}/agent/contents")
        assert "skills/review/SKILL.md" in _names(contents.content)

    # Tighten the policy: the row stays, but the skill is neither served nor updatable,
    # and the source, no longer in the config, is an ordinary one an admin may remove.
    tighter = {"skills_marketplace": {"allowed_sources": ["github.com/trusted/*"]}}
    app, _ = _build(db_uri, tmp_path, server_config=tighter)
    app.state.skill_fetcher = _fetcher(repo)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post(f"/v1/installed-skills/{skill_id}/update")).status_code == 403
        assert (await client.get(f"/v1/skill-sources/{source_id}/catalog")).status_code == 403
        contents = await client.get(f"/v1/sessions/{session_id}/agent/contents")
        assert "skills/review/SKILL.md" not in _names(contents.content)
        assert (await client.get("/v1/installed-skills")).json()["data"][0]["id"] == skill_id
        assert (await client.delete(f"/v1/skill-sources/{source_id}")).status_code == 204
