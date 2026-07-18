"""``harness: coco-native`` wrap (the native Snowflake CoCo TUI).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"coco-native"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.coco_native_executor.CocoNativeExecutor`, which
injects web-UI messages into the running ``cortex`` TUI (launched by
``omnigent coco`` in the session terminal) via tmux. The bridge dir is read
from :data:`~omnigent.coco_native_bridge.BRIDGE_DIR_ENV_VAR` in the spawn env.

Tool policies: Omnigent's PreToolUse/PostToolUse policy gates do NOT apply to
coco-native — ``cortex`` runs its tools inside its own TUI and gates them with
its own three-tier approval system (confirm actions / plan / bypass), which
Omnigent does not intercept. Treat the CoCo TUI's own approval as the sole
tool gate.
"""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.coco_native_executor import CocoNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_coco_native_executor() -> Executor:
    """Construct a :class:`CocoNativeExecutor` (reads the bridge dir from env)."""
    return CocoNativeExecutor()


def create_app() -> FastAPI:
    """Build the coco-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_coco_native_executor)
    return adapter.build()
