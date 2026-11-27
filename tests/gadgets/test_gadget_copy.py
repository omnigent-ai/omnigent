"""Make a copy: a gadget the copier owns, with the source's published code and, optionally,
its records; nothing else of the source's, and no link back to it."""

from __future__ import annotations

import pytest

from omnigent.gadgets import service as service_module
from omnigent.gadgets.constraints import ConstraintViolation
from omnigent.stores.gadget_store import CapacityExceeded, InvalidInput, NotFound
from tests.gadgets.conftest import READ, Fixture, person
from tests.gadgets.test_gadget_service import (
    WIP_LIMIT,
    card,
    publish_board,
    publish_ruled_board,
    rpc,
)


def _records(service, app: dict) -> list[tuple[str, dict]]:
    page = rpc(service, app, "list", limit=100)
    return [(item["id"], item["value"]) for item in page["items"]]


def test_a_reader_copies_the_published_code_without_the_data(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    source = publish_board(owner)
    rpc(owner, source, "create", id="t1", value={"title": "Owner's task"}, operation_id="o1")
    owner.create_comment(
        source["id"],
        {"body": "Synthetic comment", "anchor": {"text": "Synthetic"}, "operation_id": "c1"},
    )
    gadgets.access.share(source, 2, READ)
    reader = gadgets.service(2)

    copied = reader.copy(source["id"], {})

    assert copied["id"] != source["id"]
    assert copied["owner_id"] == person(2)
    assert copied["name"] == "Copy of Synthetic team board"
    assert copied["permissions"]["manage"] is True
    assert copied["published_revision"] is not None
    assert copied["published_revision"] != source["published_revision"]
    source_code = owner.get_revision(source["id"])
    copy_code = reader.get_revision(copied["id"])
    for field in ("html", "capabilities", "collections"):
        assert copy_code[field] == source_code[field]
    assert _records(reader, copied) == []
    assert reader.list_comments(copied["id"])["items"] == []
    # The source is untouched, and its owner can't reach the copy.
    assert _records(owner, source) == [("t1", {"title": "Owner's task"})]
    with pytest.raises(NotFound):
        owner.get(copied["id"])
    assert [item["id"] for item in reader.list_owned()["items"]] == [copied["id"]]


def test_a_copy_with_data_keeps_the_records_and_then_goes_its_own_way(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    source = publish_board(owner)
    for number in range(3):
        rpc(
            owner,
            source,
            "create",
            id=f"t{number}",
            value={"title": f"Task {number}"},
            operation_id=f"o{number}",
        )
    gadgets.access.share(source, 2, READ)
    reader = gadgets.service(2)

    copied = reader.copy(source["id"], {"name": "My board", "include_data": True})

    assert copied["name"] == "My board"
    assert _records(reader, copied) == [(f"t{n}", {"title": f"Task {n}"}) for n in range(3)]
    # Later writes on either side stay on that side.
    first = rpc(reader, copied, "get", id="t0")
    rpc(
        reader,
        copied,
        "replace",
        id="t0",
        value={"title": "Changed in the copy"},
        if_version=first["version"],
        operation_id="c1",
    )
    rpc(owner, source, "create", id="t9", value={"title": "Added later"}, operation_id="o9")
    assert dict(_records(owner, source))["t0"] == {"title": "Task 0"}
    assert "t9" not in dict(_records(reader, copied))


def test_copied_records_count_under_the_copys_rules(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    source = publish_ruled_board(owner, [WIP_LIMIT])
    card(owner, source, "a", "doing")
    card(owner, source, "b", "doing")
    gadgets.access.share(source, 2, READ)
    reader = gadgets.service(2)

    copied = reader.copy(source["id"], {"include_data": True})

    with pytest.raises(ConstraintViolation):
        card(reader, copied, "c", "doing")


def test_a_gadget_the_viewer_cant_read_or_that_was_never_published_is_not_copied(
    gadgets: Fixture,
) -> None:
    owner = gadgets.service(1)
    published = publish_board(owner)
    unbuilt = owner.create("Synthetic draft")
    gadgets.access.share(unbuilt, 2, READ)
    other = gadgets.service(2)

    with pytest.raises(NotFound):
        gadgets.service(3).copy(published["id"], {})
    with pytest.raises(NotFound):
        other.copy(unbuilt["id"], {})
    with pytest.raises(InvalidInput):
        owner.copy(published["id"], {"include_data": "yes"})

    assert other.list_owned()["items"] == []
    assert gadgets.service(3).list_owned()["items"] == []


def test_data_over_the_copy_limit_is_refused_before_anything_is_created(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "MAX_COPIED_RECORDS", 2)
    owner = gadgets.service(1)
    source = publish_board(owner)
    for number in range(3):
        rpc(
            owner,
            source,
            "create",
            id=f"t{number}",
            value={"title": "x"},
            operation_id=f"o{number}",
        )

    with pytest.raises(CapacityExceeded):
        owner.copy(source["id"], {"include_data": True})

    assert [item["id"] for item in owner.list_owned()["items"]] == [source["id"]]
    # Without the data the same gadget copies.
    assert owner.copy(source["id"], {})["published_revision"] is not None


def test_a_copy_that_fails_half_way_is_removed(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    source = publish_board(owner)
    rpc(owner, source, "create", id="t1", value={"title": "x"}, operation_id="o1")
    rpc(owner, source, "create", id="t2", value={"title": "y"}, operation_id="o2")

    def fail_copied_data(path: str) -> None:
        # The copy only reads the source, so the data written now is the copy's: the first
        # record lands, the second fails.
        if path.startswith("data/cards/t2/"):
            raise OSError("synthetic storage failure")

    gadgets.store.before_create = fail_copied_data
    with pytest.raises(OSError):
        owner.copy(source["id"], {"include_data": True})
    gadgets.store.before_create = None

    assert [item["id"] for item in owner.list_owned()["items"]] == [source["id"]]


@pytest.mark.parametrize("failing", ["permissions", "forget"])
def test_a_copy_is_removed_whichever_later_step_fails(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    owner = gadgets.service(1)
    source = publish_board(owner)
    real_permissions = gadgets.access.permissions

    def permissions(viewer_id, gadget):
        if gadget.id != source["id"]:
            raise RuntimeError("synthetic permission lookup failure")
        return real_permissions(viewer_id, gadget)

    if failing == "permissions":
        monkeypatch.setattr(gadgets.access, "permissions", permissions)
    else:
        # The copy fails publishing, then its grant cleanup fails too.
        # The publish failure, not the cleanup's, is what the caller sees.
        monkeypatch.setattr(owner, "_publish", lambda *args: (_ for _ in ()).throw(OSError("x")))
        monkeypatch.setattr(
            gadgets.access, "forget", lambda gadget_id: (_ for _ in ()).throw(RuntimeError())
        )

    expected = RuntimeError if failing == "permissions" else OSError
    with pytest.raises(expected):
        owner.copy(source["id"], {})
    monkeypatch.undo()

    assert [item["id"] for item in owner.list_owned()["items"]] == [source["id"]]


def test_the_published_version_is_copied_not_a_newer_draft(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    source = publish_board(owner)
    owner.create_revision(
        source["id"],
        {
            "html": "<main>Unpublished draft</main>",
            "sdk_version": 1,
            "capabilities": ["data.read"],
            "collections": {},
        },
    )

    copied = owner.copy(source["id"], {})

    assert owner.get_revision(copied["id"])["html"] == "<main>Synthetic app fixture</main>"


def test_data_is_copied_across_pages_and_collections_and_capped_over_all_of_them(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = gadgets.service(1)
    app = owner.create("Two collections")
    revision = owner.create_revision(
        app["id"],
        {
            "html": "<main>Two</main>",
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write"],
            "collections": {"cards": {"type": "object"}, "notes": {"type": "object"}},
        },
    )
    source = owner.publish(app["id"], {"revision_id": revision["id"], "if_version": None})
    page = service_module._DATA_STORE_LIMITS.max_list_limit
    for number in range(page + 3):
        rpc(
            owner,
            source,
            "create",
            id=f"c{number}",
            value={"n": number},
            operation_id=f"c{number}",
        )
    for number in range(2):
        owner.rpc(
            source["id"],
            source["published_revision"],
            "data.v1.create",
            {"collection": "notes", "id": f"n{number}", "value": {}, "operation_id": f"n{number}"},
        )

    copied = owner.copy(source["id"], {"include_data": True})
    listed = rpc(owner, copied, "list", limit=page)
    rest = rpc(owner, copied, "list", limit=page, cursor=listed["next_cursor"])
    assert len(listed["items"]) + len(rest["items"]) == page + 3
    notes = owner.rpc(
        copied["id"], copied["published_revision"], "data.v1.list", {"collection": "notes"}
    )
    assert [item["id"] for item in notes["items"]] == ["n0", "n1"]

    # The cap counts every collection: the cards alone fit, the notes push it over.
    monkeypatch.setattr(service_module, "MAX_COPIED_RECORDS", page + 4)
    with pytest.raises(CapacityExceeded):
        owner.copy(source["id"], {"include_data": True})


def test_a_copy_reads_a_bounded_number_of_pages_however_much_was_deleted(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleted records keep their place in a collection's order, so pages can come back
    empty and still continue; the copy stops after a fixed number of them."""
    monkeypatch.setattr(service_module, "MAX_COPY_PAGES", 3)
    monkeypatch.setattr(
        service_module, "_DATA_STORE_LIMITS", service_module.StoreLimits(max_scan_per_list=2)
    )
    owner = gadgets.service(1)
    source = publish_board(owner)
    for number in range(8):
        created = rpc(
            owner,
            source,
            "create",
            id=f"d{number}",
            value={"title": "x"},
            operation_id=f"d{number}",
        )
        rpc(
            owner,
            source,
            "delete",
            id=f"d{number}",
            if_version=created["version"],
            operation_id=f"x{number}",
        )
    pages: list[str | None] = []
    real = service_module.ImmutableDocumentStore.list

    def counted(self, collection, limit=100, cursor=None):  # type: ignore[no-untyped-def]
        pages.append(cursor)
        return real(self, collection, limit, cursor)

    monkeypatch.setattr(service_module.ImmutableDocumentStore, "list", counted)

    with pytest.raises(CapacityExceeded, match="too much history"):
        owner.copy(source["id"], {"include_data": True})
    assert len(pages) == 3
    assert [item["id"] for item in owner.list_owned()["items"]] == [source["id"]]
