"""``harness: grok-native`` wrap for the native Grok Build server."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.grok_native_executor import GrokNativeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_grok_native_executor() -> Executor:
    """Construct the grok-native bridge executor from the spawn environment."""
    return GrokNativeExecutor()


def create_app() -> FastAPI:
    """Build the ``grok-native`` harness FastAPI app."""
    adapter = ExecutorAdapter(executor_factory=_build_grok_native_executor)
    return adapter.build()
