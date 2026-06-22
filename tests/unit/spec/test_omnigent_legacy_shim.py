"""
Tests for :mod:`omnigent.spec._omnigent_legacy_shim` — the
compatibility layer that lets legacy omnigent
``(content, phase)`` function-policy callables run under
Omnigent' ``(ctx, context)`` convention.

Each test pins one of the shim's contracts. The e2e integration
that exercises the full translator → engine pipeline lives in
``tests/e2e/omnigent/test_run_omnigent_policy_enforcement.py``.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.spec._omnigent_legacy_shim import (
    _convert_args,
    _has_legacy_signature,
    _legacy_content,
    _legacy_context,
    _wrap_legacy,
    build,
)
from omnigent.spec.types import Phase

# ── signature detection ──────────────────────────────────────


def _legacy_two_arg(content: Any, phase: str) -> dict[str, Any]:
    """Legacy-style fixture: positional params named (content, phase)."""
    return {"action": "allow"}


def _legacy_three_arg(
    content: Any,
    phase: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Legacy-style fixture: three positional params."""
    return {"action": "allow"}


def _modern_one_arg(ctx: EvaluationContext) -> dict[str, Any]:
    """Agent-plane-native one-arg fixture."""
    return {"action": "allow"}


def _modern_two_arg(
    ctx: EvaluationContext,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Agent-plane-native two-arg fixture."""
    return {"action": "allow"}


def _wrong_order(phase: str, content: Any) -> dict[str, Any]:
    """
    Two args but names are swapped — detection must treat this
    as modern (the shim deliberately refuses to second-guess
    authors whose signatures don't exactly match the legacy
    convention).
    """
    return {"action": "allow"}


@pytest.mark.parametrize(
    "fn,expected_legacy",
    [
        (_legacy_two_arg, True),
        (_legacy_three_arg, True),
        (_modern_one_arg, False),
        (_modern_two_arg, False),
        (_wrong_order, False),
    ],
    ids=[
        "legacy_two_arg",
        "legacy_three_arg",
        "modern_one_arg",
        "modern_two_arg",
        "wrong_order_param_names",
    ],
)
def test_has_legacy_signature_matches_only_exact_param_names(
    fn: Any,
    expected_legacy: bool,
) -> None:
    """
    Signature detection is strict: only params named
    ``(content, phase)`` in that order count as legacy. Any
    other naming (including swapped order) is treated as modern
    so ``_wrap_legacy`` stays out of the way.

    What breaks if this fails: a modern callable happens to be
    wrapped and its ``ctx`` is re-shaped into a legacy content
    dict — the policy then reads garbage and mis-decides.
    """
    assert _has_legacy_signature(fn) is expected_legacy


# ── content conversion per phase ─────────────────────────────


def test_legacy_content_input_passes_string_through() -> None:
    """REQUEST: omnigent puts the raw text in ``ctx.content``; legacy expects the same."""
    ctx = EvaluationContext(phase=Phase.REQUEST, content="user said hello", tool_name=None)
    assert _legacy_content(ctx) == "user said hello"


def test_legacy_content_output_passes_string_through() -> None:
    """RESPONSE: ``ctx.content`` is the assistant text — pass through."""
    ctx = EvaluationContext(phase=Phase.RESPONSE, content="here is the answer", tool_name=None)
    assert _legacy_content(ctx) == "here is the answer"


def test_legacy_content_tool_call_passes_dict_through() -> None:
    """
    TOOL_CALL: omnigent already normalizes to
    ``{"tool": name, "args": parsed_args}`` at the enforcement
    site (see ``_enforce_tool_call_policy``). Legacy omnigent
    expects the same shape — pass the dict through verbatim.

    What breaks if this fails: ``block_long_sleep`` reads
    ``content["args"]["seconds"]``; if the shim reshapes the
    dict under the wrong key path, ``seconds`` defaults to 0
    and the policy silently allows every sleep.
    """
    ctx_content = {"tool": "sleep", "args": {"seconds": 8}}
    ctx = EvaluationContext(phase=Phase.TOOL_CALL, content=ctx_content, tool_name="sleep")
    # Exact-dict equality: any reshape would fail here.
    assert _legacy_content(ctx) == {"tool": "sleep", "args": {"seconds": 8}}


def test_legacy_content_tool_result_plain_string_passes_through() -> None:
    """
    TOOL_RESULT: omnigent passes the raw tool output string
    (no longer wrapped — kasey bug #4 fix). Non-JSON strings
    pass through to the legacy callable unchanged so callables
    branching on ``isinstance(content, str)`` still work.
    """
    ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content="slept 2 seconds",
        tool_name="sleep",
    )
    assert _legacy_content(ctx) == "slept 2 seconds"


def test_legacy_content_tool_result_json_text_parses_to_dict() -> None:
    """
    TOOL_RESULT: when Omnigent' raw string IS a JSON-encoded
    object, the shim parses it into the corresponding Python
    dict. Mirrors omnigent-native's
    :func:`omnigent.inner.mcp_tools._extract_call_result_payload`,
    which JSON-parses each text content block on the way back.

    Why this matters: the Databricks ``google_policy``'s
    ``tool_result`` branch is gated on
    ``isinstance(content, dict)`` — without JSON-parsing here,
    every MCP tool result reaches the policy as a string and
    the file-id tracking branch never runs, so any follow-up
    operation on a doc the agent just created (update,
    comment) gets denied because the doc id was never
    recorded. This is the original screenshot bug's second
    failure mode.
    """
    payload_dict = {"document_id": "DocXYZ", "url": "https://docs/x/DocXYZ"}
    payload_text = json.dumps(payload_dict)
    ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content=payload_text,
        tool_name="google__docs_document_create",
    )
    parsed = _legacy_content(ctx)
    # Exact-dict equality, not just isinstance — any change
    # to the parse semantics shows up here.
    assert parsed == payload_dict


def test_legacy_content_tool_result_partial_json_falls_back() -> None:
    """
    Truncated / malformed JSON must NOT raise — fall back to
    the raw string. A flaky tool result that emits half a JSON
    object should reach the policy unmodified, not crash the
    whole evaluation.
    """
    bad = '{"document_id": "DocXYZ", "url": "ht'
    ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content=bad,
        tool_name="x",
    )
    assert _legacy_content(ctx) == bad


# ── _convert_args (wiring of content + phase str + optional context) ─


def test_convert_args_two_arg_form_omits_context() -> None:
    """
    Two-arg legacy callables (``fn(content, phase)``) must
    receive exactly two positional arguments — passing an
    extra ``context`` dict would raise
    ``TypeError: too many positional arguments``.
    """
    ctx = EvaluationContext(phase=Phase.REQUEST, content="hi", tool_name=None)
    args = _convert_args(ctx, {"labels": {}}, wants_context=False)
    # Exactly (content, "input") — length 2 is the contract.
    assert args == ("hi", "input")


def test_convert_args_three_arg_form_includes_context() -> None:
    """
    Three-arg legacy callables (``fn(content, phase, context)``)
    receive the legacy context dict: labels from the engine +
    ``tool_name`` on tool phases.
    """
    ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        # Workflow now passes the raw tool output string (kasey
        # bug #4 fix); shim's JSON-parse leaves a non-JSON string
        # untouched.
        content="ok",
        tool_name="web_search",
    )
    args = _convert_args(
        ctx,
        {"labels": {"integrity": "0"}},
        wants_context=True,
    )
    assert args == (
        "ok",
        "tool_result",
        {"labels": {"integrity": "0"}, "tool_name": "web_search"},
    )


def test_legacy_context_does_not_mutate_engine_context() -> None:
    """
    Building the legacy context dict must not mutate the
    engine's dict — if it did, ``tool_name`` would persist
    between evaluations and cross-contaminate policy calls.
    Uses ``TOOL_RESULT`` because that's the phase native
    omnigent adds ``tool_name`` on (see
    :meth:`Session._apply_tool_result_policy`); on
    ``TOOL_CALL`` the legacy context has no ``tool_name`` key,
    so a mutation test there couldn't observe the leakage
    this test guards against.
    """
    engine_ctx = {"labels": {"integrity": "1"}}
    ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content="ok",
        tool_name="sleep",
    )
    legacy = _legacy_context(ctx, engine_ctx)
    # Derived dict carries tool_name; engine dict is unchanged.
    assert legacy == {"labels": {"integrity": "1"}, "tool_name": "sleep"}
    assert engine_ctx == {"labels": {"integrity": "1"}}
    assert "tool_name" not in engine_ctx


def test_legacy_context_threads_configured_phases_when_provided() -> None:
    """
    When ``build`` is given the YAML ``on:`` list as
    ``configured_phases``, the legacy context dict carries it
    through to the callable on every evaluation.

    Why this matters: legacy omnigent callables that read
    ``context["configured_phases"]`` (notably the Databricks
    ``google_policy``) deny by default unless the caller
    advertises which phases they hooked. Without this, every
    Google MCP write through the Omnigent denied with
    ``google_policy requires on=["tool_call", "tool_result"]``
    (the original bug).

    What breaks if this fails: any policy that uses
    ``context["configured_phases"]`` as a contract assertion
    silently denies under Omnigent mode.
    """
    engine_ctx = {"labels": {}, "conversation_id": "c_abc"}
    # ``TOOL_RESULT`` chosen because that's the phase that
    # populates ``tool_name`` in the legacy context (matching
    # native omnigent — see ``_legacy_context``'s docstring),
    # so this test exercises both ``configured_phases`` AND
    # ``tool_name`` plumbing in the same call.
    ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content="{}",
        tool_name="google__docs_document_create",
    )
    phases_in: list[str] = ["tool_call", "tool_result"]
    legacy = _legacy_context(ctx, engine_ctx, configured_phases=phases_in)
    # Value: the YAML ``on:`` shows up verbatim in the dict.
    assert legacy["configured_phases"] == ["tool_call", "tool_result"]
    # Identity: must be a fresh list, not the input object —
    # otherwise a legacy callable that mutates
    # ``context["configured_phases"]`` (e.g. .append, .clear)
    # would leak state into the shared spec / next evaluation.
    assert legacy["configured_phases"] is not phases_in
    # ``labels`` and ``tool_name`` keep their existing semantics.
    assert legacy["labels"] == {}
    assert legacy["tool_name"] == "google__docs_document_create"


def test_legacy_context_omits_configured_phases_when_not_provided() -> None:
    """
    Calling ``_legacy_context`` without ``configured_phases``
    must NOT add the key to the dict — preserves the contract
    for callers that build the shim directly without a
    translator (the existing test suite + ad-hoc unit tests).

    What breaks if this fails: legacy callables that branch on
    ``"configured_phases" in context`` (none today, but the
    contract has always been "absent => not provided") would
    behave differently between the translator path and direct
    callers.
    """
    legacy = _legacy_context(
        EvaluationContext(phase=Phase.REQUEST, content="hi", tool_name=None),
        {"labels": {}},
    )
    assert "configured_phases" not in legacy


def test_legacy_context_omits_tool_name_on_tool_call() -> None:
    """
    Native omnigent adds ``tool_name`` to the legacy context
    ONLY on ``TOOL_RESULT`` (see
    :meth:`Session._apply_tool_result_policy`'s ``context =
    {"tool_name": tool_name}`` setup). On ``TOOL_CALL`` no
    ``tool_name`` key is added — the legacy callable reads the
    name from ``content["tool"]`` instead.

    The shim must mirror this so callables that branch on
    ``"tool_name" in context`` to discriminate phase behave
    identically across native and Omnigent mode.

    What breaks if this fails: a legacy callable that writes
    ``if "tool_name" in context:`` as shorthand for "we're on a
    tool_result" silently misclassifies tool_call evaluations
    as tool_result evaluations.
    """
    legacy = _legacy_context(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"tool": "search", "args": {}},
            tool_name="search",
        ),
        {"labels": {}},
    )
    assert "tool_name" not in legacy


def test_legacy_context_omits_tool_name_on_input_and_output() -> None:
    """
    Same contract as above for ``REQUEST`` / ``RESPONSE``: those
    phases never carry a tool name in native, so ``tool_name``
    must not appear in the legacy context dict either.

    Also verifies the shim doesn't accidentally fabricate a
    tool name on these phases — ``ctx.tool_name`` is ``None``
    on REQUEST/RESPONSE in omnigent today, and any future change
    that populated it should still result in the legacy
    context omitting the key.
    """
    for phase in (Phase.REQUEST, Phase.RESPONSE):
        legacy = _legacy_context(
            EvaluationContext(phase=phase, content="hi", tool_name=None),
            {"labels": {}},
        )
        assert "tool_name" not in legacy, f"tool_name leaked on {phase}"


def test_convert_args_three_arg_form_forwards_configured_phases() -> None:
    """
    The wiring from ``_convert_args`` to ``_legacy_context``
    must not drop ``configured_phases``. This is the layer that
    hands the legacy context dict to the legacy callable —
    a regression here is silent (the callable just sees the
    pre-fix legacy context).
    """
    ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content="ok",
        tool_name="google__docs_document_create",
    )
    args = _convert_args(
        ctx,
        {"labels": {}},
        wants_context=True,
        configured_phases=["tool_call", "tool_result"],
    )
    # Last positional is the legacy context dict; it must
    # contain ``configured_phases`` verbatim.
    assert args[-1]["configured_phases"] == ["tool_call", "tool_result"]


# ── build(): wrapping / passthrough ──────────────────────────


@pytest.fixture()
def ephemeral_module() -> Any:
    """
    Create a fresh module on ``sys.modules`` holding test
    callables. Used by ``build`` tests that need real import
    targets but shouldn't depend on ``examples/`` being on the
    path. Cleans up on teardown.
    """
    mod_name = "_legacy_shim_test_ephemeral"
    module = types.ModuleType(mod_name)
    sys.modules[mod_name] = module
    yield module
    sys.modules.pop(mod_name, None)


def test_build_wraps_legacy_callable_and_converts_args(
    ephemeral_module: Any,
) -> None:
    """
    Legacy callables get wrapped. The wrapper takes Omnigent'
    ``(ctx, context)`` call shape and invokes the underlying
    callable with the legacy ``(content, phase)`` shape.

    What breaks if this fails: ``block_long_sleep`` and friends
    see ``ctx`` where they expected a content dict — their
    ``isinstance(content, dict)`` check fails, they allow
    everything, and the tool call goes through unblocked.
    """
    calls: list[tuple[Any, str]] = []

    def _legacy(content: Any, phase: str) -> dict[str, Any]:
        calls.append((content, phase))
        if content.get("args", {}).get("seconds", 0) > 5:
            return {"action": "deny", "reason": "too long"}
        return {"action": "allow"}

    ephemeral_module.policy = _legacy

    wrapped = build("_legacy_shim_test_ephemeral.policy")
    # Not the same function object — wrapping happened.
    assert wrapped is not _legacy

    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"tool": "sleep", "args": {"seconds": 8}},
        tool_name="sleep",
    )
    result = wrapped(ctx, {"labels": {}})
    # Legacy callable received legacy-shaped args — verified by
    # inspecting the captured call and by the real return value.
    # The shim coerces the old {"action": ...} dict to the V0
    # {"decision": {"result": ...}} format so the Omnigent'
    # _coerce_to_policy_result can handle it uniformly.
    assert calls == [({"tool": "sleep", "args": {"seconds": 8}}, "tool_call")]
    assert result == {"result": "DENY", "reason": "too long"}


def test_build_returns_modern_callable_unchanged(ephemeral_module: Any) -> None:
    """
    Modern ``(ctx, context)`` callables pass through ``build``
    without any wrapping. This keeps the shim transparent for
    omnigent-native policies and lets mixed YAMLs (one
    legacy policy + one modern policy) work uniformly.

    What breaks if this fails: modern callables end up
    double-dispatched or get their ``ctx`` reshaped, misreading
    the engine context entirely.
    """

    def _modern(ctx: EvaluationContext, context: dict[str, Any]) -> dict[str, Any]:
        return {"action": "allow"}

    ephemeral_module.modern = _modern
    resolved = build("_legacy_shim_test_ephemeral.modern")
    # Identity: same function object, no wrapping.
    assert resolved is _modern


def test_build_applies_factory_kwargs_before_wrapping(ephemeral_module: Any) -> None:
    """
    YAMLs that use ``factory_params:`` expect the target to be
    called as a factory. The shim must call the factory first
    (passing ``factory_kwargs``) and then decide whether to
    wrap the *factory's return value*.

    This keeps closure-state policies (rate limits etc.)
    working through the shim.
    """
    captured_kwargs: dict[str, Any] = {}

    def _factory(limit: int) -> Any:
        captured_kwargs["limit"] = limit

        def _legacy_inner(content: Any, phase: str) -> dict[str, Any]:
            if phase == "tool_call":
                return {"action": "deny", "reason": f"limit={limit}"}
            return {"action": "allow"}

        return _legacy_inner

    ephemeral_module.rate_factory = _factory
    wrapped = build(
        "_legacy_shim_test_ephemeral.rate_factory",
        factory_kwargs={"limit": 3},
    )
    # Factory was called with the declared kwargs.
    assert captured_kwargs == {"limit": 3}

    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"tool": "x", "args": {}},
        tool_name="x",
    )
    # The wrapped factory-result still decides via its closure.
    # The shim normalizes legacy dict returns to V0 format.
    result = wrapped(ctx, {"labels": {}})
    assert result == {"result": "DENY", "reason": "limit=3"}


def test_build_raises_when_target_not_callable(ephemeral_module: Any) -> None:
    """
    Non-callable targets must fail loud at build time rather
    than silently short-circuiting to ALLOW at call time.
    A ``42`` or a string in place of a policy is a config bug
    and should crash the engine construction.
    """
    ephemeral_module.not_a_func = 42

    with pytest.raises(TypeError, match="expected a callable"):
        build("_legacy_shim_test_ephemeral.not_a_func")


# ── end-to-end via FunctionPolicySpec ────────────────────────


def test_shim_builds_usable_FunctionPolicy_through_spec_factory(
    ephemeral_module: Any,
) -> None:
    """
    Proves the shim integrates with the existing
    :func:`omnigent.policies.function.resolve_function_policy`
    factory mechanism — i.e. the translator's ``function: {path:
    shim.build, arguments: {target: ...}}`` shape drives a real
    :class:`FunctionPolicy` that returns the legacy callable's
    decision.

    What breaks if this fails: the end-to-end path has a gap
    somewhere between the translator's factory form and the
    engine's invocation; only integration tests would catch it
    otherwise.
    """
    from omnigent.policies.function import resolve_function_policy
    from omnigent.spec.types import (
        FunctionPolicySpec,
        FunctionRef,
        Phase,
        PhaseSelector,
    )

    def _legacy(content: Any, phase: str) -> dict[str, Any]:
        # Deny only tool_call on "sleep" with seconds > 5.
        if phase != "tool_call":
            return {"action": "allow"}
        if not isinstance(content, dict):
            return {"action": "allow"}
        if content.get("tool") != "sleep":
            return {"action": "allow"}
        if content.get("args", {}).get("seconds", 0) > 5:
            return {"action": "deny", "reason": "sleep too long"}
        return {"action": "allow"}

    ephemeral_module.policy = _legacy

    spec = FunctionPolicySpec(
        name="sleep_gate",
        on=(PhaseSelector(phase=Phase.TOOL_CALL),),
        function=FunctionRef(
            path="omnigent.spec._omnigent_legacy_shim.build",
            arguments={"target": "_legacy_shim_test_ephemeral.policy"},
        ),
    )
    policy = resolve_function_policy(spec)

    import asyncio

    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"tool": "sleep", "args": {"seconds": 8}},
        tool_name="sleep",
    )
    result = asyncio.run(policy.evaluate(ctx, {"labels": {}}))
    # PolicyResult carries the deny — full pipeline worked.
    from omnigent.spec.types import PolicyAction

    assert result.action == PolicyAction.DENY
    assert result.reason == "sleep too long"


# ── reset_turn forwarding (fix #2) ───────────────────────────


def test_build_forwards_reset_turn_attribute_from_legacy_callable() -> None:
    """
    The legacy ``max_tool_calls_per_turn`` factory in
    ``examples/_shared/rate_limit_policy.py`` returns an
    ``evaluate`` callable with a ``reset_turn`` attribute. The
    shim must surface that attribute on the wrapper so
    Omnigent' :class:`FunctionPolicy.reset_turn` can find
    and invoke it at turn boundaries.

    What breaks if this fails: per-turn rate-limit counters
    silently degrade to per-session limits — the agent quietly
    runs further than the YAML author intended.
    """
    reset_calls: list[None] = []

    def _legacy(content: Any, phase: str) -> dict[str, Any]:
        """Legacy-style callable with a reset_turn attribute."""
        del content, phase
        return {"action": "allow"}

    def _reset() -> None:
        reset_calls.append(None)

    _legacy.reset_turn = _reset  # type: ignore[attr-defined]

    wrapped = _wrap_legacy(_legacy)
    # Wrapper exposes the reset_turn passthrough.
    assert hasattr(wrapped, "reset_turn")
    wrapped.reset_turn()
    # The underlying reset was invoked exactly once — no
    # decorators, no copies, no extra invocations.
    assert len(reset_calls) == 1


def test_build_does_not_attach_reset_turn_when_callable_lacks_it() -> None:
    """
    Stateless legacy callables (no ``reset_turn`` attribute)
    must NOT get a fabricated reset_turn on the wrapper. A
    spurious no-op attribute would still pass the
    ``hasattr(...) and callable(...)`` check downstream and
    waste a function call per turn — but more importantly,
    pinning this contract here protects against future shim
    changes that auto-create a default no-op (which would
    mask author-side bugs where reset_turn was supposed to
    exist but didn't).
    """

    def _stateless(content: Any, phase: str) -> dict[str, Any]:
        del content, phase
        return {"action": "allow"}

    wrapped = _wrap_legacy(_stateless)
    assert not hasattr(wrapped, "reset_turn")


def test_rate_limit_factory_reset_turn_propagates_through_shim_and_policy() -> None:
    """
    End-to-end regression for fix #2: the production
    ``max_tool_calls_per_turn`` rate-limit factory in
    ``examples/_shared/rate_limit_policy.py`` returns an
    evaluator whose ``reset_turn`` clears a closure counter.
    The shim must surface that attribute, and
    :meth:`FunctionPolicy.reset_turn` must invoke it. This
    test pins the full path from build() through to reset.

    What breaks if this fails: per-turn rate limits silently
    behave as per-session limits when configured under
    Omnigent mode. The agent runs further than the YAML author
    intended, with no visible deny in logs.
    """
    import asyncio

    from omnigent.policies.function import resolve_function_policy
    from omnigent.spec.types import (
        FunctionPolicySpec,
        FunctionRef,
        Phase,
        PhaseSelector,
        PolicyAction,
    )

    spec = FunctionPolicySpec(
        name="rate_limit",
        on=(PhaseSelector(phase=Phase.TOOL_CALL),),
        function=FunctionRef(
            path="omnigent.spec._omnigent_legacy_shim.build",
            arguments={
                "target": (
                    "tests.resources.examples._shared.rate_limit_policy.max_tool_calls_per_turn"
                ),
                "factory_kwargs": {"limit": 2},
                "configured_phases": ["tool_call"],
            },
        ),
    )
    policy = resolve_function_policy(spec)

    def _call_once() -> PolicyAction:
        return asyncio.run(
            policy.evaluate(
                EvaluationContext(
                    phase=Phase.TOOL_CALL,
                    content={"tool": "x", "args": {}},
                    tool_name="x",
                ),
                {"labels": {}},
            )
        ).action

    # Two calls fit under the limit; the third trips it.
    assert _call_once() == PolicyAction.ALLOW
    assert _call_once() == PolicyAction.ALLOW
    assert _call_once() == PolicyAction.DENY

    # Now the contract: reset_turn() must zero the underlying
    # counter so subsequent calls ALLOW again. Without
    # FunctionPolicy.reset_turn forwarding (or the shim's
    # attribute passthrough), this stays DENY forever.
    policy.reset_turn()
    assert _call_once() == PolicyAction.ALLOW
    assert _call_once() == PolicyAction.ALLOW
    assert _call_once() == PolicyAction.DENY
