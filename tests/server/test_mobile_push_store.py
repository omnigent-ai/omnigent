from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import select

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationMetadata,
    SqlSessionPermission,
    workspace_scope,
)
from omnigent.db.enum_codecs import encode_session_live_status
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.errors import OmnigentError


@pytest.fixture
def push_store(db_uri):
    from omnigent.server.mobile_push_store import MobilePushStore

    return MobilePushStore(db_uri)


@pytest.fixture
def session_id(db_uri):
    identifier = uuid4().hex
    session = make_named_managed_session_maker(
        get_or_create_engine(db_uri), query_name_prefix="test.mobile_push"
    )
    with session("seed_session") as transaction:
        transaction.add(
            SqlConversation(
                id=identifier,
                root_conversation_id=identifier,
                title="My session",
                created_at=100,
                updated_at=100,
            )
        )
        transaction.add(
            SqlConversationMetadata(
                id=identifier,
                live_status=encode_session_live_status("idle"),
                pending_elicitation_count=0,
            )
        )
        transaction.add(SqlSessionPermission(user_id="owner", conversation_id=identifier, level=4))
        transaction.add(
            SqlSessionPermission(user_id="reader", conversation_id=identifier, level=1)
        )
        transaction.add(
            SqlSessionPermission(user_id="__public__", conversation_id=identifier, level=1)
        )
    return identifier


def register(store, user="owner", installation="phone", token="device-token", now=100):
    return store.register(installation, user_id=user, platform="android", fcm_token=token, now=now)


def test_reregister_after_account_creation_refreshes_captured_generation(push_store, db_uri):
    from omnigent.db.account_authority import account_authority_scope
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    first = register(push_store)
    assert first.account_generation is None
    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("owner", "hashed")
    refreshed = register(push_store)
    assert refreshed.account_generation is not None
    assert refreshed.generation != first.generation
    with account_authority_scope("owner", uuid4().hex), pytest.raises(OmnigentError) as error:
        register(push_store)
    assert error.value.code == "unauthorized"


def test_stale_recipient_does_not_starve_other_recipients(push_store, session_id, db_uri):
    from omnigent.db.db_models import SqlMobilePushOutbox, SqlUser
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("owner", "hashed")
    register(push_store)
    register(push_store, user="reader", installation="reader-phone", token="reader-token")
    session = make_named_managed_session_maker(
        push_store._engine, query_name_prefix="test.mobile_push"
    )
    with session("change_recipient_authority") as transaction:
        account = transaction.get(SqlUser, (0, "owner"))
        assert account is not None
        account.account_generation = uuid4().hex
    push_store.enqueue(session_id, "completed", now=110)
    with session("read_remaining_intents") as transaction:
        assert list(transaction.scalars(select(SqlMobilePushOutbox.user_id))) == ["reader"]


def test_due_discovery_and_claim_are_bounded_and_index_backed(push_store, session_id):
    from sqlalchemy import event

    from omnigent.db.db_models import SqlMobilePushOutbox

    for index in range(7):
        register(push_store, installation=f"phone-{index}", token=f"token-{index}")
    push_store.enqueue(session_id, "completed", now=100)
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(push_store._engine, "before_cursor_execute", record)
    try:
        assert push_store.pending_workspaces(now=109, limit=3) == []
        assert push_store.pending_workspaces(now=110, limit=3) == [0]
        batch = push_store.claim(now=110, limit=3)
    finally:
        event.remove(push_store._engine, "before_cursor_execute", record)
    assert len(batch) == 3
    assert not any(
        "DISTINCT" in statement or "GROUP BY" in statement or statement.startswith("DELETE")
        for statement, parameters in statements
    )
    outbox_selects = [
        (statement, parameters)
        for statement, parameters in statements
        if statement.startswith("SELECT") and "FROM mobile_push_outbox" in statement
    ]
    assert outbox_selects and all("LIMIT" in statement for statement, parameters in outbox_selects)
    with push_store._engine.connect() as connection:
        plans = [
            row[3]
            for statement, parameters in outbox_selects
            for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
        ]
    assert any("ix_mobile_push_outbox_tenants" in plan for plan in plans)
    assert any("ix_mobile_push_outbox_due" in plan for plan in plans)
    assert "ix_mobile_push_outbox_discovery" not in {
        index.name for index in SqlMobilePushOutbox.__table__.indexes
    }


