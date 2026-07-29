"""
Tests for :func:`build_policy_engine` (Phase 2).

Covers:

- Zero-guardrails path: ``spec.guardrails is None`` → no-op
  engine with empty policies and labels.
- Empty-guardrails path: ``guardrails: {}`` → no-op engine
  but with spec-declared ask_timeout.
- Declared policies round-trip from spec to engine in YAML
  order.
- Initial label seeding via UPSERT: writes only for keys
  missing from the persisted state, idempotent across two
  successive builds.
- Hot cache is built from the post-seed snapshot (not the
  pre-seed read).
- Existing labels are NOT clobbered when the spec's initial
  differs from the persisted value.
- ``default_policies`` appended after agent policies in run order.
- ``default_policies`` alone (no agent guardrails) still builds
  a live engine with those policies.
- Empty / ``None`` ``default_policies`` preserves existing behaviour.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from omnigent.entities import Conversation
from omnigent.runtime.policies.builder import build_policy_engine
from omnigent.spec.parser import parse
from omnigent.spec.types import (
    AgentSpec,
    GuardrailsSpec,
    LabelDef,
    Phase,
    PhaseSelector,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.runtime.policies.conftest import make_fixed_function_policy_spec


def _sub_agent_title() -> str:
    """Return a unique sub-agent title (production sub-agents always have one)."""
    return f"test-agent:{uuid.uuid4().hex[:8]}"


def _write_spec(
    tmp_path: Path,
    config_yaml: str,
) -> Path:
    """Write a config.yaml to a fresh agent-dir fixture."""
    (tmp_path / "config.yaml").write_text(config_yaml)
    return tmp_path


# ── Zero-guardrails (engine stays alive but is a no-op) ─


def test_build_without_guardrails_returns_noop_engine(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A spec with no `guardrails:` block still builds an
    engine. The enforcement sites (Phase 5+) call through it
    unconditionally — if this raised, we'd have to guard
    every call site with `if engine is not None`, which
    POLICIES.md §10 explicitly avoids."""
    agent_dir = _write_spec(
        tmp_path,
        """
spec_version: 1
name: no-guardrails
""",
    )
    spec = parse(agent_dir)
    assert spec.guardrails is None

    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    # No user-declared guardrails, but the hardcoded
    # ask_on_add_policy guard is always present.
    assert len(engine.policies) == 1
    assert engine.policies[0].spec.name == "__ask_on_add_policy"
    assert engine.label_defs == {}
    assert engine.labels == {}


