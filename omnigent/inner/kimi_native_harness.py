"""``harness: kimi-native`` wrap (the native Kimi Code TUI).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"kimi-native"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.kimi_native_executor.KimiNativeExecutor`,
which injects web-UI messages into the running ``kimi`` TUI (launched by
``omnigent kimi`` in the session terminal) via tmux. The bridge dir is read
from :data:`~omnigent.kimi_native_bridge.BRIDGE_DIR_ENV_VAR` in the spawn env.

Tool policies: kimi-native enforces Omnigent's tool deny-policy via a
``PreToolUse`` hook (registered in the per-session ``config.toml`` built by
:mod:`omnigent.kimi_native_credentials`, dispatched to
:mod:`omnigent.kimi_native_hook`). A ``POLICY_ACTION_DENY`` verdict blocks the
tool with the policy reason; everything else is "no opinion", so ``kimi``'s own
in-TUI approval prompt still runs — the deployment's deny-gate and the user's
own consent are kept as two independent gates. A companion ``PermissionRequest``
hook surfaces the pending approval in the web UI read-only (the yes/no is
answered in the TUI, which Omnigent cannot intercept). Connector/tool ASK
policies are not enforced (kimi owns the ask); treat the kimi TUI as the
approval surface, with Omnigent able to hard-deny.
"""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.kimi_native_executor import KimiNativeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_kimi_native_executor() -> Executor:
    """Construct a :class:`KimiNativeExecutor` (reads the bridge dir from env)."""
    return KimiNativeExecutor()


def create_app() -> FastAPI:
    """Build the kimi-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_kimi_native_executor)
    return adapter.build()
