"""Real database boundaries for grants, requester propagation and copied replies."""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import MessageData
from omnigent.errors import OmnigentError
from omnigent.session_authority import RequestAuthority, orchestration_scope
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.session_link_store import (
    Endpoint,
    InputIdentity,
    SessionLinkStore,
    payload_digest,
    receipt_id,
)


@pytest.fixture(autouse=True)
def authority_scope():
    with orchestration_scope(True):
        yield


def _enable(store, user="alice"):
    return store.set_enabled(user, None, True, store.settings(user)["version"])


def _disable(store, user="alice"):
    return store.set_enabled(user, None, False, store.settings(user)["version"])


@pytest.fixture
def linked_sessions(db_uri: str, request, tmp_path: Path):
    history = f"sqlite:///{tmp_path / 'history.db'}" if getattr(request, "param", False) else None
    conversations = SqlAlchemyConversationStore(db_uri, history)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("alice")
    permissions.ensure_user("bob")
    links = SessionLinkStore(db_uri)
    endpoints = []
    for title in ("manager", "worker", "private"):
        conv = conversations.create_conversation(title=title)
        permissions.grant("alice", conv.id, 4)
        conversations.set_runner_id(conv.id, f"runner-{title}")
        endpoints.append(Endpoint(conv.id, conv.id, f"runner-{title}"))
    return conversations, permissions, links, endpoints


def test_shared_requester_is_preserved_at_every_hop(linked_sessions):
    _, permissions, links, (manager, worker, private) = linked_sessions
    _enable(links)
    _enable(links, "bob")
    for endpoint in (manager, worker):
        permissions.grant("bob", endpoint.session_id, 2)
    bob = links.resolve_inputs(
        manager, RequestAuthority((links.capture_input(manager, "bob", None),))
    )
    with pytest.raises(OmnigentError, match="must own both"):
        links.create_request(manager, worker, bob, "bob", payload_digest("work"))
    alice = links.resolve_inputs(
        manager, RequestAuthority((links.capture_input(manager, "alice", None),))
    )
    request, created = links.create_request(
        manager, worker, alice, "call-1", payload_digest("work")
    )
    assert created
    assert links.begin_handoff(request["id"])
    worker_input = links.accept_request(request["id"], manager, worker)
    assert worker_input
    links.confirm_handoff(request["id"], accepted=True)
    assert links.accept_request(request["id"], manager, worker) is None
    inherited = links.resolve_inputs(worker, RequestAuthority((worker_input,)))
    assert inherited.requester == "alice"
    assert inherited.parent_request_ids == (request["id"],)
    permissions.grant("alice", private.session_id, 2)
    with pytest.raises(OmnigentError, match="must own both"):
        links.create_request(worker, private, inherited, "call-2", payload_digest("more work"))
    permissions.grant("alice", private.session_id, 4)
    child, _ = links.create_request(
        worker, private, inherited, "call-2", payload_digest("more work")
    )
    assert child["requester"] == "alice"
    assert child["root_request_id"] == request["id"]
    assert child["depth"] == 2
    reverse, _ = links.create_request(
        worker, manager, inherited, "reverse", payload_digest("new work")
    )
    assert reverse["id"] != request["id"]
    permissions.revoke("alice", private.session_id)
    assert links.begin_handoff(child["id"])
    with pytest.raises(OmnigentError):
        links.accept_request(child["id"], worker, private)


def test_inputs_cannot_change_requester_or_runner(linked_sessions):
    _, _, links, (manager, worker, _) = linked_sessions
    input_id = links.capture_input(manager, "alice", None)
    assert links.capture_input(manager, "alice", None, input_id=input_id) == input_id
    for endpoint, authority in (
        (worker, RequestAuthority((input_id,))),
        (manager, RequestAuthority((uuid4().hex,))),
        (
            Endpoint(manager.session_id, manager.permission_id, "replacement"),
            RequestAuthority((input_id,)),
        ),
        (manager, RequestAuthority((), verified=False)),
    ):
        with pytest.raises(OmnigentError):
            links.resolve_inputs(endpoint, authority)


