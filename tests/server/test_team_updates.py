"""Team notices: who hears about a settled teammate, once, and when nothing is owed."""

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import pytest

from omnigent.server.team_updates import TeamUpdateNotifier, notify_status, team_update_notice
from omnigent.stores.conversation_store import (
    PROJECT_LABEL_KEY,
    TEAM_REPLY_TO_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path) -> Iterator[SqlAlchemyConversationStore]:
    yield SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'team.db'}")


def _session(store, *, project=None, asker=None, title="Teammate"):
    labels = {}
    if project is not None:
        labels[PROJECT_LABEL_KEY] = project
    if asker is not None:
        labels[TEAM_REPLY_TO_LABEL_KEY] = asker
    conversation = store.create_conversation(conversation_id=uuid4().hex, title=title)
    if labels:
        store.set_labels(conversation.id, labels)
    return conversation.id


def _notifier(store, *, answered=True, delivered=True):
    wakes = []

    async def wake(asker_id, teammate, notice):
        wakes.append((asker_id, teammate.id, notice))
        return delivered

    notifier = TeamUpdateNotifier(
        conversation_store=store,
        wake_dispatch=wake,
        turn_answered=lambda _store, _session_id: answered,
        loop=asyncio.get_running_loop(),
    )
    return notifier, wakes


def _owed(store, session_id):
    return TEAM_REPLY_TO_LABEL_KEY in store.get_conversation(session_id).labels


@pytest.mark.parametrize("status", ["idle", "failed"])
async def test_settled_teammate_wakes_the_asker_exactly_once(store, status):
    lead = _session(store, project="A", title="Lead")
    teammate = _session(store, project="A", asker=lead)
    notifier, wakes = _notifier(store)

    await notifier.deliver(teammate, status)
    await notifier.deliver(teammate, status)

    assert [(asker, sender) for asker, sender, _ in wakes] == [(lead, teammate)]
    assert teammate in wakes[0][2] and "Teammate" in wakes[0][2]
    assert not _owed(store, teammate)


async def test_waiting_teammate_still_owes_its_result(store):
    lead = _session(store, project="A", title="Lead")
    teammate = _session(store, project="A", asker=lead)
    notifier, wakes = _notifier(store)

    await notifier.deliver(teammate, "waiting")
    assert "approval" in wakes[0][2] and _owed(store, teammate)

    await notifier.deliver(teammate, "idle")
    assert "finished" in wakes[1][2] and not _owed(store, teammate)


async def test_queued_message_is_not_reported_as_finished(store):
    lead = _session(store, project="A", title="Lead")
    teammate = _session(store, project="A", asker=lead)
    notifier, wakes = _notifier(store, answered=False)

    await notifier.deliver(teammate, "idle")

    assert wakes == [] and _owed(store, teammate)


async def test_failed_delivery_keeps_the_notice_owed(store):
    lead = _session(store, project="A", title="Lead")
    teammate = _session(store, project="A", asker=lead)
    notifier, wakes = _notifier(store, delivered=False)

    await notifier.deliver(teammate, "idle")

    assert len(wakes) == 1 and _owed(store, teammate)


@pytest.mark.parametrize("gone", ["moved", "archived", "deleted"])
async def test_no_notice_crosses_the_project_or_reaches_a_gone_asker(store, gone):
    lead = _session(store, project="A", title="Lead")
    teammate = _session(store, project="B" if gone == "moved" else "A", asker=lead)
    if gone == "archived":
        store.update_conversation(lead, archived=True)
    if gone == "deleted":
        store.set_labels(teammate, {TEAM_REPLY_TO_LABEL_KEY: uuid4().hex})
    notifier, wakes = _notifier(store)

    await notifier.deliver(teammate, "idle")

    assert wakes == [] and not _owed(store, teammate)


async def test_session_nobody_asked_stays_silent(store):
    teammate = _session(store, project="A")
    notifier, wakes = _notifier(store)

    await notifier.deliver(teammate, "idle")
    await notifier.deliver(uuid4().hex, "idle")

    assert wakes == []


async def test_running_edges_and_a_missing_notifier_do_nothing(store):
    lead = _session(store, project="A", title="Lead")
    teammate = _session(store, project="A", asker=lead)
    notifier, wakes = _notifier(store)

    notifier.status_changed(teammate, "running")
    notify_status(teammate, "idle")
    await asyncio.sleep(0)

    assert wakes == [] and _owed(store, teammate)


async def test_notice_marks_itself_as_routine(store):
    conversation = store.get_conversation(_session(store, title="Backend"))
    notice = team_update_notice(conversation, "failed")
    assert notice.startswith("[System:") and "Backend" in notice and "failed" in notice
