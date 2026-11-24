"""Escalation ladder of the project reliability loop, against in-memory fakes."""

from __future__ import annotations

import time
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
        self.events_by_ticket: dict[str, list[dict[str, Any]]] = {}

    def list(self, project_id: str, **_: Any) -> dict[str, Any]:
        return {"stages": STAGES, "data": self.tickets}

    def sessions(self, project_id: str, ticket_id: str, **_: Any) -> dict[str, Any]:
        return {"data": [{"session_id": sid} for sid in self.links.get(ticket_id, [])]}

    def events(self, project_id: str, ticket_id: str, **_: Any) -> dict[str, Any]:
        return {"data": self.events_by_ticket.get(ticket_id, []), "has_more": False}

    def update(self, project_id: str, ticket_id: str, body: Any, actor: Any) -> dict[str, Any]:
        ticket = next(t for t in self.tickets if t["id"] == ticket_id)
        ticket["labels"] = [label.model_dump() for label in body.labels]
        ticket["updated_at"] = int(time.time())
        return dict(ticket)

    def comment(self, project_id: str, ticket_id: str, body: Any, actor: Any) -> dict[str, Any]:
        assert body.audience == "agents" or body.text.startswith("Reliability check: this ticket")
        assert actor.agent_id == reliability.LOOP_AGENT_ID and actor.session_id is None
        self.notes.append((ticket_id, body.text))
        # Like the store, the returned row predates the write's updated_at stamp.
        ticket = next(t for t in self.tickets if t["id"] == ticket_id)
        before, ticket["updated_at"] = ticket["updated_at"], int(time.time())
        return {"updated_at": before}


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    conversations = FakeConversations()
    conversations.convs["lead"] = FakeConv("lead", updated_at=0)
    conversations.convs["worker"] = FakeConv("worker", updated_at=0)
    inputs = FakeInputs(conversations)
    tickets = FakeTickets(
        [{"id": "t1", "title": "Alpha", "stage_id": "in_progress", "updated_at": 1}],
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
        "Reliability check: this ticket is waiting on you: nothing has moved for 39 min",
    ]


async def test_a_reply_without_ticket_progress_skips_the_restart(world: SimpleNamespace) -> None:
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    world.conversations.convs["worker"].next_position += 3
    world.conversations.convs["worker"].updated_at = int(12 * MINUTE)
    await world.loop.check_project("p1", None, now=17 * MINUTE)
    assert world.restarts == [] and world.inputs.sent[-1][0] == "lead"
    assert "replied to the nudge" in world.inputs.sent[-1][1]


async def test_ticket_activity_resets_the_ladder(world: SimpleNamespace) -> None:
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    world.tickets.tickets[0]["updated_at"] = 2**40
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


def _wait_label(world: SimpleNamespace, wake_at: float, note: str = "release is out") -> None:
    from datetime import UTC, datetime

    when = datetime.fromtimestamp(wake_at, UTC).isoformat()
    world.tickets.tickets[0]["labels"] = [
        {"key": "keep", "value": None},
        {"key": "waiting", "value": f"{when} {note}"},
    ]


async def test_a_waiting_ticket_is_left_alone_then_woken_once(world: SimpleNamespace) -> None:
    _wait_label(world, 60 * MINUTE)
    for minute in (11, 20, 40, 59):
        await world.loop.check_project("p1", None, now=minute * MINUTE)
    assert world.inputs.sent == [] and world.tickets.notes == [] and world.restarts == []
    await world.loop.check_project("p1", None, now=61 * MINUTE)
    await world.loop.check_project("p1", None, now=62 * MINUTE)
    # No setter is known, so the lead is woken, once, and the label is cleared.
    assert [sid for sid, _ in world.inputs.sent] == ["lead"]
    assert "release is out" in world.inputs.sent[0][1]
    assert world.tickets.tickets[0]["labels"] == [{"key": "keep", "value": None}]


async def test_a_missed_wake_up_resumes_the_ladder(world: SimpleNamespace) -> None:
    _wait_label(world, 30 * MINUTE)
    # The server was down through the wake time and the 10-minute grace.
    await world.loop.check_project("p1", None, now=45 * MINUTE)
    assert world.inputs.sent == [] and "was missed" in world.tickets.notes[-1][1]
    await world.loop.check_project("p1", None, now=46 * MINUTE)
    assert [sid for sid, _ in world.inputs.sent] == ["worker"]


async def test_an_overlong_wait_asks_the_lead_once(world: SimpleNamespace) -> None:
    _wait_label(world, 200 * 3600)
    state = world.loop.state.setdefault("p1", reliability.ProjectState(started_at=0))
    await world.loop.check_project("p1", None, now=MINUTE)
    state.waits["t1"].set_at = 0
    for hours in (73, 74):
        await world.loop.check_project("p1", None, now=hours * 3600)
    confirms = [sid for sid, text in world.inputs.sent if "confirm the wait" in text]
    assert confirms == ["lead"]
    assert all(sid == "lead" for sid, _ in world.inputs.sent)  # the worker is never pinged


