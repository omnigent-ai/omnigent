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
from omnigent.server.routes._sessions.common import get_server_host_registry
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
# Uuid-shaped so the host registry's canonical keying accepts it.
_GATEWAY_HOST_ID = "bb22cc33dd44ee55ff66778899001122"

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


def _host_reporting_gateway(gateway: dict[str, bool]) -> Any:  # type: ignore[explicit-any]
    """A host row whose gateway-backing map is already reported to the server.

    Gateway backing never reaches the database: the host delivers it on its
    connect handshake and ``create_app`` publishes the receiving registry, so a
    test stands a host up by recording into that registry.

    :param gateway: Harness spelling → gateway-backed flag to report.
    :returns: A host-row stand-in carrying the id the map was recorded under.
    """
    registry = get_server_host_registry()
    assert registry is not None, "create_app publishes the host registry"
    registry.record_gateway_inference(_GATEWAY_HOST_ID, gateway)
    return SimpleNamespace(host_id=_GATEWAY_HOST_ID)


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
    # Gateway backing is read from the live registry, filled by the host's
    # connect handshake — stand in for a host that reported claude off-gateway.
    ungatewayed = _host_reporting_gateway({"claude-native": False})
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


@pytest.mark.parametrize(
    ("case", "picked", "verdict_harness", "applied"),
    [
        # The reported verdict: the external router named codex + a gpt arm for
        # a child spawned onto pi. Nothing offered runs it, so the chip declines.
        ("cross-family-declines", GPT_MODEL, "codex", False),
        # An in-family pick is applied normally.
        ("in-family-applies", ROUTED_MODEL, "pi", True),
    ],
)
async def test_named_worker_child_of_an_auto_parent_stays_on_its_own_harness(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    picked: str,
    verdict_harness: str,
    applied: bool,
) -> None:
    """A spawn that named a worker keeps that worker's harness.

    The polly shape: a Smart Routing brain whose ``pi`` sub-agent declares its
    own harness. The spawn pins the CLI the child boots on, so the router is
    offered pi alone — it used to be handed every family, and a codex verdict
    was written to the row and stamped "applied" on a pane running pi.
    """
    agent = await create_test_agent(
        client,
        name=f"routing-child-named-worker-{case}",
        executor={
            "type": "omnigent",
            "config": {"harness": "claude-sdk", "smart_routing_harness": "auto"},
        },
        sub_agents=[{"name": "pi", "executor": {"type": "omnigent", "config": {"harness": "pi"}}}],
    )
    parent = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert parent.status_code == 201, parent.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent_conv = conv_store.get_conversation(str(parent.json()["id"]))
    assert parent_conv is not None
    assert parent_conv.harness_override == "auto"

    child = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent.json()["id"],
            "sub_agent_name": "pi",
            "title": "pi:joke-pi",
        },
    )
    assert child.status_code == 201, child.text
    child_conv = conv_store.get_conversation(str(child.json()["id"]))
    assert child_conv is not None
    # The named worker is not handed the auto sentinel: its harness is decided.
    assert child_conv.harness_override != "auto"
    assert AUTO_HARNESS_LABEL_KEY not in child_conv.labels

    routing_client = FakeRoutingClient(
        RoutingResult(model=picked, rationale="cheap and fast", harness=verdict_harness)
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "tell me a joke"}]},
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

    # Only the child's own harness was on offer.
    assert set(routing_client.offered[0]) == {"pi"}
    # And nothing cross-family lands: the row never moves off pi, and the chip
    # is an in-family pick or an honest decline — never another family stamped
    # "applied" over a pane that is still running pi.
    refreshed = conv_store.get_conversation(child_conv.id)
    assert refreshed is not None
    assert refreshed.harness_override in (None, "pi")
    decisions = _routing_decisions(conv_store, child_conv.id)
    assert len(decisions) == 1
    assert decisions[0].data.scope == "child_session"
    assert decisions[0].data.harness in (None, "pi")
    assert decisions[0].data.applied is applied
    if applied:
        assert refreshed.model_override == decisions[0].data.model
    else:
        assert decisions[0].data.model == "unavailable"
        assert refreshed.model_override is None


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


