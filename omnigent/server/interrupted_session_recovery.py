"""Resume top-level managed sessions that a host restart left mid-turn.

A host restart drops the runner and host tunnels without a terminal turn
status. Nothing relaunches ``omnigent host`` on its own, so interrupted
sessions only recover when something calls
:func:`~omnigent.server.routes._sessions.orchestration.ensure_runner_connected`.
:class:`InterruptedSessionRecoverer` does that periodically, and the runner
reconnect hook asks the new runner for a continuation turn
(:func:`auto_resume_requested`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation
from omnigent.server.child_session_recovery import session_is_restorable
from omnigent.server.managed_hosts import ManagedSandboxDeployment
from omnigent.stores.conversation_store import (
    LAST_TASK_ERROR_CODE_LABEL_KEY,
    RUNNER_LIVENESS_TTL_S,
    ConversationStore,
    InterruptedSessionCursor,
)
from omnigent.stores.host_store import Host, HostStore

if TYPE_CHECKING:
    from omnigent.runner.routing import RunnerRouter

_logger = logging.getLogger(__name__)

_DEFAULT_SCAN_BATCH_SIZE = 500
_DEFAULT_MAX_CONCURRENT_HOSTS = 4


def auto_resume_enabled(sandbox_config: ManagedSandboxDeployment | None) -> bool:
    """Whether the deployment opted into resuming interrupted managed sessions."""
    return sandbox_config is not None and sandbox_config.auto_resume_interrupted.enabled


def _top_level_host_session(
    conv: Conversation, sandbox_config: ManagedSandboxDeployment | None
) -> bool:
    return (
        auto_resume_enabled(sandbox_config)
        and conv.host_id is not None
        and conv.parent_conversation_id is None
    )


def managed_host_ids(host_store: HostStore, host_ids: Iterable[str]) -> set[str]:
    """Resolve which of *host_ids* are live managed hosts, one lookup per distinct id."""
    return {host_id for host_id in set(host_ids) if _host_is_managed(host_store.get_host(host_id))}


async def preserves_interruption(
    conv: Conversation,
    sandbox_config: ManagedSandboxDeployment | None,
    host_store: HostStore | None,
    *,
    now: int | None = None,
    managed_hosts: Collection[str] | None = None,
) -> bool:
    """Whether an orphaned turn should settle as a disconnect rather than idle.

    Only sessions auto-resume can still pick up qualify: recent, restorable,
    top-level sessions on a managed host. Callers checking many sessions pass
    *managed_hosts* (from :func:`managed_host_ids`) to skip the per-session lookup.
    """
    if (
        host_store is None
        or sandbox_config is None
        or not _top_level_host_session(conv, sandbox_config)
        or not session_is_restorable(conv)
        or not recent_enough(conv, sandbox_config, now=now_epoch() if now is None else now)
    ):
        return False
    assert conv.host_id is not None
    if managed_hosts is not None:
        return conv.host_id in managed_hosts
    return _host_is_managed(await asyncio.to_thread(host_store.get_host, conv.host_id))


def interrupted_mid_turn(conv: Conversation) -> bool:
    """Whether a runner loss cut off *conv*'s turn.

    Stricter than child recovery's check: a ``runner_failed_to_start`` failure
    can mark a session that never had a turn in flight, so it does not count.
    """
    from omnigent.server.routes._sessions.common import _session_status_cache

    status = _session_status_cache.get(conv.id, conv.live_status)
    return status in {"running", "waiting"} or (
        status == "failed"
        and conv.labels.get(LAST_TASK_ERROR_CODE_LABEL_KEY) == "runner_disconnected"
    )


def recent_enough(
    conv: Conversation, sandbox_config: ManagedSandboxDeployment, *, now: int
) -> bool:
    """Whether *conv* saw activity within the auto-resume age limit."""
    return conv.updated_at >= now - sandbox_config.auto_resume_interrupted.max_interrupted_age_s


async def auto_resume_requested(
    conv: Conversation,
    sandbox_config: ManagedSandboxDeployment | None,
    host_store: HostStore | None,
    *,
    now: int | None = None,
) -> bool:
    """Whether a reconnecting runner should continue this session's interrupted turn.

    Only recent top-level sessions on a managed host qualify; children are
    restored by their parent, and sessions stopped on purpose stay stopped.
    """
    if not interrupted_mid_turn(conv):
        return False
    return await preserves_interruption(conv, sandbox_config, host_store, now=now)


def _host_is_managed(host: Host | None) -> bool:
    return host is not None and host.sandbox_provider is not None and host.deleted_at is None


def init_started_turn(response: httpx.Response) -> bool:
    """Whether a runner's session-init reply reports a turn in flight."""
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "running"


