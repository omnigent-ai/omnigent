"""Create-time Smart Routing onto a native terminal harness.

The landing screen's top-level "Smart Routing" harness sends
``harness_override: "auto"`` with a native wrapper agent as a placeholder and
the first message as ``smart_routing_message``. A native terminal launches with
the session row, so the harness is decided during the create — not on the first
message event the way the bundle-agent auto path does — and the session is
rebound to the wrapper the router picked.

A create pinned to one native harness (the web UI picking a harness with routing
on) routes on the same seam for the same reason, but only the MODEL: its turns
originate in the TUI, so the server
never sees the first message pre-inference and the turn gate would never fire.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import sqlalchemy as sa

from omnigent.db.db_models import SqlConversation
from omnigent.db.utils import generate_agent_id
from omnigent.runner.subagent_routing import AUTO_HARNESS_LABEL_KEY, ROUTING_DECISION_LABEL_KEY
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._sessions.common import (
    get_server_host_registry,
    set_server_host_registry,
)
from omnigent.server.routes._sessions.orchestration import (
    _installed_native_harnesses,
    _pre_session_model_catalog,
)
from omnigent.server.smart_routing import (
    AUTO_NATIVE_ROUTING_HARNESSES,
    RoutePick,
    RoutingResult,
    TaskV1RouteOptionSource,
    infer_models,
    route_session_harness,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import Host
from tests.server.helpers import FakeCaps, FakeRoutingClient, create_test_agent

# Uuid-shaped so the registry's canonical keying accepts it.
_GATEWAY_HOST_ID = "aa11bb22cc33dd44ee55ff6677889900"

# Names ``init_runtime`` (via the shared ``runtime_init`` fixture) rebinds and
# never restores.
_RUNTIME_GLOBALS = (
    "_conversation_store",
    "_agent_store",
    "_agent_cache",
    "_file_store",
    "_artifact_store",
    "_comment_store",
    "_policy_store",
    "_terminal_registry",
)


@pytest.fixture(autouse=True)
def _restore_runtime_globals() -> Iterator[None]:
    """Put the process-global stores back the way this module found them.

    The ``app`` fixture initializes the runtime with stores bound to a
    per-test database and leaves them installed. A later test that resolves
    a session against its own fake ids then reaches this module's dead
    store instead of the ``None`` it expects.

    :yields: None.
    """
    from omnigent.runtime import _globals

    saved = {name: getattr(_globals, name) for name in _RUNTIME_GLOBALS}
    yield
    for name, value in saved.items():
        setattr(_globals, name, value)


CLAUDE_MODEL = "databricks-claude-opus-4-8"
GPT_MODEL = "databricks-gpt-5-5"
ROUTING_MESSAGE = "refactor the auth module and add tests"

SPAWN_PAYLOAD = {
    "harness": "claude-native",
    "task_name": "code-reviewer",
    "prompt": "review the auth module",
    "parent_model": CLAUDE_MODEL,
}


async def _native_wrappers(client: httpx.AsyncClient, db_uri: str) -> dict[str, str]:
    """Register both native wrapper agents and return ``harness -> agent_id``.

    The server resolves a routed harness to its wrapper by agent NAME via
    ``get_by_name``, which only sees TEMPLATE agents — so the wrappers are
    seeded as templates over a real uploaded bundle (the app fixture skips the
    lifespan that would seed the builtins).

    :param client: Test HTTP client.
    :param db_uri: Database URI for a direct store handle.
    :returns: ``{"claude-native": id, "codex-native": id}``.
    """
    source = await create_test_agent(client, name="native-wrapper-bundle-source")
    store = SqlAlchemyAgentStore(db_uri)
    bundle = store.get(str(source["id"]))
    assert bundle is not None
    wrappers: dict[str, str] = {}
    for harness, agent_name in (
        ("claude-native", "claude-native-ui"),
        ("codex-native", "codex-native-ui"),
    ):
        agent_id = generate_agent_id()
        store.create(agent_id, name=agent_name, bundle_location=bundle.bundle_location)
        wrappers[harness] = agent_id
    return wrappers


def _caps_with(routing_client: FakeRoutingClient | None, *, oss: bool) -> FakeCaps:
    """Caps whose external side is *routing_client*, with the judge on iff *oss*.

    The gateway checks only decide WHICH router answers, so a deployment with no
    built-in judge is the only one an ungatewayed harness can take away.
    """
    from omnigent.server.routing_backend import RoutingBackends

    return FakeCaps(
        routing_client=routing_client,
        routing_backends=RoutingBackends(
            external=routing_client,
            local=routing_client if oss else None,
        ),
    )


async def _create_smart_routing_session(
    client: httpx.AsyncClient,
    wrappers: dict[str, str],
    routing_client: FakeRoutingClient | None,
    *,
    oss: bool = False,
) -> httpx.Response:
    """POST the landing screen's Smart Routing create payload.

    :param client: Test HTTP client.
    :param wrappers: Registered wrapper ids from :func:`_native_wrappers`.
    :param routing_client: Stub router, or ``None`` to leave routing unconfigured.
    :param oss: Whether the server also has the built-in judge configured.
    :returns: The raw create response.
    """
    body = {
        # The Claude wrapper is the client-side placeholder; the server rebinds.
        "agent_id": wrappers["claude-native"],
        "harness_override": "auto",
        "cost_control_mode_override": "on",
        "smart_routing_message": ROUTING_MESSAGE,
    }
    with patch("omnigent.runtime._globals._caps", new=_caps_with(routing_client, oss=oss)):
        return await client.post("/v1/sessions", json=body)


async def _create_fixed_harness_session(
    client: httpx.AsyncClient,
    agent_id: str,
    routing_client: FakeRoutingClient | None,
    *,
    oss: bool = False,
    **extra: Any,  # type: ignore[explicit-any]
) -> httpx.Response:
    """POST a Smart Routing create for a session pinned to one harness.

    The web UI's "Claude Code + Smart Routing" shape: a fixed harness (the
    native wrapper agent, no ``harness_override``) plus routing on and the
    prompt as ``smart_routing_message``.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :param routing_client: Stub router, or ``None`` to leave routing unconfigured.
    :param oss: Whether the server also has the built-in judge configured.
    :param extra: Extra create-body fields, merged last.
    :returns: The raw create response.
    """
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "cost_control_mode_override": "on",
        "smart_routing_message": ROUTING_MESSAGE,
        **extra,
    }
    with patch("omnigent.runtime._globals._caps", new=_caps_with(routing_client, oss=oss)):
        return await client.post("/v1/sessions", json=body)


def _routing_decision_items(db_uri: str, session_id: str) -> list[dict[str, Any]]:
    """The session's ``routing_decision`` item payloads, oldest first.

    :param db_uri: Database URI for a direct store handle.
    :param session_id: Session whose transcript to read.
    :returns: The ``data`` dict of each routing_decision item.
    """
    store = SqlAlchemyConversationStore(db_uri)
    items = store.list_items(session_id, type="routing_decision").data
    return [cast("dict[str, Any]", item.data.model_dump()) for item in items]


@pytest.mark.parametrize(
    ("harness", "picked_model"),
    [
        ("claude-native", CLAUDE_MODEL),
        ("codex-native", GPT_MODEL),
    ],
)
async def test_fixed_native_harness_create_routes_the_model(
    client: httpx.AsyncClient,
    db_uri: str,
    harness: str,
    picked_model: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(RoutingResult(model=picked_model, rationale="sized task"))
    created = await _create_fixed_harness_session(client, wrappers[harness], routing_client)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    # Only the session's own harness is on the menu: routing may change the
    # model, never the harness the caller asked for.
    assert list(routing_client.offered[0]) == [harness]

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    # The wrapper launches with ``--model <routed>``, and the label records what
    # pinned it.
    assert conv.model_override == picked_model
    assert conv.agent_id == wrappers[harness]
    assert conv.harness_override is None
    decision_id = conv.labels.get(ROUTING_DECISION_LABEL_KEY)
    assert decision_id is not None
    # A fixed harness is not auto: subagents stay in this session's family.
    assert AUTO_HARNESS_LABEL_KEY not in conv.labels

    decisions = _routing_decision_items(db_uri, session_id)
    assert len(decisions) == 1
    assert decisions[0]["model"] == picked_model
    assert decisions[0]["applied"] is True
    assert decisions[0]["scope"] == "session"
    assert decisions[0]["harness"] == harness
    assert decisions[0]["decision_id"] == decision_id

    # The create response and the snapshot both carry the model the CLI launches
    # with. (``harness`` is read off the bound agent's SPEC, which these seeded
    # wrappers share with a generic test bundle, so it is not asserted here.)
    assert created.json()["model_override"] == picked_model
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["model_override"] == picked_model
    # The launch harness is reported as ``harness`` on both (there is no
    # ``harness_override`` field on the snapshot — a native session stores none).
    assert "harness_override" not in snapshot.json()
    assert created.json()["harness"] == snapshot.json()["harness"]


# A create that cannot pin the pick still opens the terminal on the CLI's own
# default, records an unapplied decision naming why, and joins no decision label.
# Two causes: an unrunnable cross-family pick (harness is not negotiable here),
# and a router that is down.
@pytest.mark.parametrize(
    ("harness", "routing_client_factory", "rationale_contains"),
    [
        (
            "claude-native",
            lambda: FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="narrow change")),
            GPT_MODEL,
        ),
        ("codex-native", lambda: None, "not configured"),
    ],
)
async def test_fixed_harness_create_does_not_pin_an_unrunnable_or_absent_pick(
    client: httpx.AsyncClient,
    db_uri: str,
    harness: str,
    routing_client_factory: Any,
    rationale_contains: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    created = await _create_fixed_harness_session(
        client, wrappers[harness], routing_client_factory()
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    assert conv.model_override is None
    assert ROUTING_DECISION_LABEL_KEY not in conv.labels

    decisions = _routing_decision_items(db_uri, session_id)
    assert len(decisions) == 1
    assert decisions[0]["applied"] is False
    assert rationale_contains in decisions[0]["rationale"]


@pytest.mark.parametrize(
    "extra",
    [
        # Routing off: the create is a plain fixed-harness create.
        {"cost_control_mode_override": "off"},
        # No routing text — nothing to size the task with.
        {"smart_routing_message": None},
        {"smart_routing_message": "   "},
        # A client-pinned model wins over the router, as it does per turn.
        {"model_override": CLAUDE_MODEL},
    ],
)
async def test_fixed_harness_create_routes_only_when_asked(
    client: httpx.AsyncClient,
    db_uri: str,
    extra: dict[str, Any],
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    created = await _create_fixed_harness_session(
        client, wrappers["claude-native"], routing_client, **extra
    )
    assert created.status_code == 201, created.text
    assert routing_client.offered == []

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    assert conv.model_override == extra.get("model_override")
    assert _routing_decision_items(db_uri, created.json()["id"]) == []


async def test_sdk_harness_create_still_routes_on_its_first_turn(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    # An SDK harness's first message reaches the server before inference, so the
    # turn gate routes it; a create-time pin would only disable that gate.
    agent = await create_test_agent(client, name="fixed-harness-sdk-agent")
    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    created = await _create_fixed_harness_session(client, agent["id"], routing_client)
    assert created.status_code == 201, created.text
    assert routing_client.offered == []

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    assert conv.model_override is None
    assert _routing_decision_items(db_uri, created.json()["id"]) == []


async def test_child_session_create_is_not_routed_at_create_time(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    parent = await client.post("/v1/sessions", json={"agent_id": wrappers["claude-native"]})
    assert parent.status_code == 201, parent.text

    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    created = await _create_fixed_harness_session(
        client,
        wrappers["claude-native"],
        routing_client,
        parent_session_id=parent.json()["id"],
    )
    assert created.status_code == 201, created.text
    # A child is routed by the spawn / turn paths, which know the parent's
    # family and its own routing state.
    assert routing_client.offered == []

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    assert conv.model_override is None


@pytest.mark.parametrize(
    ("picked_model", "expected_harness"),
    [
        (CLAUDE_MODEL, "claude-native"),
        (GPT_MODEL, "codex-native"),
    ],
)
async def test_create_binds_the_wrapper_the_router_picked(
    client: httpx.AsyncClient,
    db_uri: str,
    picked_model: str,
    expected_harness: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(RoutingResult(model=picked_model, rationale="sized task"))
    created = await _create_smart_routing_session(client, wrappers, routing_client)
    assert created.status_code == 201, created.text

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    assert conv.agent_id == wrappers[expected_harness]
    # The wrapper's terminal launches with the routed model baked in.
    assert conv.model_override == picked_model
    # No sentinel survives: a native wrapper rejects harness_override, and a
    # leftover "auto" would re-route an already-running terminal.
    assert conv.harness_override is None
    # The auto marker is what keeps subagents cross-harness-eligible.
    assert conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"
    # What a client reads back to launch the TUI: the routed model plus the
    # bound wrapper. The row keeps no ``harness_override`` on this path, so
    # ``harness`` (resolved from the bound wrapper) is the harness field.
    assert created.json()["model_override"] == picked_model
    assert created.json()["agent_id"] == wrappers[expected_harness]
    assert "harness_override" not in created.json()


async def test_router_is_offered_both_native_families(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="narrow change"))
    created = await _create_smart_routing_session(client, wrappers, routing_client)
    assert created.status_code == 201, created.text
    assert set(routing_client.offered[0]) == {"claude-native", "codex-native"}


async def test_routed_wrapper_gets_terminal_first_labels(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="narrow change"))
    created = await _create_smart_routing_session(client, wrappers, routing_client)
    assert created.status_code == 201, created.text

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    # The routed wrapper's own presentation labels, not the placeholder's.
    assert conv.labels.get("omnigent.ui") == "terminal"
    assert conv.labels.get("omnigent.wrapper") == "codex-native-ui"


async def test_create_falls_back_to_a_native_cli_when_routing_is_unavailable(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    created = await _create_smart_routing_session(client, wrappers, None)
    assert created.status_code == 201, created.text

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    # Still lands on a terminal, with the CLI's own default model.
    assert conv.agent_id == wrappers["claude-native"]
    assert conv.model_override is None
    assert conv.harness_override is None
    assert conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"


async def test_smart_routing_session_keeps_cross_harness_subagents(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    created = await _create_smart_routing_session(client, wrappers, routing_client)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    spawn_router = FakeRoutingClient(
        RoutingResult(model=GPT_MODEL, rationale="narrow change", harness="codex")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=spawn_router)):
        resp = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
    assert resp.status_code == 200, resp.text
    # The auto label survives the create, so the spawn is offered both families
    # and may leave the session's own harness family.
    assert set(spawn_router.offered[0]) == {"claude-native", "codex-native"}
    assert resp.json()["harness"] == "codex-native"


# ── The create's verdict is not re-derived on the first prompt ──────────────
#
# The prompt the landing screen routed is submitted again inside the harness,
# where the first-prompt hook used to score it a second time — one more judge
# call, tens of seconds later, for the verdict the pane was already running.


async def _route_turn(
    client: httpx.AsyncClient,
    session_id: str,
    routing_client: FakeRoutingClient | None,
    *,
    harness: str,
    prompt: str,
) -> httpx.Response:
    """POST the in-harness first-prompt hook for *session_id*.

    :param client: Test HTTP client.
    :param session_id: The session whose prompt was submitted.
    :param routing_client: Stub router the hook may call.
    :param harness: The pane's harness, e.g. ``"codex-native"``.
    :param prompt: The submitted prompt text.
    :returns: The raw hook response.
    """
    with patch("omnigent.runtime._globals._caps", new=_caps_with(routing_client, oss=False)):
        return await client.post(
            f"/v1/sessions/{session_id}/hooks/route-turn",
            json={"harness": harness, "prompt": prompt},
        )


async def test_the_first_prompt_reuses_the_creates_verdict(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="narrow change"))
    created = await _create_smart_routing_session(client, wrappers, routing_client)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    route = await _route_turn(
        client,
        session_id,
        routing_client,
        harness="codex-native",
        prompt=ROUTING_MESSAGE,
    )
    assert route.status_code == 200, route.text
    # Nothing to switch to and nothing to replay: the pane launched on the pick.
    assert route.json()["action"] == "allow"
    assert route.json()["terminal"] is True
    # One router call for the whole session, not two.
    assert len(routing_client.offered) == 1

    # One chip, and it is the create's — now claimed as the session's decision,
    # so a later prompt does not ask again either.
    decisions = _routing_decision_items(db_uri, session_id)
    assert len(decisions) == 1
    assert decisions[0]["scope"] == "session"
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    assert conv.labels.get(ROUTING_DECISION_LABEL_KEY) == decisions[0]["decision_id"]


async def test_a_prompt_the_create_did_not_route_still_routes(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="narrow change"))
    created = await _create_smart_routing_session(client, wrappers, routing_client)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    # The user edited the prompt in the pane before sending it, so the create's
    # verdict was reached on text nobody submitted.
    route = await _route_turn(
        client,
        session_id,
        routing_client,
        harness="codex-native",
        prompt="actually, just fix the failing test",
    )
    assert route.status_code == 200, route.text
    assert route.json()["action"] == "route"
    assert len(routing_client.offered) == 2

    decisions = _routing_decision_items(db_uri, session_id)
    assert [d["scope"] for d in decisions] == ["session", "turn"]


async def test_an_outage_at_create_leaves_the_first_prompt_free_to_route(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    created = await _create_smart_routing_session(client, wrappers, None)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    healthy = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    route = await _route_turn(
        client,
        session_id,
        healthy,
        harness="claude-native",
        prompt=ROUTING_MESSAGE,
    )
    assert route.status_code == 200, route.text
    # Nothing was routed at create, so there is no verdict to reuse.
    assert route.json()["action"] == "route"
    assert route.json()["model"] == CLAUDE_MODEL
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    assert conv.model_override == CLAUDE_MODEL


async def test_bundle_agent_auto_path_is_unchanged(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    agent = await create_test_agent(client, name="smart-routing-bundle-agent")
    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        created = await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "harness_override": "auto",
                "cost_control_mode_override": "on",
                "smart_routing_message": ROUTING_MESSAGE,
            },
        )
    assert created.status_code == 201, created.text

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    # A non-native agent keeps the sentinel for first-message resolution, and
    # the create must not have called the router.
    assert conv.harness_override == "auto"
    assert conv.model_override is None
    assert conv.agent_id == agent["id"]
    assert conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"
    assert routing_client.offered == []


# ── Spec-level opt-in: executor.config.smart_routing_harness ───────

OPT_IN_EXECUTOR = {
    "type": "omnigent",
    "config": {"harness": "claude-sdk", "smart_routing_harness": "auto"},
}


async def _create_opt_in_session(
    client: httpx.AsyncClient,
    agent_id: str,
    **extra: Any,
) -> httpx.Response:
    """Create a session the way the landing screen does for a bundle agent.

    Sends no ``harness_override``: the spec's opt-in is what must turn the
    brain harness over to the router.

    :param client: Test HTTP client.
    :param agent_id: The bound bundle agent.
    :param extra: Extra create-body fields, e.g. ``model_override``.
    :returns: The create response.
    """
    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        return await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "cost_control_mode_override": "on",
                "smart_routing_message": ROUTING_MESSAGE,
                **extra,
            },
        )


async def test_spec_opt_in_hands_the_brain_harness_to_the_router(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    agent = await create_test_agent(client, name="spec-opt-in-agent", executor=OPT_IN_EXECUTOR)
    created = await _create_opt_in_session(client, str(agent["id"]))
    assert created.status_code == 201, created.text

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    # The spec's own pin (claude-sdk) is replaced by the sentinel, so the first
    # message routes harness AND model instead of inheriting the pinned family.
    assert conv.harness_override == "auto"
    assert conv.model_override is None
    # The durable marker is what lets a sub-agent leave the brain's family.
    assert conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"


async def test_spec_opt_in_is_inert_with_smart_routing_off(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    agent = await create_test_agent(
        client, name="spec-opt-in-routing-off", executor=OPT_IN_EXECUTOR
    )
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201, created.text

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    # No routing, no sentinel: the session runs on the spec's pinned harness.
    assert conv.harness_override is None
    assert AUTO_HARNESS_LABEL_KEY not in conv.labels


async def test_explicit_harness_override_beats_the_spec_opt_in(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    agent = await create_test_agent(
        client, name="spec-opt-in-explicit-harness", executor=OPT_IN_EXECUTOR
    )
    created = await _create_opt_in_session(client, str(agent["id"]), harness_override="codex")
    assert created.status_code == 201, created.text

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    # The user picked a harness in the gear modal; the spec does not overrule it.
    assert conv.harness_override == "codex"
    assert AUTO_HARNESS_LABEL_KEY not in conv.labels


async def test_client_model_pin_suppresses_the_spec_opt_in(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    agent = await create_test_agent(
        client, name="spec-opt-in-pinned-model", executor=OPT_IN_EXECUTOR
    )
    created = await _create_opt_in_session(client, str(agent["id"]), model_override=CLAUDE_MODEL)
    assert created.status_code == 201, created.text

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    # Going auto nulls the model, so a client's pin must keep the opt-in off
    # rather than being silently dropped.
    assert conv.harness_override is None
    assert conv.model_override == CLAUDE_MODEL
    assert AUTO_HARNESS_LABEL_KEY not in conv.labels


async def test_a_spec_without_the_opt_in_keeps_its_pinned_harness(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    agent = await create_test_agent(client, name="spec-no-opt-in")
    created = await _create_opt_in_session(client, str(agent["id"]))
    assert created.status_code == 201, created.text

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    # Smart Routing alone changes nothing about the harness — the key does.
    assert conv.harness_override is None
    assert AUTO_HARNESS_LABEL_KEY not in conv.labels


async def test_a_non_auto_opt_in_value_is_rejected(client: httpx.AsyncClient) -> None:
    agent = await create_test_agent(
        client,
        name="spec-opt-in-bad-value",
        executor={
            "type": "omnigent",
            "config": {"harness": "claude-sdk", "smart_routing_harness": "codex"},
        },
    )
    created = await _create_opt_in_session(client, str(agent["id"]))
    assert created.status_code == 400, created.text
    assert "smart_routing_harness" in created.text


# ── Create-time subagent-routing stamp ──────────────────────────────────────
#
# The spawn gate is two-state: only an explicit ``"on"`` routes. A session that
# starts on Smart Routing is therefore stamped once, at create, so no gate has
# to re-derive routing from this session's (or its parent's) state later.

_WRAPPER = "__wrapper__"
_CODEX_WRAPPER = "__codex_wrapper__"
_BUNDLE = "__bundle__"
_CODEX_BUNDLE = "__codex_bundle__"


@pytest.mark.parametrize(
    ("case", "body", "parent_cost_control", "expected"),
    [
        # Every shape that starts on Smart Routing is stamped, whichever seam
        # decided the harness.
        (
            "top-level-auto",
            {
                "agent_id": _WRAPPER,
                "harness_override": "auto",
                "cost_control_mode_override": "on",
                "smart_routing_message": ROUTING_MESSAGE,
            },
            None,
            "on",
        ),
        (
            "bundle-auto",
            {
                "agent_id": _BUNDLE,
                "harness_override": "auto",
                "cost_control_mode_override": "on",
                "smart_routing_message": ROUTING_MESSAGE,
            },
            None,
            "on",
        ),
        (
            "fixed-harness-cost-control-on",
            {
                "agent_id": _WRAPPER,
                "cost_control_mode_override": "on",
                "smart_routing_message": ROUTING_MESSAGE,
            },
            None,
            "on",
        ),
        # A session pinned to codex is stamped too: its launch installs the
        # same generated ``hooks.json`` spawn gate and routed-spawn tool
        # pre-approvals the claude arm gets, so the switch has a consumer.
        # Suppressing it here is what left the owner's pinned-codex session
        # unable to spawn at all.
        (
            "pinned-codex-cost-control-on",
            {
                "agent_id": _CODEX_WRAPPER,
                "cost_control_mode_override": "on",
                "smart_routing_message": ROUTING_MESSAGE,
            },
            None,
            "on",
        ),
        # A bundle/SDK agent whose brain is codex is codex-family too, and it
        # spawns through the session-create path, which re-reads this switch per
        # spawn — so the stamp is what gives its children default routing.
        (
            "codex-brain-bundle-cost-control-on",
            {
                "agent_id": _CODEX_BUNDLE,
                "cost_control_mode_override": "on",
                "smart_routing_message": ROUTING_MESSAGE,
            },
            None,
            "on",
        ),
        # Auto-harness may land on codex, and there the machinery IS installed.
        (
            "codex-wrapper-auto",
            {
                "agent_id": _CODEX_WRAPPER,
                "harness_override": "auto",
                "cost_control_mode_override": "on",
                "smart_routing_message": ROUTING_MESSAGE,
            },
            None,
            "on",
        ),
        # Not routed: nothing is written, so the blob stays as small as it was.
        (
            "cost-control-off",
            {"agent_id": _WRAPPER, "cost_control_mode_override": "off"},
            None,
            None,
        ),
        ("cost-control-omitted", {"agent_id": _WRAPPER}, None, None),
        # Plain native, either family: nothing routes, so nothing reads the
        # switch — and the gear hides the row to match.
        (
            "plain-codex-cost-control-off",
            {"agent_id": _CODEX_WRAPPER, "cost_control_mode_override": "off"},
            None,
            None,
        ),
        ("plain-codex-cost-control-omitted", {"agent_id": _CODEX_WRAPPER}, None, None),
        # A child carries its own stamp copied from the parent at create; the
        # gate never re-reads the parent.
        ("child-of-routed-parent", {"agent_id": _BUNDLE}, "on", "on"),
        ("child-of-unrouted-parent", {"agent_id": _BUNDLE}, None, None),
        # An explicit caller value always wins over the stamp.
        (
            "explicit-off-beside-cost-control-on",
            {
                "agent_id": _WRAPPER,
                "cost_control_mode_override": "on",
                "subagent_routing_override": "off",
                "smart_routing_message": ROUTING_MESSAGE,
            },
            None,
            "off",
        ),
        (
            "explicit-on-without-cost-control",
            {"agent_id": _WRAPPER, "subagent_routing_override": "on"},
            None,
            "on",
        ),
    ],
)
async def test_create_stamps_subagent_routing_for_routed_sessions(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    body: dict[str, Any],  # type: ignore[explicit-any]
    parent_cost_control: str | None,
    expected: str | None,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    bundle = await create_test_agent(client, name=f"stamp-{case}")
    codex_bundle = await create_test_agent(
        client,
        name=f"stamp-codex-{case}",
        executor={"type": "omnigent", "config": {"harness": "codex"}},
    )
    agent_ids = {
        _WRAPPER: wrappers["claude-native"],
        _CODEX_WRAPPER: wrappers["codex-native"],
        _BUNDLE: str(bundle["id"]),
        _CODEX_BUNDLE: str(codex_bundle["id"]),
    }
    payload = {**body, "agent_id": agent_ids[body["agent_id"]]}

    if parent_cost_control is not None or case.startswith("child-of"):
        parent_body: dict[str, Any] = {"agent_id": str(bundle["id"])}
        if parent_cost_control is not None:
            parent_body["cost_control_mode_override"] = parent_cost_control
        parent = await client.post("/v1/sessions", json=parent_body)
        assert parent.status_code == 201, parent.text
        payload["parent_session_id"] = parent.json()["id"]

    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        created = await client.post("/v1/sessions", json=payload)
    assert created.status_code == 201, created.text

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    assert conv.subagent_routing_override == expected
    # The stamp is a real row write, so a client reading the session back sees it
    # without a PATCH of its own.
    snapshot = await client.get(f"/v1/sessions/{created.json()['id']}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["subagent_routing_override"] == expected


async def test_unrouted_create_writes_no_subagent_routing_key(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    # Only "on" is ever stamped: writing "off" on every ordinary create would
    # grow the fixed-width overrides blob for no change in behavior.
    agent = await create_test_agent(client, name="stamp-absent-key")
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "reasoning_effort": "high"},
    )
    assert created.status_code == 201, created.text

    store = SqlAlchemyConversationStore(db_uri)
    with store._engine.connect() as conn:
        blob = conn.execute(
            sa.select(SqlConversation.session_overrides).where(
                SqlConversation.id == created.json()["id"]
            )
        ).scalar_one()
    assert json.loads(blob) == {"reasoning_effort": "high"}


async def test_native_candidates_impose_no_family_constraint() -> None:
    # The candidate override and the parent-family filter are orthogonal: an
    # auto session passes no allowed_family, so both native families reach the
    # router even though the family filter is applied to the same tuple.
    routing_client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="narrow change"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        harness, model, _verdict, error = await route_session_harness(
            ROUTING_MESSAGE,
            harness_candidates=AUTO_NATIVE_ROUTING_HARNESSES,
        )
    assert error is None
    assert set(routing_client.offered[0]) == {"claude-native", "codex-native"}
    assert harness == "codex-native"
    assert model == GPT_MODEL


async def test_native_candidates_still_honor_an_explicit_family() -> None:
    # A family constraint (a child of a pinned parent) narrows the same tuple,
    # so the two knobs compose rather than fight.
    routing_client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        harness, _model, _verdict, error = await route_session_harness(
            ROUTING_MESSAGE,
            harness_candidates=AUTO_NATIVE_ROUTING_HARNESSES,
            allowed_family="claude",
        )
    assert error is None
    assert set(routing_client.offered[0]) == {"claude-native"}
    assert harness == "claude-native"


async def test_no_installed_native_candidates_reports_the_standard_error() -> None:
    routing_client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="narrow change"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        harness, model, verdict, error = await route_session_harness(
            ROUTING_MESSAGE,
            harness_candidates=(),
        )
    assert (harness, model, verdict) == (None, None, None)
    assert error == "No routable harnesses are available on this runner."
    assert routing_client.offered == []


def _host(
    readiness: dict[str, Any] | None,  # type: ignore[explicit-any]
) -> Host:
    return Host(
        host_id=_GATEWAY_HOST_ID,
        name="dev",
        user_id="alice@example.com",
        status="online",
        created_at=0,
        updated_at=0,
        configured_harnesses=readiness,
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (_host({"claude-native": True, "codex-native": True}), ["claude-native", "codex-native"]),
        (_host({"claude-native": True, "codex-native": "binary-missing"}), ["claude-native"]),
        (_host({"claude-native": "needs-auth", "codex-native": True}), ["codex-native"]),
        (_host({"claude-native": "version-too-low", "codex-native": False}), []),
        # An unreported harness can't be assumed installed.
        (_host({"claude-native": True}), ["claude-native"]),
        # Fails open: a host with no readiness map doesn't disable routing.
        (_host(None), ["claude-native", "codex-native"]),
        # No host at all fails open the same way.
        (None, ["claude-native", "codex-native"]),
    ],
)
def test_installed_native_harnesses_follows_host_readiness(
    host: Host | None, expected: list[str]
) -> None:
    assert _installed_native_harnesses(host) == expected


# ── Credential-provider gating ──────────────────────────────────────────────
#
# Routing rewrites the launch model to a gateway catalog id, so a pane whose CLI
# resolves credentials somewhere else (a personal Claude/ChatGPT subscription,
# Bedrock) cannot run the pick. The host reports that per family on its connect
# handshake and the server holds the map in memory; only an explicit ``False``
# gates, so a host that has reported nothing keeps every option.


@pytest.fixture(autouse=True)
def gateway_host_registry() -> Iterator[HostRegistry]:
    """Install a fresh process-global host registry, as app startup does.

    Gateway backing is reported on the host's connect handshake and held here,
    so a test with no report models a replica that has not heard from the host.
    """
    registry = HostRegistry()
    set_server_host_registry(registry)
    try:
        yield registry
    finally:
        set_server_host_registry(None)


def _host_reporting(gateway: dict[str, bool] | None) -> Host:
    """A host row whose gateway map is already reported to the live registry.

    :param gateway: The map the host reported, or ``None`` for no report.
    :returns: The matching host row.
    """
    registry = get_server_host_registry()
    assert registry is not None
    registry.record_gateway_inference(_GATEWAY_HOST_ID, gateway)
    return _host(None)


@pytest.mark.parametrize(
    ("gateway", "expected"),
    [
        ({"claude-native": True, "codex-native": True}, []),
        # Claude Code on the gateway, Codex on a ChatGPT subscription.
        ({"claude-native": True, "codex-native": False}, ["codex-native"]),
        (
            {"claude-native": False, "codex-native": False},
            AUTO_NATIVE_ROUTING_HARNESSES,
        ),
        # Reversed spellings resolve to the same family.
        ({"native-codex": False}, ["codex-native"]),
        # Unknown is not "unavailable": a partial report, a host that reported
        # nothing, and no host at all all keep both arms.
        ({"claude-native": True}, []),
        (None, []),
    ],
)
def test_ungatewayed_native_harnesses_reads_the_reported_gateway_map(
    gateway: dict[str, bool] | None,
    expected: list[str],
    gateway_host_registry: HostRegistry,
) -> None:
    from omnigent.server.routes._sessions.orchestration import _ungatewayed_native_harnesses

    gateway_host_registry.record_gateway_inference(_GATEWAY_HOST_ID, gateway)
    assert _ungatewayed_native_harnesses(_host(None), AUTO_NATIVE_ROUTING_HARNESSES) == list(
        expected
    )


def test_ungatewayed_native_harnesses_without_a_host(
    gateway_host_registry: HostRegistry,
) -> None:
    from omnigent.server.routes._sessions.orchestration import _ungatewayed_native_harnesses

    assert _ungatewayed_native_harnesses(None, AUTO_NATIVE_ROUTING_HARNESSES) == []


def _routing_request(host: Host | None) -> Any:  # type: ignore[explicit-any]
    """A request whose host store serves *host* and whose registry is empty."""
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                host_store=SimpleNamespace(get_host=lambda host_id: host),
                host_registry=None,
            )
        )
    )


@pytest.mark.parametrize(
    ("gateway", "named"),
    [
        ({"claude-native": True, "codex-native": False}, "Codex"),
        ({"claude-native": False, "codex-native": True}, "Claude"),
        # No AI Gateway anywhere: both arms are named.
        ({"claude-native": False, "codex-native": False}, "Claude and Codex"),
    ],
)
async def test_auto_routing_is_refused_when_no_router_can_serve_an_off_gateway_arm(
    gateway: dict[str, bool],
    named: str,
) -> None:
    """Only fatal with no built-in judge: nothing is left to answer with."""
    from omnigent.server.routes._sessions.orchestration import _resolve_native_smart_routing

    body = SimpleNamespace(host_id="host_1", smart_routing_message=ROUTING_MESSAGE)
    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    with patch("omnigent.runtime._globals._caps", new=_caps_with(routing_client, oss=False)):
        agent_name, model, verdict, error = await _resolve_native_smart_routing(
            cast("Any", body),
            cast("Any", _routing_request(_host_reporting(gateway))),
            "alice@example.com",
        )

    # No wrapper name is the signal the create turns into a 400.
    assert (agent_name, model, verdict) == (None, None, None)
    assert error is not None
    assert named in error
    assert "not AI-Gateway-backed" in error
    # The router is never asked to pick between arms it cannot apply to.
    assert routing_client.offered == []


# The candidate set an ungatewayed host answers with: the pane's own provider
# spelling, never the static table's ``databricks-*`` ids.
_OFF_GATEWAY_CATALOG = {
    "claude-native": ["claude-opus-4-8"],
    "codex-native": ["gpt-5-5"],
}


@pytest.mark.parametrize(
    "gateway",
    [
        {"claude-native": True, "codex-native": False},
        {"claude-native": False, "codex-native": False},
    ],
)
async def test_auto_routing_falls_back_to_the_built_in_judge_off_the_gateway(
    gateway: dict[str, bool],
) -> None:
    from omnigent.server.routes._sessions import orchestration

    body = SimpleNamespace(host_id="host_1", smart_routing_message=ROUTING_MESSAGE)
    routing_client = FakeRoutingClient(
        RoutingResult(model="gpt-5-5", rationale="narrow change", harness="codex-native")
    )
    with (
        patch("omnigent.runtime._globals._caps", new=_caps_with(routing_client, oss=True)),
        patch.object(
            orchestration,
            "_pre_session_model_catalog",
            AsyncMock(return_value=dict(_OFF_GATEWAY_CATALOG)),
        ),
    ):
        agent_name, model, verdict, error = await orchestration._resolve_native_smart_routing(
            cast("Any", body),
            cast("Any", _routing_request(_host_reporting(gateway))),
            "alice@example.com",
        )

    assert error is None
    assert (agent_name, model) == ("codex-native-ui", "gpt-5-5")
    assert verdict is not None
    assert verdict["router_source"] == "oss-llm"
    # The host's own catalog is the whole offer: no static databricks-* top-up.
    assert routing_client.offered[0] == _OFF_GATEWAY_CATALOG


async def test_auto_routing_declines_off_the_gateway_when_the_host_answers_nothing() -> None:
    """No provider-accurate candidates means decline, not a databricks-* offer.

    The static table is every ``databricks-*`` endpoint, so offering it here
    would land the session on a model the ungatewayed pane cannot reach.
    """
    from omnigent.server.routes._sessions import orchestration

    body = SimpleNamespace(host_id="host_1", smart_routing_message=ROUTING_MESSAGE)
    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    with patch("omnigent.runtime._globals._caps", new=_caps_with(routing_client, oss=True)):
        agent_name, model, verdict, error = await orchestration._resolve_native_smart_routing(
            cast("Any", body),
            cast("Any", _routing_request(_host_reporting({"codex-native": False}))),
            "alice@example.com",
        )

    # The session still lands on a terminal; it just keeps the CLI's own model.
    assert agent_name == "claude-native-ui"
    assert (model, verdict) == (None, None)
    assert error is not None
    assert routing_client.offered == []


async def test_auto_routing_still_runs_when_the_host_reports_no_gateway_map() -> None:
    from omnigent.server.routes._sessions.orchestration import _resolve_native_smart_routing

    body = SimpleNamespace(host_id="host_1", smart_routing_message=ROUTING_MESSAGE)
    routing_client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="narrow change"))
    # An external router is what puts both panes on the menu; unknown gateway
    # backing must not take it away.
    with patch("omnigent.runtime._globals._caps", new=_caps_with(routing_client, oss=False)):
        agent_name, model, _verdict, error = await _resolve_native_smart_routing(
            cast("Any", body),
            cast("Any", _routing_request(_host_reporting(None))),
            "alice@example.com",
        )

    assert error is None
    assert (agent_name, model) == ("codex-native-ui", GPT_MODEL)


async def test_a_judge_only_deployment_keeps_the_default_pane_and_routes_its_model() -> None:
    """Choosing BETWEEN panes needs the workspace router; the model does not.

    A native create bakes its harness into the terminal launch, so with no
    external router the create keeps the default pane and routes only its
    model — a normal Smart Routing session minus the harness half — rather
    than declining into a session with no terminal.
    """
    from omnigent.server.routes._sessions.orchestration import _resolve_native_smart_routing

    body = SimpleNamespace(host_id="host_1", smart_routing_message=ROUTING_MESSAGE)
    judge = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=judge)):
        agent_name, model, verdict, error = await _resolve_native_smart_routing(
            cast("Any", body),
            cast("Any", _routing_request(_host_reporting(None))),
            "alice@example.com",
        )

    assert error is None
    assert (agent_name, model) == ("claude-native-ui", CLAUDE_MODEL)
    assert verdict is not None
    assert verdict["router_source"] == "oss-llm"
    # The chip says why the harness did not move.
    assert "Harness routing" in verdict["rationale"]
    # Only the kept pane was ever on the menu.
    assert list(judge.offered[0]) == ["claude-native"]


async def test_a_judge_only_create_pins_nothing_when_the_pick_is_out_of_family() -> None:
    """One pane on offer means any pick resolves onto it — so check the family."""
    from omnigent.server.routes._sessions.orchestration import _resolve_native_smart_routing

    body = SimpleNamespace(host_id="host_1", smart_routing_message=ROUTING_MESSAGE)
    judge = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="narrow change"))
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=judge)):
        agent_name, model, verdict, error = await _resolve_native_smart_routing(
            cast("Any", body),
            cast("Any", _routing_request(_host_reporting(None))),
            "alice@example.com",
        )

    # The session still opens a terminal; it just keeps the CLI's own model.
    assert agent_name == "claude-native-ui"
    assert (model, verdict) == (None, None)
    assert error is not None
    assert GPT_MODEL in error


async def test_top_level_smart_routing_create_is_rejected_when_no_router_can_serve(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    from omnigent.server.routes._sessions import orchestration

    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    ungatewayed = _host_reporting({"claude-native": True, "codex-native": False})
    with patch.object(orchestration, "_routing_host_for_create", return_value=ungatewayed):
        created = await _create_smart_routing_session(client, wrappers, routing_client, oss=False)

    assert created.status_code == 400, created.text
    assert "not AI-Gateway-backed" in created.text
    assert routing_client.offered == []


async def test_top_level_smart_routing_create_succeeds_off_the_gateway_with_the_judge(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    from omnigent.server.routes._sessions import orchestration

    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(
        RoutingResult(model="gpt-5-5", rationale="narrow change", harness="codex-native")
    )
    ungatewayed = _host_reporting({"claude-native": True, "codex-native": False})
    with (
        patch.object(orchestration, "_routing_host_for_create", return_value=ungatewayed),
        patch.object(
            orchestration,
            "_pre_session_model_catalog",
            AsyncMock(return_value=dict(_OFF_GATEWAY_CATALOG)),
        ),
    ):
        created = await _create_smart_routing_session(client, wrappers, routing_client, oss=True)

    assert created.status_code == 201, created.text
    assert created.json()["model_override"] == "gpt-5-5"
    # The chip records which router answered.
    items = (await client.get(f"/v1/sessions/{created.json()['id']}/items")).json()["data"]
    decisions = [i for i in items if i["type"] == "routing_decision"]
    assert decisions and decisions[0]["router_source"] == "oss-llm"


@pytest.mark.parametrize("harness", list(AUTO_NATIVE_ROUTING_HARNESSES))
async def test_fixed_harness_routing_create_is_rejected_when_no_router_can_serve(
    client: httpx.AsyncClient,
    db_uri: str,
    harness: str,
) -> None:
    from omnigent.server.routes._sessions import orchestration

    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    ungatewayed = _host_reporting({harness: False})
    with patch.object(orchestration, "_routing_host_for_create", return_value=ungatewayed):
        created = await _create_fixed_harness_session(
            client, wrappers[harness], routing_client, oss=False
        )

    assert created.status_code == 400, created.text
    assert harness in created.json()["error"]["message"]
    assert "not AI-Gateway-backed" in created.json()["error"]["message"]
    assert routing_client.offered == []


@pytest.mark.parametrize("harness", list(AUTO_NATIVE_ROUTING_HARNESSES))
async def test_fixed_harness_routing_create_succeeds_off_the_gateway_with_the_judge(
    client: httpx.AsyncClient,
    db_uri: str,
    harness: str,
) -> None:
    from omnigent.server.routes._sessions import orchestration

    wrappers = await _native_wrappers(client, db_uri)
    pick = _OFF_GATEWAY_CATALOG[harness][0]
    routing_client = FakeRoutingClient(RoutingResult(model=pick, rationale="sized task"))
    ungatewayed = _host_reporting({harness: False})
    with (
        patch.object(orchestration, "_routing_host_for_create", return_value=ungatewayed),
        patch.object(
            orchestration,
            "_pre_session_model_catalog",
            AsyncMock(return_value={harness: [pick]}),
        ),
    ):
        created = await _create_fixed_harness_session(
            client, wrappers[harness], routing_client, oss=True
        )

    assert created.status_code == 201, created.text
    assert created.json()["model_override"] == pick
    items = (await client.get(f"/v1/sessions/{created.json()['id']}/items")).json()["data"]
    decisions = [i for i in items if i["type"] == "routing_decision"]
    assert decisions and decisions[0]["router_source"] == "oss-llm"


@pytest.mark.parametrize("harness", list(AUTO_NATIVE_ROUTING_HARNESSES))
async def test_a_pane_create_routes_with_the_judge_when_the_router_is_not_enabled(
    client: httpx.AsyncClient,
    db_uri: str,
    harness: str,
) -> None:
    """Picking Smart Routing in the CLI's model menu, on a workspace with no router.

    The gateway backing is fine here — the workspace simply never had the
    routing API enabled, so ``routes:select`` 404s. The session must still get
    a normal Smart Routing decision, from the judge.
    """
    from omnigent.server.routes._sessions import orchestration
    from omnigent.server.routing_backend import RoutingBackends

    wrappers = await _native_wrappers(client, db_uri)
    pick = _OFF_GATEWAY_CATALOG[harness][0]
    external = FakeRoutingClient(
        None, last_error="router returned HTTP 404: routes:select is not enabled for this account."
    )
    judge = FakeRoutingClient(RoutingResult(model=pick, rationale="sized task"))
    caps = FakeCaps(
        routing_client=external,
        routing_backends=RoutingBackends(external=external, local=judge),
    )
    body = {
        "agent_id": wrappers[harness],
        "cost_control_mode_override": "on",
        "smart_routing_message": ROUTING_MESSAGE,
    }
    with (
        patch("omnigent.runtime._globals._caps", new=caps),
        patch.object(orchestration, "_routing_host_for_create", return_value=_host_reporting({})),
        patch.object(
            orchestration,
            "_pre_session_model_catalog",
            AsyncMock(return_value={harness: [pick]}),
        ),
    ):
        created = await client.post("/v1/sessions", json=body)

    assert created.status_code == 201, created.text
    assert created.json()["model_override"] == pick
    decisions = _routing_decision_items(db_uri, created.json()["id"])
    assert decisions and decisions[0]["router_source"] == "oss-llm"
    assert decisions[0]["applied"] is True
    # The router was asked first — the judge is the fallback, not the default.
    assert external.calls != []


@pytest.mark.parametrize(
    ("gateway", "extra"),
    [
        # The other family being off the gateway is not this pane's problem.
        ({"claude-native": True, "codex-native": False}, {}),
        # Unknown keeps the option.
        ({}, {}),
        # Routing not asked for: the guard has nothing to reject.
        ({"claude-native": False}, {"cost_control_mode_override": "off"}),
    ],
)
async def test_fixed_harness_create_is_allowed_when_its_own_family_is_backed(
    client: httpx.AsyncClient,
    db_uri: str,
    gateway: dict[str, bool],
    extra: dict[str, Any],  # type: ignore[explicit-any]
) -> None:
    from omnigent.server.routes._sessions import orchestration

    wrappers = await _native_wrappers(client, db_uri)
    routing_client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="sized task"))
    with patch.object(
        orchestration, "_routing_host_for_create", return_value=_host_reporting(gateway)
    ):
        created = await _create_fixed_harness_session(
            client, wrappers["claude-native"], routing_client, **extra
        )

    assert created.status_code == 201, created.text


async def test_child_create_is_not_gated_by_the_parents_gateway_state(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    from omnigent.server.routes._sessions import orchestration

    wrappers = await _native_wrappers(client, db_uri)
    parent = await client.post("/v1/sessions", json={"agent_id": wrappers["codex-native"]})
    assert parent.status_code == 201, parent.text

    # A child is routed by the spawn / turn paths in its parent's family, so the
    # create-time gate leaves it alone rather than 400ing a legitimate spawn.
    with patch.object(
        orchestration,
        "_routing_host_for_create",
        return_value=_host_reporting({"codex-native": False}),
    ):
        created = await _create_fixed_harness_session(
            client,
            wrappers["codex-native"],
            None,
            parent_session_id=parent.json()["id"],
        )

    assert created.status_code == 201, created.text


async def test_sdk_harness_create_is_not_gated_by_native_gateway_state(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    from omnigent.server.routes._sessions import orchestration

    # An SDK harness runs its inference through the server, not a host CLI, so a
    # host whose native CLIs are off the gateway says nothing about it.
    agent = await create_test_agent(client, name="ungatewayed-sdk-agent")
    with patch.object(
        orchestration,
        "_routing_host_for_create",
        return_value=_host_reporting({"claude-native": False, "codex-native": False}),
    ):
        created = await _create_fixed_harness_session(client, agent["id"], None)

    assert created.status_code == 201, created.text


# ── Pre-session candidate catalog ───────────────────────────────────────────
#
# A create routes before any session (and so any runner) exists, so the live
# per-session catalog is out of reach. The host answers instead, and the static
# table only tops up what it could not answer for. Either way the router's
# current arms must be servable candidates — an arm missing from the candidate
# set is substituted down a generation (a routed gpt-5-6-sol applying 5-5).

SOL = "databricks-gpt-5-6-sol"
LUNA = "databricks-gpt-5-6-luna"
HOST_GPT_CATALOG = [LUNA, SOL]


@pytest.mark.parametrize(
    ("verdict_model", "harness_candidates", "catalog", "expected_offer", "expected_pick"),
    [
        # The host's catalog is offered verbatim instead of the static table,
        # and a servable pick applies exactly (no substitution, no raw pick).
        (
            SOL,
            ("codex-native",),
            {"codex-native": HOST_GPT_CATALOG},
            {"codex-native": HOST_GPT_CATALOG},
            ("codex-native", SOL),
        ),
        # Out-of-family rows in the host's answer are filtered out before the
        # offer, so the router can never pick an unspawnable model.
        (
            SOL,
            ("codex-native",),
            {"codex-native": [*HOST_GPT_CATALOG, CLAUDE_MODEL]},
            {"codex-native": HOST_GPT_CATALOG},
            ("codex-native", SOL),
        ),
        # Hosts only resolve a pre-launch catalog for the CLIs that can report
        # one without running; the static table tops up the rest.
        (
            SOL,
            AUTO_NATIVE_ROUTING_HARNESSES,
            {"codex-native": HOST_GPT_CATALOG},
            {
                "codex-native": HOST_GPT_CATALOG,
                "claude-native": None,  # filled from infer_models below
            },
            ("codex-native", SOL),
        ),
        # No host answer at all: the static table is the whole offer.
        (
            GPT_MODEL,
            AUTO_NATIVE_ROUTING_HARNESSES,
            {},
            {"claude-native": None, "codex-native": None},
            ("codex-native", GPT_MODEL),
        ),
    ],
)
async def test_pre_session_catalog_is_offered_instead_of_the_static_table(
    verdict_model: str,
    harness_candidates: tuple[str, ...],
    catalog: dict[str, list[str]],
    expected_offer: dict[str, list[str] | None],
    expected_pick: tuple[str, str],
) -> None:
    routing_client = FakeRoutingClient(
        RoutingResult(model=verdict_model, rationale="deep reasoning")
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        harness, model, verdict, error = await route_session_harness(
            ROUTING_MESSAGE,
            harness_candidates=harness_candidates,
            catalog=catalog,
        )
    assert error is None
    # ``None`` in the expectation means "whatever the static table serves".
    want = {
        name: rows if rows is not None else infer_models(name)
        for name, rows in expected_offer.items()
    }
    assert routing_client.offered[0] == want
    assert (harness, model) == expected_pick
    # A servable pick applies exactly, so the card shows no divergent raw pick.
    assert verdict is not None
    assert "raw_model" not in verdict


@pytest.mark.parametrize(
    ("arm", "applied"),
    [
        ("gpt-5-6-sol", "databricks-gpt-5-6-sol"),
        ("gpt-5-6-luna", "databricks-gpt-5-6-luna"),
        # No discovery listing carries glm, so the static table is its only
        # source; it applies under the gateway's model-route spelling.
        ("glm-5-2", "system.ai.glm-5-2"),
    ],
)
def test_static_candidates_serve_the_routers_current_codex_arms(arm: str, applied: str) -> None:
    # The last-resort table must still cover the arms task_v1 picks from, or the
    # seam substitutes them down a generation.
    codex_models = infer_models("codex-native")
    assert codex_models is not None
    source = TaskV1RouteOptionSource(model_prefixes=["databricks-", "system.ai."])
    resolved = source.resolve_selection(
        RoutePick(model=arm),
        ["codex-native"],
        {"codex-native": list(codex_models)},
    )
    assert resolved is not None
    # Applied exactly: the same arm the router named, spelled for this
    # workspace's catalog vocabulary — never substituted down a generation.
    assert resolved.model == applied
    assert resolved.raw_model == arm


@pytest.mark.parametrize(
    ("has_registry", "answers", "expected"),
    [
        # The row's launchable ``model`` id, not its picker key; a harness the
        # host cannot answer for is simply absent.
        (
            True,
            {"claude-native": {"models": [{"id": "opus", "model": "databricks-claude-opus-4-8"}]}},
            {"claude-native": ["databricks-claude-opus-4-8"]},
        ),
        # ``routable_models`` widens the catalog past the picker rows (which
        # name the newest of each family only), so a frozen arm the workspace
        # still serves stays routable.
        (
            True,
            {
                "claude-native": {
                    "models": [{"id": "opus", "model": "databricks-claude-opus-5"}],
                    "routable_models": [
                        "databricks-claude-opus-5",
                        "databricks-claude-opus-4-8",
                    ],
                }
            },
            {
                "claude-native": [
                    "databricks-claude-opus-5",
                    "databricks-claude-opus-4-8",
                ]
            },
        ),
        # No live host: nothing to ask, so no catalog.
        (False, {}, {}),
    ],
)
async def test_pre_session_catalog_reads_the_hosts_model_options(
    has_registry: bool,
    answers: dict[str, dict[str, Any]],  # type: ignore[explicit-any]
    expected: dict[str, list[str]],
) -> None:
    from omnigent.host.frames import decode_host_frame

    conn = SimpleNamespace(host_id="host_1", pending_model_options={})

    def send_text(host_conn: Any, frame: str) -> None:  # type: ignore[explicit-any]
        decoded = decode_host_frame(frame)
        answer = answers.get(decoded.harness)
        future = host_conn.pending_model_options[decoded.request_id]
        future.set_result(
            {"status": "ok", **answer}
            if answer is not None
            else {"status": "failed", "error": "unsupported"}
        )

    registry = (
        SimpleNamespace(get=lambda host_id: conn, send_text=send_text) if has_registry else None
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host_registry=registry)))
    catalog = await _pre_session_model_catalog(
        cast("Any", request),
        _host(None),
        AUTO_NATIVE_ROUTING_HARNESSES,
    )
    assert catalog == expected
    # No in-flight request is left behind on any path.
    assert conn.pending_model_options == {}


def _native_conv(  # type: ignore[explicit-any]
    session_id: str,
    *,
    routing: bool = False,
    archived: bool = False,
) -> Any:
    from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT

    return SimpleNamespace(
        id=session_id,
        labels={"omnigent.wrapper": CLAUDE_NATIVE_CODING_AGENT.wrapper_label},
        cost_control_mode_override="on" if routing else None,
        harness_override=None,
        archived=archived,
    )


async def test_turn_catalog_and_verdict_follow_the_panes_vocabulary() -> None:
    """The picker rows bound both what a turn may pick and what it may claim."""
    from omnigent.server.routes._sessions.orchestration import (
        _model_options_cache,
        _native_turn_catalog,
        _routed_turn_model_spelling,
        _unapplied_routed_verdict,
    )

    conv = _native_conv("conv_vocab")
    _model_options_cache["conv_vocab"] = [
        {"id": "opus", "model": "databricks-claude-opus-5"},
        {"id": "sonnet_5", "model": "databricks-claude-opus-4-8"},
    ]
    try:
        assert await _native_turn_catalog("conv_vocab", conv) == [
            "databricks-claude-opus-5",
            "databricks-claude-opus-4-8",
        ]
        # An applicable pick answers with the pane's own spelling for it.
        assert (
            _routed_turn_model_spelling("conv_vocab", conv, "databricks-claude-opus-4-8")
            == "databricks-claude-opus-4-8"
        )
        assert (
            _routed_turn_model_spelling("conv_vocab", conv, "databricks-claude-opus-5") == "opus"
        )
        assert (
            _routed_turn_model_spelling("conv_vocab", conv, "databricks-claude-sonnet-5") is None
        )
        verdict = {"rationale": "deep refactor", "applied": True}
        downgraded = _unapplied_routed_verdict("databricks-claude-sonnet-5", verdict)
        assert downgraded["applied"] is False
        assert "Not applied" in downgraded["rationale"]
        # The caller's verdict is never mutated in place.
        assert verdict == {"rationale": "deep refactor", "applied": True}
    finally:
        _model_options_cache.pop("conv_vocab", None)

    # A picker row with no ``model`` still names its vocabulary through ``id``.
    _model_options_cache["conv_rowkey"] = [{"id": "opus"}]
    try:
        assert await _native_turn_catalog("conv_rowkey", _native_conv("conv_rowkey")) == ["opus"]
    finally:
        _model_options_cache.pop("conv_rowkey", None)

    # Not a claude-native session, and a native one with no rows cached: both
    # leave the caller's own resolution and claim untouched.
    plain = SimpleNamespace(id="conv_plain", labels={})
    assert await _native_turn_catalog("conv_plain", plain) is None
    assert await _native_turn_catalog("conv_vocab", conv) is None
    assert (
        _routed_turn_model_spelling("conv_vocab", conv, "databricks-gpt-5-5")
        == "databricks-gpt-5-5"
    )


async def test_turn_catalog_refetches_a_stale_pre_launch_catalog() -> None:
    """A pre-launch host catalog never bounds a turn once a runner is bound."""
    from omnigent.server.routes._sessions.orchestration import (
        _model_options_cache,
        _model_options_stale,
        _native_turn_catalog,
        _routed_turn_model_spelling,
    )

    session_id = "conv_stale"
    conv = _native_conv(session_id)
    # Hydrated from the host BEFORE launch: the opus alias carries the
    # workspace default, not this session's routed pin.
    _model_options_cache[session_id] = [{"id": "opus", "model": "databricks-claude-opus-5"}]
    _model_options_stale.add(session_id)
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        # What the runner reports post-launch: the launch config pinned the
        # routed arm onto the opus alias.
        return httpx.Response(
            200, json={"models": [{"id": "opus", "model": "databricks-claude-opus-4-8"}]}
        )

    runner_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner.invalid"
    )
    try:
        assert await _native_turn_catalog(session_id, conv, runner_client) == [
            "databricks-claude-opus-4-8"
        ]
        assert requested == [f"/v1/sessions/{session_id}/model-options"]
        assert session_id not in _model_options_stale
        # The same refreshed vocabulary now spells the pick for the apply.
        assert (
            _routed_turn_model_spelling(session_id, conv, "databricks-claude-opus-4-8") == "opus"
        )
        # A fresh entry is served without a second fetch.
        assert await _native_turn_catalog(session_id, conv, runner_client) == [
            "databricks-claude-opus-4-8"
        ]
        assert len(requested) == 1
    finally:
        await runner_client.aclose()
        _model_options_cache.pop(session_id, None)
        _model_options_stale.discard(session_id)


async def test_launch_prefetch_takes_the_stale_refresh_off_the_turn_path() -> None:
    """
    Warming the catalog when the runner binds is what makes a first turn fast.

    The stale-catalog refresh is the biggest single term in routing's
    pre-router latency: the turn awaits a fetch that retries a booting runner.
    Started at launch instead, it has landed by the time the prompt arrives and
    the turn reads the cache — no wait, no second fetch.
    """
    from omnigent.server.routes._sessions.orchestration import (
        _model_options_cache,
        _model_options_inflight,
        _model_options_stale,
        _native_turn_catalog,
    )
    from omnigent.server.routes.sessions import prefetch_session_routing_catalogs

    session_id = "conv_prefetch_launch"
    conv = _native_conv(session_id, routing=True)
    # What the host resolved before launch — stale, so a turn would refetch it.
    _model_options_cache[session_id] = [{"id": "opus", "model": "databricks-claude-opus-5"}]
    _model_options_stale.add(session_id)
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"workers": {"self": {"models": [{"id": "opus"}]}}})
        return httpx.Response(
            200, json={"models": [{"id": "opus", "model": "databricks-claude-opus-4-8"}]}
        )

    runner_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner.invalid"
    )
    try:
        prefetch_session_routing_catalogs(session_id, conv, runner_client)
        inflight = _model_options_inflight.get(session_id)
        assert inflight is not None
        await inflight
        assert session_id not in _model_options_stale
        requested.clear()

        assert await _native_turn_catalog(session_id, conv, runner_client) == [
            "databricks-claude-opus-4-8"
        ]
        # The turn neither waited nor re-fetched: the launch prefetch answered.
        assert requested == []
    finally:
        await asyncio.sleep(0)
        await runner_client.aclose()
        _model_options_cache.pop(session_id, None)
        _model_options_stale.discard(session_id)


def _prefetch_client(requested: list[str], *, fail: bool = False) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if fail:
            # What a tunnel torn down mid-prefetch raises: not an httpx error,
            # so the fetch helpers do not catch it themselves.
            raise RuntimeError("tunnel closed")
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"workers": {"self": {"models": [{"id": "opus"}]}}})
        return httpx.Response(
            200, json={"models": [{"id": "opus", "model": "databricks-claude-opus-4-8"}]}
        )

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner.invalid"
    )


async def test_plain_session_gets_no_catalog_prefetch_on_runner_connect() -> None:
    """
    A reconnect must cost a plain pane nothing.

    The reconnect callback walks every session bound to the runner, so an
    ungated prefetch turned one host's tunnel flap into two runner round trips
    per plain pane — work only Smart Routing ever reads — starving the session
    re-init running alongside it.
    """
    from omnigent.server.routes._sessions.common import _catalog_prefetch_tasks
    from omnigent.server.routes._sessions.orchestration import _model_options_inflight
    from omnigent.server.routes.sessions import prefetch_session_routing_catalogs

    session_id = "conv_prefetch_plain"
    requested: list[str] = []
    runner_client = _prefetch_client(requested)
    try:
        before = set(_catalog_prefetch_tasks)
        prefetch_session_routing_catalogs(session_id, _native_conv(session_id), runner_client)
        await asyncio.sleep(0)

        assert session_id not in _model_options_inflight
        assert set(_catalog_prefetch_tasks) == before
        assert requested == []
    finally:
        await runner_client.aclose()


async def test_archived_routed_session_gets_no_catalog_prefetch() -> None:
    """An archived session is not going to route a turn, so it warms nothing."""
    from omnigent.server.routes._sessions.common import _catalog_prefetch_tasks
    from omnigent.server.routes._sessions.orchestration import _model_options_inflight
    from omnigent.server.routes.sessions import prefetch_session_routing_catalogs

    session_id = "conv_prefetch_archived"
    conv = _native_conv(session_id, routing=True, archived=True)
    requested: list[str] = []
    runner_client = _prefetch_client(requested)
    try:
        before = set(_catalog_prefetch_tasks)
        prefetch_session_routing_catalogs(session_id, conv, runner_client)
        await asyncio.sleep(0)

        assert session_id not in _model_options_inflight
        assert set(_catalog_prefetch_tasks) == before
        assert requested == []
    finally:
        await runner_client.aclose()


async def test_routed_live_session_still_warms_both_catalogs() -> None:
    """The gate keeps the case it was built for: a routed pane warms both."""
    from omnigent.server.routes._sessions.common import _catalog_prefetch_tasks
    from omnigent.server.routes._sessions.orchestration import (
        _model_options_cache,
        _model_options_inflight,
    )
    from omnigent.server.routes.sessions import prefetch_session_routing_catalogs

    session_id = "conv_prefetch_routed"
    conv = _native_conv(session_id, routing=True)
    requested: list[str] = []
    runner_client = _prefetch_client(requested)
    try:
        before = set(_catalog_prefetch_tasks)
        prefetch_session_routing_catalogs(session_id, conv, runner_client)
        started = set(_catalog_prefetch_tasks) - before
        options_task = _model_options_inflight.get(session_id)
        assert options_task is not None
        assert len(started) == 1
        await asyncio.gather(options_task, *started)

        assert sorted(requested) == [
            f"/v1/sessions/{session_id}/model-options",
            f"/v1/sessions/{session_id}/models",
        ]
        assert _model_options_cache[session_id] == [
            {"id": "opus", "model": "databricks-claude-opus-4-8"}
        ]
    finally:
        await runner_client.aclose()
        _model_options_cache.pop(session_id, None)


async def test_failing_prefetch_retrieves_its_own_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A prefetch that raises must not leave an unretrieved task exception.

    Nothing awaits these tasks, so an escaping error surfaces only as asyncio's
    unretrieved-exception warning at GC time — noise that hides real failures.
    """
    from omnigent.server.routes._sessions.common import _catalog_prefetch_tasks
    from omnigent.server.routes._sessions.orchestration import _model_options_inflight
    from omnigent.server.routes.sessions import prefetch_session_routing_catalogs

    session_id = "conv_prefetch_raises"
    conv = _native_conv(session_id, routing=True)
    requested: list[str] = []
    runner_client = _prefetch_client(requested, fail=True)
    try:
        with caplog.at_level(logging.DEBUG, logger="omnigent.server.routes.sessions"):
            before = set(_catalog_prefetch_tasks)
            prefetch_session_routing_catalogs(session_id, conv, runner_client)
            started = set(_catalog_prefetch_tasks) - before
            options_task = _model_options_inflight.get(session_id)
            assert options_task is not None
            await asyncio.gather(options_task, *started)

        assert options_task.exception() is None
        assert [task.exception() for task in started] == [None]
        assert any("Catalog prefetch failed" in record.message for record in caplog.records)
    finally:
        await runner_client.aclose()


