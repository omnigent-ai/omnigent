"""Lazy resolver for native-harness provider hooks.

``NativeHarnessProvider`` rows hold dotted import *strings*, never live
callables — building the harness registry must not import the runner / CLI /
native-harness stack (see designs/harness-plugin-interface.md § import rules).
This module is the single place those strings become callables, and only at
dispatch time. Each dispatch hub (resume, CLI, runner launch/interrupt/stop,
seeding) resolves its hook here instead of branching on ``key == "<x>"``.

Resolution is cached per import path so a hot dispatch loop imports each target
module at most once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeAlias, cast

from omnigent.harness_plugins import (
    NativeHarnessProvider,
    load_object,
    native_provider_for_key,
)

# Provider hooks intentionally have unrelated sync and async signatures.
_NativeHook: TypeAlias = Callable[..., Any]  # type: ignore[explicit-any]


def resolve(import_path: str) -> object:
    """Resolve a ``module:attr`` / ``module.attr`` path to its object.

    Thin wrapper over :func:`omnigent.harness_plugins.load_object`. Deliberately
    *not* cached: native dispatch happens at most once per resume/launch/seed —
    never in a hot loop — and the underlying ``importlib.import_module`` already
    caches the module in ``sys.modules``, so the only repeated cost is a cheap
    ``getattr``. Resolving fresh keeps ``monkeypatch.setattr("...:run_x", ...)``
    working, which caching would silently defeat by pinning the pre-patch object.
    """
    return load_object(import_path)


def resolve_hook(provider: NativeHarnessProvider, hook: str) -> _NativeHook | None:
    """Resolve one named hook on a provider, or ``None`` if it is unset.

    ``hook`` is a field name on :class:`NativeHarnessProvider` (e.g.
    ``"run_native"``, ``"auto_create_terminal"``). Optional hooks that the
    provider leaves ``None`` resolve to ``None`` rather than raising, so callers
    can treat "no such hook yet" and "hook present" uniformly.
    """
    import_path = getattr(provider, hook)
    if import_path is None:
        return None
    resolved = resolve(import_path)
    if not callable(resolved):
        raise TypeError(f"native provider hook {import_path!r} is not callable")
    return cast(_NativeHook, resolved)


def resolve_hook_for_key(key: str, hook: str) -> _NativeHook | None:
    """Resolve a hook by native-agent ``key``, or ``None`` if unknown/unset."""
    provider = native_provider_for_key(key)
    if provider is None:
        return None
    return resolve_hook(provider, hook)