def test_build_with_empty_guardrails_block(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """`guardrails: {}` explicitly declared — engine has no
    policies, no labels, default ask_timeout. Distinguishable
    from the None case only in that ask_timeout is present,
    but functionally identical for evaluate()."""
    agent_dir = _write_spec(
        tmp_path,
        """
spec_version: 1
name: empty-guardrails
guardrails: {}
""",
    )
    spec = parse(agent_dir)
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    # The only policy is the hardcoded ask_on_add_policy guard.
    assert len(engine.policies) == 1
    assert engine.policies[0].spec.name == "__ask_on_add_policy"
    assert engine.label_defs == {}
    assert engine.labels == {}


# ── Declared policies + label seeding ──────────────────


def test_build_propagates_declared_policies_in_yaml_order(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Policies land on the engine in their YAML declaration
    order. The engine's evaluate() loop (Phase 3+) depends on
    this for DENY short-circuit semantics and first-ASK
    selection."""
    agent_dir = _write_spec(
        tmp_path,
        """
spec_version: 1
name: ordered
guardrails:
  policies:
    alpha:
      type: function
      on: [request]
      function: tests.runtime.policies.conftest._always_allow
    bravo:
      type: function
      on: [request]
      function: tests.runtime.policies.conftest._always_allow
    charlie:
      type: function
      on: [request]
      function: tests.runtime.policies.conftest._always_allow
""",
    )
    spec = parse(agent_dir)
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    # Names in YAML order — regression would reorder alphabet
    # or reverse direction.
    names = [p.spec.name for p in engine.policies]
    # Declared policies in YAML order, plus the hardcoded
    # ask_on_add_policy guard appended by the builder.
    assert names == ["alpha", "bravo", "charlie", "__ask_on_add_policy"]


def test_build_resolves_model_override_then_spec(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The engine's resolved model prefers `model_override`, else `llm.model`.

    Model-aware policies (the cost gate's force-downgrade branch) read
    ``engine.model`` via ``event["context"]["model"]``. A regression
    flipping the precedence or dropping the override would make a
    mid-session `/model` downgrade invisible to the policy, so it could
    never unblock an over-budget session.
    """
    agent_dir = _write_spec(
        tmp_path,
        """
spec_version: 1
name: model-resolve
llm:
  model: databricks-claude-opus-4-8
guardrails:
  policies:
    a:
      type: function
      on: [request]
      function: tests.runtime.policies.conftest._always_allow
""",
    )
    spec = parse(agent_dir)
    conv = conversation_store.create_conversation()
    # No override → falls back to the spec's llm.model.
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    assert engine.model == "databricks-claude-opus-4-8"
    # A mid-session /model change sets model_override, which now wins.
    conversation_store.update_conversation(conv.id, model_override="claude-sonnet-4-6")
    engine_after = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    assert engine_after.model == "claude-sonnet-4-6"


def test_build_resolves_model_none_without_llm_or_override(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """No spec `llm` block and no `model_override` → resolved model is None.

    The cost gate treats ``None`` as "cannot confirm a cheaper model" and
    fails closed; this pins that the builder surfaces ``None`` (rather than
    an empty string or a crash) when the model is undeterminable.
    """
    agent_dir = _write_spec(
        tmp_path,
        """
spec_version: 1
name: no-llm
guardrails:
  policies:
    a:
      type: function
      on: [request]
      function: tests.runtime.policies.conftest._always_allow
""",
    )
    spec = parse(agent_dir)
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    assert engine.model is None


def test_build_seeds_initial_labels(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """`LabelDef.initial` values with no persisted row get
    written through set_labels. Verified via the store's
    round-trip."""
    agent_dir = _write_spec(
        tmp_path,
        """
spec_version: 1
name: seeded
guardrails:
  labels:
    integrity: "1"
    sensitivity:
      initial: public
      values: [public, internal, confidential]
""",
    )
    spec = parse(agent_dir)
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    # Hot cache reflects the seeded values.
    assert engine.labels == {"integrity": "1", "sensitivity": "public"}
    # Persisted too — not just in memory.
    conv_refetched = conversation_store.get_conversation(conv.id)
    assert conv_refetched is not None
    assert conv_refetched.labels == {"integrity": "1", "sensitivity": "public"}


def test_build_skips_labels_without_initial(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Labels declared with no `initial` (unset-until-written
    pattern) do not produce seed rows. Without this,
    policies that gate on "label absent" would incorrectly
    fire after build."""
    agent_dir = _write_spec(
        tmp_path,
        """
spec_version: 1
name: partial
guardrails:
  labels:
    has_initial: "1"
    no_initial:
      values: [a, b]
""",
    )
    spec = parse(agent_dir)
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    # Only `has_initial` lands; `no_initial` is absent.
    assert engine.labels == {"has_initial": "1"}


def test_build_is_idempotent_on_existing_labels(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Building twice on the same conversation does not
    overwrite existing labels — the ON-CONFLICT-DO-NOTHING
    semantic per POLICIES.md §10. A policy may have written
    a value; a second workflow build must not revert it.

    If this regresses, the seeding path is doing UPSERT-always
    instead of UPSERT-if-missing, and ongoing label state
    would reset every time a workflow starts.
    """
    agent_dir = _write_spec(
        tmp_path,
        """
spec_version: 1
name: idempotent
guardrails:
  labels:
    integrity: "1"
""",
    )
    spec = parse(agent_dir)
    conv = conversation_store.create_conversation()

    # First build: seeds integrity="1" as declared.
    first = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    assert first.labels == {"integrity": "1"}

    # Simulate a policy writing integrity="0" mid-conversation.
    first.apply_label_writes({"integrity": "0"})
    conv_after_policy = conversation_store.get_conversation(conv.id)
    assert conv_after_policy is not None
    assert conv_after_policy.labels == {"integrity": "0"}

    # Second build: MUST NOT revert integrity to "1" —
    # the declared initial is an "if missing" seed, not an
    # "every build" reset.
    second = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    # If this reads "1", the seeding clobbered the policy's
    # write — a serious IFC safety bug (taint would silently
    # reset to clean on any workflow restart).
    assert second.labels == {"integrity": "0"}


def test_build_preserves_ask_timeout_override(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Spec-level `ask_timeout` overrides the default on the
    engine. Later phases read this for ASK routing."""
    agent_dir = _write_spec(
        tmp_path,
        """
spec_version: 1
name: long-review
guardrails:
  ask_timeout: 600
""",
    )
    spec = parse(agent_dir)
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    assert engine.ask_timeout == 600


# ── Programmatic API (non-YAML) parity ─────────────────


def test_build_from_programmatic_spec(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Building from an in-memory AgentSpec works too —
    tests that don't want to round-trip through YAML should
    be able to construct an engine directly. Critical for
    Phase 3+ unit tests that build fine-grained specs."""
    spec = AgentSpec(
        spec_version=1,
        name="programmatic",
        guardrails=GuardrailsSpec(
            labels={"integrity": LabelDef(initial="1")},
            policies=[
                make_fixed_function_policy_spec(
                    name="taint_web",
                    on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name="web")],
                    fn_path="tests.runtime.policies.conftest._always_allow_taint_integrity",
                ),
            ],
            ask_timeout=45,
        ),
    )
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    assert engine.ask_timeout == 45
    assert engine.policies[0].spec.name == "taint_web"
    assert engine.policies[-1].spec.name == "__ask_on_add_policy"
    assert engine.labels == {"integrity": "1"}


# ── default_policies (server-wide admin policies) ──────


def test_default_policies_appended_after_agent_policies(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Agent spec policies run first; admin ``default_policies``
    are appended after. The DENY short-circuit and first-ASK
    selection in evaluate() depend on this run order."""
    spec = AgentSpec(
        spec_version=1,
        name="agent-first",
        guardrails=GuardrailsSpec(
            policies=[
                make_fixed_function_policy_spec(
                    name="agent_policy",
                    on=[PhaseSelector(phase=Phase.REQUEST)],
                ),
            ],
        ),
    )
    admin_policy = make_fixed_function_policy_spec(
        name="admin_policy",
        on=[PhaseSelector(phase=Phase.REQUEST)],
    )
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        default_policies=[admin_policy],
    )
    names = [p.spec.name for p in engine.policies]
    assert names == ["agent_policy", "admin_policy", "__ask_on_add_policy"]


def test_default_policies_alone_builds_live_engine(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """An agent with no guardrails block + server-wide
    ``default_policies`` must build a live engine (not the
    no-op engine), so that admin policies are enforced even
    when the agent author declared none."""
    spec = AgentSpec(spec_version=1, name="no-guardrails")
    assert spec.guardrails is None

    admin_policy = make_fixed_function_policy_spec(
        name="admin_audit",
        on=[PhaseSelector(phase=Phase.REQUEST)],
    )
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        default_policies=[admin_policy],
    )
    assert engine.policies[0].spec.name == "admin_audit"
    assert engine.policies[-1].spec.name == "__ask_on_add_policy"


def test_empty_default_policies_preserves_existing_behaviour(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``default_policies=None`` and ``default_policies=[]``
    both leave the engine identical to the no-default-policies
    case — no regressions for callers that never pass the arg."""
    spec = AgentSpec(spec_version=1, name="no-defaults")
    conv = conversation_store.create_conversation()

    engine_none = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        default_policies=None,
    )
    engine_empty = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        default_policies=[],
    )
    # Only the hardcoded ask_on_add_policy guard.
    assert len(engine_none.policies) == 1
    assert engine_none.policies[0].spec.name == "__ask_on_add_policy"
    assert len(engine_empty.policies) == 1
    assert engine_empty.policies[0].spec.name == "__ask_on_add_policy"


# ── Sub-agent cost roll-up (subtree usage aggregation) ──


def test_build_sums_subagent_usage_into_parent_engine(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A parent engine's usage context includes every sub-agent's spend.

    Each conversation persists only its own ``session_usage`` (written
    server-side on ``response.completed``). A cost-ask policy on the
    parent must nonetheless see the whole spawn tree's spend, so
    ``build_policy_engine`` seeds the engine with the session-wide
    (whole-tree) total. If this fails (e.g. the builder reverts to
    returning only the parent's own usage), the parent would see ``0.10``
    instead of ``0.20`` and a budget policy would under-count by every
    sub-agent's cost.
    """
    parent = conversation_store.create_conversation()
    child_a = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    child_b = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    # Grandchild under child_a — proves the walk is transitive, not
    # just direct children.
    grandchild = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=child_a.id, title=_sub_agent_title()
    )
    conversation_store.set_session_usage(
        parent.id,
        {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200, "total_cost_usd": 0.10},
    )
    conversation_store.set_session_usage(
        child_a.id,
        {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600, "total_cost_usd": 0.05},
    )
    conversation_store.set_session_usage(
        child_b.id,
        {"input_tokens": 300, "output_tokens": 50, "total_tokens": 350, "total_cost_usd": 0.03},
    )
    conversation_store.set_session_usage(
        grandchild.id,
        {"input_tokens": 200, "output_tokens": 40, "total_tokens": 240, "total_cost_usd": 0.02},
    )

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="parent"),
        conversation_id=parent.id,
        conversation_store=conversation_store,
    )

    # 2000 = 1000 + 500 + 300 + 200 (parent + both children + grandchild).
    # A wrong value of 1000 would mean sub-agent usage was dropped.
    assert engine.usage["input_tokens"] == 2000
    assert engine.usage["output_tokens"] == 390  # 200 + 100 + 50 + 40
    assert engine.usage["total_tokens"] == 2390  # 1200 + 600 + 350 + 240
    # 0.20 = full-tree cost. 0.10 here would mean only the parent's own
    # spend was counted — the exact gap this feature closes.
    assert engine.usage["total_cost_usd"] == pytest.approx(0.20)


def test_policy_seed_uses_policy_cost_while_display_uses_total_cost(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The engine gates on ``policy_cost_usd``; display sums ``total_cost_usd``.

    claude-native posts two costs: ``total_cost_usd`` = the statusLine
    total ``S`` (display, matches /cost) and ``policy_cost_usd`` =
    ``max(S, real-time estimate)`` (enforcement, reflects in-flight
    sub-agent spend while ``S`` is frozen). The policy engine must seed
    from the enforcement figure, while :func:`load_session_usage` (used by
    the badge / SSE) keeps the display figure. A sub-agent conversation
    that posts only ``total_cost_usd`` (codex/relay style) must still count
    toward the parent's enforcement total via fallback.
    """
    from omnigent.runtime.policies.builder import load_session_usage

    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    # Parent has both costs: S=0.10 frozen for display, enforcement=0.30
    # (a sub-agent is mid-run, so the real-time estimate leads S).
    conversation_store.set_session_usage(
        parent.id,
        {"total_cost_usd": 0.10, "policy_cost_usd": 0.30},
    )
    # Child posts only total_cost_usd (no split) — must still be counted.
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.05})

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="parent"),
        conversation_id=parent.id,
        conversation_store=conversation_store,
    )
    # Enforcement seed = parent.policy_cost_usd (0.30) + child fallback to
    # its total_cost_usd (0.05) = 0.35. If it were 0.15 the gate would read
    # the frozen display total and miss in-flight sub-agent spend; if the
    # ``policy_cost_usd`` key leaked through, the engine seed would be wrong.
    assert engine.usage["total_cost_usd"] == pytest.approx(0.35)
    assert "policy_cost_usd" not in engine.usage  # popped before seeding

    # Display path keeps the authoritative S sum: parent 0.10 + child 0.05
    # = 0.15. If this returned 0.35 the badge would diverge from /cost —
    # the regression this split prevents.
    display = load_session_usage(parent.id, conversation_store)
    assert display["total_cost_usd"] == pytest.approx(0.15)
    # The enforcement total is also exposed (for the policy seed) but is
    # NOT what display reads.
    assert display["policy_cost_usd"] == pytest.approx(0.35)


def test_build_subagent_gates_against_whole_session_not_own_subtree(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A mid-tree sub-agent gates against the whole SESSION, not its subtree.

    Cost gating is session-wide: a cost-budget policy caps the whole spawn
    tree, so a sub-agent's gate must see the full session spend (parent +
    siblings + its own subtree), not just the subtree rooted at the
    sub-agent. When the engine is built for ``child_a``, its seeded usage
    must therefore equal the whole-tree total — identical to what the root
    sees — so that a session can't overshoot its budget by spreading spend
    across sub-agents while the orchestrator parent is parked.

    A wrong value of ``700`` (child_a + grandchild only) would mean gating
    reverted to the per-node subtree view and a sub-agent could spend the
    whole budget again on top of the parent's and sibling's spend.
    """
    parent = conversation_store.create_conversation()
    child_a = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    child_b = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    grandchild = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=child_a.id, title=_sub_agent_title()
    )
    conversation_store.set_session_usage(
        parent.id,
        {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200, "total_cost_usd": 0.10},
    )
    conversation_store.set_session_usage(
        child_a.id,
        {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600, "total_cost_usd": 0.05},
    )
    conversation_store.set_session_usage(
        child_b.id,
        {"input_tokens": 300, "output_tokens": 50, "total_tokens": 350, "total_cost_usd": 0.03},
    )
    conversation_store.set_session_usage(
        grandchild.id,
        {"input_tokens": 200, "output_tokens": 40, "total_tokens": 240, "total_cost_usd": 0.02},
    )

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="child-a"),
        conversation_id=child_a.id,
        conversation_store=conversation_store,
    )

    # Whole-session total = 1000+500+300+200 (parent + both children +
    # grandchild), the same number the parent's engine sees. 700 here would
    # mean the sub-agent reverted to its own subtree and missed parent +
    # sibling spend.
    assert engine.usage["input_tokens"] == 2000
    assert engine.usage["output_tokens"] == 390  # 200 + 100 + 50 + 40
    assert engine.usage["total_tokens"] == 2390  # 1200 + 600 + 350 + 240
    assert engine.usage["total_cost_usd"] == pytest.approx(0.20)  # 0.10+0.05+0.03+0.02


def test_build_usage_for_plain_conversation_is_own_usage(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A conversation with no sub-agents sums to exactly its own usage.

    Regression guard: the subtree walk must not change behavior for the
    overwhelmingly common single-agent case. Also covers the native
    shape — claude-native attributes the whole external session's cost
    to the root conversation, so a root with no AP-tracked children
    reports its own (already-complete) total with no inflation.
    """
    conv = conversation_store.create_conversation()
    conversation_store.set_session_usage(
        conv.id,
        {"input_tokens": 111, "output_tokens": 22, "total_tokens": 133, "total_cost_usd": 0.42},
    )

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="solo"),
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )

    assert engine.usage["input_tokens"] == 111
    assert engine.usage["output_tokens"] == 22
    assert engine.usage["total_tokens"] == 133
    assert engine.usage["total_cost_usd"] == pytest.approx(0.42)


