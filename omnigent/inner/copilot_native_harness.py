"""``harness: copilot-native`` wrap for the native GitHub Copilot TUI."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.copilot_native_executor import CopilotNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_copilot_native_executor() -> Executor:
    """Construct the native Copilot bridge executor."""
    return CopilotNativeExecutor()


def create_app() -> FastAPI:
    """Build the ``copilot-native`` harness FastAPI app."""
    adapter = ExecutorAdapter(executor_factory=_build_copilot_native_executor)
    return adapter.build()
