"""Action progress, derived from the sessions doing the work, through ThingyService."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.stores.thingy_store import InvalidInput, NotFound
from omnigent.thingies import progress as progress_module
from omnigent.thingies import service as service_module
from omnigent.thingies.progress import (
    Delivery,
    SessionState,
    SessionStateReader,
    derive_ledger_progress,
)
from omnigent.thingies.service import ThingyService
from tests.thingies.conftest import EDIT, Fixture
from tests.thingies.test_thingy_service import publish_actions_board

NOW = 1_800_000_000


class FakeConversations:
    """Sessions as the conversation store records them; liveness is the runner's stamp."""

    def __init__(self) -> None:
        self.rows: dict[str, SimpleNamespace] = {}
        self.seen: dict[str, int | None] = {}

    def add(
        self,
        session_id: str,
        *,
        parent: str | None = None,
        created: int = NOW - 600,
        updated: int = NOW - 600,
        status: str | None = "idle",
        seen: int | None = NOW,
        pending: int = 0,
        labels: dict | None = None,
        background: bool = False,
    ) -> None:
        root = self.rows[parent].root_conversation_id if parent else session_id
        self.rows[session_id] = SimpleNamespace(
            id=session_id,
            created_at=created,
            updated_at=updated,
            parent_conversation_id=parent,
            root_conversation_id=root,
            live_status=status,
            pending_elicitation_count=pending,
            labels=labels or {},
            background_work_running=background,
        )
        self.seen[session_id] = seen

    def set(self, session_id: str, **changes: Any) -> None:
        if "seen" in changes:
            self.seen[session_id] = changes.pop("seen")
        for key, value in changes.items():
            setattr(
                self.rows[session_id],
                {"status": "live_status", "updated": "updated_at"}.get(key, key),
                value,
            )

    def get_conversations(self, ids: list[str]) -> dict:
        return {session_id: self.rows[session_id] for session_id in ids if session_id in self.rows}

    def list_conversations(
        self,
        *,
        limit: int,
        kind: Any,
        include_archived: bool,
        root_conversation_id: str | None = None,
        owned_by: str | None = None,
    ):
        assert kind is None and include_archived
        rows = [
            row
            for row in self.rows.values()
            if root_conversation_id is None or row.root_conversation_id == root_conversation_id
        ]
        return SimpleNamespace(data=sorted(rows, key=lambda row: -row.created_at)[:limit])

    def get_session_connectivity(self, ids: list[str]) -> dict:
        return {
            session_id: SimpleNamespace(runner_last_seen=self.seen.get(session_id))
            for session_id in ids
            if session_id in self.rows
        }


@pytest.fixture
def clock(monkeypatch):
    current = {"now": NOW}
    fake_time = SimpleNamespace(time=lambda: current["now"])
    monkeypatch.setattr(service_module, "time", fake_time)
    monkeypatch.setattr(progress_module, "time", fake_time)
    return current


def progress_service(
    thingies: Fixture, conversations: FakeConversations, readable=lambda _: True, actor: int = 1
) -> ThingyService:
    return thingies.service(actor, SessionStateReader(conversations, readable))


def request_and_deliver(
    service: ThingyService, app: dict, card: str, session_id: str = "s_thingy"
) -> str:
    action = service.request_action(
        app["id"],
        {"action": "run_card", "params": {"cardId": card}, "operation_id": f"req-{card}"},
    )
    service.record_delivery(app["id"], action["id"], {"session_id": session_id})
    return action["id"]


def status(service: ThingyService, app: dict, action_id: str) -> tuple[str, str | None]:
    item = service.action_progress(app["id"], [action_id])["items"][0]
    return item["status"], item.get("reason")


