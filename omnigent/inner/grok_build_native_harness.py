"""``harness: grok-build-native`` wrap (the native Grok Build TUI).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"grok-build-native"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.grok_build_native_executor.GrokBuildNativeExecutor`,
which injects web-UI messages into the running ``grok`` TUI (launched by
``omnigent grok-build`` in the session terminal) via tmux. The bridge dir is read
from :data:`~omnigent.grok_build_native_bridge.BRIDGE_DIR_ENV_VAR` in the spawn env.

Tool policies: Grok Build runs its tools inside its own TUI and gates them with
its own approval mode, which Omnigent does not intercept. Treat the Grok Build
TUI's own approval as the sole tool gate.
"""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.grok_build_native_executor import GrokBuildNativeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_grok_build_native_executor() -> Executor:
    """Construct a :class:`GrokBuildNativeExecutor` (reads the bridge dir from env)."""
    return GrokBuildNativeExecutor()


def create_app() -> FastAPI:
    """Build the grok-build-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_grok_build_native_executor)
    return adapter.build()