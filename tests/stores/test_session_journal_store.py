"""Tests for :class:`SessionJournalStore` against a migrated database."""

from __future__ import annotations

import asyncio
import random
import string
import threading
from collections.abc import Sequence

import pytest
import sqlalchemy as sa

from omnigent.db import current_query_name
from omnigent.db.compression import EncryptedTextPayload, decode, encode_encrypted
from omnigent.db.db_models import MAX_STORED_VALUE_BYTES
from omnigent.entities import MessageData, NewConversationItem
from omnigent.entities.session_journal import (
    InvalidJournalEntryError,
    JournalFullError,
    StepNotReadyError,
)
from omnigent.errors import StaleCursorError
from omnigent.server.journal_markers import (
    record_journal_checkpoint,
    record_plan_marker,
    record_status_marker,
)
from omnigent.stores import session_journal_store
from omnigent.stores.conversation_store import ConversationAlreadyExistsError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_journal_store import (
    JOURNAL_DELETE_BATCH,
    JournalEntryTooLargeError,
    JournalSessionNotFoundError,
    SessionJournalStore,
    copy_journal_for_fork,
    reserve_fork_journal,
)

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
OTHER = "5d29bee4350489d66feafecfebd94a97"
MISSING = "f" * 32
RUN = "0b6c5f0d8a7e4c3b9a1d2e3f4a5b6c7d"


@pytest.fixture()
def conversations(db_uri: str) -> SqlAlchemyConversationStore:
    conversations = SqlAlchemyConversationStore(db_uri)
    for session in (SESSION, OTHER):
        conversations.create_conversation(conversation_id=session)
    return conversations


@pytest.fixture()
def journal(db_uri: str, conversations: SqlAlchemyConversationStore) -> SessionJournalStore:
    del conversations
    return SessionJournalStore(db_uri)


def _plan(*steps: str) -> dict[str, object]:
    return {"name": "The plan", "steps": [{"id": step, "name": step.title()} for step in steps]}


def _note(
    journal: SessionJournalStore, key: str, *, session: str = SESSION, step: str | None = None
):
    return journal.append(session, "note", {"body": f"note {key}"}, idempotency_key=key, step=step)


def _count(db_uri: str, table: str, session: str) -> int:
    with sa.create_engine(db_uri).connect() as conn:
        return conn.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE conversation_id = :id"),
            {"id": bytes.fromhex(session)},
        ).scalar_one()


def test_appends_take_consecutive_positions_and_plans_count_versions(
    journal: SessionJournalStore,
) -> None:
    first = journal.append(SESSION, "plan", _plan("baseline", "goal"), idempotency_key="tool:1")
    status = journal.append(
        SESSION,
        "status",
        {"status": "done", "text": "p95 430 ms"},
        idempotency_key="tool:2",
        step="baseline",
        scheduled_task_run_id=RUN,
    )
    second = journal.append(
        SESSION, "plan", _plan("baseline", "opt", "goal"), idempotency_key="tool:3"
    )

    assert [first.entry.position, status.entry.position, second.entry.position] == [0, 1, 2]
    assert first.entry.content["version"] == 1 and second.entry.content["version"] == 2
    assert first.entry.step is None and first.entry.content["source"] == "agent"
    assert status.entry.content == {"status": "done", "text": "p95 430 ms"}
    assert status.entry.scheduled_task_run_id == RUN
    assert all(result.created for result in (first, status, second))
    assert len(status.entry.id) == 32 and status.entry.created_at > 0
    # Another session's journal counts from zero.
    assert _note(journal, "tool:1", session=OTHER).entry.position == 0
    current = journal.current_plan(SESSION)
    assert current is not None and current.id == second.entry.id
    assert current.content["version"] == 2


def test_a_retried_append_returns_the_first_attempts_entry(journal: SessionJournalStore) -> None:
    first = _note(journal, "tool:call_1")
    retry = _note(journal, "tool:call_1")
    assert retry.created is False
    assert retry.entry == first.entry
    assert _note(journal, "tool:call_2").entry.position == 1
    with pytest.raises(InvalidJournalEntryError, match="different kind of entry"):
        journal.append(SESSION, "plan", _plan("a"), idempotency_key="tool:call_1")


