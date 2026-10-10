"""Session-list model metadata comes directly from the persisted row."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from omnigent.entities import Conversation
from omnigent.server.routes._sessions.helpers import (
    _harness_from_loaded_spec,
    _prepare_child_harnesses,
    _resolve_harness_impl,
)
from omnigent.server.routes._sessions.orchestration import (
    _build_session_list_item,
    _dump_session_list_item,
)
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.spec.types import AgentSpec, BuiltinToolConfig, ExecutorSpec, ToolsConfig
from omnigent.stores.agent_store import AgentListMetadata
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.parametrize(
    ("reported", "override", "expected"),
    [
        ("opus[1m]", "sonnet", "opus[1m]"),
        (None, "sonnet", "sonnet"),
        (None, None, None),
        ("<synthetic>", "sonnet", "sonnet"),
        ("<synthetic>", None, None),
    ],
)
@pytest.mark.parametrize("harness_override", [None, "claude-sdk", "codex"])
def test_session_list_model(
    reported: str | None,
    override: str | None,
    expected: str | None,
    harness_override: str | None,
) -> None:
    conv = Conversation(
        id="conv_model",
        agent_id="ag_model",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_model",
        reported_model=reported,
        model_override=override,
        harness_override=harness_override,
    )
    item = _build_session_list_item(
        conv,
        agent_names_by_id={},
        grants=[],
        user_id=None,
        user_is_admin=False,
        permissions_enabled=False,
        pending_count=0,
        child_session_ids=[],
        comments_fingerprint=None,
    )
    assert item.llm_model == expected
    assert item.model_dump()["llm_model"] == expected
    assert item.harness_override == harness_override
    assert item.model_dump()["harness_override"] == harness_override


@pytest.mark.parametrize("child_harness", ["codex", None])
@pytest.mark.parametrize("mode", ["on", "off", None])
def test_child_and_routing_wire_preserves_unknown_vs_absent(child_harness, mode) -> None:
    conv = Conversation(
        id="child",
        agent_id="agent",
        created_at=1,
        updated_at=1,
        root_conversation_id="parent",
        sub_agent_name="worker",
        cost_control_mode_override=mode,
    )
    kwargs = {
        "agent_names_by_id": {},
        "grants": [],
        "user_id": None,
        "user_is_admin": False,
        "permissions_enabled": False,
        "pending_count": 0,
        "child_session_ids": [],
        "comments_fingerprint": None,
        "child_harnesses": {"child": child_harness},
    }
    for full in (False, True):
        wire = _dump_session_list_item(_build_session_list_item(conv, **kwargs), full=full)
        assert "child_harness" in wire and wire["child_harness"] == child_harness
        assert wire.get("cost_control_mode_override") == mode
        conv.sub_agent_name = None
        assert "child_harness" not in _dump_session_list_item(
            _build_session_list_item(conv, **kwargs), full=full
        )
        conv.sub_agent_name = "worker"


@pytest.mark.parametrize(
    ("name", "child_harness", "override", "expected"),
    [
        ("worker", "codex", None, "codex"),
        ("nested", "codex", None, "codex"),
        ("worker", None, None, "claude-sdk"),
        ("missing", "codex", None, "claude-sdk"),
        ("worker", "claude", None, "claude-sdk"),
        ("worker", "codex", "auto", "auto"),
        ("worker", "codex", "openai-agents", "openai-agents"),
        ("__web_researcher", "codex", None, "codex"),
    ],
)
def test_loaded_child_resolver_matches_detail(name, child_harness, override, expected) -> None:
    nested = AgentSpec(
        spec_version=1, name="nested", executor=ExecutorSpec(config={"harness": "codex"})
    )
    child = AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(config={"harness": child_harness}),
        sub_agents=[nested],
        tools=ToolsConfig(
            builtins=[BuiltinToolConfig(name="web_fetch")] if name == "__web_researcher" else []
        ),
    )
    spec = AgentSpec(
        spec_version=1,
        name="parent",
        executor=ExecutorSpec(config={"harness": "claude-sdk"}),
        sub_agents=[child],
    )
    conv = Conversation(
        id="child",
        agent_id="agent",
        created_at=1,
        updated_at=1,
        root_conversation_id="parent",
        sub_agent_name=name,
        harness_override=override,
    )
    store = Mock()
    store.get.return_value = SimpleNamespace(
        id="agent", bundle_location="bundle", operator_authored=False
    )
    cache = Mock()
    cache.load.return_value = SimpleNamespace(spec=spec)
    assert _harness_from_loaded_spec(conv, spec) == expected
    assert _resolve_harness_impl(conv, agent_store=store, agent_cache=cache) == expected


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("bundles", [1, 2])
def test_child_preparation_dedupes_and_skips_nonchildren(failed, bundles) -> None:
    metadata = {
        f"agent{index}": AgentListMetadata(f"agent{index}", "bundle", f"digest{index}", "user")
        for index in range(bundles)
    }
    rows = [
        Conversation(
            id=str(index),
            agent_id=f"agent{index % bundles}",
            created_at=1,
            updated_at=1,
            root_conversation_id="parent",
            sub_agent_name="worker",
        )
        for index in range(3)
    ]
    cache = Mock()
    cache.load.return_value = SimpleNamespace(
        spec=AgentSpec(
            spec_version=1,
            name="parent",
            executor=ExecutorSpec(config={"harness": "claude-sdk"}),
            sub_agents=[
                AgentSpec(
                    spec_version=1,
                    name="worker",
                    executor=ExecutorSpec(config={"harness": "codex"}),
                )
            ],
        )
    )
    if failed:
        cache.load.side_effect = OSError("unavailable bundle")
    assert _prepare_child_harnesses(rows, metadata, cache) == {
        row.id: None if failed else "codex" for row in rows
    }
    assert cache.load.call_count == bundles
    for index in range(bundles):
        cache.load.assert_any_call(f"agent{index}", f"digest{index}", expand_env=False)
    cache.load.reset_mock()
    for row in rows:
        row.harness_override = "auto"
    assert _prepare_child_harnesses(rows, metadata, cache) == {}
    for row in rows:
        row.harness_override = None
        row.sub_agent_name = None
    assert _prepare_child_harnesses(rows, metadata, cache) == {}
    cache.load.assert_not_called()
    rows[0].sub_agent_name = "worker"
    assert _prepare_child_harnesses(rows, {}, cache) == {rows[0].id: None}


def test_list_child_resolution_adds_zero_queries_and_loads_off_loop(db_uri, monkeypatch) -> None:
    agents = SqlAlchemyAgentStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    agent_id = "087b7cb7ac30abf4debfaa578d052ec6"
    agents.create_user_agent(agent_id, "bundle", f"{agent_id}/bundle", owner="alice")
    parent = conversations.create_conversation(agent_id=agent_id)
    children = [
        conversations.create_conversation(
            agent_id=agent_id,
            parent_conversation_id=parent.id,
            sub_agent_name="worker",
            cost_control_mode_override="on",
        )
        for _ in range(2)
    ]
    spec = AgentSpec(
        spec_version=1,
        name="bundle",
        executor=ExecutorSpec(config={"harness": "claude-sdk"}),
        sub_agents=[
            AgentSpec(
                spec_version=1, name="worker", executor=ExecutorSpec(config={"harness": "codex"})
            )
        ],
    )
    cache = Mock()
    cache.load.side_effect = lambda *args, **kwargs: SimpleNamespace(spec=spec)
    app = FastAPI()
    loop_thread = []

    @app.middleware("http")
    async def remember_loop(request, call_next):
        loop_thread.append(threading.get_ident())
        return await call_next(request)

    app.include_router(
        create_sessions_router(
            conversation_store=conversations, agent_store=agents, agent_cache=cache
        ),
        prefix="/v1",
    )
    metadata = agents.get_list_metadata([agent_id])
    original = agents.get_list_metadata
    statements = []

    def capture(*args):
        if args[2].lstrip().upper().startswith("SELECT"):
            statements.append(args[2])

    event.listen(agents._engine, "before_cursor_execute", capture)
    monkeypatch.setattr(agents, "get", Mock(side_effect=AssertionError("per-agent lookup")))
    with TestClient(app) as client:
        monkeypatch.setattr(
            agents,
            "get_list_metadata",
            lambda ids: {key: metadata[key] for key in agents.get_names(ids)},
        )
        client.get("/v1/sessions?kind=any").raise_for_status()
        baseline = len(statements)
        statements.clear()
        cache.load.reset_mock()
        monkeypatch.setattr(agents, "get_list_metadata", original)
        monkeypatch.setattr(
            agents, "get_names", Mock(side_effect=AssertionError("extra name query"))
        )
        load_threads = []

        def load(*args, **kwargs):
            load_threads.append(threading.get_ident())
            return SimpleNamespace(spec=spec)

        cache.load.side_effect = load
        response = client.get("/v1/sessions?kind=any")
        response.raise_for_status()
        assert len(statements) == baseline
        wire = {item["id"]: item for item in response.json()["data"]}
        assert "child_harness" not in wire[parent.id]
        assert all(wire[child.id]["child_harness"] == "codex" for child in children)
        assert all(wire[child.id]["cost_control_mode_override"] == "on" for child in children)
        assert cache.load.call_count == 1
        assert load_threads and all(thread not in loop_thread for thread in load_threads)
        cache.load.reset_mock()
        client.get("/v1/sessions").raise_for_status()
        cache.load.assert_not_called()
    event.remove(agents._engine, "before_cursor_execute", capture)