async def test_turn_catalog_keeps_a_stale_catalog_when_the_refetch_fails() -> None:
    """A stale vocabulary still bounds the turn when the runner cannot answer."""
    from omnigent.server.routes._sessions.orchestration import (
        _model_options_cache,
        _model_options_stale,
        _native_turn_catalog,
    )

    session_id = "conv_stale_fail"
    conv = _native_conv(session_id)
    _model_options_cache[session_id] = [{"id": "opus", "model": "databricks-claude-opus-5"}]
    _model_options_stale.add(session_id)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("runner gone")

    runner_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner.invalid"
    )
    try:
        assert await _native_turn_catalog(session_id, conv, runner_client) == [
            "databricks-claude-opus-5"
        ]
        # No runner bound at all: the cache is served untouched.
        assert await _native_turn_catalog(session_id, conv, None) == ["databricks-claude-opus-5"]
    finally:
        await runner_client.aclose()
        _model_options_cache.pop(session_id, None)
        _model_options_stale.discard(session_id)


async def test_turn_catalog_gives_up_on_a_refetch_that_never_finishes() -> None:
    """
    Routing waits a bounded moment for a sharper catalog, then routes anyway.

    The fetch retries a booting runner across several delays (~30s worst case),
    and this await sits on the routing hazard path — a wedged catalog would
    stall the prompt as surely as a wedged router. The await is bounded and the
    single-flight is left running, so the next turn gets the sharper answer.
    """
    import asyncio

    from omnigent.server.routes._sessions.orchestration import (
        _ROUTING_CATALOG_WAIT_S,
        _model_options_cache,
        _model_options_inflight,
        _model_options_stale,
        _native_turn_catalog,
    )

    session_id = "conv_stale_hang"
    conv = _native_conv(session_id)
    _model_options_cache[session_id] = [{"id": "opus", "model": "databricks-claude-opus-5"}]
    _model_options_stale.add(session_id)

    started = asyncio.Event()

    async def _never_finishes() -> None:
        started.set()
        await asyncio.sleep(3600)

    hanging = asyncio.create_task(_never_finishes())
    _model_options_inflight[session_id] = hanging
    await started.wait()

    waited: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def _recording_wait_for(awaitable: Any, timeout: float | None) -> Any:
        waited.append(timeout)
        # Zero, so the test never actually sits out the budget.
        return await real_wait_for(awaitable, 0)

    try:
        with patch("asyncio.wait_for", new=_recording_wait_for):
            # The stale vocabulary is served rather than the turn hanging on it.
            assert await _native_turn_catalog(session_id, conv, _null_runner_client()) == [
                "databricks-claude-opus-5"
            ]
        assert waited == [_ROUTING_CATALOG_WAIT_S]
        # Bounded in single-digit seconds, like every other routing budget.
        assert 0 < _ROUTING_CATALOG_WAIT_S < 10.0
        # Shielded: giving up on the WAIT does not cancel the fetch, so the
        # cache still fills for whoever reads it next.
        assert not hanging.cancelled()
        assert not hanging.done()
    finally:
        hanging.cancel()
        _model_options_inflight.pop(session_id, None)
        _model_options_cache.pop(session_id, None)
        _model_options_stale.discard(session_id)


