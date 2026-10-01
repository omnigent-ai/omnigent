"""``harness: codex-native`` wrap for the native Codex TUI."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.core.executor import Executor
from omnigent.harnesses.codex_native.executor import CodexNativeExecutor
from omnigent.harnesses.runtime._executor_adapter import ExecutorAdapter


def _build_codex_native_executor() -> Executor:
    """
    Construct the native Codex bridge executor.

    :returns: A :class:`CodexNativeExecutor` configured from the
        harness spawn environment.
    """
    return CodexNativeExecutor()


def create_app() -> FastAPI:
    """
    Build the ``codex-native`` harness FastAPI app.

    :returns: The FastAPI app from :class:`ExecutorAdapter`.
    """
    adapter = ExecutorAdapter(executor_factory=_build_codex_native_executor)
    return adapter.build()