# ── 7c. A pinned parent cannot create an out-of-family child ───────


async def _pinned_parent(
    client: httpx.AsyncClient,
    *,
    agent_name: str,
    harness: str,
    routing_on: bool = True,
) -> str:
    """Create a parent pinned to *harness*, with Smart Routing on or off.

    :param client: Test HTTP client.
    :param agent_name: Agent name to register.
    :param harness: The parent agent's harness, e.g. ``"codex"``.
    :param routing_on: Whether the parent routes its spawns.
    :returns: The parent session id.
    """
    agent = await create_test_agent(
        client,
        name=agent_name,
        executor={"type": "omnigent", "config": {"harness": harness}},
    )
    body: dict[str, Any] = {"agent_id": agent["id"]}
    if routing_on:
        body["cost_control_mode_override"] = "on"
    parent = await client.post("/v1/sessions", json=body)
    assert parent.status_code == 201, parent.text
    return str(parent.json()["id"])


async def _create_child(
    client: httpx.AsyncClient,
    parent_id: str,
    *,
    agent_name: str,
    harness: str,
) -> httpx.Response:
    """POST a child create bound to an agent running *harness*.

    :param client: Test HTTP client.
    :param parent_id: The parent session id.
    :param agent_name: Agent name to register for the child.
    :param harness: The child agent's harness, e.g. ``"claude-sdk"``.
    :returns: The raw create response.
    """
    child_agent = await create_test_agent(
        client,
        name=agent_name,
        executor={"type": "omnigent", "config": {"harness": harness}},
    )
    return await client.post(
        "/v1/sessions",
        json={"agent_id": child_agent["id"], "parent_session_id": parent_id},
    )


async def test_pinned_parent_cannot_create_a_child_in_another_family(
    client: httpx.AsyncClient,
) -> None:
    parent_id = await _pinned_parent(
        client, agent_name="family-gate-pinned-parent", harness="codex"
    )
    child = await _create_child(
        client, parent_id, agent_name="family-gate-claude-child", harness="claude-sdk"
    )
    assert child.status_code == 400, child.text
    assert "model family" in child.text


async def test_pinned_parent_still_creates_children_in_its_own_family(
    client: httpx.AsyncClient,
) -> None:
    parent_id = await _pinned_parent(
        client, agent_name="family-gate-same-family-parent", harness="codex"
    )
    child = await _create_child(
        client, parent_id, agent_name="family-gate-codex-child", harness="codex"
    )
    assert child.status_code == 201, child.text