def _null_runner_client() -> Any:
    """A runner client stand-in that is never actually called.

    ``_refresh_stale_native_model_options`` only needs a non-``None`` client to
    reach its in-flight branch; the hanging task is what it waits on.

    :returns: An object standing in for the bound runner's HTTP client.
    """
    return SimpleNamespace()


async def test_routing_authorizes_host_ownership_before_touching_the_host() -> None:
    """A foreign ``host_id`` is rejected before any host read or frame push."""
    from fastapi import HTTPException

    from omnigent.server.routes._sessions.orchestration import _resolve_native_smart_routing

    sent: list[str] = []
    conn = SimpleNamespace(host_id="host_1", pending_model_options={})
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                host_store=SimpleNamespace(get_host=lambda host_id: _host(None)),
                host_registry=SimpleNamespace(
                    get=lambda host_id: conn,
                    send_text=lambda host_conn, frame: sent.append(frame),
                ),
            )
        )
    )
    body = SimpleNamespace(
        host_id="host_1",
        smart_routing_message=ROUTING_MESSAGE,
    )

    with pytest.raises(HTTPException) as excinfo:
        await _resolve_native_smart_routing(
            cast("Any", body),
            cast("Any", request),
            "mallory@example.com",
        )
    assert excinfo.value.status_code == 403
    # Nothing was pushed into the owner's live host connection.
    assert sent == []
    assert conn.pending_model_options == {}


