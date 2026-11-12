"""Session triggers: matching Omnigent session events to automations.

Runs :func:`session_triggers.notify` against real SQLite stores and checks the
pending runs it records: filters, the loop guard, the owner's access, and the
IDs-only prompt a run sends.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.entities import Conversation, ScheduledTask
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, RESERVED_USER_PUBLIC
from omnigent.server.scheduled import session_triggers
from omnigent.server.scheduled.session_triggers import (
    parse_session_trigger,
    render_event_prompt,
)
from omnigent.stores.conversation_store import SCHEDULED_TASK_ORIGIN_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

ALICE = "alice@example.com"
BOB = "bob@example.com"
AGENT = uuid.uuid4().hex


class _Stores:
    def __init__(self, db_uri: str) -> None:
        self.tasks = SqlAlchemyScheduledTaskStore(db_uri)
        self.tasks.configure_dispatch(True)
        self.conversations = SqlAlchemyConversationStore(db_uri)
        self.permissions = SqlAlchemyPermissionStore(db_uri)
        for user in (ALICE, BOB):
            self.permissions.ensure_user(user)

    def task(self, *, owner: str = ALICE, **trigger: Any) -> ScheduledTask:
        session_mode = trigger.pop("session_mode", "new")
        session_id = trigger.pop("session_id", None)
        config = {"provider": "omnigent", "events": ["turn.completed"], **trigger}
        task = self.tasks.create(
            uuid.uuid4().hex,
            "watcher",
            "Review the session.",
            None,
            owner,
            AGENT,
            "UTC",
            host_id=uuid.uuid4().hex,
            session_mode=session_mode,
            session_id=session_id,
            trigger_config=config,
        )
        session_triggers.invalidate_tasks()
        return task

    def session(self, *, owner: str = ALICE, **kwargs: Any) -> Conversation:
        conv = self.conversations.create_conversation(
            agent_id=kwargs.pop("agent_id", AGENT), **kwargs
        )
        self.permissions.grant(owner, conv.id, LEVEL_OWNER)
        return conv

    def events(self, task: ScheduledTask) -> list[dict[str, Any]]:
        runs, _ = self.tasks.list_runs(task.id)
        return [event for run in runs for event in (run.event or {}).get("events", [])]


@pytest.fixture()
def stores(db_uri: str) -> Iterator[_Stores]:
    stores = _Stores(db_uri)
    session_triggers.configure(stores.tasks, stores.conversations, stores.permissions)
    try:
        yield stores
    finally:
        session_triggers.configure(None)


def _notify(event_type: str, session_id: str, **kwargs: Any) -> None:
    future = session_triggers.notify(event_type, session_id, **kwargs)  # type: ignore[arg-type]
    assert future is not None
    future.result(timeout=10)


def test_a_matching_event_records_a_pending_run_with_ids_only(stores: _Stores) -> None:
    task = stores.task()
    conv = stores.session(title="Secret plans")
    _notify("turn.completed", conv.id, occurrence="resp_1", response_id="resp_1")
    _notify("turn.completed", conv.id, occurrence="resp_1", response_id="resp_1")
    _notify("session.renamed", conv.id)
    [event] = stores.events(task)
    assert event == {
        "provider": "omnigent",
        "type": "turn.completed",
        "session_id": conv.id,
        "response_id": "resp_1",
        "agent_id": AGENT,
        "occurred_at": event["occurred_at"],
        "key": f"event:omnigent:{conv.id}:turn.completed:resp_1",
    }
    [pending] = stores.tasks.list_pending_runs()
    assert pending.host_id == task.host_id


def test_filters_narrow_the_sessions_that_fire(stores: _Stores) -> None:
    other_agent = uuid.uuid4().hex
    project = uuid.uuid4().hex
    by_agent = stores.task(agent_id=other_agent)
    by_title = stores.task(title_contains="DEPLOY")
    by_project = stores.task(project_id=project)
    match = stores.session(agent_id=other_agent, title="Deploy staging", project_id=project)
    miss = stores.session(title="Unrelated")
    for conv in (match, miss):
        _notify("turn.completed", conv.id, occurrence="r")
    assert [e["session_id"] for e in stores.events(by_agent)] == [match.id]
    assert [e["session_id"] for e in stores.events(by_title)] == [match.id]
    assert [e["session_id"] for e in stores.events(by_project)] == [match.id]


def test_one_tasks_recording_failure_leaves_later_matches_their_run(
    monkeypatch: pytest.MonkeyPatch, stores: _Stores
) -> None:
    first, second = stores.task(), stores.task()
    conv = stores.session()
    record = stores.tasks.record_event_run
    failed: list[str] = []

    def fail_first(task: ScheduledTask, **kwargs: Any) -> Any:
        if not failed:
            failed.append(task.id)
            raise RuntimeError("store unavailable")
        return record(task, **kwargs)

    monkeypatch.setattr(stores.tasks, "record_event_run", fail_first)
    _notify("turn.completed", conv.id, occurrence="r1")
    [failed_id] = failed
    [recorded] = [t for t in (first, second) if t.id != failed_id]
    assert [e["session_id"] for e in stores.events(recorded)] == [conv.id]
    assert stores.events(first if failed_id == first.id else second) == []
    # The worker keeps serving: the next event reaches both tasks.
    _notify("turn.completed", conv.id, occurrence="r2")
    assert len(stores.events(first)) + len(stores.events(second)) == 3


def test_events_without_an_occurrence_never_collide(
    stores: _Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = stores.task(events=["session.renamed"])
    conv = stores.session()
    # Two renames in the same instant are still two events.
    monkeypatch.setattr(session_triggers.time, "time", lambda: 1_800_000_000.0)
    _notify("session.renamed", conv.id)
    _notify("session.renamed", conv.id)
    assert [e["type"] for e in stores.events(task)] == ["session.renamed"] * 2


def test_long_ids_reach_the_run_as_hashes(stores: _Stores) -> None:
    task = stores.task()
    conv = stores.session()
    _notify("turn.completed", conv.id, occurrence="r" * 400, response_id="r" * 400)
    [event] = stores.events(task)
    assert event["response_id"].startswith("sha256:")
    assert len(event["response_id"]) == len("sha256:") + 64


def test_a_full_queue_drops_new_events(monkeypatch: pytest.MonkeyPatch, stores: _Stores) -> None:
    monkeypatch.setattr(session_triggers, "_MAX_QUEUED_EVENTS", 0)
    assert session_triggers.notify("turn.completed", "conv") is None


def test_a_task_ignores_the_sessions_it_created_or_continues(stores: _Stores) -> None:
    continued = stores.session()
    watcher = stores.task(session_mode="continue", session_id=continued.id)
    created = stores.session(labels={SCHEDULED_TASK_ORIGIN_LABEL_KEY: watcher.id})
    child = stores.conversations.create_conversation(
        agent_id=AGENT, parent_conversation_id=created.id, kind="sub_agent"
    )
    everyone = stores.task(include_own_sessions=True)
    its_own = stores.session(labels={SCHEDULED_TASK_ORIGIN_LABEL_KEY: everyone.id})
    for conv in (continued, created, child, its_own):
        _notify("turn.completed", conv.id, occurrence="r")
    assert {e["session_id"] for e in stores.events(watcher)} == {its_own.id}
    # The opt-in also hears the session it started itself.
    assert {e["session_id"] for e in stores.events(everyone)} == {
        continued.id,
        created.id,
        child.id,
        its_own.id,
    }


def test_a_cached_task_whose_continued_session_changed_records_nothing(stores: _Stores) -> None:
    first, second = stores.session(), stores.session()
    task = stores.task(session_mode="continue", session_id=first.id)
    # Prime this replica's cache, then change the task's session as another replica
    # would, without this replica's cache hearing of it.
    _notify("turn.completed", second.id, occurrence="r0")
    assert [e["session_id"] for e in stores.events(task)] == [second.id]
    stores.tasks.update(task.id, session_id=second.id)
    _notify("turn.completed", second.id, occurrence="r1")
    assert [e["session_id"] for e in stores.events(task)] == [second.id]


def test_admin_access_alone_does_not_fire(stores: _Stores) -> None:
    stores.permissions.ensure_user("admin@example.com", is_admin=True)
    task = stores.task(owner="admin@example.com")
    conv = stores.session(owner=BOB)
    _notify("turn.completed", conv.id, occurrence="r")
    assert stores.events(task) == []


def test_only_sessions_the_owner_holds_a_grant_on_fire(stores: _Stores) -> None:
    task = stores.task()
    bobs = stores.session(owner=BOB)
    public = stores.session(owner=BOB)
    stores.permissions.grant(RESERVED_USER_PUBLIC, public.id, LEVEL_READ)
    shared = stores.session(owner=BOB)
    stores.permissions.grant(ALICE, shared.id, LEVEL_READ)
    child = stores.conversations.create_conversation(
        agent_id=AGENT, parent_conversation_id=shared.id, kind="sub_agent"
    )
    for conv in (bobs, public, shared, child):
        _notify("turn.completed", conv.id, occurrence="r")
    # A sub-agent is read through its root session's grants.
    assert {e["session_id"] for e in stores.events(task)} == {shared.id, child.id}
    assert (
        next(e for e in stores.events(task) if e["session_id"] == child.id)["parent_session_id"]
        == shared.id
    )


async def test_a_deleted_session_fires_from_what_the_route_captured(stores: _Stores) -> None:
    task = stores.task(events=["session.deleted"])
    stores.task(owner=BOB, events=["session.deleted"])
    conv = stores.session()
    # Only listening owners with a grant on the session are captured.
    readers = session_triggers.readers_of(conv, "session.deleted")
    assert readers == frozenset({ALICE})
    assert session_triggers.readers_of(conv, "turn.completed") == frozenset()
    assert await stores.conversations.delete_conversation(conv.id)
    stores.permissions.revoke(ALICE, conv.id)
    _notify("session.deleted", conv.id, occurrence="deleted", conversation=conv, readers=readers)
    assert [e["type"] for e in stores.events(task)] == ["session.deleted"]


def test_notify_is_a_no_op_when_triggers_are_off() -> None:
    session_triggers.configure(None)
    assert not session_triggers.enabled()
    assert session_triggers.notify("turn.completed", "conv") is None


def test_parse_rejects_other_providers_and_bad_configs() -> None:
    assert parse_session_trigger(None) is None
    assert parse_session_trigger({"provider": "jira", "events": ["x"]}) is None
    assert parse_session_trigger({"provider": "omnigent", "events": []}) is None
    assert parse_session_trigger({"provider": "omnigent", "events": ["nope"]}) is None
    config = parse_session_trigger(
        {"provider": "omnigent", "events": ["turn.failed", "turn.completed", "turn.failed"]}
    )
    assert config is not None and config.events == ["turn.completed", "turn.failed"]


def test_the_prompt_lists_events_after_the_task_prompt() -> None:
    assert render_event_prompt("Review.", None) == "Review."
    events = {
        "events": [
            {"type": "turn.completed", "session_id": "s1", "key": "event:omnigent:s1"},
            {"type": "turn.failed", "session_id": "s2", "key": "event:omnigent:s2"},
        ],
        "omitted": 3,
    }
    rendered = render_event_prompt("Review.\n", events)
    assert rendered == (
        "Review.\n\n<omnigent-trigger-events>\n"
        "This run was started by the following session events. They hold IDs only; "
        "use your session tools to read the sessions.\n"
        '{"session_id": "s1", "type": "turn.completed"}\n'
        '{"session_id": "s2", "type": "turn.failed"}\n'
        "(and 3 more events, not listed)\n"
        "</omnigent-trigger-events>"
    )


def test_status_edges_report_turn_ends_with_their_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes._sessions import helpers
    from omnigent.server.schemas import ErrorDetail

    seen: list[tuple[str, str, dict[str, Any]]] = []

    def record(event_type: str, session_id: str, **kwargs: Any) -> None:
        seen.append((event_type, session_id, kwargs))

    monkeypatch.setattr(helpers.session_triggers, "notify", record)
    monkeypatch.setattr(helpers.session_live_state, "persist_live_status", lambda *_: None)
    monkeypatch.setattr(
        helpers.session_live_state, "persist_scheduled_run_completion", lambda *_, **__: None
    )
    monkeypatch.setattr(helpers.session_pull_requests, "notify_turn_end", lambda _: None)
    session = "c0ffee00c0ffee00c0ffee00c0ffee01"
    monkeypatch.delitem(helpers._session_status_cache, session, raising=False)

    # An idle observation mid-turn (a transcript lull, PTY quiet) is no turn end; the
    # confirmed completion that follows is reported once.
    helpers._publish_status(session, "running", response_id="resp_0")
    helpers._publish_status(session, "idle")
    helpers._publish_status(session, "running", response_id="resp_0")
    helpers._publish_status(session, "idle", response_id="resp_obs")
    helpers._publish_status(session, "running", response_id="resp_0")
    helpers._publish_status(
        session, "idle", response_id="resp_0", turn_completed=True, reports_turn_end=True
    )
    # A native turn names its response on the running edge and settles with a bare idle.
    helpers._publish_status(session, "running", response_id="resp_1")
    helpers._publish_status(session, "idle", reports_turn_end=True)
    helpers._publish_status(session, "idle", reports_turn_end=True)
    helpers._publish_status(session, "running", response_id="resp_2")
    helpers._publish_status(session, "failed", error=ErrorDetail(code="runner_error", message="x"))
    helpers._publish_status(session, "failed")
    # A stop publishes idle without persisting it, which is no turn end.
    helpers._publish_status(session, "running", response_id="resp_3")
    helpers._publish_status(session, "idle", persist_live_status=False, reports_turn_end=True)
    # An anonymous late failure, then the next turn's own failure: two occurrences.
    helpers._publish_status(session, "running", response_id="resp_6")
    helpers._publish_status(session, "failed")
    helpers._publish_status(session, "running", response_id="resp_6")
    helpers._publish_status(session, "failed", response_id="resp_6")
    helpers._publish_status(session, "idle", persist_live_status=False, reports_turn_end=True)
    # A short turn can start and end between status polls: its confirmed completion
    # still counts, and a replay of it repeats the same key, which records nothing.
    helpers._publish_status(
        session, "idle", response_id="resp_5", turn_completed=True, reports_turn_end=True
    )
    helpers._publish_status(
        session, "idle", response_id="resp_5", turn_completed=True, reports_turn_end=True
    )
    # Without a response there is nothing to deduplicate by, so it is not reported.
    helpers._publish_status(session, "idle", turn_completed=True, reports_turn_end=True)
    # A previous turn's late failure lands after the next turn started; that turn's
    # confirmed completion still counts.
    helpers._publish_status(session, "running", response_id="resp_4")
    helpers._publish_status(session, "failed")
    helpers._publish_status(
        session, "idle", response_id="resp_4", turn_completed=True, reports_turn_end=True
    )
    assert seen == [
        ("turn.completed", session, {"occurrence": "resp_0", "response_id": "resp_0"}),
        ("turn.completed", session, {"occurrence": "resp_1", "response_id": "resp_1"}),
        (
            "turn.failed",
            session,
            {"occurrence": None, "response_id": None, "error_code": "runner_error"},
        ),
        ("turn.failed", session, {"occurrence": None, "response_id": None, "error_code": None}),
        (
            "turn.failed",
            session,
            {"occurrence": "resp_6", "response_id": "resp_6", "error_code": None},
        ),
        ("turn.completed", session, {"occurrence": "resp_5", "response_id": "resp_5"}),
        ("turn.completed", session, {"occurrence": "resp_5", "response_id": "resp_5"}),
        # The late failure carries no response of its own, so none is reported.
        (
            "turn.failed",
            session,
            {"occurrence": None, "response_id": None, "error_code": None},
        ),
        ("turn.completed", session, {"occurrence": "resp_4", "response_id": "resp_4"}),
    ]


async def test_legacy_timers_never_arm_an_event_triggered_task() -> None:
    from omnigent.server.scheduled.scheduler import ScheduledTaskScheduler

    scheduler = ScheduledTaskScheduler(store=None, on_fire=None)  # type: ignore[arg-type]
    task = ScheduledTask(
        id=uuid.uuid4().hex,
        name="watcher",
        prompt="p",
        rrule=None,
        user_id=None,
        agent_id=AGENT,
        timezone="UTC",
        created_at=0,
        trigger_config={"provider": "omnigent", "events": ["turn.completed"]},
    )
    scheduler.add(task)
    assert scheduler.next_run_at(task.id) is None


def test_a_long_occurrence_is_hashed_into_the_key() -> None:
    from omnigent.server.scheduled.session_triggers import SessionEvent

    event = SessionEvent(
        type="turn.completed", session_id="s" * 32, occurrence="r" * 500, occurred_at=0
    )
    assert len(event.trigger_key) <= 256
    assert (
        event.trigger_key
        == SessionEvent(
            type="turn.completed", session_id="s" * 32, occurrence="r" * 500, occurred_at=1
        ).trigger_key
    )


async def test_the_event_hold_checks_the_continued_sessions_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from omnigent.server.scheduled.fire import build_event_session_busy

    task = SimpleNamespace(continues_session=True, session_id="conv_1")
    activity: dict[str, Any] = {}
    deps = SimpleNamespace(
        scheduled_task_store=SimpleNamespace(get=lambda _id: task),
        conversation_store=SimpleNamespace(get_session_activity=lambda ids: activity),
    )
    busy = build_event_session_busy(deps)  # type: ignore[arg-type]
    run = SimpleNamespace(scheduled_task_id="st_1")

    def session(**fields: Any) -> SimpleNamespace:
        return SimpleNamespace(
            **{"live_status": "idle", "runner_id": None, "pending_elicitation_count": 0, **fields}
        )

    assert await busy(run) is False  # no session recorded activity
    activity["conv_1"] = session()
    assert await busy(run) is False
    activity["conv_1"] = session(live_status="running")
    assert await busy(run) is True
    # An idle native session waiting on an approval or a question is busy too.
    activity["conv_1"] = session(runner_id="runner_1", pending_elicitation_count=1)
    assert await busy(run) is True
    task.continues_session = False
    assert await busy(run) is False


def test_a_mirrored_approval_is_reported_once_by_the_session_that_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        session_triggers, "notify", lambda kind, sid, **kw: seen.append((kind, sid, kw))
    )
    request = {
        "type": "response.elicitation_request",
        "elicitation_id": "elic_1",
        "params": {"target_session_id": "child"},
    }
    # The child's stream, then its parent's and grandparent's mirrored copies.
    for stream in ("child", "parent", "root"):
        session_triggers.observe_elicitation(stream, request)
    # A request without a target belongs to its own stream; other events are ignored.
    session_triggers.observe_elicitation(
        "solo", {"type": "response.elicitation_request", "elicitation_id": "elic_2"}
    )
    session_triggers.observe_elicitation(
        "solo", {"type": "response.elicitation_resolved", "elicitation_id": "elic_2"}
    )
    assert seen == [
        ("input.requested", "child", {"occurrence": "elic_1", "elicitation_id": "elic_1"}),
        ("input.requested", "solo", {"occurrence": "elic_2", "elicitation_id": "elic_2"}),
    ]
