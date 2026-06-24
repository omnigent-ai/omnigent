"""Integration tests for ``GET /v1/agents`` (built-in agent discovery).

The endpoint is the read-only successor to the removed
``GET /api/agents`` list and the source the new-session picker uses to
discover bindable built-in agents (designs/BUILTIN_AGENTS.md). The
``session_id IS NULL`` exclusion of session-scoped agents lives in
``agent_store.list()`` and is covered by
``tests/stores/test_agent_store.py``; these tests cover the endpoint
wiring and response envelope.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.routes.builtin_agents import create_builtin_agents_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def agent_store(db_uri: str) -> SqlAlchemyAgentStore:
    """Agent store backed by the shared test SQLite db."""
    return SqlAlchemyAgentStore(db_uri)


@pytest.fixture()
def artifact_store(tmp_path: Path) -> LocalArtifactStore:
    """Artifact store for agent bundles, so tests can register a
    built-in agent with a real, loadable bundle."""
    return LocalArtifactStore(str(tmp_path / "artifacts"))


@pytest.fixture()
def agent_cache(artifact_store: LocalArtifactStore, tmp_path: Path) -> AgentCache:
    """Spec cache reading bundles from the test ``artifact_store``."""
    return AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")


def _register_builtin_agent(
    agent_store: SqlAlchemyAgentStore,
    artifact_store: LocalArtifactStore,
    *,
    agent_id: str,
    name: str,
    bundle: bytes,
    description: str | None = None,
) -> None:
    """Store a bundle and register a built-in (``session_id IS NULL``)
    agent pointing at it, mirroring the server's startup seeding.

    :param agent_store: Store the agent row is written to.
    :param artifact_store: Store the bundle bytes are written to.
    :param agent_id: Agent id, e.g. ``"ag_codex"``.
    :param name: Agent name, e.g. ``"codex-native-ui"``.
    :param bundle: Gzipped agent bundle bytes from
        :func:`build_agent_bundle`.
    :param description: Optional free-text description; the catalog
        surfaces it as the picker label.
    """
    bundle_key = f"{agent_id}/{hashlib.sha256(bundle).hexdigest()}"
    artifact_store.put(bundle_key, bundle)
    agent_store.create(agent_id, name, bundle_key, description=description)


@pytest.fixture()
def agents_app(
    agent_store: SqlAlchemyAgentStore,
    agent_cache: AgentCache,
) -> FastAPI:
    """Minimal app mounting only the built-in agents router at ``/v1``."""
    app = FastAPI()
    app.include_router(
        create_builtin_agents_router(agent_store, agent_cache),
        prefix="/v1",
    )
    return app


@pytest_asyncio.fixture()
async def agents_client(agents_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the built-in-agents app."""
    transport = httpx.ASGITransport(app=agents_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_list_builtin_agents_returns_registered_templates(
    agent_store: SqlAlchemyAgentStore,
    agents_client: httpx.AsyncClient,
) -> None:
    """
    ``GET /v1/agents`` surfaces built-in agents registered in the store,
    with their id and name, inside the ``PaginatedList`` envelope.

    This is what the new-session picker reads; if registered built-ins
    don't appear, the picker is empty and no session can be created
    against a built-in. ``mcp_servers`` is empty here because the test
    agents have no real bundle (the spec load fails gracefully).
    """
    agent_store.create("ag_builtin_1", "claude-native-ui", "ag_builtin_1/bundle")
    agent_store.create("ag_builtin_2", "research-agent", "ag_builtin_2/bundle")

    resp = await agents_client.get("/v1/agents")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {a["id"] for a in body["data"]} == {"ag_builtin_1", "ag_builtin_2"}
    assert {a["name"] for a in body["data"]} == {"claude-native-ui", "research-agent"}
    assert body["has_more"] is False
    # No loadable bundle → harness degrades to None rather than failing
    # the list. A non-None value here would mean the route invented a
    # harness for an unreadable spec.
    assert all(a["harness"] is None for a in body["data"])
    # Same degradation for skills: an unreadable spec yields an empty
    # list, not an error and not invented entries.
    assert all(a["skills"] == [] for a in body["data"])


@pytest.mark.parametrize(
    "harness",
    ["codex", "claude-sdk"],
)
async def test_list_builtin_agents_exposes_harness_from_spec(
    agent_store: SqlAlchemyAgentStore,
    artifact_store: LocalArtifactStore,
    agents_client: httpx.AsyncClient,
    harness: str,
) -> None:
    """
    ``GET /v1/agents`` reports each agent's ``harness`` from its spec's
    ``executor.config.harness``.

    The Web UI Add Agent picker uses this to recognise an agent's kind
    (Codex vs Claude) instead of hardcoding by name slug — a
    custom-registered Codex agent must be identifiable as Codex even
    when its name isn't ``codex-native-ui``. The value must reflect the
    actual spec, so the parametrize proves it isn't a hardcoded
    constant.
    """
    bundle = build_agent_bundle(
        name="custom-reviewer",
        executor={"type": "omnigent", "config": {"harness": harness}},
    )
    _register_builtin_agent(
        agent_store,
        artifact_store,
        agent_id="ag_harness",
        name="custom-reviewer",
        bundle=bundle,
    )

    resp = await agents_client.get("/v1/agents")

    assert resp.status_code == 200, resp.text
    entry = next(a for a in resp.json()["data"] if a["id"] == "ag_harness")
    # Proves the spec's harness traversed the load → AgentObject path.
    # A None here means the bundle failed to load; a different value
    # means the route read the wrong spec field.
    assert entry["harness"] == harness


async def test_list_builtin_agents_exposes_model_from_spec(
    agent_store: SqlAlchemyAgentStore,
    artifact_store: LocalArtifactStore,
    agents_client: httpx.AsyncClient,
) -> None:
    """
    ``GET /v1/agents`` reports each agent's resolved default ``model``
    from its spec's ``llm.model``.

    The Web UI Add Agent picker pre-fills this into an editable model
    field and forwards it as ``model_override`` on session create, so a
    custom agent with its own declared model is launched on that model
    by default instead of the harness default. A ``None`` here would
    mean the bundle failed to load (degrading like ``harness``); a
    wrong value would mean the route read the wrong spec field. The
    value mirrors the spec's ``llm.model`` (the parser syncs
    ``executor.model`` into it), so asserting it equals the declared
    model proves the load → AgentObject path.
    """
    bundle = build_agent_bundle(
        name="custom-reviewer",
        executor={"type": "omnigent", "config": {"harness": "codex"}},
    )
    _register_builtin_agent(
        agent_store,
        artifact_store,
        agent_id="ag_model",
        name="custom-reviewer",
        bundle=bundle,
    )

    resp = await agents_client.get("/v1/agents")

    assert resp.status_code == 200, resp.text
    entry = next(a for a in resp.json()["data"] if a["id"] == "ag_model")
    # build_agent_bundle writes llm.model = name, so this proves the
    # spec's llm.model traversed the load → AgentObject path verbatim.
    assert entry["model"] == "custom-reviewer"


async def test_list_builtin_agents_exposes_declared_terminals_from_spec(
    agent_store: SqlAlchemyAgentStore,
    artifact_store: LocalArtifactStore,
    agents_client: httpx.AsyncClient,
) -> None:
    """
    ``GET /v1/agents`` reports each agent's declared ``terminals:`` names.

    The Web UI gates its "new terminal" affordance on this list — a
    dropped field hides creation from every agent with terminal
    access; an invented entry offers creation the server's gate will
    then reject.
    """
    bundle = build_agent_bundle(
        name="terminal-agent",
        terminals={"shell": {"command": "bash"}, "py": {"command": "python3"}},
    )
    _register_builtin_agent(
        agent_store,
        artifact_store,
        agent_id="ag_terminals",
        name="terminal-agent",
        bundle=bundle,
    )

    resp = await agents_client.get("/v1/agents")

    assert resp.status_code == 200, resp.text
    entry = next(a for a in resp.json()["data"] if a["id"] == "ag_terminals")
    # Both declared names in spec order — proves the spec's terminals
    # block traversed the load → AgentObject path verbatim.
    assert entry["terminals"] == ["shell", "py"]


async def test_list_builtin_agents_exposes_bundled_skills_from_spec(
    agent_store: SqlAlchemyAgentStore,
    artifact_store: LocalArtifactStore,
    agents_client: httpx.AsyncClient,
) -> None:
    """
    ``GET /v1/agents`` reports each agent's bundled skills (name +
    description) from its spec's ``skills/<name>/SKILL.md`` entries.

    The Web UI's new-session composer builds its "/" suggestions menu
    from this list — before a session exists there is no runner to
    merge host-discovered skills, so the bundled set is the only
    source. Missing entries here mean the landing menu is empty and a
    first-message skill invocation falls back to plain text. The skill
    ``content`` must NOT be exposed: it can be large and is only
    loaded runner-side at invocation time.
    """
    bundle = build_agent_bundle(
        name="skilled-agent",
        skills=[
            {
                "name": "review-pr",
                "description": "Review a pull request",
                "content": "Fetch the PR and review it.",
            },
            {
                "name": "triage",
                "description": "Triage issues",
                "content": "Ask one question.",
            },
        ],
    )
    _register_builtin_agent(
        agent_store,
        artifact_store,
        agent_id="ag_skilled",
        name="skilled-agent",
        bundle=bundle,
    )

    resp = await agents_client.get("/v1/agents")

    assert resp.status_code == 200, resp.text
    entry = next(a for a in resp.json()["data"] if a["id"] == "ag_skilled")
    # Both bundled skills traversed the load → AgentObject path with the
    # exact name + description the composer menu renders. A missing or
    # renamed entry means the landing "/" menu regressed to empty.
    assert entry["skills"] == [
        {"name": "review-pr", "description": "Review a pull request"},
        {"name": "triage", "description": "Triage issues"},
    ]
    # SkillSummary is the safe subset — the SKILL.md body must not leak
    # into the catalog payload.
    assert all("content" not in s for s in entry["skills"])


async def test_catalog_keeps_custom_agent_distinct_from_builtin_claude_and_codex(
    agent_store: SqlAlchemyAgentStore,
    artifact_store: LocalArtifactStore,
    agents_client: httpx.AsyncClient,
) -> None:
    """
    A custom YAML agent registered as a built-in appears in the catalog
    alongside the Claude and Codex built-ins and stays distinguishable by
    name + harness, with its description surfaced as the picker label.

    This is the custom-agent value prop: bringing your own YAML agent
    (here ``databricks-coding-agent`` on the ``openai-agents`` harness)
    must not collapse into the built-ins. The Add Agent picker keys the
    glyph off harness and the label off name + description, so a custom
    entry that reported a built-in's harness or dropped its name would be
    mis-badged or indistinguishable from Claude/Codex. Registering all
    three in one list and asserting per-id fields proves the route keeps
    them separate rather than, say, reading one shared spec.
    """
    _register_builtin_agent(
        agent_store,
        artifact_store,
        agent_id="ag_claude",
        name="claude-native-ui",
        bundle=build_agent_bundle(
            name="claude-native-ui",
            executor={"type": "omnigent", "config": {"harness": "claude-sdk"}},
        ),
    )
    _register_builtin_agent(
        agent_store,
        artifact_store,
        agent_id="ag_codex",
        name="codex-native-ui",
        bundle=build_agent_bundle(
            name="codex-native-ui",
            executor={"type": "omnigent", "config": {"harness": "codex"}},
        ),
    )
    _register_builtin_agent(
        agent_store,
        artifact_store,
        agent_id="ag_custom",
        name="databricks-coding-agent",
        description="Custom coding agent",
        bundle=build_agent_bundle(
            name="databricks-coding-agent",
            executor={"type": "omnigent", "config": {"harness": "openai-agents"}},
        ),
    )

    resp = await agents_client.get("/v1/agents")

    assert resp.status_code == 200, resp.text
    by_id = {a["id"]: a for a in resp.json()["data"]}
    # All three discoverable in the one catalog the picker reads.
    assert by_id.keys() >= {"ag_claude", "ag_codex", "ag_custom"}
    # Each carries its own harness — the custom agent is neither the
    # Claude nor the Codex kind. A shared/wrong value here is exactly the
    # mis-badging this contract guards against.
    assert by_id["ag_claude"]["harness"] == "claude-sdk"
    assert by_id["ag_codex"]["harness"] == "codex"
    assert by_id["ag_custom"]["harness"] == "openai-agents"
    # The custom agent keeps its registered name and description (the
    # picker's label), distinct from both built-ins.
    assert by_id["ag_custom"]["name"] == "databricks-coding-agent"
    assert by_id["ag_custom"]["description"] == "Custom coding agent"


async def test_list_builtin_agents_empty_when_none_registered(
    agents_client: httpx.AsyncClient,
) -> None:
    """
    With no agents registered, ``GET /v1/agents`` returns an empty list
    (not an error) so the picker can render a clean "no agents" state.
    """
    resp = await agents_client.get("/v1/agents")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == []
    assert body["has_more"] is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "AgentObject exposes no availability / unavailable_reason fields "
        "yet. The Add Agent picker can't grey out an agent that "
        "can't be launched in the current environment, nor explain why. "
        "Flips to a failing XPASS when the catalog grows the metadata — "
        "promote this to a real contract test then."
    ),
)
async def test_catalog_entry_exposes_availability_and_reason(
    agent_store: SqlAlchemyAgentStore,
    artifact_store: LocalArtifactStore,
    agents_client: httpx.AsyncClient,
) -> None:
    """
    Each ``GET /v1/agents`` entry should report whether it is launchable
    plus a reason when it is not.

    Discoverability is only half the Add Agent contract: the picker also
    needs to know which catalog entries it may actually launch for this
    user/environment/session and why a disabled one is disabled. The two
    field names below are the proposed wire contract — strict xfail so the
    suite trips the moment the schema gains them and forces this test to
    be turned into a positive assertion.
    """
    bundle = build_agent_bundle(
        name="codex-reviewer",
        executor={"type": "omnigent", "config": {"harness": "codex"}},
    )
    _register_builtin_agent(
        agent_store,
        artifact_store,
        agent_id="ag_avail",
        name="codex-reviewer",
        bundle=bundle,
    )

    resp = await agents_client.get("/v1/agents")

    assert resp.status_code == 200, resp.text
    entry = next(a for a in resp.json()["data"] if a["id"] == "ag_avail")
    # availability gates whether the picker can launch the entry;
    # unavailable_reason explains a disabled one. Both absent from the
    # AgentObject schema today, so these key lookups fail (the xfail).
    assert "availability" in entry
    assert "unavailable_reason" in entry


