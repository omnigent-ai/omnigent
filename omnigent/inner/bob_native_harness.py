"""``harness: bob-native`` wrap (the native IBM Bob Shell TUI).

Thin module exposing :func:`create_app`, the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes for ``"bob-native"``.

Tool policies: Omnigent's policy gates do NOT apply to bob-native. Bob runs its
tools inside its own TUI and gates them with its own approval dialog, which
Omnigent neither intercepts nor answers.
"""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.bob_native_executor import BobNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_bob_native_executor() -> Executor:
    """Construct a :class:`BobNativeExecutor` (reads the bridge dir from env)."""
    return BobNativeExecutor()


def create_app() -> FastAPI:
    """Build the bob-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_bob_native_executor)
    return adapter.build()
