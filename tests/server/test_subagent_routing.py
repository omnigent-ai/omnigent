"""Tests for the native-subagent routing endpoint and policy."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.conversation import RoutingDecisionData
from omnigent.inner.hook_scripts.subagent_router import read_router_endpoint
from omnigent.runner.subagent_routing import (
    ADVERTISEMENT_FILE,
    AUTO_HARNESS_LABEL_KEY,
    PLAIN_SESSION,
    ROUTING_DECISION_LABEL_KEY,
    SessionRoutingClass,
    SubagentRouteDecision,
    SubagentRouteRequest,
    auto_harness_session,
    candidate_models,
    decision_record,
    ensure_session_router,
    ensure_session_router_quietly,
    forget_session_routing_class,
    harness_family,
    make_server_relay_resolver,
    model_in_family,
    persist_subagent_decision,
    relayed_decisions,
    remember_session_routing_class,
    resolve_subagent_route,
    routed_models,
    router_dir_for_session,
    routing_class_from_snapshot,
    routing_enabled,
    session_router_env,
    session_routing_class,
    shutdown_session_router,
    start_subagent_router,
    subagent_routing_enabled,
    write_advertisement,
)
from omnigent.server.smart_routing import RoutingResult, RoutingSettings
from tests.server.helpers import FakeCaps, FakeRoutingClient

CLAUDE_MODEL = "databricks-claude-opus-4-8"
GPT_MODEL = "databricks-gpt-5-5"
GLM_MODEL = "databricks-glm-5-2"
# The spelling the gateway actually serves GLM under; see ``_SERVABLE_ALIASES``.
GLM_SERVABLE = "system.ai.glm-5-2"
KIMI_MODEL = "databricks-kimi-k2-6"
PARENT_MODEL = "databricks-claude-sonnet-4-6"

# ── Stubs ───────────────────────────────────────────────────────────


@dataclass
class _PersistedItem:
    id: str


@dataclass
class _FakeStore:
    appended: list[Any] = field(default_factory=list)  # type: ignore[explicit-any]

    def append(self, session_id: str, items: list[Any]) -> list[_PersistedItem]:  # type: ignore[explicit-any]
        del session_id
        self.appended.extend(items)
        return [_PersistedItem(id=f"item_{len(self.appended)}")]


def _request(**overrides: Any) -> SubagentRouteRequest:
    kwargs: dict[str, Any] = {
        "harness": "claude-native",
        "task_name": "code-reviewer",
        "prompt": "review the diff",
        "parent_model": PARENT_MODEL,
    }
    kwargs.update(overrides)
    return SubagentRouteRequest(**kwargs)


# ── Candidate set ───────────────────────────────────────────────────


def test_candidate_models_stays_in_family_by_default() -> None:
    candidates = candidate_models("claude-native")
    assert set(candidates) == {"claude-native"}
    assert CLAUDE_MODEL in candidates["claude-native"]


def test_candidate_models_prefers_the_live_catalog() -> None:
    """A model the workspace serves today must not look unservable."""
    catalog = {"self": ["databricks-claude-sonnet-5", "databricks-claude-opus-4-8"]}
    candidates = candidate_models("claude-native", catalog=catalog)
    assert candidates == {
        "claude-native": ["databricks-claude-sonnet-5", "databricks-claude-opus-4-8"]
    }


def test_candidate_models_falls_back_to_the_static_table_per_harness() -> None:
    catalog = {"self": ["databricks-claude-sonnet-5"]}
    candidates = candidate_models("claude-native", cross_harness=True, catalog=catalog)
    assert candidates["claude-native"] == ["databricks-claude-sonnet-5"]
    # No codex row in the catalog — the static table fills that harness in.
    assert GPT_MODEL in candidates["codex-native"]


def test_candidate_models_ignores_an_empty_catalog() -> None:
    candidates = candidate_models("claude-native", catalog={})
    assert CLAUDE_MODEL in candidates["claude-native"]


def test_candidate_models_applies_the_family_constraint_to_catalog_rows() -> None:
    """A codex ``"self"`` row keeps GLM/Kimi and loses the Claude ids.

    The gateway behind a codex session serves Claude ids too, so the row
    carries models codex cannot speak; offering one earns a hard
    ``model_family_mismatch`` at dispatch. GLM and Kimi serve on the
    Responses wire codex does speak, so they must survive.
    """
    catalog = {"self": [GPT_MODEL, GLM_MODEL, KIMI_MODEL, CLAUDE_MODEL]}
    candidates = candidate_models("codex-native", catalog=catalog)
    assert candidates == {"codex-native": [GPT_MODEL, GLM_SERVABLE, KIMI_MODEL]}
    assert model_in_family(harness_family("codex-native"), GLM_MODEL) is True
    assert model_in_family(harness_family("claude-native"), GLM_MODEL) is False


def test_candidate_models_offers_glm_under_its_servable_alias() -> None:
    """Both catalog spellings collapse to the one the gateway serves.

    The offered id is what a rewrite spawns with, so it has to match the id
    routing resolves the ``glm-5-2`` arm to.
    """
    catalog = {"self": [GPT_MODEL, GLM_MODEL, GLM_SERVABLE]}
    assert candidate_models("codex-native", catalog=catalog) == {
        "codex-native": [GPT_MODEL, GLM_SERVABLE]
    }


def test_every_codex_spawn_is_offered_glm() -> None:
    """Policy: a codex session spawns codex OR glm subagents, always both.

    GLM is in no discovery listing, so a live catalog row never carries it;
    without the top-up a row that answers for the harness would hide GLM and
    the session could not reach it.
    """
    for catalog in (
        None,
        {},
        {"self": ["databricks-gpt-5-6-luna", "databricks-gpt-5-6-sol"]},
        # A nested spawn (a codex subagent spawning again) has the same
        # leaf-shaped catalog, so it must be offered GLM too.
        {"self": [GPT_MODEL]},
    ):
        candidates = candidate_models("codex-native", catalog=catalog)
        assert GLM_SERVABLE in candidates["codex-native"], catalog
        # Never the raw catalog spelling — the gateway serves only the alias.
        assert GLM_MODEL not in candidates["codex-native"], catalog


def test_the_glm_top_up_does_not_widen_a_multi_model_harness() -> None:
    """pi admits every family, but nothing proves its CLI can serve GLM."""
    assert GLM_SERVABLE not in candidate_models("pi")["pi"]


@pytest.mark.parametrize(
    ("harness", "cross_harness", "expected_harnesses"),
    [
        # Pinned sessions stay in their own family, in both directions.
        ("codex-native", False, {"codex-native"}),
        ("claude-native", False, {"claude-native"}),
        # A Smart Routing (auto-harness) session, and any child of one, may
        # cross — that is the only case a redirect verdict can fire.
        ("codex-native", True, {"codex-native", "claude-native"}),
        ("claude-native", True, {"claude-native", "codex-native"}),
    ],
)
def test_spawn_candidates_respect_the_family_restriction(
    harness: str,
    cross_harness: bool,
    expected_harnesses: set[str],
) -> None:
    candidates = candidate_models(harness, cross_harness=cross_harness)
    assert set(candidates) == expected_harnesses
    for name, models in candidates.items():
        family = harness_family(name)
        assert all(model_in_family(family, model) for model in models)


def test_candidate_models_drops_a_harness_with_nothing_servable() -> None:
    catalog = {"self": [CLAUDE_MODEL]}
    assert candidate_models("codex-native", catalog=catalog) == {}


def test_candidate_models_offers_both_families_for_auto_sessions() -> None:
    candidates = candidate_models("claude-native", cross_harness=True)
    assert set(candidates) == {"claude-native", "codex-native"}
    assert CLAUDE_MODEL in candidates["claude-native"]
    assert GPT_MODEL in candidates["codex-native"]


class _Conv:
    """Minimal conversation row for the auto-harness gate."""

    def __init__(self, labels: dict[str, str] | None = None, harness_override: str | None = None):
        self.labels = labels or {}
        self.harness_override = harness_override


@pytest.mark.parametrize(
    ("conv", "parent", "expected"),
    [
        # The durable label, which survives the sentinel being replaced once
        # first-message routing resolves a harness.
        (_Conv(labels={AUTO_HARNESS_LABEL_KEY: "1"}), None, True),
        (_Conv(harness_override="auto"), None, True),
        # A child of an auto-harness session inherits the permission.
        (
            _Conv(harness_override="codex-native"),
            _Conv(labels={AUTO_HARNESS_LABEL_KEY: "1"}),
            True,
        ),
        (_Conv(harness_override="codex-native"), _Conv(harness_override="auto"), True),
        # Everything else is pinned: no label, a pinned override, an off label,
        # a pinned parent, and a missing row.
        (_Conv(harness_override="claude-native"), None, False),
        (_Conv(labels={AUTO_HARNESS_LABEL_KEY: "0"}), None, False),
        (_Conv(), _Conv(harness_override="claude-native"), False),
        (None, None, False),
    ],
    ids=[
        "label",
        "sentinel",
        "parent-label",
        "parent-sentinel",
        "pinned",
        "label-off",
        "pinned-parent",
        "no-rows",
    ],
)
def test_auto_harness_session_is_the_only_cross_harness_enabler(
    conv: _Conv | None,
    parent: _Conv | None,
    expected: bool,
) -> None:
    """Pin the gate a cross-harness redirect depends on.

    ``routes_hooks`` derives ``cross_harness`` from exactly this call, and
    ``candidate_models(cross_harness=False)`` never offers the counterpart
    family — so a pinned session can never receive a redirect verdict.
    """
    assert auto_harness_session(conv, parent) is expected


@pytest.mark.parametrize("harness", ["claude-native", "codex-native"])
def test_pinned_sessions_are_never_offered_the_counterpart_family(harness: str) -> None:
    counterpart = "codex-native" if harness == "claude-native" else "claude-native"
    assert counterpart not in candidate_models(harness, cross_harness=False)
    assert counterpart in candidate_models(harness, cross_harness=True)


# ── Policy ──────────────────────────────────────────────────────────


async def test_same_family_pick_rewrites() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "rewrite"
    assert decision.model == CLAUDE_MODEL
    assert decision.harness is None
    assert decision.raw_model is None
    assert decision.rationale == "deep reasoning"
    assert len(decision.decision_id) == 36


@pytest.mark.parametrize(
    ("picked", "note", "recorded_override"),
    [
        # The router happened to pick the arm the spawn named: honored by
        # coincidence, and an honored ask is just the model.
        (GLM_SERVABLE, "honored — the router picked the same arm", None),
        # It picked another: the router's pick is applied and the ask is
        # recorded as the override attempt.
        (GPT_MODEL, "overridden — the router picked databricks-gpt-5-5", GLM_SERVABLE),
    ],
    ids=["match", "mismatch"],
)
async def test_an_explicit_in_family_ask_is_still_routed(
    picked: str,
    note: str,
    recorded_override: str | None,
) -> None:
    """An explicit ask never short-circuits the router.

    The spawn names a routable arm, so the old policy honored it without
    asking. Strict adherence means the router still decides: the ask only
    words the rationale, and it lands only when the pick agrees.
    """
    client = FakeRoutingClient(RoutingResult(model=picked, rationale="default arm"))
    req = _request(harness="codex-native", prompt=None, requested_model=GLM_SERVABLE)
    decision = await resolve_subagent_route(
        "conv_1",
        req,
        caps=FakeCaps(routing_client=client),
        catalog={"self": [GPT_MODEL, GLM_MODEL]},
    )
    assert len(client.calls) == 1
    assert decision.action == "rewrite"
    assert decision.model == picked
    # The router's own rationale stays first and intact.
    assert decision.rationale == f"default arm Spawn requested {GLM_SERVABLE}; {note}."
    assert decision_record(req, decision).attempted_override == recorded_override


async def test_the_router_is_called_even_when_the_spawn_names_a_model() -> None:
    """Regression: a ``requested_model`` used to skip ``client.route`` entirely,
    so every rationale read "it is a routable arm" and no rule trace existed."""
    client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning"))
    decision = await resolve_subagent_route(
        "conv_1",
        _request(requested_model=CLAUDE_MODEL),
        caps=FakeCaps(routing_client=client),
    )
    assert len(client.calls) == 1
    assert decision.rationale.startswith("deep reasoning")
    assert "it is a routable arm" not in decision.rationale


async def test_an_ask_matches_the_pick_on_the_bare_arm() -> None:
    """Any spelling of the ask compares against the arm the router picked."""
    client = FakeRoutingClient(RoutingResult(model=GLM_SERVABLE, rationale="delegate down"))
    req = _request(harness="codex-native", prompt=None, requested_model=GLM_MODEL)
    decision = await resolve_subagent_route(
        "conv_1",
        req,
        caps=FakeCaps(routing_client=client),
        catalog={"self": [GPT_MODEL, GLM_MODEL]},
    )
    assert decision.model == GLM_SERVABLE
    assert "honored" in decision.rationale
    assert decision_record(req, decision).attempted_override is None


async def test_a_long_context_suffix_is_the_same_arm() -> None:
    """``[1m]`` names a context window, not another arm."""
    client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning"))
    req = _request(requested_model=f"{CLAUDE_MODEL}[1m]")
    decision = await resolve_subagent_route("conv_1", req, caps=FakeCaps(routing_client=client))
    assert "honored" in decision.rationale
    assert decision_record(req, decision).attempted_override is None


@pytest.mark.parametrize(
    ("picked", "harness", "cross_harness", "expected_action", "rationale_head"),
    [
        # The router keeping the parent model still answers the ask.
        (PARENT_MODEL, None, False, "allow", "parent model fits"),
        # A cross-harness pick the spawn asked for is still the ask, honored.
        (GPT_MODEL, "codex", True, "redirect", "narrow change"),
    ],
    ids=["allow", "redirect"],
)
async def test_a_matching_ask_is_honored_on_every_action(
    picked: str,
    harness: str | None,
    cross_harness: bool,
    expected_action: str,
    rationale_head: str,
) -> None:
    """Honoring is about agreement, not about the verdict's shape.

    ``rewrite`` is covered by the strict-adherence matrix above; these are the
    two other actions a matching ask can ride, and both must read the same.
    """
    client = FakeRoutingClient(
        RoutingResult(model=picked, rationale=rationale_head, harness=harness)
    )
    req = _request(requested_model=picked)
    decision = await resolve_subagent_route(
        "conv_1", req, caps=FakeCaps(routing_client=client), cross_harness=cross_harness
    )
    assert decision.action == expected_action
    assert decision.rationale == (
        f"{rationale_head} Spawn requested {picked}; honored — the router picked the same arm."
    )
    assert decision_record(req, decision).attempted_override is None


async def test_a_deny_claims_nothing_about_the_ask() -> None:
    """Nothing spawned, so there is no ask to call honored or overridden."""
    client = FakeRoutingClient(
        RoutingResult(model="databricks-kimi-k2", rationale="unavailable", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1",
        _request(requested_model=CLAUDE_MODEL),
        caps=FakeCaps(routing_client=client),
    )
    assert decision.action == "deny"
    assert "honored" not in decision.rationale
    assert "overridden" not in decision.rationale


@pytest.mark.parametrize(
    "client",
    [
        FakeRoutingClient(error=RuntimeError("router down")),
        FakeRoutingClient(None, last_error="menu mismatch"),
        None,
    ],
    ids=["outage", "no-verdict", "no-client"],
)
async def test_fail_open_leaves_the_spawn_on_the_model_it_asked_for(
    client: FakeRoutingClient | None,
) -> None:
    """A router outage allows the spawn with no model, so the hook leaves
    ``tool_input`` alone and the spawn runs on exactly what it named — the
    audit record has to say so rather than name the inherited model."""
    req = _request(requested_model=GLM_SERVABLE)
    decision = await resolve_subagent_route("conv_1", req, caps=FakeCaps(routing_client=client))
    assert decision.action == "allow"
    assert decision.model is None
    assert "honored" not in decision.rationale
    record = decision_record(req, decision)
    assert record.model == GLM_SERVABLE
    assert record.applied is False
    assert record.attempted_override is None


async def test_a_cross_family_ask_on_an_auto_session_is_not_honored_in_place() -> None:
    """Auto-harness candidates span both families, but a rewrite runs
    in-place — a codex spawn must never be handed a claude arm to run."""
    client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="default arm"))
    decision = await resolve_subagent_route(
        "conv_1",
        _request(harness="codex-native", prompt=None, requested_model=CLAUDE_MODEL),
        caps=FakeCaps(routing_client=client),
        cross_harness=True,
    )
    assert decision.action == "rewrite"
    assert decision.model == GPT_MODEL
    assert len(client.calls) == 1
    assert f"Spawn requested {CLAUDE_MODEL}; overridden" in decision.rationale


async def test_an_out_of_family_ask_is_routed_over_and_recorded() -> None:
    """A codex spawn asking for a claude arm gets routed; the ask is recorded."""
    client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="default arm"))
    req = _request(harness="codex-native", prompt=None, requested_model=CLAUDE_MODEL)
    decision = await resolve_subagent_route(
        "conv_1",
        req,
        caps=FakeCaps(routing_client=client),
        catalog={"self": [GPT_MODEL, GLM_MODEL]},
    )
    assert decision.action == "rewrite"
    assert decision.model == GPT_MODEL
    assert len(client.calls) == 1  # not honored — routed normally
    assert f"overridden — the router picked {GPT_MODEL}" in decision.rationale
    assert decision_record(req, decision).attempted_override == CLAUDE_MODEL


async def test_raw_model_is_omitted_when_it_matches_the_resolved_model() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="r", raw_model=CLAUDE_MODEL)
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.model == CLAUDE_MODEL
    assert decision.raw_model is None


async def test_raw_model_is_preserved_when_the_router_named_another_arm() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="r", raw_model="claude-opus-4-8-thinking")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.model == CLAUDE_MODEL
    assert decision.raw_model == "claude-opus-4-8-thinking"
    assert decision_record(_request(), decision).raw_model == "claude-opus-4-8-thinking"


async def test_live_catalog_pick_is_applied_exactly() -> None:
    """A live-catalog model is offered and applied verbatim — no substitution."""
    client = FakeRoutingClient(
        RoutingResult(model="databricks-claude-sonnet-5", rationale="r", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1",
        _request(),
        caps=FakeCaps(routing_client=client),
        catalog={"self": ["databricks-claude-sonnet-5", "databricks-claude-opus-4-8"]},
    )
    assert client.calls[0][1] == {
        "claude-native": ["databricks-claude-sonnet-5", "databricks-claude-opus-4-8"]
    }
    assert decision.action == "rewrite"
    assert decision.model == "databricks-claude-sonnet-5"


async def test_cross_family_pick_redirects_to_counterpart_harness() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=GPT_MODEL, rationale="narrow change", harness="codex")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client), cross_harness=True
    )
    assert decision.action == "redirect"
    assert decision.model == GPT_MODEL
    assert decision.harness == "codex-native"


async def test_in_family_session_only_offers_its_own_harness() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=GPT_MODEL, rationale="narrow change", harness="codex")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    # Only Claude arms were offered, so a Codex pick is unrunnable — a
    # constrained session can never redirect.
    assert set(client.calls[0][1]) == {"claude-native"}
    assert decision.action == "deny"


async def test_a_pinned_codex_spawn_can_never_land_a_claude_arm() -> None:
    """Pinned codex routes spawns now, but only within its own family.

    The routing endpoint is installed for a pinned Smart Routing codex session
    so its spawns can run at all; cross-family placement stays an auto-harness
    privilege. Without the restriction a codex session would start handing its
    subagents to Claude Code.
    """
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-native")
    )
    decision = await resolve_subagent_route(
        "conv_1",
        _request(harness="codex-native"),
        caps=FakeCaps(routing_client=client),
        cross_harness=False,
    )
    assert set(client.calls[0][1]) == {"codex-native"}
    assert CLAUDE_MODEL not in client.calls[0][1]["codex-native"]
    assert decision.action == "deny"
    assert decision.harness != "claude-native"


async def test_an_auto_harness_codex_spawn_may_land_a_claude_arm() -> None:
    """The same pick, on the one class allowed to cross families."""
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-native")
    )
    decision = await resolve_subagent_route(
        "conv_1",
        _request(harness="codex-native"),
        caps=FakeCaps(routing_client=client),
        cross_harness=True,
    )
    assert set(client.calls[0][1]) == {"codex-native", "claude-native"}
    assert decision.action == "redirect"
    assert decision.model == CLAUDE_MODEL
    assert decision.harness == "claude-native"


async def test_pick_outside_candidate_set_denies() -> None:
    client = FakeRoutingClient(
        RoutingResult(model="databricks-kimi-k2", rationale="unavailable", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "deny"
    assert decision.model is None
    assert "cannot run" in decision.rationale
    # The deny message still names the pick it rejected.
    assert decision.raw_model == "databricks-kimi-k2"


async def test_parent_model_pick_allows_unchanged() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=PARENT_MODEL, rationale="parent model fits", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "allow"
    assert decision.model == PARENT_MODEL


async def test_codex_spawn_routed_to_glm_rewrites_in_family() -> None:
    """A codex spawn the router sends to GLM stays on codex and rewrites.

    GLM is codex-compatible, so the verdict must be ``rewrite`` on the
    requesting harness — never a ``redirect`` that would send the model to
    ``sys_session_create`` for a spawn its own tool can launch.
    """
    client = FakeRoutingClient(RoutingResult(model=GLM_SERVABLE, rationale="delegate down"))
    decision = await resolve_subagent_route(
        "conv_1",
        _request(harness="codex-native", prompt=None, task_name="lint-fixer"),
        caps=FakeCaps(routing_client=client),
    )
    # The static table is the only source of a glm candidate, and it offers the
    # servable spelling.
    assert GLM_SERVABLE in client.calls[0][1]["codex-native"]
    assert decision.action == "rewrite"
    assert decision.model == GLM_SERVABLE
    assert decision.harness is None


async def test_glm_arm_pick_stamps_no_substitution_arrow() -> None:
    """The arm id and the servable spelling are the same arm, not a swap."""
    client = FakeRoutingClient(
        RoutingResult(model=GLM_SERVABLE, rationale="delegate down", raw_model="glm-5-2")
    )
    decision = await resolve_subagent_route(
        "conv_1",
        _request(harness="codex-native", prompt=None, task_name="lint-fixer"),
        caps=FakeCaps(routing_client=client),
    )
    assert decision.action == "rewrite"
    assert decision.model == GLM_SERVABLE
    assert decision.raw_model is None
    assert decision_record(_request(harness="codex-native"), decision).raw_model is None


async def test_glm_spawn_from_a_live_catalog_is_applied_exactly() -> None:
    """A workspace catalog listing GLM routes to the alias, not a substitute."""
    client = FakeRoutingClient(RoutingResult(model=GLM_SERVABLE, rationale="delegate down"))
    decision = await resolve_subagent_route(
        "conv_1",
        _request(harness="codex-native", prompt=None, task_name="lint-fixer"),
        caps=FakeCaps(routing_client=client),
        catalog={"self": [GLM_MODEL, "databricks-gpt-5-6-luna"]},
    )
    assert client.calls[0][1] == {"codex-native": [GLM_SERVABLE, "databricks-gpt-5-6-luna"]}
    assert decision.action == "rewrite"
    assert decision.model == GLM_SERVABLE


async def test_claude_session_never_gets_a_glm_spawn() -> None:
    """GLM is out of family for claude, so a GLM pick cannot be offered."""
    candidates = candidate_models("claude-native", cross_harness=True)
    assert GLM_SERVABLE in candidates["codex-native"]
    assert GLM_SERVABLE not in candidates["claude-native"]


async def test_fork_is_exempt_and_never_calls_router() -> None:
    client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="x"))
    decision = await resolve_subagent_route(
        "conv_1", _request(fork=True), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "allow"
    assert decision.model == PARENT_MODEL
    assert client.calls == []


async def test_unnamed_codex_spawn_routes_on_the_placeholder_task() -> None:
    # A codex spawn with no prompt and no task/agent name still reaches the
    # router: it scores ucode's placeholder label and lands on the floor arm,
    # instead of skipping the router to inherit the parent's (pricier) model.
    client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="floor arm"))
    decision = await resolve_subagent_route(
        "conv_1",
        _request(
            harness="codex-native",
            prompt=None,
            task_name="",
            parent_model="databricks-gpt-5-6-luna",
        ),
        caps=FakeCaps(routing_client=client),
        available_models={"codex-native": [GPT_MODEL]},
    )
    assert client.calls[0][0] == "Codex subagent task"
    assert decision.action == "rewrite"
    assert decision.model == GPT_MODEL
    assert "No routable signal" not in decision.rationale


async def test_named_codex_spawn_routes_on_its_task_name() -> None:
    # A codex spawn carries no prompt (encrypted), but a task/agent name is
    # scored per-name, so named spawns already route individually — the
    # placeholder only kicks in when there is no name.
    client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="named arm"))
    await resolve_subagent_route(
        "conv_1",
        _request(harness="codex-native", prompt=None, task_name="doc-writer"),
        caps=FakeCaps(routing_client=client),
        available_models={"codex-native": [GPT_MODEL]},
    )
    assert client.calls[0][0] == "doc-writer"


async def test_every_unnamed_codex_spawn_scores_the_same_placeholder() -> None:
    # All unnamed spawns share one placeholder, so they route to the same
    # (floor) arm: a cheap default, not per-spawn intelligence.
    client = FakeRoutingClient(RoutingResult(model=GPT_MODEL, rationale="floor arm"))
    caps = FakeCaps(routing_client=client)
    for _ in range(2):
        await resolve_subagent_route(
            "conv_1",
            _request(harness="codex-native", prompt=None, task_name=""),
            caps=caps,
            available_models={"codex-native": [GPT_MODEL]},
        )
    assert [call[0] for call in client.calls] == ["Codex subagent task", "Codex subagent task"]


@pytest.mark.parametrize(
    ("client", "reason_fragment"),
    [
        # A router outage allows the spawn unchanged: the gate is advisory.
        (FakeRoutingClient(error=RuntimeError("router down")), "Routing unavailable"),
        # A no-verdict (not an outage) carries the router's own reason so the
        # user sees why nothing was routed.
        (FakeRoutingClient(None, last_error="menu mismatch"), "menu mismatch"),
        # No routing client configured at all is the same advisory outcome.
        (None, "no routing client configured"),
    ],
)
async def test_router_failure_allows_the_spawn_unchanged(
    client: FakeRoutingClient | None,
    reason_fragment: str,
) -> None:
    caps = FakeCaps(routing_client=client, routing_settings=RoutingSettings())
    decision = await resolve_subagent_route("conv_1", _request(), caps=caps)
    assert decision.action == "allow"
    assert reason_fragment in decision.rationale


async def test_a_swallowed_router_exception_still_names_its_cause() -> None:
    """A client that raises past its own reporting leaves the cause on the chip.

    ``last_error`` is unset when ``route`` raises before it can record one, and
    the decline then said only "router returned no verdict" while the real
    cause (a timeout, which stringifies empty) lived in the server log alone.
    """
    import httpx

    caps = FakeCaps(
        routing_client=FakeRoutingClient(error=httpx.ReadTimeout("")),
        routing_settings=RoutingSettings(),
    )
    decision = await resolve_subagent_route("conv_1", _request(), caps=caps)
    assert decision.action == "allow"
    assert "router call failed: ReadTimeout" in decision.rationale


async def test_identical_spawns_are_each_routed() -> None:
    """Decisions are not cached: a decision_id is an identity, not a key."""
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-sdk")
    )
    caps = FakeCaps(routing_client=client)
    first = await resolve_subagent_route("conv_1", _request(), caps=caps)
    second = await resolve_subagent_route("conv_1", _request(), caps=caps)
    assert len(client.calls) == 2
    assert second.decision_id != first.decision_id


async def test_task_name_is_capped_at_parse_time() -> None:
    """``task_name`` is agent-authored, so it is bounded before it is stored."""
    req = SubagentRouteRequest.from_payload({"harness": "codex", "task_name": "x" * 5000})
    assert len(req.task_name) == 200


# ── Decision persistence ────────────────────────────────────────────


async def test_every_decision_is_persisted_with_native_subagent_scope() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-sdk")
    )
    records: list[RoutingDecisionData] = []

    async def _persist(record: RoutingDecisionData) -> None:
        records.append(record)

    decision = await resolve_subagent_route(
        "conv_1",
        _request(),
        caps=FakeCaps(routing_client=client),
        persist=_persist,
    )
    assert len(records) == 1
    data = records[0].model_dump()
    assert data["scope"] == "native_subagent"
    assert data["decision_id"] == decision.decision_id
    # The router named the model it resolved to, so there is no raw pick to
    # report separately.
    assert data["raw_model"] is None
    assert data["model"] == CLAUDE_MODEL
    assert data["harness"] == "claude-native"
    assert data["applied"] is True
    assert data["rationale"] == "deep reasoning"


async def test_deny_is_persisted_unapplied() -> None:
    """An unoffered pick is the only deny left, and it persists unapplied."""
    client = FakeRoutingClient(
        RoutingResult(model="databricks-kimi-k2", rationale="unavailable", harness="claude-sdk")
    )
    records: list[RoutingDecisionData] = []

    async def _persist(record: RoutingDecisionData) -> None:
        records.append(record)

    caps = FakeCaps(routing_client=client)
    await resolve_subagent_route("conv_1", _request(), caps=caps, persist=_persist)
    assert records[0].applied is False
    assert records[0].model == PARENT_MODEL


async def test_persist_appends_routing_decision_item() -> None:
    store = _FakeStore()
    record = RoutingDecisionData(
        model=CLAUDE_MODEL,
        applied=True,
        rationale="deep reasoning",
        decision_id="dec_1",
        harness="claude-native",
        raw_model="claude-opus-4-8",
        scope="native_subagent",
    )
    await persist_subagent_decision("conv_1", store, record)
    assert len(store.appended) == 1
    item = store.appended[0]
    assert item.type == "routing_decision"
    assert item.data.model == CLAUDE_MODEL
    assert item.data.rationale == "deep reasoning"
    # The additive fields ride along once the item model carries them.
    if hasattr(item.data, "scope"):
        assert item.data.scope == "native_subagent"
        assert item.data.decision_id == "dec_1"
        assert item.data.raw_model == "claude-opus-4-8"
        assert item.data.harness == "claude-native"


async def test_persist_failure_does_not_break_the_decision() -> None:
    class _BoomStore:
        def append(self, session_id: str, items: list[Any]) -> list[_PersistedItem]:
            raise RuntimeError("db down")

    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-sdk")
    )

    async def _persist(record: RoutingDecisionData) -> None:
        await persist_subagent_decision("conv_1", _BoomStore(), record)

    decision = await resolve_subagent_route(
        "conv_1",
        _request(),
        caps=FakeCaps(routing_client=client),
        persist=_persist,
    )
    assert decision.action == "rewrite"


# ── Advertisement + loopback endpoint ───────────────────────────────


def test_advertisement_roundtrip(tmp_path: Path) -> None:
    path = write_advertisement(tmp_path, url="http://127.0.0.1:1234", token="tok")
    assert path.name == ADVERTISEMENT_FILE
    endpoint = read_router_endpoint(tmp_path)
    assert endpoint is not None
    assert (endpoint.url, endpoint.token) == ("http://127.0.0.1:1234", "tok")


def _post(url: str, body: dict[str, Any], token: str | None) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


async def test_loopback_endpoint_serves_decisions_and_checks_token(tmp_path: Path) -> None:
    canned = SubagentRouteDecision(
        action="rewrite", rationale="deep reasoning", model=CLAUDE_MODEL, raw_model=CLAUDE_MODEL
    )
    seen: list[SubagentRouteRequest] = []

    async def _resolver(session_id: str, req: SubagentRouteRequest) -> SubagentRouteDecision:
        del session_id
        seen.append(req)
        return canned

    router = start_subagent_router(
        bridge_dir=tmp_path,
        session_id="conv_1",
        resolver=_resolver,
        loop=asyncio.get_running_loop(),
    )
    try:
        advertised = read_router_endpoint(tmp_path)
        assert advertised is not None
        url = f"{advertised.url}/v1/sessions/conv_1/route-subagent"
        body = {
            "harness": "claude-native",
            "task_name": "code-reviewer",
            "prompt": "review the diff",
            "fork": False,
            "parent_model": PARENT_MODEL,
        }

        status, payload = await asyncio.to_thread(_post, url, body, advertised.token)
        assert status == 200
        assert payload == canned.to_payload()
        assert seen[0].task_name == "code-reviewer"

        status, _ = await asyncio.to_thread(_post, url, body, "wrong-token")
        assert status == 401
        status, _ = await asyncio.to_thread(_post, url, body, None)
        assert status == 401
        assert len(seen) == 1

        status, _ = await asyncio.to_thread(
            _post,
            f"{advertised.url}/v1/sessions/other/route-subagent",
            body,
            advertised.token,
        )
        assert status == 404

        status, _ = await asyncio.to_thread(_post, url, {"task_name": "x"}, advertised.token)
        assert status == 400
    finally:
        router.close()
    assert not (tmp_path / ADVERTISEMENT_FILE).exists()


async def test_server_relay_resolver_forwards_and_parses() -> None:
    posted: list[tuple[str, dict[str, Any]]] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "action": "redirect",
                "model": GPT_MODEL,
                "harness": "codex-native",
                "raw_model": "gpt-5-6-sol",
                "rationale": "narrow change",
                "decision_id": "dec_9",
            }

    class _Client:
        async def post(self, path: str, *, json: dict[str, Any], timeout: float) -> _Resp:
            del timeout
            posted.append((path, json))
            return _Resp()

    resolver = make_server_relay_resolver(_Client())
    decision = await resolver("conv_1", _request(requested_model=GLM_SERVABLE))
    assert posted[0][0] == "/v1/sessions/conv_1/hooks/route-subagent"
    assert posted[0][1]["harness"] == "claude-native"
    # Every routing input survives the hop — dropping one here silently
    # changes the server's verdict (requested_model was once lost this way).
    assert posted[0][1]["requested_model"] == GLM_SERVABLE
    assert posted[0][1]["prompt"] == "review the diff"
    assert decision.action == "redirect"
    assert decision.harness == "codex-native"
    assert decision.decision_id == "dec_9"


async def test_server_relay_resolver_allows_on_hop_failure() -> None:
    class _DeadClient:
        async def post(self, path: str, *, json: dict[str, Any], timeout: float) -> Any:
            raise RuntimeError("connection refused")

    decision = await make_server_relay_resolver(_DeadClient())("conv_1", _request())
    assert decision.action == "allow"
    assert "unreachable" in decision.rationale


async def test_loopback_endpoint_allows_when_the_resolver_errors(tmp_path: Path) -> None:
    async def _resolver(session_id: str, req: SubagentRouteRequest) -> SubagentRouteDecision:
        raise RuntimeError("boom")

    router = start_subagent_router(
        bridge_dir=tmp_path,
        session_id="conv_1",
        resolver=_resolver,
        loop=asyncio.get_running_loop(),
    )
    try:
        advertised = read_router_endpoint(tmp_path)
        assert advertised is not None
        status, payload = await asyncio.to_thread(
            _post,
            f"{advertised.url}/v1/sessions/conv_1/route-subagent",
            {"harness": "claude-native"},
            advertised.token,
        )
        assert status == 200
        assert payload["action"] == "allow"
    finally:
        router.close()


# ── Enablement gate + session router lifecycle (P7) ─────────────────
#
# Two gates, deliberately named apart. ``routing_enabled`` is the TURN gate: it
# decides whether a session's own turns are routed, and a child reads its
# parent's cost-control mode because the server routes children of a routed
# parent. ``subagent_routing_enabled`` is the SPAWN gate: two-state, stamped at
# create, and it reads one explicit switch. The turn-gate tests below used to be
# named ``test_routing_enabled_*``, which read as the spawn gate.


@pytest.mark.parametrize(
    ("mode", "parent_mode", "expected"),
    [
        ("on", None, True),
        ("off", None, False),
        (None, None, False),
        # An unset session reads its parent's mode — the turn gate, not the
        # spawn gate, which is two-state and never re-derives from a parent.
        (None, "on", True),
    ],
)
def test_turn_routing_enabled_reads_the_session_toggle(
    mode: str | None, parent_mode: str | None, expected: bool
) -> None:
    assert routing_enabled(mode, parent_cost_control_mode=parent_mode) is expected


@pytest.mark.parametrize(
    ("routing_client", "expected"),
    [
        # Toggle on but nothing to route with: still off.
        (None, False),
        (object(), True),
    ],
)
def test_turn_routing_enabled_requires_a_client_when_caps_are_given(
    routing_client: object | None, expected: bool
) -> None:
    assert routing_enabled("on", caps=FakeCaps(routing_client=routing_client)) is expected


def test_turn_routing_enabled_counts_a_managed_policy_llm_factory() -> None:
    """A managed deployment supplies its routing client later, so it can route.

    Reading the backends directly missed this arm and reported routing off for
    a deployment the server would have routed.
    """

    class _ManagedCaps:
        routing_client = None
        routing_backends = None
        policy_llm_connection_factory = object()

    assert routing_enabled("on", caps=_ManagedCaps()) is True


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        # Two-state: only an explicit "on" routes spawns. A routed create is
        # stamped "on", so unset is Default and reads exactly like "off".
        ("on", True),
        ("off", False),
        (None, False),
        # A row written before the switch became two-state can still hold the
        # old tri-state "inherit"; it reads as not-enabled, so no stored value
        # needs rewriting for the gate to be sound.
        ("inherit", False),
        ("", False),
    ],
)
def test_subagent_routing_enabled_is_two_state(override: str | None, expected: bool) -> None:
    assert subagent_routing_enabled(override) is expected


# ── Session routing class ───────────────────────────────────────────
#
# The three-way distinction the codex launch paths key off. Kept as its own
# value rather than folded into one bool: "Smart Routing is on" and "Smart
# Routing also owns the harness" buy different things on codex, and conflating
# them is what made a plain session pay the routing path's costs.


@pytest.mark.parametrize(
    ("cost_control_mode", "harness_override", "labels", "expected"),
    [
        # Plain: no Smart Routing anywhere.
        (None, None, {}, SessionRoutingClass()),
        ("off", "codex-native", {}, SessionRoutingClass()),
        # Pinned: Smart Routing as the model, codex chosen as the harness.
        (
            "on",
            "codex-native",
            {},
            SessionRoutingClass(routing_enabled=True, turn_routing=True),
        ),
        # Auto-harness, before first-message routing replaces the sentinel.
        (
            "on",
            "auto",
            {},
            SessionRoutingClass(routing_enabled=True, auto_harness=True, turn_routing=True),
        ),
        # …and after: the sentinel is gone, the durable label is not.
        (
            "on",
            "codex-native",
            {AUTO_HARNESS_LABEL_KEY: "1"},
            SessionRoutingClass(routing_enabled=True, auto_harness=True, turn_routing=True),
        ),
        # An auto-harness session is a Smart Routing session by construction,
        # so the label alone implies routing is on.
        (
            None,
            None,
            {AUTO_HARNESS_LABEL_KEY: "1"},
            SessionRoutingClass(routing_enabled=True, auto_harness=True, turn_routing=True),
        ),
        # Already routed — a web create pinned the model before the pane
        # launched. The session keeps every other routing affordance; only the
        # first-message hook is dropped, since it could only say "already
        # routed" once per prompt.
        (
            "on",
            "claude-native",
            {ROUTING_DECISION_LABEL_KEY: "rd_1"},
            SessionRoutingClass(routing_enabled=True),
        ),
        # Routing off plus a stale decision label is still plain.
        ("off", "claude-native", {ROUTING_DECISION_LABEL_KEY: "rd_1"}, SessionRoutingClass()),
        # An auto session the create could not route keeps its hook: the first
        # message is its retry.
        (
            "on",
            "auto",
            {AUTO_HARNESS_LABEL_KEY: "1"},
            SessionRoutingClass(routing_enabled=True, auto_harness=True, turn_routing=True),
        ),
    ],
)
def test_routing_class_from_snapshot(
    cost_control_mode: str | None,
    harness_override: str | None,
    labels: dict[str, str],
    expected: SessionRoutingClass,
) -> None:
    assert (
        routing_class_from_snapshot(
            cost_control_mode=cost_control_mode,
            harness_override=harness_override,
            labels=labels,
        )
        == expected
    )


def test_the_recorded_routing_class_survives_until_the_session_ends() -> None:
    assert session_routing_class("conv_class") == PLAIN_SESSION
    auto = SessionRoutingClass(routing_enabled=True, auto_harness=True)
    remember_session_routing_class("conv_class", auto)
    try:
        assert session_routing_class("conv_class") == auto
    finally:
        forget_session_routing_class("conv_class")
    assert session_routing_class("conv_class") == PLAIN_SESSION


def test_router_dir_for_session_is_owner_only(tmp_path: Path) -> None:
    path = router_dir_for_session("conv_router_dir")
    assert path.is_dir()
    assert path.stat().st_mode & 0o777 == 0o700


def test_ensure_session_router_is_idempotent_and_advertises_everywhere(
    tmp_path: Path,
) -> None:
    class _DeadClient:
        async def post(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("server down")

    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"

    async def _run() -> None:
        router = ensure_session_router(
            "conv_lifecycle",
            bridge_dir=first_dir,
            server_client=_DeadClient(),
        )
        again = ensure_session_router(
            "conv_lifecycle",
            bridge_dir=second_dir,
            server_client=_DeadClient(),
        )
        assert again is router
        # Same rendezvous advertised in both directories.
        assert read_router_endpoint(first_dir) == read_router_endpoint(second_dir)
        env = session_router_env("conv_lifecycle")
        assert env["OMNIGENT_SUBAGENT_ROUTER_SESSION_ID"] == "conv_lifecycle"
        assert env["OMNIGENT_CODEX_SUBAGENT_ROUTER_DIR"] == str(first_dir)
        shutdown_session_router("conv_lifecycle")
        assert session_router_env("conv_lifecycle") == {}

    asyncio.run(_run())


def test_router_startup_log_omits_the_rendezvous(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The handle carries the bearer token, so neither url nor token is logged."""

    class _DeadClient:
        async def post(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("server down")

    async def _run() -> None:
        with caplog.at_level(logging.INFO, logger="omnigent.runner.subagent_routing"):
            router = ensure_session_router(
                "conv_log_redaction",
                bridge_dir=tmp_path,
                server_client=_DeadClient(),
            )
        try:
            text = caplog.text
            assert "subagent router started" in text
            assert router.token not in text
            assert router.url not in text
            # Nothing that addresses the rendezvous is logged either.
            assert "conv_log_redaction" not in text
            assert str(tmp_path) not in text
        finally:
            shutdown_session_router("conv_log_redaction")

    asyncio.run(_run())


def test_ensure_session_router_quietly_skips_without_a_server_client(tmp_path: Path) -> None:
    assert (
        ensure_session_router_quietly(
            "conv_no_client",
            bridge_dir=tmp_path,
            server_client=None,
        )
        is None
    )
    assert not (tmp_path / ADVERTISEMENT_FILE).exists()


def test_ensure_session_router_quietly_swallows_a_bind_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runner import subagent_routing as module

    def _boom(session_id: str, **kwargs: Any) -> None:
        raise OSError("address in use")

    monkeypatch.setattr(module, "ensure_session_router", _boom)
    assert (
        ensure_session_router_quietly(
            "conv_bind_fail",
            bridge_dir=tmp_path,
            server_client=object(),  # type: ignore[arg-type]
            harness="codex-native",
            caps=FakeCaps(),
        )
        is None
    )


def test_relayed_decisions_start_empty() -> None:
    assert relayed_decisions("conv_never_routed") == ()
    assert routed_models("conv_never_routed") == frozenset()


# ── Router source ───────────────────────────────────────────────────
#
# Gateway backing selects WHICH router answers a spawn, and the verdict records
# it so the chip can say so. Off the gateway the static ``infer_models`` table is
# unusable (all ``databricks-*`` ids), so only the live catalog may supply
# candidates.


async def test_an_off_gateway_spawn_is_routed_by_the_built_in_judge() -> None:
    from omnigent.server.routing_backend import RoutingBackends

    external = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="external"))
    local = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="local"))
    caps = FakeCaps(
        routing_client=external,
        routing_backends=RoutingBackends(external=external, local=local),
    )
    decision = await resolve_subagent_route(
        "conv_oss", _request(), caps=caps, gateway_backed=False
    )
    assert decision.router_source == "oss-llm"
    assert decision.rationale == "local"
    assert external.calls == []


