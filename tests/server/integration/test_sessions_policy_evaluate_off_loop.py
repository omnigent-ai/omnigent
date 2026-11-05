"""
Integration test: ``POST /policies/evaluate`` keeps synchronous store work off the event loop.

Native harnesses call this endpoint once per tool call. The agent lookup, bundle load and
policy-engine build all make synchronous store reads, so running them on the event loop
stalls every other request the server is handling.

Uses the shared ``auth_client`` / ``db_uri`` fixtures from ``tests/server/conftest.py``.
"""

from __future__ import annotations

import asyncio
import gc
import threading
from contextlib import suppress
from typing import Any

import httpx
import pytest

from omnigent.runtime import get_caps
from omnigent.runtime.caps import RuntimeCaps
from omnigent.server.routes.sessions import routes_hooks
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from tests._helpers.polling import poll_until_async
from tests.server.helpers import create_test_agent, policy_tool_call_request

pytestmark = pytest.mark.asyncio

OWNER = "owner@example.com"

_ASK_THEN_ALLOW_CALLS: list[None] = []


def _ask_then_allow(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that ASKs on its first evaluation and ALLOWs afterwards.

    The first ASK makes the endpoint rebuild the engine inside the approval-gate lock;
    the rebuilt engine ALLOWs, so both build call sites run without a human.

    :param event: V0 event dict.
    :returns: ASK the first time, then ALLOW.
    """
    _ASK_THEN_ALLOW_CALLS.append(None)
    if len(_ASK_THEN_ALLOW_CALLS) == 1:
        return {"result": "ASK", "reason": "Approval required"}
    return {"result": "ALLOW"}


async def test_policy_engine_builds_run_off_the_event_loop(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both engine builds (first and approval-gate rebuild) run on worker threads."""
    _ASK_THEN_ALLOW_CALLS.clear()
    policy = FunctionPolicySpec(
        name="admin__ask_then_allow",
        on=None,
        function=FunctionRef(path=f"{__name__}._ask_then_allow"),
    )
    original_caps = get_caps()
    patched_caps = RuntimeCaps(
        execution_timeout=original_caps.execution_timeout,
        default_policies=[policy],
    )
    monkeypatch.setattr("omnigent.server.routes.sessions.get_caps", lambda: patched_caps)

    agent = await create_test_agent(auth_client, user=OWNER)
    resp = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
        headers={"X-Forwarded-Email": OWNER},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    build_threads: list[threading.Thread] = []
    real_build = routes_hooks.build_policy_engine

    def recording_build(*args: Any, **kwargs: Any) -> Any:
        build_threads.append(threading.current_thread())
        return real_build(*args, **kwargs)

    monkeypatch.setattr(routes_hooks, "build_policy_engine", recording_build)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={
            "event": {
                "type": "PHASE_TOOL_CALL",
                "target": "",
                "data": {"name": "Bash", "arguments": {}},
                "context": {},
            },
        },
        headers={"X-Forwarded-Email": OWNER},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "POLICY_ACTION_ALLOW"
    assert len(build_threads) == 2
    # The ASGI app runs on this test's event loop, i.e. this thread.
    assert threading.current_thread() not in build_threads


def _allow(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that ALLOWs every event, so an engine is built for every evaluation.

    :param event: V0 event dict.
    :returns: ALLOW.
    """
    return {"result": "ALLOW"}


class _RecordingLock:
    """Wraps a session's build lock, counting acquisition requests and grants."""

    def __init__(self, lock: asyncio.Lock, gated: _GatedBuilds) -> None:
        self._lock = lock
        self._gated = gated

    async def __aenter__(self) -> None:
        self._gated.lock_requests += 1
        await self._lock.acquire()
        self._gated.lock_grants += 1

    async def __aexit__(self, *exc_info: object) -> None:
        self._lock.release()


class _GatedBuilds:
    """
    Stand-in for ``build_policy_engine`` whose calls each block until the test releases them.

    It also records requests for the per-session build lock. A free ``asyncio.Lock`` is
    granted without yielding, so a request counted but not yet granted is blocked behind
    the lock holder.
    """

    def __init__(self, real_build: Any, real_lock_factory: Any) -> None:
        self._real_build = real_build
        self._real_lock_factory = real_lock_factory
        self._entered = threading.Condition()
        self.releases: list[threading.Event] = []
        self.lock_requests = 0
        self.lock_grants = 0

    def build_lock(self, session_id: str) -> _RecordingLock:
        return _RecordingLock(self._real_lock_factory(session_id), self)

    def build(self, *args: Any, **kwargs: Any) -> Any:
        release = threading.Event()
        with self._entered:
            self.releases.append(release)
            self._entered.notify_all()
        if not release.wait(timeout=10):
            raise TimeoutError("test never released this build")
        return self._real_build(*args, **kwargs)

    async def wait_for_started(self, count: int) -> None:
        """Wait (bounded) until ``count`` builds have started."""

        def wait() -> bool:
            with self._entered:
                return self._entered.wait_for(lambda: len(self.releases) >= count, timeout=5)

        assert await asyncio.to_thread(wait), f"expected {count} builds to start"

    async def assert_blocked_behind(self, started: int) -> None:
        """Wait until a competing request is blocked on the build lock, with ``started`` builds."""
        await poll_until_async(lambda: self.lock_requests > self.lock_grants, timeout_s=5)
        assert len(self.releases) == started

    def release(self, index: int) -> None:
        self.releases[index].set()


async def _setup_sessions(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    policy_function: str,
    count: int,
) -> tuple[list[str], _GatedBuilds]:
    """Create ``count`` sessions under a default policy and gate the server's engine builds."""
    policy = FunctionPolicySpec(
        name="admin__under_test",
        on=None,
        function=FunctionRef(path=f"{__name__}.{policy_function}"),
    )
    original_caps = get_caps()
    patched_caps = RuntimeCaps(
        execution_timeout=original_caps.execution_timeout,
        default_policies=[policy],
    )
    monkeypatch.setattr("omnigent.server.routes.sessions.get_caps", lambda: patched_caps)
    agent = await create_test_agent(auth_client, user=OWNER)
    session_ids = []
    for _ in range(count):
        resp = await auth_client.post(
            "/v1/sessions",
            json={"agent_id": agent["id"]},
            headers={"X-Forwarded-Email": OWNER},
        )
        assert resp.status_code == 201, resp.text
        session_ids.append(resp.json()["id"])
    gated = _GatedBuilds(routes_hooks.build_policy_engine, routes_hooks._policy_engine_build_lock)
    monkeypatch.setattr(routes_hooks, "build_policy_engine", gated.build)
    monkeypatch.setattr(routes_hooks, "_policy_engine_build_lock", gated.build_lock)
    return session_ids, gated


def _evaluate(auth_client: httpx.AsyncClient, session_id: str) -> asyncio.Task[httpx.Response]:
    return asyncio.create_task(
        auth_client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=policy_tool_call_request("Bash"),
            headers={"X-Forwarded-Email": OWNER},
        )
    )


async def _assert_allowed(*requests: asyncio.Task[httpx.Response]) -> None:
    for resp in await asyncio.gather(*requests):
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == "POLICY_ACTION_ALLOW"


async def test_same_session_builds_wait_for_each_other(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A second same-session build starts only after the first finishes.

    A build seeds declared label initial values with a non-atomic read-then-write, so
    overlapping builds for one session could overwrite a label an evaluation updated.
    """
    (session_id,), gated = await _setup_sessions(auth_client, monkeypatch, "_allow", 1)
    first = _evaluate(auth_client, session_id)
    await gated.wait_for_started(1)
    second = _evaluate(auth_client, session_id)
    await gated.assert_blocked_behind(started=1)
    gated.release(0)
    await gated.wait_for_started(2)
    gated.release(1)
    await _assert_allowed(first, second)


async def test_different_session_builds_run_concurrently(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The build lock is per session, not global."""
    session_ids, gated = await _setup_sessions(auth_client, monkeypatch, "_allow", 2)
    requests = [_evaluate(auth_client, session_id) for session_id in session_ids]
    # Both builds start while neither has been released.
    await gated.wait_for_started(2)
    gated.release(0)
    gated.release(1)
    await _assert_allowed(*requests)


async def test_ask_gate_rebuild_and_a_first_build_wait_for_each_other(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rebuild inside the native ASK gate takes the same per-session build lock."""
    _ASK_THEN_ALLOW_CALLS.clear()
    (session_id,), gated = await _setup_sessions(auth_client, monkeypatch, "_ask_then_allow", 1)
    asking = _evaluate(auth_client, session_id)
    # First build, whose engine ASKs; the request then rebuilds inside the ASK gate.
    await gated.wait_for_started(1)
    gated.release(0)
    await gated.wait_for_started(2)
    # While the ASK-gate rebuild is held, a new request's first build must wait.
    other = _evaluate(auth_client, session_id)
    await gated.assert_blocked_behind(started=2)
    gated.release(1)
    await gated.wait_for_started(3)
    gated.release(2)
    await _assert_allowed(asking, other)


async def test_cancelled_build_keeps_the_lock_until_its_worker_finishes(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a request mid-build doesn't let the next same-session build overlap it."""
    (session_id,), gated = await _setup_sessions(auth_client, monkeypatch, "_allow", 1)
    cancelled = _evaluate(auth_client, session_id)
    await gated.wait_for_started(1)
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    second = _evaluate(auth_client, session_id)
    # The cancelled request's worker thread is still building.
    await gated.assert_blocked_behind(started=1)
    gated.release(0)
    await gated.wait_for_started(2)
    gated.release(1)
    await _assert_allowed(second)


async def test_failed_build_raises_to_its_caller_and_releases_the_lock() -> None:
    def failing_build() -> Any:
        raise RuntimeError("build failed")

    with pytest.raises(RuntimeError, match="build failed"):
        await routes_hooks._build_policy_engine_serialized("conv_failed_build", failing_build)
    assert not routes_hooks._policy_engine_build_lock("conv_failed_build").locked()
    built = await routes_hooks._build_policy_engine_serialized(
        "conv_failed_build", lambda: "engine"
    )
    assert built == "engine"


async def test_failure_after_cancellation_is_retrieved_and_releases_the_lock() -> None:
    """A build that fails after its caller was cancelled doesn't log an unretrieved exception."""
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    started = threading.Event()
    release = threading.Event()

    def failing_build() -> Any:
        started.set()
        release.wait(timeout=5)
        raise RuntimeError("late failure")

    try:
        caller = asyncio.create_task(
            routes_hooks._build_policy_engine_serialized("conv_cancelled_build", failing_build)
        )
        assert await asyncio.to_thread(started.wait, 5)
        lock = routes_hooks._policy_engine_build_lock("conv_cancelled_build")
        caller.cancel()
        with suppress(asyncio.CancelledError):
            await caller
        assert lock.locked()
        release.set()
        await poll_until_async(lambda: not lock.locked(), timeout_s=5)
        del caller
        gc.collect()
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_handler)