def test_a_status_without_a_step_names_the_only_step_of_the_plan(
    journal: SessionJournalStore,
) -> None:
    started = {"status": "started"}
    with pytest.raises(InvalidJournalEntryError, match="the session has no plan"):
        journal.append(SESSION, "status", started, idempotency_key="tool:1")
    journal.append(SESSION, "plan", _plan("nightly"), idempotency_key="tool:2")
    status = journal.append(SESSION, "status", started, idempotency_key="tool:3")
    assert status.entry.step == "nightly"
    journal.append(SESSION, "plan", _plan("nightly", "ship"), idempotency_key="tool:4")
    with pytest.raises(InvalidJournalEntryError, match="exactly one step"):
        journal.append(SESSION, "status", started, idempotency_key="tool:5")
    with pytest.raises(InvalidJournalEntryError, match="a plan names no step"):
        journal.append(SESSION, "plan", _plan("a"), idempotency_key="tool:6", step="a")


@pytest.mark.parametrize("saved_workflow", [False, True])
def test_a_step_takes_a_status_only_after_its_dependencies_are_done(
    journal: SessionJournalStore,
    conversations: SqlAlchemyConversationStore,
    saved_workflow: bool,
) -> None:
    steps = [
        {"id": "deploy", "name": "Deploy"},
        {"id": "verify", "name": "Verify routing", "depends_on": ["deploy"]},
        {"id": "video", "name": "Record the video", "depends_on": ["verify"]},
    ]
    plan = {"name": "Demo", "steps": steps}
    journal.append(
        SESSION,
        "plan",
        plan,
        idempotency_key="k0",
        workflow_id=WORKFLOW if saved_workflow else None,
    )

    def status(key: str, step: str, value: str, text: str | None = "x") -> None:
        content = {"status": value, **({"text": text} if value != "started" else {})}
        journal.append(SESSION, "status", content, idempotency_key=key, step=step)

    with pytest.raises(StepNotReadyError, match="verify waits on deploy") as raised:
        status("k1", "verify", "started")
    assert raised.value.waiting == ["deploy"]
    # Waiting for the user and failed need them done too.
    with pytest.raises(StepNotReadyError, match="video waits on verify"):
        status("k1b", "video", "needs_input", "Which region should the video show?")
    with pytest.raises(StepNotReadyError, match="video waits on verify"):
        status("k2", "video", "failed", "The swap never came up")
    status("k3", "deploy", "started")
    status("k4", "deploy", "failed", "Build broke")
    with pytest.raises(StepNotReadyError):
        status("k5", "verify", "started")
    status("k6", "deploy", "done", "Pod 1/1")
    status("k7", "verify", "done", "Routed")
    status("k7b", "video", "needs_input", "Which region should the video show?")
    status("k8", "video", "started")
    # A step outside the current plan waits on nothing here.
    status("k9", "extra", "started")
    # A retry of a write that went through returns it, whatever changed since.
    status("k8", "video", "started")
    assert [entry.step for entry in journal.list_entries(SESSION).entries][-1] == "extra"

    # Replanning keeps completed dependencies from this run.
    journal.append(SESSION, "plan", plan, idempotency_key="replan")
    status("after-replan", "video", "started")
    journal.append(SESSION, "plan", plan, idempotency_key="next-run", workflow_id=WORKFLOW)
    for replanned in (False, True):
        if replanned:
            journal.append(SESSION, "plan", plan, idempotency_key="next-run-replan")
        for value in ("started", "done", "needs_input", "failed"):
            with pytest.raises(StepNotReadyError, match="verify waits on deploy"):
                status(f"next-run-{replanned}-{value}", "verify", value)
    fork = conversations.fork_conversation(SESSION)
    nested = conversations.fork_conversation(fork.id)
    for session_id in (fork.id, nested.id):
        with pytest.raises(StepNotReadyError, match="verify waits on deploy"):
            journal.append(
                session_id,
                "status",
                {"status": "started"},
                idempotency_key="verify",
                step="verify",
            )
        journal.append(
            session_id,
            "status",
            {"status": "done", "text": "Deployed in fork"},
            idempotency_key="deploy",
            step="deploy",
        )
        journal.append(
            session_id, "status", {"status": "started"}, idempotency_key="verify", step="verify"
        )
        assert all(entry.workflow_id is None for entry in journal.list_entries(session_id).entries)
    assert {plan.conversation_id for plan in journal.list_workflow_runs(WORKFLOW).plans} == {
        SESSION
    }
    status("next-run-deployed", "deploy", "done")
    status("next-run-verify", "verify", "started")