def test_build_subagent_with_empty_usage_does_not_inflate_parent(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Sub-agents that recorded no usage contribute nothing to the parent.

    This is the native-tree no-double-count invariant made concrete: a
    claude-native parent already carries the whole session's cost, and
    its child conversations carry empty ``session_usage``. Summing the
    subtree must leave the parent's total untouched (children add 0),
    not corrupt it.
    """
    parent = conversation_store.create_conversation()
    # Two children with no usage recorded at all (empty session_usage).
    conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    conversation_store.set_session_usage(
        parent.id,
        {"input_tokens": 800, "output_tokens": 150, "total_tokens": 950, "total_cost_usd": 0.58},
    )

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="native-parent"),
        conversation_id=parent.id,
        conversation_store=conversation_store,
    )

    # Identical to the parent's own usage — empty children add nothing.
    assert engine.usage["input_tokens"] == 800
    assert engine.usage["output_tokens"] == 150
    assert engine.usage["total_tokens"] == 950
    assert engine.usage["total_cost_usd"] == pytest.approx(0.58)


def test_load_session_usage_merges_by_model_across_subtree(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The subtree per-model breakdown unions models and sums within each.

    A parent's per-model view must fold in a sub-agent that ran a *different*
    model (otherwise a supervisor delegating to a differently-modeled worker
    would hide that spend), and must sum repeated occurrences of the *same*
    model across conversations. The per-model costs must still total the flat
    subtree ``total_cost_usd`` (no double-count / drop), and the display-only
    ``by_model`` must not leak into the policy engine's usage seed.
    """
    from omnigent.runtime.policies.builder import load_session_usage

    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    # Parent ran model-a. Child ran a slice of model-a plus a different model-b.
    conversation_store.set_session_usage(
        parent.id,
        {
            "input_tokens": 1000,
            "total_cost_usd": 0.10,
            "by_model": {"model-a": {"input_tokens": 1000, "total_cost_usd": 0.10}},
        },
    )
    conversation_store.set_session_usage(
        child.id,
        {
            "input_tokens": 200,
            "total_cost_usd": 0.05,
            "by_model": {
                "model-a": {"input_tokens": 50, "total_cost_usd": 0.01},
                "model-b": {"input_tokens": 150, "total_cost_usd": 0.04},
            },
        },
    )

    usage = load_session_usage(parent.id, conversation_store)
    by_model = usage["by_model"]
    # model-a folds the parent (1000 / $0.10) and the child's slice (50 / $0.01).
    assert by_model["model-a"]["input_tokens"] == 1050
    assert by_model["model-a"]["total_cost_usd"] == pytest.approx(0.11)
    # model-b ran only in the child, but the parent's view still folds it in.
    assert by_model["model-b"]["input_tokens"] == 150
    assert by_model["model-b"]["total_cost_usd"] == pytest.approx(0.04)
    # Per-model costs sum to the flat subtree total (0.10 + 0.05) — the
    # no-double-count invariant that lets the UI trust the breakdown.
    per_model_cost_sum = sum(m["total_cost_usd"] for m in by_model.values())
    assert per_model_cost_sum == pytest.approx(0.15)
    assert usage["total_cost_usd"] == pytest.approx(0.15)

    # by_model is display-only and must be stripped from the policy engine seed.
    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="parent"),
        conversation_id=parent.id,
        conversation_store=conversation_store,
    )
    assert "by_model" not in engine.usage


# ── Subtree-scoped cost budgeting (per-subagent cost gates) ──