def seed_outbox_backlog(push_store, session_id, workspaces, count, *, due):
    from sqlalchemy import delete, insert

    from omnigent.db.db_models import SqlMobilePushOutbox

    with push_store._engine.begin() as connection:
        connection.execute(delete(SqlMobilePushOutbox))
        connection.execute(
            insert(SqlMobilePushOutbox),
            [
                {
                    "workspace_id": workspace,
                    "id": uuid4().hex,
                    "session_id": session_id,
                    "user_id": "owner",
                    "installation_id": f"phone-{index}",
                    "device_generation": "generation",
                    "kind": "completed",
                    "reason": None,
                    "not_before": 110 if due and index == 0 else 10000 + index,
                    "expires_at": 100000,
                    "lease": None,
                    "lease_until": 0 if index == 0 else 10000 + index,
                    "attempts": 0,
                    "delivered": False,
                }
                for workspace in workspaces
                for index in range(count)
            ],
        )


@pytest.mark.parametrize("limit", [2, 32])
def test_discovery_rotates_across_more_tenants_than_probe_limit(push_store, session_id, limit):
    from omnigent.db.db_models import SqlMobilePushOutbox

    seed_outbox_backlog(push_store, session_id, range(limit + 1), 4, due=True)
    with push_store._session("keep_all_tenant_backlogs_due") as transaction:
        for row in transaction.scalars(select(SqlMobilePushOutbox)):
            row.not_before = 110
            row.lease_until = 0
    served = []
    for _ in range(2):
        workspaces = push_store.pending_workspaces(now=110, limit=limit)
        assert len(workspaces) <= limit
        assert len(workspaces) == len(set(workspaces))
        for workspace in workspaces:
            with workspace_scope(workspace):
                assert len(push_store.claim(now=110, limit=1)) == 1
                served.append(workspace)
    assert limit in served
    with workspace_scope(limit), push_store._session("verify_highest_tenant_claim") as transaction:
        assert any(
            row.attempts == 1
            for row in transaction.scalars(
                select(SqlMobilePushOutbox).where(SqlMobilePushOutbox.workspace_id == limit)
            )
        )
    assert push_store.pending_workspaces(now=110, limit=0) == []


@pytest.mark.parametrize("due", [True, False])
def test_discovery_vm_work_is_independent_of_pending_backlog(push_store, session_id, due):
    from sqlalchemy import event

    from omnigent.server.mobile_push_store import MobilePushStore

    measurements = []
    for size in (16, 16384):
        seed_outbox_backlog(push_store, session_id, [0, 9, 99], size, due=due)
        store = MobilePushStore(push_store.storage_location)
        steps = [0]
        statements = []

        def progress(steps=steps):
            steps[0] += 1
            return 0

        def checkout(connection, record, proxy, progress=progress):
            connection.set_progress_handler(progress, 1)

        def checkin(connection, record):
            connection.set_progress_handler(None, 0)

        def observe(
            connection, cursor, statement, parameters, context, executemany, statements=statements
        ):
            if "mobile_push_outbox" in statement:
                statements.append(statement)

        event.listen(store._engine, "checkout", checkout)
        event.listen(store._engine, "checkin", checkin)
        event.listen(store._engine, "before_cursor_execute", observe)
        try:
            assert store.pending_workspaces(now=110, limit=3) == ([0, 9, 99] if due else [])
        finally:
            event.remove(store._engine, "checkout", checkout)
            event.remove(store._engine, "checkin", checkin)
            event.remove(store._engine, "before_cursor_execute", observe)
        measurements.append((size * 3, steps[0], len(statements)))
    print(f"DISCOVERY_VM due={due} rows_steps_statements={measurements}")
    assert measurements[1][1] <= measurements[0][1] + 100
    assert measurements[0][2] == measurements[1][2]
    assert measurements[1][2] <= 2 * 3 + 2