async def test_a_gateway_backed_spawn_is_routed_by_the_external_router() -> None:
    from omnigent.server.routing_backend import RoutingBackends

    external = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="external"))
    local = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="local"))
    caps = FakeCaps(
        routing_client=external,
        routing_backends=RoutingBackends(external=external, local=local),
    )
    decision = await resolve_subagent_route(
        "conv_aigw", _request(), caps=caps, gateway_backed=True
    )
    assert decision.router_source == "databricks-aigw"
    assert local.calls == []


async def test_a_spawn_falls_back_to_the_judge_when_the_router_fails() -> None:
    """A workspace with no routing API must not cost every spawn its decision."""
    from omnigent.server.routing_backend import RoutingBackends

    external = FakeRoutingClient(
        None, last_error="router returned HTTP 404: routes:select is not enabled"
    )
    local = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="local"))
    caps = FakeCaps(
        routing_client=external,
        routing_backends=RoutingBackends(external=external, local=local),
    )
    decision = await resolve_subagent_route(
        "conv_fallback", _request(), caps=caps, gateway_backed=True
    )
    assert decision.router_source == "oss-llm"
    assert decision.model == CLAUDE_MODEL
    assert external.calls != []


async def test_a_spawn_keeps_the_routers_reason_when_nothing_can_answer() -> None:
    """Fail-open is unchanged: the spawn runs, the chip carries the reason."""
    from omnigent.server.routing_backend import RoutingBackends

    external = FakeRoutingClient(None, last_error="router returned HTTP 404: not enabled")
    local = FakeRoutingClient(None, last_error="the judge had no opinion")
    caps = FakeCaps(
        routing_client=external,
        routing_backends=RoutingBackends(external=external, local=local),
    )
    decision = await resolve_subagent_route(
        "conv_none", _request(), caps=caps, gateway_backed=True
    )
    assert decision.action == "allow"
    assert decision.model is None
    assert decision.router_source is None
    assert "the judge had no opinion" in decision.rationale