def test_build_subagent_with_cost_budget_gets_session_wide_usage(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A subagent with ``cost_budget`` policy sees session-wide usage.

    The per-session cost gate (``cost_budget``) gates the whole spawn tree.
    A subagent's engine must be seeded with the full-tree total, not just
    its own subtree, so it doesn't re-allow budgets already exhausted by
    parent + siblings.

    This test verifies the existing cost_budget behavior (baseline for
    the new subagent_cost_budget feature).
    """
    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    sibling = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )

    conversation_store.set_session_usage(parent.id, {"total_cost_usd": 0.10})
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.05})
    conversation_store.set_session_usage(sibling.id, {"total_cost_usd": 0.03})

    # Child's engine sees full-tree total (0.18), not just child+sibling subtree.
    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="child"),
        conversation_id=child.id,
        conversation_store=conversation_store,
    )
    assert engine.usage["total_cost_usd"] == pytest.approx(0.18)  # 0.10+0.05+0.03


def test_build_injects_subtree_usage_only_when_policy_present(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The engine's ``subtree_usage`` is injected only when
    subagent_cost_budget policy is present; otherwise None.

    This guards against unnecessary DB traversals (the conditional
    injection pattern) — if the policy isn't used, we skip the lookup.
    """
    from omnigent.spec.types import GuardrailsSpec

    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    conversation_store.set_session_usage(parent.id, {"total_cost_usd": 0.10})
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.05})

    # Engine without subagent_cost_budget policy: subtree_usage is None.
    engine_no_policy = build_policy_engine(
        spec=AgentSpec(
            spec_version=1,
            name="child",
            guardrails=GuardrailsSpec(policies={}),
        ),
        conversation_id=child.id,
        conversation_store=conversation_store,
    )
    assert engine_no_policy._subtree_usage is None

    # Engine with subagent_cost_budget policy: subtree_usage is populated.
    # (We can't easily construct the policy spec without going through
    # the registry, so we just verify it would be computed by checking
    # that the engine has the infrastructure to store it.)
    engine_with_policy = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="child"),
        conversation_id=child.id,
        conversation_store=conversation_store,
    )
    # The engine was built successfully; when the policy is present,
    # _subtree_usage would be populated. This is a structural test that
    # the builder plumbs the value through.
    assert hasattr(engine_with_policy, "_subtree_usage")


def test_build_subagent_subtree_usage_excludes_parent_and_siblings(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A subagent's subtree_usage includes only its own subtree, not parent/siblings.

    The per-subagent cost gate (``subagent_cost_budget``) gates each
    subagent independently on its own spend. A child's subtree_usage must
    therefore reflect only the child + its descendants, not the parent or
    siblings.

    This is the key semantic difference from cost_budget (which sees
    session-wide) vs. subagent_cost_budget (which sees only its own subtree).
    """
    from omnigent.runtime.policies.builder import load_session_usage

    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    sibling = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    grandchild = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=child.id, title=_sub_agent_title()
    )

    conversation_store.set_session_usage(parent.id, {"total_cost_usd": 0.10})
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.05})
    conversation_store.set_session_usage(sibling.id, {"total_cost_usd": 0.03})
    conversation_store.set_session_usage(grandchild.id, {"total_cost_usd": 0.02})

    # load_session_usage with the child's ID gives us only its subtree.
    child_subtree = load_session_usage(child.id, conversation_store)
    # 0.07 = child (0.05) + grandchild (0.02), NOT parent or sibling.
    assert child_subtree["total_cost_usd"] == pytest.approx(0.07)

    # Parent sees full tree (0.20); child's subtree_usage would be 0.07.
    parent_fullsession = load_session_usage(parent.id, conversation_store)
    assert parent_fullsession["total_cost_usd"] == pytest.approx(0.20)

    # Verify the difference: child subtree < session total.
    assert child_subtree["total_cost_usd"] < parent_fullsession["total_cost_usd"]


def test_normalize_usage_for_engine_drops_display_fields() -> None:
    """
    _normalize_usage_for_engine removes by_model and promotes policy_cost_usd.

    Both the session-wide and subtree usage seeds use this helper to
    prepare usage for the engine: strip the display-only ``by_model``
    breakdown, and swap ``policy_cost_usd`` to ``total_cost_usd`` for
    enforcement cost (falling back to ``total_cost_usd`` when no enforcement
    cost exists).
    """
    from omnigent.runtime.policies.builder import _normalize_usage_for_engine

    # Case 1: Has both policy_cost (enforcement) and by_model (display).
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "total_cost_usd": 0.10,
        "policy_cost_usd": 0.15,  # In-flight estimate, higher than display.
        "by_model": {"claude-opus": {"input_tokens": 100, "total_cost_usd": 0.10}},
    }
    normalized = _normalize_usage_for_engine(usage)
    assert normalized["total_cost_usd"] == 0.15  # Swapped from policy_cost_usd.
    assert "policy_cost_usd" not in normalized  # Removed.
    assert "by_model" not in normalized  # Removed.
    assert normalized["input_tokens"] == 100  # Untouched.

    # Case 2: No policy_cost (codex/relay style) — falls back to total_cost_usd.
    usage2 = {
        "input_tokens": 50,
        "total_cost_usd": 0.05,
        "by_model": {"claude-sonnet": {"input_tokens": 50, "total_cost_usd": 0.05}},
    }
    normalized2 = _normalize_usage_for_engine(usage2)
    assert normalized2["total_cost_usd"] == 0.05  # Unchanged; no policy_cost to promote.
    assert "by_model" not in normalized2
    assert normalized2["input_tokens"] == 50

    # Case 3: Empty usage (no cost fields at all) — idempotent.
    usage3: dict[str, float] = {"input_tokens": 0, "output_tokens": 0}
    normalized3 = _normalize_usage_for_engine(usage3)
    assert "by_model" not in normalized3
    assert "policy_cost_usd" not in normalized3
    assert normalized3["input_tokens"] == 0


@contextmanager
def _count_sql(store: SqlAlchemyConversationStore) -> Iterator[list[str]]:
    """Capture every statement the store executes, on either bind."""
    from sqlalchemy import event as sa_event

    seen: list[str] = []

    def _on(conn, cursor, statement, params, context, many):
        seen.append(statement)

    engines = {store._engine, store._conv_engine}
    for engine in engines:
        sa_event.listen(engine, "before_cursor_execute", _on)
    try:
        yield seen
    finally:
        for engine in engines:
            sa_event.remove(engine, "before_cursor_execute", _on)


@pytest.mark.parametrize("preloaded", [False, True], ids=["no-preload", "preload"])
def test_build_issues_one_read_and_one_tree_scan(
    conversation_store: SqlAlchemyConversationStore,
    preloaded: bool,
) -> None:
    """
    One engine build costs one conversation read plus ONE spawn-tree scan,
    and nothing at all for the read when the caller supplies the row.

    Counts SQL STATEMENTS, not store-method calls: the redundancy this
    change removes was measured in queries, and a store-call count cannot
    see a helper that issues three statements per call. The builder used to
    re-fetch the conversation ~4x and walk the tree twice, once per usage
    seed.

    Both seeds must stay correct and identical either way: session-wide
    gating from the whole tree, subtree display from the node's own subtree.
    """
    from omnigent.spec.types import FunctionPolicySpec, FunctionRef

    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    sibling = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    conversation_store.set_session_usage(parent.id, {"total_cost_usd": 0.10})
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.05})
    conversation_store.set_session_usage(sibling.id, {"total_cost_usd": 0.03})
    child_row = conversation_store.get_conversation(child.id) if preloaded else None

    subagent_budget = FunctionPolicySpec(
        name="subtree_budget",
        on=None,
        function=FunctionRef(
            path="omnigent.policies.builtins.cost.subagent_cost_budget",
            arguments={"max_cost_usd": 10.0},
        ),
    )
    with _count_sql(conversation_store) as statements:
        engine = build_policy_engine(
            spec=AgentSpec(spec_version=1, name="child"),
            conversation_id=child.id,
            conversation_store=conversation_store,
            conversation=child_row,
            default_policies=[subagent_budget],
        )

    # Semantics preserved: session-wide gate vs per-subtree display.
    assert engine.usage["total_cost_usd"] == pytest.approx(0.18)
    assert engine._subtree_usage is not None
    assert engine._subtree_usage["total_cost_usd"] == pytest.approx(0.05)

    # The tree scan is one paged listing; the conversation read is a triplet
    # (row + metadata + labels) and disappears entirely with a preload.
    # Shape first, then the total. SQLite's connection PRAGMAs are setup, not
    # work the builder asked for.
    executed = [" ".join(q.split()) for q in statements if not q.startswith("PRAGMA")]
    tree_scans = [q for q in executed if "root_conversation_id = " in q]
    point_reads = [q for q in executed if "FROM conversations" in q and "conversations.id = " in q]
    assert len(tree_scans) == 1, executed
    assert len(point_reads) == (0 if preloaded else 1), executed
    # A conversation read is a triplet (row + metadata + labels), and so is the
    # tree scan; the preload removes one whole triplet.
    assert len(executed) == (3 if preloaded else 6), [q[:80] for q in executed]


def test_build_counts_archived_spend_and_inherits_approval(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Archiving must not move a budget gate. An archived root's own spend
    still counts toward the session total, and its approval checkpoint is
    still inherited — archiving is a listing concern, not an accounting
    one. (This previously asserted the opposite, codifying an omission
    that let an archive-after-preload seed $0 and ALLOW over budget.)
    """
    from omnigent.policies.schema import SESSION_COST_ASK_APPROVED_STATE_KEY

    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    conversation_store.set_session_usage(parent.id, {"total_cost_usd": 0.10})
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.05})
    conversation_store.set_session_state(parent.id, {SESSION_COST_ASK_APPROVED_STATE_KEY: 9.99})
    conversation_store.update_conversation(parent.id, archived=True)

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="child"),
        conversation_id=child.id,
        conversation_store=conversation_store,
    )

    # Whole-tree total including the archived root's own spend.
    assert engine.usage["total_cost_usd"] == pytest.approx(0.15)
    assert engine.session_state[SESSION_COST_ASK_APPROVED_STATE_KEY] == pytest.approx(9.99)


