"""Create-time Smart Routing onto a native terminal harness.

The landing screen's top-level "Smart Routing" harness sends
``harness_override: "auto"`` with a native wrapper agent as a placeholder and
the first message as ``smart_routing_message``. A native terminal launches with
the session row, so the harness is decided during the create — not on the first
message event the way the bundle-agent auto path does — and the session is
rebound to the wrapper the router picked.

A create pinned to one native harness (the CLI's ``omni claude --smart-routing``,
or the web UI picking a harness with routing on) routes on the same seam for the
same reason, but only the MODEL: its turns originate in the TUI, so the server
never sees the first message pre-inference and the turn gate would never fire.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.runner.subagent_routing import AUTO_HARNESS_LABEL_KEY, ROUTING_DECISION_LABEL_KEY
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


async def _create_smart_routing_session(
    client: httpx.AsyncClient,
    wrappers: dict[str, str],
    routing_client: FakeRoutingClient | None,
) -> httpx.Response:
    """POST the landing screen's Smart Routing create payload.

    :param client: Test HTTP client.
    :param wrappers: Registered wrapper ids from :func:`_native_wrappers`.
    :param routing_client: Stub router, or ``None`` to leave routing unconfigured.
    :returns: The raw create response.
    """
    body = {
        # The Claude wrapper is the client-side placeholder; the server rebinds.
        "agent_id": wrappers["claude-native"],
        "harness_override": "auto",
        "cost_control_mode_override": "on",
        "smart_routing_message": ROUTING_MESSAGE,
    }
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        return await client.post("/v1/sessions", json=body)


async def _create_fixed_harness_session(
    client: httpx.AsyncClient,
    agent_id: str,
    routing_client: FakeRoutingClient | None,
    **extra: Any,  # type: ignore[explicit-any]
) -> httpx.Response:
    """POST a Smart Routing create for a session pinned to one harness.

    The CLI's ``omni claude --smart-routing`` shape: a fixed harness (the native
    wrapper agent, no ``harness_override``) plus routing on and the prompt as
    ``smart_routing_message``.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :param routing_client: Stub router, or ``None`` to leave routing unconfigured.
    :param extra: Extra create-body fields, merged last.
    :returns: The raw create response.
    """
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "cost_control_mode_override": "on",
        "smart_routing_message": ROUTING_MESSAGE,
        **extra,
    }
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
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


async def test_fixed_harness_create_does_not_pin_an_unrunnable_pick(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    # A gpt pick for a Claude terminal: the harness is not negotiable here, so
    # nothing is pinned and the card says so.
    routing_client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="narrow change"))
    created = await _create_fixed_harness_session(
        client, wrappers["claude-native"], routing_client
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
    assert GPT_MODEL in decisions[0]["rationale"]


async def test_fixed_harness_create_fails_open_when_the_router_is_down(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    wrappers = await _native_wrappers(client, db_uri)
    created = await _create_fixed_harness_session(client, wrappers["codex-native"], None)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conv is not None
    # The session still opens a terminal, on the CLI's own default model.
    assert conv.model_override is None
    assert ROUTING_DECISION_LABEL_KEY not in conv.labels

    decisions = _routing_decision_items(db_uri, session_id)
    assert len(decisions) == 1
    assert decisions[0]["applied"] is False
    assert "not configured" in decisions[0]["rationale"]


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


def _host(readiness: dict[str, Any] | None) -> Host:  # type: ignore[explicit-any]
    return Host(
        host_id="host_1",
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


def test_static_candidates_serve_the_routers_current_codex_arms() -> None:
    # The last-resort table must still cover the arms task_v1 picks from, or the
    # seam substitutes them down a generation.
    codex_models = infer_models("codex-native")
    assert codex_models is not None
    source = TaskV1RouteOptionSource(model_prefixes=["databricks-"])
    for arm in ("gpt-5-6-sol", "gpt-5-6-luna"):
        resolved = source.resolve_selection(
            RoutePick(model=arm),
            ["codex-native"],
            {"codex-native": list(codex_models)},
        )
        assert resolved is not None
        # Applied exactly: the same model the router named, prefixed for this
        # workspace's catalog vocabulary.
        assert resolved.model == f"databricks-{arm}"
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


def _native_conv(session_id: str) -> Any:  # type: ignore[explicit-any]
    from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT

    return SimpleNamespace(
        id=session_id,
        labels={"omnigent.wrapper": CLAUDE_NATIVE_CODING_AGENT.wrapper_label},
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
        assert requested == [f"/v1/sessions/{session_id}/claude-model-options"]
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
