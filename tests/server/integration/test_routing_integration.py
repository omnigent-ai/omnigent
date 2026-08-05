"""L5 integration coverage for intelligent routing with a fake router.

Drives the server's own routing paths against a stub routing client:
the ``sys_session_send`` child override precedence, the native-subagent
relay endpoint (transcript item + child-sessions join), the subagent
fail-mode knob, and the decision cache.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from omnigent.runner import subagent_routing
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


async def test_another_spelling_of_the_routed_model_is_no_override(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """``sys_session_send`` and the router can spell one arm two ways.

    The child path used to compare raw strings, so a GLM ask that routing
    landed exactly was still reported as an overridden attempt.
    """
    _parent, child, conv_store = await _parent_and_child(
        client,
        db_uri,
        agent_name="routing-spelling",
        child_model_override=GLM_SERVABLE,
    )
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model=GLM_MODEL, rationale="delegate down", harness="claude_code")
        )
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "fix the typo"}]},
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

    decisions = _routing_decisions(conv_store, child.id)
    assert len(decisions) == 1
    assert decisions[0].data.model == GLM_MODEL
    assert decisions[0].data.attempted_override is None


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


async def test_the_router_source_reaches_the_transcript_item_and_the_sse(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The chip needs to know which router answered, live and on reload."""
    from omnigent.server.routing_backend import RoutingBackends
    from omnigent.server.smart_routing import ExternalRoutingClient

    _parent, child, conv_store = await _parent_and_child(
        client, db_uri, agent_name="routing-source"
    )
    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="deep refactor", harness="claude_code")
    )
    # Classified as the external client by type, so a gateway-backed route is
    # stamped "databricks-aigw" rather than the judge's source.
    external = ExternalRoutingClient(base_url="https://ws.example.invalid", router_name="task_v1")
    external.route = routing_client.route  # type: ignore[method-assign]
    caps = FakeCaps(routing_client=external, routing_backends=RoutingBackends(external=external))
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

    decisions = _routing_decisions(conv_store, child.id)
    assert len(decisions) == 1
    assert decisions[0].data.router_source == "databricks-aigw"
    chips = [
        payload
        for sid, payload in published
        if sid == child.id and payload.get("item", {}).get("type") == "routing_decision"
    ]
    assert chips and chips[0]["item"]["router_source"] == "databricks-aigw"


# ── 2. Native-subagent relay: transcript item + child join ──────────


