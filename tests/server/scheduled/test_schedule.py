"""Persisted schedule calculations and occurrence identities without a database."""

import uuid

import pytest

from omnigent.entities import ScheduledTask
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


def test_persisted_schedule_anchor_does_not_reset_count(task) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime

    anchor = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
    counted = replace(task, created_at=anchor, rrule="FREQ=DAILY;COUNT=2")
    assert next_scheduled_time(counted, anchor) == anchor + 86400
    assert next_scheduled_time(counted, anchor + 86400) is None


def test_persisted_schedule_keeps_local_time_across_daylight_saving(task) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime

    task = replace(
        task,
        created_at=int(datetime(2026, 3, 1, tzinfo=UTC).timestamp()),
        timezone="America/Los_Angeles",
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    )
    before = int(datetime(2026, 3, 7, 16, tzinfo=UTC).timestamp())
    first = next_scheduled_time(task, before)
    second = next_scheduled_time(task, first)
    assert first == int(datetime(2026, 3, 7, 17, tzinfo=UTC).timestamp())
    assert second == int(datetime(2026, 3, 8, 16, tzinfo=UTC).timestamp())
