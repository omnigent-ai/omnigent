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
