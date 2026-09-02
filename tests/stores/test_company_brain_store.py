from __future__ import annotations

import uuid

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.stores.company_brain_store import sqlalchemy_store as store_module
from omnigent.stores.company_brain_store.sqlalchemy_store import SqlAlchemyCompanyBrainStore


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyCompanyBrainStore:
    return SqlAlchemyCompanyBrainStore(db_uri)


def test_connections_and_selections_are_workspace_scoped(
    store: SqlAlchemyCompanyBrainStore,
) -> None:
    with workspace_scope(101):
        connection = store.create_connection(
            _uid("connection-a"),
            provider="notion",
            credential_ciphertext="v1.ciphertext-a",
            account_label="Workspace A",
            granted_scopes=("read_content",),
            created_by="admin-a@example.com",
        )
        store.create_selection(
            _uid("selection-a"),
            connection_id=connection.id,
            external_resource_id="page-a",
            resource_name="Policy A",
            resource_type="notion_page",
            source_url="https://www.notion.so/page-a",
            transform_profile="notion-page.v1",
            rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            timezone="Europe/London",
        )
    with workspace_scope(202):
        assert store.list_connections() == []
        assert store.list_selections() == []
    with workspace_scope(101):
        assert [item.id for item in store.list_connections()] == [connection.id]
        assert len(store.list_selections(connection.id)) == 1
        assert "ciphertext-a" not in repr(store.get_connection(connection.id))


def test_disconnect_clears_credentials_but_preserves_history(
    store: SqlAlchemyCompanyBrainStore,
) -> None:
    connection = store.create_connection(
        _uid("connection-disconnect"),
        provider="slack",
        credential_ciphertext="v1.secret-ciphertext",
        account_label="Example Slack",
        granted_scopes=("channels:history", "channels:read"),
        created_by="admin@example.com",
    )
    selection = store.create_selection(
        _uid("selection-disconnect"),
        connection_id=connection.id,
        external_resource_id="C0123",
        resource_name="#security",
        resource_type="slack_channel",
        source_url="https://example.slack.com/archives/C0123",
        transform_profile="slack-thread.v1",
        rrule=None,
        timezone="UTC",
    )
    run = store.create_sync_run(
        _uid("run-disconnect"),
        connection_id=connection.id,
        selection_id=selection.id,
        trigger="manual",
    )

    disconnected = store.disconnect_connection(connection.id)

    assert disconnected is not None
    assert disconnected.status == "disconnected"
    assert disconnected.credential_ciphertext is None
    assert store.get_selection(selection.id) is not None
    assert [item.id for item in store.list_sync_runs()] == [run.id]


def test_sync_run_claim_is_atomic_and_errors_are_redacted(
    store: SqlAlchemyCompanyBrainStore,
) -> None:
    run = store.create_sync_run(
        _uid("run-claim"),
        connection_id=_uid("connection-claim"),
        selection_id=_uid("selection-claim"),
        trigger="retry",
    )

    claimed = store.claim_sync_run(run.id)
    duplicate = store.claim_sync_run(run.id)
    finished = store.finish_sync_run(
        run.id,
        status="failed",
        fetched_count=2,
        changed_count=1,
        deleted_count=0,
        skipped_count=1,
        commit_sha=None,
        gbrain_result=None,
        error="Bearer super-secret-token failed",
    )

    assert claimed is not None and claimed.status == "running"
    assert duplicate is None
    assert finished is not None and finished.status == "failed"
    assert finished.error == "Bearer [redacted] failed"


def test_new_sync_expires_orphaned_run_and_fences_its_completion(
    store: SqlAlchemyCompanyBrainStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000
    monkeypatch.setattr(store_module, "now_epoch", lambda: now)
    stale = store.create_sync_run(
        _uid("run-stale"),
        connection_id=_uid("connection-stale"),
        selection_id=_uid("selection-stale"),
        trigger="manual",
    )
    assert store.claim_sync_run(stale.id) is not None

    now += 3_601
    replacement = store.create_sync_run(
        _uid("run-replacement"),
        connection_id=stale.connection_id,
        selection_id=stale.selection_id,
        trigger="retry",
    )

    runs = {run.id: run for run in store.list_sync_runs(selection_id=stale.selection_id)}
    assert runs[stale.id].status == "failed"
    assert runs[stale.id].error == "Sync worker lease expired"
    assert runs[replacement.id].status == "pending"
    assert (
        store.finish_sync_run(
            stale.id,
            status="succeeded",
            fetched_count=1,
            changed_count=1,
            deleted_count=0,
            skipped_count=0,
            commit_sha="a" * 40,
            gbrain_result={"status": "ok"},
            error=None,
        )
        is None
    )


def test_oauth_nonce_claim_rejects_replay(store: SqlAlchemyCompanyBrainStore) -> None:
    assert store.claim_oauth_nonce("a" * 64, expires_at=2_000_000_000) is True
    assert store.claim_oauth_nonce("a" * 64, expires_at=2_000_000_000) is False
    assert store.claim_oauth_nonce("b" * 64, expires_at=1) is False


def test_scheduled_selection_scan_crosses_workspace_scope(
    store: SqlAlchemyCompanyBrainStore,
) -> None:
    for workspace_id in (101, 202):
        with workspace_scope(workspace_id):
            store.create_selection(
                _uid(f"scheduled-{workspace_id}"),
                connection_id=_uid(f"connection-{workspace_id}"),
                external_resource_id=f"resource-{workspace_id}",
                resource_name="Policies",
                resource_type="notion_page",
                source_url="https://www.notion.so/policies",
                transform_profile="notion-page.v1",
                rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
                timezone="UTC",
            )

    scheduled = store.list_scheduled_selections_all_workspaces()

    assert [item.workspace_id for item in scheduled] == [101, 202]


def test_selection_schedule_can_be_cleared(store: SqlAlchemyCompanyBrainStore) -> None:
    selection = store.create_selection(
        _uid("clear-schedule"),
        connection_id=_uid("clear-schedule-connection"),
        external_resource_id="page-clear",
        resource_name="Policies",
        resource_type="notion_page",
        source_url="https://www.notion.so/page-clear",
        transform_profile="notion-page.v1",
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        timezone="UTC",
    )

    updated = store.update_selection(selection.id, rrule=None)

    assert updated is not None
    assert updated.rrule is None


@pytest.mark.parametrize(
    ("rrule", "timezone"),
    [("FREQ=MINUTELY", "UTC"), ("FREQ=DAILY", "Not/A-Timezone")],
)
def test_selection_rejects_invalid_schedule_at_store_boundary(
    store: SqlAlchemyCompanyBrainStore,
    rrule: str,
    timezone: str,
) -> None:
    with pytest.raises(ValueError):
        store.create_selection(
            _uid(f"invalid-{rrule}-{timezone}"),
            connection_id=_uid("invalid-schedule-connection"),
            external_resource_id="page-invalid",
            resource_name="Policies",
            resource_type="notion_page",
            source_url="https://www.notion.so/page-invalid",
            transform_profile="notion-page.v1",
            rrule=rrule,
            timezone=timezone,
        )
