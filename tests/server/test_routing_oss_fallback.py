"""The built-in judge answers when the external router cannot.

The deployment under test is the fully-OSS one: a ``llm:`` block for the judge,
a ``kind: databricks`` provider for inference, and no ``routing:`` block — so
the bootstrap points an external client at a workspace whose account never had
the routing API enabled. Every ``routes:select`` there is a 404, and before
this the whole session declined with "Routing unavailable" while the configured
judge was never asked.

The external router is still preferred wherever it can serve (the Databricks
posture), so these tests pin both halves: the fallback, and that a healthy
router keeps the call.

Runs against the deterministic ``routes:select`` mock the e2e routing CUJs use,
over loopback — a real HTTP 404 with the real body, no gateway dependency.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from omnigent.server.routing_backend import RoutingBackends, routing_sources, select_router
from omnigent.server.smart_routing import (
    ExternalRoutingClient,
    RoutingResult,
    RoutingSettings,
    route_session_harness,
    route_turn,
)
from tests.e2e.routing._mock_router import MockRouter, serve_mock_router
from tests.server.helpers import FakeCaps, FakeRoutingClient

OPUS = "databricks-claude-opus-4-8"
SONNET = "databricks-claude-sonnet-5"
#: A claude-sdk pane's own catalog: both task_v1 claude arms are servable, so
#: the mock's ``cc`` menu is complete and its pick maps back to a catalog id.
CLAUDE_CATALOG = [OPUS, SONNET]
#: Short and code-free, so the mock's rule 0 fires and the pick is fixed.
TRIVIAL = "hi there"


@pytest.fixture
def router() -> Iterator[MockRouter]:
    """A ``routes:select`` service on loopback, healthy by default."""
    yield from serve_mock_router()


def _external(router: MockRouter) -> ExternalRoutingClient:
    """Build a client pointed at *router* that sends no credential.

    :param router: The running mock.
    :returns: The client the deployment's ``routing_backends.external`` holds.
    """
    return ExternalRoutingClient(
        base_url=router.base_url,
        router_name="task_v1",
        # The sole authority once set, so nothing probes the developer's own
        # Databricks environment for a token.
        auth_provider=dict,
    )


def _judge(model: str = OPUS, harness: str = "claude-sdk") -> FakeRoutingClient:
    """The built-in judge, returning a fixed verdict.

    :param model: The model the judge picks.
    :param harness: The harness the judge names alongside it.
    :returns: The stand-in client.
    """
    return FakeRoutingClient(RoutingResult(model=model, rationale="judge pick", harness=harness))


@contextmanager
def _deployment(
    *,
    external: Any = None,  # type: ignore[explicit-any]  # a routing client
    local: Any = None,  # type: ignore[explicit-any]  # a routing client
) -> Iterator[FakeCaps]:
    """Install caps carrying *external* / *local* as the routing backends.

    :param external: The external ``routes:select`` client, or ``None``.
    :param local: The built-in judge, or ``None``.
    :yields: The installed caps, for reading routing reports off.
    """
    caps = FakeCaps(
        routing_client=external or local,
        routing_backends=RoutingBackends(external=external, local=local),
        routing_settings=RoutingSettings(),
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        yield caps


# ── the reported regression ─────────────────────────────────────────────────


async def test_a_not_enabled_router_hands_the_turn_to_the_judge(router: MockRouter) -> None:
    router.disabled = True
    judge = _judge()
    with _deployment(external=_external(router), local=judge):
        model, verdict = await route_turn("claude-sdk", TRIVIAL, catalog=CLAUDE_CATALOG)
    assert model == OPUS
    assert verdict is not None
    assert verdict["router_source"] == "oss-llm"
    assert router.count() == 1
    assert judge.calls != []


async def test_the_not_enabled_404_is_asked_once_per_process(router: MockRouter) -> None:
    router.disabled = True
    judge = _judge()
    external = _external(router)
    with _deployment(external=external, local=judge) as caps:
        first = await route_turn("claude-sdk", TRIVIAL, catalog=CLAUDE_CATALOG)
        second = await route_turn("claude-sdk", TRIVIAL, catalog=CLAUDE_CATALOG)
        # The condition is account-level, so the report stops advertising a
        # router that can only decline — without persisting anything.
        assert routing_sources(caps) == {"external": False, "oss": True}
        choice = select_router(
            RoutingBackends(external=external, local=judge), gateway_backed=True
        )
        assert choice is not None
        assert choice.source == "oss-llm"
    assert first[0] == second[0] == OPUS
    assert second[1] is not None
    assert second[1]["router_source"] == "oss-llm"
    # One request total: the second turn never went back to the router.
    assert router.count() == 1


async def test_a_healthy_router_still_answers_the_turn(router: MockRouter) -> None:
    judge = _judge()
    with _deployment(external=_external(router), local=judge):
        model, verdict = await route_turn("claude-sdk", TRIVIAL, catalog=CLAUDE_CATALOG)
    # Rule 0's cheapest arm on the claude-only menu.
    assert model == SONNET
    assert verdict is not None
    assert verdict["router_source"] == "databricks-aigw"
    assert judge.calls == []
    assert router.count() == 1


async def test_a_judge_only_deployment_routes_the_turn(router: MockRouter) -> None:
    del router
    judge = _judge()
    with _deployment(local=judge):
        model, verdict = await route_turn("claude-sdk", TRIVIAL, catalog=CLAUDE_CATALOG)
    assert model == OPUS
    assert verdict is not None
    assert verdict["router_source"] == "oss-llm"


async def test_a_router_that_raises_hands_the_turn_to_the_judge() -> None:
    judge = _judge()
    broken = FakeRoutingClient(error=RuntimeError("connection reset"))
    with _deployment(external=broken, local=judge):
        model, verdict = await route_turn("claude-sdk", TRIVIAL, catalog=CLAUDE_CATALOG)
    assert model == OPUS
    assert verdict is not None
    assert verdict["router_source"] == "oss-llm"


async def test_no_judge_leaves_the_router_failure_on_the_card(router: MockRouter) -> None:
    """With nothing to fall back to, the turn still declines — visibly."""
    router.disabled = True
    with _deployment(external=_external(router)):
        model, verdict = await route_turn("claude-sdk", TRIVIAL, catalog=CLAUDE_CATALOG)
    assert (model, verdict) == (None, None)


# ── the session/auto-harness path (polly, debby) ────────────────────────────


async def test_polly_smart_routing_gets_a_judge_decision(router: MockRouter) -> None:
    """The reported bug: a ``smart_routing_harness: auto`` session declined.

    The spec hands its brain harness to the router, so the create routes
    harness AND model on the first message. The judge is cross-harness by
    design, so a workspace with no routing API must not cost the session its
    decision.
    """
    router.disabled = True
    judge = _judge(model=OPUS, harness="claude-sdk")
    with _deployment(external=_external(router), local=judge):
        harness, model, verdict, error = await route_session_harness(
            TRIVIAL,
            session_id="conv_polly",
            catalog={"claude-sdk": CLAUDE_CATALOG},
        )
    assert error is None
    assert (harness, model) == ("claude-sdk", OPUS)
    assert verdict is not None
    assert verdict["router_source"] == "oss-llm"


async def test_the_judge_may_move_a_session_to_another_harness() -> None:
    """Judge-only is still harness + model: it is offered the whole menu."""
    judge = _judge(model="databricks-gpt-5-5", harness="codex")
    with _deployment(local=judge):
        harness, model, verdict, error = await route_session_harness(
            TRIVIAL,
            session_id="conv_polly",
            catalog={"claude-sdk": CLAUDE_CATALOG, "codex": ["databricks-gpt-5-5"]},
        )
    assert error is None
    assert (harness, model) == ("codex", "databricks-gpt-5-5")
    assert verdict is not None
    assert verdict["router_source"] == "oss-llm"
    # Every candidate harness reached the judge, not just the session's own.
    assert set(judge.offered[0]) >= {"claude-sdk", "codex"}


# ── the deployment the bug was reported on, assembled from config ───────────


async def test_the_oss_config_routes_with_its_judge(router: MockRouter) -> None:
    """``llm:`` + a databricks provider + no ``routing:`` block.

    The bootstrap still auto-builds the workspace router — that is the right
    default for a workspace that HAS the API — and the judge covers the one
    that does not.
    """
    from omnigent.cli import _build_routing_backends

    router.disabled = True
    host = router.base_url[: -len("/ai-gateway/routing/v1")]
    cfg = {"providers": {"ws": {"kind": "databricks", "profile": "ws", "default": True}}}
    with (
        patch(
            "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
            return_value=MagicMock(host=host),
        ),
        patch(
            "omnigent.runtime.policies.builder._resolve_server_llm_connection",
            return_value=None,
        ),
        patch(
            "omnigent.runtime.policies.builder._build_policy_llm_client",
            return_value=MagicMock(),
        ),
        patch.object(ExternalRoutingClient, "_resolve_credentials", return_value=(None, {})),
    ):
        from omnigent.spec import parse_server_llm

        backends = _build_routing_backends(
            cfg, parse_server_llm({"model": OPUS}), RoutingSettings()
        )
        assert isinstance(backends.external, ExternalRoutingClient)
        judge = _judge()
        with _deployment(external=backends.external, local=judge):
            model, verdict = await route_turn("claude-sdk", TRIVIAL, catalog=CLAUDE_CATALOG)
    assert model == OPUS
    assert verdict is not None
    assert verdict["router_source"] == "oss-llm"
    assert router.count() == 1