def test_reconnect_cannot_revive_pending_work(linked_sessions):
    _, _, links, (manager, worker, _) = linked_sessions
    original = _enable(links)
    identity = InputIdentity(links.capture_input(manager, "alice", None), "alice", None)
    request, _ = links.create_request(manager, worker, identity, "call-1", payload_digest("work"))
    repeated, created = links.create_request(
        manager, worker, identity, "call-1", payload_digest("work")
    )
    assert not created
    assert repeated["id"] == request["id"]
    with pytest.raises(OmnigentError, match="different work"):
        links.create_request(manager, worker, identity, "call-1", payload_digest("changed"))
    assert links.begin_handoff(request["id"])
    _disable(links)
    assert _enable(links)["version"] != original["version"]
    with pytest.raises(OmnigentError):
        links.accept_request(request["id"], manager, worker)


@pytest.mark.parametrize("split", [False, True])
def test_reply_publication_retries_and_disconnect(tmp_path: Path, split: bool):
    metadata = f"sqlite:///{tmp_path / 'metadata.db'}"
    history = f"sqlite:///{tmp_path / 'history.db'}" if split else None
    conversations = SqlAlchemyConversationStore(metadata, history)
    permissions = SqlAlchemyPermissionStore(metadata)
    permissions.ensure_user("alice")
    links = SessionLinkStore(metadata)
    endpoints = []
    for title in ("manager", "worker"):
        conv = conversations.create_conversation(title=title)
        permissions.grant("alice", conv.id, 4)
        conversations.set_runner_id(conv.id, f"runner-{title}")
        endpoints.append(Endpoint(conv.id, conv.id, f"runner-{title}"))
    manager, worker = endpoints
    _enable(links)
    identity = InputIdentity(links.capture_input(manager, "alice", None), "alice", None)
    request, _ = links.create_request(manager, worker, identity, "call-1", payload_digest("work"))
    assert links.begin_handoff(request["id"])
    worker_input = links.accept_request(request["id"], manager, worker)
    assert worker_input
    links.confirm_handoff(request["id"], accepted=True)
    reply_identity = links.resolve_inputs(worker, RequestAuthority((worker_input,)))
    reply_id = receipt_id(request["id"], "reply-1")
    write = conversations.prepare_append(
        manager.session_id,
        [
            NewConversationItem(
                type="message",
                stable_id=reply_id,
                response_id=request["id"],
                data=MessageData(
                    role="assistant",
                    agent="worker",
                    content=[{"type": "output_text", "text": "Done"}],
                ),
            )
        ],
    )

    def publish_and_lose_receipt(transaction: Session) -> None:
        conversations.append_under_authority(transaction, write)
        raise RuntimeError("metadata receipt unavailable")

    with pytest.raises(RuntimeError, match="receipt unavailable"):
        links.publish_reply(
            request["id"],
            manager,
            worker,
            reply_identity,
            "reply-1",
            payload_digest("Done"),
            publish_and_lose_receipt,
        )
    links.publish_reply(
        request["id"],
        manager,
        worker,
        reply_identity,
        "reply-1",
        payload_digest("Done"),
        lambda transaction: conversations.append_under_authority(transaction, write),
    )
    links.publish_reply(
        request["id"],
        manager,
        worker,
        reply_identity,
        "reply-1",
        payload_digest("Done"),
        lambda _: pytest.fail("duplicate reply was republished"),
    )
    assert len(conversations.list_items(manager.session_id).data) == 1
    _disable(links)
    with pytest.raises(OmnigentError):
        links.publish_reply(
            request["id"],
            manager,
            worker,
            reply_identity,
            "reply-2",
            payload_digest("Later"),
            lambda _: pytest.fail("reply published after disconnect"),
        )
    assert len(conversations.list_items(manager.session_id).data) == 1


def test_epoch_invalidation_does_not_revive_old_requests(linked_sessions):
    _, _, links, (manager, worker, _) = linked_sessions
    _enable(links)
    identity = InputIdentity(links.capture_input(manager, "alice", None), "alice", None)
    request, _ = links.create_request(manager, worker, identity, "old", payload_digest("work"))
    links.invalidate_endpoint(worker.session_id)
    with pytest.raises(OmnigentError, match="Authority changed"):
        links.check_request(request["id"], manager, worker, identity)
    fresh, _ = links.create_request(manager, worker, identity, "new", payload_digest("work"))
    assert fresh["authority_version"] != request["authority_version"]


