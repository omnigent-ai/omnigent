"""E2E: smart routing must not pin a decommissioned serving endpoint.

The task_v1 router's cheapest codex arm is ``gpt-5-6-luna``, and the static
fallback tables (``omnigent/model_fallbacks.py``) still resolve that arm to
``databricks-gpt-5-6-luna`` — a legacy endpoint some workspaces no longer
serve. On a workspace where the live model catalog is unreachable at routing
time (the static-fallback path), the router pins that dead endpoint and the
session errors on its first turn. Workspaces whose catalog *is* reachable
never hit it: the client sees nothing servable behind the pick and declines —
which is why the failure looked region/workspace-dependent to reporters.

Drives the real routing pipeline (``route_turn`` + ``ExternalRoutingClient``)
against the deterministic task_v1 mock this suite already ships
(``tests/e2e/routing/_mock_router.py``, arms mirrored from the real router),
so the verdict is exact and no gateway credential is needed.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from omnigent.server.smart_routing import ExternalRoutingClient, route_turn
from tests.e2e.routing._mock_router import MockRouter, serve_mock_router
from tests.server.helpers import FakeCaps

#: The legacy serving endpoint the smart router must never pin: decommissioned
#: on some workspaces, so a session routed onto it errors immediately.
DECOMMISSIONED_MODEL = "databricks-gpt-5-6-luna"

#: Catalog prefixes this deployment's model ids carry and the router does not
#: expect (mirrors ``tests/e2e/routing/conftest.MODEL_PREFIXES``).
MODEL_PREFIXES: tuple[str, ...] = ("databricks-", "system.ai.")


@pytest.fixture(autouse=True)
def _bypass_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the routing client's loopback calls off any egress proxy.

    ``ExternalRoutingClient`` builds a default (``trust_env``) httpx client,
    so a CI proxy env would swallow requests to the loopback mock.
    """
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture()
def mock_router() -> Iterator[MockRouter]:
    """Run the deterministic ``routes:select`` task_v1 mock for one test.

    :yields: The router handle; ``base_url`` goes into the client under test.
    """
    yield from serve_mock_router()


def _routing_client(mock_router: MockRouter) -> ExternalRoutingClient:
    """Build the real routing client pointed at the mock's ``routes:select``.

    :param mock_router: The running deterministic task_v1 mock.
    :returns: A client configured the way the server config seam builds it.
    """
    return ExternalRoutingClient(
        base_url=mock_router.base_url,
        router_name="task_v1",
        model_prefixes=list(MODEL_PREFIXES),
    )


async def test_static_fallback_never_pins_decommissioned_luna(
    mock_router: MockRouter,
) -> None:
    """With no live catalog, the routed pick must not be the dead endpoint.

    A trivial first message routes to the router's cheapest codex arm; the
    static candidate tables then resolve it to ``databricks-gpt-5-6-luna``,
    which a workspace without that serving endpoint cannot run — the session
    errors. The pick on this path must resolve to a still-served endpoint
    (or decline), never the decommissioned one.

    :param mock_router: The running deterministic task_v1 mock.
    :returns: None.
    """
    caps = FakeCaps(routing_client=_routing_client(mock_router))
    with patch("omnigent.runtime._globals._caps", new=caps):
        model, verdict = await route_turn(
            "codex-native",
            "hi",
            session_id=None,
            gateway_backed=True,
        )
    assert model != DECOMMISSIONED_MODEL, (
        "static-fallback routing pinned the decommissioned endpoint "
        f"{DECOMMISSIONED_MODEL}: verdict={verdict!r}"
    )


async def test_live_catalog_without_luna_never_pins_it(
    mock_router: MockRouter,
) -> None:
    """With a live catalog lacking luna, the unservable pick is not applied.

    This is the guard that makes the failure workspace-dependent: when the
    catalog is reachable and the legacy endpoint is absent, the client must
    ignore the router's luna pick (decline, or resolve within the catalog)
    rather than pin a model the workspace cannot serve.

    :param mock_router: The running deterministic task_v1 mock.
    :returns: None.
    """
    catalog = ["databricks-gpt-5-6-sol", "databricks-gpt-5-5"]
    caps = FakeCaps(routing_client=_routing_client(mock_router))
    with patch("omnigent.runtime._globals._caps", new=caps):
        model, verdict = await route_turn(
            "codex-native",
            "hi",
            catalog=catalog,
            gateway_backed=True,
        )
    assert model != DECOMMISSIONED_MODEL, (
        f"live-catalog routing pinned a model the workspace does not serve: verdict={verdict!r}"
    )
    assert model is None or model in catalog, (model, verdict)