async def test_auto_parent_may_create_a_child_in_another_family(
    client: httpx.AsyncClient,
) -> None:
    agent = await create_test_agent(
        client,
        name="family-gate-auto-parent",
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
    child = await _create_child(
        client,
        str(parent.json()["id"]),
        agent_name="family-gate-auto-claude-child",
        harness="claude-sdk",
    )
    assert child.status_code == 201, child.text


async def test_plain_parent_may_create_a_child_in_another_family(
    client: httpx.AsyncClient,
) -> None:
    parent_id = await _pinned_parent(
        client,
        agent_name="family-gate-plain-parent",
        harness="codex",
        routing_on=False,
    )
    child = await _create_child(
        client, parent_id, agent_name="family-gate-plain-claude-child", harness="claude-sdk"
    )
    assert child.status_code == 201, child.text


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
    routing_client: Any = None,  # type: ignore[explicit-any]
) -> tuple[Any, list[dict[str, Any]]]:
    """Send one web message into a native pane with the router returning *model*.

    :param conv: Conversation row to dispatch for (labels must be current).
    :param conv_store: Store the row lives in.
    :param model: Model the fake router picks for this turn.
    :param routing_client: Routing-client double to use, so a caller can read
        back what was offered. ``None`` builds one returning *model*.
    :returns: The last routing-decision item's data and the forwarded bodies.
    """
    forwarded: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": True})

    caps = FakeCaps(
        routing_client=routing_client
        if routing_client is not None
        else FakeRoutingClient(
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


async def _parent_with_native_child_pane(
    client: httpx.AsyncClient,
    db_uri: str,
    *,
    agent_name: str,
    parent_harness: str,
    auto: bool,
) -> tuple[str, Any, SqlAlchemyConversationStore]:
    """A routing-on parent plus a claude-native sub-agent pane under it.

    The pane is the shape a ``sys_session_create`` spawn produces when the
    orchestrator names another family's wrapper agent: a native terminal child
    whose harness was never routed. Live example: a pinned ``codex-native``
    session that spawned a ``claude-native-ui`` child.

    :param client: Test HTTP client.
    :param db_uri: Database URI for a direct store handle.
    :param agent_name: Agent name to register.
    :param parent_harness: The parent agent's harness, e.g. ``"codex"``.
    :param auto: ``True`` to start the parent in Smart Routing (auto) harness
        mode, the only mode allowed cross-family spawns.
    :returns: ``(parent_id, child_conversation, conversation_store)``.
    """
    from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT

    agent = await create_test_agent(
        client,
        name=agent_name,
        executor={"type": "omnigent", "config": {"harness": parent_harness}},
    )
    # The spawned child is bound to another family's native wrapper, exactly as
    # a ``sys_session_create`` that named ``claude-native-ui`` leaves it.
    child_agent = await create_test_agent(
        client,
        name=f"{agent_name}-claude-pane",
        executor={"type": "omnigent", "config": {"harness": "claude-native"}},
    )
    parent = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "cost_control_mode_override": "on",
            **({"harness_override": "auto"} if auto else {}),
        },
    )
    assert parent.status_code == 201, parent.text
    parent_id = str(parent.json()["id"])
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent_conv = conv_store.get_conversation(parent_id)
    assert parent_conv is not None
    # Both cases route their spawns; only the auto one may leave its family.
    assert parent_conv.subagent_routing_override == "on"
    assert (parent_conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1") is auto

    child = conv_store.create_conversation(
        kind="sub_agent",
        title="claude-native-ui:say hello",
        parent_conversation_id=parent_id,
        agent_id=child_agent["id"],
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
    return parent_id, refreshed, conv_store


async def test_a_pinned_parents_child_pane_is_never_routed_onto_another_family(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A pinned codex parent's ``child_session`` route cannot land a claude arm.

    Cross-family spawns are for auto-harness sessions only. The in-harness
    spawn gate already held that line (``candidate_models(cross_harness=False)``)
    but the child-session path did not: a live pinned codex session spawned a
    ``claude-native-ui`` child and the router happily pinned it to
    ``claude-sonnet-5``. Now nothing is routed and the chip says why.
    """
    _parent_id, child, conv_store = await _parent_with_native_child_pane(
        client,
        db_uri,
        agent_name="routing-child-pane-pinned",
        parent_harness="codex",
        auto=False,
    )
    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="escalate", harness="claude_code")
    )

    data, forwarded = await _dispatch_native_turn(
        child, conv_store, model=ROUTED_MODEL, routing_client=routing_client
    )

    # The router is never even asked: this pane has no candidate the parent's
    # family can serve.
    assert routing_client.calls == []
    assert data.scope == "child_session"
    assert data.applied is False
    assert data.model == UNAVAILABLE_MODEL
    assert "stay in its own model family" in data.rationale
    assert "claude-native" in data.rationale
    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    # Nothing pinned, nothing switched in-band, and the route-once label is
    # left unclaimed — the pane runs on its CLI's own model.
    assert refreshed.model_override is None
    assert not refreshed.labels.get(ROUTING_DECISION_LABEL_KEY)
    assert [f.get("model_override") for f in forwarded] == [None]
    # The spawn still ran: the message reached the terminal.
    assert forwarded


async def test_an_auto_parents_child_pane_still_routes_across_families(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The same claude pane under an auto-harness parent is routed as before.

    Smart Routing owns an auto session's harness, so its spawns may cross
    families — the one case the rule above allows.
    """
    _parent_id, child, conv_store = await _parent_with_native_child_pane(
        client,
        db_uri,
        agent_name="routing-child-pane-auto",
        parent_harness="codex",
        auto=True,
    )
    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="escalate", harness="claude_code")
    )

    data, forwarded = await _dispatch_native_turn(
        child, conv_store, model=ROUTED_MODEL, routing_client=routing_client
    )

    assert len(routing_client.calls) == 1
    # Offered this pane's own family only: it is a claude terminal, whatever
    # the router may pick for a session that has no terminal yet.
    assert set(routing_client.offered[0]) == {"claude-native"}
    assert data.scope == "child_session"
    assert data.applied is True
    assert data.model == ROUTED_MODEL
    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    assert refreshed.model_override == ROUTED_MODEL
    assert [f.get("model_override") for f in forwarded] == [ROUTED_MODEL]


async def test_a_pane_still_on_the_auto_sentinel_is_routed_in_its_own_family(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The pane's terminal decides its family, not its unresolved sentinel.

    A forced-auto child keeps ``harness_override="auto"`` until its first
    message routes, and the sentinel carries no family — so the pane was
    offered every model its gateway serves and could be pinned to one its
    running CLI cannot speak.
    """
    _parent_id, child, conv_store = await _parent_with_native_child_pane(
        client,
        db_uri,
        agent_name="routing-child-pane-sentinel",
        parent_harness="codex",
        auto=True,
    )
    sentinel = conv_store.update_conversation(child.id, harness_override="auto")
    assert sentinel is not None
    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="escalate", harness="claude_code")
    )

    data, _forwarded = await _dispatch_native_turn(
        sentinel, conv_store, model=ROUTED_MODEL, routing_client=routing_client
    )

    assert set(routing_client.offered[0]) == {"claude-native"}
    assert all(
        "claude" in model for models in routing_client.offered[0].values() for model in models
    )
    assert data.applied is True


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


# ── 10. A routed child's harness is chosen once, not per message ─────


class _SequenceRoutingClient:
    """Routing-client double returning a different verdict per call.

    :param results: One verdict per call, in order; the last repeats.
    :ivar calls: ``(message, offered_models)`` per :meth:`route` call.
    """

    def __init__(self, *results: RoutingResult) -> None:
        self._results = list(results)
        self.last_error: str | None = None
        self.calls: list[tuple[str, dict[str, list[str]]]] = []

    async def route(
        self, message: str, available_models: dict[str, list[str]]
    ) -> RoutingResult | None:
        """Record the offer and return this call's verdict."""
        self.calls.append((message, dict(available_models)))
        index = min(len(self.calls) - 1, len(self._results) - 1)
        return self._results[index]


async def _send_child_message(
    child: Any,
    conv_store: SqlAlchemyConversationStore,
    routing_client: Any,
    text: str,
) -> None:
    """Forward one user message for *child* with *routing_client* installed."""
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": text}]},
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


async def test_a_follow_up_message_cannot_flip_a_routed_childs_harness(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child's harness is decided on its first message and never again.

    The child arm of the message-routing gate deliberately routes past an
    orchestrator-supplied model, and its condition
    (``conv.parent_conversation_id is not None``) was therefore true on EVERY
    message: each follow-up re-ran the judge and re-persisted BOTH overrides, so
    a second turn could move the child onto another harness family mid-session
    — contradicting the create-time-only contract the ``harness_override``
    forward documents, and stranding the pane the child was already running in.
    """
    _parent_id, child, conv_store = await _auto_parent_and_child(
        client,
        db_uri,
        agent_name="routing-child-no-harness-flip",
        cost_control="on",
        subagent_routing="on",
    )
    routing_client = _SequenceRoutingClient(
        RoutingResult(model=GPT_MODEL, rationale="first", harness="codex"),
        RoutingResult(model=ROUTED_MODEL, rationale="second", harness="claude_code"),
    )

    await _send_child_message(child, conv_store, routing_client, "audit routing")

    after_first = conv_store.get_conversation(child.id)
    assert after_first is not None
    assert len(routing_client.calls) == 1
    assert after_first.model_override == GPT_MODEL
    first_harness = after_first.harness_override
    assert first_harness not in (None, "auto")
    assert after_first.labels.get(ROUTING_DECISION_LABEL_KEY)

    # A follow-up on the SAME child, with the router now preferring the other
    # family. Nothing may move.
    await _send_child_message(after_first, conv_store, routing_client, "and now the tests")

    after_second = conv_store.get_conversation(child.id)
    assert after_second is not None
    assert len(routing_client.calls) == 1, "the follow-up must not re-run the judge"
    assert after_second.harness_override == first_harness
    assert after_second.model_override == GPT_MODEL
    assert len(_routing_decisions(conv_store, child.id)) == 1


# ── A routing OUTAGE never costs the user their turn ────────────────
#
# The owner's ruling: routing must not be blocking when the call fails. These
# drive a raising routing client through each real dispatch path and assert two
# things every time — the work still happens, and the failure is visible as a
# declined decision rather than an error.


#: What a routing outage is recorded as. Not a model, so nothing can be pinned
#: from it; the same placeholder the auto-harness path already used.
UNAVAILABLE_MODEL = "unavailable"


def _outage_caps(exc: Exception) -> Any:  # type: ignore[explicit-any]
    """
    Caps whose routing client raises *exc* on every call.

    :param exc: The failure to raise, e.g. ``httpx.ReadTimeout("...")``.
    :returns: A ``FakeCaps`` carrying the raising client.
    """
    return FakeCaps(routing_client=FakeRoutingClient(None, error=exc))


#: The five failure modes a routing call can present. A gateway 500 and a 401
#: arrive as ``HTTPStatusError``, a wedged router as a read timeout, a garbled
#: body as a decode error, and an unreachable relay as a connect error.
ROUTING_FAILURES: list[tuple[str, Exception]] = [
    (
        "gateway_500",
        httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=httpx.Request("POST", "https://ws.invalid/routes:select"),
            response=httpx.Response(500, text="upstream router unavailable"),
        ),
    ),
    ("timeout", httpx.ReadTimeout("routes:select timed out")),
    ("malformed_body", json.JSONDecodeError("Expecting value", "<not json>", 0)),
    (
        "unauthorized",
        httpx.HTTPStatusError(
            "401 Unauthorized",
            request=httpx.Request("POST", "https://ws.invalid/routes:select"),
            response=httpx.Response(401, text="invalid access token"),
        ),
    ),
    ("relay_unreachable", httpx.ConnectError("connection refused")),
]


@pytest.mark.parametrize(
    ("label", "failure"), ROUTING_FAILURES, ids=[f[0] for f in ROUTING_FAILURES]
)
async def test_a_routing_outage_still_delivers_the_turn(
    client: httpx.AsyncClient,
    db_uri: str,
    label: str,
    failure: Exception,
) -> None:
    """
    The SDK turn path forwards the message and records a declined decision.

    Regression: ``route_turn`` let its client's failure raise, and the events
    POST turned that into a 500 — the message was persisted and then abandoned.
    A router being down is not a reason to lose a turn.
    """
    agent = await create_test_agent(client, name=f"routing-outage-turn-{label}")
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert resp.status_code == 201, resp.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.get_conversation(resp.json()["id"])
    assert conv is not None

    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=_outage_caps(failure)):
        async with echo_runner_client() as runner_client:
            item_id = await orchestration_module._forward_event_to_runner(
                conv.id,
                conv,
                body,
                conv_store,
                runner_client,
            )
    # The turn happened.
    assert item_id

    # And the outage is on the record rather than swallowed.
    decisions = _routing_decisions(conv_store, conv.id)
    assert len(decisions) == 1
    assert decisions[0].data.model == UNAVAILABLE_MODEL
    assert decisions[0].data.applied is False
    assert "Routing call failed" in decisions[0].data.rationale

    refreshed = conv_store.get_conversation(conv.id)
    assert refreshed is not None
    # Nothing pinned: the turn ran on the session's own model.
    assert refreshed.model_override is None
    # And the route-once label is NOT claimed — one outage must not be the
    # reason this session never routes again.
    assert not refreshed.labels.get(ROUTING_DECISION_LABEL_KEY)


@pytest.mark.parametrize(
    ("label", "failure"), ROUTING_FAILURES, ids=[f[0] for f in ROUTING_FAILURES]
)
async def test_a_routing_outage_still_delivers_a_native_pane_turn(
    client: httpx.AsyncClient,
    db_uri: str,
    label: str,
    failure: Exception,
) -> None:
    """
    Same ruling on the native-terminal path, which has its own routing block.

    The pane keeps its own model, the message reaches the terminal, and the
    declined card explains why nothing was routed.
    """
    conv, conv_store = await _claude_native_session(
        client, db_uri, agent_name=f"routing-outage-native-{label}"
    )
    forwarded: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": True})

    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    async with httpx.AsyncClient(
        base_url="http://runner.test", transport=httpx.MockTransport(_handler)
    ) as runner_client:
        with (
            patch("omnigent.runtime._globals._caps", new=_outage_caps(failure)),
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

    # The message reached the pane, carrying no routed model.
    assert len(forwarded) == 1
    assert forwarded[0].get("model_override") is None

    decisions = _routing_decisions(conv_store, conv.id)
    assert len(decisions) == 1
    assert decisions[0].data.model == UNAVAILABLE_MODEL
    assert decisions[0].data.applied is False
    assert "Routing call failed" in decisions[0].data.rationale

    refreshed = conv_store.get_conversation(conv.id)
    assert refreshed is not None
    assert refreshed.model_override is None
    assert not refreshed.labels.get(ROUTING_DECISION_LABEL_KEY)


@pytest.mark.parametrize(
    ("label", "failure"), ROUTING_FAILURES, ids=[f[0] for f in ROUTING_FAILURES]
)
async def test_a_routing_outage_still_allows_the_spawn(
    client: httpx.AsyncClient,
    db_uri: str,
    label: str,
    failure: Exception,
) -> None:
    """
    The spawn gate's own outage coverage, widened past ``RuntimeError``.

    Extends ``test_dead_router_allows_the_spawn_unchanged`` to every failure
    shape a real routing call presents, since the fail-open branch keys on the
    exception being caught at all rather than on its type.
    """
    agent = await create_test_agent(client, name=f"routing-outage-spawn-{label}")
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    with patch("omnigent.runtime._globals._caps", new=_outage_caps(failure)):
        route = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json={"harness": "codex-native", "task_name": "explore", "prompt": "audit auth"},
        )
    assert route.status_code == 200, route.text
    payload = route.json()
    assert payload["action"] == "allow"
    # No model: the spawn keeps whatever the parent asked for.
    assert not payload.get("model")

    conv_store = SqlAlchemyConversationStore(db_uri)
    assert len(_routing_decisions(conv_store, session_id)) == 1


@pytest.mark.parametrize(
    ("label", "failure"), ROUTING_FAILURES, ids=[f[0] for f in ROUTING_FAILURES]
)
async def test_a_routing_outage_still_allows_the_first_prompt(
    client: httpx.AsyncClient,
    db_uri: str,
    label: str,
    failure: Exception,
) -> None:
    """
    The first-message relay answers ``allow`` instead of erroring the hook.

    A 4xx/5xx here would reach the hook as an unreadable verdict, and a hook
    that cannot read a verdict is the case that can eat a typed prompt.
    """
    agent = await create_test_agent(client, name=f"routing-outage-prompt-{label}")
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    with patch("omnigent.runtime._globals._caps", new=_outage_caps(failure)):
        route = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-turn",
            json={"harness": "claude-native", "prompt": "refactor auth"},
        )
    assert route.status_code == 200, route.text
    payload = route.json()
    # ``allow`` = the prompt runs, unblocked and unrouted.
    assert payload["action"] == "allow"
    assert not payload.get("model")
    # Non-terminal: a transient outage must leave the next prompt free to ask
    # again, unlike the deliberate "routing is off" answer.
    assert payload.get("terminal") is not True

    conv_store = SqlAlchemyConversationStore(db_uri)
    refreshed = conv_store.get_conversation(session_id)
    assert refreshed is not None
    assert refreshed.model_override is None
    assert not refreshed.labels.get(ROUTING_DECISION_LABEL_KEY)


@pytest.mark.parametrize(
    ("label", "failure"), ROUTING_FAILURES, ids=[f[0] for f in ROUTING_FAILURES]
)
async def test_a_routing_outage_still_delivers_a_child_spawns_turn(
    client: httpx.AsyncClient,
    db_uri: str,
    label: str,
    failure: Exception,
) -> None:
    """
    A child spawn runs on the model the orchestrator asked for, and says so.

    ``route_session_harness`` already failed open here, but its ``error`` was
    unpacked and dropped — so the one path that could not route left no card at
    all, and an outage was indistinguishable from a router with no opinion.

    A child of a PINNED parent, deliberately: a child of an auto-harness parent
    carries the ``"auto"`` sentinel and is handled by the auto-harness block
    instead, which has always emitted its own unavailable card.
    """
    _parent_id, child, conv_store = await _pinned_parent_and_child(
        client,
        db_uri,
        agent_name=f"routing-outage-child-{label}",
        harness="codex",
    )

    await _send_child_message(
        child, conv_store, FakeRoutingClient(None, error=failure), "audit routing"
    )

    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    # Nothing pinned, and the route-once label is not claimed by a failure.
    assert refreshed.model_override is None
    assert not refreshed.labels.get(ROUTING_DECISION_LABEL_KEY)

    decisions = _routing_decisions(conv_store, child.id)
    assert len(decisions) == 1
    assert decisions[0].data.model == UNAVAILABLE_MODEL
    assert decisions[0].data.applied is False
    assert decisions[0].data.rationale


@pytest.mark.parametrize(
    ("label", "failure"), ROUTING_FAILURES, ids=[f[0] for f in ROUTING_FAILURES]
)
async def test_an_auto_harness_outage_leaves_the_route_once_label_unclaimed(
    client: httpx.AsyncClient,
    db_uri: str,
    label: str,
    failure: Exception,
) -> None:
    """One outage at create must not disable this session's routing for good.

    The auto-harness path stamped the decision label on its own "unavailable"
    card, and the label is the route-once gate — so a router that was merely
    down when the session started meant the in-harness hook declined every
    later prompt as "already routed".
    """
    agent = await create_test_agent(
        client,
        name=f"routing-outage-auto-{label}",
        executor={"type": "omnigent", "config": {"harness": "codex"}},
    )
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "cost_control_mode_override": "on",
            "harness_override": "auto",
        },
    )
    assert created.status_code == 201, created.text
    session_id = str(created.json()["id"])
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.get_conversation(session_id)
    assert conv is not None
    assert conv.harness_override == "auto"

    await _send_child_message(conv, conv_store, FakeRoutingClient(None, error=failure), "hi")

    after_outage = conv_store.get_conversation(session_id)
    assert after_outage is not None
    # The failure is visible…
    decisions = _routing_decisions(conv_store, session_id)
    assert len(decisions) == 1
    assert decisions[0].data.model == UNAVAILABLE_MODEL
    assert decisions[0].data.applied is False
    # …but it claimed neither the model nor the route-once label.
    assert after_outage.model_override is None
    assert not after_outage.labels.get(ROUTING_DECISION_LABEL_KEY)

    # So the session's first in-harness prompt still routes.
    healthy = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="sized task"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=healthy)):
        route = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-turn",
            json={"harness": "codex-native", "prompt": "refactor auth"},
        )
    assert route.status_code == 200, route.text
    assert route.json()["action"] == "route"
    assert route.json()["model"] == GPT_MODEL
    routed = conv_store.get_conversation(session_id)
    assert routed is not None
    assert routed.model_override == GPT_MODEL
    assert routed.labels.get(ROUTING_DECISION_LABEL_KEY)
