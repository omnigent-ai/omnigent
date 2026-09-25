"""Regression: Smart Routing must not apply retired ``databricks-*`` endpoint ids.

The reported user journey (a Databricks-backed deployment on a workspace with
"Enforce Unity Gateway" enabled, Smart Routing on, a substantial coding task
that routes to the GPT 5.6 Sol arm) ends with the routed turn failing:

    inner executor error: unexpected status 501 Not Implemented
    'databricks-gpt-5-6-sol' is no longer available.
    Use Unity Catalog model services (v3).

The root cause the ticket names is that Smart Routing selects/localizes its pick
as a legacy ``databricks-*`` serving-endpoint id (``databricks-gpt-5-6-sol``)
rather than the Unity Catalog model-service spelling (``system.ai.gpt-5-6-sol``)
that a Unity-Gateway workspace still serves. So the retired id reaches the
executor and is sent to the gateway, which rejects it.

These tests drive the REAL server routing seam (``route_turn`` →
``infer_models`` candidate table → backend selection → servable-alias
resolution), not a hand-written end state: the retired id emerges from the
product's own candidate table and resolution, and the judge only classifies the
task as COMPLEX (→ most-capable arm), which is exactly the reported journey.

Environment fidelity: the reported failure is on the Databricks AI Gateway of a
Databricks-network workspace with "Enforce Unity Gateway", reached through the
external ``routes:select`` router. CI cannot be that network, so the built-in
judge stands in for the external router and a small in-process handler stands in
for the Unity-Gateway workspace (501 on any ``databricks-*`` model, 200 on
``system.ai.*``). Both tests assert the CORRECT post-fix behavior, so they fail
today (the bug) and pass once Smart Routing canonicalizes its pick to
``system.ai.*``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from omnigent.server.routing_backend import RoutingBackends
from omnigent.server.smart_routing import RoutingResult, RoutingSettings, route_turn

# The GPT-5.6-Sol arm's two spellings: the retired serving-endpoint id the
# gateway now rejects, and the Unity Catalog model-service id it still serves.
_RETIRED_ENDPOINT_ID = "databricks-gpt-5-6-sol"
_UNITY_CATALOG_ID = "system.ai.gpt-5-6-sol"

# The exact rejection a Unity-Gateway workspace returns for a retired id.
_GATEWAY_501_MESSAGE = (
    "'databricks-gpt-5-6-sol' is no longer available. Use Unity Catalog model services (v3)."
)

# A substantial coding task — the kind of prompt the reporter routed to the GPT
# 5.6 Sol arm (the router classifies it COMPLEX → most capable arm).
_CODING_TASK = (
    "Please add a new preferences panel to the settings page of our dashboard so "
    "users can pick their preferred landing view, remember the last workspace they "
    "opened, and choose between compact and comfortable row density. Persist the "
    "choices per user account."
)


class _MostCapableArmJudge:
    """Built-in-judge stand-in: classify COMPLEX → pick the most capable arm.

    Faithful to the real ``LLMRoutingClient`` for a COMPLEX task: it returns
    the LAST (most powerful) model from whatever candidate menu Smart Routing
    offers it. Crucially, it picks *from the offered candidates* — the retired
    id is never typed here, it comes out of the product's own candidate table.

    :ivar offered: The ``available_models`` mapping seen on the last call, so a
        test can assert which spellings Smart Routing put in front of the router.
    """

    def __init__(self) -> None:
        self.last_error: str | None = None
        self.offered: dict[str, list[str]] | None = None

    async def route(
        self,
        message: str,
        available_models: dict[str, list[str]],
    ) -> RoutingResult | None:
        del message
        self.offered = {k: list(v) for k, v in available_models.items()}
        flat = [m for models in available_models.values() for m in models]
        if not flat:
            self.last_error = "no candidates"
            return None
        # COMPLEX → most capable = last arm in the cheapest→most-powerful order.
        return RoutingResult(
            model=flat[-1],
            rationale="COMPLEX task; selected most capable model.",
            harness=None,
        )


def _judge_caps(judge: _MostCapableArmJudge) -> SimpleNamespace:
    """Wrap the judge as the deployment's built-in routing backend.

    ``routing_backends`` names the judge as the local (non-external) client, so
    ``select_router`` picks it and stamps the decision ``oss-llm`` — the same
    wiring a deployment with only the built-in judge configured gets.
    """
    return SimpleNamespace(
        routing_backends=RoutingBackends(external=None, local=judge),
        routing_client=judge,
        routing_settings=RoutingSettings(),
    )


def _unity_gateway_transport() -> httpx.MockTransport:
    """A Unity-Gateway workspace stand-in.

    Mirrors a workspace with "Enforce Unity Gateway": any legacy ``databricks-*``
    serving-endpoint id is 501 "no longer available", while a Unity Catalog
    ``system.ai.*`` model-service id is served normally.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        model = json.loads(request.content or b"{}").get("model", "")
        if model.startswith("databricks-"):
            return httpx.Response(
                501,
                json={
                    "error_code": "NOT_IMPLEMENTED",
                    "message": (
                        f"'{model}' is no longer available. Use Unity Catalog model services (v3)."
                    ),
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    return httpx.MockTransport(_handler)


@pytest.mark.asyncio
async def test_smart_routing_resolves_sol_arm_to_unity_catalog_spelling() -> None:
    """A routed GPT 5.6 Sol pick must be applied as ``system.ai.*``, never ``databricks-*``.

    Drives the real ``route_turn`` seam for a Databricks-gateway-backed GPT
    (openai-agents) session with Smart Routing on. The router (built-in judge)
    classifies the coding task COMPLEX and picks the most-capable GPT arm. Smart
    Routing must localize that pick to the Unity Catalog model-service spelling
    the gateway still serves.

    Fails today: the pick is applied as the retired ``databricks-gpt-5-6-sol``,
    which is exactly what reaches the executor and gets rejected with HTTP 501.
    """
    judge = _MostCapableArmJudge()
    with patch("omnigent.runtime._globals._caps", new=_judge_caps(judge)):
        model, verdict = await route_turn(
            "openai-agents",
            _CODING_TASK,
            session_id="conv_routed_coding_turn",
            runner_client=None,
            catalog=None,
            gateway_backed=True,
            allow_static_fallback=True,
        )

    assert model is not None, "Smart Routing declined to route the turn"
    assert verdict is not None
    # The routed pick is the Sol arm (top GPT arm), one spelling or the other.
    bare = model.rsplit(".", 1)[-1] if model.startswith("system.ai.") else model
    assert "gpt-5-6-sol" in bare, f"expected the Sol arm to be routed, got {model!r}"
    # The core defect: the retired endpoint id must not be what gets applied.
    assert not model.startswith("databricks-"), (
        f"Smart Routing applied a retired Databricks endpoint id ({model!r}); "
        f"a Unity-Gateway workspace rejects it with HTTP 501. It must resolve to "
        f"the Unity Catalog spelling {_UNITY_CATALOG_ID!r}."
    )
    assert model == _UNITY_CATALOG_ID, f"expected {_UNITY_CATALOG_ID!r}, got {model!r}"


@pytest.mark.asyncio
async def test_routed_sol_pick_is_accepted_by_unity_gateway() -> None:
    """The model Smart Routing applies must be one the Unity-Gateway workspace serves.

    Reproduces the user-visible failure end to end: take the model
    ``route_turn`` resolves and send it to a Unity-Gateway stand-in. Today the
    routed id is ``databricks-gpt-5-6-sol``, so the gateway answers HTTP 501
    "no longer available. Use Unity Catalog model services (v3)." — the turn a
    user submitted fails instead of getting a reply. After the fix the pick is
    ``system.ai.gpt-5-6-sol`` and the gateway serves it.
    """
    judge = _MostCapableArmJudge()
    with patch("omnigent.runtime._globals._caps", new=_judge_caps(judge)):
        model, _verdict = await route_turn(
            "openai-agents",
            _CODING_TASK,
            session_id="conv_routed_coding_turn",
            runner_client=None,
            catalog=None,
            gateway_backed=True,
            allow_static_fallback=True,
        )
    assert model is not None, "Smart Routing declined to route the turn"

    with httpx.Client(
        base_url="https://workspace.example.databricks.com",
        transport=_unity_gateway_transport(),
    ) as gateway:
        resp = gateway.post(
            "/serving-endpoints/chat/invocations",
            json={"model": model, "messages": [{"role": "user", "content": _CODING_TASK}]},
        )

    detail: Any = ""
    if resp.status_code == 501:
        detail = resp.json().get("message", "")
    assert resp.status_code != 501, (
        f"the routed model {model!r} was rejected by the Unity-Gateway workspace: "
        f"HTTP 501 {detail!r} — the routed turn fails instead of completing"
    )
    assert resp.status_code == 200, f"unexpected gateway status {resp.status_code}"


def test_unity_gateway_standin_rejects_the_retired_id() -> None:
    """Characterization guard: the stand-in models the reported 501 faithfully.

    Not a fail→pass regression guard — it pins the reproduction environment so a
    future edit can't quietly make the Unity-Gateway stand-in accept a retired id
    (which would make the two tests above pass for the wrong reason).
    """
    with httpx.Client(
        base_url="https://workspace.example.databricks.com",
        transport=_unity_gateway_transport(),
    ) as gateway:
        rejected = gateway.post(
            "/serving-endpoints/chat/invocations",
            json={"model": _RETIRED_ENDPOINT_ID, "messages": []},
        )
        served = gateway.post(
            "/serving-endpoints/chat/invocations",
            json={"model": _UNITY_CATALOG_ID, "messages": []},
        )
    assert rejected.status_code == 501
    assert rejected.json()["message"] == _GATEWAY_501_MESSAGE
    assert served.status_code == 200
