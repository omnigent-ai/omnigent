"""Regression tests for durable native-forwarder failure escalation.

Pins that poll-loop exceptions and supervisor restart storms POST a durable
``external_session_status: failed`` through the shared liveness helper — and
that successful polls / healthy uptime reset the counters so transient blips
do not produce a failed card.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from omnigent._native_forwarder_liveness import (
    POLL_FAILURE_THRESHOLD,
    RESTART_FAILURE_THRESHOLD,
    PollFailureTracker,
    RestartFailureTracker,
    handle_poll_failure,
    handle_supervisor_restart,
    note_poll_success,
    note_supervisor_healthy,
)


class _RecordingClient:
    """Minimal stand-in for ``httpx.AsyncClient`` in liveness tests."""


@pytest.mark.asyncio
async def test_poll_failures_escalate_to_durable_failed_status() -> None:
    """N consecutive poll failures POST ``external_session_status: failed``.

    :returns: None.
    """
    posts: list[dict[str, Any]] = []
    tracker = PollFailureTracker()

    async def _post_status(
        _client: object,
        *,
        session_id: str,
        status: str,
        output: str | None = None,
        **_kwargs: object,
    ) -> None:
        posts.append({"session_id": session_id, "status": status, "output": output})

    for i in range(POLL_FAILURE_THRESHOLD - 1):
        posted = await handle_poll_failure(
            client=_RecordingClient(),  # type: ignore[arg-type]
            session_id="conv_poll",
            tracker=tracker,
            error=RuntimeError(f"transient-{i}"),
            harness="claude-native",
            post_status=_post_status,
        )
        assert posted is False
        assert posts == []

    posted = await handle_poll_failure(
        client=_RecordingClient(),  # type: ignore[arg-type]
        session_id="conv_poll",
        tracker=tracker,
        error=RuntimeError("transient-final"),
        harness="claude-native",
        post_status=_post_status,
    )
    assert posted is True
    assert len(posts) == 1
    assert posts[0]["status"] == "failed"
    assert posts[0]["session_id"] == "conv_poll"
    assert "claude-native" in (posts[0]["output"] or "")
    assert f"{POLL_FAILURE_THRESHOLD} consecutive" in (posts[0]["output"] or "")

    posted_again = await handle_poll_failure(
        client=_RecordingClient(),  # type: ignore[arg-type]
        session_id="conv_poll",
        tracker=tracker,
        error=RuntimeError("still-broken"),
        harness="claude-native",
        post_status=_post_status,
    )
    assert posted_again is False
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_permanent_poll_error_fails_immediately() -> None:
    """A permanent poll error class escalates on the first failure.

    :returns: None.
    """
    posts: list[dict[str, Any]] = []
    tracker = PollFailureTracker()

    async def _post_status(
        _client: object,
        *,
        session_id: str,
        status: str,
        output: str | None = None,
        **_kwargs: object,
    ) -> None:
        posts.append({"session_id": session_id, "status": status, "output": output})

    posted = await handle_poll_failure(
        client=_RecordingClient(),  # type: ignore[arg-type]
        session_id="conv_perm",
        tracker=tracker,
        error=PermissionError("bridge unreadable"),
        harness="goose-native",
        post_status=_post_status,
    )
    assert posted is True
    assert posts[0]["status"] == "failed"
    assert "permanent" in (posts[0]["output"] or "")


@pytest.mark.asyncio
async def test_successful_poll_resets_failure_streak() -> None:
    """A successful poll clears the streak so later blips do not accumulate.

    :returns: None.
    """
    posts: list[dict[str, Any]] = []
    tracker = PollFailureTracker()

    async def _post_status(
        _client: object,
        *,
        session_id: str,
        status: str,
        output: str | None = None,
        **_kwargs: object,
    ) -> None:
        posts.append({"session_id": session_id, "status": status, "output": output})

    for i in range(POLL_FAILURE_THRESHOLD - 1):
        await handle_poll_failure(
            client=_RecordingClient(),  # type: ignore[arg-type]
            session_id="conv_reset",
            tracker=tracker,
            error=RuntimeError(f"blip-{i}"),
            harness="qwen-native",
            post_status=_post_status,
        )
    note_poll_success(tracker)
    assert tracker.consecutive_failures == 0

    await handle_poll_failure(
        client=_RecordingClient(),  # type: ignore[arg-type]
        session_id="conv_reset",
        tracker=tracker,
        error=RuntimeError("one-more"),
        harness="qwen-native",
        post_status=_post_status,
    )
    assert posts == []


@pytest.mark.asyncio
async def test_cold_start_empty_polls_do_not_escalate() -> None:
    """Native cold-start discovery polls must not trip durable ``failed``.

    A cold-starting native session commonly spends several polls discovering
    the vendor session / waiting for the transcript file. Those iterations
    succeed (no exception) and call :func:`note_poll_success`. Even a few
    transient exceptions below the threshold must not POST ``failed`` — that
    was the shape of prior spurious failed-card bugs during cold start.

    :returns: None.
    """
    posts: list[dict[str, Any]] = []
    tracker = PollFailureTracker()

    async def _post_status(
        _client: object,
        *,
        session_id: str,
        status: str,
        output: str | None = None,
        **_kwargs: object,
    ) -> None:
        posts.append({"session_id": session_id, "status": status, "output": output})

    # Simulate cold-start: a couple of transient misses, then a successful
    # discovery poll, then more quiet successful polls — never escalate.
    for i in range(2):
        await handle_poll_failure(
            client=_RecordingClient(),  # type: ignore[arg-type]
            session_id="conv_cold",
            tracker=tracker,
            error=RuntimeError(f"not-ready-{i}"),
            harness="claude-native",
            post_status=_post_status,
        )
    note_poll_success(tracker)
    for _ in range(POLL_FAILURE_THRESHOLD + 2):
        note_poll_success(tracker)
    assert posts == []
    assert tracker.consecutive_failures == 0
    assert tracker.failed_status_emitted is False


@pytest.mark.asyncio
async def test_supervisor_restarts_escalate_within_window() -> None:
    """M supervisor restarts inside the window POST durable ``failed``.

    :returns: None.
    """
    posts: list[dict[str, Any]] = []
    tracker = RestartFailureTracker()
    now = 1000.0

    async def _post_status(
        _client: object,
        *,
        session_id: str,
        status: str,
        output: str | None = None,
        **_kwargs: object,
    ) -> None:
        posts.append({"session_id": session_id, "status": status, "output": output})

    for i in range(RESTART_FAILURE_THRESHOLD - 1):
        posted = await handle_supervisor_restart(
            client=_RecordingClient(),  # type: ignore[arg-type]
            session_id="conv_restarts",
            tracker=tracker,
            error=RuntimeError(f"crash-{i}"),
            harness="hermes-native",
            now=now + i,
            post_status=_post_status,
        )
        assert posted is False

    posted = await handle_supervisor_restart(
        client=_RecordingClient(),  # type: ignore[arg-type]
        session_id="conv_restarts",
        tracker=tracker,
        error=RuntimeError("crash-final"),
        harness="hermes-native",
        now=now + RESTART_FAILURE_THRESHOLD,
        post_status=_post_status,
    )
    assert posted is True
    assert posts[0]["status"] == "failed"
    assert "restarted" in (posts[0]["output"] or "")


@pytest.mark.asyncio
async def test_healthy_supervisor_uptime_clears_restart_budget() -> None:
    """Healthy uptime resets restart escalation so later crashes start fresh.

    :returns: None.
    """
    posts: list[dict[str, Any]] = []
    tracker = RestartFailureTracker()

    async def _post_status(
        _client: object,
        *,
        session_id: str,
        status: str,
        output: str | None = None,
        **_kwargs: object,
    ) -> None:
        posts.append({"session_id": session_id, "status": status, "output": output})

    for i in range(RESTART_FAILURE_THRESHOLD - 1):
        await handle_supervisor_restart(
            client=_RecordingClient(),  # type: ignore[arg-type]
            session_id="conv_healthy",
            tracker=tracker,
            error=RuntimeError(f"early-{i}"),
            harness="claude-native",
            now=float(i),
            post_status=_post_status,
        )
    note_supervisor_healthy(tracker)
    assert tracker.restart_at == []

    await handle_supervisor_restart(
        client=_RecordingClient(),  # type: ignore[arg-type]
        session_id="conv_healthy",
        tracker=tracker,
        error=RuntimeError("later"),
        harness="claude-native",
        now=100.0,
        post_status=_post_status,
    )
    assert posts == []


@pytest.mark.asyncio
async def test_qwen_forwarder_poll_exception_posts_durable_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live qwen poll loop escalates via the shared helper after N failures.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for the bridge dir.
    :returns: None.
    """
    from omnigent import qwen_native_forwarder as fwd

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    posts: list[dict[str, Any]] = []

    def _always_fail(*_args: object, **_kwargs: object) -> tuple[list[object], int]:
        raise RuntimeError("simulated poll boom")

    async def _record_failure(**kwargs: Any) -> bool:
        return await handle_poll_failure(
            **kwargs,
            threshold=3,
            post_status=_post_status,
        )

    async def _post_status(
        _client: object,
        *,
        session_id: str,
        status: str,
        output: str | None = None,
        **_kwargs: object,
    ) -> None:
        posts.append({"session_id": session_id, "status": status, "output": output})

    monkeypatch.setattr(fwd, "_read_new_events", _always_fail)
    monkeypatch.setattr(fwd, "handle_poll_failure", _record_failure)

    task = asyncio.create_task(
        fwd.forward_qwen_events_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_qwen_fail",
            bridge_dir=bridge_dir,
            agent_name="qwen-native",
            poll_interval_s=0.001,
        )
    )
    try:
        for _ in range(200):
            if posts:
                break
            await asyncio.sleep(0.01)
        assert posts, "expected durable failed status after consecutive poll failures"
        assert posts[0]["status"] == "failed"
        assert posts[0]["session_id"] == "conv_qwen_fail"
        assert "qwen-native" in (posts[0]["output"] or "")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