def test_every_retained_push_index_backs_an_executed_query(push_store, session_id, db_uri):
    from sqlalchemy import event, inspect

    from omnigent.db.db_models import SqlMobilePushDevice, SqlMobilePushOutbox
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    expected = {
        "ix_mobile_push_devices_user",
        "ix_mobile_push_devices_expiry",
        "ix_mobile_push_outbox_due",
        "ix_mobile_push_outbox_expiry",
        "ix_mobile_push_outbox_device",
        "ix_mobile_push_outbox_user",
        "ix_mobile_push_outbox_tenants",
    }
    assert {
        index.name
        for model in (SqlMobilePushDevice, SqlMobilePushOutbox)
        for index in model.__table__.indexes
    } == expected
    assert {
        index["name"]
        for table in ("mobile_push_devices", "mobile_push_outbox")
        for index in inspect(push_store._engine).get_indexes(table)
    } == expected
    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("owner", "hashed")
    register(push_store)
    register(push_store, user="reader", installation="reader-phone", token="reader-token")
    push_store.enqueue(session_id, "completed", now=100)
    plans = []

    def explain(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.startswith(("SELECT", "DELETE"))
            and "mobile_push_" in statement
            and not executemany
        ):
            for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters):
                plans.append((statement, row[3]))

    event.listen(push_store._engine, "before_cursor_execute", explain)
    try:
        assert push_store.pending_workspaces(now=110, limit=1) == [0]
        assert len(push_store.claim(now=110, limit=1)) == 1
        assert len(push_store.devices_for_user("owner", now=110)) == 1
        register(push_store, now=110)
        push_store.enqueue(session_id, "completed", now=110)
        assert push_store.delete_device("phone", "owner")
        push_store.purge_expired(now=120, limit=1)
        assert accounts.delete_user("owner")
        push_store.purge_expired(now=100 + 30 * 86400, limit=1)
    finally:
        event.remove(push_store._engine, "before_cursor_execute", explain)
    used = {
        name for name in expected for statement, plan in plans if name in plan and "SEARCH" in plan
    }
    print(f"INDEX_AUDIT retained={sorted(expected)} used={sorted(used)}")
    assert used == expected


def test_expired_unpurged_intent_cannot_block_new_enqueue(push_store, session_id):
    from omnigent.db.db_models import SqlMobilePushOutbox

    register(push_store)
    push_store.enqueue(session_id, "completed", now=100)
    first = push_store.claim(now=110)[0]
    push_store.enqueue(session_id, "completed", now=3700)
    second = push_store.claim(now=3710)[0]
    assert first.id != second.id
    with push_store._session("verify_replaced_expired_intent") as transaction:
        assert transaction.get(SqlMobilePushOutbox, (0, first.id)) is None
        assert transaction.get(SqlMobilePushOutbox, (0, second.id)) is not None


@pytest.mark.parametrize("exhausted", [False, True])
def test_claim_tolerates_row_purged_after_selection(
    push_store, session_id, monkeypatch, exhausted
):
    from sqlalchemy import delete

    from omnigent.db.db_models import SqlMobilePushOutbox
    from omnigent.server import mobile_push_store

    register(push_store)
    push_store.enqueue(session_id, "completed", now=100)
    if exhausted:
        with push_store._session("seed_exhausted_claim") as transaction:
            row = transaction.scalar(select(SqlMobilePushOutbox))
            row.attempts = 5
    lock = mobile_push_store.lock_account
    deleted = []

    def purge_before_update(transaction, user):
        lock(transaction, user)
        transaction.execute(
            delete(SqlMobilePushOutbox).execution_options(synchronize_session=False)
        )
        deleted.append(user)

    monkeypatch.setattr(mobile_push_store, "lock_account", purge_before_update)
    assert push_store.claim(now=110 if exhausted else 3700) == []
    assert deleted == ["owner"]


def test_user_outbox_delete_uses_workspace_user_index(push_store, session_id, db_uri):
    from sqlalchemy import inspect, text

    from omnigent.db.db_models import SqlMobilePushOutbox
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("owner", "hashed")
    register(push_store)
    register(push_store, user="reader", installation="reader-phone", token="reader-token")
    push_store.enqueue(session_id, "completed", now=100)
    columns = ("workspace_id", "user_id")
    assert any(
        tuple(column.name for column in index.columns) == columns
        for index in SqlMobilePushOutbox.__table__.indexes
    )
    with push_store._engine.connect() as connection:
        assert columns in {
            tuple(index["column_names"])
            for index in inspect(connection).get_indexes("mobile_push_outbox")
        }
        plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN DELETE FROM mobile_push_outbox "
                "WHERE workspace_id = 0 AND user_id = 'owner'"
            )
        ).all()
    assert any("ix_mobile_push_outbox_user" in row[3] for row in plan)
    assert accounts.delete_user("owner")
    with push_store._session("verify_indexed_user_delete_isolation") as transaction:
        assert [row.user_id for row in transaction.scalars(select(SqlMobilePushOutbox))] == [
            "reader"
        ]


