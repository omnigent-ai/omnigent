"""E2E guard: a dead policy-verdict delivery channel is not an Omnigent defect.

Scenario, driven through the real runner ``proxy_stream`` /
``_evaluate_policy_via_omnigent`` code:

1. A user sends a turn to a policy-bearing agent on an executor-adapter harness
   (openai-agents / claude-sdk / ...). That adapter installs a
   ``_policy_evaluator`` on *every* turn, so the executor emits a
   ``policy_evaluation.requested`` event for the ``PHASE_LLM_REQUEST`` gate.
2. The runner intercepts it, computes a verdict (the Omnigent server answers the
   ``/policies/evaluate`` round-trip with an ALLOW), then POSTs the
   ``policy_verdict`` back to the harness subprocess so its parked future
   resolves.
3. The harness IPC channel is dead by then (the harness subprocess crashed /
   disconnected / is being torn down), so every verdict-delivery POST raises a
   dead-channel transport error.
4. The runner retries the delivery once, then signals desync and resyncs
   (tears down) the turn — its designed recovery for a dead harness channel.

The guard: that recovery is a *consequence* of an upstream harness death, so it
must not surface as an unattributed ERROR-level ``omnigent.runner.app``
"delivery unacknowledged ... signaling desync" record — error-log KPIs count
those as Omnigent defects. The wrap-up must be a correctly-attributed,
sub-ERROR record (the desync recovery itself is unchanged).

Following ``tests/e2e/test_harness_stream_error_surfaces_cause.py``, only the
harness subprocess and the Omnigent server are test doubles; the runner code
under test (``proxy_stream`` / ``_evaluate_policy_via_omnigent``) is the real
thing, driven in-process over ASGI. ``NullServerClient`` answers
``/policies/evaluate`` with a well-formed 200, so the *only* thing that fails is
the verdict delivery back to the harness — isolating exactly this failure mode
(a dead verdict-delivery channel), not an unrelated policy error.

Usage::

    pytest tests/e2e/test_policy_verdict_delivery_desync_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient

# The runner logger that emits the delivery wrap-up record (``__name__`` in app.py).
_RUNNER_LOGGER = "omnigent.runner.app"

_EVAL_ID = "poleval_3f1c2b9a8d7e6f5041a2b3c4d5e6f708"

_CONV_ID = "9c0d1e2f3a4b5c6d7e8f0a1b2c3d4e5f"
_AGENT_ID = "965906f5d9fb596610dda599a80faaee"
_RESPONSE_ID = "resp_policy_verdict_delivery"

# Distinctive substrings of the defect-attributed wrap-up ERROR. The format
# string is: "Policy verdict %s delivery unacknowledged (...) after retry;
# signaling desync for %s".
_UNACKED_SIGNATURE = "delivery unacknowledged"
_DESYNC_SIGNATURE = "signaling desync"


class _DeadVerdictChannelHarnessClient(_ScriptedHarnessClient):
    """Harness client whose verdict-delivery channel is dead.

    Streams its scripted SSE frames normally (so the turn's
    ``policy_evaluation.requested`` reaches the runner), but every attempt to
    POST the ``policy_verdict`` back raises ``httpx.ConnectError`` — a member
    of the runner's ``_DEAD_HARNESS_CHANNEL_ERRORS`` set. Other event POSTs
    (e.g. the best-effort ``interrupt`` the desync recovery forwards) succeed,
    so nothing else fails.
    """

    def __init__(self, sse_frames: list[str]) -> None:
        super().__init__(sse_frames)
        # Every verdict-delivery body the runner tried to POST to the harness.
        self.verdict_delivery_attempts: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        """Fail policy-verdict delivery with a dead-channel error; pass others."""
        if isinstance(json, dict) and json.get("type") == "policy_verdict":
            self.verdict_delivery_attempts.append(json)
            raise httpx.ConnectError("harness IPC channel is dead (subprocess gone)")
        return await super().post(url, json=json, timeout=timeout)


def _parse_sse_events(buf: str) -> list[dict[str, Any]]:
    """Parse ``data:`` payloads out of an SSE byte stream."""
    events: list[dict[str, Any]] = []
    for block in buf.split("\n\n"):
        for line in block.strip().splitlines():
            line = line.strip()
            if line.startswith("data:"):
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(line[len("data:") :].strip()))
    return events


async def _drive_turn_with_policy_eval(app: Any) -> list[dict[str, Any]]:
    """POST a streamed turn and drain the live ``?stream=true`` SSE response.

    The runner gathers its per-turn dispatch tasks (including
    ``_evaluate_policy_via_omnigent``) before the SSE generator completes, so
    fully draining the response guarantees the verdict-delivery attempt (and its
    retry + failure handling) has already run by the time this returns.
    """
    transport = httpx.ASGITransport(app=app)
    events: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        async with client.stream(
            "POST",
            f"/v1/sessions/{_CONV_ID}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": _AGENT_ID,
                "model": "policy-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        ) as resp:
            assert resp.status_code == 200, resp.status_code
            buf = ""
            with contextlib.suppress(Exception):
                async for chunk in resp.aiter_text():
                    buf += chunk
            events = _parse_sse_events(buf)
    return events


@pytest.mark.asyncio
async def test_dead_verdict_channel_is_not_an_unattributed_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dead harness verdict-delivery channel must not be logged as an
    unattributed ERROR-level Omnigent defect.

    Drives the real ``_evaluate_policy_via_omnigent`` path: the harness emits a
    ``policy_evaluation.requested`` (as executor-adapter harnesses do on every
    turn), the server returns a verdict, and the runner tries to POST it back to
    a harness whose channel is dead. The dead channel is an upstream /
    teardown condition, so the delivery wrap-up must stay below ERROR (the
    desync recovery itself still runs).
    """
    harness_client = _DeadVerdictChannelHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": _RESPONSE_ID}}),
            _sse(
                {
                    "type": "policy_evaluation.requested",
                    "evaluation_id": _EVAL_ID,
                    "phase": "PHASE_LLM_REQUEST",
                    "data": {
                        "model": "policy-agent",
                        "messages_count": 1,
                        "last_user_message": "hi",
                    },
                }
            ),
            _sse({"type": "response.completed"}),
        ]
    )
    pm = _FakeProcessManager(harness_client)
    spec = AgentSpec(spec_version=1, name="policy-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING, logger=_RUNNER_LOGGER):
        await _drive_turn_with_policy_eval(app)

    # Non-vacuity: the runner actually computed a verdict and tried to deliver it
    # to the (dead) harness channel, so the scenario under test genuinely ran.
    assert len(harness_client.verdict_delivery_attempts) >= 1, (
        "the runner never attempted to deliver the policy verdict to the "
        "harness; the reproduction scenario did not execute"
    )
    assert all(
        body.get("evaluation_id") == _EVAL_ID for body in harness_client.verdict_delivery_attempts
    ), harness_client.verdict_delivery_attempts

    records = [r for r in caplog.records if r.name == _RUNNER_LOGGER]

    # The guard: a dead harness verdict-delivery channel (an upstream /
    # teardown consequence) must NOT surface as an ERROR-level "delivery
    # unacknowledged ... signaling desync" record — error-log KPIs would
    # count it as an Omnigent defect.
    tracked_errors = [
        r
        for r in records
        if r.levelno >= logging.ERROR
        and _UNACKED_SIGNATURE in r.getMessage()
        and _DESYNC_SIGNATURE in r.getMessage()
    ]
    assert not tracked_errors, (
        "a dead harness verdict-delivery channel produced an ERROR-level "
        "'delivery unacknowledged' record from _evaluate_policy_via_omnigent "
        "instead of a correctly-attributed / downgraded outcome:\n  "
        + "\n  ".join(r.getMessage() for r in tracked_errors)
    )
