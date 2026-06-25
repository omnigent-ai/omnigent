"""``harness: hermes-native`` wrap (the native Hermes TUI).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"hermes-native"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.hermes_native_executor.HermesNativeExecutor`, which
injects web-UI messages into the running ``hermes`` TUI (launched by
``omnigent hermes`` in the session terminal) via tmux. The bridge dir is read from
:data:`~omnigent.hermes_native_bridge.BRIDGE_DIR_ENV_VAR` in the spawn env.

Tool policies: Omnigent's PreToolUse/PostToolUse policy gates (which the headless
``hermes`` harness enforces via Hermes' ``pre_tool_call`` shell hook) do NOT apply
to hermes-native — ``hermes`` runs its tools inside its own TUI and gates them with
its own in-terminal approval prompts, which Omnigent does not intercept. Treat the
Hermes TUI's own approval as the sole tool gate (same stance as goose-native).
"""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.hermes_native_executor import HermesNativeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_hermes_native_executor() -> Executor:
    """Construct a :class:`HermesNativeExecutor` (reads the bridge dir from env)."""
    return HermesNativeExecutor()


def create_app() -> FastAPI:
    """Build the hermes-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_hermes_native_executor)
    return adapter.build()
