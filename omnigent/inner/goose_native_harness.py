"""``harness: goose-native`` wrap (the native Goose TUI).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"goose-native"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.goose_native_executor.GooseNativeExecutor`, which
injects web-UI messages into the running ``goose session`` TUI (launched by
``omnigent goose`` in the session terminal) via tmux. The bridge dir is read from
:data:`~omnigent.goose_native_bridge.BRIDGE_DIR_ENV_VAR` in the spawn env.

Tool policies: Omnigent's PreToolUse/PostToolUse policy gates (which claude- and
codex-native enforce via hooks) do NOT apply to goose-native — ``goose`` runs its
tools inside its own TUI and gates them with its own approval mode
(``GOOSE_MODE`` / in-terminal prompts), which Omnigent does not intercept. Treat
the Goose TUI's own approval as the sole tool gate.
"""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.goose_native_executor import GooseNativeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_goose_native_executor() -> Executor:
    """Construct a :class:`GooseNativeExecutor` (reads the bridge dir from env)."""
    return GooseNativeExecutor()


def create_app() -> FastAPI:
    """Build the goose-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_goose_native_executor)
    return adapter.build()