# ── A routing OUTAGE never blocks the create ────────────────────────────────
#
# The owner's ruling on the create path: routing that FAILS must degrade to an
# unrouted session, promptly. (Routing that is unconfigurable for this host is a
# different answer and stays a rejection — that gate is deliberate.)


#: The five failure modes a routing call presents, as exceptions raised out of
#: the routing client: a gateway 500, a 401, a wedged router, a garbled body,
#: and an unreachable endpoint.
_ROUTING_FAILURES: list[tuple[str, Exception]] = [
    (
        "gateway_500",
        httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=httpx.Request("POST", "https://ws.invalid/routes:select"),
            response=httpx.Response(500, text="upstream router unavailable"),
        ),
    ),
    (
        "unauthorized",
        httpx.HTTPStatusError(
            "401 Unauthorized",
            request=httpx.Request("POST", "https://ws.invalid/routes:select"),
            response=httpx.Response(401, text="invalid access token"),
        ),
    ),
    ("timeout", httpx.ReadTimeout("routes:select timed out")),
    ("malformed_body", json.JSONDecodeError("Expecting value", "<not json>", 0)),
    ("relay_unreachable", httpx.ConnectError("connection refused")),
]

_FAILURE_IDS = [label for label, _ in _ROUTING_FAILURES]


