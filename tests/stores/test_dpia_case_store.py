from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.stores.dpia_case_store import DpiaCaseConflictError, SqlAlchemyDpiaCaseStore

_POSTGRES_URL = os.environ.get("DPIA_CASE_POSTGRES_TEST_URL")


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyDpiaCaseStore:
    return SqlAlchemyDpiaCaseStore(db_uri)


def _snapshot(case_id: str, value: str) -> dict[str, object]:
    return {
        "id": case_id,
        "title": "Synthetic DPIA",
        "value": value,
        "processingModel": {"caseId": case_id, "version": 1},
        "audit": [],
    }


def test_case_writes_append_revisions_and_reject_stale_updates(
    store: SqlAlchemyDpiaCaseStore,
) -> None:
    created = store.save_case(
        "student-success-alert",
        _snapshot("student-success-alert", "initial"),
        expected_revision=0,
        actor="officer@example.com",
    )
    updated = store.save_case(
        created.case_id,
        _snapshot(created.case_id, "updated"),
        expected_revision=created.revision,
        actor="reviewer@example.com",
    )

    with pytest.raises(DpiaCaseConflictError) as conflict:
        store.save_case(
            created.case_id,
            _snapshot(created.case_id, "stale"),
            expected_revision=created.revision,
            actor="stale@example.com",
        )

    assert conflict.value.current_revision == 2
    assert store.get_case(created.case_id) == updated
    revisions = store.list_revisions(created.case_id)
    assert [item.revision for item in revisions] == [1, 2]
    assert [item.snapshot["value"] for item in revisions] == ["initial", "updated"]
    assert [item.actor for item in revisions] == ["officer@example.com", "reviewer@example.com"]


def test_cases_are_workspace_scoped(store: SqlAlchemyDpiaCaseStore) -> None:
    with workspace_scope(101):
        first = store.save_case(
            "student-success-alert",
            _snapshot("student-success-alert", "workspace-a"),
            expected_revision=0,
            actor="a@example.com",
        )
    with workspace_scope(202):
        assert store.get_case(first.case_id) is None
        second = store.save_case(
            first.case_id,
            _snapshot(first.case_id, "workspace-b"),
            expected_revision=0,
            actor="b@example.com",
        )
        assert second.revision == 1
    with workspace_scope(101):
        assert store.get_case(first.case_id) == first


def test_case_identity_is_validated(store: SqlAlchemyDpiaCaseStore) -> None:
    with pytest.raises(ValueError, match="match"):
        store.save_case(
            "student-success-alert",
            _snapshot("another-case", "invalid"),
            expected_revision=0,
            actor="officer@example.com",
        )


def test_case_structure_and_size_are_bounded(store: SqlAlchemyDpiaCaseStore) -> None:
    case_id = "student-success-alert"
    invalid = _snapshot(case_id, "invalid")
    invalid["processingModel"] = {"caseId": "another-case", "version": 1}
    with pytest.raises(ValueError, match="processing model"):
        store.save_case(
            case_id,
            invalid,
            expected_revision=0,
            actor="officer@example.com",
        )

    oversized = _snapshot(case_id, "x" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="2 MiB"):
        store.save_case(
            case_id,
            oversized,
            expected_revision=0,
            actor="officer@example.com",
        )


@pytest.mark.skipif(_POSTGRES_URL is None, reason="DPIA_CASE_POSTGRES_TEST_URL is not configured")
def test_postgres_concurrent_writers_create_one_revision() -> None:
    assert _POSTGRES_URL is not None
    case_id = f"concurrent-{uuid.uuid4().hex}"
    store = SqlAlchemyDpiaCaseStore(_POSTGRES_URL)
    created = store.save_case(
        case_id,
        _snapshot(case_id, "initial"),
        expected_revision=0,
        actor="creator@example.com",
    )

    def write(value: str) -> int | DpiaCaseConflictError:
        try:
            return store.save_case(
                case_id,
                _snapshot(case_id, value),
                expected_revision=created.revision,
                actor=f"{value}@example.com",
            ).revision
        except DpiaCaseConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, ("first", "second")))

    assert sum(result == 2 for result in results) == 1
    conflicts = [result for result in results if isinstance(result, DpiaCaseConflictError)]
    assert len(conflicts) == 1
    assert conflicts[0].current_revision == 2
    current = store.get_case(case_id)
    assert current is not None
    assert current.revision == 2
    assert [item.revision for item in store.list_revisions(case_id)] == [1, 2]
