"""``harness: cline-native`` wrap for the native Cline hub session."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.cline_native_executor import ClineNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_cline_native_executor() -> Executor:
    """Construct the cline-native bridge executor from the spawn environment."""
    return ClineNativeExecutor()


def create_app() -> FastAPI:
    """Build the ``cline-native`` harness FastAPI app."""
    adapter = ExecutorAdapter(executor_factory=_build_cline_native_executor)
    return adapter.build()
