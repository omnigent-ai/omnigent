"""Collection scope: ``viewer`` collections keep each viewer's own records, in their files."""

from __future__ import annotations

import pytest

from omnigent.gadgets.constraints import ConstraintViolation
from omnigent.gadgets.service import GadgetError, GadgetService
from omnigent.stores.gadget_store import InvalidInput, NotFound
from tests.gadgets.conftest import READ, Fixture, person

COLLECTIONS = {
    "catalog": {"type": "object", "properties": {"title": {"type": "string"}}},
    "progress": {
        "type": "object",
        "properties": {"item": {"type": "string"}, "state": {"type": "string"}},
    },
}


def revision(
    service: GadgetService,
    app: dict,
    scopes: dict | None = None,
    constraints: list | None = None,
    collections: dict | None = None,
) -> dict:
    body = {
        "html": "<main>Synthetic course</main>",
        "sdk_version": 1,
        "capabilities": ["data.read", "data.write"],
        "collections": collections or COLLECTIONS,
        "collection_scopes": {"progress": "viewer"} if scopes is None else scopes,
    }
    if constraints:
        body["constraints"] = constraints
    return service.create_revision(app["id"], body)


def publish(service: GadgetService, **kwargs) -> dict:
    app = service.create("Synthetic course")
    created = revision(service, app, **kwargs)
    return service.publish(app["id"], {"revision_id": created["id"], "if_version": None})


def call(service: GadgetService, app: dict, method: str, collection: str, **params):
    return service.rpc(
        app["id"],
        app["published_revision"],
        "data.v1." + method,
        {"collection": collection, **params},
    )


def listed(service: GadgetService, app: dict, collection: str) -> list:
    return [
        (item["id"], item["value"])
        for item in call(service, app, "list", collection, limit=100)["items"]
    ]


