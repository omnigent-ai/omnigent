"""
Fixtures for runtime policy tests.

Re-imports the `conversation_store` fixture (and its
underlying `db_uri`) from `tests/stores/conftest.py` so tests
here can exercise the real persistence layer without
duplicating setup.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.policies.function import FunctionPolicy
from omnigent.policies.types import PolicyResult
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, PhaseSelector, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """
    Conversation store backed by a per-test SQLite DB.

    Mirrors the fixture in `tests/stores/conftest.py` — kept
    local so runtime policy tests can evolve their dependency
    surface without coupling to store-test internals.
    """
    return SqlAlchemyConversationStore(db_uri)


def make_fixed_policy(
    name: str,
    action: PolicyAction = PolicyAction.ALLOW,
    reason: str | None = None,
    set_labels: dict[str, str] | None = None,
    on: list[PhaseSelector] | None = None,
    condition: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
    ask_timeout: int | None = None,
) -> FunctionPolicy:
    """
    Build a :class:`FunctionPolicy` that always returns a fixed result.

    Replaces the removed ``LabelPolicy`` in tests — produces the
    same behavior (unconditional fixed action + optional label
    writes) without requiring the deleted ``LabelPolicySpec``.

    :param name: Policy name, e.g. ``"taint_web"``.
    :param action: Fixed action to return, e.g.
        ``PolicyAction.DENY``.
    :param reason: Human-readable reason, e.g.
        ``"Web content is untrusted."``.
    :param set_labels: Label writes to emit, e.g.
        ``{"integrity": "0"}``.
    :param on: Phase selectors; ``None`` means all phases.
    :param condition: Label-gate condition dict, e.g.
        ``{"integrity": "0"}``.
    :param config: Runtime key-value config dict. Unused by
        the fixed callable but stored on the spec.
    :param ask_timeout: Per-policy ASK timeout override in
        seconds. ``None`` inherits the spec-wide default.
    :returns: A :class:`FunctionPolicy` that always returns the
        specified result.
    """
    frozen_labels = dict(set_labels) if set_labels else None
    frozen_action = action
    frozen_reason = reason

    def _fixed(event: dict[str, Any]) -> PolicyResult:
        """Return the fixed result regardless of event content."""
        return PolicyResult(
            action=frozen_action,
            reason=frozen_reason,
            set_labels=frozen_labels,
        )

    spec = FunctionPolicySpec(
        name=name,
        on=on,
        condition=condition,
        config=config,
        ask_timeout=ask_timeout,
        function=FunctionRef(path=f"<inline:{name}>"),
    )
    policy = FunctionPolicy.__new__(FunctionPolicy)
    policy.spec = spec
    policy._callable = _fixed
    policy._is_async = False
    policy._arity = 1
    policy._config = dict(config) if config else {}
    return policy


def _always_allow(event: dict[str, Any]) -> dict[str, Any]:
    """Fixture callable: always ALLOW with no label writes."""
    return {"result": "allow"}


def _always_allow_taint_integrity(event: dict[str, Any]) -> dict[str, Any]:
    """Fixture callable: ALLOW and write ``integrity=0``."""
    return {
        "result": "allow",
        "set_labels": {"integrity": "0"},
    }


def make_fixed_function_policy_spec(
    name: str,
    *,
    on: list[PhaseSelector] | None = None,
    condition: dict[str, str] | None = None,
    fn_path: str = "tests.unit.runtime.policies.conftest._always_allow",
) -> FunctionPolicySpec:
    """
    Build a :class:`FunctionPolicySpec` with a real importable path.

    Use when tests need a spec that survives ``build_policy_engine``
    (which calls ``resolve_function_policy`` → ``importlib``).

    :param name: Policy name, e.g. ``"taint_web"``.
    :param on: Phase selectors.
    :param condition: Label-gate condition dict.
    :param fn_path: Dotted import path to the callable.
    :returns: A spec whose ``function.path`` resolves at import
        time.
    """
    return FunctionPolicySpec(
        name=name,
        on=on,
        condition=condition,
        function=FunctionRef(path=fn_path),
    )
