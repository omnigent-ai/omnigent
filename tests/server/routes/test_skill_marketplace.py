"""The skills marketplace routes and the bundle injection, against a fixture repository.

The fetcher's clone is replaced with a copy of a repository on disk, so no
network and no git; everything else (store, artifact store, routes) is real.
"""

from __future__ import annotations

import io
import tarfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.utils import generate_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.skill_injection import effective_installed_skills, with_installed_skills
from omnigent.skills.service import SkillFetcher
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.skill_store.sqlalchemy_store import SqlAlchemySkillStore
from tests._helpers.skill_repos import copying_cloner, skill_tarball, write_skill

ALICE = {"X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-Email": "bob@example.com"}

REPO_URL = "https://github.com/acme/skills.git"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    write_skill(root / "skills", "review", "Review a diff")
    write_skill(root / "skills", "triage", "Triage issues")
    return root


def _fetcher(repo: Path) -> SkillFetcher:
    return SkillFetcher(clone=copying_cloner(repo))


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
    app.state.skill_fetcher_repo = repo
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
    first_location = await _location(app, row["id"])
    assert artifact_store.exists(first_location)

    # Reinstalling changed content keeps the id, stores the new object and drops the old one.
    repo_dir = app.state.skill_fetcher_repo
    (repo_dir / "skills" / "triage" / "notes.txt").write_text("changed\n")
    again = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["triage"]}
    )
    assert again.status_code == 201 and again.json()["data"][0]["id"] == row["id"]
    second_location = await _location(app, row["id"])
    assert second_location != first_location
    assert artifact_store.exists(second_location) and not artifact_store.exists(first_location)

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
    # The cursor row removed between the two reads: the next page still follows.
    assert (
        await client.delete(f"/v1/installed-skills/{first['data'][0]['id']}")
    ).status_code == 204
    again = (await client.get(f"/v1/installed-skills?limit=1&after={first['last_id']}")).json()
    assert [s["name"] for s in again["data"]] == [second["data"][0]["name"]]
    assert (await client.get("/v1/installed-skills?after=" + "0" * 32)).status_code == 400
    assert (await client.get("/v1/installed-skills?limit=0")).status_code == 422


@pytest.mark.asyncio
async def test_update_cleans_up_after_a_removal_or_another_update_wins(market) -> None:
    client, app, artifact_store = market
    store = app.state.skill_store
    installed = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["triage"]}
    )
    skill_id = installed.json()["data"][0]["id"]
    first_location = await _location(app, skill_id)
    real_update = store.update
    stored: list[str] = []
    real_put = artifact_store.put

    def recording_put(key: str, data: bytes) -> None:
        stored.append(key)
        real_put(key, data)

    artifact_store.put = recording_put  # type: ignore[method-assign]

    # Another request moved the row's tarball while this update was fetching:
    # the object replaced under the lock is dropped, not the one read earlier.
    def update_after_a_rival(skill: str, **kwargs):  # type: ignore[no-untyped-def]
        rival = real_update(
            skill,
            commit=None,
            bundle_location="skills/rival.tar.gz",
            bundle_bytes=1,
            description="d",
            display_name=None,
        )
        assert rival is not None and rival.replaced_location == first_location
        artifact_store.put("skills/rival.tar.gz", b"rival")
        return real_update(skill, **kwargs)

    store.update = update_after_a_rival
    (app.state.skill_fetcher_repo / "skills" / "triage" / "notes.txt").write_text("v2\n")
    updated = await client.post(f"/v1/installed-skills/{skill_id}/update")
    assert updated.status_code == 200, updated.text
    final_location = await _location(app, skill_id)
    assert final_location not in {first_location, "skills/rival.tar.gz"}
    assert not artifact_store.exists("skills/rival.tar.gz") and artifact_store.exists(
        final_location
    )

    # The row was removed while this update was fetching: 404, and the object
    # the request had already stored is not left behind.
    def update_after_a_removal(skill: str, **kwargs):  # type: ignore[no-untyped-def]
        assert store.delete(skill) is not None
        return real_update(skill, **kwargs)

    store.update = update_after_a_removal
    (app.state.skill_fetcher_repo / "skills" / "triage" / "notes.txt").write_text("v3\n")
    stored.clear()
    assert (await client.post(f"/v1/installed-skills/{skill_id}/update")).status_code == 404
    assert len(stored) == 1 and not artifact_store.exists(stored[0])


