"""Bounded creation attribution and best-effort event delivery."""

import logging
from unittest.mock import Mock

import pytest

from omnigent.entities import ScheduledTask
from omnigent.server.scheduled import telemetry


@pytest.fixture
def task() -> ScheduledTask:
    return ScheduledTask(
        id="task",
        name="name",
        prompt="private prompt",
        rrule="FREQ=HOURLY",
        user_id=None,
        agent_id="agent",
        timezone="UTC",
        created_at=0,
    )


@pytest.mark.parametrize(
    "source,template,expected_source,expected_template",
    [
        (None, None, "api", "unknown"),
        ("ui", "custom", "ui", "custom"),
        ("ui", "jira", "ui", "jira"),
        ("ui", "slack", "ui", "slack"),
        ("ui", "github", "ui", "github"),
        ("ui", "linear", "ui", "linear"),
        ("ui", "follow-up-monitor", "ui", "follow-up-monitor"),
        ("ui", "pr-sweep", "ui", "pr-sweep"),
        ("ui", "news-digest", "ui", "news-digest"),
        ("agent", None, "agent", "unknown"),
        ("private source", "private prompt", "api", "unknown"),
    ],
)
def test_creation_telemetry_bounds_dimensions(
    task, caplog, source, template, expected_source, expected_template
):
    caplog.set_level(logging.INFO)
    telemetry.log_task_created(task, owner="alice@example.com", source=source, template=template)
    [event] = [
        r for r in caplog.records if getattr(r, "event_name", None) == "scheduled_task_created"
    ]
    assert event.user_id == "alice@example.com"
    assert event.attributes["creation_source"] == expected_source
    assert event.attributes["template_id"] == expected_template
    assert "private" not in str(event.attributes)
    assert task.prompt not in str(event.attributes)


def test_creation_telemetry_tolerates_logging_failure(task, monkeypatch):
    log = Mock(side_effect=RuntimeError("logging sink unavailable"))
    monkeypatch.setattr(telemetry._logger, "info", log)

    telemetry.log_task_created(task, owner="alice@example.com", source="ui", template="jira")

    log.assert_called_once()


def test_captured_logs_classify_automation_descendants_and_spend(task, monkeypatch, tmp_path):
    """Reconstruct origin and spend solely from rows sent to the debug sink."""
    import json

    from omnigent import debug_logging
    from omnigent.entities import ScheduledTaskRun
    from omnigent.runtime import session_stream
    from omnigent.server.creation_logging import session_created
    from omnigent.server.schemas import ModelUsage, SessionUsageEvent

    rows = []
    monkeypatch.setattr(debug_logging, "_active_sink", None)
    loggers = [
        logging.getLogger("omnigent"),
        debug_logging.sse_event_logger(),
        debug_logging.audit_event_logger(),
    ]
    for logger in loggers:
        monkeypatch.setattr(logger, "handlers", [])
        monkeypatch.setattr(logger, "level", logging.INFO)
        monkeypatch.setattr(logger, "propagate", False)
    debug_logging.attach_debug_log_sink(
        [loggers[0]], source="server", level=logging.INFO, send=rows.extend
    )
    sink = debug_logging._active_sink
    assert sink is not None
    try:
        with debug_logging.current_session_id_scope(None):
            with debug_logging.current_user_id_scope("alice@example.com"):
                run = ScheduledTaskRun(
                    id="run", scheduled_task_id=task.id, status="running", scheduled_at=0
                )
                with telemetry.scheduled_run_scope(telemetry.run_log_context(task, run)):
                    telemetry.log_session_created("automation")
                session_created("worker", created_by_session_id="automation")
                session_created("child", parent_session_id="worker")
                session_created("nested_worker", created_by_session_id="child")
            with debug_logging.current_user_id_scope("bob@example.com"):
                session_created("interactive")
                session_created("interactive_child", parent_session_id="interactive")

            # Totals include spawn children, but independent workers are separate trees.
            for session_id, owner, tokens in [
                ("automation", "alice", 100),
                ("worker", "alice", 250),
                ("child", "alice", 50),
                ("nested_worker", "alice", 300),
                ("interactive", "bob", 500),
                ("interactive_child", "bob", 100),
                # A later interactive follow-up still belongs to automation origin.
                ("automation", "alice", 120),
                ("automation", "alice", 120),
            ]:
                with debug_logging.current_user_id_scope(f"{owner}@example.com"):
                    event = SessionUsageEvent(
                        type="session.usage",
                        conversation_id=session_id,
                        total_cost_usd=tokens / 100,
                        usage_by_model={"model": ModelUsage(total_tokens=tokens)},
                    )
                    session_stream.publish(session_id, event.model_dump(exclude_none=True))
    finally:
        sink.shutdown(timeout=5)

    # From here on, only serialized rows are available to the reporting query.
    captured = tmp_path / "captured_usage_rows.json"
    captured.write_text(json.dumps(rows, indent=2))
    rows = json.loads(captured.read_text())
    automated = {
        row["session_id"] for row in rows if row["event_name"] == "scheduled_run_session_created"
    }
    creations = {
        row["session_id"]: row["attributes"]
        for row in rows
        if row["event_name"] == "session_created"
    }
    while True:
        descendants = {
            session_id
            for session_id, attrs in creations.items()
            if attrs.get("parent_session_id") in automated
            or attrs.get("created_by_session_id") in automated
        }
        if descendants <= automated:
            break
        automated.update(descendants)
    assert automated == {"automation", "worker", "child", "nested_worker"}
    latest_usage = {row["session_id"]: row for row in rows if row["event_name"] == "session.usage"}
    report = {}
    for cohort, sessions in (
        ("automation", automated),
        ("interactive", set(creations) - automated),
    ):
        # Parent snapshots already include subagent spend; sum each spawn tree once.
        spend_rows = [
            latest_usage[session_id]
            for session_id in sessions
            if creations.get(session_id, {}).get("parent_session_id") not in sessions
        ]
        assert all(row["attributes"]["usage_kind"] == "cumulative" for row in spend_rows)
        assert all(row["attributes"]["usage_scope"] == "session_subtree" for row in spend_rows)
        report[cohort] = {
            "sessions": len(sessions),
            "users": len({latest_usage[session_id]["user_id"] for session_id in sessions}),
            "tokens": sum(int(row["attributes"]["total_tokens"]) for row in spend_rows),
            "cost_usd": round(
                sum(float(row["attributes"]["total_cost_usd"]) for row in spend_rows), 2
            ),
        }
    assert report == {
        "automation": {"sessions": 4, "users": 1, "tokens": 670, "cost_usd": 6.7},
        "interactive": {"sessions": 2, "users": 1, "tokens": 500, "cost_usd": 5.0},
    }
    print(json.dumps({"report": report, "captured_logs": str(captured)}, indent=2))
