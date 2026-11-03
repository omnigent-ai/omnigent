"""Persisted schedule calculations and occurrence identities without a database."""

import uuid
from datetime import UTC, datetime

import pytest

from omnigent.entities import ScheduledTask
from omnigent.server.scheduled.rrule import RRuleSearchError
from omnigent.server.scheduled.schedule import next_scheduled_time, occurrence_host_id


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
