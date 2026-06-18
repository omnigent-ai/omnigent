"""``harness: isaac-native`` wrap for the native Isaac terminal mirror."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.isaac_native_executor import IsaacNativeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_isaac_native_executor() -> Executor:
    """
    Construct the native Isaac no-op executor.

    :returns: An :class:`IsaacNativeExecutor`.
    """
    return IsaacNativeExecutor()


def create_app() -> FastAPI:
    """
    Build the ``isaac-native`` harness FastAPI app.

    :returns: The FastAPI app from :class:`ExecutorAdapter`.
    """
    adapter = ExecutorAdapter(executor_factory=_build_isaac_native_executor)
    return adapter.build()