@pytest.mark.parametrize(
    "labels",
    [
        [{"key": "needs-human", "value": None}],
        [{"key": "approved", "value": "a" * 40}, {"key": "risk", "value": "migration"}],
    ],
)
async def test_a_ticket_waiting_on_the_owner_is_left_alone(world, labels) -> None:
    world.tickets.tickets[0]["labels"] = labels
    for minute in (11, 20, 40, 90):
        await world.loop.check_project("p1", None, now=minute * MINUTE)
    assert world.inputs.sent == [] and world.tickets.notes == [] and world.restarts == []
    # Once the owner answers, the stall checks resume from the ticket's update.
    world.tickets.tickets[0]["labels"] = []
    world.tickets.tickets[0]["updated_at"] = int(100 * MINUTE)
    await world.loop.check_project("p1", None, now=105 * MINUTE)
    assert world.inputs.sent == []
    await world.loop.check_project("p1", None, now=111 * MINUTE)
    assert [sid for sid, _ in world.inputs.sent] == ["worker"]


async def test_a_long_owner_wait_reminds_the_lead_once(world) -> None:
    world.tickets.tickets[0]["labels"] = [{"key": "needs-human", "value": None}]
    for hours in (0, 23, 25, 30, 50):
        await world.loop.check_project("p1", None, now=hours * 3600 + MINUTE)
    owner = [text for sid, text in world.inputs.sent if "waiting on the owner" in text]
    assert len(owner) == 1 and world.tickets.notes == []


def _comment(session_id: str | None, text: str, audience: str = "human") -> dict[str, Any]:
    return {
        "kind": "comment",
        "session_id": session_id,
        "agent_id": None,
        "created_at": 0,
        "payload": {"text": text, "audience": audience},
    }


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([_comment("worker", "Should I push the branch?")], "ask"),
        ([_comment("worker", "Decision needed: A or B")], "ask"),
        ([_comment("worker", "Plan ready: https://example.com/doc?id=1")], "maybe"),
        ([_comment("worker", "Should I push?", audience="agents")], None),
        ([_comment("lead", "Should I push?")], None),
        ([_comment("worker", "Should I push?"), _comment(None, "Yes")], None),
        (
            [
                _comment("worker", "Should I push?"),
                {
                    "kind": "updated",
                    "session_id": "worker",
                    "payload": {"changes": {"labels": []}},
                },
            ],
            None,
        ),
        (
            [
                _comment("worker", "Should I push?"),
                _comment("worker", "Still on it", audience="agents"),
                {
                    **_comment(None, "Reliability check: step 1/3"),
                    "agent_id": reliability.LOOP_AGENT_ID,
                },
            ],
            "ask",
        ),
    ],
)
def test_implied_owner_wait(events, expected) -> None:
    found = reliability.implied_owner_wait(events, {"lead"})
    assert (found[0] if found else None) == expected


async def test_an_unlabeled_question_is_parked_without_nudging(world) -> None:
    world.tickets.events_by_ticket["t1"] = [_comment("worker", "May I open the PR?")]
    for minute in (11, 17, 23, 40, 90):
        await world.loop.check_project("p1", None, now=minute * MINUTE)
    assert world.inputs.sent == [] and world.restarts == []
    assert [label["key"] for label in world.tickets.tickets[0]["labels"]] == ["needs-human"]
    assert len(world.tickets.notes) == 1 and "question to you" in world.tickets.notes[0][1]


async def test_an_open_update_to_the_owner_is_nudged_once_then_parked(world) -> None:
    world.tickets.events_by_ticket["t1"] = [_comment("worker", "Plan ready for review.")]
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    assert len(world.inputs.sent) == 1 and "needs-human" in world.inputs.sent[0][1]
    for minute in (17, 23, 40):
        await world.loop.check_project("p1", None, now=minute * MINUTE)
    assert len(world.inputs.sent) == 1 and world.restarts == []
    assert world.tickets.tickets[0]["labels"] == [{"key": "needs-human", "value": None}]


async def test_a_stuck_ticket_is_parked_once_after_one_cycle(world) -> None:
    world.tickets.tickets[0]["labels"] = [{"key": "keep", "value": None}]
    for minute in (11, 17, 23, 29, 35, 60, 120):
        await world.loop.check_project("p1", None, now=minute * MINUTE)
    assert sum(sid == "lead" for sid, _ in world.inputs.sent) == 1
    assert [label["key"] for label in world.tickets.tickets[0]["labels"]] == [
        "keep",
        "needs-human",
    ]
    parked = [text for _, text in world.tickets.notes if "waiting on you" in text]
    assert len(parked) == 1 and len(world.tickets.notes) == 4