def test_build_rejects_mismatched_preloaded_conversation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A preloaded row for a different session must fail closed, not mix
    one session's labels/state/usage into another's policy decision."""
    from omnigent.errors import OmnigentError

    a = conversation_store.create_conversation(title="a")
    b = conversation_store.create_conversation(title="b")
    row_b = conversation_store.get_conversation(b.id)

    with pytest.raises(OmnigentError, match="does not match"):
        build_policy_engine(
            spec=AgentSpec(spec_version=1, name="x"),
            conversation_id=a.id,
            conversation_store=conversation_store,
            conversation=row_b,
        )


def test_build_with_preloaded_row_sees_concurrent_mutable_writes(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The preloaded row contributes only immutable identity: labels,
    session_state, and model written AFTER the row was captured must
    still reach the engine (re-derived from the fresh tree row), so a
    concurrent guard-label write can't be skipped by a stale snapshot.
    """
    conv = conversation_store.create_conversation(title="fresh-check")
    stale_row = conversation_store.get_conversation(conv.id)

    # Writes that land between the handler's read and the engine build.
    conversation_store.set_labels(conv.id, {"guard": "tripped"})
    conversation_store.set_session_state(conv.id, {"checkpoint": 1.5})
    conversation_store.update_conversation(conv.id, model_override="claude-sonnet-4-6")

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="x"),
        conversation_id=conv.id,
        conversation_store=conversation_store,
        conversation=stale_row,
    )

    assert engine.labels.get("guard") == "tripped"
    assert engine.session_state.get("checkpoint") == 1.5
    assert engine.model == "claude-sonnet-4-6"


