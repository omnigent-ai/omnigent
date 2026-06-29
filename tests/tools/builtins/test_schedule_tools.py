"""Tests for the scheduler builtin tools (loops & monitors)."""

from __future__ import annotations

import json

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.schedule_store.sqlalchemy_store import SqlAlchemyScheduleStore
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.schedules import (
    CreateLoopTool,
    CreateMonitorTool,
    DeleteScheduleTool,
    ListSchedulesTool,
)


@pytest.fixture()
def conv_id(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A real conversation (FK target) with the schedule store wired in."""
    store = SqlAlchemyScheduleStore(db_uri)
    monkeypatch.setattr("omnigent.runtime.get_schedule_store", lambda: store)
    conv_store = SqlAlchemyConversationStore(db_uri)
    return conv_store.create_conversation(title="ops").id


def test_create_list_delete_flow(conv_id: str) -> None:
    ctx = ToolContext(task_id="t1", agent_id="a1", conversation_id=conv_id)

    loop = json.loads(
        CreateLoopTool().invoke(
            json.dumps({"name": "weekly", "prompt": "Write the report", "cron": "0 22 * * FRI"}),
            ctx,
        )
    )
    assert loop["schedule"]["kind"] == "loop"
    assert loop["schedule"]["cron"] == "0 22 * * FRI"
    assert loop["schedule"]["conversation_id"] == conv_id
    sid = loop["schedule"]["id"]

    # Duplicate name in the same conversation → friendly error.
    dup = json.loads(
        CreateLoopTool().invoke(
            json.dumps({"name": "weekly", "prompt": "x", "cron": "* * * * *"}), ctx
        )
    )
    assert "error" in dup and "already exists" in dup["error"]

    mon = json.loads(
        CreateMonitorTool().invoke(
            json.dumps({"name": "tail", "prompt": "look at {line}", "command": "tail -f app.log"}),
            ctx,
        )
    )
    assert mon["schedule"]["kind"] == "monitor"
    assert mon["schedule"]["command"].startswith("tail -f")

    listed = json.loads(ListSchedulesTool().invoke(json.dumps({}), ctx))
    assert listed["count"] == 2

    gone = json.loads(DeleteScheduleTool().invoke(json.dumps({"schedule_id": sid}), ctx))
    assert gone["deleted"] is True
    again = json.loads(DeleteScheduleTool().invoke(json.dumps({"schedule_id": sid}), ctx))
    assert again["deleted"] is False


def test_validation_and_missing_context(conv_id: str) -> None:
    # conv_id fixture wires the store; use a context WITHOUT a conversation.
    ctx_noconv = ToolContext(task_id="t1", agent_id="a1")

    missing_cron = json.loads(
        CreateLoopTool().invoke(json.dumps({"name": "x", "prompt": "p"}), ctx_noconv)
    )
    assert "error" in missing_cron

    no_conv = json.loads(
        CreateLoopTool().invoke(
            json.dumps({"name": "x", "prompt": "p", "cron": "* * * * *"}), ctx_noconv
        )
    )
    assert "error" in no_conv and "conversation" in no_conv["error"]


def test_store_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.runtime.get_schedule_store", lambda: None)
    out = json.loads(
        ListSchedulesTool().invoke(
            json.dumps({"conversation_id": "conv_x"}),
            ToolContext(task_id="t", agent_id="a"),
        )
    )
    assert "error" in out