async def test_catalog_description_falls_back_to_spec_when_row_unset(
    agent_store: SqlAlchemyAgentStore,
    artifact_store: LocalArtifactStore,
    agents_client: httpx.AsyncClient,
) -> None:
    """
    ``GET /v1/agents`` surfaces the spec's top-level ``description`` when
    the stored agent row has none.

    Single-file YAML built-ins don't persist a description at
    registration today, so the stored column is ``None`` for them. The
    new-session picker shows a hover description, and without this
    fallback those agents would hover blank. Registering with
    ``description=None`` but a spec that declares one proves the route
    reads through to the bundle rather than echoing the empty column.
    """
    bundle = build_agent_bundle(
        name="hoverable-agent",
        description="Planned and split across sub-agents.",
    )
    _register_builtin_agent(
        agent_store,
        artifact_store,
        agent_id="ag_specdesc",
        name="hoverable-agent",
        bundle=bundle,
        description=None,
    )

    resp = await agents_client.get("/v1/agents")

    assert resp.status_code == 200, resp.text
    entry = next(a for a in resp.json()["data"] if a["id"] == "ag_specdesc")
    # The stored column is None, so a non-None value here can only have
    # come from the spec via the load → AgentObject fallback path.
    assert entry["description"] == "Planned and split across sub-agents."


async def test_catalog_description_prefers_stored_row_over_spec(
    agent_store: SqlAlchemyAgentStore,
    artifact_store: LocalArtifactStore,
    agents_client: httpx.AsyncClient,
) -> None:
    """
    ``GET /v1/agents`` prefers the stored row's ``description`` over the
    spec's when both are set.

    The fallback to the spec must be exactly that — a fallback. An
    operator who set a description on the row (e.g. a curated catalog
    label) must not have it silently overridden by whatever the bundled
    spec happens to say. Registering with a stored description that
    differs from the spec's proves the stored value wins.
    """
    bundle = build_agent_bundle(
        name="relabelled-agent",
        description="Spec description (should be ignored).",
    )
    _register_builtin_agent(
        agent_store,
        artifact_store,
        agent_id="ag_storeddesc",
        name="relabelled-agent",
        bundle=bundle,
        description="Curated catalog label.",
    )

    resp = await agents_client.get("/v1/agents")

    assert resp.status_code == 200, resp.text
    entry = next(a for a in resp.json()["data"] if a["id"] == "ag_storeddesc")
    # Stored value present → it wins; the differing spec description
    # proves the route didn't blindly overwrite with the bundle's.
    assert entry["description"] == "Curated catalog label."
