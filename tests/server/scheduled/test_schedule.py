"""Persisted schedule calculations and occurrence identities without a database."""

import uuid
from datetime import UTC, datetime

import pytest

from omnigent.entities import ScheduledTask
from omnigent.server.scheduled.rrule import RRuleSearchError, RRuleValidationError
from omnigent.server.scheduled.schedule import (
    continued_sandbox_host_id,
    is_trigger_automation,
    next_scheduled_time,
    occurrence_host_id,
    sandbox_host_id,
    timer_interval,
    timer_rule,
)


@pytest.fixture
def task() -> ScheduledTask:
    return ScheduledTask(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, "task").hex,
        name="task",
        prompt="prompt",
        rrule="FREQ=HOURLY",
        user_id=None,
        agent_id=uuid.uuid5(uuid.NAMESPACE_DNS, "agent").hex,
        timezone="UTC",
        created_at=1792540800,
    )


def test_sandbox_identity_is_stable_per_tenant_task_and_trigger(task) -> None:
    from dataclasses import replace

    task = replace(task, execution_target="managed_sandbox")
    host = occurrence_host_id(task, "schedule:100")
    assert host == occurrence_host_id(task, "schedule:100")
    assert host != occurrence_host_id(task, "schedule:200")
    assert host != occurrence_host_id(replace(task, workspace_id=42), "schedule:100")
    assert host != occurrence_host_id(task, "manual:100")
    pinned = replace(task, execution_target="connected_host", host_id=uuid.uuid4().hex)
    assert occurrence_host_id(pinned, "schedule:100") == pinned.host_id


@pytest.mark.parametrize(
    "task_id,occurrence_host,continued_host",
    [
        # Values from before non-UUID IDs, so running sandboxes keep their host IDs.
        (
            "6f1c0a3e9b2d4c5e8f7a1b2c3d4e5f60",
            "e140df56908451ec8c9e48a16e1a9784",
            "f3570dd74571508eb915bedac22a76e1",
        ),
        # An ID a deployment allocates in another form.
        (
            "4611686018427387904",
            "1a9741b70fec58859e0afd6205d2721e",
            "e2a4b899204a553e8046228f5fce6286",
        ),
    ],
    ids=["uuid", "decimal"],
)
def test_sandbox_identity_is_pinned_for_any_task_id_form(
    task_id: str, occurrence_host: str, continued_host: str
) -> None:
    assert sandbox_host_id(task_id, 1, "schedule:100") == occurrence_host
    assert continued_sandbox_host_id(task_id, 1) == continued_host
    # Another ID of the same form gets other hosts.
    other = task_id[:-1] + ("2" if task_id.endswith("1") else "1")
    assert sandbox_host_id(other, 1, "schedule:100") != occurrence_host
    assert continued_sandbox_host_id(other, 1) != continued_host


@pytest.mark.parametrize("source", ["Jira", "Slack", "GitHub", "Linear", "PagerDuty"])
def test_trigger_automations_are_recognized_by_their_opening_sentence(task, source) -> None:
    from dataclasses import replace

    opening = f"This automation watches {source} and starts one Omnigent session per item."
    assert is_trigger_automation(replace(task, prompt=opening, session_mode="continue"))
    # The trigger form's saved state line can come first.
    saved = f'<!-- omnigent-trigger v1 {{"source": "{source.lower()}"}} -->\n{opening}'
    assert is_trigger_automation(replace(task, prompt=saved, session_mode="continue"))
    # A trigger keeps one session, and the sentence has to open the prompt.
    assert not is_trigger_automation(replace(task, prompt=opening, session_mode="new"))
    assert not is_trigger_automation(
        replace(task, prompt=f"Summarize this. {opening}", session_mode="continue")
    )


@pytest.mark.parametrize(
    "rule,created,timezone,first,interval,count",
    [
        ("FREQ=DAILY;BYHOUR=9;COUNT=3", "2026-10-01T10:00", "UTC", "2026-10-02T09:00", 86400, 3),
        (
            "FREQ=DAILY;INTERVAL=2;BYHOUR=9;COUNT=3",
            "2026-10-01T10:00",
            "UTC",
            "2026-10-03T09:00",
            172800,
            3,
        ),
        ("FREQ=HOURLY;COUNT=24", "2026-10-01T15:00", "UTC", "2026-10-01T16:00", 3600, 24),
        ("FREQ=HOURLY;COUNT=24", "2026-10-01T15:37", "UTC", "2026-10-01T16:00", 3600, 24),
        ("FREQ=DAILY;COUNT=2", "2026-10-01T00:00", "UTC", "2026-10-02T00:00", 86400, 2),
        (
            "FREQ=DAILY;BYHOUR=9;COUNT=3",
            "2026-10-02T00:00",
            "America/Los_Angeles",
            "2026-10-02T16:00",
            86400,
            3,
        ),
    ],
)
def test_persisted_schedule_anchor_does_not_reset_count(
    task, rule, created, timezone, first, interval, count
) -> None:
    from dataclasses import replace

    anchor = int(datetime.fromisoformat(created).replace(tzinfo=UTC).timestamp())
    first_run = int(datetime.fromisoformat(first).replace(tzinfo=UTC).timestamp())
    counted = replace(task, created_at=anchor, rrule=rule, timezone=timezone)
    after = anchor
    for index in range(count):
        expected = first_run + index * interval
        assert next_scheduled_time(counted, after) == expected
        after = expected
    assert next_scheduled_time(counted, after) is None
    # A later lookup (including after downtime) uses the original count window.
    assert next_scheduled_time(counted, first_run) == first_run + interval
    assert next_scheduled_time(counted, after + interval) is None


