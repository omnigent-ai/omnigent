"""Codex-native intelligent routing must honor live effort capabilities.

A codex-native session persisted on ``reasoning_effort="max"`` with
intelligent routing enabled routes its first message onto whatever arm the
router picks from the live runner catalog — the catalog's per-model
``supportedReasoningEfforts`` (which Codex's ``model/list`` already exposes)
is never consulted. A pick whose live entry tops out at ``xhigh`` is then
pinned and the turn is forwarded with ``reasoning: {"effort": "max"}``, an
invalid pairing the provider rejects with ``invalid_value`` — the user's
turn fails instead of keeping the session's working model.

Journey driven here, at the server boundary (the codex CLI and gateway are
not needed to reach the routing defect — the incompatible pin is decided
and forwarded entirely server-side):

1. create a codex-native session with ``reasoning_effort="max"`` and
   intelligent routing on (``cost_control_mode_override="on"``) — the real
   ``POST /v1/sessions`` the web composer calls;
2. the bound runner serves a live catalog where the cheap arm
   (``databricks-gpt-5-5``) advertises efforts only through ``xhigh`` and
   the premium arm (``databricks-gpt-5-6-sol``) advertises ``max``;
3. send the session's first user message through the server's turn
   dispatch (the same ``POST /events`` forward path);
4. the cost-aware router prefers the cheapest offered arm — exactly what
   routed the reported session onto ``gpt-5.5``.

Expected (the issue's contract): with an explicit session effort, routing
considers only live models that advertise that effort; when none does, the
session's model is left unchanged. Asserted on the user-facing outcome —
the forwarded turn body and the persisted ``model_override`` — so any
correct fix (filtering the offer, or declining an incompatible pick)
turns these tests green without pinning the fix's internal shape.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from omnigent.harness_plugins import CODEX_NATIVE_CODING_AGENT
from omnigent.server.routes._sessions import orchestration as orchestration_module
from omnigent.server.schemas import SessionEventInput
from omnigent.server.smart_routing import RoutingResult, invalidate_runner_catalog
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import FakeCaps, create_test_agent

pytestmark = pytest.mark.asyncio

#: The cheap arm the router prefers. Its live entry advertises efforts only
#: through ``xhigh`` — the reported ``gpt-5.5`` shape.
XHIGH_ONLY_MODEL = "databricks-gpt-5-5"

#: The premium arm whose live entry advertises ``max`` (the ``sol`` ladder
#: reaches ``ultra``); the only arm a ``max`` session may be routed onto.
MAX_CAPABLE_MODEL = "databricks-gpt-5-6-sol"

#: Efforts advertised by each live catalog row, mirroring Codex's
#: ``model/list`` ``supportedReasoningEfforts``.
LIVE_EFFORTS: dict[str, list[str]] = {
    XHIGH_ONLY_MODEL: ["low", "medium", "high", "xhigh"],
    MAX_CAPABLE_MODEL: ["low", "medium", "high", "xhigh", "max", "ultra"],
}


def _efforts_payload(model_id: str) -> list[dict[str, str]]:
    """Shape *model_id*'s ladder as Codex ``model/list`` effort rows.

    :param model_id: Catalog id, e.g. ``"databricks-gpt-5-5"``.
    :returns: ``supportedReasoningEfforts``-shaped entries.
    """
    return [{"reasoningEffort": effort} for effort in LIVE_EFFORTS[model_id]]


def _catalog_rows(model_ids: list[str]) -> list[dict[str, Any]]:
    """Live runner catalog rows for *model_ids*, cheapest tier first.

    Each row carries the effort ladder Codex's live ``model/list`` exposes,
    so the routing layer has everything it needs to filter on effort.

    :param model_ids: Catalog ids to serve, e.g. ``[XHIGH_ONLY_MODEL]``.
    :returns: ``sys_list_models``-shaped model rows.
    """
    tiers = {XHIGH_ONLY_MODEL: "economy", MAX_CAPABLE_MODEL: "premium"}
    return [
        {
            "id": model_id,
            "family": "gpt",
            "cost_tier": tiers[model_id],
            "supportedReasoningEfforts": _efforts_payload(model_id),
        }
        for model_id in model_ids
    ]


class _CheapestFirstRouter:
    """Router double picking the cheapest offered arm, like the live router.

    The reported session was routed onto ``gpt-5.5`` because the cost-aware
    router prefers the cheapest sufficient arm; offers arrive cheapest-first
    (the catalog sort), so "first offered" reproduces that preference while
    staying deterministic. Selecting from the *offer* — rather than a canned
    verdict — keeps the tests honest against a fix that filters candidates.

    :ivar offered: The ``available_models`` mapping per :meth:`route` call.
    """

    def __init__(self) -> None:
        self.last_error: str | None = None
        self.offered: list[dict[str, list[str]]] = []

    async def route(
        self, message: str, available_models: dict[str, list[str]]
    ) -> RoutingResult | None:
        """Record the offer and pick the first (cheapest) offered model."""
        del message
        offer = {name: list(models) for name, models in available_models.items()}
        self.offered.append(offer)
        for models in offer.values():
            if models:
                return RoutingResult(
                    model=models[0],
                    rationale="trivial task → cheapest sufficient arm",
                    harness="codex",
                )
        return None


async def _codex_native_max_session(
    client: httpx.AsyncClient,
    db_uri: str,
    *,
    agent_name: str,
) -> tuple[Any, SqlAlchemyConversationStore]:
    """Create a codex-native session pinned to ``max`` effort, routing on.

    The create call is the real ``POST /v1/sessions`` the web composer
    issues for a codex-native pane: wrapper label, per-session effort, and
    the intelligent-routing toggle.

    :param client: Test HTTP client bound to the app.
    :param db_uri: SQLite URI for the store the app writes.
    :param agent_name: Unique agent name for this test.
    :returns: ``(conversation row, store)`` for the created session.
    """
    agent = await create_test_agent(
        client,
        name=agent_name,
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
        include_llm=False,
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "cost_control_mode_override": "on",
            "reasoning_effort": "max",
            "labels": {"omnigent.wrapper": CODEX_NATIVE_CODING_AGENT.wrapper_label},
        },
    )
    assert resp.status_code == 201, resp.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.get_conversation(resp.json()["id"])
    assert conv is not None
    assert conv.reasoning_effort == "max", "create must persist the session effort"
    return conv, conv_store


def _runner_serving_catalog(
    model_ids: list[str],
    forwarded: list[dict[str, Any]],
) -> httpx.AsyncClient:
    """A runner client double serving the live catalog and acking turns.

    Serves ``GET /v1/sessions/{id}/models`` (the routing catalog) and
    ``GET /v1/sessions/{id}/codex-model-options`` (Codex's raw
    ``model/list`` rows) with the same effort ladders, so any fix source —
    catalog metadata or live model options — finds the capabilities.

    :param model_ids: Catalog ids the runner advertises.
    :param forwarded: Sink for bodies POSTed to the events endpoint.
    :returns: Async client backed by a mock transport.
    """
    rows = _catalog_rows(model_ids)

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "workers": {"self": {"source": "catalog", "verified": True, "models": rows}}
                },
            )
        if request.method == "GET" and path.endswith("/codex-model-options"):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": row["id"],
                            "model": row["id"],
                            "supportedReasoningEfforts": row["supportedReasoningEfforts"],
                        }
                        for row in rows
                    ]
                },
            )
        if request.method == "POST" and path.endswith("/events"):
            forwarded.append(json.loads(request.content))
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(200, json={})

    return httpx.AsyncClient(
        base_url="http://runner.test", transport=httpx.MockTransport(_handler)
    )


def _first_message_body() -> SessionEventInput:
    """The session's first user message — the turn intelligent routing decides."""
    return SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "rename one variable"}]},
    )


