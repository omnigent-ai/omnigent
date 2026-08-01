"""L5 integration coverage for intelligent routing with a fake router.

Drives the server's own routing paths against a stub routing client:
the ``sys_session_send`` child override precedence, the native-subagent
relay endpoint (transcript item + child-sessions join), the subagent
fail-mode knob, and the decision cache.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from omnigent.runner.subagent_routing import (
    AUTO_HARNESS_LABEL_KEY,
    ROUTING_DECISION_LABEL_KEY,
)
from omnigent.server.routes._sessions import orchestration as orchestration_module
from omnigent.server.schemas import SessionEventInput
from omnigent.server.smart_routing import RoutingResult
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import (
    FakeCaps,
    FakeRoutingClient,
    create_test_agent,
    echo_runner_client,
)

ROUTED_MODEL = "databricks-claude-opus-4-8"
LLM_PICKED_MODEL = "databricks-claude-sonnet-4-6"
GPT_MODEL = "databricks-gpt-5-5"
GLM_MODEL = "databricks-glm-5-2"
# The spelling the gateway serves GLM under; see ``_SERVABLE_ALIASES``.
GLM_SERVABLE = "system.ai.glm-5-2"

pytestmark = pytest.mark.asyncio


async def _parent_and_child(
    client: httpx.AsyncClient,
    db_uri: str,
    *,
    agent_name: str,
    child_model_override: str | None = None,
) -> tuple[dict[str, Any], Any, SqlAlchemyConversationStore]:
    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert resp.status_code == 201, resp.text
    parent = resp.json()
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="reviewer:auth",
        parent_conversation_id=parent["id"],
        agent_id=agent["id"],
    )
    if child_model_override is not None:
        child = conv_store.update_conversation(child.id, model_override=child_model_override)
    return parent, child, conv_store


def _routing_decisions(conv_store: SqlAlchemyConversationStore, session_id: str) -> list[Any]:
    return [
        item
        for item in conv_store.list_items(session_id).data
        if getattr(item, "type", None) == "routing_decision"
    ]


# ── 1. Child spawn: the router wins over ``args.model`` ─────────────


async def test_router_overrides_llm_supplied_child_model(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    _parent, child, conv_store = await _parent_and_child(
        client,
        db_uri,
        agent_name="routing-precedence",
        child_model_override=LLM_PICKED_MODEL,
    )
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model=ROUTED_MODEL, rationale="deep refactor", harness="claude_code")
        )
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child.id,
                child,
                body,
                conv_store,
                runner_client,
            )

    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    # The router's pick replaced the orchestrator's ``args.model``.
    assert refreshed.model_override == ROUTED_MODEL
    decisions = _routing_decisions(conv_store, child.id)
    assert len(decisions) == 1
    data = decisions[0].data
    assert data.model == ROUTED_MODEL
    assert data.scope == "child_session"
    assert data.attempted_override == LLM_PICKED_MODEL
    assert data.decision_id
    # The decision is joined onto the child row for the sidebar.
    assert refreshed.labels.get(ROUTING_DECISION_LABEL_KEY) == data.decision_id


async def test_routed_model_publishes_session_model_event(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A routed pick pushes ``session.model`` so open web pickers update live.

    Routing persists ``model_override`` server-side; without the SSE the
    dropdown keeps showing the launch model until a reload (the PATCH and
    ``external_model_change`` paths publish it — routing must too).
    """
    _parent, child, conv_store = await _parent_and_child(
        client,
        db_uri,
        agent_name="routing-sse",
        child_model_override=LLM_PICKED_MODEL,
    )
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model=ROUTED_MODEL, rationale="deep refactor", harness="claude_code")
        )
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    published: list[tuple[str, dict[str, Any]]] = []
    with (
        patch("omnigent.runtime._globals._caps", new=caps),
        patch.object(
            orchestration_module.session_stream,
            "publish",
            side_effect=lambda sid, payload: published.append((sid, payload)),
        ),
    ):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child.id,
                child,
                body,
                conv_store,
                runner_client,
            )

    model_events = [
        payload
        for sid, payload in published
        if sid == child.id and payload.get("type") == "session.model"
    ]
    assert len(model_events) == 1
    assert model_events[0]["model"] == ROUTED_MODEL


# ── 2. Native-subagent relay: transcript item + child join ──────────


