"""``harness: antigravity-native`` wrap for the native Antigravity TUI."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.antigravity_native_executor import AntigravityNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_antigravity_native_executor() -> Executor:
    """
    Construct the native Antigravity bridge executor.

    :returns: An :class:`AntigravityNativeExecutor` configured from the
        harness spawn environment.
    """
    return AntigravityNativeExecutor()


def create_app() -> FastAPI:
    """
    Build the ``antigravity-native`` harness FastAPI app.

    :returns: The FastAPI app from :class:`ExecutorAdapter`.
    """
    adapter = ExecutorAdapter(executor_factory=_build_antigravity_native_executor)
    return adapter.build()