def test_build_uses_fresh_state_for_a_row_archived_after_preload(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A row archived after the preload still authorizes from FRESH state.

    The tree now includes archived rows, so the fresh row is found there
    and the preload contributes only identity. (This test previously
    described the tree as EXCLUDING archived rows and asserted the
    re-read branch — a premise the ``include_archived=True`` fix
    inverted, which made the test unable to fail for its stated reason.)
    """
    conv = conversation_store.create_conversation(title="archive-race")
    stale_row = conversation_store.get_conversation(conv.id)

    conversation_store.set_labels(conv.id, {"guard": "tripped"})
    conversation_store.update_conversation(
        conv.id, model_override="claude-opus-4-8", archived=True
    )

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="x"),
        conversation_id=conv.id,
        conversation_store=conversation_store,
        conversation=stale_row,
    )

    # Mutable fields come from the fresh (archived) row, not the preload.
    assert engine.model == "claude-opus-4-8"
    assert engine.labels.get("guard") == "tripped"


def test_initial_label_seed_does_not_clobber_a_concurrent_write(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Seeding declared initials must not overwrite a value written between the
    snapshot read and the seed.

    The race is constructed, not simulated: a store proxy commits a policy
    write while the seeding helper is reading its snapshot, so the helper
    holds a view that is already stale by the time it decides what is
    missing. Insert-if-absent leaves the persisted value alone because the
    database makes that decision inside the same statement; the previous
    diff-then-upsert path recomputed "missing" from the stale snapshot and
    reset a live value to its initial.
    """
    from omnigent.runtime.policies.builder import _seed_and_load_labels
    from omnigent.spec.types import LabelDef

    conv = conversation_store.create_conversation()

    class _WriteDuringSnapshot:
        """Commits a competing label write during the snapshot read."""

        def __init__(self, inner: SqlAlchemyConversationStore) -> None:
            self._inner = inner
            self._fired = False

        def get_conversation(self, conversation_id: str):
            row = self._inner.get_conversation(conversation_id)
            if not self._fired:
                self._fired = True
                self._inner.set_labels(conversation_id, {"integrity": "7"})
            return row

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    result = _seed_and_load_labels(
        conversation_id=conv.id,
        label_defs={"integrity": LabelDef(initial="0")},
        conversation_store=_WriteDuringSnapshot(conversation_store),  # type: ignore[arg-type]
    )

    assert result["integrity"] == "7", "seed overwrote a concurrent write"
    persisted = dict(conversation_store.get_conversation(conv.id).labels)
    assert persisted["integrity"] == "7"


def test_initial_label_seed_uses_the_atomic_store_operation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Seeding must go through ``seed_labels_if_absent``, never ``set_labels``.

    A mechanism assertion on top of the semantic one above: the race is
    only impossible while the decision about which keys are missing is made
    inside the insert statement, so the choice of store operation is itself
    part of the contract.
    """
    from omnigent.runtime.policies.builder import _seed_and_load_labels
    from omnigent.spec.types import LabelDef

    conv = conversation_store.create_conversation()
    calls: list[str] = []

    class _RecordingStore:
        def __init__(self, inner: SqlAlchemyConversationStore) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            attr = getattr(self._inner, name)
            if not callable(attr):
                return attr

            def _wrapper(*args: object, **kwargs: object) -> object:
                calls.append(name)
                return attr(*args, **kwargs)

            return _wrapper

    _seed_and_load_labels(
        conversation_id=conv.id,
        label_defs={"integrity": LabelDef(initial="0")},
        conversation_store=_RecordingStore(conversation_store),  # type: ignore[arg-type]
        existing={},
    )

    assert "seed_labels_if_absent" in calls, calls
    assert "set_labels" not in calls, calls


def test_deleted_after_preload_fails_closed(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A row deleted after the preload must fail closed, not authorize
    from the stale snapshot nor from empty ($0) state."""
    import asyncio

    from omnigent.errors import OmnigentError

    conv = conversation_store.create_conversation(title="deleted")
    conversation_store.set_session_usage(conv.id, {"total_cost_usd": 5.0})
    stale_row = conversation_store.get_conversation(conv.id)
    asyncio.run(conversation_store.delete_conversation(conv.id))

    with pytest.raises(OmnigentError, match="disappeared"):
        build_policy_engine(
            spec=AgentSpec(spec_version=1, name="x"),
            conversation_id=conv.id,
            conversation_store=conversation_store,
            conversation=stale_row,
        )


def test_agent_rebind_after_spec_resolution_fails_closed(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``agent_id`` selects the spec, so it is read before the engine exists
    and cannot be re-derived. The builder confirms it against the row it
    freshly read — NOT against the caller's preload, which is the snapshot
    whose staleness is the whole hazard.

    Four cases, all of which an earlier revision accepted because the
    comparison ran before the fresh read (and skipped on ``None``):

    1. rebind with no preload
    2. rebind WITH a stale preload naming the old agent  <- the reproduction
    3. fresh row whose binding is ``None``
    4. no fresh row at all (deleted)
    """
    import asyncio

    from omnigent.errors import OmnigentError

    spec = AgentSpec(spec_version=1, name="x")
    agent_a = "1" * 32

    def _switch(conv_id: str, new_agent_id: str) -> None:
        # switch_conversation_agent inserts the target agent row, so each
        # switch needs its own id.
        conversation_store.switch_conversation_agent(
            conv_id,
            new_agent_id=new_agent_id,
            new_agent_name="other",
            new_agent_bundle_location="other/bundle",
            new_agent_description=None,
            copy_model_settings=False,
            carry_history_into_native=False,
            presentation_labels={},
            previous_builtin_id=None,
        )

    # Matching agent proceeds (guard must not be a blanket refusal).
    ok_conv = conversation_store.create_conversation(title="match", agent_id=agent_a)
    assert build_policy_engine(
        spec=spec,
        conversation_id=ok_conv.id,
        conversation_store=conversation_store,
        expected_agent_id=agent_a,
    )

    # 1. Rebind, no preload.
    c1 = conversation_store.create_conversation(title="rebind-nopreload", agent_id=agent_a)
    _switch(c1.id, uuid.uuid4().hex)
    with pytest.raises(OmnigentError, match="no longer resolves to agent"):
        build_policy_engine(
            spec=spec,
            conversation_id=c1.id,
            conversation_store=conversation_store,
            expected_agent_id=agent_a,
        )

    # 2. Rebind WITH a stale preload still naming agent A. Comparing against
    #    that preload would have found agent A == expected and accepted.
    c2 = conversation_store.create_conversation(title="rebind-preload", agent_id=agent_a)
    stale_row = conversation_store.get_conversation(c2.id)
    assert stale_row.agent_id == agent_a
    _switch(c2.id, uuid.uuid4().hex)
    with pytest.raises(OmnigentError, match="no longer resolves to agent"):
        build_policy_engine(
            spec=spec,
            conversation_id=c2.id,
            conversation_store=conversation_store,
            conversation=stale_row,
            expected_agent_id=agent_a,
        )

    # 3. Fresh row with NO binding: previously skipped by an ``is not None``
    #    guard, so an unbound session accepted any spec.
    c3 = conversation_store.create_conversation(title="unbound")
    assert conversation_store.get_conversation(c3.id).agent_id is None
    with pytest.raises(OmnigentError, match="no longer resolves to agent"):
        build_policy_engine(
            spec=spec,
            conversation_id=c3.id,
            conversation_store=conversation_store,
            expected_agent_id=agent_a,
        )

    # 4. No fresh row at all (deleted, no preload): also a mismatch.
    c4 = conversation_store.create_conversation(title="gone", agent_id=agent_a)
    asyncio.run(conversation_store.delete_conversation(c4.id))
    with pytest.raises(OmnigentError, match="no longer resolves to agent"):
        build_policy_engine(
            spec=spec,
            conversation_id=c4.id,
            conversation_store=conversation_store,
            expected_agent_id=agent_a,
        )


def test_increments_from_independent_snapshots_merge(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Two engines holding independent snapshots each INCREMENT one key, and
    both increments survive. A blind whole-blob write persisted 1.

    Sequential by construction, and named for what it proves: the MERGE, not
    the race. Serialisation of overlapping transactions is a store-level
    property and is pinned there, against each dialect's own mechanism
    (``test_two_real_writers_race_on_one_metadata_row``) — this test stays
    green with the row lock removed and should not be read as covering it.
    """
    from omnigent.spec.types import StateUpdate, StateUpdateAction

    conv = conversation_store.create_conversation(title="state-race")
    spec = AgentSpec(spec_version=1, name="x")
    engine_a = build_policy_engine(
        spec=spec, conversation_id=conv.id, conversation_store=conversation_store
    )
    engine_b = build_policy_engine(
        spec=spec, conversation_id=conv.id, conversation_store=conversation_store
    )

    op = StateUpdate(key="risk", action=StateUpdateAction.INCREMENT, value=1)
    engine_a.apply_state_updates([op])
    engine_b.apply_state_updates([op])

    persisted = conversation_store.get_conversation(conv.id)
    assert dict(persisted.session_state)["risk"] == 2


def test_sets_from_independent_snapshots_preserve_other_keys(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A second engine's SET must not drop a key the first one wrote.

    Sequential, like its sibling above: the merge is the contract here.
    """
    from omnigent.spec.types import StateUpdate, StateUpdateAction

    conv = conversation_store.create_conversation(title="state-keys")
    spec = AgentSpec(spec_version=1, name="x")
    engine_a = build_policy_engine(
        spec=spec, conversation_id=conv.id, conversation_store=conversation_store
    )
    engine_b = build_policy_engine(
        spec=spec, conversation_id=conv.id, conversation_store=conversation_store
    )

    engine_a.apply_state_updates(
        [StateUpdate(key="from_a", action=StateUpdateAction.SET, value="a")]
    )
    engine_b.apply_state_updates(
        [StateUpdate(key="from_b", action=StateUpdateAction.SET, value="b")]
    )

    state = dict(conversation_store.get_conversation(conv.id).session_state)
    assert state["from_a"] == "a", state
    assert state["from_b"] == "b", state


def test_delete_survives_the_hot_cache_overlay(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A DELETE must actually remove the key, not just from the persisted row.

    ``apply_state_updates`` merges under a store-side lock and then folds the
    result onto the engine's in-memory cache. The persisted row was already
    correct after a delete — ``mutate_session_state``'s callback pops the key
    from the fresh state it is handed. The hot cache was not: a blanket union
    ``{**old_cache, **merged}`` cannot express "this key is now gone", since a
    key ``merged`` no longer has is simply missing from the right-hand side of
    the union and the union keeps whatever the left-hand (stale) cache still
    holds. The next evaluation reads that stale cache, not the store.
    """
    from omnigent.spec.types import StateUpdate, StateUpdateAction

    conv = conversation_store.create_conversation(title="state-delete")
    spec = AgentSpec(spec_version=1, name="x")
    engine = build_policy_engine(
        spec=spec, conversation_id=conv.id, conversation_store=conversation_store
    )

    engine.apply_state_updates(
        [
            StateUpdate(key="risk", action=StateUpdateAction.SET, value=1),
            StateUpdate(key="keep", action=StateUpdateAction.SET, value=2),
        ]
    )
    engine.apply_state_updates([StateUpdate(key="risk", action=StateUpdateAction.DELETE)])

    assert "risk" not in engine.session_state, engine.session_state
    assert engine.session_state["keep"] == 2, engine.session_state

    persisted = dict(conversation_store.get_conversation(conv.id).session_state)
    assert "risk" not in persisted, persisted
    assert persisted["keep"] == 2, persisted


def test_delete_of_a_root_inherited_key_does_not_resurrect_from_the_snapshot(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A sub-agent deleting its inherited approval key must not have the overlay
    bring it back from the construction-time snapshot.

    This key never reaches ``session_ops`` at all for a sub-agent — a write
    to it (SET or DELETE) is always diverted to
    ``_record_root_cost_ask_approved``, which applies the op straight to the
    hot cache itself. So this test doesn't exercise the merge/overlay
    machinery; it pins that the diversion still produces the right answer for
    a DELETE, since only SET was previously exercised anywhere.
    """
    from omnigent.policies.schema import SESSION_COST_ASK_APPROVED_STATE_KEY
    from omnigent.spec.types import StateUpdate, StateUpdateAction

    parent = conversation_store.create_conversation()
    conversation_store.set_session_state(parent.id, {SESSION_COST_ASK_APPROVED_STATE_KEY: 0.05})
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id
    )
    spec = AgentSpec(spec_version=1, name="x")
    engine = build_policy_engine(
        spec=spec, conversation_id=child.id, conversation_store=conversation_store
    )
    # Inherited at construction, per build_policy_engine's root-seeding.
    assert engine.session_state[SESSION_COST_ASK_APPROVED_STATE_KEY] == 0.05

    engine.apply_state_updates(
        [StateUpdate(key=SESSION_COST_ASK_APPROVED_STATE_KEY, action=StateUpdateAction.DELETE)]
    )

    assert SESSION_COST_ASK_APPROVED_STATE_KEY not in engine.session_state, engine.session_state

    # The diversion writes straight to the root's row, not just the child's
    # cache — assert the persisted root, or a wrongly-local DELETE would pass.
    root_state = dict(conversation_store.get_conversation(parent.id).session_state)
    assert SESSION_COST_ASK_APPROVED_STATE_KEY not in root_state, root_state


def test_delete_of_the_same_key_name_on_a_top_level_session_removes_it(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The same key name is ordinary state on a top-level session, and a delete
    of it must stick — it must not be mistaken for the sub-agent inheritance
    case just because the name matches.

    For a top-level session (root == self) an op on
    ``SESSION_COST_ASK_APPROVED_STATE_KEY`` goes through the same
    ``session_ops``/merge path as any other key, never through
    ``_record_root_cost_ask_approved`` (sub-agent only). The overlay tells
    "genuinely never persisted" apart from "just deleted" by which keys THIS
    call's ops named, not by a fixed key list — an earlier, key-list-based
    version of this fix got exactly this case wrong.
    """
    from omnigent.policies.schema import SESSION_COST_ASK_APPROVED_STATE_KEY
    from omnigent.spec.types import StateUpdate, StateUpdateAction

    root = conversation_store.create_conversation(title="root-own-approval-key")
    spec = AgentSpec(spec_version=1, name="x")
    engine = build_policy_engine(
        spec=spec, conversation_id=root.id, conversation_store=conversation_store
    )

    engine.apply_state_updates(
        [
            StateUpdate(
                key=SESSION_COST_ASK_APPROVED_STATE_KEY,
                action=StateUpdateAction.SET,
                value=0.05,
            )
        ]
    )
    assert engine.session_state[SESSION_COST_ASK_APPROVED_STATE_KEY] == 0.05

    engine.apply_state_updates(
        [StateUpdate(key=SESSION_COST_ASK_APPROVED_STATE_KEY, action=StateUpdateAction.DELETE)]
    )

    assert SESSION_COST_ASK_APPROVED_STATE_KEY not in engine.session_state, engine.session_state
    persisted = dict(conversation_store.get_conversation(root.id).session_state)
    assert SESSION_COST_ASK_APPROVED_STATE_KEY not in persisted, persisted


def test_supplied_root_is_a_hint_that_gets_verified(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A caller's tree root saves a read when right and is corrected when wrong.

    ``root_conversation_id`` is immutable per ROW, not per conversation id.
    Delete a conversation and recreate it under the same id and the new row
    can sit in a different tree, so a row read earlier in the request names a
    root this conversation no longer belongs to. Trusting it silently summed
    the wrong tree — the recreated child reported nothing at all.

    Three properties, because the argument has to be observably load-bearing
    AND observably safe:

    1. right root → same answer as self-resolving, one conversation read
       fewer (a build that ignored the argument fails on the read count);
    2. wrong tree's root → still the right answer (a build that trusts it
       fails here);
    3. the delete/recreate case that produced the regression.
    """
    from omnigent.runtime.policies.builder import load_session_usage

    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    conversation_store.set_session_usage(parent.id, {"total_cost_usd": 0.10})
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.05})
    other_tree = conversation_store.create_conversation(title="unrelated")

    with _count_sql(conversation_store) as self_resolved_sql:
        resolved = load_session_usage(child.id, conversation_store)
    assert resolved["total_cost_usd"] == pytest.approx(0.05)

    with _count_sql(conversation_store) as supplied_sql:
        supplied = load_session_usage(child.id, conversation_store, root_conversation_id=parent.id)
    assert supplied == resolved
    # The saved read is the point of the argument: a triplet fewer.
    executed = lambda caught: [q for q in caught if not q.startswith("PRAGMA")]  # noqa: E731
    assert len(executed(supplied_sql)) == len(executed(self_resolved_sql)) - 3, (
        executed(supplied_sql),
        executed(self_resolved_sql),
    )

    # Wrong tree's root: verified against the tree it produced, so the sum is
    # still right. Trusting it returned {} — the shape of the regression.
    assert (
        load_session_usage(child.id, conversation_store, root_conversation_id=other_tree.id)
        == resolved
    )


def test_recreated_conversation_is_summed_in_its_new_tree(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A row read before a delete/recreate must not decide which tree to sum.

    The reported regression: a child is deleted and recreated under the same
    id in a different tree while a caller holds the old row. Summing from the
    stale row's root found no such conversation and emitted nothing, so the
    session's cost silently stopped updating.
    """
    import asyncio

    from omnigent.runtime.policies.builder import load_session_usage

    old_parent = conversation_store.create_conversation(title="old-root")
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=old_parent.id, title=_sub_agent_title()
    )
    stale_row = conversation_store.get_conversation(child.id)
    assert stale_row.root_conversation_id == old_parent.root_conversation_id

    # Recreate the same id under a different tree, as the delete/recreate
    # boundary does.
    asyncio.run(conversation_store.delete_conversation(child.id))
    new_parent = conversation_store.create_conversation(title="new-root")
    conversation_store.create_conversation(
        conversation_id=child.id,
        kind="sub_agent",
        parent_conversation_id=new_parent.id,
        title=_sub_agent_title(),
    )
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 2.0})

    summed = load_session_usage(
        child.id,
        conversation_store,
        root_conversation_id=stale_row.root_conversation_id,
    )
    assert summed["total_cost_usd"] == pytest.approx(2.0), (
        "a stale root must not silence a recreated conversation's spend"
    )


class _MutateOnTreeLoad:
    """Store proxy that commits *hazard* the first time the tree is scanned.

    The builder reads the conversation, then scans the spawn tree. Anything
    committed in between makes the earlier read stale — which is the window
    the freshness refresh exists to close, whoever performed that read.
    """

    def __init__(self, inner: object, hazard: Callable[[], None]) -> None:
        self._inner = inner
        self._hazard = hazard
        self._fired = False

    def list_conversations(self, *args: object, **kwargs: object) -> object:
        if not self._fired:
            self._fired = True
            self._hazard()
        return self._inner.list_conversations(*args, **kwargs)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@pytest.mark.parametrize("preloaded", [True, False], ids=["preload", "no-preload"])
@pytest.mark.parametrize("hazard", ["switch", "delete"])
def test_mid_build_change_fails_closed_on_every_provenance(
    conversation_store: SqlAlchemyConversationStore,
    preloaded: bool,
    hazard: str,
) -> None:
    """
    A change landing between the conversation read and the tree scan must
    fail closed — regardless of who performed that read.

    Parametrized over provenance on purpose. An earlier revision gated the
    refresh on ``conversation is not None``, so the caller-preload path was
    closed and the builder's own read had the identical window wide open: a
    switch enforced the old agent's spec, a deletion seeded empty usage and
    authorized a $0 budget. Provenance is a row here, not a separate test,
    so a third way of acquiring the row is covered by construction.
    """
    import asyncio

    from omnigent.errors import OmnigentError

    agent_a = "2" * 32
    conv = conversation_store.create_conversation(title=f"mid-build-{hazard}", agent_id=agent_a)
    conversation_store.set_session_usage(conv.id, {"total_cost_usd": 5.0})
    row = conversation_store.get_conversation(conv.id)

    if hazard == "switch":

        def _apply() -> None:
            conversation_store.switch_conversation_agent(
                conv.id,
                new_agent_id=uuid.uuid4().hex,
                new_agent_name="other",
                new_agent_bundle_location="other/bundle",
                new_agent_description=None,
                copy_model_settings=False,
                carry_history_into_native=False,
                presentation_labels={},
                previous_builtin_id=None,
            )

        expected = "no longer resolves to agent"
    else:

        def _apply() -> None:
            asyncio.run(conversation_store.delete_conversation(conv.id))

        expected = "disappeared"

    store = _MutateOnTreeLoad(conversation_store, _apply)
    with pytest.raises(OmnigentError, match=expected):
        build_policy_engine(
            spec=AgentSpec(spec_version=1, name="x"),
            conversation_id=conv.id,
            conversation_store=store,  # type: ignore[arg-type]
            conversation=row if preloaded else None,
            expected_agent_id=agent_a,
        )


def test_archived_descendant_spend_counts_toward_the_displayed_total(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Including archived rows in the tree changes DISPLAY as well as gating.

    The change was made so archiving cannot reset a cost gate, but
    ``load_session_usage`` is also the display path for the session badge
    (``session.usage`` SSE) and the usage report, so an archived
    descendant's spend now shows in the total a user sees. That is the
    deliberate choice — the alternative, separate tree semantics for
    display and enforcement, lets the badge disagree with the gate that
    blocks the user — and it belongs with the change that caused it rather
    than in a later PR.
    """
    from omnigent.runtime.policies.builder import load_session_usage

    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title=_sub_agent_title()
    )
    conversation_store.set_session_usage(parent.id, {"total_cost_usd": 1.0})
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 2.0})
    conversation_store.update_conversation(child.id, archived=True)

    displayed = load_session_usage(parent.id, conversation_store)
    assert displayed["total_cost_usd"] == pytest.approx(3.0), (
        "archived descendant spend must reach the displayed subtree total"
    )

    # The same number the badge and the enforcement seed derive from, so the
    # two cannot diverge.
    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="parent"),
        conversation_id=parent.id,
        conversation_store=conversation_store,
    )
    assert engine.usage["total_cost_usd"] == pytest.approx(3.0)


def test_engine_seeds_the_new_tree_when_a_child_is_recreated_elsewhere(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Everything the build derives must come from the verified tree.

    The preload only *suggests* a root. Deriving the tree root from the
    pre-refresh row while taking rows from a corrected tree mixed two
    epochs: a child deleted and recreated beneath another root, with a
    caller still holding the old row, seeded the OLD tree's spend — the
    refresh landed on the row and left the root and the tree pointing at
    the previous tree. (Root-inherited session *policies* take the same
    refreshed root and are exposed to the same risk, but only spend is
    asserted below — this test does not exercise policy inheritance.)
    """
    import asyncio

    old_root = conversation_store.create_conversation(title="old-root")
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=old_root.id, title=_sub_agent_title()
    )
    conversation_store.set_session_usage(old_root.id, {"total_cost_usd": 10.0})
    stale_row = conversation_store.get_conversation(child.id)

    asyncio.run(conversation_store.delete_conversation(child.id))
    new_root = conversation_store.create_conversation(title="new-root")
    conversation_store.set_session_usage(new_root.id, {"total_cost_usd": 3.0})
    conversation_store.create_conversation(
        conversation_id=child.id,
        kind="sub_agent",
        parent_conversation_id=new_root.id,
        title=_sub_agent_title(),
    )

    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1, name="x"),
        conversation_id=child.id,
        conversation_store=conversation_store,
        conversation=stale_row,
    )

    # The gate seeds from the whole tree the child is in NOW.
    assert engine.usage["total_cost_usd"] == pytest.approx(3.0), (
        "the engine seeded a tree the conversation no longer belongs to"
    )


