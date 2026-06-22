"""Tests for omnigent.runtime.caps."""

from __future__ import annotations

import pytest

from omnigent.runtime.caps import RuntimeCaps
from omnigent.spec.types import ExecutorSpec


def test_runtime_caps_default_value() -> None:
    """RuntimeCaps with no args uses the 7200s default."""
    caps = RuntimeCaps()

    # Default execution_timeout is 7200s per the dataclass definition.
    # Failure means the default was changed without updating dependents.
    assert caps.execution_timeout == 7200


def test_runtime_caps_custom_value() -> None:
    """RuntimeCaps accepts a custom execution_timeout."""
    caps = RuntimeCaps(execution_timeout=3600)

    # Custom value should override the default.
    # Failure means the constructor ignores the argument.
    assert caps.execution_timeout == 3600


def test_execution_config_default_values() -> None:
    """
    ExecutorSpec defaults match the values the runtime relies on.

    Verifies timeout=3600 and max_iterations=1000 so that changes to
    defaults are caught before they silently alter clamping behavior.
    """
    config = ExecutorSpec()

    # Default timeout is 3600s per the dataclass definition.
    # Failure means the default shifted, which changes clamping outcomes.
    assert config.timeout == 3600

    # Default max_iterations is 1000 per the dataclass definition.
    # Failure means the iteration ceiling changed without updating dependents.
    assert config.max_iterations == 1000


@pytest.mark.parametrize(
    ("spec_timeout", "cap_timeout", "expected"),
    [
        pytest.param(
            1800,
            7200,
            1800,
            id="spec_lower_than_cap_uses_spec",
        ),
        pytest.param(
            7200,
            3600,
            3600,
            id="cap_lower_than_spec_uses_cap",
        ),
        pytest.param(
            3600,
            3600,
            3600,
            id="equal_values_returns_same",
        ),
    ],
)
def test_execution_timeout_resolution(
    spec_timeout: int,
    cap_timeout: int,
    expected: int,
) -> None:
    """
    Verify ``min(spec.executor.timeout, caps.execution_timeout)`` clamping.

    The runtime resolves the effective execution timeout as
    ``min(spec.executor.timeout, caps.execution_timeout)``. This test
    constructs both dataclasses and applies the same ``min()`` logic to
    confirm the resolved value matches expectations.

    :param spec_timeout: The agent spec's ``executor.timeout`` value
        in seconds, e.g. ``1800``.
    :param cap_timeout: The operator cap's ``execution_timeout`` value
        in seconds, e.g. ``7200``.
    :param expected: The effective timeout after clamping, e.g. ``1800``.
    """
    config = ExecutorSpec(timeout=spec_timeout)
    caps = RuntimeCaps(execution_timeout=cap_timeout)

    resolved = min(config.timeout, caps.execution_timeout)

    # The resolved timeout must equal the smaller of the two inputs.
    # Failure means the dataclass fields don't hold the values passed
    # to their constructors, which would break the runtime's clamping.
    assert resolved == expected
