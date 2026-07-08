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
from omnigent.server.schemas import SkillCapability
from omnigent.spec import AgentSpec, ExecutorSpec
from omnigent.spec.types import (
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    MCPServerConfig,
    Phase,
    PhaseSelector,
    SkillSpec,
)

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


async def _fake_runner_skill_capabilities(
    runner_client: Any,
    session_id: str,
) -> list[SkillCapability]:
    """Stand in for the runner-owned enriched-skills fetch."""
    del runner_client, session_id
    return [
        SkillCapability(
            name="merged-skill",
            description="from runner",
            source="workspace",
            in_scope=True,
        )
    ]


async def _no_blocked_skills(
    spec: Any,
    session_id: str,
    conversation_store: Any,
    skill_names: list[str],
    *,
    user_id: str | None,
) -> dict[str, bool]:
    """Stand in for the policy would-block check (nothing blocked)."""
    del spec, session_id, conversation_store, skill_names, user_id
    return {}


@pytest.fixture()
def _patched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the spec loader at the in-memory specs and stub the
    runner-owned skills source + policy block check (no runner / policy
    infra in these unit tests)."""
    monkeypatch.setattr(sessions_mod, "get_agent_cache", lambda: _AgentCacheStub({"ag_top": _TOP}))
    monkeypatch.setattr(
        sessions_mod, "_fetch_runner_skill_capabilities", _fake_runner_skill_capabilities
    )
    monkeypatch.setattr(sessions_mod, "_compute_blocked_skills", _no_blocked_skills)


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

    # 1) merged skills come from the runner-owned source, enriched with
    #    provenance (source), an in_scope flag, and a policy blocked flag.
    assert body["skills"] == [
        {
            "name": "merged-skill",
            "description": "from runner",
            "source": "workspace",
            "in_scope": True,
            "blocked": False,
        }
    ]

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


def test_skills_degrade_to_empty_when_runner_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing/unreachable runner yields an empty skills list, not a 500."""
    monkeypatch.setattr(sessions_mod, "get_agent_cache", lambda: _AgentCacheStub({"ag_top": _TOP}))
    monkeypatch.setattr(sessions_mod, "_compute_blocked_skills", _no_blocked_skills)

    async def _no_skills(runner_client: Any, session_id: str) -> list[SkillCapability]:
        del runner_client, session_id
        return []

    monkeypatch.setattr(sessions_mod, "_fetch_runner_skill_capabilities", _no_skills)

    conv_store = _ConversationStore({"conv_top": _conv("conv_top")})
    agent_store = _AgentStore({"ag_top": _agent()})
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.get("/v1/sessions/conv_top/capabilities")

    assert resp.status_code == 200, resp.text
    # Graceful degrade: skills empty, but the other groups still resolve.
    assert resp.json()["skills"] == []
    assert [s["name"] for s in resp.json()["mcp_servers"]] == ["github"]