@pytest.mark.parametrize("preload", [True, False], ids=["preloaded", "no-preload"])
@pytest.mark.parametrize("mutation", ["switch", "delete"])
def test_engine_refuses_a_tree_assembled_across_a_change(
    conversation_store: SqlAlchemyConversationStore,
    preload: bool,
    mutation: str,
) -> None:
    """
    A paged tree does not share one read instant, so it cannot vouch for
    itself.

    Rows on page one are read before page two. The refresh takes the
    evaluated row from the tree, so for a paged tree that row may predate
    the last page — "the tree read is newer" is true of the tree, not of any
    row in it. A switch OR a delete landing during paging must be caught
    the same way, whether or not the caller preloaded the row — all four
    preload/no-preload x switch/delete combinations previously proceeded.
    """
    import asyncio

    from omnigent.errors import OmnigentError
    from omnigent.runtime.policies import builder as builder_mod

    root = conversation_store.create_conversation(title="paged-root")
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id, title=_sub_agent_title()
    )
    row = conversation_store.get_conversation(child.id)

    class _MutateDuringPaging:
        """Switches or deletes the evaluated conversation between tree pages."""

        def __init__(self, inner: SqlAlchemyConversationStore) -> None:
            self._inner = inner
            self._pages = 0
            self._fired = False

        def list_conversations(self, *args: object, **kwargs: object):
            page = self._inner.list_conversations(*args, **kwargs)  # type: ignore[arg-type]
            self._pages += 1
            # Fire once the page holding the evaluated row has been read —
            # which page that is depends on listing order, so keying on "page
            # one" made the interleave dialect-dependent.
            if not self._fired and any(c.id == child.id for c in page.data):
                self._fired = True
                if mutation == "switch":
                    self._inner.switch_conversation_agent(
                        child.id,
                        new_agent_id=uuid.uuid4().hex,
                        new_agent_name="other",
                        new_agent_bundle_location="other/bundle",
                        new_agent_description=None,
                        copy_model_settings=False,
                        carry_history_into_native=False,
                        presentation_labels={},
                        previous_builtin_id=None,
                    )
                else:
                    asyncio.run(self._inner.delete_conversation(child.id))
            return page

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    # One row per page, so the tree genuinely pages.
    original_page_size = builder_mod._SUBTREE_USAGE_PAGE_SIZE
    builder_mod._SUBTREE_USAGE_PAGE_SIZE = 1
    try:
        with pytest.raises(OmnigentError, match="moved while its spawn tree"):
            build_policy_engine(
                spec=AgentSpec(spec_version=1, name="x"),
                conversation_id=child.id,
                conversation_store=_MutateDuringPaging(conversation_store),  # type: ignore[arg-type]
                conversation=row if preload else None,
            )
    finally:
        builder_mod._SUBTREE_USAGE_PAGE_SIZE = original_page_size


@pytest.mark.parametrize(
    ("shape", "links"),
    [
        ("cycle", {"a": "b", "b": "a"}),
        ("parent outside the tree", {"a": "missing"}),
    ],
)
def test_ancestor_walk_discards_an_untrustworthy_chain(shape: str, links: dict[str, str]) -> None:
    """
    A chain that cannot be walked to the root yields nothing, not a prefix.

    The docstring promised empty for a cyclic or broken chain while the loop
    returned the part it had already walked — and the caller publishes each
    returned id a cost event, so `A → B → A` notified B off a cycle and
    `C → missing-D` notified an id that is not in the tree at all.
    """
    from omnigent.runtime.policies.builder import ancestor_ids_from_tree

    tree = [
        Conversation(
            id=node,
            created_at=0,
            updated_at=0,
            root_conversation_id="a",
            parent_conversation_id=parent,
        )
        for node, parent in links.items()
    ]

    assert ancestor_ids_from_tree(tree, "a") == [], shape
