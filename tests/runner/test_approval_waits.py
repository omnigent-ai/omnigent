"""Approval storage must not block synchronous runner liveness checks."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.runner import approval_waits
from omnigent.runner.approval_waits import NativeApprovalWaits
from tests._helpers.polling import poll_until_async


@pytest.mark.parametrize("marker_state", ["fresh", "stale", "absent"])
async def test_approval_observations_follow_marker_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker_state: str
) -> None:
    monkeypatch.setattr(bridge, "_APPROVAL_WAIT_ROOT", tmp_path)
    monkeypatch.setattr(approval_waits, "_POLL_INTERVAL_S", 0.01)
    marker = bridge.approval_wait_marker_path("conv")
    if marker_state != "absent":
        bridge.touch_approval_wait_marker(marker)
        if marker_state == "stale":
            stale_at = time.time() - bridge.APPROVAL_WAIT_MARKER_TTL_S - 1
            os.utime(marker, (stale_at, stale_at))
    observations = NativeApprovalWaits()
    instance = object()
    try:
        # A parent-death thread cannot create a task on the runner loop.
        assert await asyncio.to_thread(observations.is_busy, "conv", instance)
        assert observations._probes == {}
        assert observations.is_busy("conv", instance)
        await poll_until_async(lambda: "conv" in observations._observations)
        assert observations.is_busy("conv", instance) is (marker_state == "fresh")
        if marker_state == "stale":
            assert not marker.exists()
        if marker_state == "fresh":
            bridge.clear_approval_wait_marker(marker)
            await poll_until_async(lambda: not observations.is_busy("conv", instance))
        async with asyncio.timeout(2):
            while await asyncio.to_thread(observations.is_busy, "conv", instance):
                await asyncio.sleep(0.001)
        # A stalled/suspended observer must not keep serving an old idle verdict.
        monkeypatch.setattr(approval_waits, "_MAX_OBSERVATION_AGE_S", -1)
        assert observations.is_busy("conv", instance)
        observations.retain(set())
        assert observations._probes == {}
        assert observations._observations == {}
    finally:
        await observations.close()
    assert observations.is_busy("conv", instance)
    assert observations._probes == {}


async def test_replaced_pane_cannot_receive_a_cancelled_observers_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls: list[str] = []

    def read_marker(session_id: str) -> bool:
        calls.append(session_id)
        if len(calls) == 1:
            entered.set()
            try:
                assert release.wait(5)
                return True
            finally:
                finished.set()
        return False

    monkeypatch.setattr(bridge, "approval_wait_is_fresh", read_marker)
    observations = NativeApprovalWaits()
    old_instance, new_instance = object(), object()
    try:
        assert observations.is_busy("conv", old_instance)
        assert await asyncio.to_thread(entered.wait, 2)
        old_task = observations._probes["conv"][1]
        assert observations.is_busy("conv", new_instance)
        await poll_until_async(lambda: not observations.is_busy("conv", new_instance))
        assert old_task.cancelled()
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert observations.is_busy("conv", new_instance) is False
        assert len(calls) == 2
    finally:
        release.set()
        await observations.close()


async def test_shutdown_cancels_a_blocked_observer_without_waiting_for_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def read_marker(session_id: str) -> bool:
        entered.set()
        try:
            assert release.wait(5)
            return False
        finally:
            finished.set()

    monkeypatch.setattr(bridge, "approval_wait_is_fresh", read_marker)
    observations = NativeApprovalWaits()
    try:
        assert observations.is_busy("conv", object())
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.wait_for(observations.close(), timeout=1)
        assert not finished.is_set()
        assert observations._observations == {}
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await observations.close()
    assert observations._observations == {}


async def test_failed_observation_stays_busy_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def read_marker(session_id: str) -> bool:
        calls.append(session_id)
        if len(calls) == 1:
            raise RuntimeError("storage observation failed")
        return False

    monkeypatch.setattr(bridge, "approval_wait_is_fresh", read_marker)
    monkeypatch.setattr(approval_waits, "_POLL_INTERVAL_S", 0.01)
    observations = NativeApprovalWaits()
    instance = object()
    try:
        assert observations.is_busy("conv", instance)
        await poll_until_async(lambda: "conv" in observations._observations, interval_s=0.001)
        assert observations.is_busy("conv", instance)
        await poll_until_async(lambda: not observations.is_busy("conv", instance))
    finally:
        await observations.close()
