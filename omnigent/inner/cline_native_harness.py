"""``harness: cline-native`` wrap (the native Cline TUI).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"cline-native"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.cline_native_executor.ClineNativeExecutor`, which
injects web-UI messages into the running ``cline`` TUI (launched by ``omnigent
cline`` in the session terminal) via tmux. The bridge dir is read from
:data:`~omnigent.cline_native_bridge.BRIDGE_DIR_ENV_VAR` in the spawn env.

Tool policies: Omnigent's PreToolUse/PostToolUse policy gates (which claude- and
codex-native enforce via hooks) do NOT apply to cline-native — ``cline`` runs its
tools inside its own TUI and gates them with its own in-terminal plan/act
approval prompts (and its auto-approve setting), which omnigent does not
intercept. Treat the cline TUI's own approval as the sole tool gate; do not
assume Omnigent connector/tool deny-policies constrain a cline-native session.
"""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.cline_native_executor import ClineNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_cline_native_executor() -> Executor:
    """Construct a :class:`ClineNativeExecutor` (reads the bridge dir from env)."""
    return ClineNativeExecutor()


def create_app() -> FastAPI:
    """Build the cline-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_cline_native_executor)
    return adapter.build()
