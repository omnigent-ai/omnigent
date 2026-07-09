"""Tests for session policy loading in :func:`build_policy_engine`.

Verifies that enabled session policies stored via the CRUD API are
loaded by the builder, converted to :class:`FunctionPolicySpec`,
resolved to :class:`FunctionPolicy` instances, and participate in
engine evaluation alongside spec-declared policies.
"""

from __future__ import annotations

import pytest

from omnigent.entities import Policy as StoredPolicy
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.policies.function import FunctionPolicy
from omnigent.runtime.policies.builder import (
    _load_session_policy_specs,
    _stored_policy_to_spec,
    build_policy_engine,
)
from omnigent.spec.types import (
    AgentSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

# ── _stored_policy_to_spec ──────────────────────────────────────────────────


def test_stored_python_policy_to_spec() -> None:
    """A stored ``type="python"`` policy converts to a FunctionPolicySpec.

    The FunctionRef must carry the handler as ``path`` and
    ``factory_params`` as ``arguments``. ``on`` must be ``None``
    so the engine skips phase filtering (callable self-selects).
    """
    stored = StoredPolicy(
        id="pol_abc",
        name="rate_limit",
        session_id="conv_123",
        scope="session",
        created_at=1000,
        type="python",
        handler="myorg.policies.rate_limit",
        factory_params={"limit": 10},
    )
    spec = _stored_policy_to_spec(stored)

    assert spec is not None
    assert isinstance(spec, FunctionPolicySpec)
    assert spec.name == "rate_limit"
    assert spec.on is None
    assert spec.function is not None
    assert spec.function.path == "myorg.policies.rate_limit"
    assert spec.function.arguments == {"limit": 10}


def test_stored_python_policy_without_factory_params() -> None:
    """A stored Python policy with no factory_params gets ``arguments=None``."""
    stored = StoredPolicy(
        id="pol_def",
        name="simple",
        session_id="conv_123",
        scope="session",
        created_at=1000,
        type="python",
        handler="myorg.policies.simple_check",
    )
    spec = _stored_policy_to_spec(stored)

    assert spec is not None
    assert isinstance(spec, FunctionPolicySpec)
    assert spec.function is not None
    assert spec.function.arguments is None


def test_stored_url_policy_raises() -> None:
    """A stored ``type="url"`` policy is rejected loudly, not skipped.

    URL policy evaluation is unimplemented; converting one must raise
    rather than silently return ``None`` (which would let an operator
    store a guardrail that never enforces).
    """
    stored = StoredPolicy(
        id="pol_url",
        name="external",
        session_id="conv_123",
        scope="session",
        created_at=1000,
        type="url",
        handler="https://example.com/eval",
    )
    with pytest.raises(OmnigentError) as excinfo:
        _stored_policy_to_spec(stored)
    assert excinfo.value.code == ErrorCode.INVALID_INPUT
    assert "url" in str(excinfo.value)
    assert "external" in str(excinfo.value)


# ── _load_session_policy_specs ──────────────────────────────────────────────


def test_load_session_policy_specs_none_store() -> None:
    """When ``policy_store`` is ``None``, returns an empty list."""
    assert _load_session_policy_specs("conv_123", None) == []


def test_load_session_policy_specs_filters_disabled(db_uri: str) -> None:
    """Disabled policies are excluded from the loaded specs.

    :param db_uri: Per-test SQLite URI from the root conftest.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    store = SqlAlchemyPolicyStore(db_uri)
    store.create(
        policy_id="pol_enabled",
        session_id=conv.id,
        name="enabled_policy",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    store.create(
        policy_id="pol_disabled",
        session_id=conv.id,
        name="disabled_policy",
        type="python",
        handler="myorg.policies.deny_all",
        enabled=False,
    )

    specs = _load_session_policy_specs(conv.id, store)

    assert len(specs) == 1
    assert specs[0].name == "enabled_policy"


def test_load_session_policy_specs_rejects_enabled_url(db_uri: str) -> None:
    """An enabled url-type session policy raises at load time (fail closed).

    :param db_uri: Per-test SQLite URI from the root conftest.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    store = SqlAlchemyPolicyStore(db_uri)
    store.create(
        policy_id="pol_url",
        session_id=conv.id,
        name="external",
        type="url",
        handler="https://example.com/eval",
        enabled=True,
    )

    with pytest.raises(OmnigentError) as excinfo:
        _load_session_policy_specs(conv.id, store)
    assert excinfo.value.code == ErrorCode.INVALID_INPUT


# ── build_policy_engine integration ─────────────────────────────────────────


def _make_minimal_spec() -> AgentSpec:
    """Build a minimal AgentSpec with no guardrails.

    :returns: An :class:`AgentSpec` with all required fields set to
        minimal values and no guardrails.
    """
    return AgentSpec(
        spec_version=1,
        name="test-agent",
    )


def test_build_engine_includes_session_policies(db_uri: str) -> None:
    """Session policies from the store appear in the engine's policy list.

    Creates a session policy pointing at a test callable, builds the
    engine, and verifies the callable was resolved into a FunctionPolicy.

    :param db_uri: Per-test SQLite URI.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create(
        policy_id="pol_test",
        session_id=conv.id,
        name="test_policy",
        type="python",
        # Point at a real callable in the test resources.
        handler="tests.resources.examples._shared.tool_functions.block_long_sleep",
    )

    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id=conv.id,
        conversation_store=conv_store,
        policy_store=policy_store,
    )

    assert isinstance(engine.policies[0], FunctionPolicy)
    assert engine.policies[0].spec.name == "test_policy"
    assert engine.policies[-1].spec.name == "__ask_on_add_policy"


def test_build_engine_no_store_returns_noop(db_uri: str) -> None:
    """Without a policy store, the engine has no policies (noop).

    :param db_uri: Per-test SQLite URI.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)

    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id="conv_nonexistent",
        conversation_store=conv_store,
        policy_store=None,
    )

    # No user-declared policies, but ask_on_add_policy is always present.
    assert len(engine.policies) == 1
    assert engine.policies[0].spec.name == "__ask_on_add_policy"


