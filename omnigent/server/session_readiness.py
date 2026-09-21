"""Observe usable sessions without blocking creation or changing startup behavior."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.debug_logging import debug_event, runner_log_scope

if TYPE_CHECKING:
    from omnigent.entities import Conversation
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.server.routes._sessions.common import _RelayHandle
    from omnigent.stores.conversation_store import ConversationStore

_logger = logging.getLogger(__name__)


@dataclass
class _Observation:
    runner_id: str
    connection: object
    task: asyncio.Task[None]


class SessionReadinessObserver:
    """Require init, a live relay, and runner-confirmed input on one connection."""

    def __init__(
        self,
        registry: TunnelRegistry,
        store: ConversationStore,
        get_relay: Callable[[str], _RelayHandle | None],
        *,
        timeout: float = 300,
        poll_interval: float = 1,
        enabled: Callable[[], bool] = lambda: True,
        start_relay: Callable[[str, str, httpx.AsyncClient], None] | None = None,
    ) -> None:
        self._closed = False
        self._enabled = enabled
        self._start_relay = start_relay
        self._registry = registry
        self._store = store
        self._get_relay = get_relay
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._observations: WorkspaceScopedCache[str, _Observation] = WorkspaceScopedCache()
        self._tasks: set[asyncio.Task[None]] = set()

    def connection_for(self, runner_id: str | None) -> object | None:
        """Capture the tunnel before an initialization request is sent."""
        return self._registry.get(runner_id) if runner_id is not None else None

    def initialized(
        self, conversation: Conversation, client: httpx.AsyncClient, connection: object
    ) -> None:
        if self._closed or not self._enabled():
            return
        runner_id = conversation.runner_id
        if runner_id is None or self._registry.get(runner_id) is not connection:
            return
        previous = self._observations.get(conversation.id)
        if previous is not None:
            if previous.runner_id == runner_id and previous.connection is connection:
                return
            previous.task.cancel()
        with runner_log_scope(conversation.id, runner_id):
            task = asyncio.create_task(
                self._observe(conversation.id, runner_id, connection, client),
                name=f"session-readiness-{conversation.id}",
            )
        self._observations[conversation.id] = _Observation(runner_id, connection, task)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def invalidate_runner(self, runner_id: str) -> None:
        for session_id, observation in list(self._observations.items()):
            if observation.runner_id == runner_id:
                observation.task.cancel()
                self._observations.pop(session_id, None)

    async def shutdown(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._observations.clear()

    async def _observe(
        self, session_id: str, runner_id: str, connection: object, client: httpx.AsyncClient
    ) -> None:
        stage = "relay_ready"
        try:
            if self._start_relay is not None:
                self._start_relay(session_id, runner_id, client)
            async with asyncio.timeout(self._timeout):
                while self._registry.get(runner_id) is connection:
                    relay = self._get_relay(session_id)
                    if (
                        relay is not None
                        and relay.runner_id == runner_id
                        and relay.connection is connection
                        and not relay.task.done()
                        and relay.ready.is_set()
                    ):
                        stage = "input_ready"
                        try:
                            response = await client.get(
                                f"/v1/sessions/{session_id}/readiness", timeout=5
                            )
                            if response.status_code == 404:
                                self._unavailable("runner_readiness_unsupported")
                                return
                            response.raise_for_status()
                            payload = response.json()
                            if not isinstance(payload, dict):
                                raise ValueError("Invalid readiness response")
                            if payload.get("supported") is False:
                                self._unavailable("native_readiness_unsupported")
                                return
                            if (
                                payload.get("initialized") is True
                                and payload.get("input_ready") is True
                            ):
                                current = await asyncio.to_thread(
                                    self._store.get_conversation, session_id
                                )
                                # Every await can race a replacement, relay loss, or deletion.
                                if (
                                    current is not None
                                    and current.runner_id == runner_id
                                    and self._registry.get(runner_id) is connection
                                    and self._get_relay(session_id) is relay
                                    and not relay.task.done()
                                    and relay.ready.is_set()
                                ):
                                    _logger.info(
                                        "Session runner is ready for input",
                                        extra=debug_event(
                                            "session_runner_ready",
                                            session_id=session_id,
                                            runner_id=runner_id,
                                            stage="ready",
                                            harness=payload.get("harness"),
                                        ),
                                    )
                                    return
                                if current is None or current.runner_id != runner_id:
                                    return
                        except (httpx.HTTPError, ConnectionError, ValueError):
                            # Startup probes can race runner initialization or tunnel recovery.
                            pass
                    else:
                        stage = "relay_ready"
                    await asyncio.sleep(self._poll_interval)
        except TimeoutError:
            _logger.error(
                "Session readiness was not observed before the deadline",
                extra=debug_event(
                    "session_readiness_timeout", stage=stage, error_code="readiness_timeout"
                ),
            )
        except Exception:
            _logger.exception(
                "Session readiness observation failed",
                extra=debug_event("session_readiness_observation_failed", stage=stage),
            )

    @staticmethod
    def _unavailable(error_code: str) -> None:
        _logger.info(
            "Session readiness cannot be confirmed by this runner",
            extra=debug_event(
                "session_readiness_unavailable", stage="input_ready", error_code=error_code
            ),
        )