async def test_native_subagent_relay_persists_decision_and_joins_child_row(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    parent, child, conv_store = await _parent_and_child(
        client, db_uri, agent_name="routing-native-subagent"
    )
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model=ROUTED_MODEL, rationale="deep reasoning", harness="claude_code")
        )
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        resp = await client.post(
            f"/v1/sessions/{parent['id']}/hooks/route-subagent",
            json={
                "harness": "claude-native",
                "task_name": "code-reviewer",
                "prompt": "review the auth module",
                "parent_model": LLM_PICKED_MODEL,
            },
        )
    assert resp.status_code == 200, resp.text
    decision = resp.json()
    assert decision["action"] == "rewrite"
    assert decision["model"] == ROUTED_MODEL

    decisions = _routing_decisions(conv_store, parent["id"])
    assert len(decisions) == 1
    data = decisions[0].data
    assert data.scope == "native_subagent"
    assert data.decision_id == decision["decision_id"]
    assert data.harness == "claude-native"

    # A routed child row surfaces both fields to the sidebar.
    conv_store.update_conversation(child.id, model_override=ROUTED_MODEL)
    conv_store.set_labels(child.id, {ROUTING_DECISION_LABEL_KEY: decision["decision_id"]})
    rows = await client.get(f"/v1/sessions/{parent['id']}/child_sessions")
    assert rows.status_code == 200
    row = rows.json()["data"][0]
    assert row["routed_model"] == ROUTED_MODEL
    assert row["routing_decision_id"] == decision["decision_id"]

    # A user-pinned model that no decision produced is NOT a routed model —
    # reporting one alongside a null decision id makes the two fields disagree.
    pinned = conv_store.create_conversation(
        kind="sub_agent",
        title="pinned:auth",
        parent_conversation_id=parent["id"],
        agent_id=child.agent_id,
    )
    conv_store.update_conversation(pinned.id, model_override=LLM_PICKED_MODEL)
    rows = await client.get(f"/v1/sessions/{parent['id']}/child_sessions")
    assert rows.status_code == 200
    pinned_row = next(item for item in rows.json()["data"] if item["id"] == pinned.id)
    assert pinned_row["routed_model"] is None
    assert pinned_row["routing_decision_id"] is None


# ── 3. Dead router ─────────────────────────────────────────────────


