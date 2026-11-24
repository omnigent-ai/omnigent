"""Escalation ladder of the project reliability loop, against in-memory fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.server import project_reliability as reliability

STAGES = [
    {"id": "open", "name": "Pending"},
    {"id": "in_progress", "name": "Working"},
    {"id": "done", "name": "Verified"},
]
MINUTE = 60.0


@dataclass
class FakeConv:
    id: str
    updated_at: int
    next_position: int = 5
    live_status: str | None = "idle"
    archived: bool = False
    labels: dict[str, str] = field(default_factory=dict)
    project_id: str = "p1"


class FakeConversations:
    def __init__(self) -> None:
        self.convs: dict[str, FakeConv] = {}
        self.pending: dict[str, list[Any]] = {}

    def get_conversation(self, session_id: str) -> FakeConv | None:
        return self.convs.get(session_id)

    def list_pending_inputs(self, session_id: str) -> list[Any]:
        return self.pending.get(session_id, [])


class FakeInputs:
    def __init__(self, conversations: FakeConversations) -> None:
        self.conversations = conversations
        self.sent: list[tuple[str, str]] = []

    async def enqueue(self, session_id: str, event: Any, **_: Any) -> None:
        self.sent.append((session_id, event.data["content"][0]["text"]))
        self.conversations.convs[session_id].next_position += 1

    async def deliver_pending(self, app: Any, session_id: str) -> bool:
        return True


class FakeTickets:
    def __init__(self, tickets: list[dict[str, Any]], links: dict[str, list[str]]) -> None:
        self.tickets, self.links = tickets, links
        self.notes: list[tuple[str, str]] = []

    def list(self, project_id: str, **_: Any) -> dict[str, Any]:
        return {"stages": STAGES, "data": self.tickets}

    def sessions(self, project_id: str, ticket_id: str, **_: Any) -> dict[str, Any]:
        return {"data": [{"session_id": sid} for sid in self.links.get(ticket_id, [])]}

    def comment(self, project_id: str, ticket_id: str, body: Any, actor: Any) -> dict[str, Any]:
        assert body.audience == "agents"
        self.notes.append((ticket_id, body.text))
        return {"updated_at": 10_000_000}


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    conversations = FakeConversations()
    conversations.convs["lead"] = FakeConv("lead", updated_at=0)
    conversations.convs["worker"] = FakeConv("worker", updated_at=0)
    inputs = FakeInputs(conversations)
    tickets = FakeTickets(
        [{"id": "t1", "title": "Alpha", "stage_id": "in_progress", "updated_at": 0}],
        {"t1": ["worker", "lead"]},
    )
    project = SimpleNamespace(
        id="p1",
        user_id=None,
        main_session_id="lead",
        retired_main_session_ids=[],
        config={"reliability": {"stall_minutes": 10, "step_minutes": 5}},
    )
    projects = SimpleNamespace(get=lambda *a, **k: project, list_main_sessions=list)
    app = SimpleNamespace(
        state=SimpleNamespace(
            project_ticket_notifications=SimpleNamespace(
                inputs=inputs, tickets=tickets, projects=projects
            ),
            permission_store=None,
        )
    )
    loop = reliability.ReliabilityLoop(app)
    restarts: list[str] = []

    async def restart(session_id: str, user_id: Any) -> bool:
        restarts.append(session_id)
        return True

    async def lead_ok(*_: Any) -> bool:
        return True

    monkeypatch.setattr(loop, "_restart", restart)
    monkeypatch.setattr(loop, "_check_lead", lead_ok)
    return SimpleNamespace(
        loop=loop, inputs=inputs, tickets=tickets, conversations=conversations, restarts=restarts
    )


async def test_stalled_worker_is_nudged_restarted_then_escalated(world: SimpleNamespace) -> None:
    await world.loop.check_project("p1", None, now=5 * MINUTE)
    assert world.inputs.sent == []  # below the 10-minute threshold
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    assert [sid for sid, _ in world.inputs.sent] == ["worker"]
    await world.loop.check_project("p1", None, now=13 * MINUTE)
    assert len(world.inputs.sent) == 1  # step cooldown: no double nudge
    await world.loop.check_project("p1", None, now=17 * MINUTE)
    assert world.restarts == ["worker"]
    await world.loop.check_project("p1", None, now=23 * MINUTE)
    assert world.inputs.sent[-1][0] == "lead" and "Alpha" in world.inputs.sent[-1][1]
    await world.loop.check_project("p1", None, now=40 * MINUTE)
    assert sum(sid == "lead" for sid, _ in world.inputs.sent) == 1
    assert [text.split(",")[0] for _, text in world.tickets.notes] == [
        "Reliability check: step 1/3",
        "Reliability check: step 2/3",
        "Reliability check: step 3/3",
    ]


async def test_worker_reply_resets_the_ladder(world: SimpleNamespace) -> None:
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    world.conversations.convs["worker"].next_position += 3
    world.conversations.convs["worker"].updated_at = int(12 * MINUTE)
    await world.loop.check_project("p1", None, now=17 * MINUTE)
    assert world.restarts == [] and "t1" not in world.loop.state["p1"].tickets


async def test_running_or_disabled_work_is_left_alone(world: SimpleNamespace) -> None:
    world.conversations.convs["worker"].live_status = "running"
    await world.loop.check_project("p1", None, now=60 * MINUTE)
    world.conversations.convs["worker"].live_status = "idle"
    project = world.loop.app.state.project_ticket_notifications.projects.get()
    project.config = {"reliability": {"enabled": False}}
    await world.loop.check_project("p1", None, now=90 * MINUTE)
    assert world.inputs.sent == [] and world.tickets.notes == []


async def test_dead_worker_skips_the_nudge(world: SimpleNamespace) -> None:
    world.conversations.convs["worker"].live_status = "failed"
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    assert world.restarts == ["worker"]


def test_settings_defaults_and_overrides() -> None:
    config = reliability.settings(
        {"reliability": {"stall_minutes": 2, "stage_minutes": {"review": 90}, "step_minutes": -1}}
    )
    assert config.threshold("in_progress") == 120 and config.threshold("review") == 5400
    assert config.step_s == 15 * 60 and config.enabled
