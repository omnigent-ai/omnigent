"""Server-owned coordination for runner session initialization."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx

from omnigent.debug_logging import debug_event, runner_log_scope
from omnigent.entities import Conversation
from omnigent.errors import SESSION_AGENT_MISSING_MESSAGE, ErrorCategory, ErrorCode
from omnigent.runner.session_init_protocol import build_runner_session_init_payload

if TYPE_CHECKING:
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.stores.agent_store import AgentStore
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.file_store import FileStore


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


def is_session_agent_removed(response: httpx.Response) -> bool:
    """
    Whether *response* is the initializer skipping a session whose agent was removed.

    The runner could only reject that session, and it says so itself on the
    session's next message, so callers treat this as expected, not a failure.

    :param response: A response from :meth:`RunnerSessionInitializer.initialize`.
    :returns: ``True`` for the removed-agent response.
    """
    if response.status_code != 410:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("error") == ErrorCode.SESSION_AGENT_MISSING


_logger = logging.getLogger(__name__)


class RunnerSessionInitializer:
    """Share initialization readiness within one runner tunnel generation."""

    def __init__(
        self,
        registry: TunnelRegistry,
        *,
        server_version: str,
        conversation_store: ConversationStore | None = None,
        file_store: FileStore | None = None,
        agent_store: AgentStore | None = None,
    ) -> None:
        self._registry = registry
        self._server_version = server_version
        self._conversation_store = conversation_store
        self._file_store = file_store
        self._agent_store = agent_store
        self._tasks: dict[
            tuple[str, int, str, str, str | None, bool],
            asyncio.Task[httpx.Response],
        ] = {}
        self._recovery_ids: dict[tuple[str, int, str, str, str | None, bool], str] = {}

    def generation_for(self, runner_id: str, runner_client: httpx.AsyncClient) -> int:
        """Identify the current tunnel, or the client for embedded transports."""
        connection = self._registry.get(runner_id)
        return connection.generation if connection is not None else id(runner_client)

    def require_generation(
        self, runner_id: str, runner_client: httpx.AsyncClient, generation: int
    ) -> None:
        """Prevent delayed recovery work from following a replacement tunnel."""
        if self.generation_for(runner_id, runner_client) != generation:
            raise ConnectionError("runner tunnel changed during session recovery")

    async def initialize(
        self,
        conversation: Conversation,
        runner_client: httpx.AsyncClient,
        *,
        timeout: float,
        suppress_recovery_turn: bool = False,
        resume_interrupted_turn: bool = False,
        generation: int | None = None,
        store_slots: asyncio.Semaphore | None = None,
    ) -> httpx.Response:
        """Initialize once for the current connection and persisted snapshot."""
        runner_id = conversation.runner_id
        agent_id = conversation.agent_id
        if runner_id is None or agent_id is None:
            raise ValueError("runner session initialization requires runner_id and agent_id")
        if generation is None:
            generation = self.generation_for(runner_id, runner_client)
        self.require_generation(runner_id, runner_client, generation)
        key = (
            runner_id,
            generation,
            conversation.id,
            agent_id,
            conversation.sub_agent_name,
            resume_interrupted_turn,
        )
        task = self._tasks.get(key)
        if task is None and self._agent_store is not None:
            async with store_slots or nullcontext():
                agent = await asyncio.to_thread(self._agent_store.get, agent_id)
            if agent is None:
                # The user removed the agent (`omnigent agent remove`). The runner
                # could only reject this init; the session reports the removal on
                # its next message, so this is expected and not worth a failure.
                _logger.warning(
                    "Not initializing session %s on its runner: its agent %s was removed",
                    conversation.id,
                    agent_id,
                )
                return httpx.Response(
                    410,
                    json={
                        "error": ErrorCode.SESSION_AGENT_MISSING,
                        "detail": SESSION_AGENT_MISSING_MESSAGE,
                    },
                    request=httpx.Request("POST", "/v1/sessions"),
                )
            # Another caller may have started this initialization during the lookup.
            task = self._tasks.get(key)
        if task is None:
            recovery_id = (
                self._recovery_ids.setdefault(key, uuid4().hex)
                if resume_interrupted_turn
                else None
            )
            payload = build_runner_session_init_payload(
                conversation,
                server_version=self._server_version,
                suppress_recovery_turn=suppress_recovery_turn,
                resume_interrupted_turn=resume_interrupted_turn,
                recovery_id=recovery_id,
            )

            async def post_session_init() -> httpx.Response:
                if self._conversation_store is not None and self._file_store is not None:
                    from omnigent.server.routes._sessions.helpers import (
                        _filesystem_attachment_in_history,
                        require_filesystem_attachment_runtime,
                    )

                    async with store_slots or nullcontext():
                        attachment = await asyncio.to_thread(
                            _filesystem_attachment_in_history,
                            conversation.id,
                            self._conversation_store,
                            self._file_store,
                        )
                    if attachment is not None:
                        require_filesystem_attachment_runtime(
                            host_id=None,
                            runner_id=runner_id,
                            host_registry=None,
                            tunnel_registry=self._registry,
                        )
                self.require_generation(runner_id, runner_client, generation)
                return await self._post_initialize(
                    runner_client,
                    session_id=conversation.id,
                    runner_id=runner_id,
                    payload=payload,
                    timeout=timeout,
                    resume_interrupted_turn=resume_interrupted_turn,
                    suppress_recovery_turn=suppress_recovery_turn,
                    recovery_id=recovery_id,
                    generation=generation,
                )

            task = asyncio.create_task(
                post_session_init(),
                name=f"runner-session-init-{conversation.id}",
            )
            self._tasks[key] = task

            def _drop_failed(done: asyncio.Task[httpx.Response]) -> None:
                failed = done.cancelled() or done.exception() is not None
                if self._tasks.get(key) is not done:
                    return
                if failed:
                    self._tasks.pop(key, None)
                    return
                response = done.result()
                if response.status_code >= 400:
                    self._tasks.pop(key, None)

            task.add_done_callback(_drop_failed)
        try:
            response = await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and current is not None and not current.cancelling():
                # Retiring a shared attempt must not cancel its independent callers.
                raise ConnectionError("runner session initialization was cancelled") from None
            raise
        except Exception:
            if self._tasks.get(key) is task:
                self._tasks.pop(key, None)
            raise
        self.require_generation(runner_id, runner_client, generation)
        if not runner_inference_verified(conversation, response):
            response = httpx.Response(
                409,
                json={"error": "The runner did not accept this session's inference configuration"},
                request=httpx.Request("POST", "/v1/sessions"),
            )
        if response.status_code >= 400 and self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        return response

    def invalidate_session(
        self, session_id: str, *, runner_id: str | None = None
    ) -> list[asyncio.Task[httpx.Response]]:
        """Retire session readiness, optionally only for its former runner binding."""
        cancelled = []
        for key in list(self._tasks.keys() | self._recovery_ids.keys()):
            if key[2] == session_id and (runner_id is None or key[0] == runner_id):
                task = self._tasks.pop(key, None)
                if task is not None and not task.done():
                    task.cancel()
                    cancelled.append(task)
                self._recovery_ids.pop(key, None)
        return cancelled

    async def _post_initialize(
        self,
        runner_client: httpx.AsyncClient,
        *,
        session_id: str,
        runner_id: str,
        payload: dict[str, object],
        timeout: float,
        resume_interrupted_turn: bool,
        suppress_recovery_turn: bool,
        recovery_id: str | None,
        generation: int,
    ) -> httpx.Response:
        with runner_log_scope(session_id, runner_id):
            # The flags name the caller: neither set is the tunnel-reconnect
            # hook, resume is a sub-agent restore, suppress is a message forward.
            _logger.info(
                "Initializing runner session",
                extra=debug_event(
                    "runner_session_init_started",
                    stage="session_init",
                    resume_interrupted_turn=resume_interrupted_turn,
                    suppress_recovery_turn=suppress_recovery_turn,
                    recovery_id=recovery_id,
                ),
            )
            try:
                response = await runner_client.post(
                    "/v1/sessions",
                    json=payload,
                    timeout=timeout,
                    extensions={"runner_tunnel_generation": generation},
                )
            except Exception as exc:
                _logger.exception(
                    "Runner session initialization failed",
                    extra=debug_event(
                        "runner_session_init_failed",
                        stage="session_init",
                        # The request rides the runner's tunnel: a closed tunnel
                        # (ConnectionError) or offline runner (httpx.ConnectError)
                        # is the runner going away, not an upstream.
                        error_category=(
                            ErrorCategory.RUNNER.value
                            if isinstance(exc, (ConnectionError, httpx.TransportError))
                            else None
                        ),
                    ),
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

    def invalidate_runner(
        self, runner_id: str, *, generation: int | None = None
    ) -> list[asyncio.Task[httpx.Response]]:
        """Forget readiness and cancel work belonging to a retired connection."""
        for key in list(self._recovery_ids):
            if key[0] == runner_id and (generation is None or key[1] == generation):
                self._recovery_ids.pop(key)
        stale = [
            key
            for key in self._tasks
            if key[0] == runner_id and (generation is None or key[1] == generation)
        ]
        cancelled = []
        for key in stale:
            task = self._tasks.pop(key)
            if not task.done():
                task.cancel()
                cancelled.append(task)
        return cancelled