async def test_dead_router_allows_the_spawn_unchanged(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The subagent gate is advisory: an outage never blocks a spawn."""
    agent = await create_test_agent(client, name="routing-dead-router")
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    caps = FakeCaps(
        routing_client=FakeRoutingClient(None, error=RuntimeError("router down")),
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        route = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json={"harness": "codex-native", "task_name": "explore"},
        )
    assert route.status_code == 200, route.text
    assert route.json()["action"] == "allow"

    # The outage still leaves a decision item behind.
    conv_store = SqlAlchemyConversationStore(db_uri)
    assert len(_routing_decisions(conv_store, session_id)) == 1


# ── 5. Per-session subagent-routing gate ───────────────────────────


SPAWN_PAYLOAD = {
    "harness": "claude-native",
    "task_name": "code-reviewer",
    "prompt": "review the auth module",
    "parent_model": LLM_PICKED_MODEL,
}
DISABLED_RATIONALE = "subagent routing disabled for this session"


async def _session_with_routing_flags(
    client: httpx.AsyncClient,
    *,
    agent_name: str,
    cost_control: str | None = None,
    subagent_routing: str | None = None,
) -> str:
    agent = await create_test_agent(client, name=agent_name)
    body: dict[str, Any] = {"agent_id": agent["id"]}
    if cost_control is not None:
        body["cost_control_mode_override"] = cost_control
    if subagent_routing is not None:
        body["subagent_routing_override"] = subagent_routing
    resp = await client.post("/v1/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


@pytest.mark.parametrize(
    ("case", "cost_control", "subagent_routing", "routed"),
    [
        ("inherit-on", "on", None, True),
        ("inherit-off", None, None, False),
        ("override-off", "on", "off", False),
        ("override-on", None, "on", True),
    ],
)
async def test_subagent_gate_follows_the_session_setting(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    cost_control: str | None,
    subagent_routing: str | None,
    routed: bool,
) -> None:
    session_id = await _session_with_routing_flags(
        client,
        agent_name=f"routing-gate-{case}",
        cost_control=cost_control,
        subagent_routing=subagent_routing,
    )
    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="deep reasoning", harness="claude_code")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        resp = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
    assert resp.status_code == 200, resp.text
    decision = resp.json()
    conv_store = SqlAlchemyConversationStore(db_uri)
    if routed:
        assert decision["action"] == "rewrite"
        assert decision["model"] == ROUTED_MODEL
        assert len(routing_client.calls) == 1
        assert len(_routing_decisions(conv_store, session_id)) == 1
    else:
        # Allowed unchanged, router untouched, and no transcript spam.
        assert decision["action"] == "allow"
        assert decision["model"] is None
        assert decision["rationale"] == DISABLED_RATIONALE
        assert routing_client.calls == []
        assert _routing_decisions(conv_store, session_id) == []


async def test_child_inherits_the_parents_routing_state(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    _parent, child, conv_store = await _parent_and_child(
        client, db_uri, agent_name="routing-gate-child-inherit"
    )
    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="deep reasoning", harness="claude_code")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        resp = await client.post(
            f"/v1/sessions/{child.id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "rewrite"
    assert len(routing_client.calls) == 1

    # The child's own "off" wins over the parent's routed state.
    conv_store.update_conversation(child.id, subagent_routing_override="off")
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        second = await client.post(
            f"/v1/sessions/{child.id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
    assert second.status_code == 200, second.text
    assert second.json()["action"] == "allow"
    assert second.json()["rationale"] == DISABLED_RATIONALE
    assert len(routing_client.calls) == 1


async def test_patch_round_trips_the_subagent_setting_and_rejects_junk(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    session_id = await _session_with_routing_flags(client, agent_name="routing-gate-patch")

    for value in ("on", "off"):
        patched = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"subagent_routing_override": value},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["subagent_routing_override"] == value
        snapshot = await client.get(f"/v1/sessions/{session_id}")
        assert snapshot.json()["subagent_routing_override"] == value

    # Explicit null clears it back to inheriting the main routing state.
    cleared = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"subagent_routing_override": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["subagent_routing_override"] is None

    rejected = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"subagent_routing_override": "maybe"},
    )
    assert rejected.status_code == 400, rejected.text
    assert "subagent_routing_override" in rejected.text


async def test_toggling_the_setting_on_mid_session_routes_the_next_spawn(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    session_id = await _session_with_routing_flags(client, agent_name="routing-gate-midsession")
    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="deep reasoning", harness="claude_code")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        before = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
        assert before.json()["action"] == "allow"
        patched = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"subagent_routing_override": "on"},
        )
        assert patched.status_code == 200, patched.text
        after = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
    assert after.json()["action"] == "rewrite"
    assert after.json()["model"] == ROUTED_MODEL


# ── 6. Harness-family constraint on the candidate set ──────────────


@pytest.mark.parametrize(
    ("harness", "counterpart", "picked"),
    [
        ("claude-native", "codex-native", ROUTED_MODEL),
        ("codex-native", "claude-native", GPT_MODEL),
    ],
)
async def test_pinned_session_is_offered_only_its_own_family(
    client: httpx.AsyncClient,
    db_uri: str,
    harness: str,
    counterpart: str,
    picked: str,
) -> None:
    session_id = await _session_with_routing_flags(
        client,
        agent_name=f"routing-family-{harness}",
        subagent_routing="on",
    )
    routing_client = FakeRoutingClient(RoutingResult(model=picked, rationale="deep reasoning"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        resp = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json={**SPAWN_PAYLOAD, "harness": harness},
        )
    assert resp.status_code == 200, resp.text
    assert set(routing_client.offered[0]) == {harness}
    assert counterpart not in routing_client.offered[0]
    assert resp.json()["action"] == "rewrite"


async def test_codex_session_keeps_glm_candidates_and_applies_a_glm_pick(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A codex session's GLM endpoint survives filtering and a GLM pick applies.

    GLM serves on the same Responses wire codex speaks, so the live catalog
    row must reach the router and the resulting pick must pass the dispatch
    family gate a real spawn goes through. The row is offered — and the pick
    applied — under the gateway's own ``system.ai.glm-5-2`` model route, which
    is the only name that serves.
    """
    from omnigent.model_override import model_family_mismatch
    from omnigent.server import smart_routing as smart_routing_module
    from omnigent.server.routes import sessions as sessions_facade

    session_id = await _session_with_routing_flags(
        client,
        agent_name="routing-codex-glm",
        subagent_routing="on",
    )
    routing_client = FakeRoutingClient(
        RoutingResult(model=GLM_SERVABLE, rationale="delegate arm", harness="codex")
    )
    live_catalog = {"self": [GPT_MODEL, GLM_MODEL]}

    async def _fake_runner_client(*_args: Any, **_kwargs: Any) -> Any:
        return object()

    async def _fake_fetch(*_args: Any, **_kwargs: Any) -> dict[str, list[str]]:
        return live_catalog

    with (
        patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)),
        patch.object(sessions_facade, "_get_runner_client", _fake_runner_client),
        patch.object(smart_routing_module, "fetch_runner_models", _fake_fetch),
    ):
        resp = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json={**SPAWN_PAYLOAD, "harness": "codex-native", "parent_model": GPT_MODEL},
        )

    assert resp.status_code == 200, resp.text
    # The GLM row reached the router as a codex candidate.
    assert routing_client.offered[0] == {"codex-native": [GPT_MODEL, GLM_SERVABLE]}
    body = resp.json()
    assert body["action"] == "rewrite"
    assert body["model"] == GLM_SERVABLE
    # And the applied pick is one the dispatch gate accepts on codex.
    assert model_family_mismatch("codex-native", GLM_SERVABLE) is None