def test_an_append_to_a_missing_session_writes_nothing(
    db_uri: str, journal: SessionJournalStore
) -> None:
    with pytest.raises(JournalSessionNotFoundError):
        _note(journal, "tool:1", session=MISSING)
    assert _count(db_uri, "session_journal_heads", MISSING) == 0
    assert _count(db_uri, "session_journal_entries", MISSING) == 0


def test_the_stored_content_limit_counts_compressed_bytes(
    journal: SessionJournalStore,
) -> None:
    # Random text barely compresses: within the raw limit, but not the stored one.
    noise = random.Random(7)
    goal = lambda: "".join(noise.choices(string.ascii_letters + string.digits, k=300))  # noqa: E731
    steps = [{"id": f"s{index}", "name": f"Step {index}", "goal": goal()} for index in range(90)]
    with pytest.raises(JournalEntryTooLargeError) as raised:
        journal.append(
            SESSION, "plan", {"name": "The plan", "steps": steps}, idempotency_key="tool:1"
        )
    assert raised.value.limit == MAX_STORED_VALUE_BYTES == 16 * 1024 - 1
    assert raised.value.size > MAX_STORED_VALUE_BYTES
    assert journal.list_entries(SESSION).entries == []


def test_pages_follow_cursors_in_both_directions_and_filter(journal: SessionJournalStore) -> None:
    journal.append(SESSION, "plan", _plan("a", "b"), idempotency_key="k0")
    for index in range(1, 7):
        _note(journal, f"k{index}", step="a" if index % 2 else "b")
    journal.append(
        SESSION, "status", {"status": "done", "text": "ok"}, idempotency_key="k7", step="a"
    )
    everything = journal.list_entries(SESSION, limit=100).entries
    assert [entry.position for entry in everything] == list(range(8))
    ids = [entry.id for entry in everything]

    newest = journal.list_entries(SESSION, limit=3, order="desc")
    assert [entry.position for entry in newest.entries] == [7, 6, 5] and newest.has_more
    older = journal.list_entries(SESSION, limit=3, order="desc", after=newest.entries[-1].id)
    assert [entry.position for entry in older.entries] == [4, 3, 2]
    caught_up = journal.list_entries(SESSION, order="asc", after=ids[5])
    assert [entry.position for entry in caught_up.entries] == [6, 7] and not caught_up.has_more
    window = journal.list_entries(SESSION, order="asc", after=ids[1], before=ids[5])
    assert [entry.position for entry in window.entries] == [2, 3, 4]
    reverse_window = journal.list_entries(SESSION, order="desc", after=ids[5], before=ids[1])
    assert [entry.position for entry in reverse_window.entries] == [4, 3, 2]

    step_a = journal.list_entries(SESSION, step="a")
    assert [entry.position for entry in step_a.entries] == [1, 3, 5, 7]
    notes_a = journal.list_entries(SESSION, kind="note", step="a", order="desc", limit=2)
    assert [entry.position for entry in notes_a.entries] == [5, 3] and notes_a.has_more
    plans = journal.list_entries(SESSION, kind="plan", order="desc", limit=1)
    assert [entry.content["version"] for entry in plans.entries] == [1] and not plans.has_more


def test_page_arguments_are_checked(journal: SessionJournalStore) -> None:
    _note(journal, "k1")
    with pytest.raises(StaleCursorError):
        journal.list_entries(SESSION, after="a" * 32)
    with pytest.raises(ValueError, match="before must be an entry ID"):
        journal.list_entries(SESSION, before="not-an-id")
    with pytest.raises(ValueError, match="limit must be between 1 and 1000"):
        journal.list_entries(SESSION, limit=1001)
    with pytest.raises(ValueError, match="order must be"):
        journal.list_entries(SESSION, order="newest")
    with pytest.raises(ValueError, match="kind must be one of"):
        journal.list_entries(SESSION, kind="objective")
    # A cursor from another session's journal doesn't resolve here.
    other = _note(journal, "k1", session=OTHER).entry.id
    with pytest.raises(StaleCursorError):
        journal.list_entries(SESSION, after=other)


def test_concurrent_appends_get_unique_positions_without_gaps(
    journal: SessionJournalStore,
) -> None:
    errors: list[BaseException] = []

    def append_many(worker: int) -> None:
        try:
            for index in range(10):
                _note(journal, f"tool:{worker}-{index}")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append_many, args=(worker,)) for worker in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    positions = [entry.position for entry in journal.list_entries(SESSION, limit=1000).entries]
    assert positions == list(range(60))


