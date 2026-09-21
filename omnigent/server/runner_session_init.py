"""Server-owned coordination for runner session initialization."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx

from omnigent.debug_logging import debug_event, runner_log_scope
from omnigent.entities import Conversation
from omnigent.runner.session_init_protocol import build_runner_session_init_payload

if TYPE_CHECKING:
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry


def runner_inference_verified(conversation: Conversation, response: httpx.Response) -> bool:
    """Configured sessions require a runner that accepted their saved routing."""
    if conversation.inference_snapshot is None:
        return True
    if response.status_code >= 400:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("inference_config_verified") is True


_logger = logging.getLogger(__name__)


class RunnerSessionInitializer:
    """Share initialization readiness within one runner tunnel generation."""

    def __init__(self, registry: TunnelRegistry, *, server_version: str) -> None:
        self._registry = registry
        self._server_version = server_version
        self._tasks: dict[
            tuple[str, int, str, str, str | None, bool],
            asyncio.Task[httpx.Response],
        ] = {}
        self._recovery_ids: dict[tuple[str, int, str, str, str | None, bool], str] = {}

    async def initialize(
        self,
        conversation: Conversation,
        runner_client: httpx.AsyncClient,
        *,
        timeout: float,
        suppress_recovery_turn: bool = False,
        resume_interrupted_turn: bool = False,
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
            resume_interrupted_turn,
        )
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                self._post_initialize(
                    runner_client,
                    session_id=conversation.id,
                    runner_id=runner_id,
                    payload=build_runner_session_init_payload(
                        conversation,
                        server_version=self._server_version,
                        suppress_recovery_turn=suppress_recovery_turn,
                        resume_interrupted_turn=resume_interrupted_turn,
                        recovery_id=(
                            self._recovery_ids.setdefault(key, uuid4().hex)
                            if resume_interrupted_turn
                            else None
                        ),
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
        if not runner_inference_verified(conversation, response):
            response = httpx.Response(
                409,
                json={"error": "The runner did not accept this session's inference configuration"},
                request=httpx.Request("POST", "/v1/sessions"),
            )
        if response.status_code >= 400 and self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        return response

    def invalidate_session(self, session_id: str) -> None:
        """A new binding needs fresh readiness and a new continuation identity."""
        for key in list(self._tasks.keys() | self._recovery_ids.keys()):
            if key[2] == session_id:
                self._tasks.pop(key, None)
                self._recovery_ids.pop(key, None)

    async def _post_initialize(
        self,
        runner_client: httpx.AsyncClient,
        *,
        session_id: str,
        runner_id: str,
        payload: dict[str, object],
        timeout: float,
    ) -> httpx.Response:
        with runner_log_scope(session_id, runner_id):
            _logger.info(
                "Initializing runner session",
                extra=debug_event("runner_session_init_started", stage="session_init"),
            )
            try:
                response = await runner_client.post(
                    "/v1/sessions",
                    json=payload,
                    timeout=timeout,
                )
            except Exception:
                _logger.exception(
                    "Runner session initialization failed",
                    extra=debug_event("runner_session_init_failed", stage="session_init"),
                )
                raise
            failed = not 200 <= response.status_code < 300
            log = _logger.error if failed else _logger.info
            log(
                "Runner session initialization finished",
                extra=debug_event(
                    "runner_session_init_failed" if failed else "runner_session_initialized",
                    stage="session_init",
                    status_code=response.status_code,
                ),
            )
            return response

    def invalidate_runner(self, runner_id: str) -> None:
        """Forget completed readiness when a runner tunnel goes away."""
        for key in list(self._recovery_ids):
            if key[0] == runner_id:
                self._recovery_ids.pop(key)
        stale = [key for key in self._tasks if key[0] == runner_id]
        for key in stale:
            task = self._tasks.pop(key)
            if not task.done():
                task.cancel()
