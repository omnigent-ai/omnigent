"""An archived session's managed sandbox is stopped, resumably, when its provider opts in."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import click
import pytest

from omnigent.db.db_models import current_workspace_id
from omnigent.entities import MessageData, NewConversationItem
from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.server.managed_hosts import (
    ManagedLaunchTracker,
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    launch_managed_host,
    parse_sandbox_config,
    resume_managed_host,
    stop_archived_session_sandbox,
)
from omnigent.server.routes._sessions import orchestration
from omnigent.server.scheduled.schedule import continued_sandbox_host_id
from omnigent.stores.conversation_store import (
    CREATED_BY_SESSION_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import FakeSandboxLauncher, HostStartInvocation

pytestmark = pytest.mark.asyncio

_OWNER = "alice@example.com"


def _deployment(fake: FakeSandboxLauncher, *, stop_on_archive: bool = True):
    return ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://srv.example.com",
            launcher_factory=lambda: fake,
            token_ttl_s=3600,
            stop_on_archive=stop_on_archive,
        )
    )


async def _archived_session_on_a_sandbox(
    db_uri: str,
    *,
    can_resume: bool = True,
    labels=None,
    git_branch=None,
    runner_id=None,
    host_id=None,
):
    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)

    def register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id=_OWNER
        )

    fake = FakeSandboxLauncher(on_host_start=register, can_resume=can_resume)
    launched = await launch_managed_host(
        config=_deployment(fake), owner=_OWNER, host_store=host_store, host_id=host_id
    )
    conv = conversations.create_conversation(
        host_id=launched.host_id,
        workspace="/workspace",
        labels=labels,
        git_branch=git_branch,
        runner_id=runner_id,
    )
    conversations.update_conversation(conv.id, archived=True)
    return fake, host_store, conversations, conversations.get_conversation(conv.id)


async def _stop(fake, host_store, conversations, conv, **deployment) -> bool:
    return await stop_archived_session_sandbox(
        conv, host_store, conversations, _deployment(fake, **deployment)
    )


async def test_stops_the_sandbox_of_its_only_session_and_the_next_message_resumes_it(db_uri):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    sandbox_id = host_store.get_host(conv.host_id).sandbox_id

    assert await _stop(fake, host_store, conversations, conv) is True
    assert fake.stopped == [sandbox_id] and fake.terminated == []
    # The host row stays, so an unarchived session's next message wakes it.
    host_store.set_offline(conv.host_id)
    await resume_managed_host(conv.host_id, host_store, _deployment(fake))
    assert fake.resumed == [sandbox_id]


async def test_leaves_the_sandbox_running_when_not_enabled(db_uri):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    assert await _stop(fake, host_store, conversations, conv, stop_on_archive=False) is False
    assert fake.stopped == []


@pytest.mark.parametrize(
    "configured", [False, True], ids=["provider-enables", "provider-declines"]
)
async def test_the_providers_launcher_makes_the_final_call(db_uri, monkeypatch, configured):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    asked: list[tuple[str, bool]] = []

    def stop_when_archived(*, owner: str, configured: bool) -> bool:
        asked.append((owner, configured))
        return not configured

    monkeypatch.setattr(fake, "stop_when_archived", stop_when_archived)
    stopped = await _stop(fake, host_store, conversations, conv, stop_on_archive=configured)
    assert asked == [(_OWNER, configured)]
    assert stopped is (not configured)
    assert len(fake.stopped) == (0 if configured else 1)


@pytest.mark.parametrize("configured", [True, False], ids=["on", "off"])
async def test_a_launcher_without_the_hook_follows_the_setting(db_uri, monkeypatch, configured):
    # Embedding deployments may inject duck-typed launchers that predate the hook.
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    monkeypatch.setattr(type(fake), "stop_when_archived", property(_raise_attribute_error))
    assert not hasattr(fake, "stop_when_archived")
    stopped = await _stop(fake, host_store, conversations, conv, stop_on_archive=configured)
    assert stopped is configured
    assert len(fake.stopped) == (1 if configured else 0)


def _raise_attribute_error(_self: object) -> None:
    raise AttributeError("stop_when_archived")


async def test_an_unarchive_while_the_launcher_decides_keeps_the_sandbox(db_uri, monkeypatch):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)

    def stop_when_archived(**_kwargs: object) -> bool:
        # The user unarchives (and may already be sending a message) while a
        # slow decision, such as a flag lookup, is in flight.
        conversations.update_conversation(conv.id, archived=False)
        return True

    monkeypatch.setattr(fake, "stop_when_archived", stop_when_archived)
    assert await _stop(fake, host_store, conversations, conv) is False
    assert fake.stopped == []


async def test_a_failing_launcher_decision_leaves_the_sandbox_running(db_uri, monkeypatch):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)

    def stop_when_archived(**_kwargs: object) -> bool:
        raise RuntimeError("flag service unavailable")

    monkeypatch.setattr(fake, "stop_when_archived", stop_when_archived)
    assert await _stop(fake, host_store, conversations, conv) is False
    assert fake.stopped == []


@pytest.mark.parametrize("other_archived", [False, True], ids=["in-use", "also-archived"])
async def test_keeps_a_sandbox_another_session_is_bound_to(db_uri, other_archived):
    # An archived sibling may still be in its Undo grace or finishing its last
    # turn, which only its own teardown knows, so any sibling keeps the sandbox.
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    other = conversations.create_conversation(host_id=conv.host_id, workspace="/workspace")
    if other_archived:
        conversations.update_conversation(other.id, archived=True)

    assert await _stop(fake, host_store, conversations, conv) is False
    assert fake.stopped == []


@pytest.mark.parametrize("archived", [False, True], ids=["in-use", "also-archived"])
@pytest.mark.parametrize("kind", ["side-chat", "sub-agent"])
async def test_keeps_a_sandbox_a_side_chat_or_sub_agent_runs_on(db_uri, archived, kind):
    # Side chats and sub-agents run on the session's runner without a host binding.
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    if kind == "side-chat":
        other = conversations.create_conversation(side_chat_parent_id=conv.id)
    else:
        other = conversations.create_conversation(
            kind="sub_agent", title="helper", parent_conversation_id=conv.id
        )
    assert other.host_id is None
    if archived:
        conversations.update_conversation(other.id, archived=True)

    assert await _stop(fake, host_store, conversations, conv) is False
    assert fake.stopped == []


async def test_a_session_without_its_own_host_binding_is_left_alone(db_uri):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    side_chat = conversations.create_conversation(side_chat_parent_id=conv.id)
    conversations.update_conversation(side_chat.id, archived=True)
    side_chat = conversations.get_conversation(side_chat.id)
    assert await _stop(fake, host_store, conversations, side_chat) is False
    assert fake.stopped == []


async def test_keeps_a_sandbox_its_provider_cannot_resume(db_uri):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(
        db_uri, can_resume=False
    )
    assert await _stop(fake, host_store, conversations, conv) is False
    assert fake.stopped == []


@pytest.mark.parametrize("placement", ["scheduler", "independent-worker", "retained-host-worker"])
async def test_scheduled_sandbox_retention_does_not_cover_independent_workers(db_uri, placement):
    task_id = "0123456789abcdef0123456789abcdef"
    retained_host = continued_sandbox_host_id(task_id, current_workspace_id())
    labels = {SCHEDULED_TASK_ORIGIN_LABEL_KEY: task_id}
    creator = None
    if placement != "scheduler":
        creator = SqlAlchemyConversationStore(db_uri).create_conversation(labels=labels)
        labels = {CREATED_BY_SESSION_LABEL_KEY: creator.id}
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(
        db_uri,
        labels=labels,
        host_id=retained_host if placement == "retained-host-worker" else None,
    )
    assert conv.labels[SCHEDULED_TASK_ORIGIN_LABEL_KEY] == task_id
    if creator is not None:
        # The worker's provenance and the host's retention outlive the creator.
        assert await conversations.delete_conversation(creator.id)
    stopped = placement == "independent-worker"
    assert await _stop(fake, host_store, conversations, conv) is stopped
    assert fake.stopped == ([host_store.get_host(conv.host_id).sandbox_id] if stopped else [])


@pytest.mark.parametrize(
    "missing", ["host-listing", "archived-state", "activity-row", "activity-read"]
)
async def test_keeps_the_sandbox_when_the_store_cannot_list_the_hosts_sessions(
    db_uri, monkeypatch, missing
):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    # The host listing's base default (nothing), or a store that reports
    # activity without the archived flag: both read as unknown.
    real_activity = conversations.get_session_activity

    def activity_without_this_session(ids):
        return {i: a for i, a in real_activity(ids).items() if i != conv.id}

    def activity_read_fails(_ids):
        raise RuntimeError("database unavailable")

    if missing == "host-listing":
        monkeypatch.setattr(conversations, "list_host_conversation_ids", lambda *a, **k: [])
    elif missing == "activity-row":
        monkeypatch.setattr(conversations, "get_session_activity", activity_without_this_session)
    elif missing == "activity-read":
        monkeypatch.setattr(conversations, "get_session_activity", activity_read_fails)
    else:
        real = conversations.get_session_activity

        def without_archived(ids):
            return {i: replace(a, archived=None) for i, a in real(ids).items()}

        monkeypatch.setattr(conversations, "get_session_activity", without_archived)
    assert await _stop(fake, host_store, conversations, conv) is False
    assert fake.stopped == []


@pytest.mark.parametrize(
    "error",
    [click.ClickException("provider stop failed"), SandboxCapabilityError("cannot stop")],
    ids=["failed", "unsupported"],
)
async def test_a_failed_stop_does_not_raise(db_uri, monkeypatch, error):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)

    def fail(_sandbox_id: str) -> None:
        raise error

    monkeypatch.setattr(fake, "stop", fail)
    assert await _stop(fake, host_store, conversations, conv) is False


async def test_the_archive_teardown_stops_the_sandbox_last(db_uri, monkeypatch):
    from omnigent.server.routes import sessions as sessions_facade

    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(
        db_uri, git_branch="topic", runner_id="runner_" + "a" * 32
    )
    calls: list[str] = []

    async def stop_host_runner(*_args: object, **_kwargs: object) -> None:
        calls.append("host-runner")

    async def stop_runner(*_args: object, **_kwargs: object) -> None:
        calls.append("runner")

    async def remove_worktree(*_args: object, **_kwargs: object) -> None:
        calls.append("worktree")

    monkeypatch.setattr(sessions_facade, "_best_effort_stop", stop_runner)
    monkeypatch.setattr(orchestration, "_stop_host_runner_intentionally", stop_host_runner)
    monkeypatch.setattr(sessions_facade, "_remove_session_worktree_best_effort", remove_worktree)
    monkeypatch.setattr(fake, "stop", lambda sandbox_id: calls.append("sandbox"))
    await orchestration._archive_stop(
        conv.id,
        conversations,
        None,
        None,
        delete_worktree=True,
        host_store=host_store,
        sandbox_config=_deployment(fake),
    )
    assert calls == ["runner", "host-runner", "worktree", "sandbox"]


async def _launch_for(conversations, host_store, fake, session_id, **launch):
    tracker = ManagedLaunchTracker()
    tracker.begin(session_id)
    await orchestration._run_managed_launch(
        session_id=session_id,
        owner=_OWNER,
        sandbox_config=_deployment(fake),
        repos=[],
        tracker=tracker,
        conversation_store=conversations,
        host_store=host_store,
        host_registry=None,
        tunnel_registry=None,
        **launch,
    )
    assert tracker.get(session_id) is None, "the launch itself must still succeed"


def _registering_launcher(host_store: HostStore) -> FakeSandboxLauncher:
    def register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id=_OWNER
        )

    return FakeSandboxLauncher(on_host_start=register, can_resume=True)


@pytest.fixture
def no_undo_grace(monkeypatch):
    from omnigent.server.routes import sessions as sessions_facade

    monkeypatch.setattr(sessions_facade, "_ARCHIVE_STOP_UNDO_GRACE_S", 0.0)
    return sessions_facade


async def test_a_session_archived_while_provisioning_stops_once_its_host_binds(
    db_uri, no_undo_grace
):
    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    fake = _registering_launcher(host_store)
    # The archive's own teardown already ran and found no host on the row.
    conv = conversations.create_conversation()
    conversations.update_conversation(conv.id, archived=True)

    await _launch_for(conversations, host_store, fake, conv.id)

    bound = conversations.get_conversation(conv.id)
    assert bound.host_id is not None
    await orchestration._pending_archive_stops[conv.id]
    assert fake.stopped == [host_store.get_host(bound.host_id).sandbox_id]
    assert fake.terminated == []


async def test_a_session_left_active_keeps_its_new_sandbox(db_uri, no_undo_grace):
    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    fake = _registering_launcher(host_store)
    conv = conversations.create_conversation()

    await _launch_for(conversations, host_store, fake, conv.id)

    assert conv.id not in orchestration._pending_archive_stops
    assert fake.stopped == []


async def test_an_undo_during_the_new_grace_keeps_the_sandbox(db_uri, monkeypatch):
    from omnigent.server.routes import sessions as sessions_facade

    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    fake = _registering_launcher(host_store)
    conv = conversations.create_conversation()
    conversations.update_conversation(conv.id, archived=True)
    # Hold the teardown inside its grace until the Undo is persisted.
    grace = 12.5
    monkeypatch.setattr(sessions_facade, "_ARCHIVE_STOP_UNDO_GRACE_S", grace)
    in_grace, release = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep

    async def gated_sleep(delay, *args, **kwargs):
        if delay == grace:
            in_grace.set()
            await release.wait()
            return None
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", gated_sleep)

    await _launch_for(conversations, host_store, fake, conv.id)
    await asyncio.wait_for(in_grace.wait(), timeout=5)
    # Undo on another replica: only the persisted flag changes.
    conversations.update_conversation(conv.id, archived=False)
    release.set()
    await orchestration._pending_archive_stops[conv.id]

    assert fake.stopped == []


async def test_a_relaunch_of_an_archived_session_does_not_schedule_a_teardown(
    db_uri, no_undo_grace
):
    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    fake = _registering_launcher(host_store)
    conv = conversations.create_conversation()
    await _launch_for(conversations, host_store, fake, conv.id)
    host = host_store.get_host(conversations.get_conversation(conv.id).host_id)
    conversations.update_conversation(conv.id, archived=True)

    # A message to the archived session kicks this relaunch and waits on it.
    await _launch_for(conversations, host_store, fake, conv.id, relaunch_host=host)

    assert conv.id not in orchestration._pending_archive_stops
    assert fake.stopped == []


@pytest.mark.parametrize(
    "status_at_bind", ["running", None], ids=["running", "unknown-after-a-turn"]
)
async def test_a_turn_that_may_be_running_finishes_before_the_teardown(
    db_uri, no_undo_grace, monkeypatch, status_at_bind
):
    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    fake = _registering_launcher(host_store)
    conv = conversations.create_conversation()
    # The first turn archived its own session. Its status may not have reached this
    # replica or the store, but its message has.
    conversations.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_first_turn",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            )
        ],
    )
    conversations.update_conversation(conv.id, archived=True)
    if status_at_bind is not None:
        conversations.set_session_live_status(conv.id, status_at_bind)
    monkeypatch.setattr(orchestration, "_SELF_ARCHIVE_TURN_POLL_S", 0.01)
    reads = 0
    polled = asyncio.Event()
    loop = asyncio.get_running_loop()
    read_activity = conversations.get_session_activity

    def counting_read(ids):
        nonlocal reads
        reads += 1
        # The launch's own check is the first read; the rest are the teardown's turn wait.
        if reads >= 3:
            loop.call_soon_threadsafe(polled.set)
        return read_activity(ids)

    monkeypatch.setattr(conversations, "get_session_activity", counting_read)

    await _launch_for(conversations, host_store, fake, conv.id)
    teardown = orchestration._pending_archive_stops[conv.id]
    await asyncio.wait_for(polled.wait(), timeout=5)
    assert not teardown.done() and fake.stopped == []

    conversations.set_session_live_status(conv.id, "idle")
    await asyncio.wait_for(teardown, timeout=5)
    assert len(fake.stopped) == 1


async def test_a_failed_archive_check_keeps_the_launch_and_schedules_nothing(
    db_uri, no_undo_grace, monkeypatch
):
    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    fake = _registering_launcher(host_store)
    conv = conversations.create_conversation()
    conversations.update_conversation(conv.id, archived=True)

    def unavailable(_ids):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(conversations, "get_session_activity", unavailable)

    await _launch_for(conversations, host_store, fake, conv.id)

    assert conversations.get_conversation(conv.id).host_id is not None
    assert conv.id not in orchestration._pending_archive_stops
    assert fake.stopped == []


async def test_a_failed_item_read_makes_the_teardown_wait_for_the_turn(
    db_uri, no_undo_grace, monkeypatch
):
    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    fake = _registering_launcher(host_store)
    conv = conversations.create_conversation()
    conversations.update_conversation(conv.id, archived=True)
    scheduled: list[bool] = []

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    def capture(*_args, after_turn=False, **_kwargs):
        scheduled.append(after_turn)

    monkeypatch.setattr(conversations, "list_items", unavailable)
    monkeypatch.setattr(orchestration, "_spawn_archive_stop", capture)

    await _launch_for(conversations, host_store, fake, conv.id)

    # With no status and no way to see whether a turn ran, the teardown waits for one.
    assert scheduled == [True]


async def test_moving_an_archived_session_to_a_sandbox_does_not_schedule_a_teardown(
    db_uri, no_undo_grace
):
    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    fake = _registering_launcher(host_store)
    held_runner_id = "runner_" + "b" * 32
    # The move holds this claim on the session's runner while the sandbox provisions.
    conv = conversations.create_conversation(runner_id=held_runner_id)
    conversations.update_conversation(conv.id, archived=True)

    await _launch_for(conversations, host_store, fake, conv.id, held_runner_id=held_runner_id)

    assert conversations.get_conversation(conv.id).host_id is not None
    assert conv.id not in orchestration._pending_archive_stops
    assert fake.stopped == []


class _SecondProviderLauncher(FakeSandboxLauncher):
    provider = "e2b"


@pytest.mark.parametrize("second_stops", [True, False], ids=["second-on", "second-off"])
async def test_uses_the_setting_of_the_provider_that_launched_the_host(db_uri, second_stops):
    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)

    def register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id=_OWNER
        )

    first = FakeSandboxLauncher(on_host_start=register, can_resume=True)
    second = _SecondProviderLauncher(on_host_start=register, can_resume=True)
    deployment = ManagedSandboxDeployment(
        configs=tuple(
            ManagedSandboxConfig(
                server_url="https://srv.example.com",
                launcher_factory=lambda fake=fake: fake,
                token_ttl_s=3600,
                provider=fake.provider,
                stop_on_archive=stops,
            )
            for fake, stops in ((first, not second_stops), (second, second_stops))
        )
    )
    launched = await launch_managed_host(
        config=deployment, owner=_OWNER, host_store=host_store, provider="e2b"
    )
    conv = conversations.create_conversation(host_id=launched.host_id, workspace="/workspace")
    conversations.update_conversation(conv.id, archived=True)

    stopped = await stop_archived_session_sandbox(
        conversations.get_conversation(conv.id), host_store, conversations, deployment
    )
    assert stopped is second_stops
    assert first.stopped == []
    assert len(second.stopped) == (1 if second_stops else 0)


def _stop_settings(raw: dict) -> dict[str | None, bool]:
    return {entry.provider: entry.stop_on_archive for entry in parse_sandbox_config(raw).configs}


def test_parses_stop_on_archive_per_provider():
    base = {"provider": "modal", "server_url": "https://srv.example.com"}
    assert _stop_settings(base) == {"modal": False}
    assert _stop_settings({**base, "stop_on_archive": True}) == {"modal": True}
    with pytest.raises(ValueError, match="must be a boolean"):
        parse_sandbox_config({**base, "stop_on_archive": "yes"})
    # A top-level value is each entry's default; an entry can override it.
    multi = {
        "server_url": "https://srv.example.com",
        "stop_on_archive": True,
        "providers": [{"provider": "modal"}, {"provider": "e2b", "stop_on_archive": False}],
    }
    assert _stop_settings(multi) == {"modal": True, "e2b": False}