def test_sibling_requests_and_chains_share_the_original_input_budget(linked_sessions, monkeypatch):
    from omnigent.stores import session_link_store

    _, _, links, (manager, worker, private) = linked_sessions
    monkeypatch.setattr(session_link_store, "MAX_REQUESTS_PER_ROOT", 3)
    _enable(links)
    input_id = links.capture_input(manager, "alice", None)
    identity = links.resolve_inputs(manager, RequestAuthority((input_id,)))
    first, _ = links.create_request(manager, worker, identity, "first", payload_digest("one"))
    links.begin_handoff(first["id"])
    worker_input = links.accept_request(first["id"], manager, worker)
    inherited = links.resolve_inputs(worker, RequestAuthority((worker_input,)))
    assert links.get_request(first["id"])["delivery_state"] == "accepted"
    links.create_request(worker, private, inherited, "child", payload_digest("two"))
    links.create_request(manager, private, identity, "sibling", payload_digest("three"))
    with pytest.raises(OmnigentError, match="delegation limit"):
        links.create_request(manager, worker, identity, "too-many", payload_digest("four"))


def test_reply_pagination_follows_publication_order_and_original_requester(linked_sessions):
    _, permissions, links, (manager, worker, _) = linked_sessions
    _enable(links)
    identity = InputIdentity(links.capture_input(manager, "alice", None), "alice", None)
    request, _ = links.create_request(manager, worker, identity, "request", payload_digest("work"))
    links.begin_handoff(request["id"])
    worker_input = links.accept_request(request["id"], manager, worker)
    inherited = links.resolve_inputs(worker, RequestAuthority((worker_input,)))
    reply_ids = []
    for operation in ("z", "a", "n"):
        result = links.publish_reply(
            request["id"],
            manager,
            worker,
            inherited,
            operation,
            payload_digest(operation),
            lambda _: None,
        )
        reply_ids.append(result["reply_id"])
    first, cursor = links.list_replies(request["id"], limit=2)
    assert [row["id"] for row in first] == reply_ids[:2]
    last, end = links.list_replies(request["id"], after=cursor, limit=2)
    assert [row["id"] for row in last] == reply_ids[2:]
    assert end is None
    for endpoint in (manager, worker):
        permissions.grant("bob", endpoint.session_id, 2)
    bob = InputIdentity(links.capture_input(manager, "bob", None), "bob", None)
    with pytest.raises(OmnigentError):
        links.check_request(request["id"], manager, worker, bob)


def test_expired_claims_are_uncertain_and_cannot_be_replayed(linked_sessions, monkeypatch):
    from omnigent.stores import session_link_store

    _, _, links, (manager, worker, _) = linked_sessions
    _enable(links)
    identity = InputIdentity(links.capture_input(manager, "alice", None), "alice", None)
    request, _ = links.create_request(manager, worker, identity, "claim", payload_digest("work"))
    links.begin_handoff(request["id"])
    links.accept_request(request["id"], manager, worker)
    assert links.affected_handoff_deadline(user_id="alice")
    assert links.get_request(request["id"])["accepted_at"] is None
    monkeypatch.setattr(session_link_store.time, "time", lambda: request["expires_at"] + 1)
    assert links.get_request(request["id"])["delivery_state"] == "uncertain"
    assert links.affected_handoff_deadline(user_id="alice") is None
    assert not links.begin_handoff(request["id"])
    assert links.accept_request(request["id"], manager, worker) is None


@pytest.mark.parametrize("feature_enabled", [False, True])
def test_owner_change_and_archive_do_not_reactivate_old_requests(linked_sessions, feature_enabled):
    conversations, permissions, links, (manager, worker, _) = linked_sessions
    _enable(links)
    identity = InputIdentity(links.capture_input(manager, "alice", None), "alice", None)
    for change in ("ownership", "archive"):
        request, _ = links.create_request(
            manager, worker, identity, change, payload_digest("work")
        )
        with orchestration_scope(feature_enabled):
            if change == "ownership":
                permissions.grant("alice", worker.session_id, 2)
                permissions.grant("alice", worker.session_id, 4)
            else:
                conversations.update_conversation(worker.session_id, archived=True)
                with pytest.raises(OmnigentError, match="archived"):
                    links.check_link(manager, worker, identity)
                conversations.update_conversation(worker.session_id, archived=False)
        with pytest.raises(OmnigentError, match="Authority changed"):
            links.check_request(request["id"], manager, worker, identity)


