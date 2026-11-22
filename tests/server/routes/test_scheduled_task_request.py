"""Field bounds on the scheduled-task create request, checked before any route logic."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from omnigent.server.routes.scheduled_tasks import (
    CreateScheduledTaskRequest,
    _resolve_create_timezone,
)
from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_BODY = {
    "name": "reminder",
    "prompt": "ping",
    "rrule": "FREQ=DAILY;BYHOUR=9",
    "agent_id": "ag_1",
}


def test_omitted_timezone_keeps_the_documented_utc_default() -> None:
    assert CreateScheduledTaskRequest(**_BODY).timezone == "UTC"
    timezone_schema = CreateScheduledTaskRequest.model_json_schema()["properties"]["timezone"]
    assert timezone_schema["default"] == "UTC"


@pytest.mark.parametrize(
    ("fields", "recorded", "expected"),
    [
        ({}, "America/Chicago", "UTC"),
        ({"timezone": None}, "America/Chicago", "America/Chicago"),
        ({"timezone": None}, None, "UTC"),
        ({"timezone": "UTC"}, "America/Chicago", "UTC"),
        ({"timezone": "Asia/Tokyo"}, "America/Chicago", "Asia/Tokyo"),
    ],
    ids=["legacy-omission", "opt-in", "no-preference", "explicit-utc", "explicit-zone"],
)
def test_timezone_selection_requires_explicit_opt_in(
    fields: dict[str, str | None], recorded: str | None, expected: str
) -> None:
    store = Mock(spec=ScheduledTaskStore)
    store.get_default_timezone.return_value = recorded
    request = CreateScheduledTaskRequest(**{**_BODY, **fields})

    assert _resolve_create_timezone(request, store, "owner@example.com") == expected
    if "timezone" in fields and fields["timezone"] is None:
        store.get_default_timezone.assert_called_once_with("owner@example.com")
    else:
        store.get_default_timezone.assert_not_called()


# 253402300800 is one second past 9999-12-31T23:59:59Z, the last one datetime formats.
@pytest.mark.parametrize("first_run_at", [0, 253402300800])
def test_out_of_range_first_run_at_is_rejected(first_run_at: int) -> None:
    # Rejected before any datetime formatting in the route can overflow.
    with pytest.raises(ValidationError, match="first_run_at"):
        CreateScheduledTaskRequest(**_BODY, first_run_at=first_run_at)


def test_last_formattable_first_run_at_is_accepted() -> None:
    assert CreateScheduledTaskRequest(**_BODY, first_run_at=253402300799).first_run_at == (
        253402300799
    )


_ONE_TIME = "FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=14;BYHOUR=15;BYMINUTE=0;BYSECOND=0;COUNT=1"


def test_tasks_are_automations_unless_marked() -> None:
    assert CreateScheduledTaskRequest(**_BODY).kind == "automation"


def test_a_reminder_is_a_one_time_message_into_a_given_session() -> None:
    body = {**_BODY, "rrule": _ONE_TIME, "session_mode": "continue", "session_id": "conv_1"}
    assert CreateScheduledTaskRequest(**body, kind="reminder").kind == "reminder"


@pytest.mark.parametrize(
    "fields",
    [
        {"session_mode": "continue", "session_id": "conv_1"},
        {"rrule": _ONE_TIME, "session_mode": "continue"},
    ],
    ids=["recurring", "no_session"],
)
def test_a_reminder_that_does_not_fit_is_rejected(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="kind 'reminder' needs"):
        CreateScheduledTaskRequest(**{**_BODY, **fields}, kind="reminder")


def test_an_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError, match="kind"):
        CreateScheduledTaskRequest(**_BODY, kind="trigger")