@pytest.mark.asyncio
async def test_content_names_a_missing_or_unreadable_stored_copy(market) -> None:
    client, app, artifact_store = market
    installed = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["review"]}
    )
    skill_id = installed.json()["data"][0]["id"]
    location = await _location(app, skill_id)

    artifact_store.put(location, b"not a tarball")
    unreadable = await client.get(f"/v1/installed-skills/{skill_id}/content")
    assert unreadable.status_code == 409
    assert "cannot be read" in unreadable.json()["error"]["message"]

    artifact_store.delete(location)
    missing = await client.get(f"/v1/installed-skills/{skill_id}/content")
    assert missing.status_code == 404
    assert "update or remove" in missing.json()["error"]["message"]


@pytest.mark.asyncio
async def test_oversized_metadata_is_rejected_before_anything_is_written(
    db_uri: str, tmp_path: Path, repo: Path, runtime_init: None
) -> None:
    write_skill(repo / "skills", "wordy", "é" * 1025)
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
    artifacts.put(
        "skills/review.tar.gz",
        skill_tarball(
            {"SKILL.md": b"---\nname: review\n---\n", "notes.txt": b"supporting file\n"}
        ),
    )
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
        bundle_bytes=1,
        installed_by=None,
    )
    bundle = skill_tarball(
        {"config.yaml": b"name: bundled\n", "skills/own/SKILL.md": b"---\nname: own\n---\n"}
    )
    session_id = _bind_session(db_uri, artifacts, bundle)

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
            # With the feature on, the real agent-contents route carries the
            # installed skill with its supporting file next to the bundle's own.
            assert _names(contents.content) == (
                [
                    "config.yaml",
                    "skills/own/SKILL.md",
                    "skills/review/SKILL.md",
                    "skills/review/notes.txt",
                ]
                if enabled
                else ["config.yaml", "skills/own/SKILL.md"]
            )
        assert (app.state.skill_store is not None) is enabled


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
async def test_installs_are_capped_per_owner_and_delivery_is_bounded(
    market, db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server import skill_injection
    from omnigent.server.routes import skill_marketplace
    from omnigent.stores.skill_store import sqlalchemy_store

    client, _app, artifact_store = market
    monkeypatch.setattr(skill_marketplace, "MAX_INSTALLED_PER_OWNER", 1)
    monkeypatch.setattr(sqlalchemy_store, "MAX_INSTALLED_PER_OWNER", 1)
    first = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["review"]}
    )
    assert first.status_code == 201
    # Reinstalling the same name replaces it and does not count; a new name is over the cap.
    assert (
        await client.post(
            "/v1/installed-skills", json={"source": "acme/skills", "names": ["review"]}
        )
    ).status_code == 201
    capped = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["triage"]}
    )
    assert capped.status_code == 400 and "At most 1 skills" in capped.text
    # A batch is all or nothing: one name over the cap installs neither.
    assert len(list((tmp_path / "artifacts").rglob("*.tar.gz"))) == 1
    assert [s["name"] for s in (await client.get("/v1/installed-skills")).json()["data"]] == [
        "review"
    ]

    # The byte budget is enforced when a set would become undeliverable, at
    # install, update and enable time, so a later session launch cannot fail.
    monkeypatch.setattr(skill_marketplace, "MAX_INSTALLED_PER_OWNER", 10)
    monkeypatch.setattr(sqlalchemy_store, "MAX_INSTALLED_PER_OWNER", 10)
    review = first.json()["data"][0]
    # Room for one tarball of about this size (the two fixtures differ by a few
    # bytes of gzip output), never for two.
    budget = review["bundle_bytes"] + 64
    monkeypatch.setattr(sqlalchemy_store, "MAX_DELIVERY_BYTES_PER_SCOPE", budget)
    over = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["triage"]}
    )
    assert over.status_code == 400 and "would exceed" in over.text
    assert len(list((tmp_path / "artifacts").rglob("*.tar.gz"))) == 1
    # Disabling frees the budget; enabling again must fit.
    disabled = await client.patch(f"/v1/installed-skills/{review['id']}", json={"enabled": False})
    assert disabled.status_code == 200
    triage = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["triage"]}
    )
    assert triage.status_code == 201
    enable = await client.patch(f"/v1/installed-skills/{review['id']}", json={"enabled": True})
    assert enable.status_code == 400 and "Enabling review" in enable.text
    update = await client.post(f"/v1/installed-skills/{review['id']}/update")
    assert update.status_code == 200  # the disabled row's size is not counted

    # Delivery keeps a backstop: exceeding it is an error the caller sees
    # rather than a skill silently missing from the bundle.
    session_id = _bind_session(
        db_uri, artifact_store, skill_tarball({"config.yaml": b"name: bundled\n"})
    )
    assert "skills/triage/SKILL.md" in _names(
        (await client.get(f"/v1/sessions/{session_id}/agent/contents")).content
    )
    monkeypatch.setattr(skill_injection, "MAX_DELIVERY_BYTES", 10)
    over = await client.get(f"/v1/sessions/{session_id}/agent/contents")
    assert over.status_code == 400 and "Installed skills exceed" in over.text