@pytest.mark.asyncio
async def test_compute_blocked_skills_flags_denied_names(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skill denied by a session ``block_skills`` policy is flagged
    ``blocked=True``; a non-denied one is ``blocked=False``.

    Exercises the real read-only would-block path: a policy engine built
    from a spec carrying a ``block_skills`` guardrail, evaluated against a
    synthetic native ``Skill`` tool-call per name (``read_only=True`` — no
    persistence, no ASK gate).
    """
    from omnigent.runtime.policies.builder import build_policy_engine
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    spec = AgentSpec(
        spec_version=1,
        name="guarded",
        description="Has a block_skills policy",
        executor=ExecutorSpec(type="claude_sdk"),
        guardrails=GuardrailsSpec(
            policies=[
                FunctionPolicySpec(
                    name="block",
                    on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
                    function=FunctionRef(
                        path="omnigent.policies.builtins.safety.block_skills",
                        arguments={"blocked": ["blocked-skill"]},
                    ),
                )
            ]
        ),
    )
    # Bypass get_caps()/get_policy_store(): build the engine straight from
    # the spec so the test needs no server-cap wiring.
    monkeypatch.setattr(
        sessions_mod,
        "_build_policy_engine_from_spec",
        lambda sp, sid, cs: build_policy_engine(
            spec=sp, conversation_id=sid, conversation_store=cs
        ),
    )

    blocked = await sessions_mod._compute_blocked_skills(
        spec,
        "conv_guarded",
        store,  # type: ignore[arg-type]
        ["blocked-skill", "ok-skill"],
        user_id="alice",
    )

    assert blocked == {"blocked-skill": True, "ok-skill": False}


@pytest.mark.asyncio
async def test_compute_blocked_skills_never_runs_llm_prompt_policy(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``blocked`` preview consults only cheap, name-based deny rules:
    an LLM-backed prompt-classifier policy is NEVER invoked, even with many
    discovered skills, and ``blocked`` still reflects the name-based
    ``block_skills`` deny.

    Without this guard, a session config with a ``prompt_policy`` on
    ``tool_call`` fires one LLM classifier call per skill name — ~100+
    sequential LLM calls on a single read-only Capabilities panel open.
    """
    from omnigent.runtime.policies.builder import build_policy_engine
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)

    class _RecordingLLM:
        """Spy LLM client: a prompt policy that ran would call ``create``."""

        def __init__(self) -> None:
            self.calls = 0

        async def create(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            self.calls += 1
            raise AssertionError("prompt-policy LLM must not run in blocked preview")

    spy = _RecordingLLM()

    spec = AgentSpec(
        spec_version=1,
        name="classified",
        description="Has an LLM prompt policy plus a name-based block",
        executor=ExecutorSpec(type="claude_sdk"),
        guardrails=GuardrailsSpec(
            policies=[
                # LLM-backed prompt classifier on every tool_call.
                FunctionPolicySpec(
                    name="classify",
                    on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
                    function=FunctionRef(
                        path="omnigent.policies.builtins.prompt.prompt_policy",
                        arguments={"prompt": "Deny risky skills."},
                    ),
                ),
                # Cheap name-based deny — must still be reflected.
                FunctionPolicySpec(
                    name="block",
                    on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
                    function=FunctionRef(
                        path="omnigent.policies.builtins.safety.block_skills",
                        arguments={"blocked": ["blocked-skill"]},
                    ),
                ),
            ]
        ),
    )

    def _build(sp: Any, sid: str, cs: Any) -> Any:
        engine = build_policy_engine(spec=sp, conversation_id=sid, conversation_store=cs)
        # Inject a live LLM client so the prompt policy WOULD call it if it
        # were left in the probe engine — the fix must filter it out first.
        engine._llm_client = spy
        return engine

    monkeypatch.setattr(sessions_mod, "_build_policy_engine_from_spec", _build)

    names = [f"skill-{i}" for i in range(50)] + ["blocked-skill"]
    blocked = await sessions_mod._compute_blocked_skills(
        spec,
        "conv_classified",
        store,  # type: ignore[arg-type]
        names,
        user_id="alice",
    )

    # Zero LLM/prompt-policy calls despite 51 skills.
    assert spy.calls == 0
    # Name-based deny still reflected; everything else not blocked.
    assert blocked["blocked-skill"] is True
    assert all(v is False for k, v in blocked.items() if k != "blocked-skill")
    assert len(blocked) == len(names)


@pytest.mark.asyncio
async def test_compute_blocked_skills_never_runs_intent_authorization_llm(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generalized guard: an LLM-backed router other than the prompt
    classifier is also excluded from the blocked preview.

    ``intent_based_authorization`` fires on ``tool_call`` and, once a
    session intent is captured, calls the LLM to classify every tool
    invocation. The synthetic per-skill ``Skill`` tool-call probe would
    otherwise fire one classifier call per discovered skill. The registry
    ``llm_backed`` flag must exclude it before the per-skill loop.

    Fails without the generalized fix: the old ``PROMPT_POLICY_HANDLER``
    string check does not match this factory, so it stays in the probe
    engine and the spy LLM is invoked.
    """
    from omnigent.policies.builtins.routing import _INTENT_KEY
    from omnigent.runtime.policies.builder import build_policy_engine
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)

    class _RecordingLLM:
        """Spy LLM client: an intent check that ran would call ``create``."""

        def __init__(self) -> None:
            self.calls = 0

        async def create(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            self.calls += 1
            raise AssertionError("intent-authorization LLM must not run in blocked preview")

    spy = _RecordingLLM()

    spec = AgentSpec(
        spec_version=1,
        name="intent-gated",
        description="Has an LLM-gated intent router plus a name-based block",
        executor=ExecutorSpec(type="claude_sdk"),
        guardrails=GuardrailsSpec(
            policies=[
                # LLM-backed router on every tool_call — NOT the prompt policy.
                FunctionPolicySpec(
                    name="intent",
                    on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
                    function=FunctionRef(
                        path="omnigent.policies.builtins.routing.intent_based_authorization",
                        arguments={},
                    ),
                ),
                # Cheap name-based deny — must still be reflected.
                FunctionPolicySpec(
                    name="block",
                    on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
                    function=FunctionRef(
                        path="omnigent.policies.builtins.safety.block_skills",
                        arguments={"blocked": ["blocked-skill"]},
                    ),
                ),
            ]
        ),
    )

    def _build(sp: Any, sid: str, cs: Any) -> Any:
        engine = build_policy_engine(spec=sp, conversation_id=sid, conversation_store=cs)
        engine._llm_client = spy
        # Pre-seed a captured intent so the tool_call phase would reach the
        # classification LLM call if the router were left in the probe engine.
        engine._session_state = {_INTENT_KEY: "Summarize the quarterly report."}
        return engine

    monkeypatch.setattr(sessions_mod, "_build_policy_engine_from_spec", _build)

    names = [f"skill-{i}" for i in range(50)] + ["blocked-skill"]
    blocked = await sessions_mod._compute_blocked_skills(
        spec,
        "conv_intent",
        store,  # type: ignore[arg-type]
        names,
        user_id="alice",
    )

    # Zero LLM calls despite 51 skills and a captured intent.
    assert spy.calls == 0
    # Name-based deny still reflected; everything else not blocked.
    assert blocked["blocked-skill"] is True
    assert all(v is False for k, v in blocked.items() if k != "blocked-skill")
    assert len(blocked) == len(names)


@pytest.mark.asyncio
async def test_compute_blocked_skills_only_llm_policy_short_circuits(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the ONLY deny policy is LLM-backed, the preview short-circuits to
    all-not-blocked without invoking the classifier."""
    from omnigent.runtime.policies.builder import build_policy_engine
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)

    class _RecordingLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def create(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            self.calls += 1
            raise AssertionError("prompt-policy LLM must not run in blocked preview")

    spy = _RecordingLLM()

    spec = AgentSpec(
        spec_version=1,
        name="llm-only",
        description="Only an LLM prompt policy",
        executor=ExecutorSpec(type="claude_sdk"),
        guardrails=GuardrailsSpec(
            policies=[
                FunctionPolicySpec(
                    name="classify",
                    on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
                    function=FunctionRef(
                        path="omnigent.policies.builtins.prompt.prompt_policy",
                        arguments={"prompt": "Deny risky skills."},
                    ),
                ),
            ]
        ),
    )

    def _build(sp: Any, sid: str, cs: Any) -> Any:
        engine = build_policy_engine(spec=sp, conversation_id=sid, conversation_store=cs)
        engine._llm_client = spy
        # Isolate the LLM policy: drop the always-appended sys_add_policy
        # guard so that after the preview filters out the LLM policy, ZERO
        # name-based policies remain — exercising the short-circuit branch.
        engine.policies = [p for p in engine.policies if sessions_mod._is_llm_backed_policy(p)]
        assert engine.policies, "expected the prompt policy to be present"
        return engine

    monkeypatch.setattr(sessions_mod, "_build_policy_engine_from_spec", _build)

    blocked = await sessions_mod._compute_blocked_skills(
        spec,
        "conv_llm_only",
        store,  # type: ignore[arg-type]
        ["a", "b", "c"],
        user_id=None,
    )

    assert spy.calls == 0
    assert blocked == {"a": False, "b": False, "c": False}


@pytest.mark.asyncio
async def test_compute_blocked_skills_empty_without_policies(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no guardrails, nothing is blocked (and no per-skill eval runs)."""
    from omnigent.runtime.policies.builder import build_policy_engine
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    spec = AgentSpec(
        spec_version=1,
        name="plain",
        description="No guardrails",
        executor=ExecutorSpec(type="claude_sdk"),
    )
    monkeypatch.setattr(
        sessions_mod,
        "_build_policy_engine_from_spec",
        lambda sp, sid, cs: build_policy_engine(
            spec=sp, conversation_id=sid, conversation_store=cs
        ),
    )

    blocked = await sessions_mod._compute_blocked_skills(
        spec,
        "conv_plain",
        store,  # type: ignore[arg-type]
        ["anything"],
        user_id=None,
    )

    # build_policy_engine always appends the sys_add_policy guard, so the
    # engine is non-empty; the Skill tool-call still resolves to ALLOW.
    assert blocked == {"anything": False}


# ── MCP per-server tools + status (Slice D) ──────────────────────

# A two-server spec so an endpoint test can show one connected server (with
# discovered tools) alongside one that failed to connect.
_TWO_MCP = AgentSpec(
    spec_version=1,
    name="two-mcp",
    description="Two MCP servers",
    executor=ExecutorSpec(type="claude_sdk"),
    mcp_servers=[
        MCPServerConfig(
            name="github", transport="http", url="https://gh.example/sse", description="GitHub"
        ),
        MCPServerConfig(
            name="broken", transport="http", url="https://x.example/sse", description="Broken"
        ),
    ],
)


def _gh_schemas() -> list[dict[str, Any]]:
    """Two runner ``tools/list`` schemas for the ``github`` server."""
    return [
        {
            "type": "function",
            "name": "github__search",
            "description": "Search repos",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "function",
            "name": "github__create_issue",
            "description": "Open an issue",
            "parameters": {"type": "object", "properties": {}},
        },
    ]


def test_mcp_per_server_tools_and_status_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner's discovered MCP tool surface lands per-server: a connected
    server carries its tools + ``status="connected"``; a failed one carries an
    empty tool list + ``status="failed"`` and the connect error."""
    monkeypatch.setattr(
        sessions_mod, "get_agent_cache", lambda: _AgentCacheStub({"ag_top": _TWO_MCP})
    )
    monkeypatch.setattr(
        sessions_mod, "_fetch_runner_skill_capabilities", _fake_runner_skill_capabilities
    )
    monkeypatch.setattr(sessions_mod, "_compute_blocked_skills", _no_blocked_skills)

    async def _fetch(runner_client: Any, session_id: str) -> tuple[Any, Any, bool]:
        del runner_client, session_id
        return _gh_schemas(), {"broken": "ConnectError: refused"}, True

    async def _no_blocked_tools(
        spec: Any, session_id: str, cs: Any, names: list[str], *, user_id: str | None
    ) -> dict[str, bool]:
        del spec, session_id, cs, names, user_id
        return {}

    monkeypatch.setattr(sessions_mod, "_fetch_runner_mcp_tools", _fetch)
    monkeypatch.setattr(sessions_mod, "_compute_blocked_tool_names", _no_blocked_tools)

    conv_store = _ConversationStore({"conv_top": _conv("conv_top")})
    agent_store = _AgentStore({"ag_top": _agent()})
    client = TestClient(_build_app(conv_store, agent_store))

    body = client.get("/v1/sessions/conv_top/capabilities").json()
    servers = {s["name"]: s for s in body["mcp_servers"]}

    gh = servers["github"]
    assert gh["status"] == "connected"
    assert gh["error"] is None
    assert [t["name"] for t in gh["tools"]] == ["search", "create_issue"]
    assert gh["tools"][0]["description"] == "Search repos"
    assert all(t["blocked"] is False for t in gh["tools"])

    broken = servers["broken"]
    assert broken["status"] == "failed"
    assert broken["error"] == "ConnectError: refused"
    assert broken["tools"] == []


def test_mcp_degrades_to_unknown_when_runner_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable runner/MCP yields per-server ``status="unknown"`` with
    empty tools — never a 500."""
    monkeypatch.setattr(
        sessions_mod, "get_agent_cache", lambda: _AgentCacheStub({"ag_top": _TWO_MCP})
    )
    monkeypatch.setattr(
        sessions_mod, "_fetch_runner_skill_capabilities", _fake_runner_skill_capabilities
    )
    monkeypatch.setattr(sessions_mod, "_compute_blocked_skills", _no_blocked_skills)

    async def _fetch_none(runner_client: Any, session_id: str) -> tuple[Any, Any, bool]:
        del runner_client, session_id
        return [], {}, False

    monkeypatch.setattr(sessions_mod, "_fetch_runner_mcp_tools", _fetch_none)

    conv_store = _ConversationStore({"conv_top": _conv("conv_top")})
    agent_store = _AgentStore({"ag_top": _agent()})
    client = TestClient(_build_app(conv_store, agent_store))

    resp = client.get("/v1/sessions/conv_top/capabilities")
    assert resp.status_code == 200, resp.text
    for server in resp.json()["mcp_servers"]:
        assert server["status"] == "unknown"
        assert server["error"] is None
        assert server["tools"] == []


def _cel_deny_spec(name: str, denied_tool: str) -> AgentSpec:
    """A spec with a single CEL policy that DENYs one tool-call name."""
    return AgentSpec(
        spec_version=1,
        name=name,
        description="CEL name-based deny",
        executor=ExecutorSpec(type="claude_sdk"),
        mcp_servers=[
            MCPServerConfig(
                name="github", transport="http", url="https://gh.example/sse", description="GitHub"
            ),
        ],
        guardrails=GuardrailsSpec(
            policies=[
                FunctionPolicySpec(
                    name="deny_one",
                    on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
                    function=FunctionRef(
                        path="omnigent.policies.builtins.cel.cel_policy",
                        arguments={
                            "expression": (
                                f'event.data.name == "{denied_tool}" '
                                '? {"result": "DENY"} : {"result": "ALLOW"}'
                            )
                        },
                    ),
                )
            ]
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", ["github__create_issue", "mcp__github__create_issue"])
async def test_compute_blocked_tool_names_flags_mcp_tool(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    denied: str,
) -> None:
    """An MCP tool denied by a name-based CEL policy is ``blocked=True`` — for
    BOTH the spec-proxied ``<server>__<tool>`` and connector-native
    ``mcp__<server>__<tool>`` surfaces the endpoint probes; a sibling name is
    ``blocked=False``."""
    from omnigent.runtime.policies.builder import build_policy_engine
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    spec = _cel_deny_spec("mcp-guarded", denied)
    monkeypatch.setattr(
        sessions_mod,
        "_build_policy_engine_from_spec",
        lambda sp, sid, cs: build_policy_engine(
            spec=sp, conversation_id=sid, conversation_store=cs
        ),
    )

    blocked = await sessions_mod._compute_blocked_tool_names(
        spec,
        "conv_mcp",
        store,  # type: ignore[arg-type]
        [denied, "github__search"],
        user_id="alice",
    )

    assert blocked[denied] is True
    assert blocked["github__search"] is False


def test_mcp_tool_blocked_wiring_ors_both_name_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The endpoint marks an MCP tool ``blocked`` when the probe denies EITHER
    the spec-proxied or the connector-native name shape."""
    monkeypatch.setattr(
        sessions_mod, "get_agent_cache", lambda: _AgentCacheStub({"ag_top": _TWO_MCP})
    )
    monkeypatch.setattr(
        sessions_mod, "_fetch_runner_skill_capabilities", _fake_runner_skill_capabilities
    )
    monkeypatch.setattr(sessions_mod, "_compute_blocked_skills", _no_blocked_skills)

    async def _fetch(runner_client: Any, session_id: str) -> tuple[Any, Any, bool]:
        del runner_client, session_id
        return _gh_schemas(), {}, True

    monkeypatch.setattr(sessions_mod, "_fetch_runner_mcp_tools", _fetch)

    async def _blocked_tools(
        spec: Any, session_id: str, cs: Any, names: list[str], *, user_id: str | None
    ) -> dict[str, bool]:
        del spec, session_id, cs, user_id
        # Deny only the connector-native shape; the endpoint must still flag it.
        return dict.fromkeys(names, False) | {"mcp__github__create_issue": True}

    monkeypatch.setattr(sessions_mod, "_compute_blocked_tool_names", _blocked_tools)

    conv_store = _ConversationStore({"conv_top": _conv("conv_top")})
    agent_store = _AgentStore({"ag_top": _agent()})
    client = TestClient(_build_app(conv_store, agent_store))

    body = client.get("/v1/sessions/conv_top/capabilities").json()
    tools = {t["name"]: t for t in body["mcp_servers"][0]["tools"]}
    assert tools["create_issue"]["blocked"] is True
    assert tools["search"]["blocked"] is False


@pytest.mark.asyncio
async def test_compute_blocked_tool_names_flags_builtin_and_runs_no_llm(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local builtin tool denied by a name policy is ``blocked=True``; a
    normal one is ``blocked=False``; and NO LLM fires even with an LLM-backed
    policy present and many tool names."""
    from omnigent.runtime.policies.builder import build_policy_engine
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)

    class _RecordingLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def create(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            self.calls += 1
            raise AssertionError("LLM policy must not run in blocked-tools preview")

    spy = _RecordingLLM()

    spec = AgentSpec(
        spec_version=1,
        name="tool-guarded",
        description="CEL deny + LLM policy",
        executor=ExecutorSpec(type="claude_sdk"),
        guardrails=GuardrailsSpec(
            policies=[
                FunctionPolicySpec(
                    name="deny_shell",
                    on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
                    function=FunctionRef(
                        path="omnigent.policies.builtins.cel.cel_policy",
                        arguments={
                            "expression": (
                                'event.data.name == "sys_os_shell" '
                                '? {"result": "DENY"} : {"result": "ALLOW"}'
                            )
                        },
                    ),
                ),
                FunctionPolicySpec(
                    name="classify",
                    on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)],
                    function=FunctionRef(
                        path="omnigent.policies.builtins.prompt.prompt_policy",
                        arguments={"prompt": "Deny risky tools."},
                    ),
                ),
            ]
        ),
    )

    def _build(sp: Any, sid: str, cs: Any) -> Any:
        engine = build_policy_engine(spec=sp, conversation_id=sid, conversation_store=cs)
        engine._llm_client = spy
        return engine

    monkeypatch.setattr(sessions_mod, "_build_policy_engine_from_spec", _build)

    names = [f"sys_tool_{i}" for i in range(50)] + ["sys_os_shell", "sys_os_read"]
    blocked = await sessions_mod._compute_blocked_tool_names(
        spec,
        "conv_tool_guarded",
        store,  # type: ignore[arg-type]
        names,
        user_id="alice",
    )

    assert spy.calls == 0
    assert blocked["sys_os_shell"] is True
    assert blocked["sys_os_read"] is False
    assert all(v is False for k, v in blocked.items() if k != "sys_os_shell")


def test_sub_agent_tree_recursion_is_depth_bounded() -> None:
    """A pathologically deep declared-sub-agent chain is truncated at the depth
    cap instead of recursing without bound."""
    depth = sessions_mod._SUB_AGENT_TREE_MAX_DEPTH + 10
    # Build a linear chain deeper than the cap, innermost first.
    node = AgentSpec(
        spec_version=1, name="leaf", description="", executor=ExecutorSpec(type="claude_sdk")
    )
    for i in range(depth):
        node = AgentSpec(
            spec_version=1,
            name=f"n{i}",
            description="",
            executor=ExecutorSpec(type="claude_sdk"),
            sub_agents=[node],
        )

    tree = sessions_mod._sub_agent_capability_tree([node])

    # Walk the single-child chain; it must terminate at exactly the cap.
    walked = 0
    level = tree
    while level:
        walked += 1
        level = level[0].sub_agents
    assert walked == sessions_mod._SUB_AGENT_TREE_MAX_DEPTH
