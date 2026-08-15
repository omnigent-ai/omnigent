"""Integration tests for git agent import/refresh routes.

Strategy for host faking
------------------------
Rather than wiring a real WebSocket and simulating full tunnel I/O,
we monkeypatch ``omnigent.server.routes.agents.clone_and_bundle_on_host``
with an async fake that:
  - calls the real ``git_source.clone_and_bundle`` with ``_allow_local=True``
    (bypasses the URL guard that rejects ``file://`` / bare paths),
  - returns a ``ClonedBundle`` with real bytes and a real SHA.

This exercises the full route logic (host-resolution, persistence, error
mapping) while keeping tests deterministic and free of background threads.

A minimal ``HostConnection`` is registered in ``app.state.host_registry``
so ``_require_host_conn`` finds the fake host and returns it — we still
exercise the 409 path when a host is NOT registered.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from omnigent.host.frames import HostHelloFrame
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._host_git_import import (
    ClonedBundle,
    GitImportProxyError,
)

pytestmark = pytest.mark.asyncio

_CONFIG = (
    "spec_version: 1\nname: imported-agent\n"
    "executor:\n  type: omnigent\n  config:\n    harness: claude-sdk\n"
)
_FAKE_HOST_ID = "host_test_fake_001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local_repo(tmp_path: Path) -> str:
    """Create a bare local git repo with an agent config and return its path."""
    repo = tmp_path / "origin"
    repo.mkdir()

    def run(*a: str) -> None:
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    run("init", "-b", "main")
    run("config", "user.email", "t@t.com")
    run("config", "user.name", "t")
    (repo / "config.yaml").write_text(_CONFIG)
    run("add", "-A")
    run("commit", "-m", "init")
    return str(repo)


def _register_fake_host(app) -> None:
    """Register a minimal HostConnection in app.state.host_registry.

    The connection itself is never used for I/O (the monkeypatch intercepts
    the clone call before any frames are sent), but ``_require_host_conn``
    requires that ``host_registry.get(host_id)`` returns a non-None value.
    """
    registry: HostRegistry = app.state.host_registry

    class _FakeWS:
        async def send_text(self, data: str) -> None:
            pass  # never called in tests

        async def receive_text(self) -> str:
            return ""  # never called in tests

    registry.register(
        _FAKE_HOST_ID,
        _FakeWS(),
        HostHelloFrame(version="0.0.0-test", frame_protocol_version=1, name="fake-host"),
        owner=None,
    )


def _make_fake_clone(repo_path: str):
    """Return an async fake for clone_and_bundle_on_host that calls real git locally."""

    async def _fake(*, host_registry, host_conn, git_url, git_ref, git_subpath):
        from omnigent import git_source

        bundle_bytes, sha, resolved_ref = git_source.clone_and_bundle(
            git_url, git_ref, git_subpath, _allow_local=True
        )
        return ClonedBundle(bundle_bytes=bundle_bytes, commit_sha=sha, resolved_ref=resolved_ref)

    return _fake


# ---------------------------------------------------------------------------
# Import tests
# ---------------------------------------------------------------------------


async def test_import_git_happy_path(app, client: httpx.AsyncClient, tmp_path, monkeypatch):
    """Import succeeds, returns git fields including git_host_id."""
    repo = _local_repo(tmp_path)
    _register_fake_host(app)
    monkeypatch.setattr(
        "omnigent.server.routes.agents.clone_and_bundle_on_host",
        _make_fake_clone(repo),
    )

    resp = await client.post(
        "/v1/agents/import-git",
        json={"git_url": repo, "git_ref": "main", "host_id": _FAKE_HOST_ID},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "imported-agent"
    assert body["git_url"] == repo
    assert body["git_ref"] == "main"
    assert len(body["git_commit"]) == 40
    assert body["version"] == 1
    assert body["git_host_id"] == _FAKE_HOST_ID


async def test_import_git_missing_host_id(client: httpx.AsyncClient, tmp_path):
    """Omitting host_id (required Pydantic field) → 422 Unprocessable Entity."""
    resp = await client.post(
        "/v1/agents/import-git",
        json={"git_url": "https://github.com/owner/repo.git"},
    )
    assert resp.status_code == 422


async def test_import_git_unknown_host_id(app, client: httpx.AsyncClient, tmp_path, monkeypatch):
    """A host_id that is not registered → 409 CONFLICT."""
    repo = _local_repo(tmp_path)
    # Do NOT register the host — registry stays empty for this host_id.
    monkeypatch.setattr(
        "omnigent.server.routes.agents.clone_and_bundle_on_host",
        _make_fake_clone(repo),
    )

    resp = await client.post(
        "/v1/agents/import-git",
        json={"git_url": repo, "git_ref": "main", "host_id": "host_does_not_exist"},
    )
    assert resp.status_code == 409


async def test_import_git_bad_url_maps_to_400(app, client: httpx.AsyncClient, monkeypatch):
    """A host-side clone failure (GitImportProxyError) maps to 400 INVALID_INPUT.

    URL validation runs host-side now (inside clone_and_bundle on the host),
    so a bad URL surfaces as GitImportProxyError from the proxy; the route
    must map that to 400 (not 409, which is reserved for host-unavailable).
    """
    _register_fake_host(app)

    async def _raise_proxy(*, host_registry, host_conn, git_url, git_ref, git_subpath):
        raise GitImportProxyError("git clone failed: not a valid git URL")

    monkeypatch.setattr("omnigent.server.routes.agents.clone_and_bundle_on_host", _raise_proxy)

    resp = await client.post(
        "/v1/agents/import-git",
        json={"git_url": "file:///etc/passwd", "host_id": _FAKE_HOST_ID},
    )
    assert resp.status_code == 400, resp.text
    assert "not a valid git URL" in resp.text


# ---------------------------------------------------------------------------
# Helpers for refresh tests
# ---------------------------------------------------------------------------


async def _import(
    app,
    client: httpx.AsyncClient,
    repo: str,
    monkeypatch,
) -> dict:
    """Import a git agent through the API, returning the response body."""
    _register_fake_host(app)
    monkeypatch.setattr(
        "omnigent.server.routes.agents.clone_and_bundle_on_host",
        _make_fake_clone(repo),
    )
    r = await client.post(
        "/v1/agents/import-git",
        json={"git_url": repo, "git_ref": "main", "host_id": _FAKE_HOST_ID},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Agent-naming tests (optional ``name`` override)
# ---------------------------------------------------------------------------


async def test_import_git_name_override_persists(
    app, client: httpx.AsyncClient, tmp_path, monkeypatch
):
    """An explicit ``name`` wins over the repo's own ``name:``."""
    repo = _local_repo(tmp_path)
    _register_fake_host(app)
    monkeypatch.setattr(
        "omnigent.server.routes.agents.clone_and_bundle_on_host",
        _make_fake_clone(repo),
    )

    resp = await client.post(
        "/v1/agents/import-git",
        json={
            "git_url": repo,
            "git_ref": "main",
            "host_id": _FAKE_HOST_ID,
            "name": "myagent-dev",
        },
    )
    assert resp.status_code == 200, resp.text
    # Repo declares 'imported-agent'; the override must win.
    assert resp.json()["name"] == "myagent-dev"


