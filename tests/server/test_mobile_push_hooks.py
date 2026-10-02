import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from omnigent.runtime import pending_elicitations
from tests.server.test_mobile_push_config import credentials as credentials
from tests.server.test_mobile_push_store import push_store as push_store
from tests.server.test_mobile_push_store import register
from tests.server.test_mobile_push_store import session_id as session_id


@pytest.fixture(autouse=True)
def isolated_hooks(monkeypatch):
    from omnigent.server import session_live_state

    pending_elicitations.reset_for_tests()
    session_live_state.configure(None)
    monkeypatch.setattr(pending_elicitations, "_count_persist_hook", None)
    yield
    pending_elicitations.reset_for_tests()


@pytest.mark.parametrize(
    "before,after,expected",
    [
        ("running", "idle", "completed"),
        ("waiting", "idle", "completed"),
        ("running", "failed", "failed"),
        ("waiting", "failed", "failed"),
        (None, "idle", None),
        ("idle", "idle", None),
        ("failed", "idle", None),
    ],
)
def test_accepted_status_mapping_and_sticky_failures(monkeypatch, before, after, expected):
    from omnigent.server import mobile_push
    from omnigent.server.routes._sessions import helpers

    identifier = uuid4().hex
    service = Mock()
    monkeypatch.setattr(mobile_push, "_service", service)
    if before:
        helpers._session_status_cache[identifier] = before
    try:
        helpers._publish_status(identifier, after)
        if expected:
            service.status.assert_called_once_with(identifier, before, after, None)
        else:
            service.status.assert_not_called()
    finally:
        helpers._session_status_cache.pop(identifier, None)


def test_repair_idles_and_disabled_seam_are_noops(monkeypatch):
    from omnigent.server import mobile_push
    from omnigent.server.routes._sessions import helpers

    identifier = uuid4().hex
    service = Mock()
    monkeypatch.setattr(mobile_push, "_service", service)
    helpers._session_status_cache[identifier] = "running"
    helpers._publish_status(
        identifier, "idle", persist_live_status=False, scheduled_run_outcome="failed"
    )
    service.status.assert_not_called()
    helpers._session_status_cache.pop(identifier, None)
    monkeypatch.setattr(mobile_push, "_service", None)
    mobile_push.observe_status(identifier, "running", "idle", None)


async def test_composed_observer_keeps_subagent_notifier(monkeypatch, db_uri):
    from omnigent.server.routes._sessions.orchestration import configure_subagent_block_notifier
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    primary = Mock()
    push = Mock()
    pending_elicitations.set_elicitation_observer(primary)
    remove_push = pending_elicitations.add_elicitation_observer(push)
    notifier = Mock()
    monkeypatch.setattr(
        "omnigent.runtime.subagent_block_notifier.SubagentBlockNotifier", lambda **kwargs: notifier
    )
    remove_notifier = configure_subagent_block_notifier(store, None)
    event = {"type": "response.elicitation_request", "elicitation_id": "request"}
    pending_elicitations.record_publish("child", event)
    primary.assert_called_once_with("child", event)
    push.assert_called_once_with("child", event)
    notifier.observe.assert_called_once_with("child", event)
    remove_notifier()
    assert notifier.close.call_count == 1
    remove_push()
    pending_elicitations.reset_for_tests()


async def test_intents_commit_off_loop_with_workspace_context(push_store, session_id):
    from omnigent.db.db_models import current_workspace_id, workspace_scope
    from omnigent.server.mobile_push import MobilePushService

    calls = []
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    store = Mock()

    def enqueue(*args):
        calls.append((current_workspace_id(), args))
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        loop.call_soon_threadsafe(finished.set)

    store.enqueue.side_effect = enqueue
    service = MobilePushService(store, Mock(), Mock(), preview=False)
    with workspace_scope(34):
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(
                copy_context().run, service.status, session_id, "running", "idle", None
            ).result()
    await asyncio.wait_for(finished.wait(), 5)
    assert calls == [(34, (session_id, "completed", None))]


