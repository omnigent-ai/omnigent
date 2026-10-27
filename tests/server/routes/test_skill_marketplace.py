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

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.skill_injection import skill_allowed, with_installed_skills
from omnigent.skills.service import SkillFetcher
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.skill_store.sqlalchemy_store import SqlAlchemySkillStore

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
    def clone(url: str, ref: str | None, dest: Path) -> None:
        shutil.copytree(repo, dest)

    return SkillFetcher(clone=clone)


def _build(
    db_uri: str, tmp_path: Path, *, enabled: bool = True
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
    )
    return app, artifact_store


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
    assert [s["id"] for s in (await client.get("/v1/skill-sources")).json()["data"]] == [
        source["id"]
    ]

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

    merged = with_installed_skills(
        bundle, owner_id="alice", skill_store=store, artifact_store=artifacts
    )
    assert _names(merged) == [
        "config.yaml",
        "skills/mine/SKILL.md",
        "skills/own/SKILL.md",
        "skills/shared/SKILL.md",
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