def test_persisted_schedule_keeps_local_time_across_daylight_saving(task) -> None:
    from dataclasses import replace

    task = replace(
        task,
        created_at=int(datetime(2026, 3, 2, 18, tzinfo=UTC).timestamp()),
        timezone="America/Los_Angeles",
        rrule="FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=0",
    )
    before = int(datetime(2026, 3, 7, 16, tzinfo=UTC).timestamp())
    assert next_scheduled_time(task, int(datetime(2026, 3, 5, tzinfo=UTC).timestamp())) == int(
        datetime(2026, 3, 6, 17, tzinfo=UTC).timestamp()
    )
    first = next_scheduled_time(task, before)
    assert first == int(datetime(2026, 3, 8, 16, tzinfo=UTC).timestamp())
    assert next_scheduled_time(task, int(datetime(2026, 3, 9, tzinfo=UTC).timestamp())) == int(
        datetime(2026, 3, 10, 16, tzinfo=UTC).timestamp()
    )


def test_persisted_schedule_propagates_search_budget_errors(task, monkeypatch) -> None:
    monkeypatch.setattr("omnigent.server.scheduled.rrule._MAX_SEARCH_STEPS", 0)
    with pytest.raises(RRuleSearchError, match="work limit"):
        next_scheduled_time(task, task.created_at)


def test_persisted_schedule_logs_timezone_fallback(task, caplog) -> None:
    from dataclasses import replace

    invalid_zone = replace(task, timezone="Invalid/Timezone")
    assert next_scheduled_time(invalid_zone, task.created_at) == next_scheduled_time(
        task, task.created_at
    )
    assert "Invalid/Timezone" in caplog.text
    assert "defaulting to UTC" in caplog.text


@pytest.mark.parametrize("changes", [{"state": "paused"}, {"rrule": "not a rule"}])
def test_inactive_or_invalid_schedule_has_no_next_occurrence(task, changes) -> None:
    from dataclasses import replace

    assert next_scheduled_time(replace(task, **changes), task.created_at) is None


# A timer anchored here with a 120-second interval first fires at _ANCHOR + 120.
_ANCHOR = 1_800_000_000


@pytest.mark.parametrize(
    "repeat,after,expected",
    [
        (True, _ANCHOR, _ANCHOR + 120),
        # Saved a moment late: the first firing is still the next one, not skipped.
        (True, _ANCHOR + 119, _ANCHOR + 120),
        # Strictly after: claiming the firing due now moves to the next tick.
        (True, _ANCHOR + 120, _ANCHOR + 240),
        # Back from a long outage: missed ticks are skipped, the phase is kept.
        (True, _ANCHOR + 1000, _ANCHOR + 1080),
        (False, _ANCHOR + 119, _ANCHOR + 120),
        # A one-shot timer has no firing after its only one.
        (False, _ANCHOR + 120, None),
        (False, _ANCHOR + 5000, None),
    ],
    ids=[
        "repeat-first",
        "repeat-just-before",
        "repeat-at-firing",
        "repeat-after-outage",
        "once-just-before",
        "once-at-firing",
        "once-long-after",
    ],
)
def test_timer_firings_fall_on_whole_intervals_from_the_anchor(
    task, repeat: bool, after: int, expected: int | None
) -> None:
    from dataclasses import replace

    timer = replace(
        task,
        kind="timer",
        rrule=timer_rule(120, repeat=repeat),
        created_at=_ANCHOR,
        session_mode="continue",
    )
    assert next_scheduled_time(timer, after) == expected


def test_timer_rules_round_trip_their_interval() -> None:
    assert timer_rule(120, repeat=True) == "FREQ=SECONDLY;INTERVAL=120"
    assert timer_rule(1, repeat=False) == "FREQ=SECONDLY;INTERVAL=1;COUNT=1"
    assert timer_interval(timer_rule(1_000_000, repeat=True)) == 1_000_000


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=DAILY",
        "FREQ=SECONDLY;INTERVAL=0",
        "FREQ=SECONDLY;INTERVAL=1000001",
        "FREQ=SECONDLY;INTERVAL=5;COUNT=2",
        "FREQ=SECONDLY;INTERVAL=05",
    ],
)
def test_a_rule_a_timer_never_saves_is_invalid(task, rule: str) -> None:
    from dataclasses import replace

    with pytest.raises(RRuleValidationError):
        timer_interval(rule)
    # The scheduler finds no firing for it instead of failing the poll.
    assert next_scheduled_time(replace(task, kind="timer", rrule=rule), _ANCHOR) is None
