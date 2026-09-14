"""``harness: antigravity-native`` wrap for the native Antigravity TUI."""

from __future__ import annotations

from fastapi import FastAPI, Response

from omnigent.inner.antigravity_native_executor import AntigravityNativeExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import HarnessApp, TurnContext
from omnigent.server.schemas import CreateResponseRequest


def _build_antigravity_native_executor() -> Executor:
    """
    Construct the native Antigravity bridge executor.

    :returns: An :class:`AntigravityNativeExecutor` configured from the
        harness spawn environment.
    """
    return AntigravityNativeExecutor()


class AntigravityNativeExecutorAdapter(ExecutorAdapter):
    """Executor adapter that retains native cancellation ownership across teardown."""

    def __init__(self) -> None:
        self._native_executor: Executor | None = None
        super().__init__(executor_factory=self._build_retained_executor)

    def _build_retained_executor(self) -> Executor:
        if self._native_executor is None:
            self._native_executor = _build_antigravity_native_executor()
        return self._native_executor

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        if ctx.cancelled.is_set():
            return
        await super().run_turn(request, ctx)

    async def _handle_interrupt_event(self) -> Response:
        if self._in_flight:
            await HarnessApp._handle_interrupt_event(self)
        executor = self._build_retained_executor()
        if await executor.interrupt_session(self._session_key):
            return Response(status_code=204)
        return Response(status_code=503)


def create_app() -> FastAPI:
    """
    Build the ``antigravity-native`` harness FastAPI app.

    :returns: The FastAPI app from :class:`ExecutorAdapter`.
    """
    adapter = AntigravityNativeExecutorAdapter()
    return adapter.build()