@dataclass
class _Attempts:
    count: int = 0
    next_at: int = 0


class InterruptedSessionRecoverer:
    """Periodically wake the runners of interrupted top-level managed sessions.

    Each sweep pages :meth:`ConversationStore.list_interrupted_host_sessions`
    and calls ``ensure_runner_connected`` for every qualifying session, which
    reuses the managed wake/relaunch single-flight. The new runner's connect
    hook then sends the continuation turn. Hosts are woken concurrently;
    sessions sharing a host go one at a time so the first wake serves the rest.

    Attempts are capped per session with exponential backoff, tracked in this
    process. A session that leaves the interrupted set (it resumed, or the user
    acted on it) forgets its attempts, so a later interruption starts fresh.
    """

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        host_store: HostStore,
        sandbox_config: ManagedSandboxDeployment,
        app_state: Any,  # Starlette ``app.state`` namespace
        runner_router: RunnerRouter | None,
        clock: Callable[[], int] = now_epoch,
        scan_batch_size: int = _DEFAULT_SCAN_BATCH_SIZE,
        max_concurrent_hosts: int = _DEFAULT_MAX_CONCURRENT_HOSTS,
    ) -> None:
        if scan_batch_size <= 0:
            raise ValueError("scan_batch_size must be positive")
        if max_concurrent_hosts <= 0:
            raise ValueError("max_concurrent_hosts must be positive")
        self._conversation_store = conversation_store
        self._host_store = host_store
        self._sandbox_config = sandbox_config
        self._app_state = app_state
        self._runner_router = runner_router
        self._clock = clock
        self._scan_batch_size = scan_batch_size
        self._max_concurrent_hosts = max_concurrent_hosts
        self._attempts: dict[tuple[int, str], _Attempts] = {}
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start this server process's recovery loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="interrupted-session-recoverer")

    async def shutdown(self) -> None:
        """Stop the recovery loop and wait for cancellation to settle."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def sweep_once(self, *, now: int | None = None) -> int:
        """Run one cross-workspace sweep and return the number of wake attempts."""
        reference_time = self._clock() if now is None else now
        config = self._sandbox_config.auto_resume_interrupted
        seen: set[tuple[int, str]] = set()
        by_host: dict[tuple[int, str], list[Conversation]] = {}
        cursor: InterruptedSessionCursor | None = None
        while True:
            page, cursor = await asyncio.to_thread(
                self._conversation_store.list_interrupted_host_sessions,
                stale_before=reference_time - RUNNER_LIVENESS_TTL_S,
                after=cursor,
                limit=self._scan_batch_size,
            )
            for workspace_id, conv in page:
                # Too-old sessions are never retried, so they hold no attempts entry.
                if not recent_enough(conv, self._sandbox_config, now=reference_time):
                    continue
                key = (workspace_id, conv.id)
                seen.add(key)
                attempts = self._attempts.setdefault(key, _Attempts())
                if attempts.count >= config.max_attempts or reference_time < attempts.next_at:
                    continue
                with workspace_scope(workspace_id):
                    if not session_is_restorable(conv) or not interrupted_mid_turn(conv):
                        continue
                assert conv.host_id is not None
                by_host.setdefault((workspace_id, conv.host_id), []).append(conv)
            if cursor is None:
                break
        for key in self._attempts.keys() - seen:
            del self._attempts[key]

        semaphore = asyncio.Semaphore(self._max_concurrent_hosts)

        async def recover_host(workspace_id: int, host_id: str, convs: list[Conversation]) -> int:
            async with semaphore:
                with workspace_scope(workspace_id):
                    try:
                        host = await asyncio.to_thread(self._host_store.get_host, host_id)
                    except Exception:  # noqa: BLE001 - one broken host must not stop the sweep
                        _logger.warning(
                            "Cannot load host %s for interrupted sessions", host_id, exc_info=True
                        )
                        return 0
                    if not _host_is_managed(host):
                        return 0
                    attempted = 0
                    for conv in convs:
                        await self._recover_session(workspace_id, conv, reference_time)
                        attempted += 1
                    return attempted

        results = await asyncio.gather(
            *(recover_host(ws, host_id, convs) for (ws, host_id), convs in by_host.items())
        )
        return sum(results)

    async def _recover_session(self, workspace_id: int, conv: Conversation, now: int) -> None:
        from omnigent.server.routes._sessions.orchestration import ensure_runner_connected

        config = self._sandbox_config.auto_resume_interrupted
        attempts = self._attempts.setdefault((workspace_id, conv.id), _Attempts())
        attempts.count += 1
        attempts.next_at = now + config.sweep_interval_s * 2 ** (attempts.count - 1)
        _logger.info(
            "Waking interrupted session %s (attempt %s of %s)",
            conv.id,
            attempts.count,
            config.max_attempts,
            extra={"session_id": conv.id},
        )
        try:
            client, current = await ensure_runner_connected(
                session_id=conv.id,
                conv=conv,
                app_state=self._app_state,
                conversation_store=self._conversation_store,
                runner_router=self._runner_router,
            )
            if client is None:
                _logger.warning(
                    "Interrupted session %s has no reachable runner yet",
                    conv.id,
                    extra={"session_id": conv.id},
                )
                return
            await self._request_continuation(current, client, now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one broken host must not stop the sweep
            _logger.warning(
                "Failed to wake interrupted session %s",
                conv.id,
                exc_info=True,
                extra={"session_id": conv.id},
            )

    async def _request_continuation(
        self, conv: Conversation, client: httpx.AsyncClient, now: int
    ) -> None:
        """Ask a reachable runner to continue, retrying a resume its connect hook lost.

        The initializer single-flights per tunnel generation, so this shares the
        connect hook's request when the runner has only just connected.
        """
        initializer = getattr(self._app_state, "runner_session_initializer", None)
        if initializer is None or not await auto_resume_requested(
            conv, self._sandbox_config, self._host_store, now=now
        ):
            return
        from omnigent.server.routes.sessions import _publish_runner_recovered_status
        from omnigent.server.runner_session_init import is_session_agent_removed

        response = await initializer.initialize(
            conv,
            client,
            timeout=10.0,
            resume_interrupted_turn=True,
            recovery_prompt=self._sandbox_config.auto_resume_interrupted.recovery_prompt,
        )
        if is_session_agent_removed(response):
            return
        response.raise_for_status()
        if not init_started_turn(response):
            # The runner declined the continuation (it survived the outage, or the
            # turn already ran). Clear the disconnect the same way the connect hook does.
            await _publish_runner_recovered_status(
                conv.id, self._conversation_store, require_disconnect_code=True
            )

    async def _run(self) -> None:
        while True:
            try:
                attempted = await self.sweep_once()
                if attempted:
                    _logger.info("Interrupted session recoverer woke %s session(s)", attempted)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Interrupted session recovery sweep failed; retrying later")
            await asyncio.sleep(self._sandbox_config.auto_resume_interrupted.sweep_interval_s)
