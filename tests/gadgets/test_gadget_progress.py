"""Action progress, derived from the sessions doing the work, through GadgetService."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.gadgets import progress as progress_module
from omnigent.gadgets import service as service_module
from omnigent.gadgets.documents import StoreLimits
from omnigent.gadgets.progress import (
    Delivery,
    SessionState,
    SessionStateReader,
    derive_ledger_progress,
)
from omnigent.gadgets.service import GadgetService
from omnigent.stores.gadget_store import InvalidInput, NotFound
from tests.gadgets.conftest import EDIT, Fixture
from tests.gadgets.test_gadget_service import publish_actions_board

NOW = 1_800_000_000


class FakeConversations:
    """Sessions as the conversation store records them; liveness is the runner's stamp."""

    def __init__(self) -> None:
        self.rows: dict[str, SimpleNamespace] = {}
        self.seen: dict[str, int | None] = {}
        self.reads: list[tuple] = []
        # Sessions bound to a runner or host, which a missing stamp then means is not running.
        self.bound: set[str] = set()

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
        bound: bool = False,
        family: str | None = None,
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
            family_id=family,
        )
        self.seen[session_id] = seen
        if bound:
            self.bound.add(session_id)

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
        self.reads.append(("list", root_conversation_id, owned_by, limit))
        rows = [
            row
            for row in self.rows.values()
            if root_conversation_id is None or row.root_conversation_id == root_conversation_id
        ]
        ordered = sorted(rows, key=lambda row: -row.created_at)
        return SimpleNamespace(data=ordered[:limit], has_more=len(ordered) > limit)

    def list_spawn_tree(self, root_conversation_id: str, *, limit: int):
        self.reads.append(("tree", root_conversation_id, limit))
        rows = [
            row for row in self.rows.values() if row.root_conversation_id == root_conversation_id
        ]
        ordered = sorted(rows, key=lambda row: -row.created_at)
        return SimpleNamespace(data=ordered[:limit], has_more=len(ordered) > limit)

    def get_session_families(self, family_ids: list[str]) -> dict:
        self.reads.append(("families", tuple(family_ids), None))
        return {
            family: [row for row in self.rows.values() if row.family_id == family]
            for family in family_ids
        }

    def get_session_connectivity(self, ids: list[str]) -> dict:
        return {
            session_id: SimpleNamespace(
                runner_last_seen=self.seen.get(session_id),
                runner_id="runner_1" if session_id in self.bound else None,
                host_id=None,
            )
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
    gadgets: Fixture, conversations: FakeConversations, readable=lambda _: True, actor: int = 1
) -> GadgetService:
    return gadgets.service(actor, SessionStateReader(conversations, readable))


def request_and_deliver(
    service: GadgetService, app: dict, card: str, session_id: str = "s_gadget"
) -> str:
    action = service.request_action(
        app["id"],
        {"action": "run_card", "params": {"cardId": card}, "operation_id": f"req-{card}"},
    )
    service.record_delivery(app["id"], action["id"], {"session_id": session_id})
    return action["id"]


def status(service: GadgetService, app: dict, action_id: str) -> tuple[str, str | None]:
    item = service.action_progress(app["id"], [action_id])["items"][0]
    return item["status"], item.get("reason")


def test_an_action_runs_while_its_gadget_session_works_and_settles_when_the_session_stops(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget")
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    action = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r1"}
    )
    assert status(service, app, action["id"]) == ("pending", None)

    service.record_delivery(app["id"], action["id"], {"session_id": "s_gadget"})
    assert status(service, app, action["id"]) == ("queued", None)
    conversations.set("s_gadget", status="running", updated=NOW + 5)
    assert status(service, app, action["id"]) == ("running", None)

    # The agent never reports; the session stopping still ends the action.
    conversations.set("s_gadget", status="idle", updated=NOW + 60)
    assert status(service, app, action["id"]) == ("finished_unreported", None)

    entry = service.list_actions(app["id"])["items"][0]
    service.update_action(
        app["id"],
        {"id": entry["id"], "if_version": entry["version"], "operation_id": "u", "state": "done"},
    )
    assert status(service, app, action["id"]) == ("done", None)