async def test_import_git_blank_name_falls_back_to_spec(
    app, client: httpx.AsyncClient, tmp_path, monkeypatch
):
    """A whitespace-only ``name`` is treated as absent, not as an empty name."""
    repo = _local_repo(tmp_path)
    _register_fake_host(app)
    monkeypatch.setattr(
        "omnigent.server.routes.agents.clone_and_bundle_on_host",
        _make_fake_clone(repo),
    )

    resp = await client.post(
        "/v1/agents/import-git",
        json={"git_url": repo, "git_ref": "main", "host_id": _FAKE_HOST_ID, "name": "   "},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "imported-agent"


async def test_import_same_repo_twice_under_distinct_names(
    app, client: httpx.AsyncClient, tmp_path, monkeypatch
):
    """The headline use case: one repo, two branches, two independent agents.

    Without a name override the second import would 400 on the duplicate-name
    guard (template agent names are unique), so this is what makes per-branch
    agents of the same repo possible.
    """
    repo = _local_repo(tmp_path)
    _register_fake_host(app)
    monkeypatch.setattr(
        "omnigent.server.routes.agents.clone_and_bundle_on_host",
        _make_fake_clone(repo),
    )

    def run(*a: str) -> None:
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    # A second branch of the SAME repo, with the same config.yaml name:.
    run("branch", "dev")

    first = await client.post(
        "/v1/agents/import-git",
        json={
            "git_url": repo,
            "git_ref": "main",
            "host_id": _FAKE_HOST_ID,
            "name": "myagent-main",
        },
    )
    second = await client.post(
        "/v1/agents/import-git",
        json={
            "git_url": repo,
            "git_ref": "dev",
            "host_id": _FAKE_HOST_ID,
            "name": "myagent-dev",
        },
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    a, b = first.json(), second.json()
    assert {a["name"], b["name"]} == {"myagent-main", "myagent-dev"}
    assert a["id"] != b["id"]  # two independent agents
    assert a["git_url"] == b["git_url"] == repo
    assert (a["git_ref"], b["git_ref"]) == ("main", "dev")


async def test_import_git_duplicate_override_name_rejected(
    app, client: httpx.AsyncClient, tmp_path, monkeypatch
):
    """The uniqueness guard still applies to an overridden name."""
    repo = _local_repo(tmp_path)
    _register_fake_host(app)
    monkeypatch.setattr(
        "omnigent.server.routes.agents.clone_and_bundle_on_host",
        _make_fake_clone(repo),
    )

    body = {
        "git_url": repo,
        "git_ref": "main",
        "host_id": _FAKE_HOST_ID,
        "name": "myagent-dev",
    }
    assert (await client.post("/v1/agents/import-git", json=body)).status_code == 200
    dup = await client.post("/v1/agents/import-git", json=body)
    assert dup.status_code == 400, dup.text
    assert "already exists" in dup.text


async def test_refresh_keeps_override_name_when_repo_renames(
    app, client: httpx.AsyncClient, tmp_path, monkeypatch
):
    """Refresh never renames: a repo-side ``name:`` change is ignored.

    The agent's name is its identity (chosen at import), so an upstream rename
    must neither break refresh nor rename the agent.
    """
    repo = _local_repo(tmp_path)
    _register_fake_host(app)
    monkeypatch.setattr(
        "omnigent.server.routes.agents.clone_and_bundle_on_host",
        _make_fake_clone(repo),
    )
    imported = await client.post(
        "/v1/agents/import-git",
        json={
            "git_url": repo,
            "git_ref": "main",
            "host_id": _FAKE_HOST_ID,
            "name": "myagent-dev",
        },
    )
    assert imported.status_code == 200, imported.text
    agent = imported.json()

    # Upstream renames the agent in its config.yaml and adds a commit.
    def run(*a: str) -> None:
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    (Path(repo) / "config.yaml").write_text(_CONFIG.replace("imported-agent", "renamed-upstream"))
    run("add", "-A")
    run("commit", "-m", "rename agent")

    resp = await client.post(f"/v1/agents/{agent['id']}/refresh")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "myagent-dev"  # unchanged by the upstream rename
    assert body["version"] == 2
    assert body["git_commit"] != agent["git_commit"]


# ---------------------------------------------------------------------------
# Refresh tests
# ---------------------------------------------------------------------------


async def test_refresh_bumps_version_on_new_commit(
    app, client: httpx.AsyncClient, tmp_path, monkeypatch
):
    """Refresh re-clones on the same host; version bumps when HEAD advances."""
    repo = _local_repo(tmp_path)
    agent = await _import(app, client, repo, monkeypatch)

    # Add a commit that changes bundle content.
    def run(*a: str) -> None:
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    (Path(repo) / "AGENTS.md").write_text("# new instructions")
    run("add", "-A")
    run("commit", "-m", "update")

    # The fake is already installed by _import; host is still registered.
    resp = await client.post(f"/v1/agents/{agent['id']}/refresh")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 2
    assert body["git_commit"] != agent["git_commit"]


async def test_refresh_idempotent_when_unchanged(
    app, client: httpx.AsyncClient, tmp_path, monkeypatch
):
    """Refresh with no new commits returns the existing agent unchanged."""
    repo = _local_repo(tmp_path)
    agent = await _import(app, client, repo, monkeypatch)

    resp = await client.post(f"/v1/agents/{agent['id']}/refresh")
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 1  # no content change → no bump


async def test_refresh_rejects_missing_agent(client: httpx.AsyncClient):
    """Bogus agent id → 404 NOT_FOUND."""
    resp = await client.post("/v1/agents/ag_does_not_exist/refresh")
    assert resp.status_code == 404


async def test_refresh_rejects_non_git_agent(client: httpx.AsyncClient, db_uri: str):
    """A plain (non-git) agent → 400 INVALID_INPUT."""
    from omnigent.db.utils import generate_agent_id
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    store = SqlAlchemyAgentStore(db_uri)
    agent_id = generate_agent_id()
    store.create(agent_id, "plain-agent", "some/loc")  # no git_url
    resp = await client.post(f"/v1/agents/{agent_id}/refresh")
    assert resp.status_code == 400
    assert "not imported from git" in resp.text


async def test_refresh_rejects_offline_host(app, client: httpx.AsyncClient, tmp_path, monkeypatch):
    """If the stored git_host_id host goes offline, refresh returns 409 CONFLICT."""
    repo = _local_repo(tmp_path)
    agent = await _import(app, client, repo, monkeypatch)

    # Deregister the host to simulate it going offline.
    app.state.host_registry.deregister(_FAKE_HOST_ID)

    resp = await client.post(f"/v1/agents/{agent['id']}/refresh")
    assert resp.status_code == 409