def test_deleting_a_session_deletes_its_journal_in_batches(
    db_uri: str, conversations: SqlAlchemyConversationStore, journal: SessionJournalStore
) -> None:
    for index in range(JOURNAL_DELETE_BATCH + 7):
        _note(journal, f"k{index}")
    _note(journal, "k0", session=OTHER)

    assert asyncio.run(conversations.delete_conversation(SESSION)) is True
    assert _count(db_uri, "session_journal_entries", SESSION) == 0
    assert _count(db_uri, "session_journal_heads", SESSION) == 0
    assert [entry.position for entry in journal.list_entries(OTHER).entries] == [0]
    with pytest.raises(JournalSessionNotFoundError):
        _note(journal, "late")
    assert _count(db_uri, "session_journal_heads", SESSION) == 0


def test_journal_deletion_removes_ranges_across_sessions_in_key_order(
    db_uri: str, journal: SessionJournalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_journal_store, "JOURNAL_DELETE_BATCH", 3)
    for index in range(4):
        _note(journal, f"a{index}")
        _note(journal, f"b{index}", session=OTHER)
    with sa.orm.Session(sa.create_engine(db_uri)) as session:
        removed = [
            session_journal_store.delete_journal_entries_batch(session, [OTHER, SESSION])
            for _ in range(4)
        ]
        session.commit()
    assert removed == [3, 3, 2, 0]
    assert _count(db_uri, "session_journal_entries", SESSION) == 0
    assert _count(db_uri, "session_journal_entries", OTHER) == 0