def test_build_engine_ordering_session_agent_admin(db_uri: str) -> None:
    """Policy evaluation order is session → agent → admin.

    Creates one policy at each layer and verifies their position
    in the engine's policy list matches the documented contract.

    :param db_uri: Per-test SQLite URI.
    """
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    # Agent-declared policy via spec guardrails.
    agent_policy = FunctionPolicySpec(
        name="agent_policy",
        on=None,
        function=FunctionRef(path=handler),
    )
    spec = AgentSpec(
        spec_version=1,
        name="test-agent",
        guardrails=GuardrailsSpec(policies=[agent_policy]),
    )

    # Admin (server-wide default) policy.
    admin_policy = FunctionPolicySpec(
        name="admin_policy",
        on=None,
        function=FunctionRef(path=handler),
    )

    # Session policy from the store.
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create(
        policy_id="pol_session",
        session_id=conv.id,
        name="session_policy",
        type="python",
        handler=handler,
    )

    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conv_store,
        default_policies=[admin_policy],
        policy_store=policy_store,
    )

    names = [p.spec.name for p in engine.policies]
    assert names == [
        "session_policy",
        "agent_policy",
        "admin_policy",
        "__ask_on_add_policy",
    ]


# ── Sub-agent session policy inheritance ───────────────────────────────────


def test_subagent_inherits_root_session_policies(db_uri: str) -> None:
    """Session policies on the root conversation propagate to sub-agents.

    Creates a root conversation with a session policy, spawns a
    sub-agent (child conversation), and verifies that the child's
    policy engine includes the root's session policy.

    :param db_uri: Per-test SQLite URI.
    """
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    conv_store = SqlAlchemyConversationStore(db_uri)
    root_conv = conv_store.create_conversation()
    child_conv = conv_store.create_conversation(
        parent_conversation_id=root_conv.id,
        kind="sub_agent",
    )

    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create(
        policy_id="pol_root",
        session_id=root_conv.id,
        name="root_guard",
        type="python",
        handler=handler,
    )

    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id=child_conv.id,
        conversation_store=conv_store,
        policy_store=policy_store,
    )

    names = [p.spec.name for p in engine.policies]
    assert "root_guard" in names, f"root session policy not inherited by sub-agent; got {names}"
    # Root policy should come before the ask_on_add_policy sentinel.
    assert names.index("root_guard") < names.index("__ask_on_add_policy")


def test_subagent_deduplicates_same_name_policy(db_uri: str) -> None:
    """When root and child both have a policy with the same name, child wins.

    The root's copy is dropped to avoid double-evaluation. The
    child's version appears in the engine at the session-policy
    position.

    :param db_uri: Per-test SQLite URI.
    """
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    conv_store = SqlAlchemyConversationStore(db_uri)
    root_conv = conv_store.create_conversation()
    child_conv = conv_store.create_conversation(
        parent_conversation_id=root_conv.id,
        kind="sub_agent",
    )

    policy_store = SqlAlchemyPolicyStore(db_uri)
    # Same-name policy on both root and child.
    policy_store.create(
        policy_id="pol_root",
        session_id=root_conv.id,
        name="shared_guard",
        type="python",
        handler=handler,
    )
    policy_store.create(
        policy_id="pol_child",
        session_id=child_conv.id,
        name="shared_guard",
        type="python",
        handler=handler,
    )

    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id=child_conv.id,
        conversation_store=conv_store,
        policy_store=policy_store,
    )

    names = [p.spec.name for p in engine.policies]
    # "shared_guard" should appear exactly once (child's version).
    assert names.count("shared_guard") == 1, (
        f"expected exactly 1 'shared_guard', got {names.count('shared_guard')} in {names}"
    )


def test_root_session_does_not_double_load(db_uri: str) -> None:
    """A root conversation (no parent) loads its own policies once.

    Ensures the root-inheritance path is a no-op when the
    conversation is already the root (``root_conversation_id == id``).

    :param db_uri: Per-test SQLite URI.
    """
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    conv_store = SqlAlchemyConversationStore(db_uri)
    root_conv = conv_store.create_conversation()

    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create(
        policy_id="pol_root",
        session_id=root_conv.id,
        name="root_only",
        type="python",
        handler=handler,
    )

    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id=root_conv.id,
        conversation_store=conv_store,
        policy_store=policy_store,
    )

    names = [p.spec.name for p in engine.policies]
    assert names.count("root_only") == 1, (
        f"root policy loaded {names.count('root_only')} times in {names}"
    )
