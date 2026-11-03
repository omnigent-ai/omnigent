"""Bound preference request work and retain deployment ownership until it finishes."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, TypeVar
from weakref import WeakKeyDictionary

from fastapi import HTTPException, Request

from omnigent.db.db_models import current_workspace_id
from omnigent.server.routes._oauth import SlidingWindowRateLimiter

MAX_PREFERENCE_WORKERS = 16
MAX_PREFERENCE_REQUESTS_PER_MINUTE = 2048
MAX_PREFERENCE_REQUEST_BYTES = 64 * 1024
_Result = TypeVar("_Result")
PreferenceTaskFactory = Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]]


@dataclass(frozen=True)
class PreferencePolicy:
    """Deployment hooks for request-scoped rollout and background ownership."""

    enabled: Callable[[], bool] | None = None
    create_task: PreferenceTaskFactory = asyncio.create_task

    def is_enabled(self) -> bool:
        return self.enabled is None or self.enabled()

    def require_enabled(self) -> None:
        if not self.is_enabled():
            raise HTTPException(404, "Shared preferences are not enabled")

    async def run(self, user_id: str | None, call: Callable[[], _Result]) -> _Result:
        self.require_enabled()
        return await run_preference_work(user_id, call, create_task=self.create_task)


DEFAULT_PREFERENCE_POLICY = PreferencePolicy()


@dataclass
class _Limits:
    active: set[asyncio.Task] = field(default_factory=set)
    rate: SlidingWindowRateLimiter = field(
        default_factory=lambda: SlidingWindowRateLimiter(
            MAX_PREFERENCE_REQUESTS_PER_MINUTE, 60, 4096
        )
    )


# custom-lint: disable-next=workspace-scoped-cache -- Global budget; rates use workspace keys.
_limits: WeakKeyDictionary[asyncio.AbstractEventLoop, _Limits] = WeakKeyDictionary()


async def run_preference_work(
    user_id: str | None,
    call: Callable[[], _Result],
    *,
    create_task: PreferenceTaskFactory = asyncio.create_task,
) -> _Result:
    """Reject excess work without queuing more threads; cancellation keeps its slot."""
    loop = asyncio.get_running_loop()
    limits = _limits.get(loop)
    if limits is None:
        limits = _limits[loop] = _Limits()
    key = json.dumps((current_workspace_id(), user_id))
    if len(limits.active) >= MAX_PREFERENCE_WORKERS or not limits.rate.allow(
        key, time.monotonic()
    ):
        raise HTTPException(
            429, "Preference service is busy; retry shortly", headers={"Retry-After": "5"}
        )
    work = asyncio.to_thread(call)
    try:
        task = create_task(work)
    except BaseException:
        work.close()
        raise
    limits.active.add(task)

    def finished(done: asyncio.Task) -> None:
        limits.active.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(finished)
    return await asyncio.shield(task)


async def read_preference_payload(request: Request, limit: int) -> bytes:
    """Bound streamed input before JSON decoding, including chunked uploads."""
    payload = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(payload) + len(chunk) > limit:
                    raise HTTPException(413, "Preference action exceeds the request limit")
                payload.extend(chunk)
    except TimeoutError as exc:
        raise HTTPException(408, "Preference upload timed out; retry") from exc
    return bytes(payload)
