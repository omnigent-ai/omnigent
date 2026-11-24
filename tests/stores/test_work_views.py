"""Saved queries and durable tasks, using the migrated isolated database."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from omnigent.db.db_models import workspace_scope
from omnigent.entities.work_view import (
    LinkWorkSession,
    SaveView,
    SaveWorkTask,
    ViewDisplay,
    ViewFilters,
    ViewQuery,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.work_view_store import WorkViewStore


def test_tasks_survive_project_and_view_deletion(db_uri: str) -> None:
    store = WorkViewStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(uuid.uuid4().hex, "Release", None)
    standalone = store.save_task(SaveWorkTask(title=" Independent "), None)
    assigned = store.save_task(SaveWorkTask(title="Ship", project_id=project.id), None)
    assert standalone["title"] == "Independent"
    assert standalone["project_id"] is None
    assert assigned["project_name"] == "Release"
    saved = store.save_view(
        SaveView(name="Release", filters=ViewFilters(project_ids=[project.id])), None
    )
    assert store.query(ViewQuery(filters=ViewFilters(project_ids=[project.id])), None)["data"] == [
        assigned
    ]
    store.delete_view(saved["id"], None)
    projects.delete(project.id, user_id=None)
    assert store.get_task(assigned["id"], None)["project_id"] is None
    assert len(store.query(ViewQuery(filters=ViewFilters(no_project=True)), None)["data"]) == 2
    edited = store.save_task(SaveWorkTask(title="Ship", status="done"), None, assigned["id"])
    assert edited["status"] == "done"


@pytest.mark.parametrize("order_by", ["title", "priority", "created_at", "updated_at"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_filtered_keyset_pages_and_display_preserve_membership(
    db_uri: str, order_by: str, direction: str
) -> None:
    store = WorkViewStore(db_uri)
    tasks = [
        store.save_task(SaveWorkTask(title=f"Item {index}", priority=index % 5), None)
        for index in range(7)
    ]
    store.save_task(SaveWorkTask(title="Excluded", status="done"), None)
    query = ViewQuery(
        filters=ViewFilters(statuses=["todo"]),
        display=ViewDisplay(order_by=order_by, direction=direction),
        limit=2,
    )
    pages = []
    while True:
        page = store.query(query, None)
        pages.extend(page["data"])
        if not page["has_more"]:
            break
        query.cursor = page["next_cursor"]

    def sort_key(task):
        value = task[order_by]
        return (5 if order_by == "priority" and value == 0 else value, task["id"])

    assert [task["id"] for task in pages] == [
        task["id"] for task in sorted(tasks, key=sort_key, reverse=direction == "desc")
    ]
    query.cursor, query.limit = None, 100
    list_page = store.query(query, None)
    query.display.layout, query.display.group_by = "board", "priority"
    assert store.query(query, None) == list_page


def test_combined_filters_search_and_project_queries(db_uri: str) -> None:
    store = WorkViewStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(uuid.uuid4().hex, "100% ready", "alice")
    projects.create(uuid.uuid4().hex, "Other", "alice")
    matching = store.save_task(
        SaveWorkTask(title="100% ready", project_id=project.id, status="review", priority=1),
        "alice",
    )
    store.save_task(SaveWorkTask(title="1000 ready", status="review", priority=1), "alice")
    filters = ViewFilters(
        search="%",
        priorities=[1],
        statuses=["review"],
        project_ids=[project.id],
        no_project=True,
        updated_after=matching["updated_at"],
    )
    assert store.query(ViewQuery(filters=filters), "alice")["data"] == [matching]
    filters.updated_after += 1
    assert store.query(ViewQuery(filters=filters), "alice")["data"] == []
    project_query = ViewQuery(
        entity_type="projects",
        display=ViewDisplay(group_by="none", order_by="title"),
        filters=ViewFilters(search="%"),
    )
    assert [row["id"] for row in store.query(project_query, "alice")["data"]] == [project.id]


def test_personal_views_roundtrip_and_owner_workspace_isolation(db_uri: str) -> None:
    store = WorkViewStore(db_uri)
    with workspace_scope(101):
        task = store.save_task(SaveWorkTask(title="Private"), "alice")
        definition = SaveView(
            name="My work",
            description="Review next",
            favorite=True,
            filters=ViewFilters(statuses=["review"], no_project=True),
            display=ViewDisplay(layout="board", group_by="priority", properties=["priority"]),
        )
        saved = store.save_view(definition, "alice")
        definition.display.layout = "list"
        store.save_view(definition, "alice", saved["id"])
        reopened = WorkViewStore(db_uri).get_view(saved["id"], "alice")
        assert {key: reopened[key] for key in definition.model_dump()} == definition.model_dump()
        assert store.list_views("bob")["data"] == []
        assert store.query(ViewQuery(), "bob")["data"] == []
        for operation in (
            lambda: store.get_view(saved["id"], "bob"),
            lambda: store.save_view(definition, "bob", saved["id"]),
            lambda: store.delete_view(saved["id"], "bob"),
            lambda: store.get_task(task["id"], "bob"),
            lambda: store.delete_task(task["id"], "bob"),
            lambda: store.save_task(SaveWorkTask(title="Changed"), "bob", task["id"]),
            lambda: store.query(ViewQuery(cursor=task["id"]), "bob"),
            lambda: store.list_views("bob", saved["id"]),
        ):
            with pytest.raises(OmnigentError) as exc:
                operation()
            assert exc.value.code == ErrorCode.NOT_FOUND
    with workspace_scope(102):
        assert store.list_views("alice")["data"] == []
        with pytest.raises(OmnigentError):
            store.get_task(task["id"], "alice")
        with pytest.raises(OmnigentError):
            store.delete_task(task["id"], "alice")
    with workspace_scope(101):
        assert store.get_task(task["id"], "alice")["title"] == "Private"


@pytest.mark.parametrize("session_count", [0, 2])
def test_delete_task_releases_only_its_session_links(db_uri: str, session_count: int) -> None:
    store = WorkViewStore(db_uri)
    task = store.save_task(SaveWorkTask(title="Delete me"), None)
    retained = store.save_task(SaveWorkTask(title="Keep me"), None)
    retained_link = LinkWorkSession(session_id=uuid.uuid4().hex)
    store.link_session(retained["id"], retained_link, None)
    links = [LinkWorkSession(session_id=uuid.uuid4().hex) for _ in range(session_count)]
    for link in links:
        store.link_session(task["id"], link, None)

    store.delete_task(task["id"], None)

    for operation in (store.get_task, store.delete_task):
        with pytest.raises(OmnigentError) as exc:
            operation(task["id"], None)
        assert exc.value.code == ErrorCode.NOT_FOUND
    assert [row["id"] for row in store.query(ViewQuery(), None)["data"]] == [retained["id"]]
    assert store.get_task(retained["id"], None)["sessions"] == [retained_link.model_dump()]
    for link in links:
        store.link_session(retained["id"], link, None)
    assert len(store.get_task(retained["id"], None)["sessions"]) == session_count + 1


def test_task_session_one_to_many_and_explicit_reassignment(db_uri: str) -> None:
    store = WorkViewStore(db_uri)
    first = store.save_task(SaveWorkTask(title="First"), None)
    second = store.save_task(SaveWorkTask(title="Second"), None)
    session_ids = [uuid.uuid4().hex for _ in range(2)]
    for session_id in session_ids:
        store.link_session(first["id"], LinkWorkSession(session_id=session_id), None)
    link = LinkWorkSession(session_id=session_ids[0], involvement="Implementation")
    store.link_session(first["id"], link, None)
    detail = store.get_task(first["id"], None)
    assert len(detail["sessions"]) == 2
    assert any(row["involvement"] == "Implementation" for row in detail["sessions"])
    with pytest.raises(OmnigentError) as exc:
        store.link_session(second["id"], link, None)
    assert exc.value.code == ErrorCode.ALREADY_EXISTS
    store.unlink_session(first["id"], session_ids[0], None)
    store.link_session(second["id"], link, None)
    assert len(store.get_task(first["id"], None)["sessions"]) == 1
    assert store.get_task(second["id"], None)["sessions"] == [link.model_dump()]
    assert store.get_task(first["id"], None)["status"] == "todo"


@pytest.mark.parametrize(
    "body",
    [
        {"entity_type": "sessions", "filters": {"statuses": ["done"]}},
        {"entity_type": "projects", "filters": {"statuses": ["todo"]}},
        {"entity_type": "sessions", "display": {"order_by": "title"}},
        {"entity_type": "sessions", "filters": {"priorities": [1]}},
        {"filters": {"priorities": [5]}},
        {"filters": {"project_ids": ["invalid"]}},
    ],
)
def test_reject_incompatible_view_filters(body: dict) -> None:
    with pytest.raises(ValidationError):
        ViewQuery.model_validate(body)
