"""Tests for SqlAlchemyScheduleStore."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.schedule_store.sqlalchemy_store import SqlAlchemyScheduleStore


def test_create_loop_and_get(
    schedule_store: SqlAlchemyScheduleStore,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation(title="ops").id
    sched = schedule_store.create(
        "sch_1",
        conv,
        "weekly-report",
        "loop",
        "Write the weekly status report.",
        cron="0 22 * * FRI",
    )
    assert sched.id == "sch_1"
    assert sched.kind == "loop"
    assert sched.cron == "0 22 * * FRI"
    assert sched.enabled is True
    assert sched.status == "idle"

    fetched = schedule_store.get("sch_1")
    assert fetched is not None
    assert fetched.prompt.startswith("Write the weekly")


def test_create_monitor(
    schedule_store: SqlAlchemyScheduleStore,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation().id
    mon = schedule_store.create(
        "sch_m",
        conv,
        "tail-errors",
        "monitor",
        "Investigate this log line: {line}",
        command="tail -f /var/log/app.log",
    )
    assert mon.kind == "monitor"
    assert mon.command.startswith("tail -f")
    assert mon.cron is None


def test_duplicate_name_in_conversation_raises(
    schedule_store: SqlAlchemyScheduleStore,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation().id
    schedule_store.create("sch_a", conv, "dupe", "loop", "p", cron="* * * * *")
    with pytest.raises(IntegrityError):
        schedule_store.create("sch_b", conv, "dupe", "loop", "p", cron="* * * * *")


def test_list_for_conversation_and_list_enabled(
    schedule_store: SqlAlchemyScheduleStore,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv_a = conversation_store.create_conversation(title="a").id
    conv_b = conversation_store.create_conversation(title="b").id
    schedule_store.create("sch_on", conv_a, "on", "loop", "p", cron="* * * * *")
    schedule_store.create("sch_off", conv_a, "off", "loop", "p", cron="* * * * *", enabled=False)
    schedule_store.create("sch_other", conv_b, "x", "loop", "p", cron="* * * * *")

    conv_a_list = schedule_store.list_for_conversation(conv_a)
    assert {s.id for s in conv_a_list} == {"sch_on", "sch_off"}

    enabled_ids = {s.id for s in schedule_store.list_enabled()}
    assert "sch_on" in enabled_ids and "sch_other" in enabled_ids
    assert "sch_off" not in enabled_ids


def test_update_and_delete(
    schedule_store: SqlAlchemyScheduleStore,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation().id
    schedule_store.create("sch_u", conv, "u", "monitor", "p", command="echo hi")

    updated = schedule_store.update(
        "sch_u",
        enabled=False,
        status="errored",
        last_run_id="resp_1",
        last_fired_at=123,
    )
    assert updated is not None
    assert updated.enabled is False
    assert updated.status == "errored"
    assert updated.last_run_id == "resp_1"
    assert updated.last_fired_at == 123
    assert updated.updated_at is not None

    assert schedule_store.update("sch_missing", enabled=True) is None

    assert schedule_store.delete("sch_u") is True
    assert schedule_store.get("sch_u") is None
    assert schedule_store.delete("sch_u") is False