@pytest.mark.asyncio
async def test_a_fetch_in_flight_refuses_a_second_from_the_same_account(market) -> None:
    import asyncio

    client, app, _ = market
    started = asyncio.Event()
    release = asyncio.Event()
    real_package = app.state.skill_fetcher.package

    def slow_package(*args, **kwargs):  # type: ignore[no-untyped-def]
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
        return real_package(*args, **kwargs)

    loop = asyncio.get_running_loop()
    app.state.skill_fetcher.package = slow_package
    first = asyncio.create_task(
        client.post("/v1/installed-skills", json={"source": "acme/skills", "names": ["review"]})
    )
    await asyncio.wait_for(started.wait(), 5)
    second = await client.post(
        "/v1/installed-skills", json={"source": "acme/skills", "names": ["triage"]}
    )
    assert second.status_code == 409 and "still running" in second.text
    release.set()
    assert (await first).status_code == 201


def test_effective_skills_read_every_page_and_stop_at_the_delivery_limit(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server import skill_injection
    from omnigent.stores.skill_store import SkillInstall

    store = SqlAlchemySkillStore(db_uri)
    monkeypatch.setattr(skill_injection, "MAX_PAGE", 2)
    monkeypatch.setattr(skill_injection, "MAX_DELIVERED_SKILLS", 5)
    items = [
        SkillInstall(
            name=f"s{i}",
            display_name=None,
            description="d",
            source_url=REPO_URL,
            ref=None,
            commit=None,
            path=f"skills/s{i}",
            bundle_location=f"skills/s{i}.tar.gz",
            bundle_bytes=1,
        )
        for i in range(5)
    ]
    store.install_many(items, scope="org", owner_user_id=None, installed_by=None)
    # Five rows across three pages of two: every one is returned.
    assert sorted(s.name for s in effective_installed_skills(store, "alice")) == [
        f"s{i}" for i in range(5)
    ]
    # The last row of page one is removed before page two is read, as a
    # concurrent removal would: the walk still reaches every remaining row.
    real_list = store.list_for_user
    removed: list[str] = []

    def list_then_remove_the_cursor_row(user_id, *, limit, after=None):  # type: ignore[no-untyped-def]
        page = real_list(user_id, limit=limit, after=after)
        if after is None:
            removed.append(page.data[-1].name)
            assert store.delete(page.data[-1].id) is not None
        return page

    store.list_for_user = list_then_remove_the_cursor_row  # type: ignore[method-assign]
    delivered = sorted(s.name for s in effective_installed_skills(store, "alice"))
    del store.list_for_user
    assert len(removed) == 1 and delivered == [f"s{i}" for i in range(5)]
    # One row past the delivery limit raises instead of silently stopping.
    store.install_many(
        [
            replace(
                items[0], name=name, path=f"skills/{name}", bundle_location=f"skills/{name}.tar.gz"
            )
            for name in (removed[0], "s5")
        ],
        scope="org",
        owner_user_id=None,
        installed_by=None,
    )
    with pytest.raises(skill_injection.SkillDeliveryError, match="More than 5"):
        effective_installed_skills(store, "alice")


# ── bundle injection ──


def _names(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        return sorted(tf.getnames())


def test_served_bundle_carries_the_owners_enabled_skills(db_uri: str, tmp_path: Path) -> None:
    store = SqlAlchemySkillStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    bundle = skill_tarball(
        {"config.yaml": b"name: a\n", "skills/own/SKILL.md": b"---\nname: own\n---\n"}
    )

    def install(name: str, owner: str | None, scope: str = "user", enabled: bool = True) -> None:
        artifacts.put(
            f"skills/{name}.tar.gz",
            skill_tarball({"SKILL.md": f"---\nname: {name}\n---\n".encode()}),
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
            bundle_bytes=1,
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
    bundle = skill_tarball(
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
            bundle_bytes=1,
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
    session_id = _bind_session(
        db_uri, artifact_store, skill_tarball({"config.yaml": b"name: bundled\n"})
    )
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