@pytest.mark.parametrize(("label", "failure"), _ROUTING_FAILURES, ids=_FAILURE_IDS)
async def test_a_routing_outage_still_creates_the_auto_harness_session(
    client: httpx.AsyncClient,
    db_uri: str,
    label: str,
    failure: Exception,
) -> None:
    """
    The auto route lands on an installed CLI instead of erroring the create.

    The harness itself comes from the router here, so a failure has nothing to
    bind — and a create that 4xx'd would leave the user with no session at all.
    It falls back to the first installed CLI on the CLI's own default model.
    """
    del label
    wrappers = await _native_wrappers(client, db_uri)
    created = await _create_smart_routing_session(
        client, wrappers, FakeRoutingClient(None, error=failure)
    )
    assert created.status_code == 201, created.text

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(created.json()["id"])
    assert conv is not None
    # A real terminal, on the CLI's own default model, with nothing pinned.
    assert conv.agent_id == wrappers["claude-native"]
    assert conv.model_override is None


@pytest.mark.parametrize(("label", "failure"), _ROUTING_FAILURES, ids=_FAILURE_IDS)
async def test_a_routing_outage_still_creates_the_fixed_harness_session(
    client: httpx.AsyncClient,
    db_uri: str,
    label: str,
    failure: Exception,
) -> None:
    """
    A fixed-harness create launches unrouted, with a card.

    The harness was the caller's own choice, so only the model was ever at
    stake: nothing is pinned, the session opens on the CLI's default, and the
    declined decision names the outage rather than leaving it invisible.
    """
    del label
    wrappers = await _native_wrappers(client, db_uri)
    created = await _create_fixed_harness_session(
        client, wrappers["claude-native"], FakeRoutingClient(None, error=failure)
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    assert conv.model_override is None
    # The route-once gate is not claimed by a failure: the session's own
    # first-message hook must still be free to try.
    assert ROUTING_DECISION_LABEL_KEY not in conv.labels

    decisions = _routing_decision_items(db_uri, session_id)
    assert len(decisions) == 1
    assert decisions[0]["applied"] is False
    assert decisions[0]["rationale"]
