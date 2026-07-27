"""Server-owned coordination for runner session initialization."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx

from omnigent.entities import Conversation
from omnigent.runner.session_init_protocol import build_runner_session_init_payload

if TYPE_CHECKING:
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry


class RunnerSessionInitializer:
    """Share initialization readiness within one runner tunnel generation."""

    def __init__(self, registry: TunnelRegistry, *, server_version: str) -> None:
        self._registry = registry
        self._server_version = server_version
        self._tasks: dict[
            tuple[str, int, str, str, str | None],
            asyncio.Task[httpx.Response],
        ] = {}

    async def initialize(
        self,
        conversation: Conversation,
        runner_client: httpx.AsyncClient,
        *,
        timeout: float,
    ) -> httpx.Response:
        """Initialize once for the current connection and persisted snapshot."""
        runner_id = conversation.runner_id
        agent_id = conversation.agent_id
        if runner_id is None or agent_id is None:
            raise ValueError("runner session initialization requires runner_id and agent_id")
        connection = self._registry.get(runner_id)
        # Production routed clients always have a registry entry. The client
        # identity fallback keeps embedded/test transports usable without
        # weakening the real tunnel-generation key.
        generation = id(connection) if connection is not None else id(runner_client)
        key = (
            runner_id,
            generation,
            conversation.id,
            agent_id,
            conversation.sub_agent_name,
        )
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                runner_client.post(
                    "/v1/sessions",
                    json=build_runner_session_init_payload(
                        conversation,
                        server_version=self._server_version,
                    ),
                    timeout=timeout,
                ),
                name=f"runner-session-init-{conversation.id}",
            )
            self._tasks[key] = task

            def _drop_failed(done: asyncio.Task[httpx.Response]) -> None:
                if self._tasks.get(key) is not done:
                    return
                if done.cancelled():
                    self._tasks.pop(key, None)
                    return
                if done.exception() is not None:
                    self._tasks.pop(key, None)
                    return
                response = done.result()
                if response.status_code >= 400:
                    self._tasks.pop(key, None)

            task.add_done_callback(_drop_failed)
        try:
            response = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._tasks.get(key) is task:
                self._tasks.pop(key, None)
            raise
        if response.status_code >= 400 and self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        return response

    def invalidate_runner(self, runner_id: str) -> None:
        """Forget completed readiness when a runner tunnel goes away."""
        stale = [key for key in self._tasks if key[0] == runner_id]
        for key in stale:
            task = self._tasks.pop(key)
            if not task.done():
                task.cancel()
