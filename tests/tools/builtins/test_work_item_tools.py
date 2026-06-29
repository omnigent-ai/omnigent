"""Tests for the work-item builtin tools (create / list / update)."""

from __future__ import annotations

import json

import pytest

from omnigent.stores.work_item_store.sqlalchemy_store import SqlAlchemyWorkItemStore
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.work_items import (
    CreateWorkItemTool,
    ListTasksTool,
    UpdateWorkItemTool,
)

_CTX = ToolContext(task_id="t1", agent_id="a1")


@pytest.fixture()
def wired_store(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> SqlAlchemyWorkItemStore:
    """A work-item store wired into ``runtime.get_work_item_store`` (lazy bind)."""
    store = SqlAlchemyWorkItemStore(db_uri)
    monkeypatch.setattr("omnigent.runtime.get_work_item_store", lambda: store)
    return store


def test_create_list_update_flow(wired_store: SqlAlchemyWorkItemStore) -> None:
    create = CreateWorkItemTool()
    out = json.loads(
        create.invoke(
            json.dumps(
                {
                    "title": "Fix deploy",
                    "source": "github",
                    "external_id": "123",
                    "dedup_key": "github:acme/app#123",
                }
            ),
            _CTX,
        )
    )
    assert out["created"] is True
    wid = out["work_item"]["id"]
    assert out["work_item"]["status"] == "new"
    # Cross-check it actually landed in the store.
    assert wired_store.get(wid) is not None

    # Same dedup_key → idempotent, resolves to the existing row.
    dup = json.loads(
        create.invoke(
            json.dumps({"title": "dup", "source": "github", "dedup_key": "github:acme/app#123"}),
            _CTX,
        )
    )
    assert dup["created"] is False
    assert dup["work_item"]["id"] == wid

    listed = json.loads(ListTasksTool().invoke(json.dumps({}), _CTX))
    assert listed["count"] == 1
    assert listed["work_items"][0]["id"] == wid

    upd = json.loads(
        UpdateWorkItemTool().invoke(
            json.dumps(
                {
                    "work_item_id": wid,
                    "needs_review": True,
                    "pr_url": "https://github.com/acme/app/pull/9",
                }
            ),
            _CTX,
        )
    )
    assert upd["work_item"]["status"] == "needs_review"
    assert upd["work_item"]["pr_url"].endswith("/pull/9")

    only_review = json.loads(ListTasksTool().invoke(json.dumps({"status": "needs_review"}), _CTX))
    assert only_review["count"] == 1
    assert json.loads(ListTasksTool().invoke(json.dumps({"status": "done"}), _CTX))["count"] == 0


def test_validation_errors(wired_store: SqlAlchemyWorkItemStore) -> None:
    create = CreateWorkItemTool()
    assert "error" in json.loads(
        create.invoke(json.dumps({"title": "x", "source": "bogus"}), _CTX)
    )
    assert "error" in json.loads(
        create.invoke(json.dumps({"source": "manual"}), _CTX)
    )  # missing title

    not_found = json.loads(
        UpdateWorkItemTool().invoke(
            json.dumps({"work_item_id": "wi_nope", "status": "done"}), _CTX
        )
    )
    assert not_found["error"] == "not_found"


def test_store_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.runtime.get_work_item_store", lambda: None)
    out = json.loads(ListTasksTool().invoke(json.dumps({}), _CTX))
    assert "error" in out
