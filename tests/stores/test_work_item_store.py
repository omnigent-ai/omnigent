"""Tests for SqlAlchemyWorkItemStore."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from omnigent.stores.work_item_store.sqlalchemy_store import SqlAlchemyWorkItemStore


def test_create_and_get(work_item_store: SqlAlchemyWorkItemStore) -> None:
    item = work_item_store.create(
        "wi_1",
        "github",
        "Fix the deploy script",
        dedup_key="github:acme/app#123",
        external_id="123",
        body="The Friday deploy fails on the migration step.",
    )
    assert item.id == "wi_1"
    assert item.source == "github"
    assert item.status == "new"
    assert item.dedup_key == "github:acme/app#123"
    assert item.created_at > 0
    assert item.updated_at is None

    fetched = work_item_store.get("wi_1")
    assert fetched is not None
    assert fetched.title == "Fix the deploy script"
    assert fetched.external_id == "123"


def test_get_nonexistent(work_item_store: SqlAlchemyWorkItemStore) -> None:
    assert work_item_store.get("wi_missing") is None


def test_duplicate_dedup_key_raises(work_item_store: SqlAlchemyWorkItemStore) -> None:
    work_item_store.create("wi_a", "slack", "First", dedup_key="slack:C1/167.1")
    with pytest.raises(IntegrityError):
        work_item_store.create("wi_b", "slack", "Dup", dedup_key="slack:C1/167.1")


def test_get_by_dedup_key(work_item_store: SqlAlchemyWorkItemStore) -> None:
    assert work_item_store.get_by_dedup_key("jira:OPS-7") is None
    work_item_store.create("wi_j", "jira", "Rotate keys", dedup_key="jira:OPS-7")
    found = work_item_store.get_by_dedup_key("jira:OPS-7")
    assert found is not None
    assert found.id == "wi_j"


def test_list_filters_and_orders_newest_first(
    work_item_store: SqlAlchemyWorkItemStore,
) -> None:
    work_item_store.create("wi_old", "manual", "Old", dedup_key="m:old", status="done")
    work_item_store.create("wi_new", "manual", "New", dedup_key="m:new", status="new")

    all_items = work_item_store.list()
    assert {i.id for i in all_items} == {"wi_old", "wi_new"}
    # Ordered newest-first: created_at must be non-increasing down the list.
    # (Two rows written in the same wall-clock second tie on created_at, so we
    # assert the ordering invariant rather than a specific tie order.)
    created = [i.created_at for i in all_items]
    assert created == sorted(created, reverse=True)

    only_new = work_item_store.list(status="new")
    assert [i.id for i in only_new] == ["wi_new"]

    assert work_item_store.list(status="blocked") == []


def test_update_sets_fields_and_timestamp(
    work_item_store: SqlAlchemyWorkItemStore,
) -> None:
    work_item_store.create("wi_u", "github", "Add metric", dedup_key="github:acme/app#9")

    updated = work_item_store.update(
        "wi_u",
        status="needs_review",
        pr_url="https://github.com/acme/app/pull/9",
        plan="1. add counter  2. wire dashboard",
    )
    assert updated is not None
    assert updated.status == "needs_review"
    assert updated.pr_url.endswith("/pull/9")
    assert updated.plan.startswith("1.")
    assert updated.updated_at is not None

    assert work_item_store.update("wi_missing", status="done") is None


def test_delete_is_idempotent(work_item_store: SqlAlchemyWorkItemStore) -> None:
    work_item_store.create("wi_d", "manual", "Temp", dedup_key="m:temp")
    assert work_item_store.delete("wi_d") is True
    assert work_item_store.get("wi_d") is None
    assert work_item_store.delete("wi_d") is False