def test_expired_devices_and_outbox_are_physically_purged_in_batches(push_store, session_id):
    from omnigent.db.db_models import SqlMobilePushDevice, SqlMobilePushOutbox

    for index in range(3):
        register(push_store, installation=f"expired-{index}", token=f"token-{index}", now=100)
    push_store.enqueue(session_id, "completed", now=100 + 30 * 86400 - 10)
    register(push_store, installation="live", token="live-token", now=100 + 30 * 86400)
    assert push_store.purge_expired(now=100 + 30 * 86400, limit=2) == 4
    session = make_named_managed_session_maker(
        push_store._engine, query_name_prefix="test.mobile_push"
    )
    with session("verify_bounded_purge") as transaction:
        assert len(list(transaction.scalars(select(SqlMobilePushDevice)))) == 2
        assert len(list(transaction.scalars(select(SqlMobilePushOutbox)))) == 1
    for _ in range(4):
        push_store.purge_expired(now=100 + 30 * 86400, limit=2)
    with session("verify_expiry_removal") as transaction:
        assert list(transaction.scalars(select(SqlMobilePushDevice.installation_id))) == ["live"]
        assert list(transaction.scalars(select(SqlMobilePushOutbox))) == []


def test_registration_rejects_guessed_ids_and_moves_only_exact_token(push_store):
    first = register(push_store)
    with pytest.raises(OmnigentError) as error:
        register(push_store, user="intruder", token="different-token")
    assert error.value.code == "conflict"
    assert not push_store.delete_device("phone", "intruder")
    assert push_store.devices_for_user("owner", now=101) == [first]
    moved = register(push_store, user="reader")
    assert moved.user_id == "reader"
    assert moved.generation != first.generation
    assert push_store.devices_for_user("owner", now=101) == []
    assert push_store.delete_device("phone", "reader")


def test_workspace_and_user_isolation_and_registration_expiry(push_store):
    register(push_store)
    register(push_store, user="reader", installation="reader-phone", token="reader-token")
    with workspace_scope(2):
        register(push_store, user="reader")
        assert len(push_store.devices_for_user("reader", now=101)) == 1
        assert not push_store.delete_device("phone", "owner")
    assert len(push_store.devices_for_user("owner", now=101)) == 1
    assert len(push_store.devices_for_user("reader", now=101)) == 1
    assert push_store.devices_for_user("owner", now=100 + 30 * 86400) == []


def test_outbox_settle_dedupe_and_cancel_on_activity(push_store, session_id):
    register(push_store)
    push_store.enqueue(session_id, "completed", now=100)
    push_store.enqueue(session_id, "completed", now=101)
    assert push_store.claim(now=109) == []
    claimed = push_store.claim(now=110)
    assert len(claimed) == 1
    assert claimed[0].session_id == session_id
    push_store.cancel(session_id)
    assert not push_store.prepare(claimed[0], now=111)
    assert not push_store.acknowledge(claimed[0], "sent", now=111)


def test_concurrent_claimers_lease_and_conditional_ack(push_store, session_id):
    register(push_store)
    push_store.enqueue(session_id, "failed", reason="Runner unavailable", now=100)
    with ThreadPoolExecutor(max_workers=2) as executor:
        batches = list(executor.map(lambda _: push_store.claim(now=110), range(2)))
    claimed = [item for batch in batches for item in batch]
    assert len(claimed) == 1
    assert push_store.claim(now=139) == []
    assert not push_store.acknowledge(claimed[0], "sent", now=140)
    reclaimed = push_store.claim(now=140)
    assert len(reclaimed) == 1
    assert reclaimed[0].lease != claimed[0].lease
    assert not push_store.acknowledge(claimed[0], "sent", now=140)
    assert push_store.acknowledge(reclaimed[0], "sent", now=140)
    assert push_store.claim(now=171) == []


def test_recipients_and_send_time_access_revocation(push_store, session_id):
    for user in ("owner", "reader", "__public__", "__all__"):
        register(push_store, user=user, installation=user, token=user)
    push_store.enqueue(session_id, "completed", now=100)
    claimed = push_store.claim(now=110)
    assert {item.user_id for item in claimed} == {"owner", "reader"}
    session = make_named_managed_session_maker(
        push_store._engine, query_name_prefix="test.mobile_push"
    )
    with session("revoke_reader") as transaction:
        permission = transaction.get(SqlSessionPermission, (0, "reader", session_id))
        assert permission is not None
        transaction.delete(permission)
    for item in claimed:
        assert bool(push_store.prepare(item, now=110)) == (item.user_id == "owner")


def test_generation_and_state_rechecked_before_delivery(push_store, session_id):
    register(push_store)
    push_store.enqueue(session_id, "completed", now=100)
    claimed = push_store.claim(now=110)[0]
    register(push_store, token="rotated-token", now=111)
    assert push_store.prepare(claimed, now=111) is None
    assert not push_store.acknowledge(claimed, "prune", now=111)
    assert len(push_store.devices_for_user("owner", now=112)) == 1
    push_store.enqueue(session_id, "completed", now=112)
    current = push_store.claim(now=122)[0]
    session = make_named_managed_session_maker(
        push_store._engine, query_name_prefix="test.mobile_push"
    )
    with session("resume") as transaction:
        metadata = transaction.get(SqlConversationMetadata, (0, session_id))
        assert metadata is not None
        metadata.live_status = encode_session_live_status("running")
    assert push_store.prepare(current, now=122) is None


