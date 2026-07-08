"""Tests for ``GET /v1/sessions/{id}/capabilities``.

Exercises the consolidated read-only capabilities endpoint: the four
groups for a top-agent session (skills, mcp config, local tools, declared
sub-agents), context-aware resolution for a named sub-agent child, the
deferred-empty per-server MCP tools, and the ``LEVEL_READ`` / GET-only /
no-write posture. Real-type store stubs — no MagicMock.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from omnigent.entities import Agent, Conversation, ResolvedAccess
from omnigent.errors import OmnigentError
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.server.schemas import SkillSummary
from omnigent.spec import AgentSpec, ExecutorSpec
from omnigent.spec.types import MCPServerConfig, SkillSpec

# ── Specs ────────────────────────────────────────────────────────

# A nested tree: top -> researcher -> note_taker. The researcher declares
# its OWN mcp server + skill so a child-session query can be shown to
# return the researcher's capabilities, not the top agent's.
_NOTE_TAKER = AgentSpec(
    spec_version=1,
    name="note-taker",
    description="Takes notes",
    executor=ExecutorSpec(type="claude_sdk"),
)
_RESEARCHER = AgentSpec(
    spec_version=1,
    name="researcher",
    description="Researches topics",
    executor=ExecutorSpec(type="claude_sdk"),
    skills=[SkillSpec(name="web-search", description="Search the web", content="body")],
    mcp_servers=[
        MCPServerConfig(
            name="arxiv",
            transport="http",
            url="https://arxiv.example/sse",
            description="arXiv MCP",
        )
    ],
    sub_agents=[_NOTE_TAKER],
)
_TOP = AgentSpec(
    spec_version=1,
    name="top",
    description="Top agent",
    executor=ExecutorSpec(type="claude_sdk"),
    skills=[SkillSpec(name="code-review", description="Review code", content="body")],
    mcp_servers=[
        MCPServerConfig(
            name="github",
            transport="http",
            url="https://mcp.example/sse",
            description="GitHub MCP",
        )
    ],
    sub_agents=[_RESEARCHER],
)


# ── Stubs ────────────────────────────────────────────────────────


class _AgentStore:
    """Agent store stub: get by id."""

    def __init__(self, agents: dict[str, Agent]) -> None:
        self._agents = dict(agents)

    def get(self, agent_id: str) -> Agent | None:
        """:returns: The agent if present, else None."""
        return self._agents.get(agent_id)


class _ConversationStore:
    """Conversation store stub. Records any mutating call so a test can
    assert the read-only endpoint never writes."""

    def __init__(self, conversations: dict[str, Conversation]) -> None:
        self._convs = conversations
        self.write_calls: list[str] = []

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """:returns: The conversation if present, else None."""
        return self._convs.get(conversation_id)

    # Any accidental mutation would go through one of these — record it so
    # the no-write test fails loudly rather than silently passing.
    def create_conversation(self, *args: Any, **kwargs: Any) -> None:
        self.write_calls.append("create_conversation")

    def update_conversation(self, *args: Any, **kwargs: Any) -> None:
        self.write_calls.append("update_conversation")

    def set_labels(self, *args: Any, **kwargs: Any) -> None:
        self.write_calls.append("set_labels")


class _LoadedStub:
    """Loaded-bundle stub exposing ``.spec``."""

    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec


class _AgentCacheStub:
    """``get_agent_cache()`` stub mapping agent id -> spec."""

    def __init__(self, spec_by_id: dict[str, AgentSpec]) -> None:
        self._spec_by_id = spec_by_id

    def load(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expand_env: bool = False,
    ) -> _LoadedStub:
        del bundle_location, expand_env
        return _LoadedStub(self._spec_by_id[agent_id])


class _PermissionStore:
    """Minimal permission store: only ``resolve_access`` is exercised for
    top-level conversations. Levels are keyed by ``(user, conv)``."""

    def __init__(self, grants: dict[tuple[str, str], int]) -> None:
        self._grants = grants

    def resolve_access(self, user_id: str | None, conversation_id: str) -> ResolvedAccess:
        level = self._grants.get((user_id or "", conversation_id))
        return ResolvedAccess(
            is_admin=False,
            user_grant_level=level,
            public_grant_level=None,
        )


class _AuthProvider:
    """Auth provider stub: reads the user id from the ``X-User`` header."""

    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-user")


# ── Helpers ──────────────────────────────────────────────────────


def _conv(
    conv_id: str,
    *,
    agent_id: str | None = "ag_top",
    sub_agent_name: str | None = None,
    parent_conversation_id: str | None = None,
) -> Conversation:
    """Build a Conversation entity."""
    return Conversation(
        id=conv_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=parent_conversation_id or conv_id,
        agent_id=agent_id,
        sub_agent_name=sub_agent_name,
        parent_conversation_id=parent_conversation_id,
        kind="sub_agent" if parent_conversation_id else "default",
        title="Session",
    )


def _agent(agent_id: str = "ag_top") -> Agent:
    """Build an Agent entity."""
    return Agent(
        id=agent_id,
        created_at=1,
        name="top",
        bundle_location="bundle/top",
        version=1,
        session_id=None,
    )


def _build_app(
    conv_store: _ConversationStore,
    agent_store: _AgentStore,
    *,
    auth_provider: _AuthProvider | None = None,
    permission_store: _PermissionStore | None = None,
) -> FastAPI:
    """Mount the sessions router with an OmnigentError handler."""
    router = create_sessions_router(
        conversation_store=conv_store,  # type: ignore[arg-type]
        agent_store=agent_store,  # type: ignore[arg-type]
        auth_provider=auth_provider,  # type: ignore[arg-type]
        permission_store=permission_store,  # type: ignore[arg-type]
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(router, prefix="/v1")
    return app


async def _fake_runner_skills(
    runner_client: Any,
    session_id: str,
) -> list[SkillSummary]:
    """Stand in for the runner-owned merged-skills fetch."""
    del runner_client, session_id
    return [SkillSummary(name="merged-skill", description="from runner")]


@pytest.fixture()
def _patched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the spec loader at the in-memory specs and stub the
    runner-owned skills source (no runner in these unit tests)."""
    monkeypatch.setattr(sessions_mod, "get_agent_cache", lambda: _AgentCacheStub({"ag_top": _TOP}))
    monkeypatch.setattr(sessions_mod, "_fetch_runner_skills", _fake_runner_skills)