async def test_delivery_reads_preview_at_send_time_and_never_sends_on_call_path(
    push_store, session_id, credentials
):
    from omnigent.entities.conversation import MessageData
    from omnigent.server.feature_flags import resolve_feature_flags
    from omnigent.server.mobile_push import MobilePushService
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    register(push_store)
    push_store.enqueue(session_id, "completed", now=100)
    conversation_store = Mock()
    conversation_store.list_items.return_value.data = [
        Mock(
            data=MessageData(
                role="assistant",
                agent="test-agent",
                content=[{"type": "text", "text": "fresh preview"}],
            )
        )
    ]
    conversation_store.list_items.return_value.has_more = False
    requests = []

    def google(request):
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return httpx.Response(200, json={"name": "sent"})

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    assert requests == []
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        service = MobilePushService(
            push_store, conversation_store, FcmSender(config, client), preview=False
        )
        await service.deliver_once(now=110)
        payload = json.loads(requests[-1].content)
        assert "preview" not in payload["message"]["data"]
        conversation_store.list_items.assert_not_called()
        push_store.cancel(session_id)
        push_store.enqueue(session_id, "completed", now=120)
        service.preview = True
        await service.deliver_once(now=130)
        assert json.loads(requests[-1].content)["message"]["data"]["preview"] == "fresh preview"
        assert conversation_store.list_items.call_count == 1


async def test_elicitation_attention_survives_waiting_and_cancels_on_new_input(
    push_store, session_id, monkeypatch
):
    from concurrent.futures import Future
    from time import time

    from omnigent.db.db_models import SqlConversationMetadata
    from omnigent.db.utils import make_named_managed_session_maker
    from omnigent.server import mobile_push, session_live_state

    register(push_store, now=int(time()))
    session = make_named_managed_session_maker(
        push_store._engine, query_name_prefix="test.mobile_push"
    )
    with session("seed_pending_prompt") as transaction:
        metadata = transaction.get(SqlConversationMetadata, (0, session_id))
        assert metadata is not None
        metadata.pending_elicitation_count = 1
    service = mobile_push.MobilePushService(push_store, Mock(), Mock(), preview=False)
    monkeypatch.setattr(mobile_push, "_service", service)
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": "request",
        "params": {"message": "private prompt"},
    }
    remove = pending_elicitations.add_elicitation_observer(service.elicitation)
    try:
        pending_elicitations.record_publish(session_id, event)
        service.status(session_id, "running", "waiting", None)
        barrier = Future()
        session_live_state.submit("test_push_barrier", barrier.set_result, None)
        await asyncio.wrap_future(barrier)
        deliveries = push_store.claim()
        assert len(deliveries) == 1
        assert deliveries[0].kind == "needs_input"
        assert "private prompt" not in repr(deliveries)
        mobile_push.observe_input(session_id)
        barrier = Future()
        session_live_state.submit("test_push_barrier", barrier.set_result, None)
        await asyncio.wrap_future(barrier)
        assert push_store.prepare(deliveries[0]) is None
    finally:
        remove()


async def test_worker_claims_bounded_batch_concurrently(push_store, session_id):
    from unittest.mock import AsyncMock

    from omnigent.server.mobile_push import MobilePushService
    from omnigent.server.mobile_push_sender import SendResult

    for index in range(10):
        register(push_store, installation=f"phone-{index}", token=f"token-{index}")
    push_store.enqueue(session_id, "completed", now=100)
    sender = Mock()
    sender.authorization = AsyncMock(return_value="access")
    entered = asyncio.Event()
    resume = asyncio.Event()
    active = 0
    peak = 0

    async def post(payload, token):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 8:
            entered.set()
        await resume.wait()
        active -= 1
        return SendResult("sent")

    sender.post = AsyncMock(side_effect=post)
    service = MobilePushService(push_store, Mock(), sender, preview=False)
    task = asyncio.create_task(service.deliver_once(now=110))
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        resume.set()
    assert await task == 8
    assert peak == 8
    assert sender.post.call_count == 8
    assert len(push_store.claim(now=110)) == 2