@pytest.mark.parametrize(
    "change",
    ["revoke", "downgrade", "archive", "rebind", "transfer", "delete-account", "password"],
)
def test_revocation_waits_for_claimed_delivery_without_holding_database_locks(
    linked_sessions, monkeypatch, db_uri, change
):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    from threading import Event

    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    conversations, permissions, links, (manager, worker, _) = linked_sessions
    accounts = SqlAlchemyAccountStore(db_uri)
    generation = accounts.update_password("alice", "test-password-hash", revoke_sessions=True)
    for endpoint in (manager, worker):
        permissions.grant("alice", endpoint.session_id, 4)
    links.set_enabled("alice", generation, True, links.settings("alice")["version"])
    identity = InputIdentity(
        links.capture_input(manager, "alice", generation), "alice", generation
    )
    request, _ = links.create_request(manager, worker, identity, "request", payload_digest("work"))
    links.begin_handoff(request["id"])
    links.accept_request(request["id"], manager, worker)

    waiting = Event()
    original = SessionLinkStore.affected_handoff_deadline

    def observe_wait(store, **scope):
        deadline = original(store, **scope)
        if deadline is not None:
            waiting.set()
        return deadline

    monkeypatch.setattr(SessionLinkStore, "affected_handoff_deadline", observe_wait)
    changes = {
        "revoke": lambda: permissions.revoke("alice", worker.session_id),
        "downgrade": lambda: permissions.grant("alice", worker.session_id, 1),
        "archive": lambda: conversations.update_conversation(worker.session_id, archived=True),
        "rebind": lambda: conversations.replace_runner_id(manager.session_id, "replacement"),
        "transfer": lambda: permissions.reassign_user_grants("alice", "new-owner"),
        "delete-account": lambda: accounts.delete_user("alice"),
        "password": lambda: accounts.update_password("alice", "new-hash", revoke_sessions=True),
    }
    with ThreadPoolExecutor(max_workers=1) as executor:
        changed = executor.submit(copy_context().run, changes[change])
        try:
            assert waiting.wait(3), "authority change did not drain the in-flight delivery"
            assert not changed.done()
            with pytest.raises(OmnigentError):
                links.create_request(manager, worker, identity, "later", payload_digest("later"))
        finally:
            links.confirm_handoff(request["id"], accepted=True)
        changed.result(timeout=3)
    assert links.get_request(request["id"])["delivery_state"] == "accepted"


def test_stale_endpoint_and_identity_cannot_survive_runner_rebinding(linked_sessions):
    conversations, _, links, (manager, worker, _) = linked_sessions
    _enable(links)
    identity = InputIdentity(links.capture_input(manager, "alice", None), "alice", None)
    conversations.replace_runner_id(manager.session_id, "replacement")
    for source in (
        manager,
        Endpoint(manager.session_id, manager.permission_id, "replacement"),
    ):
        with pytest.raises(OmnigentError, match="runner"):
            links.create_request(source, worker, identity, "late", payload_digest("late"))


def test_settings_are_per_user_versioned_and_cover_future_sessions(linked_sessions):
    conversations, permissions, links, (manager, _, _) = linked_sessions
    assert links.settings("alice")["enabled"] is False
    original = _enable(links)
    assert links.settings("bob")["enabled"] is False
    with pytest.raises(OmnigentError, match="setting changed"):
        links.set_enabled("alice", None, False, None)
    future = conversations.create_conversation(title="Future worker")
    permissions.grant("alice", future.id, 4)
    conversations.set_runner_id(future.id, "future-runner")
    target = Endpoint(future.id, future.id, "future-runner")
    identity = InputIdentity(links.capture_input(manager, "alice", None), "alice", None)
    request, _ = links.create_request(manager, target, identity, "future", payload_digest("work"))
    assert links.settings("alice") == original
    _disable(links)
    _enable(links)
    with pytest.raises(OmnigentError, match="Authority changed"):
        links.check_request(request["id"], manager, target, identity)
    assert links.list_activity(manager.session_id)[0][0]["target_id"] == future.id


