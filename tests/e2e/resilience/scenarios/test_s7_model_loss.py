"""S7: Claude Code loses its model provider while the rest of Omnigent is fine.

Only the model link breaks: new connections are refused and live ones reset,
either before the turn's first model call or in the middle of a streamed
reply. Contract: a short loss is retried and the turn completes once. A long
one ends in a failure the user can retry with one action. The session never
spins in ``running`` with nothing happening.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from tests.e2e.resilience.lab.driver import SessionDriver, Turn
from tests.e2e.resilience.lab.lab import Harness, Lab
from tests.e2e.resilience.lab.report import ScenarioReport
from tests.e2e.resilience.scenarios import _contract as contract

# Failure codes the web UI offers a one-click Retry for (RETRYABLE_ERROR_CODES
# in web/src/components/blocks/StatusBlocks.tsx).
_RETRYABLE = frozenset(
    {"rate_limit_exceeded", "transient_upstream_error", "connection_error", "runner_disconnected"}
)
_PHASES = ["turn_start", "mid_stream"]


@pytest.mark.timeout(900)
@pytest.mark.parametrize(
    ("harness", "phase", "outage_s"), contract.cases(_PHASES, contract.outages([10], [60, 180]))
)
def test_s7_model_loss(
    lab_factory: Callable[..., Lab], harness: Harness, phase: str, outage_s: int
) -> None:
    lab = lab_factory()
    driver = SessionDriver.create(lab, harness)
    session_id = driver.session_id
    report = ScenarioReport(
        "S7 model loss", {"harness": harness, "phase": phase, "outage_s": outage_s}
    )
    model = lab.proxies.model
    with contract.observe(lab, driver, report):
        driver.round_trip()
        if phase == "turn_start":
            fault = model.refuse()
            turn = driver.text_turn()
            driver.send(turn)
        else:
            turn = driver.start_streaming_turn(8)
            fault = model.refuse()
            model.reset()
        try:
            time.sleep(outage_s)
        finally:
            fault.clear()
        outcome = _wait_outcome(lab, driver, turn)
        report.check(
            "turn_reaches_an_outcome",
            outcome is not None,
            "" if outcome else f"still {lab.snapshot(session_id).get('status')} with no reply",
        )
        if outcome == "completed":
            replies = driver.count_text(turn.reply, role="assistant")
            report.check("final_reply_committed_once", replies == 1, f"{replies} copies")
        elif outcome == "failed":
            code = (lab.snapshot(session_id).get("labels") or {}).get(
                "omnigent.last_task_error_code"
            )
            report.check(
                "failure_is_retryable",
                code in _RETRYABLE,
                f"error code {code!r}",
                known_gap="R7" if harness == "claude" else None,
            )
        if outage_s <= 10:
            report.check("short_loss_is_retried", outcome == "completed", f"outcome={outcome}")
        user = driver.count_text(turn.marker, role="user")
        report.check("user_message_committed_once", user == 1, f"{user} copies")
        next_turn = contract.eventually_round_trip(driver)
        report.check(
            "next_turn_round_trips",
            next_turn is not None,
            "" if next_turn else "a new message after recovery got no reply",
        )
    report.require()


def _wait_outcome(lab: Lab, driver: SessionDriver, turn: Turn) -> str | None:
    """``completed``, ``failed``, or ``None`` if the turn neither finished nor failed."""

    def _outcome() -> str | None:
        if driver.count_text(turn.reply, role="assistant"):
            return "completed"
        if lab.snapshot(driver.session_id).get("status") == "failed":
            return "failed"
        return None

    return contract.eventually(_outcome, timeout=contract.RECOVERY_S)  # type: ignore[return-value]
