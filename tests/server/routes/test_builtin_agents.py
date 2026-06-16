"""Tests for the builtin agents discovery route (``GET /v1/agents``).

The app fixture does not trigger the lifespan event that seeds
built-in agents, so the test database starts empty. We seed a
test agent directly via the agent_store to verify the endpoint works.
"""

from __future__ import annotations

import io
import re
import tarfile
from pathlib import Path

import httpx
import pytest_asyncio
import yaml

from omnigent.db.utils import generate_agent_id
from omnigent.server.app import _build_polly_codex_bundle
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

_ROOT = Path(__file__).resolve().parents[3]
_POLLY_BUNDLE = _ROOT / "omnigent" / "resources" / "examples" / "polly"


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


def _assert_polly_model_dispatch_requirements(
    files: dict[str, str],
    *,
    expected_name: str,
) -> None:
    """
    Assert Polly-style orchestration docs require explicit agent/model dispatch.

    The runtime ``sys_session_send`` tool keeps ``args.model`` optional for
    general agents, so Polly's stricter behavior lives in its bundled prompt
    and skills. This test catches future examples that regress to relying on
    worker defaults.
    """
    config = yaml.safe_load(files["config.yaml"])
    assert config["name"] == expected_name
    prompt = config["prompt"]
    assert "Every sub-agent spawn MUST explicitly name both the worker and the concrete" in prompt
    assert "Do not rely on worker defaults" in prompt
    assert "choose and record a real `args.model` for each sub-agent" in prompt
    assert "`conversation_id`, `agent`, and `title`" in prompt
    assert "chosen `args.model`" in prompt
    assert "continuation is for the existing `<agent>` session running the recorded" in prompt

    for path, text in files.items():
        if "sys_session_send(" not in text:
            continue
        for match in re.finditer(r"sys_session_send\((?P<example>.*?)\)\s*`", text, re.S):
            example = match.group("example")
            assert "agent=" in example, path
            assert "title=" in example, path
            assert "purpose:" in example, path
            assert "model:" in example, path


def test_polly_rules_require_explicit_agent_and_model_for_subagent_dispatch() -> None:
    """Packaged Polly examples must show explicit agent, title, purpose, and model."""
    files = {
        "config.yaml": (_POLLY_BUNDLE / "config.yaml").read_text(encoding="utf-8"),
        "skills/fanout/SKILL.md": (_POLLY_BUNDLE / "skills" / "fanout" / "SKILL.md").read_text(
            encoding="utf-8"
        ),
        "skills/cross-review/SKILL.md": (
            _POLLY_BUNDLE / "skills" / "cross-review" / "SKILL.md"
        ).read_text(encoding="utf-8"),
        "skills/investigate/SKILL.md": (
            _POLLY_BUNDLE / "skills" / "investigate" / "SKILL.md"
        ).read_text(encoding="utf-8"),
    }

    _assert_polly_model_dispatch_requirements(files, expected_name="polly")


def test_polly_codex_clone_inherits_explicit_agent_and_model_dispatch_rules() -> None:
    """Generated polly-codex bundle must retain Polly's sub-agent model rules."""
    with tarfile.open(fileobj=io.BytesIO(_build_polly_codex_bundle()), mode="r:gz") as tar:
        files = {
            member.name.lstrip("./"): tar.extractfile(member).read().decode("utf-8")
            for member in tar.getmembers()
            if member.isfile()
            and member.name.lstrip("./")
            in {
                "config.yaml",
                "skills/fanout/SKILL.md",
                "skills/cross-review/SKILL.md",
                "skills/investigate/SKILL.md",
            }
        }

    _assert_polly_model_dispatch_requirements(files, expected_name="polly-codex")
