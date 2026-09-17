"""``harness: devin-native`` wrap (the native Devin TUI)."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.devin_native_executor import DevinNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_devin_native_executor() -> Executor:
    """Construct a :class:`DevinNativeExecutor`."""
    return DevinNativeExecutor()


def create_app() -> FastAPI:
    """Build the devin-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_devin_native_executor)
    return adapter.build()
