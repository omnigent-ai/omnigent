"""``harness: commandcode-native`` wrap (the native Command Code TUI).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"commandcode-native"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.commandcode_native_executor.CommandCodeNativeExecutor`,
which injects web-UI messages into the running ``cmd`` TUI (launched by
``omnigent commandcode`` in the session terminal) via tmux. The bridge dir is
read from :data:`~omnigent.harnesses.commandcode_native.bridge.BRIDGE_DIR_ENV_VAR`
in the spawn env.

Tool policies: commandcode-native enforces Omnigent's tool policies via a
generated per-session Command Code **mod** (``--mod``): ``beforeToolCall`` posts
a DENY verdict from the Omnigent policy endpoint, and ``onPermissionRequest``
mirrors Command Code's consent prompt to the web UI and returns allow/deny.
"""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.commandcode_native_executor import CommandCodeNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_commandcode_native_executor() -> Executor:
    """Construct a :class:`CommandCodeNativeExecutor` (reads the bridge dir from env)."""
    return CommandCodeNativeExecutor()


def create_app() -> FastAPI:
    """Build the commandcode-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_commandcode_native_executor)
    return adapter.build()