async def _dispatch_first_message(
    conv: Any,
    conv_store: SqlAlchemyConversationStore,
    router: _CheapestFirstRouter,
    model_ids: list[str],
) -> list[dict[str, Any]]:
    """Send the first message through the server's turn-dispatch path.

    :param conv: The session's conversation row.
    :param conv_store: Store the dispatch persists through.
    :param router: Routing-client double installed for the call.
    :param model_ids: Live catalog the bound runner serves.
    :returns: Bodies the server forwarded to the runner's events endpoint.
    """
    invalidate_runner_catalog(conv.id)
    forwarded: list[dict[str, Any]] = []
    async with _runner_serving_catalog(model_ids, forwarded) as runner_client:
        with patch(
            "omnigent.runtime._globals._caps",
            new=FakeCaps(routing_client=router),
        ):
            await orchestration_module._forward_event_to_runner(
                conv.id,
                conv,
                _first_message_body(),
                conv_store,
                runner_client,
            )
    return forwarded


def _assert_turn_effort_compatible(
    forwarded: list[dict[str, Any]],
    conv_store: SqlAlchemyConversationStore,
    session_id: str,
) -> None:
    """Assert the routed turn cannot pair ``max`` with an ``xhigh``-capped pick.

    The user-facing contract from the issue: with an explicit session
    effort, a routed pick must advertise that effort in the live catalog;
    otherwise routing leaves the session's model unchanged. A forwarded
    ``model_override`` whose live ladder lacks ``max`` — alongside
    ``reasoning: {"effort": "max"}`` — is exactly the pairing the provider
    rejects with ``invalid_value``, failing the user's turn.

    :param forwarded: Bodies the server forwarded to the runner.
    :param conv_store: Store to re-read the persisted pin from.
    :param session_id: The session under test.
    """
    assert len(forwarded) == 1, forwarded
    turn = forwarded[0]
    assert (turn.get("reasoning") or {}).get("effort") == "max", turn

    max_capable = {m for m, efforts in LIVE_EFFORTS.items() if "max" in efforts}
    pinned = turn.get("model_override")
    assert pinned is None or pinned in max_capable, (
        f"intelligent routing forwarded model_override={pinned!r} with "
        f"reasoning effort 'max', but that model's live catalog entry only "
        f"advertises {LIVE_EFFORTS.get(pinned, [])} — the provider rejects "
        f"this pairing with invalid_value and the turn fails. Routing must "
        f"only pick live models advertising the session's effort, or leave "
        f"the model unchanged."
    )

    refreshed = conv_store.get_conversation(session_id)
    assert refreshed is not None
    persisted = refreshed.model_override
    assert persisted is None or persisted in max_capable, (
        f"intelligent routing persisted model_override={persisted!r} for a "
        f"session whose effort is 'max'; every later turn now runs on an "
        f"effort-incompatible model."
    )


