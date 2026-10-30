"""Runner hold while a harness prompt is parked on the server.

A runner that predates the runner-side hold idles out once its pane reads
quiet, which ends a turn still parked on a permission prompt. While the
prompt waits, the server re-sends a ``waiting`` status to the bound runner on
an interval, and stops after the cap measured from the id's first park so the
host can restart and upgrade. The hold is advisory: it must never change the
wait's outcome.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.server import _elicitation_registry
from omnigent.server.routes import sessions as sessions_route
from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult

SESSION_ID = "conv_runnerhold"
WAITING_EVENT = {"type": "external_session_status", "data": {"status": "waiting"}}


async def _never_disconnects(_request: Any) -> None:
    """Model a client that stays connected for the whole wait."""
    await asyncio.sleep(3600)


async def _wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> None:
    """Poll *predicate* until it holds, failing the test after *timeout_s*."""
    async with asyncio.timeout(timeout_s):
        while not predicate():
            await asyncio.sleep(0.01)


async def _park(elicitation_id: str, timeout_s: float) -> ElicitationResult | None:
    """Park one harness elicitation on the real wait, as a hook re-park would."""
    return await sessions_route._publish_and_wait_for_harness_elicitation(
        SimpleNamespace(),
        session_id=SESSION_ID,
        params=ElicitationRequestParams(mode="form", message="Run Bash?", requestedSchema={}),
        timeout_s=timeout_s,
        elicitation_id=elicitation_id,
    )


def _resolve(elicitation_id: str, action: str) -> None:
    """Deliver a web verdict to the parked wait, as the approval route does."""
    future = _elicitation_registry._harness_elicitation_registry[elicitation_id]
    future.set_result(ElicitationResult(action=action))


@pytest.fixture
def runner_forward(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Record runner forwards instead of contacting a runner."""
    forward = AsyncMock(return_value=None)
    monkeypatch.setattr(sessions_route, "_forward_session_change_to_runner", forward)
    monkeypatch.setattr(sessions_route, "_poll_request_disconnect", _never_disconnects)
    monkeypatch.setattr(sessions_route, "_HARNESS_ELICITATION_REPARK_GRACE_S", 0.05)
    _elicitation_registry.reset_for_tests()
    yield forward
    _elicitation_registry.reset_for_tests()


async def test_parked_prompt_sends_waiting_status_and_keeps_verdict(
    runner_forward: AsyncMock,
) -> None:
    """Publishing a prompt tells the runner it is waiting; the verdict still returns."""
    elicitation_id = "elicit_hold_publish"
    park_task = asyncio.create_task(_park(elicitation_id, timeout_s=5.0))

    await _wait_until(lambda: runner_forward.await_count >= 1)
    first_call = runner_forward.await_args_list[0]
    assert first_call.args[0] == SESSION_ID
    assert first_call.args[2] == WAITING_EVENT

    _resolve(elicitation_id, "accept")
    verdict = await asyncio.wait_for(park_task, timeout=2.0)
    assert verdict is not None
    assert verdict.action == "accept"


async def test_waiting_status_is_resent_on_the_interval(
    runner_forward: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long park re-sends ``waiting`` each interval, not only at publish."""
    monkeypatch.setattr(sessions_route, "_HARNESS_ELICITATION_RUNNER_HOLD_INTERVAL_S", 0.05)
    elicitation_id = "elicit_hold_interval"
    park_task = asyncio.create_task(_park(elicitation_id, timeout_s=5.0))

    await _wait_until(lambda: runner_forward.await_count >= 3)
    assert all(call.args[2] == WAITING_EVENT for call in runner_forward.await_args_list)

    _resolve(elicitation_id, "accept")
    await asyncio.wait_for(park_task, timeout=2.0)


async def test_hold_stops_at_cap_measured_from_first_park(
    runner_forward: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the cap, a re-park of the same id no longer holds the runner."""
    monkeypatch.setattr(sessions_route, "_HARNESS_ELICITATION_RUNNER_HOLD_INTERVAL_S", 0.05)
    monkeypatch.setattr(sessions_route, "_HARNESS_ELICITATION_RUNNER_HOLD_CAP_S", 0.3)
    elicitation_id = "elicit_hold_cap"

    first_park = await _park(elicitation_id, timeout_s=0.1)
    assert first_park is None
    assert runner_forward.await_count >= 1

    # Let the cap pass since the first park; a re-park must not restart it.
    await asyncio.sleep(0.35)
    runner_forward.reset_mock()
    second_park = await _park(elicitation_id, timeout_s=0.1)

    assert second_park is None
    assert runner_forward.await_count == 0


async def test_forward_failure_does_not_change_the_wait(
    runner_forward: AsyncMock,
) -> None:
    """A runner forward that raises is logged and the prompt still resolves."""
    runner_forward.side_effect = RuntimeError("runner unreachable")
    elicitation_id = "elicit_hold_failure"
    park_task = asyncio.create_task(_park(elicitation_id, timeout_s=5.0))

    await _wait_until(lambda: runner_forward.await_count >= 1)
    _resolve(elicitation_id, "decline")
    verdict = await asyncio.wait_for(park_task, timeout=2.0)

    assert verdict is not None
    assert verdict.action == "decline"
    assert runner_forward.await_count >= 1
