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
import hashlib
import json
import logging
from builtins import ExceptionGroup
from collections.abc import Awaitable, Callable, Collection, Iterable
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import httpx

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.db.utils import now_epoch
from omnigent.entities import Conversation, MessageData
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
    from omnigent.onboarding.sandboxes.base import SandboxHostLauncher
    from omnigent.onboarding.sandboxes.types import ResumeOutcome
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
    status = conv.live_status
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
        if not (
            host_store is not None
            and conv.live_status == "failed"
            and conv.labels.get(LAST_TASK_ERROR_CODE_LABEL_KEY) == "runner_failed_to_start"
            and await asyncio.to_thread(host_store.get_session_recovery, conv.id) is not None
        ):
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
class ManagedRecoveryContext:
    """Provider and outcome writer owned by one fenced automatic attempt."""

    host_id: str
    sandbox_id: str | None
    launcher: SandboxHostLauncher
    record_outcome: Callable[[ResumeOutcome], Awaitable[None]]
    refusal: str | None = None


managed_recovery_context: ContextVar[ManagedRecoveryContext | None] = ContextVar(
    "managed_recovery_context", default=None
)
_LEASE_S = 180
_ATTEMPT_TIMEOUT_S = 600


class InterruptedSessionRecoverer:
    """Recover interrupted roots using provider authority and durable retry ownership."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        host_store: HostStore,
        sandbox_config: ManagedSandboxDeployment,
        app_state: Any,
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
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="interrupted-session-recoverer")

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def sweep_once(self, *, now: int | None = None) -> int:
        reference_time = self._clock() if now is None else now
        seen: set[tuple[int, str]] = set()
        by_host: dict[tuple[int, str], list[str]] = {}
        cursor: InterruptedSessionCursor | None = None
        while True:
            page, cursor = await asyncio.to_thread(
                self._conversation_store.list_interrupted_host_session_candidates,
                stale_before=reference_time - RUNNER_LIVENESS_TTL_S,
                after=cursor,
                limit=self._scan_batch_size,
            )
            for workspace_id, session_id, host_id in page:
                key = (workspace_id, session_id)
                if key not in seen:
                    seen.add(key)
                    by_host.setdefault((workspace_id, host_id), []).append(session_id)
            if cursor is None:
                break
        semaphore = asyncio.Semaphore(self._max_concurrent_hosts)

        async def recover_host(workspace_id: int, host_id: str, sessions: list[str]) -> int:
            async with semaphore:
                attempts = 0
                with workspace_scope(workspace_id):
                    for session_id in sessions:
                        attempted, _ = await self._recover_session(
                            workspace_id, session_id, host_id, reference_time
                        )
                        attempts += int(attempted)
                return attempts

        return sum(
            await asyncio.gather(
                *(
                    recover_host(workspace_id, host_id, sessions)
                    for (workspace_id, host_id), sessions in by_host.items()
                )
            )
        )

    async def resume_connected(
        self, conv: Conversation, client: httpx.AsyncClient
    ) -> httpx.Response | None:
        """Reconnects share the worker's budget and never race its pending wake."""
        if conv.host_id is None:
            return None
        _, response = await self._recover_session(
            current_workspace_id(), conv.id, conv.host_id, self._clock(), client=client
        )
        return response

    async def _episode(self, session_id: str) -> str:
        # Native transcript echoes alone cannot authorize another retry budget.
        conv = await asyncio.to_thread(self._conversation_store.get_conversation, session_id)
        retry = conv.labels.get("omnigent.recovery.manual_retry", "") if conv else ""
        prefix = f"{session_id}:{retry}"
        cursor = None
        while True:
            page = await asyncio.to_thread(
                self._conversation_store.list_items,
                session_id,
                type="message",
                order="desc",
                limit=100,
                after=cursor,
            )
            for item in page.data:
                if (
                    isinstance(item.data, MessageData)
                    and item.data.role == "user"
                    and not item.data.is_meta
                    and item.data.user_authored
                ):
                    return hashlib.sha256(f"{prefix}:{item.id}".encode()).hexdigest()
            if not page.has_more or not page.data:
                return hashlib.sha256(prefix.encode()).hexdigest()
            cursor = page.data[-1].id

    async def _eligible(self, conv: Conversation, episode: str, now: int) -> bool:
        if not await preserves_interruption(conv, self._sandbox_config, self._host_store, now=now):
            return False
        if interrupted_mid_turn(conv):
            return True
        if (
            conv.live_status == "failed"
            and conv.labels.get(LAST_TASK_ERROR_CODE_LABEL_KEY) == "runner_failed_to_start"
        ):
            previous = await asyncio.to_thread(self._host_store.get_session_recovery, conv.id)
            return previous is not None and previous.episode_key == episode
        return False

    async def _recover_session(
        self,
        workspace_id: int,
        session_id: str,
        host_id: str,
        now: int,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[bool, httpx.Response | None]:
        from omnigent.onboarding.sandboxes.base import SandboxRecoveryRefusedError
        from omnigent.onboarding.sandboxes.types import (
            RecoveryTarget,
            RestoredState,
            ResumeOutcome,
        )
        from omnigent.server.managed_hosts import resolve_managed_host_launcher
        from omnigent.server.routes._sessions.orchestration import ensure_runner_connected

        attempted = False
        try:
            host = await asyncio.to_thread(self._host_store.get_host, host_id)
            if not _host_is_managed(host):
                return False, None
            assert host is not None
            launcher = resolve_managed_host_launcher(host, self._sandbox_config)
            if launcher is None:
                return False, None
            target = RecoveryTarget(
                workspace_id=workspace_id,
                owner_id=host.user_id,
                session_id=session_id,
                host_id=host_id,
                sandbox_id=host.sandbox_id,
            )
            authority = launcher.background_recovery_context(target)
            if authority is None:
                return False, None
            async with authority:
                now = max(now, self._clock())
                conv = await asyncio.to_thread(
                    self._conversation_store.get_conversation, session_id
                )
                if conv is None or conv.host_id != host_id:
                    return False, None
                episode = await self._episode(session_id)
                if not await self._eligible(conv, episode, now):
                    return False, None
                claim = await asyncio.to_thread(
                    self._host_store.claim_session_recovery,
                    host_id,
                    session_id,
                    episode,
                    now=now,
                    lease_s=_LEASE_S,
                    max_attempts=self._sandbox_config.auto_resume_interrupted.max_attempts,
                    retry_backoff_s=self._sandbox_config.auto_resume_interrupted.sweep_interval_s,
                )
                if claim is None:
                    previous = await asyncio.to_thread(
                        self._host_store.get_session_recovery, session_id
                    )
                    if (
                        previous is not None
                        and previous.episode_key == episode
                        and previous.lease_until is None
                    ):
                        code = previous.terminal_reason
                        if (
                            previous.attempts
                            >= self._sandbox_config.auto_resume_interrupted.max_attempts
                        ):
                            code = code or "exhausted"
                        reason = {
                            "exhausted": (
                                f"Automatic recovery stopped after {previous.attempts} attempts. "
                                "Send a message to retry."
                            ),
                            "outcome_unknown": (
                                "Automatic recovery was interrupted. Check the provider's state "
                                "before sending a message to retry."
                            ),
                        }.get(code or "")
                        if reason is not None:
                            await self._show_failure(session_id, host_id, episode, reason)
                    return False, None
                attempted = True
                outcome = ResumeOutcome()
                previous = await asyncio.to_thread(
                    self._host_store.get_session_recovery, session_id
                )
                if previous is not None and previous.outcome_json is not None:
                    with suppress(ValueError, TypeError, KeyError):
                        facts = json.loads(previous.outcome_json)
                        if isinstance(facts, dict) and facts.get("sandbox_id") == host.sandbox_id:
                            outcome = ResumeOutcome(
                                workspace=RestoredState(**facts["workspace"]),
                                harness=RestoredState(**facts["harness"]),
                            )
                response = None
                reason = None
                reason_code = None

                async def record_outcome(value: ResumeOutcome) -> None:
                    nonlocal outcome
                    if not await asyncio.to_thread(
                        self._host_store.save_session_recovery_outcome,
                        session_id,
                        claim,
                        json.dumps({**asdict(value), "sandbox_id": host.sandbox_id}),
                    ):
                        raise RuntimeError("Recovery ownership changed during wake")
                    outcome = value

                async def renew() -> None:
                    while True:
                        await asyncio.sleep(_LEASE_S // 3)
                        if not await asyncio.to_thread(
                            self._host_store.renew_session_recovery,
                            session_id,
                            claim,
                            now=self._clock(),
                            lease_s=_LEASE_S,
                        ):
                            raise RuntimeError("Recovery ownership expired")

                attempt_context = ManagedRecoveryContext(
                    host_id, host.sandbox_id, launcher, record_outcome
                )
                token = managed_recovery_context.set(attempt_context)
                try:
                    async with asyncio.timeout(_ATTEMPT_TIMEOUT_S), asyncio.TaskGroup() as group:
                        heartbeat = group.create_task(renew())
                        try:
                            # Revalidate after claiming, before any provider side effect.
                            fresh = await asyncio.to_thread(
                                self._conversation_store.get_conversation, session_id
                            )
                            if (
                                fresh is None
                                or fresh.host_id != host_id
                                or not await self._eligible(fresh, episode, now)
                                or await self._episode(session_id) != episode
                            ):
                                return True, None
                            if client is None:
                                client, current = await ensure_runner_connected(
                                    session_id=session_id,
                                    conv=fresh,
                                    app_state=self._app_state,
                                    conversation_store=self._conversation_store,
                                    runner_router=self._runner_router,
                                    raise_host_refusal=True,
                                )
                            else:
                                current = fresh
                            if client is None:
                                raise ConnectionError("Replacement runner did not connect")
                            # Reconnects retain the previous wake's restoration facts.
                            await record_outcome(outcome)
                            response = await self._request_continuation(
                                current, client, now, episode, claim, outcome
                            )
                        finally:
                            tracker = getattr(self._app_state, "managed_launches", None)
                            launch = tracker.get(session_id) if tracker is not None else None
                            task = launch.task if launch is not None else None
                            if task is not None and not task.done():
                                try:
                                    owner = asyncio.current_task()
                                    if owner is not None and owner.cancelling():
                                        task.cancel()
                                        await asyncio.gather(task, return_exceptions=True)
                                        assert tracker is not None
                                        tracker.fail(
                                            session_id, "Automatic recovery was interrupted"
                                        )
                                        raise asyncio.CancelledError
                                    await asyncio.shield(task)
                                except asyncio.CancelledError:
                                    task.cancel()
                                    await asyncio.gather(task, return_exceptions=True)
                                    raise
                            heartbeat.cancel()
                except asyncio.CancelledError:
                    reason_code = "outcome_unknown"
                    raise
                except TimeoutError:
                    reason_code = "outcome_unknown"
                    reason = (
                        "Automatic recovery timed out. The provider may still be restarting. "
                        "Check its state before sending a message to retry."
                    )
                except Exception as exc:  # noqa: BLE001 - one attempt must not stop the sweep
                    failures = list(exc.exceptions) if isinstance(exc, ExceptionGroup) else [exc]
                    refusal = next(
                        (
                            failure
                            for failure in failures
                            if isinstance(failure, SandboxRecoveryRefusedError)
                        ),
                        None,
                    )
                    record = await asyncio.to_thread(
                        self._host_store.get_session_recovery, session_id
                    )
                    if refusal is not None or attempt_context.refusal is not None:
                        reason_code = "refused"
                        reason = attempt_context.refusal or str(refusal)
                    elif (
                        record is not None
                        and record.attempts
                        >= self._sandbox_config.auto_resume_interrupted.max_attempts
                    ):
                        reason_code = "exhausted"
                        reason = (
                            f"Automatic recovery stopped after {record.attempts} attempts. "
                            "Send a message to retry."
                        )
                    _logger.warning(
                        "Interrupted session %s recovery failed", session_id, exc_info=True
                    )
                finally:
                    managed_recovery_context.reset(token)
                    finished = await asyncio.to_thread(
                        self._host_store.finish_session_recovery,
                        session_id,
                        claim,
                        terminal_reason=reason_code,
                    )
                    if finished and reason is not None:
                        await self._show_failure(session_id, host_id, episode, reason)
                return True, response
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - isolate provider and tenant failures
            _logger.warning("Cannot recover interrupted session %s", session_id, exc_info=True)
            return attempted, None

    async def _show_failure(
        self, session_id: str, host_id: str, episode: str, reason: str
    ) -> None:
        from omnigent.server.routes._sessions.helpers import reconcile_orphaned_running_status
        from omnigent.server.routes.sessions import _publish_sandbox_status

        conv = await asyncio.to_thread(self._conversation_store.get_conversation, session_id)
        if conv is None or conv.host_id != host_id or await self._episode(session_id) != episode:
            return
        if conv.live_status in {"running", "waiting"}:
            if not await asyncio.to_thread(
                reconcile_orphaned_running_status,
                session_id,
                self._conversation_store,
                self._clock() - RUNNER_LIVENESS_TTL_S,
                preserve_interruption=True,
                owner_runner_id=conv.runner_id,
            ):
                return
            current = await asyncio.to_thread(
                self._conversation_store.get_conversation, session_id
            )
            if current is None or (current.host_id, current.runner_id) != (
                host_id,
                conv.runner_id,
            ):
                return
            conv = current
        if (
            conv.live_status != "failed"
            or conv.labels.get("omnigent.last_task_error_message") == reason
        ):
            return
        await asyncio.to_thread(
            self._conversation_store.set_labels,
            session_id,
            {"omnigent.last_task_error_message": reason},
        )
        _publish_sandbox_status(session_id, "failed", reason)

    async def _request_continuation(
        self,
        snapshot: Conversation,
        client: httpx.AsyncClient,
        now: int,
        episode: str,
        claim: str,
        outcome: ResumeOutcome,
    ) -> httpx.Response | None:
        from omnigent.runtime.recovery import compose_recovery_prompt
        from omnigent.server.routes.sessions import _publish_runner_recovered_status
        from omnigent.server.runner_session_init import is_session_agent_removed

        initializer = getattr(self._app_state, "runner_session_initializer", None)
        context = managed_recovery_context.get()
        assert context is not None
        host = await asyncio.to_thread(self._host_store.get_host, context.host_id)
        conv = await asyncio.to_thread(self._conversation_store.get_conversation, snapshot.id)
        if (
            initializer is None
            or host is None
            or host.sandbox_id != context.sandbox_id
            or conv is None
            or (conv.host_id, conv.runner_id) != (snapshot.host_id, snapshot.runner_id)
            or await self._episode(conv.id) != episode
            or not await self._eligible(conv, episode, now)
        ):
            return None
        if not await asyncio.to_thread(
            self._host_store.renew_session_recovery,
            conv.id,
            claim,
            now=max(now, self._clock()),
            lease_s=_LEASE_S,
        ):
            raise RuntimeError("Recovery ownership expired before continuation")
        response = await initializer.initialize(
            conv,
            client,
            timeout=10.0,
            resume_interrupted_turn=True,
            recovery_prompt=compose_recovery_prompt(
                outcome, self._sandbox_config.auto_resume_interrupted.recovery_prompt
            ),
            recovery_id=claim,
        )
        if is_session_agent_removed(response):
            return response
        response.raise_for_status()
        if not init_started_turn(response):
            await _publish_runner_recovered_status(
                conv.id, self._conversation_store, require_disconnect_code=True
            )
        return response

    async def _run(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Interrupted session recovery sweep failed; retrying later")
            await asyncio.sleep(self._sandbox_config.auto_resume_interrupted.sweep_interval_s)
