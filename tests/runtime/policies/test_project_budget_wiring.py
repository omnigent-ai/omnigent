"""Tests for automatic project monthly budget policy synthesis and wiring.

A project's monthly budget (``Project.budget_config``) has no entry in any
agent's YAML, session policy store, or server-wide ``default_policies`` — so
unlike every other policy source ``build_policy_engine`` merges,
:func:`_load_project_budget_policy_specs` synthesizes the
``project_monthly_cost_budget`` spec on the fly from the project's stored
config (see ``PLAN.md``, closes #1662). These tests exercise that synthesis,
the context injection it depends on, and the ``any_policies_apply`` fast-path
gap it closes — through the real ``build_policy_engine`` → ``PolicyEngine``
pipeline, not the callable in isolation (that's
``tests/policies/builtins/test_project_monthly_cost.py``).
"""

from __future__ import annotations

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.builder import any_policies_apply, build_policy_engine
from omnigent.spec.types import AgentSpec, LLMConfig, Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

_PROJECT_POLICY_NAME = "__project_monthly_cost_budget"
_SPEC = AgentSpec(spec_version=1, name="test-agent", llm=LLMConfig(model="gpt-4"))


@pytest.fixture()
def project_store(db_uri: str) -> SqlAlchemyProjectStore:
    """A :class:`SqlAlchemyProjectStore` backed by the same per-test SQLite DB
    as the ``conversation_store`` fixture (see ``tests/runtime/policies/conftest.py``)."""
    return SqlAlchemyProjectStore(db_uri)


def _request_ctx() -> EvaluationContext:
    return EvaluationContext(phase=Phase.REQUEST, content="hello", tool_name=None)


# ── Synthesis: does a budgeted project get the policy at all? ──────────────