@pytest.mark.parametrize("failure", [None, "copy", "publish"])
def test_a_fork_carries_the_journal_and_continues_it(
    conversations: SqlAlchemyConversationStore,
    journal: SessionJournalStore,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
    db_uri: str,
) -> None:
    monkeypatch.setattr("omnigent.stores.session_journal_store.JOURNAL_COPY_BATCH", 2)
    journal.append(SESSION, "plan", _plan("deploy", "video"), idempotency_key="k0")
    journal.append(
        SESSION,
        "status",
        {"status": "done", "text": "Pod 1/1"},
        idempotency_key="k1",
        step="deploy",
        scheduled_task_run_id=RUN,
    )
    journal.append(SESSION, "note", {"body": "Swap is up"}, idempotency_key="k2", step="video")
    source = journal.list_entries(SESSION).entries

    reads = 0

    def inspect_batch(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal reads
        if (current_query_name() or "").endswith(
            "copy_fork_journal_batch"
        ) and statement.startswith("SELECT"):
            reads += 1
            assert "LIMIT" in statement
            if reads == 2 and failure == "copy":
                raise RuntimeError("copy interrupted")
        if (
            failure == "publish"
            and (current_query_name() or "").endswith("insert_fork_conversation")
            and statement.startswith("INSERT")
        ):
            raise RuntimeError("publish interrupted")

    sa.event.listen(conversations._engine, "before_cursor_execute", inspect_batch)
    try:
        if failure:
            with pytest.raises(RuntimeError, match=f"{failure} interrupted"):
                conversations.fork_conversation(SESSION)
            assert {c.id for c in conversations.list_conversations().data} == {SESSION, OTHER}
            with sa.create_engine(db_uri).connect() as connection:
                assert (
                    connection.execute(
                        sa.text("SELECT COUNT(*) FROM session_journal_entries")
                    ).scalar_one()
                    == 3
                )
                assert (
                    connection.execute(
                        sa.text("SELECT COUNT(*) FROM session_journal_heads")
                    ).scalar_one()
                    == 1
                )
            return
        fork = conversations.fork_conversation(SESSION)
    finally:
        sa.event.remove(conversations._engine, "before_cursor_execute", inspect_batch)
    assert reads == 2
    copied = journal.list_entries(fork.id).entries
    assert [(e.position, e.kind, e.step, e.content) for e in copied] == [
        (e.position, e.kind, e.step, e.content) for e in source
    ]
    assert [e.created_at for e in copied] == [e.created_at for e in source]
    assert not {e.id for e in copied} & {e.id for e in source}
    # A run's history lists each entry once, under the session that served it.
    assert [e.scheduled_task_run_id for e in copied] == [None, None, None]
    assert journal.current_plan(fork.id).content == journal.current_plan(SESSION).content
    # Retrying a caller-supplied target must preserve its published journal,
    # including when the new source has no journal to stage.
    for source_id in (SESSION, OTHER):
        with pytest.raises(ConversationAlreadyExistsError):
            conversations._fork_conversation_with_id(fork.id, source_id)
        assert journal.list_entries(fork.id).entries == copied
    # The fork goes on from where its copy ends; the parent is untouched.
    started = journal.append(
        fork.id, "status", {"status": "started"}, idempotency_key="k3", step="video"
    )
    assert started.entry.position == 3
    assert len(journal.list_entries(SESSION).entries) == 3


@pytest.mark.parametrize("copy_journal", [False, True])
@pytest.mark.parametrize("state", ["reserved", "staged", "orphaned"])
def test_a_competing_fork_leaves_another_attempts_staged_journal(
    conversations: SqlAlchemyConversationStore,
    journal: SessionJournalStore,
    db_uri: str,
    copy_journal: bool,
    state: str,
) -> None:
    _note(journal, "source")
    reserve_fork_journal(conversations._session_immediate, MISSING)
    if state != "reserved":
        assert copy_journal_for_fork(conversations._session_immediate, SESSION, MISSING) == 1
    if state == "orphaned":
        with sa.create_engine(db_uri).begin() as connection:
            connection.execute(
                sa.text("DELETE FROM session_journal_heads WHERE conversation_id = :id"),
                {"id": bytes.fromhex(MISSING)},
            )

    with pytest.raises(sa.exc.IntegrityError if state == "reserved" else ValueError):
        conversations._fork_conversation_with_id(MISSING, SESSION, copy_journal=copy_journal)
    assert _count(db_uri, "session_journal_entries", MISSING) == int(state != "reserved")
    assert _count(db_uri, "session_journal_heads", MISSING) == int(state != "orphaned")
    assert {c.id for c in conversations.list_conversations().data} == {SESSION, OTHER}


def test_a_side_chat_or_an_empty_fork_starts_with_no_journal(
    conversations: SqlAlchemyConversationStore, journal: SessionJournalStore
) -> None:
    journal.append(SESSION, "plan", _plan("a"), idempotency_key="k0")
    side_chat = conversations.fork_conversation(SESSION, side_chat_parent_id=SESSION)
    empty = conversations.fork_conversation(SESSION, copy_items=False)
    for fork in (side_chat, empty):
        assert journal.list_entries(fork.id).entries == []
        assert journal.current_plan(fork.id) is None


def test_a_fork_that_leaves_the_journal_keeps_its_history_and_starts_one(
    conversations: SqlAlchemyConversationStore, journal: SessionJournalStore
) -> None:
    # An automation run's copy of its source session plans its own work.
    journal.append(SESSION, "plan", _plan("a"), idempotency_key="k0")
    fork = conversations.fork_conversation(SESSION, copy_journal=False)
    assert journal.list_entries(fork.id).entries == []
    first = journal.append(fork.id, "plan", _plan("b"), idempotency_key="k1")
    assert first.entry.position == 0
    assert [e.kind for e in journal.list_entries(SESSION).entries] == ["plan"]


@pytest.mark.asyncio
async def test_a_truncated_fork_copies_only_what_its_history_wrote(
    monkeypatch: pytest.MonkeyPatch,
    conversations: SqlAlchemyConversationStore,
    journal: SessionJournalStore,
) -> None:
    monkeypatch.setattr("omnigent.stores.session_journal_store.now_epoch", lambda: 100)
    monkeypatch.setattr(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch", lambda: 100
    )

    def reply(response_id: str) -> None:
        conversations.append(
            SESSION,
            [
                NewConversationItem(
                    type="message",
                    response_id=response_id,
                    data=MessageData(
                        role="assistant",
                        agent="agent",
                        content=[{"type": "text", "text": "Ready"}],
                    ),
                )
            ],
        )

    reply("before-plan")
    first = journal.append(SESSION, "plan", _plan("a"), idempotency_key="plan").entry
    await record_plan_marker(conversations, SESSION, first, None)
    note = _note(journal, "note").entry
    await record_journal_checkpoint(conversations, SESSION, note)
    reply("copied")
    later = journal.append(SESSION, "plan", _plan("a", "b"), idempotency_key="later").entry
    await record_plan_marker(conversations, SESSION, later, first)
    done = journal.append(
        SESSION,
        "status",
        {"status": "done", "text": "After the cutoff"},
        idempotency_key="done",
        step="a",
    ).entry
    await record_status_marker(conversations, SESSION, done, later)
    reply("left-behind")

    empty = conversations.fork_conversation(SESSION, up_to_response_id="before-plan")
    assert journal.list_entries(empty.id).entries == []
    fork = conversations.fork_conversation(SESSION, up_to_response_id="copied")
    nested = conversations.fork_conversation(fork.id, up_to_response_id="copied")
    for session_id in (fork.id, nested.id):
        copied = journal.list_entries(session_id).entries
        assert [(entry.kind, entry.content) for entry in copied] == [
            (first.kind, first.content),
            (note.kind, note.content),
        ]
        assert {entry.created_at for entry in copied} == {100}
        markers = [
            item.data.resource["entry_id"]
            for item in conversations.list_items(session_id).data
            if item.type == "resource_event"
        ]
        assert markers == [entry.id for entry in copied]
    assert len(journal.list_entries(SESSION).entries) == 4
    full = conversations.fork_conversation(SESSION)
    assert journal.current_plan(full.id).content == later.content
    assert journal.plan_step_statuses([journal.current_plan(full.id)])[0]["a"].content == (
        done.content
    )


WORKFLOW = "9a3c1e5b7d2f4a6c8e0b1d3f5a7c9e2b"
OTHER_WORKFLOW = "1f2e3d4c5b6a79881726354453627180"


def test_a_plan_names_its_workflow_and_the_entries_under_it_carry_it(
    journal: SessionJournalStore,
) -> None:
    run = journal.append(
        SESSION, "plan", _plan("tests"), idempotency_key="k0", workflow_id=WORKFLOW.upper()
    )
    status = journal.append(
        SESSION, "status", {"status": "started"}, idempotency_key="k1", step="tests"
    )
    note = _note(journal, "k2")
    replan = journal.append(SESSION, "plan", _plan("tests", "docs"), idempotency_key="k3")
    after = _note(journal, "k4")

    assert run.entry.workflow_id == WORKFLOW
    assert [status.entry.workflow_id, note.entry.workflow_id] == [WORKFLOW, WORKFLOW]
    # A replan names no workflow, so what follows it carries none.
    assert [replan.entry.workflow_id, after.entry.workflow_id] == [None, None]
    assert [e.workflow_id for e in journal.list_entries(SESSION).entries] == [
        WORKFLOW,
        WORKFLOW,
        WORKFLOW,
        WORKFLOW,
        None,
        None,
    ]
    with pytest.raises(InvalidJournalEntryError, match="only a plan names a workflow"):
        journal.append(
            SESSION,
            "note",
            {"body": "x"},
            idempotency_key="k5",
            workflow_id=WORKFLOW,
        )
    with pytest.raises(InvalidJournalEntryError, match="workflow_id must be a workflow ID"):
        journal.append(SESSION, "plan", _plan("tests"), idempotency_key="k6", workflow_id="nope")


def test_a_workflows_runs_page_newest_first_across_sessions(
    monkeypatch: pytest.MonkeyPatch,
    conversations: SqlAlchemyConversationStore,
    journal: SessionJournalStore,
) -> None:
    times = iter((100, 200, 300, 400, 500, 500, 600))
    monkeypatch.setattr("omnigent.stores.session_journal_store.now_epoch", lambda: next(times))
    journal.append(SESSION, "plan", _plan("a"), idempotency_key="k0", workflow_id=WORKFLOW)
    journal.append(OTHER, "plan", _plan("a"), idempotency_key="k1", workflow_id=WORKFLOW)
    journal.append(SESSION, "plan", _plan("a", "b"), idempotency_key="k2")  # a replan
    journal.append(OTHER, "plan", _plan("x"), idempotency_key="k3", workflow_id=OTHER_WORKFLOW)
    # The same second: the session ID, then the position, breaks the tie.
    journal.append(SESSION, "plan", _plan("c"), idempotency_key="k4", workflow_id=WORKFLOW)
    journal.append(OTHER, "plan", _plan("c"), idempotency_key="k5", workflow_id=WORKFLOW)
    journal.append(
        SESSION, "status", {"status": "done", "text": "ok"}, idempotency_key="k6", step="c"
    )
    conversations.fork_conversation(SESSION)  # A copy isn't a run.

    first = journal.list_workflow_runs(WORKFLOW, limit=2)
    rest = journal.list_workflow_runs(WORKFLOW, limit=2, before=first.next_cursor)

    assert [(p.conversation_id, p.created_at) for p in first.plans] == [
        (OTHER, 500),
        (SESSION, 500),
    ]
    assert [(p.conversation_id, p.created_at) for p in rest.plans] == [
        (OTHER, 200),
        (SESSION, 100),
    ]
    assert rest.next_cursor is None
    assert [p.content["steps"][0]["id"] for p in rest.plans] == ["a", "a"]
    assert journal.list_workflow_runs(OTHER_WORKFLOW).plans[0].conversation_id == OTHER
    with pytest.raises(ValueError, match="cursor"):
        journal.list_workflow_runs(WORKFLOW, before="100.nope.1")
    with pytest.raises(ValueError, match="workflow_id"):
        journal.list_workflow_runs("nope")


@pytest.mark.parametrize("next_workflow", [None, WORKFLOW, OTHER_WORKFLOW])
def test_plan_step_statuses_reads_each_steps_newest_status(
    journal: SessionJournalStore, next_workflow: str | None
) -> None:
    plan = journal.append(
        SESSION, "plan", _plan("a", "b", "c"), idempotency_key="k0", workflow_id=WORKFLOW
    ).entry
    for key, step, status in (("k1", "a", "started"), ("k2", "a", "done"), ("k3", "b", "failed")):
        journal.append(
            SESSION, "status", {"status": status, "text": "x"}, idempotency_key=key, step=step
        )
    other = journal.append(OTHER, "plan", _plan("a"), idempotency_key="k4").entry

    statuses = journal.plan_step_statuses([plan, other])
    assert [{step: e.content["status"] for step, e in s.items()} for s in statuses] == [
        {"a": "done", "b": "failed"},
        {},
    ]
    assert statuses[0]["a"].kind == "status" and statuses[0]["a"].step == "a"

    later = journal.append(
        SESSION,
        "plan",
        _plan("a", "b", "c"),
        idempotency_key="k5",
        workflow_id=next_workflow,
    ).entry
    assert journal.plan_step_statuses([later]) == (
        [statuses[0]] if next_workflow is None else [{}]
    )
    journal.append(SESSION, "status", {"status": "started"}, idempotency_key="k6", step="a")
    journal.append(
        SESSION, "status", {"status": "done", "text": "Finished c"}, idempotency_key="k7", step="c"
    )

    # Replans keep updating a run; a new invocation isolates reused step IDs.
    original_statuses = (
        {"a": "started", "b": "failed", "c": "done"}
        if next_workflow is None
        else {"a": "done", "b": "failed"}
    )
    assert [
        {step: entry.content["status"] for step, entry in current.items()}
        for current in journal.plan_step_statuses([later, other, plan])
    ] == [
        original_statuses if next_workflow is None else {"a": "started", "c": "done"},
        {},
        original_statuses,
    ]


def test_an_automation_runs_start_plan_is_its_first(journal: SessionJournalStore) -> None:
    other_run = "1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f"
    journal.append(SESSION, "plan", _plan("old"), idempotency_key="k0")
    content = {
        "name": "The plan",
        "steps": [
            {"id": "a", "name": "First"},
            {"id": "b", "name": "Second", "depends_on": ["a"]},
        ],
    }
    first = journal.append(
        SESSION, "plan", content, idempotency_key="k1", scheduled_task_run_id=RUN
    ).entry
    journal.append(
        SESSION,
        "status",
        {"status": "done", "text": "Finished a"},
        idempotency_key="a-done",
        step="a",
        scheduled_task_run_id=RUN,
    )
    journal.append(SESSION, "plan", content, idempotency_key="k2", scheduled_task_run_id=RUN)
    journal.append(
        SESSION,
        "status",
        {"status": "done", "text": "Finished b"},
        idempotency_key="b-done",
        step="b",
        scheduled_task_run_id=RUN,
    )
    journal.append(OTHER, "note", {"body": "x"}, idempotency_key="k3", scheduled_task_run_id=RUN)

    plans = journal.run_start_plans([(RUN, SESSION), (other_run, SESSION), (RUN, OTHER)])
    assert [plan.id if plan else None for plan in plans] == [first.id, None, None]
    completed = journal.plan_step_statuses([first])
    assert {step: entry.content["status"] for step, entry in completed[0].items()} == {
        "a": "done",
        "b": "done",
    }
    second = journal.append(
        SESSION, "plan", content, idempotency_key="k4", scheduled_task_run_id=other_run
    ).entry
    with pytest.raises(StepNotReadyError, match="b waits on a"):
        journal.append(
            SESSION,
            "status",
            {"status": "started"},
            idempotency_key="next-b",
            step="b",
            scheduled_task_run_id=other_run,
        )
    journal.append(
        SESSION,
        "status",
        {"status": "started"},
        idempotency_key="next-a",
        step="a",
        scheduled_task_run_id=other_run,
    )
    assert journal.plan_step_statuses([first]) == completed
    assert journal.plan_step_statuses([second])[0]["a"].content["status"] == "started"
    # Retrying the start must not insert another boundary or reset dependencies.
    retry = journal.append(
        SESSION, "plan", content, idempotency_key="k4", scheduled_task_run_id=other_run
    )
    assert not retry.created and retry.entry == second
    assert len(journal.list_entries(SESSION, kind="plan_status").entries) == 2


class _SealedJournal(SessionJournalStore):
    """A deployment that encrypts journal content with a key service, one call per batch."""

    def __init__(self, storage_location: str, *, overhead: int = 4) -> None:
        super().__init__(storage_location)
        self.overhead = overhead
        self.calls: list[tuple[str, int]] = []

    def _encode_contents(self, texts: Sequence[str]) -> list[bytes]:
        self.calls.append(("encrypt", len(texts)))
        return [
            encode_encrypted(
                EncryptedTextPayload(text.encode()[::-1], {"key": "k" * self.overhead})
            )
            for text in texts
        ]

    def _decode_contents(self, stored: Sequence[bytes]) -> list[str]:
        self.calls.append(("decrypt", len(stored)))
        return [
            decode(bytes(value), decrypt=lambda payload: payload.ciphertext[::-1].decode()) or ""
            for value in stored
        ]


def test_a_deployment_encrypts_content_a_batch_at_a_time(
    db_uri: str, conversations: SqlAlchemyConversationStore
) -> None:
    del conversations
    sealed = _SealedJournal(db_uri)
    steps = [{"id": "a", "name": "A"}, {"id": "b", "name": "B", "depends_on": ["a"]}]
    sealed.append(SESSION, "plan", {"name": "The plan", "steps": steps}, idempotency_key="k1")
    sealed.append(
        SESSION, "status", {"status": "done", "text": "ok"}, idempotency_key="k2", step="a"
    )
    sealed.calls.clear()
    # The entry is encrypted before the lock; the plan and the dependency's newest
    # status are each decrypted once.
    sealed.append(SESSION, "status", {"status": "started"}, idempotency_key="k3", step="b")
    assert sealed.calls == [("encrypt", 1), ("decrypt", 1), ("decrypt", 1)]

    sealed.calls.clear()
    entries = sealed.list_entries(SESSION).entries
    assert [entry.content.get("status") for entry in entries] == [None, "done", "started"]
    statuses = sealed.plan_step_statuses([entries[0]])
    assert {step: entry.content["status"] for step, entry in statuses[0].items()} == {
        "a": "done",
        "b": "started",
    }
    assert sealed.calls == [("decrypt", 3), ("decrypt", 2)]

    # Stored values say they're encrypted, so a store without the key fails closed.
    with sa.create_engine(db_uri).connect() as conn:
        stored = conn.execute(
            sa.text("SELECT content FROM session_journal_entries WHERE conversation_id = :id"),
            {"id": bytes.fromhex(SESSION)},
        ).scalars()
        assert {bytes(value)[:2] for value in stored} == {b"\x00\x02"}
    with pytest.raises(ValueError, match="decryptor"):
        SessionJournalStore(db_uri).list_entries(SESSION)

    # The limit counts the encrypted envelope, not just the compressed text.
    bloated = _SealedJournal(db_uri, overhead=MAX_STORED_VALUE_BYTES)
    with pytest.raises(JournalEntryTooLargeError):
        _note(bloated, "k4")


def test_a_full_journal_refuses_new_entries_but_returns_retried_ones(
    journal: SessionJournalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_journal_store, "JOURNAL_MAX_ENTRIES", 3)
    journal.append(SESSION, "plan", _plan("a"), idempotency_key="k0", start_run=False)
    _note(journal, "k1")
    _note(journal, "k2")
    with pytest.raises(JournalFullError):
        _note(journal, "k3")
    assert _note(journal, "k1").created is False
    assert [entry.position for entry in journal.list_entries(SESSION).entries] == [0, 1, 2]