def test_retry_after_bounded_retries_expiry_and_prune(push_store, session_id):
    register(push_store)
    push_store.enqueue(session_id, "completed", now=100)
    delivery = push_store.claim(now=110)[0]
    assert push_store.acknowledge(delivery, "retry", retry_after=17, now=110)
    assert push_store.claim(now=126) == []
    for now in (127, 187, 247, 367):
        delivery = push_store.claim(now=now)[0]
        assert push_store.acknowledge(delivery, "retry", now=now)
    assert push_store.claim(now=1000) == []
    assert len(push_store.devices_for_user("owner", now=1000)) == 1
    push_store.cancel(session_id)
    push_store.enqueue(session_id, "failed", now=1000)
    delivery = push_store.claim(now=1010)[0]
    assert push_store.acknowledge(delivery, "prune", now=1010)
    assert push_store.devices_for_user("owner", now=1011) == []
    register(push_store, now=2000)
    push_store.enqueue(session_id, "failed", now=2000)
    assert push_store.claim(now=5600) == []


def test_child_prompts_collapse_to_root_without_storing_preview(push_store, session_id):
    from omnigent.server.mobile_push_store import SqlMobilePushOutbox

    register(push_store)
    session = make_named_managed_session_maker(
        push_store._engine, query_name_prefix="test.mobile_push"
    )
    children = [uuid4().hex, uuid4().hex]
    with session("seed_children") as transaction:
        for child in children:
            transaction.add(
                SqlConversation(
                    id=child,
                    parent_conversation_id=session_id,
                    root_conversation_id=session_id,
                    title="Child",
                    created_at=100,
                    updated_at=100,
                )
            )
            transaction.add(
                SqlConversationMetadata(
                    id=child,
                    live_status=encode_session_live_status("waiting"),
                    pending_elicitation_count=1,
                )
            )
    for child in children:
        push_store.enqueue(child, "needs_input", now=100)
    deliveries = push_store.claim(now=100)
    assert len(deliveries) == 1
    assert deliveries[0].session_id == session_id
    prepared = push_store.prepare(deliveries[0], now=100)
    assert prepared is not None
    assert prepared.title == "My session"
    assert "preview" not in SqlMobilePushOutbox.__table__.columns
    with session("resolve_prompts") as transaction:
        for child in children:
            metadata = transaction.get(SqlConversationMetadata, (0, child))
            assert metadata is not None
            metadata.pending_elicitation_count = 0
    assert push_store.prepare(deliveries[0], now=101) is None


def test_delete_user_removes_devices_and_pending_deliveries(push_store, session_id, db_uri):
    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.mobile_push_store import SqlMobilePushDevice, SqlMobilePushOutbox

    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("owner", "hashed")
    register(push_store)
    push_store.enqueue(session_id, "completed", now=100)
    assert accounts.delete_user("owner") is True
    session = make_named_managed_session_maker(
        push_store._engine, query_name_prefix="test.mobile_push"
    )
    with session("verify_cleanup") as transaction:
        assert transaction.execute(select(SqlMobilePushDevice)).scalars().all() == []
        assert transaction.execute(select(SqlMobilePushOutbox)).scalars().all() == []


def test_registration_database_errors_do_not_expose_tokens(push_store):
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    def fail_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO mobile_push_devices"):
            raise IntegrityError(statement, parameters, RuntimeError("sensitive-device-token"))

    event.listen(push_store._engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(OmnigentError) as error:
            register(push_store, token="sensitive-device-token")
        assert "sensitive-device-token" not in str(error.value)
        assert "sensitive-device-token" not in repr(error.value)
    finally:
        event.remove(push_store._engine, "before_cursor_execute", fail_insert)


def test_expiring_intents_locks_the_recipient_account(push_store, session_id, monkeypatch):
    from omnigent.server import mobile_push_store

    register(push_store)
    push_store.enqueue(session_id, "completed", now=100)
    locked_users = []
    require = mobile_push_store.lock_account

    def observe_lock(session, user_id, **kwargs):
        locked_users.append(user_id)
        return require(session, user_id, **kwargs)

    monkeypatch.setattr(mobile_push_store, "lock_account", observe_lock)
    assert push_store.purge_expired(now=3700, limit=2) == 1
    assert locked_users == ["owner"]