def test_each_viewer_reads_and_writes_only_their_own_viewer_records(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    app = publish(owner)
    gadgets.access.share(app, 2, READ)
    reader = gadgets.service(2)
    call(owner, app, "create", "catalog", id="c1", value={"title": "Lesson"}, operation_id="o1")

    call(owner, app, "create", "progress", id="c1", value={"state": "done"}, operation_id="p1")
    # Can view is enough for one's own viewer records, and the same ID is theirs alone.
    call(reader, app, "create", "progress", id="c1", value={"state": "started"}, operation_id="p1")

    assert listed(owner, app, "progress") == [("c1", {"state": "done"})]
    assert listed(reader, app, "progress") == [("c1", {"state": "started"})]
    # Shared data is still shared, and still needs edit access to change.
    assert listed(reader, app, "catalog") == [("c1", {"title": "Lesson"})]
    with pytest.raises(GadgetError) as refused:
        call(reader, app, "create", "catalog", value={"title": "x"}, operation_id="r1")
    assert refused.value.code == "PERMISSION_DENIED"
    # The page a read-only viewer loads may write, so its own records can be saved.
    assert "data.write" in reader.content(app["id"])["capabilities"]
    assert reader.describe(app["id"])["collection_scopes"] == {"progress": "viewer"}


def test_viewer_records_live_in_the_viewers_own_files(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    app = publish(owner)
    call(owner, app, "create", "progress", id="r1", value={"state": "done"}, operation_id="p1")

    gadget_files = list((gadgets.root / "gadgets" / app["id"]).rglob("*.json"))
    viewer_files = list((gadgets.root / "users").rglob(f"viewer-data/{app['id']}/progress/**/*"))
    assert viewer_files
    assert not [path for path in gadget_files if "/progress/" in str(path)]


def test_unique_and_limit_rules_count_one_viewers_records(gadgets: Fixture) -> None:
    rules = [
        {"name": "one_per_item", "kind": "unique", "collection": "progress", "fields": ["item"]},
        {"name": "at_most_two", "kind": "max_records", "collection": "progress", "max": 2},
    ]
    owner = gadgets.service(1)
    app = publish(owner, constraints=rules)
    gadgets.access.share(app, 2, READ)
    reader = gadgets.service(2)

    call(owner, app, "create", "progress", id="a", value={"item": "x"}, operation_id="o1")
    # Another viewer may hold the same value, and has their own limit.
    call(reader, app, "create", "progress", id="a", value={"item": "x"}, operation_id="o1")
    with pytest.raises(ConstraintViolation):
        call(owner, app, "create", "progress", id="b", value={"item": "x"}, operation_id="o2")
    call(owner, app, "create", "progress", id="b", value={"item": "y"}, operation_id="o3")
    with pytest.raises(ConstraintViolation):
        call(owner, app, "create", "progress", id="c", value={"item": "z"}, operation_id="o4")
    call(reader, app, "create", "progress", id="b", value={"item": "y"}, operation_id="o2")


def test_references_stay_within_one_scope(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    app = owner.create("Synthetic course")
    across = {
        "name": "progress_item",
        "kind": "reference",
        "collection": "progress",
        "field": "item",
        "target": "catalog",
    }
    with pytest.raises(InvalidInput, match="across scopes"):
        revision(owner, app, constraints=[across])
    within = {**across, "target": "notes"}
    collections = {**COLLECTIONS, "notes": {"type": "object"}}
    created = revision(
        owner,
        app,
        scopes={"progress": "viewer", "notes": "viewer"},
        constraints=[within],
        collections=collections,
    )
    published = owner.publish(app["id"], {"revision_id": created["id"], "if_version": None})
    with pytest.raises(ConstraintViolation):
        call(owner, published, "create", "progress", value={"item": "n1"}, operation_id="p1")
    call(owner, published, "create", "notes", id="n1", value={}, operation_id="n1")
    call(owner, published, "create", "progress", value={"item": "n1"}, operation_id="p1")


def test_a_published_collections_scope_cannot_change(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    app = publish(owner)
    for scopes in ({}, {"progress": "viewer", "catalog": "viewer"}):
        changed = revision(owner, app, scopes=scopes)
        with pytest.raises(InvalidInput, match="schema and scope cannot change"):
            owner.publish(app["id"], {"revision_id": changed["id"], "if_version": app["version"]})
    # Declaring "shared" outright is the same as leaving it out.
    same = revision(owner, app, scopes={"progress": "viewer", "catalog": "shared"})
    owner.publish(app["id"], {"revision_id": same["id"], "if_version": app["version"]})


def test_a_scope_must_be_known_and_name_a_declared_collection(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    app = owner.create("Synthetic course")
    with pytest.raises(InvalidInput):
        revision(owner, app, scopes={"progress": "private"})
    with pytest.raises(InvalidInput):
        revision(owner, app, scopes={"missing": "viewer"})


def test_a_copy_never_takes_viewer_records(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    app = publish(owner)
    call(owner, app, "create", "catalog", id="c1", value={"title": "Lesson"}, operation_id="o1")
    call(owner, app, "create", "progress", id="c1", value={"state": "done"}, operation_id="p1")

    copied = owner.copy(app["id"], {"include_data": True})

    assert listed(owner, copied, "catalog") == [("c1", {"title": "Lesson"})]
    assert listed(owner, copied, "progress") == []
    assert owner.describe(copied["id"])["collection_scopes"] == {"progress": "viewer"}
    assert listed(owner, app, "progress") == [("c1", {"state": "done"})]
    assert person(1) == copied["owner_id"]


def test_a_viewers_first_viewer_record_marks_their_files(gadgets: Fixture) -> None:
    from omnigent.gadgets.service import VIEWER_DATA_MARKER

    owner = gadgets.service(1)
    app = publish(owner)
    files = gadgets.store.user_files(person(1))
    with pytest.raises(NotFound):
        files.read_json(VIEWER_DATA_MARKER)
    call(owner, app, "create", "catalog", value={"title": "x"}, operation_id="o1")
    with pytest.raises(NotFound):
        files.read_json(VIEWER_DATA_MARKER)
    call(owner, app, "create", "progress", value={}, operation_id="p1")
    call(owner, app, "create", "progress", value={}, operation_id="p2")
    assert files.read_json(VIEWER_DATA_MARKER) == {"viewer_data": True}
