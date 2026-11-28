"""Share as template: a grantee makes their own gadget from the published code and starter
data, without ever opening the template."""

from __future__ import annotations

import json

import pytest

from omnigent.gadgets.access import TEMPLATE_ONLY
from omnigent.stores.gadget_store import InvalidInput, NotFound
from tests.gadgets.conftest import READ, Fixture, person

COLLECTIONS = {
    "lessons": {"type": "object", "properties": {"title": {"type": "string"}}},
    "notes": {"type": "object"},
    "progress": {"type": "object"},
}


def publish_template(service, starters: list | None = None, html: str = "<main>v1</main>"):
    app = service.create("Synthetic course")
    revision = service.create_revision(
        app["id"],
        {
            "html": html,
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write"],
            "collections": COLLECTIONS,
            "collection_scopes": {"progress": "viewer"},
            "starter_collections": ["lessons"] if starters is None else starters,
        },
    )
    return service.publish(app["id"], {"revision_id": revision["id"], "if_version": None})


def call(service, app: dict, method: str, collection: str, **params):
    return service.rpc(
        app["id"],
        app["published_revision"],
        "data.v1." + method,
        {"collection": collection, **params},
    )


def ids(service, app: dict, collection: str) -> list[str]:
    return [item["id"] for item in call(service, app, "list", collection, limit=100)["items"]]


@pytest.fixture
def course(gadgets: Fixture) -> dict:
    owner = gadgets.service(1)
    app = publish_template(owner)
    call(owner, app, "create", "lessons", id="l1", value={"title": "One"}, operation_id="l1")
    call(owner, app, "create", "notes", id="n1", value={}, operation_id="n1")
    call(owner, app, "create", "progress", id="p1", value={}, operation_id="p1")
    gadgets.access.share(app, 2, TEMPLATE_ONLY)
    return app


def test_a_template_grantee_never_opens_the_template(gadgets: Fixture, course: dict) -> None:
    user = gadgets.service(2)
    for read in (
        lambda: user.get(course["id"]),
        lambda: user.content(course["id"]),
        lambda: user.describe(course["id"]),
        lambda: user.get_revision(course["id"]),
        lambda: user.list_comments(course["id"]),
        lambda: call(user, course, "list", "lessons"),
        lambda: user.copy(course["id"], {}),
    ):
        with pytest.raises(NotFound):
            read()
    # Nor is it listed as shared with them: only as a template.
    assert user.shared()["items"] == []
    assert [item["id"] for item in user.templates()["items"]] == [course["id"]]
    assert user.templates()["items"][0]["owner_email"] == person(1)


def test_creating_from_a_template_copies_the_code_and_starter_data_only(
    gadgets: Fixture, course: dict
) -> None:
    user = gadgets.service(2)

    made = user.create_from_template(course["id"], {})

    assert made["owner_id"] == person(2) and made["name"] == "Synthetic course"
    assert made["permissions"]["publish"] is True
    assert user.get_revision(made["id"])["html"] == "<main>v1</main>"
    assert ids(user, made, "lessons") == ["l1"]
    assert ids(user, made, "notes") == []
    assert ids(user, made, "progress") == []
    # Its source is kept for later, on the new gadget's own files.
    source = gadgets.store.gadget_files(made["id"])
    entry = next(
        path
        for path in (gadgets.root / "gadgets" / made["id"]).rglob("*.json")
        if "/template/source/" in str(path)
    )
    assert json.loads(entry.read_text())["value"] == {
        "gadget_id": course["id"],
        "revision_id": gadgets.service(1).get(course["id"])["published_revision"],
    }
    assert source is not None