async def test_an_external_only_deployment_cannot_route_an_off_gateway_spawn() -> None:
    from omnigent.server.routing_backend import RoutingBackends

    external = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="external"))
    caps = FakeCaps(routing_client=external, routing_backends=RoutingBackends(external=external))
    decision = await resolve_subagent_route(
        "conv_no_source", _request(), caps=caps, gateway_backed=False
    )
    assert decision.action == "allow"
    assert decision.model is None
    assert decision.router_source is None
    assert external.calls == []


def test_candidate_models_skips_the_static_table_when_it_is_unreachable() -> None:
    # No catalog and no static fallback leaves nothing to offer.
    assert candidate_models("claude-native", allow_static_fallback=False) == {}
    # The live catalog still supplies candidates, in the pane's own spelling.
    offered = candidate_models(
        "claude-native",
        catalog={"self": ["claude-opus-4-8"]},
        allow_static_fallback=False,
    )
    assert offered == {"claude-native": ["claude-opus-4-8"]}


async def test_router_source_survives_the_payload_round_trip() -> None:
    decision = SubagentRouteDecision(
        action="rewrite",
        rationale="deep reasoning",
        model=CLAUDE_MODEL,
        router_source="databricks-aigw",
    )
    payload = decision.to_payload()
    assert payload["router_source"] == "databricks-aigw"
    assert SubagentRouteDecision.from_payload(payload).router_source == "databricks-aigw"


def test_a_payload_without_a_router_source_parses_as_none() -> None:
    parsed = SubagentRouteDecision.from_payload(
        {"action": "rewrite", "rationale": "x", "model": CLAUDE_MODEL}
    )
    assert parsed.router_source is None
    # A blank string is no source at all, not an empty one.
    assert SubagentRouteDecision.from_payload({"router_source": ""}).router_source is None


def test_decision_record_copies_the_router_source() -> None:
    req = _request()
    decision = SubagentRouteDecision(
        action="rewrite",
        rationale="deep reasoning",
        model=CLAUDE_MODEL,
        router_source="oss-llm",
    )
    assert decision_record(req, decision).router_source == "oss-llm"
    # A fail-open allow routed nothing, so it names no source.
    unrouted = SubagentRouteDecision(action="allow", rationale="Routing unavailable")
    assert decision_record(req, unrouted).router_source is None