@pytest.mark.parametrize("barrier", ["oauth", "preview", "refresh"])
@pytest.mark.parametrize("change", ["transfer", "revoke", "cancel", "delete"])
async def test_final_authority_after_oauth_preview_and_refresh(
    push_store, session_id, credentials, monkeypatch, barrier, change
):
    import threading

    from omnigent.db.db_models import SqlSessionPermission
    from omnigent.db.utils import make_named_managed_session_maker
    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.feature_flags import resolve_feature_flags
    from omnigent.server.mobile_push import MobilePushService
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    accounts = SqlAlchemyAccountStore(push_store.storage_location)
    accounts.create_user_with_password("owner", "hashed")
    register(push_store)
    push_store.enqueue(session_id, "completed", now=100)
    entered = asyncio.Event()
    resume = asyncio.Event()
    preview_entered = threading.Event()
    preview_resume = threading.Event()
    oauth_count = 0
    fcm = []

    async def google(request):
        nonlocal oauth_count
        if request.url.host == "oauth2.googleapis.com":
            oauth_count += 1
            if barrier == "oauth" or (barrier == "refresh" and oauth_count == 2):
                entered.set()
                await resume.wait()
            return httpx.Response(
                200, json={"access_token": f"access-{oauth_count}", "expires_in": 3600}
            )
        fcm.append(request)
        return httpx.Response(401 if barrier == "refresh" else 200)

    def preview(identifier):
        preview_entered.set()
        assert preview_resume.wait(5)
        return "fresh preview"

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        service = MobilePushService(
            push_store, Mock(), FcmSender(config, client), preview=barrier == "preview"
        )
        monkeypatch.setattr(service, "_preview", preview)
        task = asyncio.create_task(service.deliver_once(now=110))
        try:
            if barrier == "preview":
                assert await asyncio.to_thread(preview_entered.wait, 5)
            else:
                await asyncio.wait_for(entered.wait(), 5)
            if change == "transfer":
                register(push_store, user="reader", now=110)
            elif change == "cancel":
                push_store.cancel(session_id)
            elif change == "delete":
                accounts.delete_user("owner")
            else:
                session = make_named_managed_session_maker(
                    push_store._engine, query_name_prefix="test.mobile_push"
                )
                with session("revoke_during_send") as transaction:
                    grant = transaction.get(SqlSessionPermission, (0, "owner", session_id))
                    assert grant is not None
                    transaction.delete(grant)
        finally:
            resume.set()
            preview_resume.set()
            await task
    assert len(fcm) == (1 if barrier == "refresh" else 0)


