"""Tests for the builtin agents discovery route (``GET /v1/agents``).

The app fixture does not trigger the lifespan event that seeds
built-in agents, so the test database starts empty. We seed a
test agent directly via the agent_store to verify the endpoint works.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import httpx
import pytest_asyncio
import yaml

from omnigent.db.utils import builtin_agent_id, generate_agent_id
from omnigent.server.routes.builtin_agents import _resolve_icon_file
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore


@pytest_asyncio.fixture()
async def _seeded_agent(db_uri: str) -> str:
    """Seed a built-in (session_id=None) agent and return its ID."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name="test-builtin", bundle_location="test:///bundle")
    return agent_id


async def test_list_builtin_agents_empty(client: httpx.AsyncClient) -> None:
    """GET /v1/agents with no agents returns an empty paginated list."""
    resp = await client.get("/v1/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert isinstance(body["data"], list)
    assert "has_more" in body


async def test_list_builtin_agents_with_limit(client: httpx.AsyncClient) -> None:
    """Limit parameter constrains the result size."""
    resp = await client.get("/v1/agents?limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) <= 1


async def test_list_builtin_agents_seeded(
    client: httpx.AsyncClient,
    _seeded_agent: str,
) -> None:
    """A seeded agent appears in the list."""
    resp = await client.get("/v1/agents?limit=100")
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()["data"]]
    assert _seeded_agent in ids


async def test_list_builtin_agents_response_shape(
    client: httpx.AsyncClient,
    _seeded_agent: str,
) -> None:
    """Each agent object has the expected fields."""
    resp = await client.get("/v1/agents?limit=100")
    assert resp.status_code == 200
    for agent in resp.json()["data"]:
        assert "id" in agent
        assert "name" in agent
        assert "created_at" in agent


async def test_builtin_flag_distinguishes_seeded_from_registered(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``builtin`` is True only for a server-seeded agent (deterministic,
    name-derived id); an operator/user-registered template (random id)
    reports False. The Web UI picker keys its supersession policy on this:
    a same-named upload may shadow a registered template but never a seeded
    built-in.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    seeded_id = builtin_agent_id("polly")
    agent_store.create(seeded_id, name="polly", bundle_location="test:///polly")
    registered_id = generate_agent_id()
    agent_store.create(registered_id, name="my-agent", bundle_location="test:///mine")

    resp = await client.get("/v1/agents?limit=100")
    assert resp.status_code == 200
    by_id = {a["id"]: a for a in resp.json()["data"]}
    assert by_id[seeded_id]["builtin"] is True
    assert by_id[registered_id]["builtin"] is False


# ── Icon payload + read-only icon endpoint ─────────────────────

_SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'


def _make_bundle(files: dict[str, bytes]) -> bytes:
    """Pack ``{archive_path: content}`` into a ``.tar.gz`` bundle."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _seed_agent_with_bundle(
    db_uri: str,
    tmp_path: Path,
    *,
    name: str,
    config: dict[str, object],
    extra_files: dict[str, bytes] | None = None,
) -> str:
    """Seed a built-in agent whose bundle the app's cache can load.

    The bundle is written to the same on-disk artifact root the ``app``
    fixture wires up (``tmp_path / "artifacts"``), so the app's own
    ``AgentCache`` downloads and extracts it on demand.
    """
    agent_id = generate_agent_id()
    files: dict[str, bytes] = {"config.yaml": yaml.dump(config).encode()}
    files.update(extra_files or {})
    bundle_location = f"{agent_id}/bundle.tar.gz"
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    artifact_store.put(bundle_location, _make_bundle(files))
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_store.create(agent_id, name=name, bundle_location=bundle_location)
    return agent_id


def _min_config(name: str, **extra: object) -> dict[str, object]:
    """A minimal loadable omnigent agent config, plus any extra keys."""
    return {
        "spec_version": 1,
        "name": name,
        "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
        "prompt": "hi",
        **extra,
    }


async def test_payload_includes_emoji_icon(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """An emoji ``icon`` in the spec passes through to the payload verbatim."""
    agent_id = _seed_agent_with_bundle(
        db_uri, tmp_path, name="emoji-agent", config=_min_config("emoji-agent", icon="🔥")
    )
    resp = await client.get("/v1/agents?limit=100")
    assert resp.status_code == 200
    by_id = {a["id"]: a for a in resp.json()["data"]}
    assert by_id[agent_id]["icon"] == "🔥"


async def test_payload_icon_none_when_unset(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """An agent with no ``icon`` reports ``icon: null`` (client uses default)."""
    agent_id = _seed_agent_with_bundle(
        db_uri, tmp_path, name="plain-agent", config=_min_config("plain-agent")
    )
    resp = await client.get("/v1/agents?limit=100")
    assert resp.status_code == 200
    by_id = {a["id"]: a for a in resp.json()["data"]}
    assert by_id[agent_id]["icon"] is None


async def test_icon_endpoint_serves_svg(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """A path-like icon streams the file bytes with the svg media type."""
    agent_id = _seed_agent_with_bundle(
        db_uri,
        tmp_path,
        name="svg-agent",
        config=_min_config("svg-agent", icon="brand/logo.svg"),
        extra_files={"brand/logo.svg": _SVG_BYTES},
    )
    resp = await client.get(f"/v1/agents/{agent_id}/icon")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert resp.content == _SVG_BYTES


async def test_icon_endpoint_404_for_no_icon(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """An agent without an icon has no file to serve — 404."""
    agent_id = _seed_agent_with_bundle(
        db_uri, tmp_path, name="noicon-agent", config=_min_config("noicon-agent")
    )
    resp = await client.get(f"/v1/agents/{agent_id}/icon")
    assert resp.status_code == 404


async def test_icon_endpoint_404_for_emoji_icon(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """An emoji icon has no backing file — the endpoint returns 404."""
    agent_id = _seed_agent_with_bundle(
        db_uri, tmp_path, name="emoji2-agent", config=_min_config("emoji2-agent", icon="🔥")
    )
    resp = await client.get(f"/v1/agents/{agent_id}/icon")
    assert resp.status_code == 404


async def test_icon_endpoint_404_for_unknown_agent(client: httpx.AsyncClient) -> None:
    """An unknown agent id yields 404, not a 500."""
    resp = await client.get("/v1/agents/ag_does_not_exist/icon")
    assert resp.status_code == 404


def test_resolve_icon_file_returns_contained_path(tmp_path: Path) -> None:
    """A clean relative icon resolves to the file under the agent dir."""
    (tmp_path / "brand").mkdir()
    icon = tmp_path / "brand" / "logo.svg"
    icon.write_bytes(_SVG_BYTES)
    assert _resolve_icon_file(tmp_path, "brand/logo.svg") == icon.resolve()


def test_resolve_icon_file_rejects_missing_file(tmp_path: Path) -> None:
    """A path with no file on disk resolves to None."""
    assert _resolve_icon_file(tmp_path, "brand/logo.svg") is None


def test_resolve_icon_file_rejects_traversal(tmp_path: Path) -> None:
    """Parent-dir escapes are rejected even if the target exists."""
    secret = tmp_path / "secret.svg"
    secret.write_bytes(b"secret")
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    assert _resolve_icon_file(agent_dir, "../secret.svg") is None


def test_resolve_icon_file_rejects_absolute(tmp_path: Path) -> None:
    """An absolute icon path is rejected."""
    secret = tmp_path / "secret.svg"
    secret.write_bytes(b"secret")
    assert _resolve_icon_file(tmp_path, str(secret)) is None


def test_resolve_icon_file_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink inside the agent dir pointing outside is rejected."""
    secret = tmp_path / "secret.svg"
    secret.write_bytes(b"secret")
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "evil.svg").symlink_to(secret)
    assert _resolve_icon_file(agent_dir, "evil.svg") is None
