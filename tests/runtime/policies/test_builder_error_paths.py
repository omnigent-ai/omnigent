"""
Builder error-path tests.

Verifies ``build_policy_engine`` + ``resolve_function_policy``
fail loudly on malformed input rather than silently producing
broken engines.

Load-bearing: silent failures here would let broken agents
ship to production — a FunctionPolicy whose dotted path
doesn't resolve should fail at workflow start, not silently
ALLOW every evaluation.
"""

from __future__ import annotations

import pytest

from omnigent.policies.function import (
    resolve_function_policy,
)
from omnigent.spec.types import (
    FunctionPolicySpec,
    FunctionRef,
    Phase,
    PhaseSelector,
)


def _fn_spec(
    path: str,
    arguments: dict | None = None,
) -> FunctionPolicySpec:
    """Build a minimal FunctionPolicySpec with the given path."""
    return FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path=path, arguments=arguments),
    )


# ── Dotted path resolution errors ─────────────────────


def test_resolve_bare_path_rejected() -> None:
    """Single-segment path (no dot) is rejected — useful
    module-level imports are dotted. If this regresses, an
    author writing `function: my_tool` would get a confusing
    AttributeError on some irrelevant module attribute."""
    with pytest.raises(ValueError, match=r"dotted module.attribute"):
        resolve_function_policy(_fn_spec("invalid_path"))


def test_resolve_missing_module_raises_import_error() -> None:
    """Module not found → clear ImportError. The caller
    (Phase 6 workflow init) surfaces this at workflow start
    so an incorrect spec fails before any evaluation runs."""
    with pytest.raises(ImportError):
        resolve_function_policy(
            _fn_spec("omnigent_nonexistent_module.handler"),
        )


def test_resolve_missing_attribute_raises_attribute_error() -> None:
    """Module exists but attribute doesn't → AttributeError.
    Distinguishes "typo in module name" from "typo in attr
    name" — gives the author a precise hint."""
    with pytest.raises(AttributeError):
        resolve_function_policy(
            _fn_spec("omnigent.spec.types.nonexistent_attr"),
        )


def test_resolve_non_callable_rejected() -> None:
    """Dotted path resolves to a non-callable (e.g. a module
    constant) → ValueError naming the resolved type."""
    # Point at a non-callable module-level constant
    # (`omnigent.spec.types.DEFAULT_ASK_TIMEOUT` is an int).
    with pytest.raises(ValueError, match=r"not callable"):
        resolve_function_policy(
            _fn_spec("omnigent.spec.types.DEFAULT_ASK_TIMEOUT"),
        )


# ── Missing function field ────────────────────────────


def test_resolve_with_none_function_raises() -> None:
    """A FunctionPolicySpec with function=None (shouldn't
    happen after parser validation, but defensive) → clear
    ValueError mentioning the parser responsibility."""
    bad_spec = FunctionPolicySpec(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=None,  # parser should have rejected
    )
    with pytest.raises(ValueError, match=r"no function reference"):
        resolve_function_policy(bad_spec)


# ── Factory-form errors ───────────────────────────────


def test_factory_bad_arguments_raises_at_build() -> None:
    """Factory accepts kwargs; calling with wrong kwargs →
    TypeError surfaces at build time, NOT at evaluate time.
    Fail-early so deploy pipelines catch the misconfiguration."""
    # rate_limit_search accepts `limit: int`; passing an
    # unexpected kwarg raises immediately.
    with pytest.raises(TypeError):
        resolve_function_policy(
            _fn_spec(
                "tests._fixtures.agents.rate_limit_policies.rate_limit_search",
                arguments={"bogus_kwarg": 99},
            ),
        )


# ── Builder + spec integration ────────────────────────


def test_build_engine_fails_on_invalid_function_path(
    conversation_store,
) -> None:
    """build_policy_engine propagates resolution errors so
    the workflow startup fails loudly on a broken spec."""
    from omnigent.runtime.policies import build_policy_engine
    from omnigent.spec.types import (
        AgentSpec,
        GuardrailsSpec,
    )

    spec = AgentSpec(
        spec_version=1,
        name="broken",
        guardrails=GuardrailsSpec(
            policies=[
                FunctionPolicySpec(
                    name="broken_fn",
                    on=[PhaseSelector(phase=Phase.REQUEST)],
                    function=FunctionRef(
                        path="totally.not.a.real.path",
                    ),
                ),
            ],
        ),
    )
    conv = conversation_store.create_conversation()
    # Workflow init should raise, not silently ALLOW.
    with pytest.raises(ImportError):
        build_policy_engine(
            spec=spec,
            conversation_id=conv.id,
            conversation_store=conversation_store,
        )
