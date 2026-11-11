"""An archived session's managed sandbox is stopped, resumably, when its provider opts in."""

from __future__ import annotations

import click
import pytest

from omnigent.server import managed_hosts
from omnigent.server.managed_hosts import (
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    launch_managed_host,
    parse_sandbox_config,
    resume_managed_host,
    stop_archived_session_sandbox,
)
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.conversation_store import SCHEDULED_TASK_ORIGIN_LABEL_KEY
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
    db_uri: str, *, can_resume: bool = True, labels=None, git_branch=None
):
    host_store = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)

    def register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id=_OWNER
        )

    fake = FakeSandboxLauncher(on_host_start=register, can_resume=can_resume)
    launched = await launch_managed_host(
        config=_deployment(fake), owner=_OWNER, host_store=host_store
    )
    conv = conversations.create_conversation(
        host_id=launched.host_id, workspace="/workspace", labels=labels, git_branch=git_branch
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


async def test_a_sibling_unarchived_while_the_launcher_decides_keeps_the_sandbox(
    db_uri, monkeypatch
):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    sibling = conversations.create_conversation(host_id=conv.host_id, workspace="/workspace")
    conversations.update_conversation(sibling.id, archived=True)

    def stop_when_archived(**_kwargs: object) -> bool:
        conversations.update_conversation(sibling.id, archived=False)
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
async def test_keeps_a_sandbox_another_unarchived_session_uses(db_uri, other_archived):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    other = conversations.create_conversation(host_id=conv.host_id, workspace="/workspace")
    if other_archived:
        conversations.update_conversation(other.id, archived=True)

    stopped = await _stop(fake, host_store, conversations, conv)
    assert stopped is other_archived
    assert len(fake.stopped) == (1 if other_archived else 0)


async def test_keeps_a_sandbox_its_provider_cannot_resume(db_uri):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(
        db_uri, can_resume=False
    )
    assert await _stop(fake, host_store, conversations, conv) is False
    assert fake.stopped == []


async def test_keeps_a_scheduled_task_runs_sandbox(db_uri):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(
        db_uri, labels={SCHEDULED_TASK_ORIGIN_LABEL_KEY: "task-1"}
    )
    assert await _stop(fake, host_store, conversations, conv) is False
    assert fake.stopped == []


@pytest.mark.parametrize("missing", ["host-listing", "archived-states"])
async def test_keeps_the_sandbox_when_the_store_cannot_list_the_hosts_sessions(
    db_uri, monkeypatch, missing
):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    # The base store's defaults for both reads: nothing, read as unknown.
    method = "list_host_conversation_ids" if missing == "host-listing" else "get_archived_states"
    monkeypatch.setattr(conversations, method, lambda *a, **k: {} if "states" in method else [])
    assert await _stop(fake, host_store, conversations, conv) is False
    assert fake.stopped == []


@pytest.mark.parametrize(("pages", "stops"), [(2, False), (3, True)], ids=["capped", "complete"])
async def test_keeps_the_sandbox_when_the_hosts_sessions_exceed_the_page_cap(
    db_uri, monkeypatch, pages, stops
):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)
    other = conversations.create_conversation(host_id=conv.host_id, workspace="/workspace")
    conversations.update_conversation(other.id, archived=True)
    # Two archived sessions at one per page: the listing only ends on a third,
    # short page. A cap that cuts it off first counts the host as in use.
    monkeypatch.setattr(managed_hosts, "_ARCHIVE_STOP_HOST_SESSION_PAGE_SIZE", 1)
    monkeypatch.setattr(managed_hosts, "_ARCHIVE_STOP_HOST_SESSION_PAGES", pages)
    assert await _stop(fake, host_store, conversations, conv) is stops
    assert len(fake.stopped) == (1 if stops else 0)


async def test_a_failed_stop_does_not_raise(db_uri, monkeypatch):
    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(db_uri)

    def fail(_sandbox_id: str) -> None:
        raise click.ClickException("provider stop failed")

    monkeypatch.setattr(fake, "stop", fail)
    assert await _stop(fake, host_store, conversations, conv) is False


async def test_the_archive_teardown_stops_the_sandbox_last(db_uri, monkeypatch):
    from omnigent.server.routes import sessions as sessions_facade

    fake, host_store, conversations, conv = await _archived_session_on_a_sandbox(
        db_uri, git_branch="topic"
    )
    calls: list[str] = []

    async def stop_runner(*_args: object, **_kwargs: object) -> None:
        calls.append("runner")

    async def remove_worktree(*_args: object, **_kwargs: object) -> None:
        calls.append("worktree")

    monkeypatch.setattr(sessions_facade, "_best_effort_stop", stop_runner)
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
    assert calls == ["runner", "worktree", "sandbox"]


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
