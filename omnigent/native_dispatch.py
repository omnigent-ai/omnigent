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

from typing import Any

from omnigent.harness_plugins import (
    NativeHarnessProvider,
    load_object,
    native_provider_for_key,
)

_RESOLVE_CACHE: dict[str, Any] = {}


def resolve(import_path: str) -> Any:
    """Resolve a ``module:attr`` / ``module.attr`` path to its object, cached.

    Thin caching wrapper over :func:`omnigent.harness_plugins.load_object`.
    """
    cached = _RESOLVE_CACHE.get(import_path)
    if cached is None:
        cached = load_object(import_path)
        _RESOLVE_CACHE[import_path] = cached
    return cached


def resolve_hook(provider: NativeHarnessProvider, hook: str) -> Any | None:
    """Resolve one named hook on a provider, or ``None`` if it is unset.

    ``hook`` is a field name on :class:`NativeHarnessProvider` (e.g.
    ``"run_native"``, ``"auto_create_terminal"``). Optional hooks that the
    provider leaves ``None`` resolve to ``None`` rather than raising, so callers
    can treat "no such hook yet" and "hook present" uniformly.
    """
    import_path = getattr(provider, hook)
    if import_path is None:
        return None
    return resolve(import_path)


def resolve_hook_for_key(key: str, hook: str) -> Any | None:
    """Resolve a hook by native-agent ``key``, or ``None`` if unknown/unset."""
    provider = native_provider_for_key(key)
    if provider is None:
        return None
    return resolve_hook(provider, hook)


def reset_resolve_cache_for_tests() -> None:
    """Clear the per-path resolution cache."""
    _RESOLVE_CACHE.clear()
