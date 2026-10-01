"""``harness: claude-native`` wrap for the native Claude Code UI."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.core.executor import Executor
from omnigent.harnesses.claude_native.executor import ClaudeNativeExecutor
from omnigent.harnesses.runtime._executor_adapter import ExecutorAdapter


def _build_claude_native_executor() -> Executor:
    """
    Construct the native Claude Code bridge executor.

    :returns: A :class:`ClaudeNativeExecutor` configured from the
        harness spawn environment.
    """
    return ClaudeNativeExecutor()


def create_app() -> FastAPI:
    """
    Build the ``claude-native`` harness FastAPI app.

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s
        :meth:`build` method.
    """
    adapter = ExecutorAdapter(executor_factory=_build_claude_native_executor)
    return adapter.build()
