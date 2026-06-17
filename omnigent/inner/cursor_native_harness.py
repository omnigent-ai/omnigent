"""``harness: cursor-native`` wrap (the native Cursor TUI).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"cursor-native"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.cursor_native_executor.CursorNativeExecutor`,
which injects web-UI messages into the running ``cursor-agent`` TUI (launched by
``omnigent cursor`` in the session terminal) via tmux. The bridge dir is read
from :data:`~omnigent.cursor_native_bridge.BRIDGE_DIR_ENV_VAR` in the spawn env.
"""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.cursor_native_executor import CursorNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_cursor_native_executor() -> Executor:
    """Construct a :class:`CursorNativeExecutor` (reads the bridge dir from env)."""
    return CursorNativeExecutor()


def create_app() -> FastAPI:
    """Build the cursor-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_cursor_native_executor)
    return adapter.build()