async def test_routed_first_message_must_not_pin_an_effort_incompatible_model(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A ``max`` session's routed pick must advertise ``max`` in the live catalog.

    The live catalog offers a cheap ``xhigh``-capped arm and a premium
    ``max``-capable arm. The cost-aware router prefers the cheap arm — so
    unless routing filters (or declines) on the session's effort, the
    session is pinned onto a model that cannot run its turns.
    """
    conv, conv_store = await _codex_native_max_session(
        client, db_uri, agent_name="routing-effort-caps-mixed"
    )
    router = _CheapestFirstRouter()

    forwarded = await _dispatch_first_message(
        conv, conv_store, router, [XHIGH_ONLY_MODEL, MAX_CAPABLE_MODEL]
    )

    # Routing ran against the live catalog (guards against the test passing
    # vacuously because routing was skipped outright).
    assert router.offered, "intelligent routing must have consulted the router"
    _assert_turn_effort_compatible(forwarded, conv_store, conv.id)


async def test_routing_leaves_the_model_alone_when_no_live_model_supports_the_effort(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """With every live arm capped below ``max``, routing must change nothing.

    The issue's explicit fallback: when no live model advertises the
    session's effort, routing leaves the current model unchanged rather
    than pinning a pick that fails every subsequent turn.
    """
    conv, conv_store = await _codex_native_max_session(
        client, db_uri, agent_name="routing-effort-caps-none"
    )
    router = _CheapestFirstRouter()

    forwarded = await _dispatch_first_message(conv, conv_store, router, [XHIGH_ONLY_MODEL])

    assert len(forwarded) == 1, forwarded
    turn = forwarded[0]
    assert turn.get("model_override") is None, (
        f"no live model advertises effort 'max', yet routing pinned "
        f"model_override={turn.get('model_override')!r} — the session's "
        f"model must be left unchanged."
    )
    refreshed = conv_store.get_conversation(conv.id)
    assert refreshed is not None
    assert refreshed.model_override is None, (
        f"routing persisted model_override={refreshed.model_override!r} even "
        f"though no live model supports the session's 'max' effort."
    )
