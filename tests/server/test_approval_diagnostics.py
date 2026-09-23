"""Approval diagnostics identify outcomes that share an empty HTTP success."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.server.routes import sessions as sessions_route
from omnigent.server.routes._sessions import common, orchestration
from omnigent.server.schemas import ElicitationRequestParams

SESSION = "conv_diagnostics"
ELICITATION = "elicit_claude_diagnostics"


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [
        record.attributes
        for record in caplog.records
        if getattr(record, "event_name", None) == name
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "web_verdict",
        "terminal_result",
        "native_resolution",
        "timeout",
        "disconnect",
        "cancelled",
        "error",
    ],
)
async def test_wait_reports_actual_resolution_boundary(
    outcome: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="omnigent.server.routes.sessions")
    disconnected = asyncio.Event()

    async def poll(_request: object) -> None:
        await disconnected.wait()

    monkeypatch.setattr(sessions_route, "_poll_request_disconnect", poll)
    monkeypatch.setattr(sessions_route, "_HARNESS_ELICITATION_REPARK_GRACE_S", 0.001)
    task = asyncio.create_task(
        orchestration._publish_and_wait_for_harness_elicitation(
            AsyncMock(),
            session_id=SESSION,
            params=ElicitationRequestParams(
                mode="form", message="private prompt", permission_mode="auto"
            ),
            timeout_s=0 if outcome == "timeout" else 10,
            elicitation_id=ELICITATION,
            tool_name="Bash",
            tool_input={"command": "private command"},
        )
    )
    await asyncio.sleep(0)
    if outcome == "web_verdict":
        await sessions_route._resolve_elicitation(
            SESSION, {"elicitation_id": ELICITATION, "action": "accept"}, None
        )
    elif outcome == "terminal_result":
        sessions_route._signal_terminal_resolved_harness_elicitation(
            SESSION, "Bash", {"command": "private command"}
        )
    elif outcome == "native_resolution":
        sessions_route._signal_harness_elicitation_resolved_by_id(SESSION, ELICITATION)
    elif outcome == "disconnect":
        disconnected.set()
    elif outcome == "cancelled":
        task.cancel()
    elif outcome == "error":
        sessions_route._harness_elicitation_registry[ELICITATION].set_exception(
            RuntimeError("failed")
        )
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await asyncio.wait_for(task, 1)
        assert (result is not None) == (outcome == "web_verdict")
    await asyncio.gather(*tuple(common._deferred_elicitation_clear_tasks))

    (started,) = _events(caplog, "approval_wait_started")
    (ended,) = _events(caplog, "approval_wait_ended")
    assert started["elicitation_id"] == ended["elicitation_id"] == ELICITATION
    assert started["wait_attempt_id"] == ended["wait_attempt_id"]
    assert started["permission_mode"] == "auto"
    assert started["native_reason"] == "not_exposed"
    assert ended["outcome"] == outcome
    assert ended["response_kind"] == (
        "verdict" if outcome == "web_verdict" else "none" if outcome == "cancelled" else "empty"
    )
    assert ended["action"] == ("accept" if outcome == "web_verdict" else None)
    assert ended["duration_ms"] >= 0
    diagnostics = [
        r for r in caplog.records if getattr(r, "event_name", "").startswith("approval_")
    ]
    assert all(getattr(record, "session_id", None) == SESSION for record in diagnostics)
    assert "private" not in repr([r.attributes for r in diagnostics])
    if outcome in {"timeout", "disconnect", "cancelled", "error"}:
        assert _events(caplog, "approval_expired")[0]["reason"] == "repark_expiry"
    else:
        assert not _events(caplog, "approval_expired")


@pytest.mark.asyncio
async def test_repark_adopts_verdict_without_reporting_another_publication(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="omnigent.server.routes.sessions")
    params = ElicitationRequestParams(mode="form", message="approval")
    task = asyncio.create_task(
        orchestration._publish_and_wait_for_harness_elicitation(
            AsyncMock(),
            session_id=SESSION,
            params=params,
            timeout_s=10,
            elicitation_id=ELICITATION,
        )
    )

    async def no_disconnect(_request: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(sessions_route, "_poll_request_disconnect", no_disconnect)
    await asyncio.sleep(0)
    await sessions_route._resolve_elicitation(
        SESSION, {"elicitation_id": ELICITATION, "action": "decline"}, None
    )
    await task
    result = await orchestration._publish_and_wait_for_harness_elicitation(
        AsyncMock(), session_id=SESSION, params=params, timeout_s=10, elicitation_id=ELICITATION
    )
    assert result is not None and result.action == "decline"
    ended = _events(caplog, "approval_wait_ended")
    assert len(ended) == 2
    assert ended[0]["wait_attempt_id"] != ended[1]["wait_attempt_id"]
    assert ended[1]["adopted_verdict"] is True
    assert ended[1]["outcome"] == "web_verdict"
    assert len(_events(caplog, "approval_published")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 409])
async def test_runner_delivery_status_is_distinct_from_verdict_acceptance(
    status: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="omnigent.server.routes.sessions")
    runner = AsyncMock()
    runner.post.return_value = httpx.Response(status)
    monkeypatch.setattr(sessions_route, "_get_runner_client", AsyncMock(return_value=runner))
    await sessions_route._forward_approval_to_runner(
        SESSION, {"elicitation_id": ELICITATION}, None
    )
    (record,) = _events(caplog, "approval_runner_delivery")
    assert record["outcome"] == ("accepted" if status == 200 else "rejected")
    assert record["http_status"] == status


@pytest.mark.asyncio
async def test_logging_failure_cannot_strand_a_permission_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_logger(*args: object, **kwargs: object) -> None:
        raise RuntimeError("broken telemetry handler")

    async def no_disconnect(_request: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(common._logger, "info", broken_logger)
    monkeypatch.setattr(sessions_route, "_poll_request_disconnect", no_disconnect)
    task = asyncio.create_task(
        orchestration._publish_and_wait_for_harness_elicitation(
            AsyncMock(),
            session_id=SESSION,
            params=ElicitationRequestParams(mode="form", message="approval"),
            timeout_s=10,
            elicitation_id=ELICITATION,
        )
    )
    await asyncio.sleep(0)
    await sessions_route._resolve_elicitation(
        SESSION, {"elicitation_id": ELICITATION, "action": "accept"}, None
    )
    result = await asyncio.wait_for(task, 1)
    assert result is not None and result.action == "accept"
    assert ELICITATION not in sessions_route._harness_elicitation_registry
    assert ELICITATION not in sessions_route._harness_parked_elicitations


@pytest.mark.asyncio
async def test_deferred_clear_does_not_call_an_accepted_gap_verdict_unanswered(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def no_disconnect(_request: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(sessions_route, "_poll_request_disconnect", no_disconnect)
    caplog.set_level(logging.INFO, logger="omnigent.server.routes.sessions")
    monkeypatch.setattr(sessions_route, "_HARNESS_ELICITATION_REPARK_GRACE_S", 0.05)
    await orchestration._publish_and_wait_for_harness_elicitation(
        AsyncMock(),
        session_id=SESSION,
        params=ElicitationRequestParams(mode="form", message="approval"),
        timeout_s=0,
        elicitation_id=ELICITATION,
    )
    await sessions_route._resolve_elicitation(
        SESSION, {"elicitation_id": ELICITATION, "action": "accept"}, None
    )
    await asyncio.gather(*tuple(common._deferred_elicitation_clear_tasks))
    assert not _events(caplog, "approval_expired")
    (cleared,) = _events(caplog, "approval_deferred_clear")
    assert cleared["reason"] == "already_settled"
