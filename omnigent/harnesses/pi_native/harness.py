"""``harness: pi-native`` wrap for the native Pi TUI."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.core.executor import Executor
from omnigent.harnesses.pi_native.executor import PiNativeExecutor
from omnigent.harnesses.runtime._executor_adapter import ExecutorAdapter


def _build_pi_native_executor() -> Executor:
    """
    Construct the native Pi bridge executor.

    :returns: A :class:`PiNativeExecutor` configured from the harness
        spawn environment.
    """
    return PiNativeExecutor()


def create_app() -> FastAPI:
    """
    Build the ``pi-native`` harness FastAPI app.

    :returns: The FastAPI app from :class:`ExecutorAdapter`.
    """
    adapter = ExecutorAdapter(executor_factory=_build_pi_native_executor)
    return adapter.build()
