"""``harness: opencode-native`` wrap for the native OpenCode server."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.core.executor import Executor
from omnigent.harnesses.opencode_native.executor import OpenCodeNativeExecutor
from omnigent.harnesses.runtime._executor_adapter import ExecutorAdapter


def _build_opencode_native_executor() -> Executor:
    """
    Construct the native OpenCode bridge executor.

    :returns: An :class:`OpenCodeNativeExecutor` configured from the
        harness spawn environment.
    """
    return OpenCodeNativeExecutor()


def create_app() -> FastAPI:
    """
    Build the ``opencode-native`` harness FastAPI app.

    :returns: The FastAPI app from :class:`ExecutorAdapter`.
    """
    adapter = ExecutorAdapter(executor_factory=_build_opencode_native_executor)
    return adapter.build()