async def test_the_subagent_relay_falls_back_to_the_judge_off_the_gateway(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An ungatewayed parent still routes its spawns, with the built-in judge."""
    from omnigent.server.routing_backend import RoutingBackends

    parent, _child, conv_store = await _parent_and_child(
        client, db_uri, agent_name="routing-subagent-source"
    )
    external = FakeRoutingClient(RoutingResult(model=ROUTED_MODEL, rationale="external"))
    local = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="local", harness="claude_code")
    )
    caps = FakeCaps(
        routing_client=external,
        routing_backends=RoutingBackends(external=external, local=local),
    )
    ungatewayed = SimpleNamespace(gateway_inference={"claude-native": False})
    # No runner is bound here, so stand in for the pane's live catalog — the only
    # provider-accurate candidate source once the static table is off the table.
    with (
        patch("omnigent.runtime._globals._caps", new=caps),
        patch.object(
            orchestration_module,
            "_session_routing_host",
            AsyncMock(return_value=ungatewayed),
        ),
        patch.object(
            subagent_routing,
            "candidate_models",
            lambda harness, **kwargs: {harness: [ROUTED_MODEL]},
        ),
    ):
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
    assert resp.json()["router_source"] == "oss-llm"
    # The workspace router's picks are unreachable from this pane; never asked.
    assert external.calls == []
    decisions = _routing_decisions(conv_store, parent["id"])
    assert decisions[-1].data.router_source == "oss-llm"


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
    ("case", "cost_control", "subagent_routing", "routed", "stored"),
    [
        # A Smart Routing create is stamped "on" server-side, so the two-state
        # gate reads one explicit switch and the GET snapshot reports it.
        ("stamped-on", "on", None, True, "on"),
        ("unstamped", None, None, False, None),
        ("override-off", "on", "off", False, "off"),
        ("override-on", None, "on", True, "on"),
    ],
)
async def test_subagent_gate_follows_the_session_setting(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    cost_control: str | None,
    subagent_routing: str | None,
    routed: bool,
    stored: str | None,
) -> None:
    session_id = await _session_with_routing_flags(
        client,
        agent_name=f"routing-gate-{case}",
        cost_control=cost_control,
        subagent_routing=subagent_routing,
    )
    # An explicit "off" sent alongside cost control survives the create stamp:
    # the stamp only fills an unset value.
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["subagent_routing_override"] == stored
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
        # A pinned session is offered its own family only, so no verdict it can
        # receive is ever a cross-harness redirect. `candidate_models` is pinned
        # per harness in tests/server/test_subagent_routing.py; this is the
        # route-level half — that the endpoint really passes cross_harness=False.
        assert set(routing_client.offered[0]) == {"claude-native"}
    else:
        # Allowed unchanged, router untouched, and no transcript spam.
        assert decision["action"] == "allow"
        assert decision["model"] is None
        assert decision["rationale"] == DISABLED_RATIONALE
        assert routing_client.calls == []
        assert _routing_decisions(conv_store, session_id) == []


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

    # Explicit null clears the stored value; the session then reads as Default.
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


@pytest.mark.parametrize(
    ("case", "start", "flip_to"),
    [
        # Default → Smart Routing: the very next spawn routes.
        ("off-to-on", None, "on"),
        # Smart Routing → Default: the next spawn stops routing, and the stored
        # value wins whether it was stamped at create or flipped by hand. This
        # is the direction a child of a routed parent needs — its inherited
        # stamp is not special, only stored.
        ("on-to-off", "on", "off"),
    ],
)
async def test_flipping_the_setting_mid_session_takes_effect_on_the_next_spawn(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    start: str | None,
    flip_to: str,
) -> None:
    session_id = await _session_with_routing_flags(
        client,
        agent_name=f"routing-gate-midsession-{case}",
        subagent_routing=start,
    )
    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="deep reasoning", harness="claude_code")
    )
    routes_before = 1 if start == "on" else 0
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        before = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
        assert before.json()["action"] == ("rewrite" if start == "on" else "allow")
        assert len(routing_client.calls) == routes_before
        patched = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"subagent_routing_override": flip_to},
        )
        assert patched.status_code == 200, patched.text
        after = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
    if flip_to == "on":
        assert after.json()["action"] == "rewrite"
        assert after.json()["model"] == ROUTED_MODEL
        assert len(routing_client.calls) == routes_before + 1
    else:
        assert after.json()["action"] == "allow"
        assert after.json()["rationale"] == DISABLED_RATIONALE
        # The router is not asked again — declining costs no round trip.
        assert len(routing_client.calls) == routes_before


# ── 6. Harness-family constraint on the candidate set ──────────────


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


@pytest.mark.parametrize("labelled", [True, False], ids=["labelled", "sentinel-only"])
async def test_auto_session_and_its_children_keep_cross_harness_picks(
    client: httpx.AsyncClient,
    db_uri: str,
    labelled: bool,
) -> None:
    """The auto sentinel alone is enough; the durable label is not required.

    The label and the sentinel are written by different create paths, so the
    endpoint must read both through ``auto_harness_session`` — otherwise a
    session carrying only the sentinel is silently pinned to one family.
    """
    agent = await create_test_agent(client, name=f"routing-family-auto-{labelled}")
    body: dict[str, Any] = {
        "agent_id": agent["id"],
        "cost_control_mode_override": "on",
        "subagent_routing_override": "on",
    }
    # Only the labelled shape asks the create route for the auto harness — that
    # route is what writes the durable label. The sentinel-only shape sets the
    # override afterwards, which is how the other create paths leave a session.
    if labelled:
        body["harness_override"] = "auto"
    created = await client.post("/v1/sessions", json=body)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    if not labelled:
        sentinel_only = conv_store.update_conversation(session_id, harness_override="auto")
        assert sentinel_only is not None
        assert sentinel_only.harness_override == "auto"
        assert AUTO_HARNESS_LABEL_KEY not in (sentinel_only.labels or {})
    else:
        labelled_conv = conv_store.get_conversation(session_id)
        assert labelled_conv is not None
        assert labelled_conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="reviewer:auth",
        parent_conversation_id=session_id,
        agent_id=agent["id"],
    )
    # Created through the store, so it misses the create route's stamp; apply the
    # value that route would have copied from the routed parent.
    conv_store.update_conversation(child.id, subagent_routing_override="on")

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


@pytest.mark.parametrize(
    ("case", "executor_config", "create_body", "picked", "prompt"),
    [
        # The client asked for the auto harness outright.
        (
            "client-sentinel",
            {"harness": "codex"},
            {"cost_control_mode_override": "on", "harness_override": "auto"},
            ROUTED_MODEL,
            "rewrite the router",
        ),
        # The spec opted itself in: a brain pinned to claude-sdk used to clamp
        # every child into the claude family, so a codex sub-agent was rerouted
        # onto claude-sdk. `smart_routing_harness: auto` starts the parent auto
        # with no harness_override in the request at all.
        (
            "spec-opt-in",
            {"harness": "claude-sdk", "smart_routing_harness": "auto"},
            {"cost_control_mode_override": "on"},
            GPT_MODEL,
            "compare the options",
        ),
    ],
)
async def test_child_of_an_auto_parent_keeps_cross_harness_candidates(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    executor_config: dict[str, Any],
    create_body: dict[str, Any],
    picked: str,
    prompt: str,
) -> None:
    """However the parent became auto, its children are routed against every family.

    ``allowed_family=None``: every family is on offer, so a codex head can stay
    on codex instead of being clamped into the brain's claude family.
    """
    agent = await create_test_agent(
        client,
        name=f"routing-child-family-{case}",
        executor={"type": "omnigent", "config": executor_config},
    )
    parent = await client.post("/v1/sessions", json={"agent_id": agent["id"], **create_body})
    assert parent.status_code == 201, parent.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent_conv = conv_store.get_conversation(str(parent.json()["id"]))
    assert parent_conv is not None
    assert parent_conv.harness_override == "auto"
    assert parent_conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"

    child = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "parent_session_id": parent.json()["id"]},
    )
    assert child.status_code == 201, child.text
    child_conv = conv_store.get_conversation(str(child.json()["id"]))
    assert child_conv is not None
    # A Smart Routing parent still hands its children the auto treatment.
    assert child_conv.harness_override == "auto"
    assert child_conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"

    routing_client = FakeRoutingClient(RoutingResult(model=picked, rationale="big task"))
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": prompt}]},
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


# ── 7b. The subagent-routing switch is the child-spawn gate ────────


async def _auto_parent_and_child(
    client: httpx.AsyncClient,
    db_uri: str,
    *,
    agent_name: str,
    cost_control: str | None,
    subagent_routing: str | None,
) -> tuple[str, Any, SqlAlchemyConversationStore]:
    """Create an auto-harness parent with explicit switches, plus one child.

    The child goes through the real ``POST /v1/sessions`` create path, so the
    forced-auto decision and the create stamp are exercised rather than faked.

    :param client: Test HTTP client.
    :param db_uri: Database URI for a direct store handle.
    :param agent_name: Agent name to register.
    :param cost_control: ``cost_control_mode_override`` for the parent.
    :param subagent_routing: ``subagent_routing_override`` for the parent.
    :returns: ``(parent_id, child_conversation, conversation_store)``.
    """
    agent = await create_test_agent(
        client,
        name=agent_name,
        executor={"type": "omnigent", "config": {"harness": "codex"}},
    )
    body: dict[str, Any] = {"agent_id": agent["id"], "harness_override": "auto"}
    if cost_control is not None:
        body["cost_control_mode_override"] = cost_control
    if subagent_routing is not None:
        body["subagent_routing_override"] = subagent_routing
    parent = await client.post("/v1/sessions", json=body)
    assert parent.status_code == 201, parent.text
    parent_id = str(parent.json()["id"])
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent_conv = conv_store.get_conversation(parent_id)
    assert parent_conv is not None
    # The parent really is an auto-harness session either way, so the switch is
    # the only thing that differs between the cases below.
    assert parent_conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"
    assert parent_conv.subagent_routing_override == subagent_routing

    child = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "parent_session_id": parent_id, "title": "audit routing"},
    )
    assert child.status_code == 201, child.text
    child_conv = conv_store.get_conversation(str(child.json()["id"]))
    assert child_conv is not None
    return parent_id, child_conv, conv_store


@pytest.mark.parametrize(
    ("case", "cost_control", "subagent_routing", "routed"),
    [
        # The switch says off, so the parent's own routing must not leak into its
        # spawns — the child stays on the harness it was created with.
        ("cc-on-switch-off", "on", "off", False),
        # Preservation: the ordinary Smart Routing session, switch left stamped.
        ("cc-on-switch-on", "on", "on", True),
        # The switch alone drives spawns: the parent's own turns aren't routed,
        # but its children are.
        ("cc-off-switch-on", None, "on", True),
    ],
)
async def test_child_spawn_gate_follows_the_parents_subagent_switch(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    cost_control: str | None,
    subagent_routing: str | None,
    routed: bool,
) -> None:
    _parent_id, child, conv_store = await _auto_parent_and_child(
        client,
        db_uri,
        agent_name=f"routing-child-switch-{case}",
        cost_control=cost_control,
        subagent_routing=subagent_routing,
    )
    # The forced-auto decision at create.
    if routed:
        assert child.harness_override == "auto"
        assert child.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"
    else:
        assert child.harness_override != "auto"
        assert AUTO_HARNESS_LABEL_KEY not in child.labels

    routing_client = FakeRoutingClient(RoutingResult(model=ROUTED_MODEL, rationale="big task"))
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "rewrite the router"}]},
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

    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    if routed:
        assert len(routing_client.calls) == 1
        assert refreshed.model_override == ROUTED_MODEL
        assert len(_routing_decisions(conv_store, child.id)) == 1
    else:
        assert routing_client.calls == []
        assert refreshed.model_override is None
        assert _routing_decisions(conv_store, child.id) == []


@pytest.mark.parametrize(
    ("case", "cost_control", "subagent_routing", "stamped"),
    [
        ("cc-on-switch-off", "on", "off", None),
        ("cc-off-switch-on", None, "on", "on"),
    ],
)
async def test_child_create_stamp_inherits_the_parents_switch_not_its_cost_control(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    cost_control: str | None,
    subagent_routing: str | None,
    stamped: str | None,
) -> None:
    """The child carries its own copy of the parent's *switch*.

    The gate never re-reads the parent, so what the stamp copies decides whether
    a grandchild spawn routes. Copying the parent's cost-control mode instead
    would revive the switch the UI no longer exposes.
    """
    _parent_id, child, _conv_store = await _auto_parent_and_child(
        client,
        db_uri,
        agent_name=f"routing-child-stamp-{case}",
        cost_control=cost_control,
        subagent_routing=subagent_routing,
    )
    assert child.subagent_routing_override == stamped


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