async def test_delivery_loop_survives_unexpected_errors_and_skips_productive_sleep(
    monkeypatch, caplog
):
    from unittest.mock import AsyncMock

    from omnigent.server.mobile_push import MobilePushService

    service = MobilePushService(Mock(), Mock(), Mock(), preview=False)
    delivered = AsyncMock(
        side_effect=[LookupError("sensitive-payload"), 2, asyncio.CancelledError()]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(service, "deliver_once", delivered)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await service._run()
    assert delivered.call_count == 3
    sleep.assert_awaited_once_with(1)
    assert "sensitive-payload" not in caplog.text
    assert "LookupError" in caplog.text


async def test_unexpected_worker_death_is_logged(monkeypatch, caplog):
    from unittest.mock import AsyncMock

    from omnigent.server import mobile_push

    service = mobile_push.MobilePushService(Mock(), Mock(), Mock(), preview=False)
    service.sender.client.aclose = AsyncMock()
    monkeypatch.setattr(service, "_run", AsyncMock(side_effect=RuntimeError("sensitive-payload")))
    await service.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "unexpectedly" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "sensitive-payload" not in caplog.text
    assert service._task is not None
    with pytest.raises(RuntimeError):
        await service.stop()
    monkeypatch.setattr(mobile_push, "_service", None)


def test_deployment_rollback_and_rotation_contract():
    from pathlib import Path

    text = Path("docs/mobile-push.md").read_text()
    for required in (
        "automatically\nmigrates at startup",
        "deploy the schema release first, then the feature code",
        "flag-off roll-forward",
        "OMNIGENT_DB_URL=… alembic -c omnigent/db/alembic.ini downgrade mm1a2b3c4d5e",
        "old replica restarting",
        "re-register",
        "read once at startup",
        "restart",
        "up to 1 h",
        "DELETE on logout",
    ):
        assert required in text
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config("omnigent/db/alembic.ini")
    script = ScriptDirectory.from_config(config)
    revision = script.get_revision("mp1b2c3d4e5f")
    assert revision is not None
    assert revision.down_revision == "mm1a2b3c4d5e"
    assert script.get_revision(revision.down_revision) is not None


async def test_expiry_maintenance_runs_on_slow_cadence(push_store, monkeypatch):
    from omnigent.db.db_models import SqlMobilePushDevice
    from omnigent.server import mobile_push

    register(push_store, now=100)
    stamp = [10.0]
    monkeypatch.setattr(mobile_push, "monotonic", lambda: stamp[0])
    purges = Mock(wraps=push_store.purge_expired)
    monkeypatch.setattr(push_store, "purge_expired", purges)
    service = mobile_push.MobilePushService(push_store, Mock(), Mock(), preview=False)
    await service.deliver_once(now=100 + 30 * 86400)
    await service.deliver_once(now=100 + 30 * 86400 + 1)
    assert purges.call_count == 1
    with push_store._session("verify_loop_device_erasure") as transaction:
        assert transaction.get(SqlMobilePushDevice, (0, "phone")) is None
    stamp[0] += 60
    await service.deliver_once(now=100 + 30 * 86400 + 60)
    assert purges.call_count == 2


@pytest.mark.parametrize("count", [121, 1100])
async def test_expiry_maintenance_drains_bounded_batches_per_tick(push_store, monkeypatch, count):
    from sqlalchemy import func, select

    from omnigent.db.db_models import SqlMobilePushDevice
    from omnigent.server import mobile_push

    for index in range(count):
        register(push_store, installation=f"phone-{index}", token=f"token-{index}")
    clock = [10.0]
    monkeypatch.setattr(mobile_push, "monotonic", lambda: clock[0])
    purges = Mock(wraps=push_store.purge_expired)
    monkeypatch.setattr(push_store, "purge_expired", purges)
    service = mobile_push.MobilePushService(push_store, Mock(), Mock(), preview=False)
    expired_at = 100 + 30 * 86400
    assert await service.deliver_once(now=expired_at) == 0
    assert purges.call_count == (3 if count == 121 else 20)
    assert all(call.kwargs["limit"] == 50 for call in purges.call_args_list)
    with push_store._session("verify_bounded_cleanup_tick") as transaction:
        remaining = transaction.scalar(select(func.count()).select_from(SqlMobilePushDevice))
    assert remaining == max(0, count - 1000)
    await service.deliver_once(now=expired_at)
    assert purges.call_count == (3 if count == 121 else 20)
    clock[0] += 60
    await service.deliver_once(now=expired_at)
    with push_store._session("verify_following_cleanup_tick") as transaction:
        assert transaction.scalar(select(func.count()).select_from(SqlMobilePushDevice)) == 0


async def test_failed_delivery_does_not_cancel_or_resend_healthy_sibling(
    push_store, session_id, credentials, caplog
):
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from omnigent.db.db_models import SqlMobilePushOutbox
    from omnigent.server.feature_flags import resolve_feature_flags
    from omnigent.server.mobile_push import MobilePushService
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    register(push_store, installation="bad", token="bad-token")
    register(push_store, installation="good", token="good-token")
    push_store.enqueue(session_id, "completed", now=100)
    entered = asyncio.Event()
    failed = asyncio.Event()
    requests = []
    cancelled = []

    async def google(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        token = json.loads(request.content)["message"]["token"]
        requests.append(token)
        if token == "bad-token":
            await entered.wait()
            failed.set()
            raise RuntimeError("sensitive-payload")
        entered.set()
        try:
            await failed.wait()
            await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            cancelled.append(token)
            raise
        return httpx.Response(200)

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        service = MobilePushService(push_store, Mock(), FcmSender(config, client), preview=False)
        for stamp in (110, 130, 170, 250, 410, 730):
            await asyncio.wait_for(service.deliver_once(now=stamp), 5)
            with push_store._session("verify_sibling_ack_and_attempts") as transaction:
                rows = list(transaction.scalars(select(SqlMobilePushOutbox)))
                healthy = next(row for row in rows if row.installation_id == "good")
                assert healthy.delivered
                assert healthy.attempts == 1
        assert requests.count("good-token") == 1
        assert requests.count("bad-token") == 5
        assert cancelled == []
        assert "RuntimeError" in caplog.text
        assert "sensitive-payload" not in caplog.text
        blocked = asyncio.Event()

        async def shutdown(delivery, *, now):
            blocked.set()
            await asyncio.Event().wait()

        service._deliver = shutdown
        acknowledge = AsyncMock()
        service.store = Mock(acknowledge=acknowledge)
        task = asyncio.create_task(service._deliver_safely(Mock(), now=730))
        await asyncio.wait_for(blocked.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        acknowledge.assert_not_called()


async def test_due_workspace_backlog_cannot_starve_other_tenant(
    push_store, session_id, credentials
):
    from sqlalchemy import event

    from omnigent.db.db_models import (
        SqlConversation,
        SqlConversationMetadata,
        SqlSessionPermission,
        workspace_scope,
    )
    from omnigent.db.enum_codecs import encode_session_live_status
    from omnigent.server.feature_flags import resolve_feature_flags
    from omnigent.server.mobile_push import MobilePushService
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    for index in range(48):
        register(push_store, installation=f"phone-{index}", token=f"backlog-{index}")
    push_store.enqueue(session_id, "completed", now=100)
    with workspace_scope(22):
        with push_store._session("seed_other_due_workspace") as transaction:
            transaction.add(
                SqlConversation(
                    id=session_id,
                    root_conversation_id=session_id,
                    title="Other",
                    created_at=100,
                    updated_at=100,
                )
            )
            transaction.add(
                SqlConversationMetadata(
                    id=session_id,
                    live_status=encode_session_live_status("idle"),
                    pending_elicitation_count=0,
                )
            )
            transaction.add(
                SqlSessionPermission(user_id="owner", conversation_id=session_id, level=4)
            )
        register(push_store, token="other-token")
        push_store.enqueue(session_id, "completed", now=100)
    statements = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "mobile_push_outbox" in statement:
            statements.append((statement, parameters))

    event.listen(push_store._engine, "before_cursor_execute", observe)
    try:
        assert push_store.pending_workspaces(now=110, limit=2) == [0, 22]
    finally:
        event.remove(push_store._engine, "before_cursor_execute", observe)
    assert push_store.pending_workspaces(now=110, limit=1) == [0]
    with push_store._engine.connect() as connection:
        plan = [
            row[3]
            for statement, parameters in statements
            for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
        ]
    assert all("GROUP BY" not in statement for statement, parameters in statements)
    assert any("ix_mobile_push_outbox_tenants" in row for row in plan)
    assert any("ix_mobile_push_outbox_due" in row for row in plan)
    sent = []

    def google(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        sent.append(json.loads(request.content)["message"]["token"])
        return httpx.Response(200)

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        service = MobilePushService(push_store, Mock(), FcmSender(config, client), preview=False)
        assert await service.deliver_once(now=110) == 9
    assert sent.count("other-token") == 1
    assert len(sent) == 9


@pytest.mark.parametrize("code", ["timeout", "budget_exceeded"])
def test_dead_failure_codes_are_not_forwarded(code):
    from omnigent.server.mobile_push_content import failure_reason, message_payload

    reason = failure_reason(code)
    assert reason is None
    payload = message_payload(
        platform="ios",
        token="token",
        session_id="session",
        kind="failed",
        title="Title",
        reason=reason,
    )
    assert (
        payload["message"]["apns"]["payload"]["aps"]["alert"]["body"]
        == "Agent stopped with an error."
    )


@pytest.mark.parametrize("previous", ["running", "waiting"])
@pytest.mark.parametrize("path", ["transport_failure", "skill_failure", "user_stop"])
async def test_noncompletion_callers_do_not_enqueue(
    push_store, session_id, monkeypatch, previous, path
):
    from concurrent.futures import Future
    from time import time
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from omnigent.db.db_models import SqlMobilePushOutbox
    from omnigent.errors import OmnigentError
    from omnigent.server import mobile_push, session_live_state
    from omnigent.server.routes._sessions import helpers, orchestration
    from omnigent.server.schemas import SessionEventInput
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    register(push_store, now=int(time()))
    store = SqlAlchemyConversationStore(push_store.storage_location)
    conv = store.get_conversation(session_id)
    assert conv is not None
    service = mobile_push.MobilePushService(push_store, store, Mock(), preview=False)
    monkeypatch.setattr(mobile_push, "_service", service)
    helpers._session_status_cache[session_id] = previous
    try:
        if path == "user_stop":
            monkeypatch.setattr(
                orchestration,
                "_relay_runner_stream_once",
                AsyncMock(side_effect=orchestration._RelayTransportLost(intentional=True)),
            )
            await orchestration._relay_runner_stream(session_id, Mock(), store)
        else:

            def fail(request):
                raise httpx.ReadError("runner transport failed", request=request)

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(fail), base_url="http://runner"
            ) as runner:
                with pytest.raises(OmnigentError, match="Runner is unreachable"):
                    if path == "transport_failure":
                        await orchestration._forward_event_to_runner(
                            session_id,
                            conv,
                            SessionEventInput(
                                type="message",
                                data={
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "hello"}],
                                },
                            ),
                            store,
                            runner,
                        )
                    else:
                        monkeypatch.setattr(
                            helpers,
                            "_resolve_skill_meta_text_via_runner",
                            AsyncMock(return_value="skill instructions"),
                        )
                        skill_agent = Mock()
                        skill_agent.name = "agent"
                        skill_agent.bundle_location = None
                        await helpers._dispatch_skill_slash_command_to_runner(
                            session_id,
                            conv,
                            SessionEventInput(
                                type="slash_command",
                                data={"kind": "skill", "name": "demo", "arguments": ""},
                            ),
                            store,
                            runner,
                            agent=skill_agent,
                            has_mcp_servers=False,
                            created_by=None,
                        )
        barrier = Future()
        session_live_state.submit("test_push_barrier", barrier.set_result, None)
        await asyncio.wrap_future(barrier)
        assert helpers._session_status_cache[session_id] == "idle"
        with push_store._session("verify_noncompletion_has_no_push") as transaction:
            assert list(transaction.scalars(select(SqlMobilePushOutbox))) == []
    finally:
        helpers._session_status_cache.pop(session_id, None)


@pytest.mark.parametrize("previous", ["running", "waiting"])
async def test_cross_tenant_terminal_edge_sends_only_its_devices(
    push_store, session_id, credentials, monkeypatch, previous
):
    from concurrent.futures import Future

    from sqlalchemy import select

    from omnigent.db.db_models import (
        SqlConversation,
        SqlConversationMetadata,
        SqlMobilePushOutbox,
        SqlSessionPermission,
        workspace_scope,
    )
    from omnigent.db.enum_codecs import encode_session_live_status
    from omnigent.server import mobile_push, session_live_state
    from omnigent.server.feature_flags import resolve_feature_flags
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    for workspace in (11, 22):
        with workspace_scope(workspace):
            with push_store._session("seed_shared_tenant_ids") as transaction:
                transaction.add(
                    SqlConversation(
                        id=session_id,
                        root_conversation_id=session_id,
                        title="Shared",
                        created_at=100,
                        updated_at=100,
                    )
                )
                transaction.add(
                    SqlConversationMetadata(
                        id=session_id,
                        live_status=encode_session_live_status("idle"),
                        pending_elicitation_count=0,
                    )
                )
                transaction.add(
                    SqlSessionPermission(user_id="owner", conversation_id=session_id, level=4)
                )
            register(push_store, token=f"workspace-{workspace}")
            if workspace == 11:
                register(push_store, installation="second", token="workspace-11-second")
    requests = []

    def google(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        requests.append(json.loads(request.content)["message"]["token"])
        return httpx.Response(200)

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    monkeypatch.setattr("omnigent.server.mobile_push_store.time.time", lambda: 100)
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        service = mobile_push.MobilePushService(
            push_store, Mock(), FcmSender(config, client), preview=False
        )
        with workspace_scope(11):
            service.status(session_id, previous, "idle", None)
            barrier = Future()
            session_live_state.submit("test_push_barrier", barrier.set_result, None)
            await asyncio.wrap_future(barrier)
        await service.deliver_once(now=110)
    assert sorted(requests) == ["workspace-11", "workspace-11-second"]
    with workspace_scope(22), push_store._session("verify_other_tenant_empty") as transaction:
        assert (
            list(
                transaction.scalars(
                    select(SqlMobilePushOutbox).where(SqlMobilePushOutbox.workspace_id == 22)
                )
            )
            == []
        )