def test_the_creator_customizes_right_away_and_the_template_doesnt_follow(
    gadgets: Fixture, course: dict
) -> None:
    owner, user = gadgets.service(1), gadgets.service(2)
    made = user.create_from_template(course["id"], {"name": "My course"})
    assert made["name"] == "My course"
    revision = user.create_revision(
        made["id"],
        {
            "html": "<main>mine</main>",
            "sdk_version": 1,
            "capabilities": ["data.read"],
            "collections": COLLECTIONS,
            "collection_scopes": {"progress": "viewer"},
        },
    )
    user.publish(made["id"], {"revision_id": revision["id"], "if_version": made["version"]})

    # Revoking and deleting the template leaves the gadget made from it.
    gadgets.access.forget(course["id"])
    owner.delete(course["id"])
    assert user.get_revision(user.get(made["id"])["id"])["html"] == "<main>mine</main>"
    assert ids(user, user.get(made["id"]), "lessons") == ["l1"]


def test_a_template_uses_its_current_published_code(gadgets: Fixture, course: dict) -> None:
    owner, user = gadgets.service(1), gadgets.service(2)
    revision = owner.create_revision(
        course["id"],
        {
            "html": "<main>v2</main>",
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write"],
            "collections": COLLECTIONS,
            "collection_scopes": {"progress": "viewer"},
        },
    )
    owner.publish(course["id"], {"revision_id": revision["id"], "if_version": course["version"]})
    # A draft after it is not what templates use.
    owner.create_revision(
        course["id"],
        {"html": "<main>draft</main>", "sdk_version": 1, "capabilities": [], "collections": {}},
    )

    made = user.create_from_template(course["id"], {})

    assert user.get_revision(made["id"])["html"] == "<main>v2</main>"
    # That revision marks no starter data, so the new gadget starts empty.
    assert ids(user, made, "lessons") == []


def test_only_grantees_and_readers_make_gadgets_from_a_published_template(
    gadgets: Fixture, course: dict
) -> None:
    with pytest.raises(NotFound):
        gadgets.service(3).create_from_template(course["id"], {})
    gadgets.access.share(course, 3, READ)
    # A reader can, too; they see it in Shared, not in Templates.
    assert gadgets.service(3).create_from_template(course["id"], {})["owner_id"] == person(3)
    assert gadgets.service(3).templates()["items"] == []

    draft = gadgets.service(1).create("Unpublished")
    gadgets.access.share(draft, 2, TEMPLATE_ONLY)
    with pytest.raises(NotFound):
        gadgets.service(2).create_from_template(draft["id"], {})
    assert [item["id"] for item in gadgets.service(2).templates()["items"]] == [course["id"]]


def test_starter_collections_must_be_declared_and_shared(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    with pytest.raises(InvalidInput):
        publish_template(owner, starters=["missing"])
    with pytest.raises(InvalidInput, match="per viewer"):
        publish_template(owner, starters=["progress"])


def test_starter_data_keeps_its_references_whole(gadgets: Fixture) -> None:
    owner = gadgets.service(1)
    rule = {
        "name": "lesson_course",
        "kind": "reference",
        "collection": "lessons",
        "field": "course",
        "target": "courses",
    }

    def course_revision(app, starters):
        return owner.create_revision(
            app["id"],
            {
                "html": "<main>Course</main>",
                "sdk_version": 1,
                "capabilities": ["data.read", "data.write"],
                "collections": {
                    "courses": {"type": "object"},
                    "lessons": {"type": "object", "properties": {"course": {"type": "string"}}},
                },
                "constraints": [rule],
                "starter_collections": starters,
            },
        )

    app = owner.create("Synthetic course")
    with pytest.raises(InvalidInput, match="make courses a starter collection too"):
        course_revision(app, ["lessons"])
    created = course_revision(app, ["lessons", "courses"])
    app = owner.publish(app["id"], {"revision_id": created["id"], "if_version": None})
    call(owner, app, "create", "courses", id="c1", value={}, operation_id="c1")
    call(owner, app, "create", "lessons", id="l1", value={"course": "c1"}, operation_id="l1")
    gadgets.access.share(app, 2, TEMPLATE_ONLY)

    made = gadgets.service(2).create_from_template(app["id"], {})

    assert ids(gadgets.service(2), made, "lessons") == ["l1"]
    assert ids(gadgets.service(2), made, "courses") == ["c1"]