async def test_auto_session_and_its_children_keep_cross_harness_picks(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    agent = await create_test_agent(client, name="routing-family-auto")
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "cost_control_mode_override": "on",
            "harness_override": "auto",
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="reviewer:auth",
        parent_conversation_id=session_id,
        agent_id=agent["id"],
    )

    routing_client = FakeRoutingClient(
        RoutingResult(model=GPT_MODEL, rationale="narrow change", harness="codex")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        resp = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
        child_resp = await client.post(
            f"/v1/sessions/{child.id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
    assert resp.status_code == 200, resp.text
    assert set(routing_client.offered[0]) == {"claude-native", "codex-native"}
    # Auto keeps the cross-family escape hatch: a Codex pick redirects.
    assert resp.json()["action"] == "redirect"
    assert resp.json()["harness"] == "codex-native"
    # The child of an auto session inherits the cross-harness allowance.
    assert child_resp.status_code == 200, child_resp.text
    assert set(routing_client.offered[1]) == {"claude-native", "codex-native"}


async def test_unlabelled_auto_sentinel_still_allows_cross_harness_picks(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The ``auto`` sentinel alone is enough, with no auto-harness label.

    The label and the sentinel are written by different create paths, so the
    endpoint must read both through ``auto_harness_session`` — otherwise a
    session carrying only the sentinel is silently pinned to one family.
    """
    session_id = await _session_with_routing_flags(
        client,
        agent_name="routing-auto-sentinel",
        subagent_routing="on",
    )
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.update_conversation(session_id, harness_override="auto")
    assert conv is not None
    assert conv.harness_override == "auto"
    assert AUTO_HARNESS_LABEL_KEY not in (conv.labels or {})

    routing_client = FakeRoutingClient(
        RoutingResult(model=GPT_MODEL, rationale="narrow change", harness="codex")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        resp = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
    assert resp.status_code == 200, resp.text
    assert set(routing_client.offered[0]) == {"claude-native", "codex-native"}
    assert resp.json()["action"] == "redirect"


# ── 7. Omnigent child sessions stay in the parent's family ─────────


async def _pinned_parent_and_child(
    client: httpx.AsyncClient,
    db_uri: str,
    *,
    agent_name: str,
    harness: str,
) -> tuple[str, Any, SqlAlchemyConversationStore]:
    """Create a routing-on parent pinned to *harness* plus one child session.

    The child goes through the real ``POST /v1/sessions`` create path so the
    forced-auto decision is exercised, not simulated.

    :param client: Test HTTP client.
    :param db_uri: Database URI for a direct store handle.
    :param agent_name: Agent name to register.
    :param harness: The parent agent's harness, e.g. ``"codex"``.
    :returns: ``(parent_id, child_conversation, conversation_store)``.
    """
    agent = await create_test_agent(
        client,
        name=agent_name,
        executor={"type": "omnigent", "config": {"harness": harness}},
    )
    parent = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert parent.status_code == 201, parent.text
    parent_id = str(parent.json()["id"])
    child = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent_id,
            "title": "audit routing",
        },
    )
    assert child.status_code == 201, child.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    child_conv = conv_store.get_conversation(str(child.json()["id"]))
    assert child_conv is not None
    return parent_id, child_conv, conv_store


@pytest.mark.parametrize(
    ("harness", "picked", "expected_harnesses"),
    [
        ("codex", GPT_MODEL, {"codex"}),
        ("claude-sdk", ROUTED_MODEL, {"claude-sdk"}),
    ],
)
async def test_child_of_a_pinned_parent_is_routed_in_the_parents_family(
    client: httpx.AsyncClient,
    db_uri: str,
    harness: str,
    picked: str,
    expected_harnesses: set[str],
) -> None:
    parent_id, child, conv_store = await _pinned_parent_and_child(
        client,
        db_uri,
        agent_name=f"routing-child-family-{harness}",
        harness=harness,
    )
    # The auto sentinel and its marker belong to Smart Routing sessions only —
    # a child of a pinned parent must carry neither.
    assert child.harness_override != "auto"
    assert AUTO_HARNESS_LABEL_KEY not in child.labels

    routing_client = FakeRoutingClient(RoutingResult(model=picked, rationale="in-family pick"))
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "audit routing"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child.id,
                child,
                body,
                conv_store,
                runner_client,
            )

    assert set(routing_client.offered[0]) == expected_harnesses
    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    assert refreshed.model_override == picked
    assert refreshed.harness_override != "auto"
    decisions = _routing_decisions(conv_store, child.id)
    assert len(decisions) == 1
    assert decisions[0].data.scope == "child_session"
    assert decisions[0].data.harness in expected_harnesses
    # The parent's mirrored copy names the same in-family harness.
    parent_decisions = _routing_decisions(conv_store, parent_id)
    assert [d.data.harness for d in parent_decisions] == [decisions[0].data.harness]


async def test_child_of_an_auto_parent_keeps_cross_harness_candidates(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    agent = await create_test_agent(
        client,
        name="routing-child-family-auto",
        executor={"type": "omnigent", "config": {"harness": "codex"}},
    )
    parent = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "cost_control_mode_override": "on",
            "harness_override": "auto",
        },
    )
    assert parent.status_code == 201, parent.text
    child = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "parent_session_id": parent.json()["id"]},
    )
    assert child.status_code == 201, child.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    child_conv = conv_store.get_conversation(str(child.json()["id"]))
    assert child_conv is not None
    # A Smart Routing parent still hands its children the auto treatment.
    assert child_conv.harness_override == "auto"
    assert child_conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"

    routing_client = FakeRoutingClient(RoutingResult(model=ROUTED_MODEL, rationale="big task"))
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "rewrite the router"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child_conv.id,
                child_conv,
                body,
                conv_store,
                runner_client,
            )

    assert set(routing_client.offered[0]) == {"claude-sdk", "codex", "pi"}


# ── Session warnings channel ───────────────────────────────────────


async def _post_routing_unenforced_warning(client: httpx.AsyncClient, session_id: str) -> None:
    """Post the warning a codex forwarder sends when the canary never fired.

    :param client: Test HTTP client.
    :param session_id: Session to post the warning for.
    """
    posted = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_warning",
            "data": {
                "warnings": [
                    {
                        "code": "subagent_routing_unenforced",
                        "harness": "codex-native",
                        "reason": "SessionStart canary did not fire",
                    }
                ]
            },
        },
    )
    assert posted.status_code in (200, 201, 202), posted.text


async def _snapshot_warning_codes(client: httpx.AsyncClient, session_id: str) -> list[str]:
    """Return the warning codes the session snapshot exposes.

    :param client: Test HTTP client.
    :param session_id: Session to read.
    :returns: ``code`` of each warning on the snapshot.
    """
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    return [w["code"] for w in snapshot.json()["warnings"]]


_UNENFORCED_WARNING = {
    "code": "subagent_routing_unenforced",
    "harness": "codex-native",
    "reason": "SessionStart canary did not fire",
}


@pytest.mark.parametrize(
    ("case", "cost_control", "subagent_routing", "expected"),
    [
        # Routing on, explicitly or inherited from the cost-control switch ⇒
        # the banner is published with every field the watcher sent intact.
        ("explicit-on", None, "on", [_UNENFORCED_WARNING]),
        ("inherit-on", "on", None, [_UNENFORCED_WARNING]),
        ("explicit-on-beats-cost-off", "off", "on", [_UNENFORCED_WARNING]),
        # Routing off ⇒ no banner, even though the canary never fired. Hooks
        # and the router advertisement are installed on every native session
        # (so the setting can be toggled mid-session), so the runner-side
        # watcher posts this warning regardless. A session that never asked
        # for subagent routing must not be told routing is unenforced.
        ("routing-off", None, None, []),
    ],
)
async def test_routing_unenforced_warning_visibility_follows_the_setting(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    cost_control: str | None,
    subagent_routing: str | None,
    expected: list[dict[str, str]],
) -> None:
    session_id = await _session_with_routing_flags(
        client,
        agent_name=f"routing-warn-{case}",
        cost_control=cost_control,
        subagent_routing=subagent_routing,
    )
    await _post_routing_unenforced_warning(client, session_id)
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["warnings"] == expected


async def test_routing_unenforced_warning_follows_a_mid_session_toggle(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The banner appears on toggle-on and clears on toggle-off, with no re-post.

    Clearing semantics: the recorded warning is a durable observation
    ("the hooks never ran"); whether it is *shown* is re-derived from the
    session's current effective routing state every time a snapshot is
    built. So a user who turns routing on sees it without waiting for the
    watcher's next post, and a user who turns it back off stops seeing it.
    """
    session_id = await _session_with_routing_flags(client, agent_name="routing-warn-toggle")
    await _post_routing_unenforced_warning(client, session_id)
    assert await _snapshot_warning_codes(client, session_id) == []

    on = await client.patch(f"/v1/sessions/{session_id}", json={"subagent_routing_override": "on"})
    assert on.status_code == 200, on.text
    # No second event was posted — the toggle alone reveals the warning.
    assert await _snapshot_warning_codes(client, session_id) == ["subagent_routing_unenforced"]

    off = await client.patch(
        f"/v1/sessions/{session_id}", json={"subagent_routing_override": "off"}
    )
    assert off.status_code == 200, off.text
    assert await _snapshot_warning_codes(client, session_id) == []


async def test_subagent_warning_visibility_inherits_the_parent_setting(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A sub-agent with no override of its own follows its parent's mode."""
    agent = await create_test_agent(client, name="routing-warn-child")
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert created.status_code == 201, created.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="reviewer:auth",
        parent_conversation_id=created.json()["id"],
        agent_id=agent["id"],
    )

    await _post_routing_unenforced_warning(client, child.id)
    assert await _snapshot_warning_codes(client, child.id) == ["subagent_routing_unenforced"]

    # The child's own explicit "off" wins over the inherited "on".
    conv_store.update_conversation(child.id, subagent_routing_override="off")
    assert await _snapshot_warning_codes(client, child.id) == []


async def test_unknown_warning_codes_are_dropped_at_the_boundary(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Only codes the UI can render are stored; the rest never reach a snapshot."""
    session_id = await _session_with_routing_flags(client, agent_name="routing-warn-other")
    posted = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_warning",
            "data": {"warnings": [{"code": "some_other_condition", "harness": "codex-native"}]},
        },
    )
    assert posted.status_code in (200, 201, 202), posted.text
    assert await _snapshot_warning_codes(client, session_id) == []


async def test_a_relayed_spawn_leaves_the_warning_for_its_publisher(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A relay must not clear: it cannot tell which check raised the warning.

    The code carries two verdicts — "the hooks never ran" (which a relay
    does disprove) and "a spawn started on a model the router never
    approved" (which it does not, and which is *itself* produced by a
    relayed spawn). Clearing here wiped the second permanently, because
    its publisher only re-posts on a transition. So the publisher owns the
    clear and the banner stays up until its next check reports repair.
    """
    session_id = await _session_with_routing_flags(
        client, agent_name="routing-warn-hook", subagent_routing="on"
    )
    await _post_routing_unenforced_warning(client, session_id)
    assert await _snapshot_warning_codes(client, session_id) == ["subagent_routing_unenforced"]

    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="deep reasoning", harness="claude_code")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        relayed = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
    assert relayed.status_code == 200, relayed.text
    assert await _snapshot_warning_codes(client, session_id) == ["subagent_routing_unenforced"]

    # The publisher's own "nothing is wrong" post is what takes it down.
    cleared = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_warning", "data": {"warnings": []}},
    )
    assert cleared.status_code in (200, 201, 202), cleared.text
    assert await _snapshot_warning_codes(client, session_id) == []


async def test_an_empty_warning_post_clears_the_banner(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The publisher reports what it still sees, so an empty list is a repair."""
    session_id = await _session_with_routing_flags(
        client, agent_name="routing-warn-clear", subagent_routing="on"
    )
    await _post_routing_unenforced_warning(client, session_id)
    assert await _snapshot_warning_codes(client, session_id) == ["subagent_routing_unenforced"]

    cleared = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_warning", "data": {"warnings": []}},
    )
    assert cleared.status_code in (200, 201, 202), cleared.text
    assert await _snapshot_warning_codes(client, session_id) == []


# ── 8. Truthful record on a claude-native turn ─────────────────────


async def _claude_native_session(
    client: httpx.AsyncClient,
    db_uri: str,
    *,
    agent_name: str,
) -> tuple[Any, SqlAlchemyConversationStore]:
    from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT

    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "cost_control_mode_override": "on",
            "labels": {"omnigent.wrapper": CLAUDE_NATIVE_CODING_AGENT.wrapper_label},
        },
    )
    assert resp.status_code == 201, resp.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.get_conversation(resp.json()["id"])
    assert conv is not None
    return conv, conv_store


async def _route_one_turn(conv: Any, conv_store: SqlAlchemyConversationStore) -> Any:
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model=ROUTED_MODEL, rationale="deep refactor", harness="claude_code")
        )
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                conv.id,
                conv,
                body,
                conv_store,
                runner_client,
            )
    decisions = _routing_decisions(conv_store, conv.id)
    assert len(decisions) == 1
    return decisions[0].data


async def test_turn_decision_is_not_applied_when_the_pane_cannot_speak_the_model(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The pane's picker has no entry for the routed id, so the record says so."""
    conv, conv_store = await _claude_native_session(
        client, db_uri, agent_name="routing-honest-record"
    )
    # The workspace moved ``opus`` on to the next generation, so ``/model``
    # would land on opus-5 while the record claimed opus-4-8.
    orchestration_module._model_options_cache[conv.id] = [
        {"id": "opus", "model": "databricks-claude-opus-5"},
        {"id": "sonnet", "model": "databricks-claude-sonnet-5"},
    ]
    try:
        data = await _route_one_turn(conv, conv_store)
    finally:
        orchestration_module._model_options_cache.pop(conv.id, None)

    assert data.model == ROUTED_MODEL
    assert data.applied is False
    assert "Not applied" in data.rationale
    assert "deep refactor" in data.rationale
    # A model the pane cannot switch to is never pinned: the pin is what
    # disables routing for every later turn and misattributes usage.
    refreshed = conv_store.get_conversation(conv.id)
    assert refreshed is not None
    assert refreshed.model_override is None


async def test_turn_decision_stays_applied_when_the_pane_can_speak_the_model(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The launch pinned the routed id into the custom slot, so it applies."""
    conv, conv_store = await _claude_native_session(
        client, db_uri, agent_name="routing-honest-record-ok"
    )
    orchestration_module._model_options_cache[conv.id] = [
        {"id": "opus", "model": "databricks-claude-opus-5"},
        {"id": "sonnet_5", "model": ROUTED_MODEL},
    ]
    try:
        data = await _route_one_turn(conv, conv_store)
    finally:
        orchestration_module._model_options_cache.pop(conv.id, None)

    assert data.model == ROUTED_MODEL
    assert data.applied is True
    assert data.rationale == "deep refactor"
    refreshed = conv_store.get_conversation(conv.id)
    assert refreshed is not None
    assert refreshed.model_override == ROUTED_MODEL


async def test_turn_decision_is_left_alone_with_no_known_vocabulary(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """No picker rows cached yet: the launch env is the only authority."""
    conv, conv_store = await _claude_native_session(
        client, db_uri, agent_name="routing-honest-record-unknown"
    )
    orchestration_module._model_options_cache.pop(conv.id, None)

    data = await _route_one_turn(conv, conv_store)

    assert data.applied is True


async def _native_child(
    client: httpx.AsyncClient,
    db_uri: str,
    *,
    agent_name: str,
) -> tuple[Any, SqlAlchemyConversationStore]:
    """Create a claude-native sub-agent under a Smart Routing parent.

    :param client: Test HTTP client.
    :param db_uri: Store URI for the conversation store.
    :param agent_name: Agent name to create the parent session with.
    :returns: The child conversation row and the store it lives in.
    """
    from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT

    agent = await create_test_agent(client, name=agent_name)
    parent = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert parent.status_code == 201, parent.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="reviewer:auth",
        parent_conversation_id=parent.json()["id"],
        agent_id=agent["id"],
    )
    conv_store.set_labels(
        child.id,
        {
            "omnigent.ui": "terminal",
            "omnigent.wrapper": CLAUDE_NATIVE_CODING_AGENT.wrapper_label,
        },
    )
    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    return refreshed, conv_store


async def _dispatch_native_turn(
    conv: Any,
    conv_store: SqlAlchemyConversationStore,
    *,
    model: str,
) -> tuple[Any, list[dict[str, Any]]]:
    """Send one web message into a native pane with the router returning *model*.

    :param conv: Conversation row to dispatch for (labels must be current).
    :param conv_store: Store the row lives in.
    :param model: Model the fake router picks for this turn.
    :returns: The last routing-decision item's data and the forwarded bodies.
    """
    forwarded: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": True})

    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model=model, rationale="deep refactor", harness="claude_code")
        )
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    async with httpx.AsyncClient(
        base_url="http://runner.test", transport=httpx.MockTransport(_handler)
    ) as runner_client:
        with (
            patch("omnigent.runtime._globals._caps", new=caps),
            patch(
                "omnigent.server.routes.sessions._get_runner_client",
                new=AsyncMock(return_value=runner_client),
            ),
        ):
            await orchestration_module._dispatch_session_event_to_runner_impl(
                conv.id,
                conv,
                body,
                conv_store,
                runner_client,
                agent_name="reviewer",
                file_store=None,
                artifact_store=None,
            )
    return _routing_decisions(conv_store, conv.id)[-1].data, forwarded


async def test_a_childs_second_spawn_cannot_pin_a_model_the_pane_rejects(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A child routes per spawn, so its 2nd turn is a ``/model`` on a live pane.

    The pane's picker has no entry for the second pick, so ``/model`` would
    be skipped by the executor. Pinning it anyway would misattribute usage
    and (via the pin) lie about what the turn ran on. The first turn is
    different: the pane starts on whatever the row says, so no picker
    spelling has to exist for it.
    """
    child, conv_store = await _native_child(client, db_uri, agent_name="routing-child-second")
    # The pane's vocabulary covers the first pick only.
    orchestration_module._model_options_cache[child.id] = [
        {"id": "opus", "model": ROUTED_MODEL},
    ]
    try:
        first, first_forwards = await _dispatch_native_turn(child, conv_store, model=ROUTED_MODEL)
        assert first.applied is True
        pinned = conv_store.get_conversation(child.id)
        assert pinned is not None
        assert pinned.model_override == ROUTED_MODEL
        assert [f.get("model_override") for f in first_forwards] == [ROUTED_MODEL]

        # Second spawn, unspellable pick: recorded as not applied, and the
        # row keeps the model the pane is actually on.
        second, second_forwards = await _dispatch_native_turn(pinned, conv_store, model=GPT_MODEL)
    finally:
        orchestration_module._model_options_cache.pop(child.id, None)

    assert second.model == GPT_MODEL
    assert second.applied is False
    assert "Not applied" in second.rationale
    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    assert refreshed.model_override == ROUTED_MODEL
    # Nothing was carried in-band either, so the executor never types a
    # ``/model`` the pane would drop.
    assert [f.get("model_override") for f in second_forwards] == [None]


async def test_turn_candidates_come_from_the_panes_own_vocabulary(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The router is only offered models the terminal can be switched onto."""
    conv, conv_store = await _claude_native_session(
        client, db_uri, agent_name="routing-turn-vocabulary"
    )
    orchestration_module._model_options_cache[conv.id] = [
        {"id": "opus", "model": "databricks-claude-opus-5"},
        {"id": "sonnet_5", "model": ROUTED_MODEL},
        {"id": "haiku"},
    ]
    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="deep refactor", harness="claude_code")
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    try:
        with patch(
            "omnigent.runtime._globals._caps",
            new=FakeCaps(routing_client=routing_client),
        ):
            async with echo_runner_client() as runner_client:
                await orchestration_module._forward_event_to_runner(
                    conv.id,
                    conv,
                    body,
                    conv_store,
                    runner_client,
                )
    finally:
        orchestration_module._model_options_cache.pop(conv.id, None)

    assert [sorted(offer.values()) for offer in routing_client.offered] == [
        [["databricks-claude-opus-5", ROUTED_MODEL]]
    ]
