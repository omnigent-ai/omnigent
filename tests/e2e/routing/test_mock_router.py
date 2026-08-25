"""Proof that the mock router is honest to the ``task_v1`` contract.

The five CUJs assert exact arms and exact rationales, which is only meaningful
if the mock's rule table is itself pinned. This module pins it two ways:

* the rule table directly (:func:`~tests.e2e.routing._mock_router.decide`), and
* the real client against the real HTTP surface — the request
  :class:`~omnigent.server.smart_routing.ExternalRoutingClient` builds is
  answered by the running mock, so the wire shape, the menu injection and the
  menu rejection are exercised end to end without a gateway.

These are fast and need no server, host or CLI, so they run whenever the
suite's opt-in gate is set.
"""

from __future__ import annotations

import pytest

from omnigent.server.smart_routing import ExternalRoutingClient
from tests.e2e.routing._mock_router import (
    CLAUDE_ARMS,
    CODEX_ARMS,
    MockRouter,
    decide,
    missing_arms,
    scenario_for,
    select_route,
)
from tests.e2e.routing.conftest import MODEL_PREFIXES, prompts

pytestmark = pytest.mark.smart_routing


def _client(mock_router: MockRouter) -> ExternalRoutingClient:
    """Build the production routing client pointed at the mock.

    :param mock_router: The running mock.
    :returns: A client configured exactly as the server's would be.
    """
    return ExternalRoutingClient(
        base_url=mock_router.base_url,
        router_name="task_v1",
        model_prefixes=list(MODEL_PREFIXES),
    )


def test_scenario_is_inferred_from_the_arms_not_the_tags() -> None:
    assert scenario_for(CLAUDE_ARMS) == "cc"
    assert scenario_for(CODEX_ARMS) == "codex"
    assert scenario_for([*CLAUDE_ARMS, *CODEX_ARMS]) == "both"
    assert scenario_for(["gpt-5-5", "kimi-k2"]) is None


@pytest.mark.parametrize(
    ("scenario", "prompt_key", "expected_model", "expected_trace"),
    [
        ("cc", "trivial", "claude-sonnet-5", "cheapest arm claude-sonnet-5; never escalate."),
        ("codex", "trivial", "gpt-5-6-luna", "cheapest arm gpt-5-6-luna; never escalate."),
        ("both", "trivial", "gpt-5-6-luna", "cheapest arm gpt-5-6-luna; never escalate."),
        ("codex", "delegate", "glm-5-2", "all hold -> delegate down to glm-5-2."),
        ("cc", "escalate", "claude-opus-4-8", "all hold -> escalate up to claude-opus-4-8."),
        ("both", "escalate", "claude-opus-4-8", "all hold -> escalate up to claude-opus-4-8."),
        ("cc", "crosscutting", "claude-sonnet-5", "not all hold -> default claude-sonnet-5."),
        ("codex", "crosscutting", "gpt-5-6-sol", "not all hold -> default gpt-5-6-sol."),
        ("both", "crosscutting", "gpt-5-6-sol", "not all hold -> default gpt-5-6-sol."),
    ],
)
def test_rule_table(
    scenario: str, prompt_key: str, expected_model: str, expected_trace: str
) -> None:
    verdict = decide(prompts()[prompt_key], scenario)
    assert verdict.model == expected_model
    assert verdict.rationale.startswith(f"Routed to {expected_model} because ")
    assert verdict.rationale.endswith(expected_trace)


def test_a_narrowed_menu_is_rejected_with_the_routers_own_message() -> None:
    assert missing_arms("codex", ["glm-5-2", "gpt-5-6-sol"]) == ["gpt-5-6-luna"]
    with pytest.raises(Exception) as caught:
        select_route(
            {
                "route_options": [{"model": "glm-5-2"}, {"model": "gpt-5-6-sol"}],
                "task": {"prompt": "hi"},
            }
        )
    assert "scenario 'codex' requires its full menu; missing [gpt-5-6-luna]" in str(caught.value)


def test_extra_non_arm_models_are_tolerated() -> None:
    payload = select_route(
        {
            "route_options": [
                *({"model": arm} for arm in CODEX_ARMS),
                {"model": "gpt-5-5"},
                {"model": "kimi-k2"},
            ],
            "task": {"prompt": "hi"},
        }
    )
    assert payload["route_selection"][0]["route_option"]["model"] == "gpt-5-6-luna"


def test_the_harness_tag_is_echoed_verbatim_never_read() -> None:
    """A nonsensical tag changes neither the pick nor the status."""
    payload = select_route(
        {
            # Claude arms tagged codex and vice versa: the real router echoes the
            # tag it was given and picks on the model alone.
            "route_options": [{"model": arm, "harness": "codex"} for arm in CLAUDE_ARMS],
            "task": {"prompt": "hi"},
        }
    )
    selection = payload["route_selection"][0]["route_option"]
    assert selection["model"] == "claude-sonnet-5"
    assert selection["harness"] == "codex"


@pytest.mark.parametrize(
    ("catalog", "expected"),
    [
        ({"claude-native": ["databricks-claude-sonnet-5"]}, "databricks-claude-sonnet-5"),
        ({"codex-native": ["databricks-gpt-5-6-luna"]}, "databricks-gpt-5-6-luna"),
    ],
)
async def test_real_client_round_trips_the_mock(
    mock_router: MockRouter,
    catalog: dict[str, list[str]],
    expected: str,
) -> None:
    """The production client's own request is answered and resolved.

    Proves the mock's wire shape against the code under test rather than
    against a hand-written body: the client injects the scenario menu, sends
    snake_case protos, and resolves the pick back onto a servable catalog id.
    """
    result = await _client(mock_router).route("hi", catalog)
    assert result is not None
    assert result.model == expected
    assert "cheapest arm" in result.rationale
    served = mock_router.snapshot()[-1]
    assert served.router_name == "task_v1"
    # The client must have injected the full menu, not just the catalog row.
    for arm in CLAUDE_ARMS if "claude-native" in catalog else CODEX_ARMS:
        assert arm in served.offered


async def test_real_client_surfaces_a_menu_rejection(mock_router: MockRouter) -> None:
    """A non-task_v1 client sends no menu, and the mock rejects it as the gateway does."""
    client = ExternalRoutingClient(
        base_url=mock_router.base_url,
        # Any name but task_v1 makes the seam skip menu injection, which is
        # exactly the narrowed-menu case the real router 400s on.
        router_name="task_v2",
        model_prefixes=list(MODEL_PREFIXES),
    )
    result = await client.route("hi", {"codex-native": ["databricks-gpt-5-6-luna"]})
    assert result is None
    assert "requires its full menu" in (client.last_error or "")
    assert mock_router.snapshot()[-1].status == 400