# ── Tests ────────────────────────────────────────────────────────


def test_top_agent_capabilities_all_four_groups(_patched: None) -> None:
    """A top-agent session returns skills + mcp config + local tools +
    declared sub-agents."""
    conv_store = _ConversationStore({"conv_top": _conv("conv_top")})
    agent_store = _AgentStore({"ag_top": _agent()})
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.get("/v1/sessions/conv_top/capabilities")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "session.capabilities"
    assert body["session_id"] == "conv_top"
    assert body["agent_id"] == "ag_top"
    assert body["sub_agent_name"] is None

    # 1) merged skills come from the runner-owned source.
    assert body["skills"] == [{"name": "merged-skill", "description": "from runner"}]

    # 2) mcp config is the top agent's server (secret-free shape).
    assert [s["name"] for s in body["mcp_servers"]] == ["github"]
    gh = body["mcp_servers"][0]
    assert gh["transport"] == "http"
    assert gh["url"] == "https://mcp.example/sse"

    # 3) local/builtin function tools (name + description), non-empty.
    names = {t["name"] for t in body["local_tools"]}
    assert names, "expected a non-empty local/builtin tool surface"
    assert "load_skill" in names  # skill loader is part of the surface
    assert all("name" in t and "description" in t for t in body["local_tools"])

    # 4) declared sub-agent tree (recursive).
    assert [s["name"] for s in body["sub_agents"]] == ["researcher"]
    assert [s["name"] for s in body["sub_agents"][0]["sub_agents"]] == ["note-taker"]


def test_mcp_per_server_tools_present_but_empty(_patched: None) -> None:
    """Per-server MCP ``tools`` is present but deferred (empty) this slice."""
    conv_store = _ConversationStore({"conv_top": _conv("conv_top")})
    agent_store = _AgentStore({"ag_top": _agent()})
    client = TestClient(_build_app(conv_store, agent_store))

    body = client.get("/v1/sessions/conv_top/capabilities").json()

    assert body["mcp_servers"], "expected at least one mcp server"
    for server in body["mcp_servers"]:
        assert "tools" in server
        assert server["tools"] == []


def test_sub_agent_child_is_context_aware(_patched: None) -> None:
    """The SAME endpoint on a named sub-agent child returns THAT sub-agent's
    capabilities, not the parent's."""
    conv_store = _ConversationStore(
        {
            "conv_top": _conv("conv_top"),
            # Omnigent-spawned child: shares the parent agent_id, identified
            # by sub_agent_name.
            "conv_child": _conv(
                "conv_child",
                agent_id="ag_top",
                sub_agent_name="researcher",
                parent_conversation_id="conv_top",
            ),
        }
    )
    agent_store = _AgentStore({"ag_top": _agent()})
    client = TestClient(_build_app(conv_store, agent_store))

    body = client.get("/v1/sessions/conv_child/capabilities").json()

    assert body["session_id"] == "conv_child"
    assert body["agent_id"] == "ag_top"
    assert body["sub_agent_name"] == "researcher"
    # Researcher's OWN mcp server + declared sub-agent — distinct from top.
    assert [s["name"] for s in body["mcp_servers"]] == ["arxiv"]
    assert [s["name"] for s in body["sub_agents"]] == ["note-taker"]


def test_get_only_rejects_writes(_patched: None) -> None:
    """The route is GET-only: POST/PUT/DELETE return 405 and never write."""
    conv_store = _ConversationStore({"conv_top": _conv("conv_top")})
    agent_store = _AgentStore({"ag_top": _agent()})
    client = TestClient(_build_app(conv_store, agent_store))

    for method in ("post", "put", "delete", "patch"):
        resp = getattr(client, method)("/v1/sessions/conv_top/capabilities")
        assert resp.status_code == 405, f"{method} should be rejected"

    # A GET must not have mutated the conversation store either.
    client.get("/v1/sessions/conv_top/capabilities")
    assert conv_store.write_calls == []


def test_level_read_enforced(_patched: None) -> None:
    """Permission checks gate the read: a read-granted user gets 200, a user
    with no grant is denied."""
    conv_store = _ConversationStore({"conv_top": _conv("conv_top")})
    agent_store = _AgentStore({"ag_top": _agent()})
    # alice has READ (level 1); mallory has no grant.
    perm = _PermissionStore({("alice", "conv_top"): 1})
    app = _build_app(
        conv_store,
        agent_store,
        auth_provider=_AuthProvider(),
        permission_store=perm,
    )
    client = TestClient(app)

    ok = client.get("/v1/sessions/conv_top/capabilities", headers={"X-User": "alice"})
    assert ok.status_code == 200, ok.text

    denied = client.get("/v1/sessions/conv_top/capabilities", headers={"X-User": "mallory"})
    assert denied.status_code == 404, denied.text

    unauthenticated = client.get("/v1/sessions/conv_top/capabilities")
    assert unauthenticated.status_code == 401, unauthenticated.text
