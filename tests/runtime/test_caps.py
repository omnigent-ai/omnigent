"""Tests for omnigent.runtime.caps."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest.mock import Mock, sentinel

import pytest

from omnigent import runtime, terminals
from omnigent.runtime import _globals
from omnigent.runtime.caps import RuntimeCaps
from omnigent.spec.types import ExecutorSpec


class _ObservedLock:
    """Expose contention and bound waits so a locking regression cannot hang pytest."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.contended = Event()

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            assert self._lock.acquire(timeout=5), "caps lock was not released"

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


@pytest.fixture
def isolated_runtime(monkeypatch: pytest.MonkeyPatch) -> _ObservedLock:
    """Restore all init assignments and prevent real terminal-registry creation."""
    for name in (
        "_caps",
        "_conversation_store",
        "_agent_store",
        "_agent_cache",
        "_file_store",
        "_artifact_store",
        "_comment_store",
        "_policy_store",
        "_terminal_registry",
    ):
        monkeypatch.setattr(_globals, name, None)
    lock = _ObservedLock()
    monkeypatch.setattr(_globals, "_caps_lock", lock)
    monkeypatch.setattr(
        terminals, "TerminalRegistry", Mock(return_value=sentinel.terminal_registry)
    )
    return lock


def _init_runtime(caps: RuntimeCaps | None) -> None:
    runtime.init(
        conversation_store=sentinel.conversation_store,
        agent_store=sentinel.agent_store,
        agent_cache=sentinel.agent_cache,
        caps=caps,
    )


@pytest.mark.parametrize("configure", [False, True], ids=["second-getter", "configured-init"])
def test_lazy_caps_concurrent_initialization(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: _ObservedLock,
    configure: bool,
) -> None:
    """First access constructs once; a concurrent configured init wins publication."""
    default = RuntimeCaps()
    configured = RuntimeCaps(execution_timeout=45)
    constructor_entered = Event()
    release_constructor = Event()

    def construct_default() -> RuntimeCaps:
        constructor_entered.set()
        assert release_constructor.wait(timeout=5), "test did not release caps construction"
        return default

    factory = Mock(side_effect=construct_default)
    monkeypatch.setattr(runtime, "RuntimeCaps", factory)
    factory.assert_not_called()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime.get_caps)
        try:
            assert constructor_entered.wait(timeout=5)
            second = (
                pool.submit(_init_runtime, configured)
                if configure
                else pool.submit(runtime.get_caps)
            )
            assert isolated_runtime.contended.wait(timeout=5)
        finally:
            release_constructor.set()

        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    if configure:
        # Either value can reach the caller racing init; later readers see configured caps.
        assert first_result is default or first_result is configured
        assert runtime.get_caps() is configured
    else:
        assert first_result is default
        assert second_result is default
        assert runtime.get_caps() is default
    factory.assert_called_once_with()


@pytest.mark.parametrize(
    "read_before_init", [False, True], ids=["uninitialized", "cached-default"]
)
def test_runtime_init_replaces_caps_and_resets_defaults(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: _ObservedLock,
    read_before_init: bool,
) -> None:
    """Explicit caps replace any lazy default; init(None) restores fresh defaults."""
    expected = RuntimeCaps()
    configured = RuntimeCaps(execution_timeout=45, sandbox_enabled=False)
    factory = Mock(wraps=RuntimeCaps)
    monkeypatch.setattr(runtime, "RuntimeCaps", factory)
    monkeypatch.setattr(_globals, "RuntimeCaps", factory)
    initial = runtime.get_caps() if read_before_init else None
    assert factory.call_count == int(read_before_init)

    _init_runtime(configured)
    assert runtime.get_caps() is configured
    assert runtime.get_caps() is configured
    assert factory.call_count == int(read_before_init)

    _init_runtime(None)
    reset = runtime.get_caps()
    assert reset is not configured
    assert reset is not initial
    assert reset.execution_timeout == expected.execution_timeout
    assert reset.sandbox_enabled is expected.sandbox_enabled
    assert runtime.get_caps() is reset
    assert factory.call_count == int(read_before_init) + 1


def test_lazy_caps_constructor_failure_can_retry(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: _ObservedLock,
) -> None:
    """Failed construction neither publishes caps nor leaves the lock held."""
    recovered = RuntimeCaps()
    factory = Mock(side_effect=[RuntimeError("default caps failed"), recovered])
    monkeypatch.setattr(runtime, "RuntimeCaps", factory)

    with pytest.raises(RuntimeError, match="default caps failed"):
        runtime.get_caps()
    assert _globals._caps is None

    assert runtime.get_caps() is recovered
    assert runtime.get_caps() is recovered
    assert factory.call_count == 2


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