def test_project_with_budget_synthesizes_policy(
    conversation_store: SqlAlchemyConversationStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A project with a positive ``limit_usd`` gets the policy automatically —
    no agent-spec / session-policy / default_policies declaration needed."""
    project = project_store.create(
        "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "budgeted",
        "alice@example.com",
        budget_config={"limit_usd": 10.0},
    )
    conv = conversation_store.create_conversation()
    assert conversation_store.set_conversation_project(conv.id, project.id)

    engine = build_policy_engine(
        spec=_SPEC,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        project_store=project_store,
    )
    assert _PROJECT_POLICY_NAME in [p.spec.name for p in engine.policies]


def test_project_without_budget_synthesizes_nothing(
    conversation_store: SqlAlchemyConversationStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A project with no configured budget never gets the policy."""
    project = project_store.create(
        "b1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", "unbudgeted", "alice@example.com"
    )
    conv = conversation_store.create_conversation()
    assert conversation_store.set_conversation_project(conv.id, project.id)

    engine = build_policy_engine(
        spec=_SPEC,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        project_store=project_store,
    )
    assert _PROJECT_POLICY_NAME not in [p.spec.name for p in engine.policies]


def test_unfiled_session_synthesizes_nothing(
    conversation_store: SqlAlchemyConversationStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A session with no ``project_id`` gets no project budget policy, even
    when *some* project somewhere has one configured."""
    project_store.create(
        "c1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "someone else's budget",
        "alice@example.com",
        budget_config={"limit_usd": 10.0},
    )
    conv = conversation_store.create_conversation()  # never filed

    engine = build_policy_engine(
        spec=_SPEC,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        project_store=project_store,
    )
    assert _PROJECT_POLICY_NAME not in [p.spec.name for p in engine.policies]


def test_no_project_store_synthesizes_nothing(
    conversation_store: SqlAlchemyConversationStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """``project_store=None`` (the default) skips synthesis entirely, even
    for a session filed into a budgeted project — deployments that don't wire
    a project store see no behavior change."""
    project = project_store.create(
        "d1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "budgeted",
        "alice@example.com",
        budget_config={"limit_usd": 10.0},
    )
    conv = conversation_store.create_conversation()
    assert conversation_store.set_conversation_project(conv.id, project.id)

    engine = build_policy_engine(
        spec=_SPEC,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        # project_store omitted.
    )
    assert _PROJECT_POLICY_NAME not in [p.spec.name for p in engine.policies]


def test_subagent_synthesizes_from_root_project(
    conversation_store: SqlAlchemyConversationStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A sub-agent conversation (no ``project_id`` of its own) still gets the
    root session's project budget policy via the root fallback."""
    project = project_store.create(
        "e1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "budgeted",
        "alice@example.com",
        budget_config={"limit_usd": 10.0},
    )
    parent = conversation_store.create_conversation()
    assert conversation_store.set_conversation_project(parent.id, project.id)
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id
    )
    assert conversation_store.get_conversation(child.id).project_id is None  # sanity

    engine = build_policy_engine(
        spec=_SPEC,
        conversation_id=child.id,
        conversation_store=conversation_store,
        project_store=project_store,
    )
    assert _PROJECT_POLICY_NAME in [p.spec.name for p in engine.policies]


# ── End-to-end enforcement through the real engine ──────────────────────────


@pytest.mark.asyncio
async def test_under_budget_allows(
    conversation_store: SqlAlchemyConversationStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    project = project_store.create(
        "f1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "p",
        "alice@example.com",
        budget_config={"limit_usd": 10.0, "ask_thresholds_usd": [5.0]},
    )
    conv = conversation_store.create_conversation()
    assert conversation_store.set_conversation_project(conv.id, project.id)
    conversation_store.add_project_monthly_cost(project.id, _this_month(), 2.0)

    engine = build_policy_engine(
        spec=_SPEC,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        project_store=project_store,
    )
    result = await engine.evaluate(_request_ctx())
    assert result.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_crossing_threshold_asks(
    conversation_store: SqlAlchemyConversationStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    project = project_store.create(
        "01b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "p",
        "alice@example.com",
        budget_config={"limit_usd": 10.0, "ask_thresholds_usd": [5.0]},
    )
    conv = conversation_store.create_conversation()
    assert conversation_store.set_conversation_project(conv.id, project.id)
    conversation_store.add_project_monthly_cost(project.id, _this_month(), 6.0)

    engine = build_policy_engine(
        spec=_SPEC,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        project_store=project_store,
    )
    result = await engine.evaluate(_request_ctx())
    assert result.action == PolicyAction.ASK


@pytest.mark.asyncio
async def test_over_limit_denies(
    conversation_store: SqlAlchemyConversationStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    project = project_store.create(
        "11b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "p",
        "alice@example.com",
        budget_config={"limit_usd": 10.0},
    )
    conv = conversation_store.create_conversation()
    assert conversation_store.set_conversation_project(conv.id, project.id)
    conversation_store.add_project_monthly_cost(project.id, _this_month(), 12.0)

    engine = build_policy_engine(
        spec=_SPEC,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        project_store=project_store,
    )
    result = await engine.evaluate(_request_ctx())
    assert result.action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_owner_raising_limit_clears_deny_on_next_build(
    conversation_store: SqlAlchemyConversationStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """The synthesized policy re-reads ``budget_config`` on every
    ``build_policy_engine`` call — no separate "retry" mechanism is needed
    for a limit raise to take effect; the very next engine build sees it."""
    project = project_store.create(
        "21b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "p",
        "alice@example.com",
        budget_config={"limit_usd": 10.0},
    )
    conv = conversation_store.create_conversation()
    assert conversation_store.set_conversation_project(conv.id, project.id)
    conversation_store.add_project_monthly_cost(project.id, _this_month(), 12.0)

    engine = build_policy_engine(
        spec=_SPEC,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        project_store=project_store,
    )
    assert (await engine.evaluate(_request_ctx())).action == PolicyAction.DENY

    project_store.update(
        project.id, owner_user_id="alice@example.com", budget_config={"limit_usd": 20.0}
    )
    rebuilt = build_policy_engine(
        spec=_SPEC,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        project_store=project_store,
    )
    assert (await rebuilt.evaluate(_request_ctx())).action == PolicyAction.ALLOW


# ── any_policies_apply fast-path (the gap this feature closed) ─────────────


def test_any_policies_apply_true_for_budgeted_project_with_no_other_policies(
    conversation_store: SqlAlchemyConversationStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A session with NO agent guardrails / session policies / server
    defaults, filed into a budgeted project, must still report
    ``any_policies_apply() == True`` — otherwise the native-hook fast path
    (``routes_hooks.py``) would skip the engine build and the project's
    budget would silently never be enforced for that session.
    """
    project = project_store.create(
        "31b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "p",
        "alice@example.com",
        budget_config={"limit_usd": 10.0},
    )
    conv = conversation_store.create_conversation()
    assert conversation_store.set_conversation_project(conv.id, project.id)

    assert (
        any_policies_apply(
            spec=_SPEC,
            conversation_id=conv.id,
            default_policies=None,
            policy_store=None,
            conversation_store=conversation_store,
            project_store=project_store,
        )
        is True
    )


def test_any_policies_apply_false_without_conversation_store(
    conversation_store: SqlAlchemyConversationStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Omitting ``conversation_store`` (a caller that can't check project
    budgets) falls back to the pre-existing checks only — no crash, just no
    project-budget fast path (the full engine build still catches it later)."""
    project = project_store.create(
        "41b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "p",
        "alice@example.com",
        budget_config={"limit_usd": 10.0},
    )
    conv = conversation_store.create_conversation()
    assert conversation_store.set_conversation_project(conv.id, project.id)

    assert (
        any_policies_apply(
            spec=_SPEC,
            conversation_id=conv.id,
            default_policies=None,
            policy_store=None,
        )
        is False
    )


def _this_month() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m")
