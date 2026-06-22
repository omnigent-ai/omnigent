"""
Tests for :class:`FunctionPolicy` (Phase 4).

Ports and extends these omnigent cases:

From ``test_policies.py``:
- ``test_allow_by_default`` — empty FunctionPolicy → ALLOW
- ``test_sync_callable_block`` — sync DENY via callable
- ``test_sync_callable_allow`` — sync ALLOW via lambda
- ``test_async_callable`` — async def evaluator
- ``test_callable_returns_dict`` — dict return parses
- ``test_deny_action_from_dict`` — string 'deny' in dict
- ``test_tool_call_rate_limit`` — closure rate-limit policy

From ``test_labels_and_policies.py`` (FunctionPolicy-context):
- ``test_three_arg_callable_receives_context``
- ``test_three_arg_callable_reads_labels_for_decision``
- ``test_three_arg_async_callable``
- ``test_rate_limit_counter_isolated``
- ``test_zero_arg_factory_copy_creates_fresh_state``

Plus Phase 4 carve-outs:
- Exception → DENY (fail-closed)
- Exception with classifier-only action list → ALLOW substituted
- Action whitelist validation
- set_labels whitelist filtering
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from omnigent.policies.function import (
    FunctionPolicy,
    resolve_function_policy,
)
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    FunctionPolicySpec,
    FunctionRef,
    Phase,
    PhaseSelector,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.runtime.policies.conftest import make_fixed_policy


def _install_module(tmp_path: Path, module_name: str, source: str) -> None:
    """
    Write a Python module into a tmp dir and make it importable.

    Used by tests that need to exercise
    ``resolve_function_policy`` — the real code path that
    production YAMLs go through.
    """
    pkg_dir = tmp_path / "test_fn_policy_pkg"
    pkg_dir.mkdir(exist_ok=True)
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / f"{module_name}.py").write_text(textwrap.dedent(source))
    sys.path.insert(0, str(tmp_path))


@pytest.fixture(autouse=True)
def _cleanup_sys_path(tmp_path: Path) -> None:
    """
    Remove any tmp-path entries we inserted after each test.

    Without this, successive tests could pick up a stale
    module with the same name from a previous test's tmp_path.
    """
    yield
    path_str = str(tmp_path)
    while path_str in sys.path:
        sys.path.remove(path_str)
    # Drop the cached package so re-use of the name in
    # another test (with different source) is a clean import.
    for mod_name in list(sys.modules):
        if mod_name.startswith("test_fn_policy_pkg"):
            del sys.modules[mod_name]


def _spec(
    *,
    name: str = "p",
    phase: Phase = Phase.REQUEST,
    tool_name: str | None = None,
    function: FunctionRef | None = None,
    action: list[PolicyAction] | None = None,
    set_labels: list[str] | None = None,
) -> FunctionPolicySpec:
    """Build a FunctionPolicySpec with sensible defaults."""
    return FunctionPolicySpec(
        name=name,
        on=[PhaseSelector(phase=phase, tool_name=tool_name)],
        function=function or FunctionRef(path="test_fn_policy_pkg.probe.noop"),
        action=action,
        set_labels=set_labels,
    )


def _build_engine(
    store: SqlAlchemyConversationStore,
    policies: list,
    *,
    initial_labels: dict[str, str] | None = None,
) -> PolicyEngine:
    """Build a PolicyEngine + fresh conversation for tests."""
    conv = store.create_conversation()
    return PolicyEngine(
        policies=policies,
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels=initial_labels or {},
        conversation_store=store,
    )


# ── Direct FunctionPolicy (no dotted-path resolution) ──


@pytest.mark.asyncio
async def test_sync_callable_allow() -> None:
    """Ports omnigent ``test_sync_callable_allow``. A sync
    lambda that returns PolicyResult(ALLOW) produces ALLOW."""

    def fn(event: dict) -> PolicyResult:
        return PolicyResult(action=PolicyAction.ALLOW)

    policy = FunctionPolicy(_spec(), fn)
    result = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="hi"),
        {"labels": {}, "conversation_id": "c"},
    )
    assert result.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_sync_callable_block() -> None:
    """Ports omnigent ``test_sync_callable_block``. A sync
    function that returns DENY blocks."""

    def fn(event: dict) -> PolicyResult:
        if isinstance(event["data"], str) and "badword" in event["data"]:
            return PolicyResult(action=PolicyAction.DENY, reason="Profanity")
        return PolicyResult(action=PolicyAction.ALLOW)

    policy = FunctionPolicy(_spec(), fn)
    result = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="has badword here"),
        {"labels": {}, "conversation_id": "c"},
    )
    assert result.action == PolicyAction.DENY
    assert result.reason == "Profanity"


@pytest.mark.asyncio
async def test_async_callable() -> None:
    """Ports omnigent ``test_async_callable``. An async
    def evaluator works identically to sync."""

    async def fn(event: dict) -> PolicyResult:
        return PolicyResult(action=PolicyAction.ALLOW)

    policy = FunctionPolicy(_spec(), fn)
    result = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="x"),
        {"labels": {}, "conversation_id": "c"},
    )
    assert result.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_callable_returns_dict_allow() -> None:
    """Ports omnigent ``test_callable_returns_dict``. A
    V0 dict return with string result parses into PolicyResult."""
    policy = FunctionPolicy(
        _spec(),
        lambda event: {"result": "ALLOW"},
    )
    result = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="x"),
        {"labels": {}, "conversation_id": "c"},
    )
    assert result.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_callable_returns_dict_deny_with_reason() -> None:
    """Ports omnigent ``test_deny_action_from_dict``. A
    V0 dict return with explicit deny and reason."""
    policy = FunctionPolicy(
        _spec(),
        lambda event: {"result": "DENY", "reason": "policy says no"},
    )
    result = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="x"),
        {"labels": {}, "conversation_id": "c"},
    )
    assert result.action == PolicyAction.DENY
    assert result.reason == "policy says no"


@pytest.mark.asyncio
async def test_callable_returns_dict_with_set_labels() -> None:
    """A callable may return a PolicyResult with set_labels.
    Verifies the PolicyResult coercion path doesn't drop the
    label writes. (V0 dict output doesn't include set_labels;
    callables that need to write labels return PolicyResult
    directly.)"""
    policy = FunctionPolicy(
        _spec(),
        lambda event: PolicyResult(
            action=PolicyAction.ALLOW,
            set_labels={"integrity": "0"},
        ),
    )
    result = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="x"),
        {"labels": {}, "conversation_id": "c"},
    )
    assert result.action == PolicyAction.ALLOW
    assert result.set_labels == {"integrity": "0"}


@pytest.mark.asyncio
async def test_callable_returns_foreign_policy_result_shape() -> None:
    """
    A PolicyResult-shaped object from a different module parses
    cleanly instead of failing with the tautological
    "returned unsupported type PolicyResult; expected
    PolicyResult" error.

    Claim: the coercion path treats any object with
    ``.action``/``.reason``/``.set_labels`` attributes as a
    foreign PolicyResult and routes it through the dict
    coercion, regardless of its class identity. This pins the
    regression reported against
    ``examples/rate_limit_policy.py``, which imports
    ``PolicyResult`` from ``omnigent.policies`` — a different
    module than the engine's ``omnigent.policies.types`` so
    ``isinstance`` fails.

    Uses a local stand-in dataclass so the test doesn't
    depend on whether omnigent is installed in this
    environment. The failure signature would be:
    ``PolicyDecisionError: FunctionPolicy 'p' failed:
    FunctionPolicy 'p' returned unsupported type
    _ForeignPolicyResult; expected PolicyResult or dict``.
    """
    import enum
    from dataclasses import dataclass

    class _ForeignAction(enum.Enum):
        """
        Mimics ``omnigent.policies.PolicyAction`` — wire
        values match Omnigent', but the enum class is
        distinct so ``isinstance(x, PolicyAction)`` fails.
        """

        ALLOW = "allow"
        DENY = "deny"

    @dataclass
    class _ForeignPolicyResult:
        """
        Mimics ``omnigent.policies.PolicyResult`` — same
        attributes, different class identity.
        """

        action: _ForeignAction
        reason: str | None = None
        set_labels: dict[str, str] | None = None

    policy = FunctionPolicy(
        _spec(),
        lambda event: _ForeignPolicyResult(
            action=_ForeignAction.DENY,
            reason="quota exhausted",
        ),
    )
    result = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="x"),
        {"labels": {}, "conversation_id": "c"},
    )
    # DENY (action coerced via the enum's ``.value`` — a plain
    # ``str(_ForeignAction.DENY)`` would give
    # ``"_ForeignAction.DENY"`` which ``PolicyAction(...)``
    # rejects.
    assert result.action == PolicyAction.DENY
    # Reason passes through unchanged so the deny message
    # reaches the user (otherwise ``[Denied by policy: ]``
    # would lose the 'why' context).
    assert result.reason == "quota exhausted"


@pytest.mark.asyncio
async def test_two_arg_callable_receives_config() -> None:
    """Ports omnigent
    ``test_three_arg_callable_receives_context`` (ours is 2-arg
    because we fold content+phase into the V0 event dict). Under
    the V0 contract the second arg is the spec's static ``config``
    dict, NOT the engine's runtime context bundle."""
    captured: dict[str, Any] = {}

    def fn(event: dict, config: dict[str, Any]) -> PolicyResult:
        captured.update(config)
        return PolicyResult(action=PolicyAction.ALLOW)

    # Build a spec with a config block so there is something to receive.
    spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test_fn_policy_pkg.probe.noop"),
        config={"threshold": "5", "mode": "strict"},
    )
    policy = FunctionPolicy(spec, fn)
    await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="x"),
        {"labels": {"integrity": "1"}, "conversation_id": "conv_42"},
    )
    # The callable observed the spec's static config, not the
    # engine's runtime label/conversation bundle.
    assert captured == {"threshold": "5", "mode": "strict"}