@pytest.mark.parametrize("linked_sessions", [False, True], indirect=True, ids=["same", "split"])
@pytest.mark.parametrize("change", ["archive", "delete"])
@pytest.mark.parametrize("reply_first", [False, True], ids=["change-first", "reply-first"])
def test_reply_publication_serializes_with_session_removal(
    linked_sessions, monkeypatch, change, reply_first
):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from contextvars import copy_context
    from threading import Event

    conversations, _, links, (manager, worker, _) = linked_sessions
    _enable(links)
    input_id = links.capture_input(manager, "alice", None)
    identity = links.resolve_inputs(manager, RequestAuthority((input_id,)))
    request, _ = links.create_request(manager, worker, identity, "work", payload_digest("work"))
    assert links.begin_handoff(request["id"])
    worker_input = links.accept_request(request["id"], manager, worker)
    links.confirm_handoff(request["id"], accepted=True)
    responder = links.resolve_inputs(worker, RequestAuthority((worker_input,)))
    write = conversations.prepare_append(
        manager.session_id,
        [
            NewConversationItem(
                type="message",
                stable_id=receipt_id(request["id"], "reply"),
                response_id=request["id"],
                data=MessageData(
                    role="assistant",
                    agent="worker",
                    content=[{"type": "output_text", "text": "Done"}],
                ),
            )
        ],
    )
    publication_started, release_publication, change_started = Event(), Event(), Event()
    metadata_session = conversations._session_immediate

    class ObservedMetadataTransactions:
        engine = metadata_session.engine
        query_name_prefix = metadata_session.query_name_prefix

        @contextmanager
        def __call__(self, name):
            change_started.set()
            with metadata_session(name) as session:
                yield session

    monkeypatch.setattr(conversations, "_session_immediate", ObservedMetadataTransactions())

    def remove():
        if change == "archive":
            return conversations.update_conversation(worker.session_id, archived=True)
        return asyncio.run(conversations.delete_conversation(worker.session_id))

    def append(transaction):
        publication_started.set()
        assert release_publication.wait(5), "publication barrier was never released"
        conversations.append_under_authority(transaction, write)

    def publish(operation, callback):
        return links.publish_reply(
            request["id"], manager, worker, responder, operation, payload_digest("Done"), callback
        )

    if reply_first:
        with ThreadPoolExecutor(max_workers=2) as executor:
            publication = executor.submit(copy_context().run, publish, "reply", append)
            try:
                assert publication_started.wait(5)
                removal = executor.submit(copy_context().run, remove)
                assert change_started.wait(5)
                assert not removal.done(), "removal passed the publication's authority lock"
            finally:
                release_publication.set()
            assert publication.result(timeout=5)["published"]
            assert removal.result(timeout=5)
    else:
        assert remove()
    with pytest.raises(OmnigentError):
        publish("later", lambda _: pytest.fail("a reply was published after removal"))
    assert len(conversations.list_items(manager.session_id).data) == int(reply_first)


def test_link_authority_and_receipts_are_workspace_scoped(linked_sessions):
    from omnigent.db.db_models import workspace_scope

    conversations, permissions, links, (manager, worker, _) = linked_sessions
    original = _enable(links)
    input_id = links.capture_input(manager, "alice", None)
    identity = links.resolve_inputs(manager, RequestAuthority((input_id,)))
    request, _ = links.create_request(manager, worker, identity, "work", payload_digest("work"))
    links.begin_handoff(request["id"])
    worker_input = links.accept_request(request["id"], manager, worker)
    links.confirm_handoff(request["id"], accepted=True)
    responder = links.resolve_inputs(worker, RequestAuthority((worker_input,)))
    links.publish_reply(
        request["id"], manager, worker, responder, "reply", payload_digest("Done"), lambda _: None
    )
    with workspace_scope(42):
        permissions.ensure_user("alice")
        for endpoint in (manager, worker):
            conversations.create_conversation(conversation_id=endpoint.session_id)
            permissions.grant("alice", endpoint.session_id, 4)
            conversations.set_runner_id(endpoint.session_id, endpoint.runner_id)
        assert links.list_activity(manager.session_id) == ([], None)
        assert links.get_request(request["id"]) is None
        assert links.list_replies(request["id"]) == ([], None)
        assert links.accepted_inputs([request["id"]]) == set()
        with pytest.raises(OmnigentError):
            links.resolve_inputs(manager, RequestAuthority((input_id,)))
        with pytest.raises(OmnigentError):
            links.check_link(manager, worker, identity)
        local = _enable(links)
        assert local["version"] != original["version"]
        local_input = links.capture_input(manager, "alice", None)
        local_request, _ = links.create_request(
            manager,
            worker,
            links.resolve_inputs(manager, RequestAuthority((local_input,))),
            "work",
            payload_digest("work"),
        )
        assert local_request["id"] != request["id"]
        assert local_request["payload_key"] != request["payload_key"]
        _disable(links)
        links.invalidate_endpoint(worker.session_id)
    links.check_link(manager, worker, identity)
    assert links.settings("alice")["version"] == original["version"]
    assert links.accepted_inputs([request["id"]]) == {request["id"]}
    assert len(links.list_replies(request["id"])[0]) == 1