def test_an_action_waits_while_a_stopped_gadget_session_wakes_up(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    # The gadget session's runner stopped long ago.
    conversations.add("s_gadget", seen=NOW - 3600)
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    action_id = request_and_deliver(service, app, "c1")

    # The delivered message marks the session working before its runner is back: it is starting.
    conversations.set("s_gadget", status="running", updated=NOW + 1)
    clock["now"] = NOW + 30
    assert status(service, app, action_id) == ("queued", None)
    conversations.set("s_gadget", seen=NOW + 40)
    clock["now"] = NOW + 45
    assert status(service, app, action_id) == ("running", None)

    # A runner that never comes back still ends the action.
    conversations.set("s_gadget", seen=NOW - 3600)
    clock["now"] = NOW + 11 * 60
    assert status(service, app, action_id) == ("interrupted", "runner_offline")


def test_a_session_bound_to_a_runner_that_never_reported_in_is_not_online(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    # Bound to a host's runner, with no heartbeat yet: starting, or gone, not in-process.
    conversations.add("s_gadget", seen=None, bound=True)
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    action_id = request_and_deliver(service, app, "c1")
    conversations.set("s_gadget", status="running", updated=NOW + 1)

    clock["now"] = NOW + 30
    assert status(service, app, action_id) == ("queued", None)
    clock["now"] = NOW + 11 * 60
    assert status(service, app, action_id) == ("interrupted", "runner_offline")


def test_an_independent_worker_does_not_borrow_its_creators_heartbeat(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget", seen=NOW, family="f_gadget")
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    action_id = request_and_deliver(service, app, "c1")
    # Started by the gadget session but bound to its own runner, which never reported in.
    conversations.add(
        "s_worker",
        created=NOW + 1,
        status="running",
        updated=NOW + 1,
        seen=None,
        bound=True,
        labels={"omnigent.created_by_session_id": "s_gadget"},
        family="f_gadget",
    )
    conversations.set("s_gadget", status="idle", updated=NOW + 2, seen=NOW + 11 * 60)

    clock["now"] = NOW + 11 * 60
    # The healthy creator says nothing about the worker's own runner.
    assert status(service, app, action_id) == ("interrupted", "runner_offline")


def test_an_action_stays_running_while_not_every_session_could_be_read(
    gadgets: Fixture, clock: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(progress_module, "MAX_CHILD_SESSIONS", 2)
    conversations = FakeConversations()
    conversations.add("s_gadget")
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    action_id = request_and_deliver(service, app, "c1")
    # An older worker the action started, then newer, idle children that fill the listing.
    conversations.add(
        "s_worker", parent="s_gadget", created=NOW + 1, status="running", updated=NOW + 5
    )
    for n in range(3):
        conversations.add(f"s_new{n}", parent="s_gadget", created=NOW + 10 + n, updated=NOW + 12)
    conversations.set("s_gadget", updated=NOW + 20)
    clock["now"] = NOW + 30

    assert status(service, app, action_id) == ("running", "sessions_not_all_read")


def test_a_child_session_keeps_the_action_running_after_the_gadget_session_hands_it_off(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget")
    conversations.add(
        "s_old_child", parent="s_gadget", created=NOW - 300, status="running", updated=NOW
    )
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    action_id = request_and_deliver(service, app, "c1")

    conversations.add(
        "s_child",
        parent="s_gadget",
        created=NOW + 3,
        status="running",
        updated=NOW + 10,
        seen=None,
    )
    conversations.set("s_gadget", status="idle", updated=NOW + 4)
    # The child runs in the gadget session's runner and has no stamp of its own.
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
    conversations.set("s_gadget", seen=NOW - 600)
    assert status(service, app, action_id) == ("interrupted", "runner_offline")
    item = service.action_progress(app["id"], [action_id])["items"][0]
    assert "s_old_child" not in item["session_ids"]


def test_the_session_an_agent_records_on_the_action_is_the_one_followed(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget")
    conversations.add("s_worker", created=NOW + 2, status="running", updated=NOW + 5)
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
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


def test_a_later_action_waits_while_the_gadget_session_works_on_an_earlier_one(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget", status="running", updated=NOW)
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    first = request_and_deliver(service, app, "c1")
    clock["now"] = NOW + 10
    second = request_and_deliver(service, app, "c2")

    assert status(service, app, first) == ("running", None)
    assert status(service, app, second) == ("queued", None)


@pytest.mark.parametrize("unrelated", [False, True], ids=["few", "long_history"])
@pytest.mark.parametrize("settles", ["idle", "offline"])
def test_asking_again_for_an_open_action_joins_it(
    gadgets: Fixture,
    clock: dict,
    monkeypatch: pytest.MonkeyPatch,
    unrelated: bool,
    settles: str,
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget", status="running", updated=NOW)
    if unrelated:
        # Unrelated sessions, all from before the delivery: they say nothing about this
        # action, so it still settles.
        for n in range(4):
            conversations.add(f"s_other{n}", created=NOW - 900 + n, updated=NOW - 900 + n)
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    first = request_and_deliver(service, app, "c1")

    again = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r2"}
    )
    other = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c2"}, "operation_id": "r3"}
    )
    assert (again["id"], again["merged"]) == (first, True)
    assert other["merged"] is False and other["id"] != first

    if settles == "idle":
        conversations.set("s_gadget", status="idle", updated=NOW + 60)
        assert status(service, app, first) == ("finished_unreported", None)
    else:
        # Its runner stopped reporting and stayed gone past the start grace.
        conversations.set("s_gadget", seen=NOW - 3600)
        conversations.bound.add("s_gadget")
        clock["now"] = NOW + progress_module.RUNNER_START_GRACE_S + 1
        assert status(service, app, first) == ("interrupted", "runner_offline")
    fresh = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r4"}
    )
    assert fresh["merged"] is False and fresh["id"] != first


def test_actions_that_never_start_or_never_arrive_are_interrupted(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget")
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    delivered = request_and_deliver(service, app, "c1")
    undelivered = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c2"}, "operation_id": "r2"}
    )["id"]

    clock["now"] = NOW + progress_module.START_GRACE_S + 1
    assert status(service, app, delivered) == ("interrupted", "not_started")
    assert status(service, app, undelivered) == ("pending", None)
    clock["now"] = NOW + service_module.UNDELIVERED_TTL_S + 1
    assert status(service, app, undelivered) == ("interrupted", "not_delivered")


def test_deliveries_go_only_to_the_callers_own_gadget_session_and_unreadable_sessions_stay_private(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget", status="running", updated=NOW)
    conversations.add("s_someone_else", status="running", updated=NOW)
    hidden = {"s_someone_else"}
    service = progress_service(
        gadgets, conversations, readable=lambda session_id: session_id not in hidden
    )
    app = publish_actions_board(service)
    # Only a session the viewer can read becomes their gadget session.
    with pytest.raises(NotFound):
        service.set_gadget_session(app["id"], {"session_id": "s_someone_else"})
    with pytest.raises(NotFound):
        service.set_gadget_session(app["id"], {"session_id": "s_made_up"})
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    action = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r1"}
    )

    with pytest.raises(InvalidInput):
        service.record_delivery(app["id"], action["id"], {"session_id": "s_someone_else"})
    with pytest.raises(NotFound):
        service.record_delivery(app["id"], "f" * 32, {"session_id": "s_gadget"})
    service.record_delivery(app["id"], action["id"], {"session_id": "s_gadget"})
    # Access to the session lost since: its state stays private.
    hidden.add("s_gadget")
    assert status(service, app, action["id"]) == ("interrupted", "session_unavailable")


def test_progress_of_another_viewers_action_is_its_ledger_state(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget", status="running", updated=NOW)
    owner = progress_service(gadgets, conversations)
    app = publish_actions_board(owner)
    owner.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    action_id = request_and_deliver(owner, app, "c1")
    gadgets.access.share(app, 2, EDIT)
    teammate = progress_service(gadgets, conversations, actor=2)

    assert status(teammate, app, action_id) == ("queued", None)
    # Without IDs each viewer gets the actions they are following.
    assert [item["id"] for item in owner.action_progress(app["id"], [])["items"]] == [action_id]
    assert teammate.action_progress(app["id"], [])["items"] == []
    with pytest.raises(InvalidInput):
        owner.action_progress(app["id"], ["a"] * 21)


def test_independent_sessions_and_background_work_keep_an_action_running(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget", family="f_gadget")
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    action_id = request_and_deliver(service, app, "c1")

    # An independent worker has no parent link, only the label naming the session that started it.
    conversations.add(
        "s_independent",
        created=NOW + 2,
        status="running",
        updated=NOW + 9,
        seen=None,
        labels={"omnigent.created_by_session_id": "s_gadget"},
        family="f_gadget",
    )
    conversations.set("s_gadget", status="idle", updated=NOW + 3)
    assert status(service, app, action_id) == ("running", None)
    item = service.action_progress(app["id"], [action_id])["items"][0]
    assert item["session_ids"] == ["s_gadget", "s_independent"]

    # A shell the session left running between turns is still the action's work.
    conversations.set("s_independent", status="idle", updated=NOW + 20)
    conversations.set("s_gadget", background_work_running=True, updated=NOW + 21)
    assert status(service, app, action_id) == ("running", None)
    conversations.set("s_gadget", background_work_running=False, updated=NOW + 40)
    assert status(service, app, action_id) == ("finished_unreported", None)


def test_progress_rules_without_the_store():
    sessions = {
        "s_gadget": SessionState(
            "s_gadget", NOW - 600, NOW + 50, None, "s_gadget", "idle", True, False, 0, None
        ),
        "s_late": SessionState(
            "s_late", NOW + 40, NOW + 45, "s_gadget", "s_gadget", "running", True, False, 0, None
        ),
    }
    progress = derive_ledger_progress(
        [
            ("a1", {"state": "running"}, Delivery("s_gadget", NOW)),
            ("a2", {"state": "running"}, Delivery("s_gadget", NOW + 30)),
        ],
        sessions=sessions,
        now=NOW + 60,
    )
    # A child started after the gadget session took up the second action belongs to it.
    assert progress["a1"].status == "finished_unreported"
    assert progress["a2"].status == "running"
    # While the second action still waits its turn, a child started meanwhile may be the
    # first one's worker, so the first stays running.
    waiting = derive_ledger_progress(
        [
            ("a1", {"state": "running"}, Delivery("s_gadget", NOW)),
            ("a2", {"state": "queued"}, Delivery("s_gadget", NOW + 30)),
        ],
        sessions=sessions,
        now=NOW + 60,
    )
    assert waiting["a1"].status == "running"
    # Deliveries in the same second keep the order they were made in.
    same_second = derive_ledger_progress(
        [
            ("b", {"state": "running"}, Delivery("s_gadget", NOW + 30, sequence=2)),
            ("a", {"state": "running"}, Delivery("s_gadget", NOW + 30, sequence=1)),
        ],
        sessions=sessions,
        now=NOW + 60,
    )
    assert same_second["a"].status == "finished_unreported"
    assert same_second["b"].status == "running"
    # When not every session that could do the work was read, the work is not called done.
    partial = progress_module.SessionStates(
        {
            **sessions,
            "s_late": SessionState(
                "s_late", NOW + 40, NOW + 45, "s_gadget", "s_gadget", "idle", True, False, 0, None
            ),
        }
    )
    partial.complete = False
    unsure = derive_ledger_progress(
        [("a1", {"state": "running"}, Delivery("s_gadget", NOW))], sessions=partial, now=NOW + 60
    )
    assert unsure["a1"] == progress_module.ActionProgress(
        "running", "sessions_not_all_read", ("s_gadget", "s_late")
    )
    # A session reported running that has been silent past the limit no longer holds the action.
    silent = {
        **sessions,
        "s_late": SessionState(
            "s_late", NOW + 40, NOW + 45, "s_gadget", "s_gadget", "running", True, False, 0, None
        ),
    }
    later = derive_ledger_progress(
        [("a2", {"state": "queued"}, Delivery("s_gadget", NOW + 30))],
        sessions=silent,
        now=NOW + 45 + progress_module.SILENT_RUNNING_LIMIT_S,
    )
    assert later["a2"] == progress_module.ActionProgress(
        "interrupted", "no_activity", ("s_gadget", "s_late")
    )


def test_a_deleted_or_archived_gadget_session_is_no_longer_the_gadget_session(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    conversations.add("s_gadget")
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    assert service.get_gadget_session(app["id"])["session_id"] == "s_gadget"

    conversations.set("s_gadget", archived=True)
    assert service.get_gadget_session(app["id"])["session_id"] is None
    conversations.rows.pop("s_gadget")
    assert service.get_gadget_session(app["id"])["session_id"] is None

    # The next session started from the gadget takes its place.
    conversations.add("s_next")
    service.set_gadget_session(app["id"], {"session_id": "s_next"})
    assert service.get_gadget_session(app["id"])["session_id"] == "s_next"


def test_the_gadget_session_status_follows_the_session_and_its_children(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    assert service.gadget_session_status(app["id"]) == {
        "session_id": None,
        "status": "none",
        "reason": None,
    }

    conversations.add("s_gadget", status="running", updated=NOW)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    assert service.gadget_session_status(app["id"])["status"] == "running"
    conversations.set("s_gadget", status="idle", updated=NOW + 5)
    conversations.add(
        "s_child",
        parent="s_gadget",
        created=NOW + 2,
        status="waiting",
        updated=NOW + 6,
        pending=1,
        seen=None,
    )
    assert service.gadget_session_status(app["id"])["status"] == "needs_input"
    conversations.set("s_child", status="idle", updated=NOW + 9)
    assert service.gadget_session_status(app["id"]) == {
        "session_id": "s_gadget",
        "status": "idle",
        "reason": None,
    }
    conversations.set("s_gadget", status="running", seen=NOW - 600)
    assert service.gadget_session_status(app["id"])["status"] == "interrupted"


def test_the_gadget_session_status_is_unknown_when_its_tree_was_not_all_read(
    gadgets: Fixture, clock: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(progress_module, "MAX_CHILD_SESSIONS", 2)
    conversations = FakeConversations()
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    conversations.add("s_gadget", status="idle", updated=NOW)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    # An older, running child past the listing limit; the newer children read are idle.
    conversations.add(
        "s_old", parent="s_gadget", created=NOW + 1, status="running", updated=NOW + 2
    )
    for n in range(3):
        conversations.add(f"s_new{n}", parent="s_gadget", created=NOW + 10 + n, updated=NOW + 12)
    clock["now"] = NOW + 30
    assert service.gadget_session_status(app["id"]) == {
        "session_id": "s_gadget",
        "status": "unknown",
        "reason": "sessions_not_all_read",
    }


def test_the_gadget_session_status_ignores_the_owners_other_sessions(
    gadgets: Fixture, clock: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A viewer with many other sessions still gets an answer.
    conversations = FakeConversations()
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    conversations.add("s_gadget", status="idle", updated=NOW)
    for n in range(4):
        conversations.add(f"s_other{n}", created=NOW + n, updated=NOW + n)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    clock["now"] = NOW + 30
    assert service.gadget_session_status(app["id"])["status"] == "idle"


def test_the_reader_follows_a_real_conversation_store(db_uri: str) -> None:
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(title="Gadget session")
    child = store.create_conversation(title="Worker", parent_conversation_id=parent.id)
    # A session it started on its own: in its family, labelled with its creator.
    independent = store.create_conversation(
        title="Independent worker", labels={"omnigent.created_by_session_id": parent.id}
    )
    store.add_new_sibling(parent.id, independent.id, user_id=None, move_existing=True)
    store.create_conversation(title="Someone else's work")
    listings: list[dict] = []
    real_list = store.list_conversations

    def list_conversations(**kwargs):
        listings.append(kwargs)
        return real_list(**kwargs)

    store.list_conversations = list_conversations  # type: ignore[method-assign]
    reader = SessionStateReader(store, lambda _session_id: True, user_id="viewer@example.com")

    states = reader.read([parent.id])
    assert set(states) == {parent.id, child.id, independent.id}
    assert states[child.id].parent_id == parent.id
    # Bounded reads only: trees through the root index (list_spawn_tree reads it in id order),
    # never a workspace-wide or per-owner listing, which scan the session history.
    assert listings
    assert all(
        call.get("root_conversation_id") is not None and call.get("sort_by") == "id"
        for call in listings
    )
    assert states[parent.id].status == "idle"
    assert reader.available(parent.id)
    store.update_conversation(parent.id, archived=True)
    assert not reader.available(parent.id)
    assert not SessionStateReader(store, lambda _session_id: False).available(child.id)
    assert not reader.available("0" * 32)


@pytest.mark.parametrize("many_sessions", [False, True], ids=["few", "many_sessions"])
def test_a_gadget_session_that_fails_on_delivery_reports_its_failure_at_once(
    gadgets: Fixture, clock: dict, monkeypatch: pytest.MonkeyPatch, many_sessions: bool
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget", seen=None)
    if many_sessions:
        # Many sessions, none related: the gadget session's own failure is known and says
        # what happened.
        for n in range(4):
            conversations.add(f"s_other{n}", created=NOW - 900 + n, updated=NOW - 900 + n)
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    action_id = request_and_deliver(service, app, "c1")

    # The runner failed to start in the same second the message was posted.
    conversations.set(
        "s_gadget",
        status="failed",
        updated=NOW,
        labels={
            "omnigent.last_task_error_code": "runner_failed_to_start",
            "omnigent.last_task_error_message": "The runner could not start.",
        },
    )
    assert status(service, app, action_id) == ("interrupted", "runner_failed_to_start")
    conversations.set("s_gadget", labels={})
    assert status(service, app, action_id) == ("interrupted", "session_failed")
    # Asking again starts a fresh request instead of joining the failed one.
    again = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "again"}
    )
    assert again["merged"] is False and again["id"] != action_id


def test_a_repeat_of_an_undelivered_action_says_it_was_never_delivered(
    gadgets: Fixture, clock: dict
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget")
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    params = {"cardId": "c1"}
    first = service.request_action(
        app["id"], {"action": "run_card", "params": params, "operation_id": "r1"}
    )
    assert first["merged"] is False and first["delivered"] is False
    # The viewer left the New session it opened; asking again joins the open request and
    # says it still needs handing to a session.
    repeat = service.request_action(
        app["id"], {"action": "run_card", "params": params, "operation_id": "r2"}
    )
    assert (repeat["id"], repeat["merged"], repeat["delivered"]) == (first["id"], True, False)

    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    service.record_delivery(app["id"], first["id"], {"session_id": "s_gadget"})
    delivered = service.request_action(
        app["id"], {"action": "run_card", "params": params, "operation_id": "r3"}
    )
    assert (delivered["id"], delivered["merged"], delivered["delivered"]) == (
        first["id"],
        True,
        True,
    )
    # The viewer can also cancel an action that waits.
    cancelled = service.update_action(
        app["id"],
        {
            "id": first["id"],
            "if_version": delivered["version"],
            "operation_id": "cancel-1",
            "state": "cancelled",
        },
    )
    assert cancelled["value"]["state"] == "cancelled"
    fresh = service.request_action(
        app["id"], {"action": "run_card", "params": params, "operation_id": "r4"}
    )
    assert fresh["merged"] is False and fresh["id"] != first["id"]


def test_routine_action_delivery_never_runs_out_of_viewer_state(
    gadgets: Fixture, clock: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Segments of two revisions; the viewer's record is rewritten twice per action.
    monkeypatch.setattr(
        service_module,
        "_STATE_STORE_LIMITS",
        StoreLimits(max_revisions_per_record=10_000, revisions_per_segment=2),
    )
    conversations = FakeConversations()
    conversations.add("s_gadget")
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    for n in range(6):
        request_and_deliver(service, app, f"c{n}")
    assert len(service.list_actions(app["id"])["items"]) == 6


@pytest.mark.parametrize("length", [123, 124, 128])
def test_an_action_with_an_operation_id_at_the_length_limit_is_tracked_and_delivered_once(
    gadgets: Fixture, clock: dict, length: int
) -> None:
    conversations = FakeConversations()
    conversations.add("s_gadget")
    service = progress_service(gadgets, conversations)
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    request = {
        "action": "run_card",
        "params": {"cardId": "c1"},
        "operation_id": "r" + "x" * (length - 1),
    }

    action = service.request_action(app["id"], request)
    assert action["delivered"] is False
    service.record_delivery(app["id"], action["id"], {"session_id": "s_gadget"})
    assert status(service, app, action["id"]) == ("queued", None)

    replay = service.request_action(app["id"], request)
    assert replay["id"] == action["id"]
    assert replay["delivered"] is True
    assert [item["id"] for item in service.list_actions(app["id"])["items"]] == [action["id"]]
