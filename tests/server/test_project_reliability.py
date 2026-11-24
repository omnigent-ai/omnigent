"""Escalation ladder of the project reliability loop, against in-memory fakes."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities.project_ticket import DEFAULT_STAGE_NAMES
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
        self.events_by_ticket.setdefault(ticket_id, []).append(
            {
                "kind": "updated",
                "session_id": actor.session_id,
                "agent_id": actor.agent_id,
                "created_at": ticket["updated_at"],
                "payload": {"changes": {"labels": ticket["labels"]}},
            }
        )
        return dict(ticket)

    def comment(self, project_id: str, ticket_id: str, body: Any, actor: Any) -> dict[str, Any]:
        assert body.audience == "agents" or body.text.startswith("Reliability check: this ticket")
        self.events_by_ticket.setdefault(ticket_id, []).append(
            {**_comment(None, body.text, body.audience), "agent_id": actor.agent_id}
        )
        assert actor.agent_id == reliability.LOOP_AGENT_ID and actor.session_id is None
        self.notes.append((ticket_id, body.text))
        # Like the store, the returned row predates the write's updated_at stamp.
        ticket = next(t for t in self.tickets if t["id"] == ticket_id)
        before, ticket["updated_at"] = ticket["updated_at"], int(time.time())
        return {"updated_at": before}


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> SimpleNamespace:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
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
    restarts: list[str] = []

    async def restart(session_id: str, user_id: Any) -> bool:
        restarts.append(session_id)
        return True

    async def lead_ok(*_: Any) -> bool:
        return True

    def new_loop() -> reliability.ReliabilityLoop:
        fresh = reliability.ReliabilityLoop(app)
        monkeypatch.setattr(fresh, "_restart", restart)
        monkeypatch.setattr(fresh, "_check_lead", lead_ok)
        return fresh

    return SimpleNamespace(
        loop=new_loop(),
        new_loop=new_loop,
        projects=projects,
        inputs=inputs,
        tickets=tickets,
        conversations=conversations,
        restarts=restarts,
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
    off = reliability.settings(
        {"reliability": {"stage_minutes": {"verified": 0, "merged": False, "review": -3}}}
    )
    assert off.unwatched_stages == {"verified", "merged"}
    assert off.threshold("review") == 30 * 60  # invalid values keep the default


async def test_an_unwatched_stage_gets_no_ladder_but_wake_ups_still_fire(world) -> None:
    # A stage that only waits on a push, like Verified, is switched off with 0 minutes.
    project = world.projects.get()
    project.config["reliability"]["stage_minutes"] = {"in_progress": 0}
    for minute in (11, 17, 23, 40):
        await world.loop.check_project("p1", None, now=minute * MINUTE)
    assert world.inputs.sent == [] and world.tickets.notes == [] and world.restarts == []
    _wait_label(world, 60 * MINUTE)
    await world.loop.check_project("p1", None, now=61 * MINUTE)
    assert [sid for sid, _ in world.inputs.sent] == ["lead"]


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
                {
                    **_comment(None, "Reliability check: step 1/3"),
                    "agent_id": reliability.LOOP_AGENT_ID,
                },
            ],
            "ask",
        ),
        ([_comment("worker", "Should I use X?"), _comment("worker", "Using X", "agents")], None),
        ([_comment("worker", "Should I use X?"), _comment("lead", "Owner says use X")], None),
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


async def test_a_later_agent_note_clears_an_earlier_question(world) -> None:
    world.tickets.events_by_ticket["t1"] = [
        _comment("worker", "Should I use X?"),
        _comment("worker", "Using X", audience="agents"),
    ]
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    assert [sid for sid, _ in world.inputs.sent] == ["worker"]
    assert "needs-human" not in str(world.tickets.tickets[0].get("labels"))


async def test_a_restart_mid_cycle_resumes_the_ladder(world) -> None:
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    assert [sid for sid, _ in world.inputs.sent] == ["worker"]
    restarted = world.new_loop()  # a new server process reads the saved ladders
    await restarted.check_project("p1", None, now=13 * MINUTE)
    await restarted.check_project("p1", None, now=17 * MINUTE)
    nudges = [text for _, text in world.inputs.sent if "has had no activity" in text]
    assert len(nudges) == 1  # no second nudge
    assert world.restarts == ["worker"]
    assert [text.split(",")[0] for _, text in world.tickets.notes] == [
        "Reliability check: step 1/3",
        "Reliability check: step 2/3",
    ]


async def test_saved_ladders_are_dropped_with_activity_or_the_project(world) -> None:
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    world.tickets.tickets[0]["updated_at"] = 2**40
    await world.loop.check_project("p1", None, now=12 * MINUTE)
    assert "t1" not in world.new_loop().state.get("p1", reliability.ProjectState()).tickets
    world.tickets.tickets[0]["updated_at"] = 1
    await world.loop.check_project("p1", None, now=30 * MINUTE)
    assert "t1" in world.new_loop().state["p1"].tickets
    await world.loop.check_all(now=31 * MINUTE)  # the project no longer has a lead or was deleted
    assert world.new_loop().state == {}


def test_an_unreadable_ladder_file_is_ignored(world) -> None:
    path = reliability._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"p1": {"tickets": {"t1": {"step": "x"}}}}')
    assert world.new_loop().state == {}


async def _park_on_silence(world: SimpleNamespace) -> None:
    world.conversations.convs["worker"].live_status = "failed"  # e.g. stuck on 502s
    for minute in (11, 17, 23, 29, 35):
        await world.loop.check_project("p1", None, now=minute * MINUTE)
    assert world.tickets.tickets[0]["labels"] == [{"key": "needs-human", "value": None}]


def _keys(world: SimpleNamespace) -> list[str]:
    return [label["key"] for label in world.tickets.tickets[0]["labels"]]


async def test_a_park_clears_when_its_worker_comes_back(world) -> None:
    await _park_on_silence(world)
    await world.loop.check_project("p1", None, now=40 * MINUTE)
    assert _keys(world) == ["needs-human"]  # still dead: nothing moved
    worker = world.conversations.convs["worker"]
    worker.live_status, worker.updated_at = "running", int(time.time()) + 5
    await world.loop.check_project("p1", None, now=45 * MINUTE)
    assert _keys(world) == []
    assert "moving again" in world.tickets.notes[-1][1]


async def test_a_park_clears_when_a_session_moves_the_ticket(world) -> None:
    await _park_on_silence(world)
    world.tickets.events_by_ticket["t1"].append(
        _comment("worker", "Recovered, tests pass", "agents")
    )
    world.tickets.tickets[0]["updated_at"] += 1
    await world.loop.check_project("p1", None, now=45 * MINUTE)
    assert _keys(world) == []


@pytest.mark.parametrize(
    "later",
    [
        _comment("worker", "Should I drop the migration?"),  # a fresh question to the owner
        {  # the worker kept needs-human in its own label edit: the ask is now theirs
            "kind": "updated",
            "session_id": "worker",
            "agent_id": None,
            "payload": {"changes": {"labels": [{"key": "needs-human", "value": None}]}},
        },
    ],
)
async def test_a_park_an_agent_took_over_stays(world, later) -> None:
    await _park_on_silence(world)
    world.tickets.events_by_ticket["t1"].append(later)
    world.tickets.tickets[0]["updated_at"] += 1
    world.conversations.convs["worker"].live_status = "idle"
    world.conversations.convs["worker"].updated_at = int(time.time()) + 5
    await world.loop.check_project("p1", None, now=45 * MINUTE)
    assert _keys(world) == ["needs-human"]


async def test_a_lead_restating_the_park_keeps_it(world) -> None:
    await _park_on_silence(world)
    world.tickets.events_by_ticket["t1"].append(_comment("lead", "Still waiting on the owner"))
    world.tickets.tickets[0]["updated_at"] += 1
    await world.loop.check_project("p1", None, now=45 * MINUTE)
    assert _keys(world) == ["needs-human"]


async def test_an_agent_raised_ask_is_never_cleared(world) -> None:
    world.tickets.tickets[0]["labels"] = [{"key": "needs-human", "value": None}]
    world.tickets.events_by_ticket["t1"] = [
        {
            "kind": "updated",
            "session_id": "worker",
            "agent_id": None,
            "payload": {"changes": {"labels": [{"key": "needs-human", "value": None}]}},
        },
        _comment("worker", "Blocked on 502s, retrying", "agents"),
    ]
    world.conversations.convs["worker"].live_status = "running"
    await world.loop.check_project("p1", None, now=45 * MINUTE)
    assert _keys(world) == ["needs-human"] and world.tickets.notes == []


async def test_a_park_on_a_question_ignores_mere_worker_activity(world) -> None:
    world.tickets.events_by_ticket["t1"] = [_comment("worker", "May I open the PR?")]
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    assert _keys(world) == ["needs-human"]
    world.conversations.convs["worker"].live_status = "running"
    await world.loop.check_project("p1", None, now=20 * MINUTE)
    assert _keys(world) == ["needs-human"]


# A board like this project's: Verified comes after Review, before Merged.
SHIP_STAGES = [
    {"id": "open", "name": "Pending"},
    {"id": "in_progress", "name": "Working"},
    {"id": "code_review", "name": "Review"},
    {"id": "verified", "name": "Verified"},
    {"id": "merged", "name": "Merged"},
]
# The shipped default board, where the stage before Done is Review itself.
DEFAULT_STAGES = [{"id": stage_id, "name": name} for stage_id, name in DEFAULT_STAGE_NAMES.items()]


async def test_work_waiting_to_ship_is_not_chased_but_the_lead_is_reminded_once(world) -> None:
    world.tickets.list = lambda project_id, **_: {
        "stages": SHIP_STAGES,
        "data": world.tickets.tickets,
    }
    world.tickets.tickets[0]["stage_id"] = "verified"

    def ship_reminders():
        return [text for sid, text in world.inputs.sent if "waiting to ship" in text]

    # No nudge, restart or park for the worker of finished work, however long it sits.
    for minute in (11, 17, 23, 40, 23 * 60):
        await world.loop.check_project("p1", None, now=minute * MINUTE)
    assert [sid for sid, _ in world.inputs.sent if sid != "lead"] == []
    assert world.tickets.notes == [] and world.restarts == [] and ship_reminders() == []
    assert world.tickets.tickets[0].get("labels") is None
    # After a day the Lead hears about it once, and not again until the ticket moves.
    await world.loop.check_project("p1", None, now=24 * 60 * MINUTE + 60)
    await world.loop.check_project("p1", None, now=30 * 60 * MINUTE)
    assert len(ship_reminders()) == 1 and "Alpha" in ship_reminders()[0]
    world.tickets.tickets[0]["updated_at"] = int(31 * 60 * MINUTE)
    await world.loop.check_project("p1", None, now=40 * 60 * MINUTE)
    assert len(ship_reminders()) == 1
    await world.loop.check_project("p1", None, now=56 * 60 * MINUTE)
    assert len(ship_reminders()) == 2
    assert [sid for sid, _ in world.inputs.sent if sid != "lead"] == []


@pytest.mark.parametrize("stage_id", ["in_progress", "code_review"])
async def test_the_other_working_stages_on_a_longer_board_are_still_chased(
    world, stage_id
) -> None:
    world.tickets.list = lambda project_id, **_: {
        "stages": SHIP_STAGES,
        "data": world.tickets.tickets,
    }
    world.tickets.tickets[0]["stage_id"] = stage_id
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    assert [sid for sid, _ in world.inputs.sent] == ["worker"]


async def test_review_on_the_default_board_is_still_chased(world) -> None:
    assert [stage["id"] for stage in DEFAULT_STAGES][-2] == "review"
    world.tickets.list = lambda project_id, **_: {
        "stages": DEFAULT_STAGES,
        "data": world.tickets.tickets,
    }
    world.tickets.tickets[0]["stage_id"] = "review"
    await world.loop.check_project("p1", None, now=11 * MINUTE)
    assert [sid for sid, _ in world.inputs.sent] == ["worker"]


async def test_a_worker_stuck_mid_turn_is_reported_to_the_lead_once(world) -> None:
    worker = world.conversations.convs["worker"]
    worker.live_status = "running"
    for minute in (0, 5, 9):
        await world.loop.check_project("p1", None, now=minute * MINUTE)
    assert world.inputs.sent == []  # below the 10-minute threshold
    for minute in (11, 15, 30):
        await world.loop.check_project("p1", None, now=minute * MINUTE)
    reports = [(sid, text) for sid, text in world.inputs.sent if "mid-turn" in text]
    assert [sid for sid, _ in reports] == ["lead"] and "Alpha" in reports[0][1]
    assert len(world.tickets.notes) == 1 and "no new output" in world.tickets.notes[0][1]
    assert world.restarts == []  # the worker is never interrupted


async def test_a_running_worker_with_new_output_is_left_alone(world) -> None:
    worker = world.conversations.convs["worker"]
    worker.live_status = "running"
    for minute in range(0, 40, 4):
        worker.next_position += 1  # a tool call or message landed
        await world.loop.check_project("p1", None, now=minute * MINUTE)
    assert world.inputs.sent == [] and world.tickets.notes == []