def test_an_action_runs_while_its_thingy_session_works_and_settles_when_the_session_stops(
    thingies: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_thingy")
    service = progress_service(thingies, conversations)
    app = publish_actions_board(service)
    service.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    action = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r1"}
    )
    assert status(service, app, action["id"]) == ("pending", None)

    service.record_delivery(app["id"], action["id"], {"session_id": "s_thingy"})
    assert status(service, app, action["id"]) == ("queued", None)
    conversations.set("s_thingy", status="running", updated=NOW + 5)
    assert status(service, app, action["id"]) == ("running", None)

    # The agent never reports; the session stopping still ends the action.
    conversations.set("s_thingy", status="idle", updated=NOW + 60)
    assert status(service, app, action["id"]) == ("finished_unreported", None)

    entry = service.list_actions(app["id"])["items"][0]
    service.update_action(
        app["id"],
        {"id": entry["id"], "if_version": entry["version"], "operation_id": "u", "state": "done"},
    )
    assert status(service, app, action["id"]) == ("done", None)


def test_an_action_waits_while_a_stopped_thingy_session_wakes_up(
    thingies: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    # The thingy session's runner stopped long ago.
    conversations.add("s_thingy", seen=NOW - 3600)
    service = progress_service(thingies, conversations)
    app = publish_actions_board(service)
    service.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    action_id = request_and_deliver(service, app, "c1")

    # The delivered message marks the session working before its runner is back: it is starting.
    conversations.set("s_thingy", status="running", updated=NOW + 1)
    clock["now"] = NOW + 30
    assert status(service, app, action_id) == ("queued", None)
    conversations.set("s_thingy", seen=NOW + 40)
    clock["now"] = NOW + 45
    assert status(service, app, action_id) == ("running", None)

    # A runner that never comes back still ends the action.
    conversations.set("s_thingy", seen=NOW - 3600)
    clock["now"] = NOW + 11 * 60
    assert status(service, app, action_id) == ("interrupted", "runner_offline")


def test_a_child_session_keeps_the_action_running_after_the_thingy_session_hands_it_off(
    thingies: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_thingy")
    conversations.add(
        "s_old_child", parent="s_thingy", created=NOW - 300, status="running", updated=NOW
    )
    service = progress_service(thingies, conversations)
    app = publish_actions_board(service)
    service.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    action_id = request_and_deliver(service, app, "c1")

    conversations.add(
        "s_child",
        parent="s_thingy",
        created=NOW + 3,
        status="running",
        updated=NOW + 10,
        seen=None,
    )
    conversations.set("s_thingy", status="idle", updated=NOW + 4)
    # The child runs in the thingy session's runner and has no stamp of its own.
    assert status(service, app, action_id) == ("running", None)
    conversations.add(
        "s_grandchild",
        parent="s_child",
        created=NOW + 20,
        status="waiting",
        updated=NOW + 20,
        pending=1,
        seen=None,
    )
    conversations.set("s_child", status="idle", updated=NOW + 21)
    assert status(service, app, action_id) == ("needs_input", None)

    # The runner goes away mid-action: the action is interrupted, not left running.
    conversations.set("s_thingy", seen=NOW - 600)
    assert status(service, app, action_id) == ("interrupted", "runner_offline")
    item = service.action_progress(app["id"], [action_id])["items"][0]
    assert "s_old_child" not in item["session_ids"]


def test_the_session_an_agent_records_on_the_action_is_the_one_followed(
    thingies: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_thingy")
    conversations.add("s_worker", created=NOW + 2, status="running", updated=NOW + 5)
    service = progress_service(thingies, conversations)
    app = publish_actions_board(service)
    service.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    action_id = request_and_deliver(service, app, "c1")
    entry = service.list_actions(app["id"])["items"][0]
    service.update_action(
        app["id"],
        {
            "id": entry["id"],
            "if_version": entry["version"],
            "operation_id": "u",
            "state": "running",
            "session_id": "s_worker",
        },
    )

    assert status(service, app, action_id) == ("running", None)
    conversations.set("s_worker", status="failed", updated=NOW + 30, labels={})
    assert status(service, app, action_id) == ("interrupted", "session_failed")


def test_a_later_action_waits_while_the_thingy_session_works_on_an_earlier_one(
    thingies: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_thingy", status="running", updated=NOW)
    service = progress_service(thingies, conversations)
    app = publish_actions_board(service)
    service.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    first = request_and_deliver(service, app, "c1")
    clock["now"] = NOW + 10
    second = request_and_deliver(service, app, "c2")

    assert status(service, app, first) == ("running", None)
    assert status(service, app, second) == ("queued", None)


def test_asking_again_for_an_open_action_joins_it(thingies: Fixture, clock: dict) -> None:
    conversations = FakeConversations()
    conversations.add("s_thingy", status="running", updated=NOW)
    service = progress_service(thingies, conversations)
    app = publish_actions_board(service)
    service.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    first = request_and_deliver(service, app, "c1")

    again = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r2"}
    )
    other = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c2"}, "operation_id": "r3"}
    )
    assert (again["id"], again["merged"]) == (first, True)
    assert other["merged"] is False and other["id"] != first

    conversations.set("s_thingy", status="idle", updated=NOW + 60)
    fresh = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r4"}
    )
    assert fresh["merged"] is False and fresh["id"] != first


def test_actions_that_never_start_or_never_arrive_are_interrupted(
    thingies: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_thingy")
    service = progress_service(thingies, conversations)
    app = publish_actions_board(service)
    service.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    delivered = request_and_deliver(service, app, "c1")
    undelivered = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c2"}, "operation_id": "r2"}
    )["id"]

    clock["now"] = NOW + progress_module.START_GRACE_S + 1
    assert status(service, app, delivered) == ("interrupted", "not_started")
    assert status(service, app, undelivered) == ("pending", None)
    clock["now"] = NOW + service_module.UNDELIVERED_TTL_S + 1
    assert status(service, app, undelivered) == ("interrupted", "not_delivered")


def test_deliveries_go_only_to_the_callers_own_thingy_session_and_unreadable_sessions_stay_private(
    thingies: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_thingy", status="running", updated=NOW)
    service = progress_service(
        thingies, conversations, readable=lambda session_id: session_id != "s_thingy"
    )
    app = publish_actions_board(service)
    service.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    action = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r1"}
    )

    with pytest.raises(InvalidInput):
        service.record_delivery(app["id"], action["id"], {"session_id": "s_someone_else"})
    with pytest.raises(NotFound):
        service.record_delivery(app["id"], "f" * 32, {"session_id": "s_thingy"})
    service.record_delivery(app["id"], action["id"], {"session_id": "s_thingy"})
    assert status(service, app, action["id"]) == ("interrupted", "session_unavailable")


def test_progress_of_another_viewers_action_is_its_ledger_state(
    thingies: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_thingy", status="running", updated=NOW)
    owner = progress_service(thingies, conversations)
    app = publish_actions_board(owner)
    owner.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    action_id = request_and_deliver(owner, app, "c1")
    thingies.access.share(app, 2, EDIT)
    teammate = progress_service(thingies, conversations, actor=2)

    assert status(teammate, app, action_id) == ("queued", None)
    # Without IDs each viewer gets the actions they are following.
    assert [item["id"] for item in owner.action_progress(app["id"], [])["items"]] == [action_id]
    assert teammate.action_progress(app["id"], [])["items"] == []
    with pytest.raises(InvalidInput):
        owner.action_progress(app["id"], ["a"] * 21)


def test_independent_sessions_and_background_work_keep_an_action_running(
    thingies: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_thingy")
    service = progress_service(thingies, conversations)
    app = publish_actions_board(service)
    service.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    action_id = request_and_deliver(service, app, "c1")

    # An independent worker has no parent link, only the label naming the session that started it.
    conversations.add(
        "s_independent",
        created=NOW + 2,
        status="running",
        updated=NOW + 9,
        seen=None,
        labels={"omnigent.created_by_session_id": "s_thingy"},
    )
    conversations.set("s_thingy", status="idle", updated=NOW + 3)
    assert status(service, app, action_id) == ("running", None)
    item = service.action_progress(app["id"], [action_id])["items"][0]
    assert item["session_ids"] == ["s_thingy", "s_independent"]

    # A shell the session left running between turns is still the action's work.
    conversations.set("s_independent", status="idle", updated=NOW + 20)
    conversations.set("s_thingy", background_work_running=True, updated=NOW + 21)
    assert status(service, app, action_id) == ("running", None)
    conversations.set("s_thingy", background_work_running=False, updated=NOW + 40)
    assert status(service, app, action_id) == ("finished_unreported", None)


def test_progress_rules_without_the_store():
    sessions = {
        "s_thingy": SessionState(
            "s_thingy", NOW - 600, NOW + 50, None, "s_thingy", "idle", True, False, 0, None
        ),
        "s_late": SessionState(
            "s_late", NOW + 40, NOW + 45, "s_thingy", "s_thingy", "running", True, False, 0, None
        ),
    }
    progress = derive_ledger_progress(
        [
            ("a1", {"state": "running"}, Delivery("s_thingy", NOW)),
            ("a2", {"state": "queued"}, Delivery("s_thingy", NOW + 30)),
        ],
        sessions=sessions,
        now=NOW + 60,
    )
    # A child started after the second delivery belongs to the second action.
    assert progress["a1"].status == "finished_unreported"
    assert progress["a2"].status == "running"
    # A session reported running that has been silent past the limit no longer holds the action.
    silent = {
        **sessions,
        "s_late": SessionState(
            "s_late", NOW + 40, NOW + 45, "s_thingy", "s_thingy", "running", True, False, 0, None
        ),
    }
    later = derive_ledger_progress(
        [("a2", {"state": "queued"}, Delivery("s_thingy", NOW + 30))],
        sessions=silent,
        now=NOW + 45 + progress_module.SILENT_RUNNING_LIMIT_S,
    )
    assert later["a2"] == progress_module.ActionProgress(
        "interrupted", "no_activity", ("s_thingy", "s_late")
    )


def test_a_deleted_or_archived_thingy_session_is_no_longer_the_thingy_session(
    thingies: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    service = progress_service(thingies, conversations)
    app = publish_actions_board(service)
    conversations.add("s_thingy")
    service.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    assert service.get_thingy_session(app["id"])["session_id"] == "s_thingy"

    conversations.set("s_thingy", archived=True)
    assert service.get_thingy_session(app["id"])["session_id"] is None
    conversations.rows.pop("s_thingy")
    assert service.get_thingy_session(app["id"])["session_id"] is None

    # The next session started from the thingy takes its place.
    conversations.add("s_next")
    service.set_thingy_session(app["id"], {"session_id": "s_next"})
    assert service.get_thingy_session(app["id"])["session_id"] == "s_next"


def test_the_thingy_session_status_follows_the_session_and_its_children(
    thingies: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    service = progress_service(thingies, conversations)
    app = publish_actions_board(service)
    assert service.thingy_session_status(app["id"]) == {
        "session_id": None,
        "status": "none",
        "reason": None,
    }

    conversations.add("s_thingy", status="running", updated=NOW)
    service.set_thingy_session(app["id"], {"session_id": "s_thingy"})
    assert service.thingy_session_status(app["id"])["status"] == "running"
    conversations.set("s_thingy", status="idle", updated=NOW + 5)
    conversations.add(
        "s_child",
        parent="s_thingy",
        created=NOW + 2,
        status="waiting",
        updated=NOW + 6,
        pending=1,
        seen=None,
    )
    assert service.thingy_session_status(app["id"])["status"] == "needs_input"
    conversations.set("s_child", status="idle", updated=NOW + 9)
    assert service.thingy_session_status(app["id"]) == {
        "session_id": "s_thingy",
        "status": "idle",
        "reason": None,
    }
    conversations.set("s_thingy", status="running", seen=NOW - 600)
    assert service.thingy_session_status(app["id"])["status"] == "interrupted"


def test_the_reader_follows_a_real_conversation_store(db_uri: str) -> None:
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(title="Thingy session")
    child = store.create_conversation(title="Worker", parent_conversation_id=parent.id)
    reader = SessionStateReader(store, lambda _session_id: True)

    states = reader.read([parent.id])
    assert set(states) == {parent.id, child.id}
    assert states[child.id].parent_id == parent.id
    assert states[parent.id].status == "idle"
    assert reader.available(parent.id)
    store.update_conversation(parent.id, archived=True)
    assert not reader.available(parent.id)
    assert not SessionStateReader(store, lambda _session_id: False).available(child.id)
    assert not reader.available("0" * 32)