@pytest.mark.asyncio
async def test_two_arg_callable_reads_config_for_decision(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Ports omnigent ``test_three_arg_callable_reads_labels_for_decision``.
    Under V0, labels are NOT passed to the callable — decisions
    that once depended on the runtime label state should instead
    use the spec's static ``config`` thresholds. This test verifies
    that config values steer the decision and that label state is
    visible via ``engine.labels`` after evaluation."""

    def fn(event: dict, config: dict[str, Any]) -> PolicyResult:
        # Decision driven by static config, not runtime labels.
        if config.get("mode") == "strict":
            return PolicyResult(action=PolicyAction.DENY, reason="strict mode")
        return PolicyResult(action=PolicyAction.ALLOW)

    strict_spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test_fn_policy_pkg.probe.noop"),
        config={"mode": "strict"},
    )
    permissive_spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test_fn_policy_pkg.probe.noop"),
        config={"mode": "permissive"},
    )

    strict_policy = FunctionPolicy(strict_spec, fn)
    permissive_policy = FunctionPolicy(permissive_spec, fn)

    ctx = EvaluationContext(phase=Phase.REQUEST, content="x")
    engine_ctx = {"labels": {}, "conversation_id": "c"}

    denied = await strict_policy.evaluate(ctx, engine_ctx)
    assert denied.action == PolicyAction.DENY

    allowed = await permissive_policy.evaluate(ctx, engine_ctx)
    assert allowed.action == PolicyAction.ALLOW

    # Separately verify labels ARE visible on the engine after a
    # label-writing policy runs — they just aren't piped through
    # the callable's config arg.
    label_policy = make_fixed_policy(
        name="taint",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ALLOW,
        set_labels={"integrity": "0"},
    )
    engine = _build_engine(conversation_store, [label_policy])
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert engine.labels["integrity"] == "0"


@pytest.mark.asyncio
async def test_async_two_arg_callable() -> None:
    """Ports omnigent ``test_three_arg_async_callable``.
    Async two-arg callables receive the spec's static config as
    the second argument. Verifies async dispatch works correctly
    for the two-arg V0 signature."""

    async def fn(event: dict, config: dict[str, Any]) -> PolicyResult:
        if config.get("blocked") == "1":
            return PolicyResult(action=PolicyAction.DENY, reason="blocked")
        return PolicyResult(action=PolicyAction.ALLOW)

    spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test_fn_policy_pkg.probe.noop"),
        config={"blocked": "1"},
    )
    policy = FunctionPolicy(spec, fn)
    r = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="x"),
        {"labels": {}, "conversation_id": "c"},
    )
    assert r.action == PolicyAction.DENY


# ── Rate-limit closure (the load-bearing §9.1 example) ─


@pytest.mark.asyncio
async def test_rate_limit_closure_counts() -> None:
    """Ports omnigent ``test_tool_call_rate_limit``. A
    closure counter ticks across evaluations in the same
    workflow. Without this, stateful FunctionPolicies are
    useless."""

    def rate_limit_search(limit: int = 3) -> Any:
        calls = 0

        def _eval(event: dict) -> PolicyResult:
            nonlocal calls
            calls += 1
            if calls > limit:
                return PolicyResult(
                    action=PolicyAction.DENY,
                    reason=f"Rate limit {limit} exceeded",
                )
            return PolicyResult(action=PolicyAction.ALLOW)

        return _eval

    policy = FunctionPolicy(
        _spec(phase=Phase.TOOL_CALL, tool_name="web_search"),
        rate_limit_search(limit=3),
    )
    # First 3 calls ALLOW.
    for _ in range(3):
        r = await policy.evaluate(
            EvaluationContext(
                phase=Phase.TOOL_CALL,
                content={"tool": "web"},
                tool_name="web_search",
            ),
            {"labels": {}, "conversation_id": "c"},
        )
        assert r.action == PolicyAction.ALLOW
    # 4th denies.
    r = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"tool": "web"},
            tool_name="web_search",
        ),
        {"labels": {}, "conversation_id": "c"},
    )
    assert r.action == PolicyAction.DENY


# ── Factory resolution (the dict-form YAML path) ───────


def test_resolve_function_policy_short_form(tmp_path: Path) -> None:
    """Short-form: `function: module.attr` → the attr IS
    the evaluator."""
    _install_module(
        tmp_path,
        "probe",
        """
        from omnigent.policies.types import PolicyResult

        from omnigent.spec.types import PolicyAction

        def noop(event):
            return PolicyResult(action=PolicyAction.ALLOW)
        """,
    )
    spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test_fn_policy_pkg.probe.noop"),
    )
    policy = resolve_function_policy(spec)
    # The policy is an instance, spec bound, callable ready.
    assert isinstance(policy, FunctionPolicy)
    assert policy.spec is spec


def test_resolve_function_policy_factory_form(tmp_path: Path) -> None:
    """Dict-form: `function: {path, arguments}` → path is a
    factory. The factory runs once at build time, returning
    the evaluator. Closure state is per-workflow."""
    _install_module(
        tmp_path,
        "probe_factory",
        """
        from omnigent.policies.types import PolicyResult

        from omnigent.spec.types import PolicyAction

        def make(limit):
            calls = 0
            def _eval(event):
                nonlocal calls
                calls += 1
                if calls > limit:
                    return PolicyResult(action=PolicyAction.DENY)
                return PolicyResult(action=PolicyAction.ALLOW)
            return _eval
        """,
    )
    spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(
            path="test_fn_policy_pkg.probe_factory.make",
            arguments={"limit": 2},
        ),
    )
    policy = resolve_function_policy(spec)
    assert isinstance(policy, FunctionPolicy)


def test_resolve_function_policy_empty_arguments_invokes_factory(
    tmp_path: Path,
) -> None:
    """``arguments={}`` invokes the factory with no kwargs (defaults).

    Before the ``is not None`` fix, ``{}`` was falsy and the factory
    was used as the evaluator directly — calling it with ``(event)``
    returned an inner function instead of a verdict. If this
    regresses, factory policies stored with ``factory_params={}``
    (the shape the web UI sends) would fail at evaluation time.
    """
    _install_module(
        tmp_path,
        "probe_empty_args",
        """
        from omnigent.policies.types import PolicyResult
        from omnigent.spec.types import PolicyAction

        def make(limit=5):
            def _eval(event):
                return PolicyResult(action=PolicyAction.ALLOW)
            return _eval
        """,
    )
    spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(
            path="test_fn_policy_pkg.probe_empty_args.make",
            arguments={},
        ),
    )
    policy = resolve_function_policy(spec)
    assert isinstance(policy, FunctionPolicy), (
        "Empty arguments should invoke the factory with defaults. "
        "If this fails, {} is being treated as None (direct callable)."
    )


def test_resolve_function_policy_none_arguments_auto_detects_factory(
    tmp_path: Path,
) -> None:
    """``arguments=None`` auto-detects factories with all-default params.

    Legacy DB rows store ``factory_params=None``. The resolver
    inspects the signature: if every positional param has a default,
    it's a factory — call it with no args to produce the evaluator.
    Direct callables (required ``event`` param) are used as-is.
    """
    _install_module(
        tmp_path,
        "probe_auto",
        """
        from omnigent.policies.types import PolicyResult
        from omnigent.spec.types import PolicyAction

        def factory_all_defaults(limit=10, action="ALLOW"):
            def _eval(event):
                return PolicyResult(action=PolicyAction.ALLOW)
            return _eval

        def direct_callable(event):
            return PolicyResult(action=PolicyAction.ALLOW)
        """,
    )
    # Factory with all-default params: auto-detected and invoked.
    spec_factory = FunctionPolicySpec(
        name="f",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(
            path="test_fn_policy_pkg.probe_auto.factory_all_defaults",
            arguments=None,
        ),
    )
    policy_factory = resolve_function_policy(spec_factory)
    assert isinstance(policy_factory, FunctionPolicy), (
        "Factory with all-default params should be auto-detected when arguments=None."
    )

    # Direct callable: used as-is (not invoked as factory).
    spec_direct = FunctionPolicySpec(
        name="d",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(
            path="test_fn_policy_pkg.probe_auto.direct_callable",
            arguments=None,
        ),
    )
    policy_direct = resolve_function_policy(spec_direct)
    assert isinstance(policy_direct, FunctionPolicy), (
        "Direct callable should be used as-is when arguments=None."
    )


@pytest.mark.asyncio
async def test_factory_closure_counter_isolated_per_build(
    tmp_path: Path,
) -> None:
    """Ports omnigent ``test_rate_limit_counter_isolated``.
    Two separate FunctionPolicy builds from the same factory
    have independent closure state — if this regresses,
    rate limits for different agents (or different workflows
    of the same agent) would pool into one counter."""
    _install_module(
        tmp_path,
        "probe_iso",
        """
        from omnigent.policies.types import PolicyResult

        from omnigent.spec.types import PolicyAction

        def make(limit):
            calls = 0
            def _eval(event):
                nonlocal calls
                calls += 1
                if calls > limit:
                    return PolicyResult(action=PolicyAction.DENY)
                return PolicyResult(action=PolicyAction.ALLOW)
            return _eval
        """,
    )
    spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(
            path="test_fn_policy_pkg.probe_iso.make",
            arguments={"limit": 1},
        ),
    )
    policy_a = resolve_function_policy(spec)
    policy_b = resolve_function_policy(spec)

    ctx = EvaluationContext(phase=Phase.REQUEST, content="x")
    context = {"labels": {}, "conversation_id": "c"}

    # A: 1 ALLOW then DENY (limit=1).
    assert (await policy_a.evaluate(ctx, context)).action == PolicyAction.ALLOW
    assert (await policy_a.evaluate(ctx, context)).action == PolicyAction.DENY
    # B starts fresh — its first call is ALLOW even though
    # A has already exhausted its counter.
    assert (await policy_b.evaluate(ctx, context)).action == PolicyAction.ALLOW


# ── Engine-level FunctionPolicy dispatch ──────────────


@pytest.mark.asyncio
async def test_function_policy_exception_fails_closed_to_deny(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A callable that raises → engine coerces to DENY with
    the exception message in reason. Critical safety property
    — a broken callable must not silently ALLOW."""

    def fn(event: dict) -> PolicyResult:
        raise RuntimeError("crashed")

    policy = FunctionPolicy(_spec(), fn)
    engine = _build_engine(conversation_store, [policy])
    result = await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert result.action == PolicyAction.DENY
    # Reason contains both the policy name and the exception.
    assert "crashed" in result.reason
    assert "p" in result.reason  # policy name


@pytest.mark.asyncio
async def test_function_policy_exception_with_classifier_only_substitutes_allow(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """POLICIES.md §13 classifier-only carve-out: when the
    spec's action list contains no DENY, a raising callable
    becomes ALLOW instead of DENY. Honors the author's
    declared 'this policy never blocks' intent."""

    def fn(event: dict) -> PolicyResult:
        raise RuntimeError("crashed")

    policy = FunctionPolicy(_spec(action=[PolicyAction.ALLOW]), fn)
    engine = _build_engine(conversation_store, [policy])
    result = await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    # Engine substituted ALLOW because DENY is not in the
    # declared action list.
    assert result.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_function_policy_returns_action_outside_whitelist_fails_closed(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Callable returns ASK, but the spec declared only
    [allow, deny] — engine fail-closes to DENY."""

    def fn(event: dict) -> PolicyResult:
        return PolicyResult(action=PolicyAction.ASK, reason="uncertain")

    policy = FunctionPolicy(
        _spec(action=[PolicyAction.ALLOW, PolicyAction.DENY]),
        fn,
    )
    engine = _build_engine(conversation_store, [policy])
    result = await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert result.action == PolicyAction.DENY
    # Reason names the violation explicitly so operators can
    # debug the misbehaving callable.
    assert "not in its declared action list" in result.reason


@pytest.mark.asyncio
async def test_function_policy_set_labels_whitelist_drops_extras(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Spec declares `set_labels: [integrity]`; callable
    returns extra keys → engine filters them out silently
    (POLICIES.md §9.2 on the prompt-policy path but applies
    uniformly here per §4 step 5)."""

    def fn(event: dict) -> PolicyResult:
        return PolicyResult(
            action=PolicyAction.ALLOW,
            set_labels={"integrity": "0", "stealthy_key": "bad"},
        )

    policy = FunctionPolicy(_spec(set_labels=["integrity"]), fn)
    engine = _build_engine(conversation_store, [policy])
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    # Hot cache reflects only the whitelisted key.
    assert engine.labels == {"integrity": "0"}
    # Persisted state matches — the stealthy_key never
    # touched the store.
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {"integrity": "0"}


@pytest.mark.asyncio
async def test_function_policy_without_whitelist_writes_freely(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """When the spec does NOT declare `set_labels`, every
    key the callable writes lands (schemaless semantics,
    matches omnigent parity)."""

    def fn(event: dict) -> PolicyResult:
        return PolicyResult(
            action=PolicyAction.ALLOW,
            set_labels={"any": "value", "other": "thing"},
        )

    policy = FunctionPolicy(_spec(set_labels=None), fn)
    engine = _build_engine(conversation_store, [policy])
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert engine.labels == {"any": "value", "other": "thing"}


# ── Composition: FunctionPolicy + FunctionPolicy together ─


@pytest.mark.asyncio
async def test_function_and_label_policies_compose(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Mix a fixed policy (taint) and a FunctionPolicy
    (shell guard) across two evaluate() calls. Verifies:
    - FunctionPolicy.set_labels persists after turn 1 (visible
      via engine.labels)
    - FunctionPolicy can drive decisions from event data
    - DENY from FunctionPolicy names the deciding policy

    Under V0 the callable receives the event dict, not the
    runtime label state. The IFC pattern for label-driven
    decisions uses a second FunctionPolicy (or checks labels
    on the engine between turns); this test shows the
    FunctionPolicy half of the composition still works
    correctly when its decision is based on event content.

    This is the same pattern the secure_research_agent
    example uses — the Phase 4 e2e proxy."""
    taint = make_fixed_policy(
        name="taint_web",
        on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name="web_search")],
        action=PolicyAction.ALLOW,
        set_labels={"integrity": "0"},
    )

    # Shell guard denies any tool whose event target is
    # "run_shell" — its decision is driven by event["target"],
    # not runtime labels. Label state is verified separately
    # via engine.labels after each turn.
    def shell_guard(event: dict) -> PolicyResult:
        if event.get("target") == "run_shell":
            return PolicyResult(
                action=PolicyAction.DENY,
                reason="tainted state; shell disallowed",
            )
        return PolicyResult(action=PolicyAction.ALLOW)

    shell = FunctionPolicy(
        _spec(
            name="shell_guard",
            phase=Phase.TOOL_CALL,
            tool_name="run_shell",
        ),
        shell_guard,
    )
    engine = _build_engine(conversation_store, [taint, shell])

    # Turn 1: web_search taints integrity to 0.
    r1 = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"tool": "web"},
            tool_name="web_search",
        ),
    )
    assert r1.action == PolicyAction.ALLOW
    # Label taint is visible on the engine after turn 1.
    assert engine.labels["integrity"] == "0"

    # Turn 2: run_shell → shell_guard sees event["target"]
    # == "run_shell" and DENIES.
    r2 = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"tool": "sh"},
            tool_name="run_shell",
        ),
    )
    assert r2.action == PolicyAction.DENY
    assert r2.deciding_policy == "shell_guard"
    assert "tainted" in r2.reason


# ── reset_turn forwarding (fix #2) ───────────────────────────


def test_function_policy_reset_turn_invokes_callable_attribute(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``FunctionPolicy.reset_turn`` must look up ``reset_turn``
    on the wrapped callable and invoke it. This is how legacy
    omnigent policies like ``max_tool_calls_per_turn`` clear
    per-turn accumulators between turns — see
    :meth:`omnigent.inner.policies.FunctionPolicy.reset_turn`
    for the native implementation we mirror.

    What breaks if this fails: the rate-limit factory in
    ``examples/_shared/rate_limit_policy.py`` runs forever
    without ever resetting its counter, and a "15 per turn"
    config silently behaves as "15 per session".
    """
    del conversation_store  # Unused — engine isn't needed for this assertion.
    reset_calls: list[None] = []

    def evaluate(
        event: dict,
        config: dict[str, Any],
    ) -> PolicyResult:
        del event, config
        return PolicyResult(action=PolicyAction.ALLOW)

    def reset_turn() -> None:
        reset_calls.append(None)

    evaluate.reset_turn = reset_turn  # type: ignore[attr-defined]

    policy = FunctionPolicy(_spec(), evaluate)
    policy.reset_turn()
    policy.reset_turn()
    # Two explicit invocations → two underlying invocations.
    # Anything other than 2 indicates either a missed
    # delegation (0) or a duplicated call (>2).
    assert len(reset_calls) == 2


def test_function_policy_reset_turn_no_op_when_callable_lacks_attribute(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Stateless callables (no ``reset_turn`` attribute) must be
    a clean no-op — calling ``policy.reset_turn()`` cannot
    raise. The base-class ``Policy.reset_turn`` and the
    FunctionPolicy override both default to "do nothing" for
    plain callables.

    What breaks if this fails: any FunctionPolicy author who
    didn't attach a reset_turn (the common case) starts
    getting an AttributeError on every turn boundary.
    """
    del conversation_store

    def evaluate(
        event: dict,
        config: dict[str, Any],
    ) -> PolicyResult:
        del event, config
        return PolicyResult(action=PolicyAction.ALLOW)

    policy = FunctionPolicy(_spec(), evaluate)
    # Should not raise; should not require any attribute on
    # the callable.
    policy.reset_turn()


def test_engine_reset_turn_calls_every_policy(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``PolicyEngine.reset_turn`` must invoke ``reset_turn`` on
    every policy in YAML order, regardless of type. Verifies:

    - Stateless policies are called (no-op, but the call
      happens — pinned by counter on a recording subclass).
    - Stateful FunctionPolicies clear their underlying
      callable's accumulator.
    - FunctionPolicy entries (which have a default no-op
      ``reset_turn`` from the base class) don't raise.
    """
    fn_reset_calls: list[None] = []

    def fn_evaluate(
        event: dict,
        config: dict[str, Any],
    ) -> PolicyResult:
        del event, config
        return PolicyResult(action=PolicyAction.ALLOW)

    def fn_reset() -> None:
        fn_reset_calls.append(None)

    fn_evaluate.reset_turn = fn_reset  # type: ignore[attr-defined]
    fn_policy = FunctionPolicy(_spec(name="fn"), fn_evaluate)

    label_policy = make_fixed_policy(
        name="lp",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ALLOW,
        set_labels={"k": "v"},
    )

    engine = _build_engine(conversation_store, [fn_policy, label_policy])
    engine.reset_turn()
    # The function policy's underlying reset_turn ran exactly
    # once on this single engine.reset_turn() call. If 0,
    # delegation broke; if 2+, the engine called per-policy
    # reset more than once per invocation.
    assert len(fn_reset_calls) == 1


def test_engine_reset_turn_does_not_cross_engines(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Calling ``reset_turn`` on one engine MUST NOT reset
    state on a separate engine instance. Today both engines
    happen to be independent (each FunctionPolicy holds its
    own callable closure), but the test pins this isolation
    so a future refactor that introduces shared state across
    engines (e.g. process-global rate-limit counters) would
    fail loud here.
    """
    counters_a: list[None] = []
    counters_b: list[None] = []

    def make_callable(sink: list[None]) -> Any:
        def evaluate(
            event: dict,
            config: dict[str, Any],
        ) -> PolicyResult:
            del event, config
            return PolicyResult(action=PolicyAction.ALLOW)

        def reset() -> None:
            sink.append(None)

        evaluate.reset_turn = reset  # type: ignore[attr-defined]
        return evaluate

    pol_a = FunctionPolicy(_spec(name="a"), make_callable(counters_a))
    pol_b = FunctionPolicy(_spec(name="b"), make_callable(counters_b))

    engine_a = _build_engine(conversation_store, [pol_a])
    engine_b = _build_engine(conversation_store, [pol_b])

    engine_a.reset_turn()
    # Only A's counter advanced — B's engine wasn't touched.
    assert len(counters_a) == 1
    assert len(counters_b) == 0

    engine_b.reset_turn()
    # Now both have advanced exactly once each. Any other
    # numbers would mean reset_turn either skipped a policy
    # or leaked across engines.
    assert len(counters_a) == 1
    assert len(counters_b) == 1


# ── PolicyResult.data propagation ─────────────────────


@pytest.mark.asyncio
async def test_callable_dict_return_data_field_propagated() -> None:
    """A V0 dict return with a ``data`` field propagates to PolicyResult.data.

    The canonical use case: a PII-redaction policy returns
    ``{"result": "ALLOW", "data": <redacted-args>}``
    so the enforcement site can substitute the original content.
    A ``None`` value for ``data`` is the "no replacement" sentinel.
    """
    redacted = {"query": "<REDACTED>"}
    policy = FunctionPolicy(
        _spec(),
        lambda event: {"result": "ALLOW", "data": redacted},
    )
    result = await policy.evaluate(
        EvaluationContext(phase=Phase.TOOL_CALL, content={"query": "SSN 123-45-6789"}),
        {"labels": {}, "conversation_id": "c"},
    )
    assert result.action == PolicyAction.ALLOW
    assert result.data == redacted, (
        f"Policy data must be propagated to PolicyResult.data; "
        f"got {result.data!r}. If None, the dict parser dropped the 'data' key."
    )


@pytest.mark.asyncio
async def test_engine_propagates_data_to_composed_allow(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Engine-composed ALLOW carries the policy's ``data`` field.

    Covers the TOOL_CALL PII-redaction path end-to-end: a policy
    returns ``data`` with modified arguments, the engine composes to
    ALLOW, and the composed result carries the replacement.
    If this fails, the enforcement site would see ``result.data is None``
    and pass the original (un-redacted) arguments to the tool.
    """
    redacted = {"query": "<REDACTED>"}

    policy = FunctionPolicy(
        _spec(phase=Phase.TOOL_CALL),
        lambda event: PolicyResult(action=PolicyAction.ALLOW, data=redacted),
    )
    engine = _build_engine(conversation_store, [policy])
    result = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"query": "SSN 123-45-6789"},
            tool_name="search",
        )
    )
    assert result.action == PolicyAction.ALLOW
    assert result.data == redacted, (
        f"Engine-composed ALLOW must carry data from the policy; "
        f"got {result.data!r}. If None, the engine dropped data during composition."
    )


@pytest.mark.asyncio
async def test_engine_data_chains_sequentially_across_policies(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Each policy that returns ``data`` receives the previous
    policy's output as ``event["data"]`` (i.e. ``ctx.content``),
    not the original content.

    The canonical use case: a two-stage redaction pipeline where
    the first policy scrubs PII and the second strips secrets —
    the second policy must see the already-PII-scrubbed payload,
    not the raw original.
    """
    seen_by_second: list[dict] = []

    def first(_event: dict) -> PolicyResult:
        return PolicyResult(
            action=PolicyAction.ALLOW,
            data={"query": "after-first"},
        )

    def second(event: dict) -> PolicyResult:
        seen_by_second.append(event["data"])
        return PolicyResult(
            action=PolicyAction.ALLOW,
            data={"query": "after-second"},
        )

    policies = [
        FunctionPolicy(_spec(name="first", phase=Phase.TOOL_CALL), first),
        FunctionPolicy(_spec(name="second", phase=Phase.TOOL_CALL), second),
    ]
    engine = _build_engine(conversation_store, policies)
    result = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"query": "original"},
            tool_name="search",
        )
    )
    assert result.action == PolicyAction.ALLOW
    # The second policy must have seen the first policy's output.
    assert seen_by_second == [{"query": "after-first"}], (
        f"Second policy must receive first policy's data as content; "
        f"got {seen_by_second!r}. If 'original', the engine didn't chain."
    )
    # The composed result carries the last transform in the chain.
    assert result.data == {"query": "after-second"}


# ── Gap 7: legacy (content, phase) callable shim ─────────────────────────────


def test_resolve_function_policy_wraps_legacy_callable(tmp_path: Path) -> None:
    """``resolve_function_policy`` detects and wraps a legacy
    ``(content, phase)`` callable so it can run under the
    agent-plane FunctionPolicy dispatch without becoming a no-op.

    Before the fix, passing a legacy callable to
    ``resolve_function_policy`` stored it unwrapped. The
    evaluator would then receive ``(event_dict, config)`` instead
    of ``(content, phase)`` — the phase check (``phase ==
    "tool_call"``) always failed against a dict, silently
    producing ALLOW regardless of the event."""
    _install_module(
        tmp_path,
        "probe_legacy",
        """
        def deny_on_tool_call(content, phase):
            if phase == "tool_call":
                return {"action": "deny", "reason": "legacy denied"}
            return {"action": "allow"}
        """,
    )
    spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.TOOL_CALL)],
        function=FunctionRef(path="test_fn_policy_pkg.probe_legacy.deny_on_tool_call"),
    )
    policy = resolve_function_policy(spec)
    assert isinstance(policy, FunctionPolicy)


@pytest.mark.asyncio
async def test_resolve_function_policy_legacy_callable_evaluates_correctly(
    tmp_path: Path,
) -> None:
    """A legacy ``(content, phase)`` callable wrapped by
    ``resolve_function_policy`` returns the right decision at
    evaluation time — both the action and the reason survive
    the ``_coerce_legacy_return`` path."""
    _install_module(
        tmp_path,
        "probe_legacy_eval",
        """
        def deny_on_tool_call(content, phase):
            if phase == "tool_call":
                return {"action": "deny", "reason": "legacy denied"}
            return {"action": "allow"}
        """,
    )
    spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.TOOL_CALL)],
        function=FunctionRef(path="test_fn_policy_pkg.probe_legacy_eval.deny_on_tool_call"),
    )
    policy = resolve_function_policy(spec)
    result = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "sleep", "arguments": {"seconds": 10}},
        ),
        {"labels": {}, "conversation_id": "c"},
    )
    assert result.action == PolicyAction.DENY
    assert result.reason == "legacy denied"


def test_resolve_function_policy_modern_callable_not_wrapped(tmp_path: Path) -> None:
    """A modern ``(event)`` callable passes through
    ``resolve_function_policy`` unchanged — its identity is
    preserved and no legacy shim wrapper is injected."""
    _install_module(
        tmp_path,
        "probe_modern",
        """
        from omnigent.policies.types import PolicyResult
        from omnigent.spec.types import PolicyAction

        def modern_allow(event):
            return PolicyResult(action=PolicyAction.ALLOW)
        """,
    )
    spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test_fn_policy_pkg.probe_modern.modern_allow"),
    )
    policy = resolve_function_policy(spec)
    assert isinstance(policy, FunctionPolicy)
    # Modern callable must NOT be wrapped in a legacy shim.
    # The shim produces an inner function named "_sync_shim" or
    # "_async_shim"; the original function is named "modern_allow".
    assert policy._callable.__name__ == "modern_allow"
