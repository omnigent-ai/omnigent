"""GadgetService on a real local store: records, publication, rules, actions, shelf, comments."""

from __future__ import annotations

import contextlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from omnigent.gadgets import constraints as constraints_module
from omnigent.gadgets import documents as documents_module
from omnigent.gadgets import service as service_module
from omnigent.gadgets.constraints import ConstraintGuard, ConstraintViolation
from omnigent.gadgets.documents import StoreLimits
from omnigent.gadgets.service import GadgetError, GadgetService, GadgetViewer
from omnigent.stores.gadget_store import (
    CapacityExceeded,
    Conflict,
    CorruptData,
    InvalidInput,
    NotFound,
)
from omnigent.stores.gadget_store.local import LocalGadgetFiles
from tests.gadgets.conftest import DATA_EDIT, EDIT, READ, Fixture, HookedFiles, person

CARDS = {
    "cards": {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}}}
}


def publish_board(service: GadgetService) -> dict:
    app = service.create("Synthetic team board")
    revision = service.create_revision(
        app["id"],
        {
            "html": "<main>Synthetic app fixture</main>",
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write", "sessions.read"],
            "collections": CARDS,
        },
    )
    return service.publish(app["id"], {"revision_id": revision["id"], "if_version": None})


def rpc(service: GadgetService, app: dict, method: str, **params):
    return service.rpc(
        app["id"],
        app["published_revision"],
        "data.v1." + method,
        {"collection": "cards", **params},
    )


def make_revision(service: GadgetService, app: dict, collections: dict) -> dict:
    return service.create_revision(
        app["id"],
        {
            "html": "<main>Synthetic schema revision</main>",
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write"],
            "collections": collections,
        },
    )


def test_card_crud_and_idempotent_create_survive_new_service_instances(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = publish_board(service)
    created = rpc(service, app, "create", value={"title": "Task"}, operation_id="create:one")
    replay = rpc(
        gadgets.service(), app, "create", value={"title": "Task"}, operation_id="create:one"
    )
    assert replay == created
    updated = rpc(
        service,
        app,
        "replace",
        id=created["id"],
        value={"title": "Moved"},
        if_version=created["version"],
        operation_id="move:one",
    )
    assert updated["value"] == {"title": "Moved"}
    with pytest.raises(Conflict):
        rpc(
            service,
            app,
            "replace",
            id=created["id"],
            value={"title": "Stale"},
            if_version=created["version"],
            operation_id="stale:one",
        )
    assert rpc(service, app, "list", limit=10)["items"] == [updated]
    deleted = rpc(
        service, app, "delete", id=created["id"], if_version=updated["version"], operation_id="d:1"
    )
    assert deleted["deleted"] is True
    assert rpc(service, app, "get", id=created["id"]) is None


def test_sharing_data_does_not_grant_frontend_publication(gadgets: Fixture) -> None:
    app = publish_board(gadgets.service())
    gadgets.access.share(app, 2, DATA_EDIT)
    teammate = gadgets.service(2)
    seen = teammate.get(app["id"])
    assert seen["permissions"] == {
        "read": True,
        "write_data": True,
        "publish": False,
        "manage": False,
    }
    assert seen["viewer_id"] == person(2)
    assert seen["owner_id"] == person(1)
    shared = rpc(teammate, app, "create", id="shared", value={"title": "Mate"}, operation_id="c-s")
    assert shared["id"] == "shared"
    # Gadget code sees its author as the teammate's gadget-scoped ID, not their user ID.
    assert shared["created_by"] == teammate.content(app["id"])["viewer"]["id"]
    with pytest.raises(GadgetError) as error:
        teammate.create_revision(app["id"], {})
    assert error.value.code == "PERMISSION_DENIED"
    gadgets.access.share(app, 2, READ)
    with pytest.raises(GadgetError):
        rpc(teammate, app, "create", id="denied", value={"title": "No"}, operation_id="denied")
    gadgets.access.grants.clear()
    with pytest.raises(NotFound):
        teammate.get(app["id"])


def test_revision_publication_preserves_data_and_requires_latest_publication_version(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    app = publish_board(service)
    card = rpc(service, app, "create", id="kept", value={"title": "Keep"}, operation_id="c-kept")
    revision = make_revision(service, app, CARDS)
    updated_app = service.publish(
        app["id"], {"revision_id": revision["id"], "if_version": app["version"]}
    )
    with pytest.raises(Conflict) as stale:
        rpc(service, app, "get", id="kept")
    assert stale.value.details == {  # type: ignore[attr-defined]
        "reason": "REVISION_CHANGED",
        "published_revision": updated_app["published_revision"],
    }
    assert rpc(service, updated_app, "get", id="kept") == card
    competing = make_revision(service, app, {})
    with pytest.raises(Conflict):
        service.publish(app["id"], {"revision_id": competing["id"], "if_version": app["version"]})


def test_publication_retries_and_rollback_preserve_data_and_schema_identity(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    original = publish_board(service)
    card = rpc(
        service, original, "create", id="kept", value={"title": "Keep"}, operation_id="keep"
    )
    revision = make_revision(service, original, {**CARDS, "notes": {"type": "string"}})
    request = {"revision_id": revision["id"], "if_version": original["version"]}
    published = service.publish(original["id"], request)
    assert service.publish(original["id"], request)["version"] == published["version"]
    rolled_back = service.publish(
        original["id"],
        {"revision_id": original["published_revision"], "if_version": published["version"]},
    )
    assert rpc(service, rolled_back, "get", id="kept") == card
    incompatible = make_revision(service, original, {"notes": {"type": "integer"}})
    with pytest.raises(InvalidInput):
        service.publish(
            original["id"],
            {"revision_id": incompatible["id"], "if_version": rolled_back["version"]},
        )
    assert service.get(original["id"])["published_revision"] == original["published_revision"]


def test_stale_publication_does_not_reserve_a_new_collection_name(gadgets: Fixture) -> None:
    service = gadgets.service()
    original = publish_board(service)
    newer = make_revision(service, original, {})
    current = service.publish(
        original["id"], {"revision_id": newer["id"], "if_version": original["version"]}
    )
    stale = make_revision(service, current, {"extra": {"type": "string"}})
    for expected in (original["version"], None):
        before = gadgets.snapshot()
        with pytest.raises(Conflict):
            service.publish(current["id"], {"revision_id": stale["id"], "if_version": expected})
        assert gadgets.snapshot() == before
    valid = make_revision(service, current, {"extra": {"type": "integer"}})
    published = service.publish(
        current["id"], {"revision_id": valid["id"], "if_version": current["version"]}
    )
    assert published["published_revision"] == valid["id"]


def test_incompatible_schema_does_not_partially_register_earlier_definitions(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    app = publish_board(service)
    invalid = make_revision(
        service, app, {"extra": {"type": "string"}, "cards": {"type": "integer"}}
    )
    before = gadgets.snapshot()
    with pytest.raises(InvalidInput):
        service.publish(app["id"], {"revision_id": invalid["id"], "if_version": app["version"]})
    assert gadgets.snapshot() == before
    valid = make_revision(service, app, {"extra": {"type": "integer"}})
    published = service.publish(
        app["id"], {"revision_id": valid["id"], "if_version": app["version"]}
    )
    assert published["published_revision"] == valid["id"]


@pytest.mark.parametrize(("original_value", "replacement_value"), [(1, True), (0, False)])
def test_schema_identity_distinguishes_boolean_and_number_enums(
    gadgets: Fixture, original_value: int, replacement_value: bool
) -> None:
    service = gadgets.service()
    app = service.create("Synthetic enum schema")
    first = make_revision(service, app, {"values": {"enum": [original_value]}})
    app = service.publish(app["id"], {"revision_id": first["id"], "if_version": None})
    incompatible = make_revision(service, app, {"values": {"enum": [replacement_value]}})
    with pytest.raises(InvalidInput):
        service.publish(
            app["id"], {"revision_id": incompatible["id"], "if_version": app["version"]}
        )
    assert service.get(app["id"])["published_revision"] == first["id"]


def test_publication_retry_after_another_publish_preserves_its_original_catalogue(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    first = publish_board(service)
    revision = make_revision(service, first, {"notes": {"type": "string"}})
    request = {"revision_id": revision["id"], "if_version": first["version"]}
    second = service.publish(first["id"], request)
    third_revision = make_revision(service, second, {"extra": {"type": "integer"}})
    third = service.publish(
        first["id"], {"revision_id": third_revision["id"], "if_version": second["version"]}
    )
    before = gadgets.snapshot()
    assert service.publish(first["id"], request)["version"] == third["version"]
    assert gadgets.snapshot() == before
    incompatible = make_revision(service, third, {"notes": {"type": "integer"}})
    with pytest.raises(InvalidInput):
        service.publish(
            first["id"], {"revision_id": incompatible["id"], "if_version": third["version"]}
        )


def test_losing_publication_does_not_bind_a_schema_during_a_real_cas_race(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    first = publish_board(service)
    loser = make_revision(service, first, {"extra": {"type": "string"}})
    winner = make_revision(service, first, {"notes": {"type": "string"}})

    def publish_winner_first(path: str) -> None:
        if path.startswith("code/state/publication/current/"):
            gadgets.store.before_create = None
            service.publish(
                first["id"], {"revision_id": winner["id"], "if_version": first["version"]}
            )

    # The winner can only slip in between the loser's check and its write on a store
    # without record locks; with them, the second publish waits instead.
    gadgets.store.locks = False
    gadgets.store.before_create = publish_winner_first
    with pytest.raises(Conflict):
        service.publish(first["id"], {"revision_id": loser["id"], "if_version": first["version"]})
    current = service.get(first["id"])
    assert current["published_revision"] == winner["id"]
    valid = make_revision(service, current, {"extra": {"type": "integer"}})
    published = service.publish(
        first["id"], {"revision_id": valid["id"], "if_version": current["version"]}
    )
    assert published["published_revision"] == valid["id"]


def test_data_store_limit_leaves_existing_records_listable_at_capacity(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def capped(path: str, entries: list[str]) -> None:
        if len(entries) > 3:
            raise CapacityExceeded("Synthetic namespace scan limit")

    service = gadgets.service()
    app = publish_board(service)
    gadgets.store.after_list = capped
    monkeypatch.setattr(
        service_module,
        "_DATA_STORE_LIMITS",
        StoreLimits(max_records_per_collection=3, max_list_entries=3),
    )
    for index in range(3):
        rpc(
            service,
            app,
            "create",
            id=f"card{index}",
            value={"title": "S"},
            operation_id=f"c{index}",
        )
    with pytest.raises(CapacityExceeded):
        rpc(service, app, "create", id="overflow", value={"title": "S"}, operation_id="overflow")
    assert len(rpc(service, app, "list", limit=3)["items"]) == 3


class _TwoMinutesLater(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return datetime.now(tz) + timedelta(minutes=2)


# Segments of two revisions: every record below is rewritten far more often than that.
_TINY_SEGMENTS = StoreLimits(max_revisions_per_record=10_000, revisions_per_segment=2)


def test_publishing_and_pinning_many_times_never_run_out_of_revisions(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "_STATE_STORE_LIMITS", _TINY_SEGMENTS)
    service = gadgets.service()
    app = publish_board(service)
    for n in range(5):
        revision = make_revision(service, app, {})
        app = service.publish(
            app["id"], {"revision_id": revision["id"], "if_version": app["version"]}
        )
    assert app["published_revision"] == revision["id"]
    for n in range(5):
        service.pin(app["id"], {"pinned": n % 2 == 0})
    assert [pin["id"] for pin in service.shelf()["pinned"]] == [app["id"]]


def test_deleted_records_free_their_place_once_retention_passes(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service_module,
        "_DATA_STORE_LIMITS",
        StoreLimits(max_records_per_collection=2, max_list_entries=4, tombstone_retention_s=60),
    )
    service = gadgets.service()
    app = publish_board(service)
    first = rpc(service, app, "create", id="card0", value={"title": "S"}, operation_id="c0")
    rpc(service, app, "create", id="card1", value={"title": "S"}, operation_id="c1")
    rpc(service, app, "delete", id="card0", if_version=first["version"], operation_id="d0")
    with pytest.raises(CapacityExceeded):
        rpc(service, app, "create", id="card2", value={"title": "S"}, operation_id="c2")
    monkeypatch.setattr(documents_module, "datetime", _TwoMinutesLater)
    rpc(service, app, "create", id="card2", value={"title": "S"}, operation_id="c2")
    assert [item["id"] for item in rpc(service, app, "list", limit=10)["items"]] == [
        "card1",
        "card2",
    ]


def test_schema_validation_and_session_capability_fail_closed(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = publish_board(service)
    with pytest.raises(InvalidInput):
        rpc(service, app, "create", id="invalid", value={"title": 123}, operation_id="invalid")
    with pytest.raises(InvalidInput):
        rpc(service, app, "get", id="../escape")
    with pytest.raises(GadgetError) as error:
        service.rpc(app["id"], app["published_revision"], "sessions.v1.create", {})
    assert error.value.code == "UNSUPPORTED"
    # A capability no server knows is refused when the revision is uploaded.
    with pytest.raises(InvalidInput):
        service.create_revision(
            app["id"],
            {
                "html": "<main>x</main>",
                "sdk_version": 1,
                "capabilities": ["sessions.create"],
                "collections": CARDS,
            },
        )


def test_new_server_capabilities_do_not_automatically_expand_an_existing_publication(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = gadgets.service()
    # Published while this server could not grant session reads yet.
    monkeypatch.setattr(service_module, "_SUPPORTED_CAPABILITIES", frozenset({"data.read"}))
    app = publish_board(service)
    monkeypatch.setattr(
        service_module,
        "_SUPPORTED_CAPABILITIES",
        frozenset({"data.read", "data.write", "sessions.read"}),
    )
    assert "sessions.read" not in service.content(app["id"])["capabilities"]
    # Approving a new capability needs manage; an editor cannot.
    gadgets.access.share(app, 2, EDIT)
    with pytest.raises(GadgetError):
        gadgets.service(2).publish(
            app["id"], {"revision_id": app["published_revision"], "if_version": app["version"]}
        )
    service.publish(
        app["id"], {"revision_id": app["published_revision"], "if_version": app["version"]}
    )
    assert "sessions.read" in service.content(app["id"])["capabilities"]


def test_external_frontend_changes_are_detected_before_serving(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = publish_board(service)
    path = gadgets.revision_path(app, app["published_revision"])
    changed = json.loads(path.read_text())
    changed["html"] = "<main>Changed externally</main>"
    path.write_text(json.dumps(changed))
    with pytest.raises(CorruptData):
        service.content(app["id"])


def test_external_schema_references_are_rejected_even_for_direct_uploads(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    app = service.create("Schema test")
    revision_id = "a" * 32
    files = gadgets.store.gadget_files(app["id"])
    files.mkdirs("code/revisions")
    files.create_json(
        f"code/revisions/{revision_id}.json",
        {
            "id": revision_id,
            "html": "example",
            "sdk_version": 1,
            "capabilities": ["data.read"],
            "collections": {"cards": {"$ref": "https://example.com/schema"}},
        },
    )
    with pytest.raises(CorruptData):
        service.publish(app["id"], {"revision_id": revision_id, "if_version": None})


# --- Guides and rules across records ----------------------------------------------------------

_RULED_COLLECTIONS = {
    "cards": {
        "type": "object",
        "required": ["title", "column"],
        "properties": {"title": {"type": "string"}, "column": {"type": "string"}},
    },
    "runs": {
        "type": "object",
        "required": ["state"],
        "properties": {
            "cardId": {"type": "string"},
            "reviewCardId": {"type": "string"},
            "state": {"type": "string"},
        },
    },
}
WIP_LIMIT = {
    "name": "wip_limit",
    "kind": "max_records",
    "collection": "cards",
    "where": {"column": "doing"},
    "max": 2,
    "message": "In progress holds at most 2 cards.",
}


def ruled_revision(
    service: GadgetService, app_id: str, constraints: list, guide: str | None = None
) -> str:
    body = {
        "html": "<main>Synthetic ruled board</main>",
        "sdk_version": 1,
        "capabilities": ["data.read", "data.write"],
        "collections": _RULED_COLLECTIONS,
        "constraints": constraints,
    }
    if guide is not None:
        body["guide"] = guide
    return service.create_revision(app_id, body)["id"]


def publish_ruled_board(
    service: GadgetService, constraints: list, guide: str | None = None
) -> dict:
    app = service.create("Synthetic ruled board")
    revision = ruled_revision(service, app["id"], constraints, guide)
    return service.publish(app["id"], {"revision_id": revision, "if_version": None})


def card(service: GadgetService, app: dict, record_id: str, column: str) -> dict:
    return rpc(
        service,
        app,
        "create",
        id=record_id,
        value={"title": record_id, "column": column},
        operation_id=f"c-{record_id}",
    )


def move(
    service: GadgetService, app: dict, document: dict, column: str, operation_id: str
) -> dict:
    return rpc(
        service,
        app,
        "replace",
        id=document["id"],
        value={**document["value"], "column": column},
        if_version=document["version"],
        operation_id=operation_id,
    )


def test_a_record_limit_refuses_only_writes_that_would_exceed_it(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = publish_ruled_board(service, [WIP_LIMIT])
    first = card(service, app, "a", "doing")
    card(service, app, "b", "doing")
    with pytest.raises(ConstraintViolation) as error:
        card(service, app, "c", "doing")
    assert str(error.value) == "In progress holds at most 2 cards."
    assert error.value.details["constraint"] == "wip_limit"
    queued = card(service, app, "c", "todo")
    with pytest.raises(ConstraintViolation):
        move(service, app, queued, "doing", "m-c-1")
    move(gadgets.service(), app, first, "done", "m-a")
    moved = move(service, app, queued, "doing", "m-c-2")
    assert moved["value"]["column"] == "doing"
    # A retried create is answered from the store and does not count the card twice.
    assert card(service, app, "b", "doing")["id"] == "b"
    with pytest.raises(ConstraintViolation):
        card(service, app, "d", "doing")


def test_a_rule_added_later_keeps_data_that_already_breaks_it_editable(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = publish_ruled_board(service, [])
    cards = [card(service, app, name, "doing") for name in ("a", "b", "c")]
    revision = ruled_revision(service, app["id"], [WIP_LIMIT])
    app = service.publish(app["id"], {"revision_id": revision, "if_version": app["version"]})
    with pytest.raises(ConstraintViolation):
        card(service, app, "d", "doing")
    move(service, app, cards[0], "done", "m-a")
    with pytest.raises(ConstraintViolation):
        card(service, app, "e", "doing")
    move(service, app, cards[1], "done", "m-b")
    assert card(service, app, "e", "doing")["id"] == "e"


def test_a_unique_rule_added_over_duplicates_keeps_them_editable_and_the_value_taken(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    app = publish_ruled_board(service, [])
    a = rpc(
        service,
        app,
        "create",
        id="a",
        value={"title": "Ship", "column": "todo"},
        operation_id="c-a",
    )
    b = rpc(
        service,
        app,
        "create",
        id="b",
        value={"title": "Ship", "column": "todo"},
        operation_id="c-b",
    )
    rule = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    revision = ruled_revision(service, app["id"], [rule])
    app = service.publish(app["id"], {"revision_id": revision, "if_version": app["version"]})
    # Both records that already shared the title stay editable while they keep it.
    b = move(service, app, b, "doing", "m-b")
    a = move(service, app, a, "doing", "m-a")
    # Deleting one leaves the title held by the other: a third record still can't take it.
    rpc(service, app, "delete", id="a", if_version=a["version"], operation_id="d-a")
    with pytest.raises(ConstraintViolation):
        rpc(
            service,
            app,
            "create",
            id="c",
            value={"title": "Ship", "column": "todo"},
            operation_id="c-c",
        )
    # Once the last holder moves off it, the title is free.
    renamed = rpc(
        service,
        app,
        "replace",
        id="b",
        value={"title": "Shipped", "column": "doing"},
        if_version=b["version"],
        operation_id="r-b",
    )
    assert renamed["value"]["title"] == "Shipped"
    created = rpc(
        service,
        app,
        "create",
        id="c",
        value={"title": "Ship", "column": "todo"},
        operation_id="c-c2",
    )
    assert created["id"] == "c"


def _nested(depth: int) -> dict:
    """An object *depth* levels deep, counting itself."""
    return {} if depth == 1 else {"n": _nested(depth - 1)}


@pytest.mark.parametrize("depth", [13, 14, 15, 16])
def test_a_ruled_collection_takes_any_record_the_public_limits_accept(
    gadgets: Fixture, depth: int
) -> None:
    # The rules' bookkeeping wraps the record in a few containers of its own; it still has
    # room for a record at the deepest the public limit accepts.
    service = gadgets.service()
    rule = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    app = publish_ruled_board(service, [rule])
    value = {"title": "Ship", "column": "todo", "extra": _nested(depth - 1)}
    created = rpc(service, app, "create", id="a", value=value, operation_id="c-a")
    replaced = rpc(
        service,
        app,
        "replace",
        id="a",
        value={**value, "column": "doing"},
        if_version=created["version"],
        operation_id="r-a",
    )
    assert replaced["value"]["column"] == "doing"


def test_a_ruled_collection_still_refuses_a_record_past_the_public_depth(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    rule = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    app = publish_ruled_board(service, [rule])
    with pytest.raises(InvalidInput):
        rpc(
            service,
            app,
            "create",
            id="a",
            value={"title": "Ship", "column": "todo", "extra": _nested(16)},
            operation_id="c-a",
        )


def _blank(service: GadgetService) -> tuple[dict, str]:
    app = service.create("Synthetic draft")
    return app, ruled_revision(service, app["id"], [])


@pytest.mark.parametrize("built", ["nothing", "published", "adopted"])
def test_a_discard_deletes_only_a_gadget_nothing_was_built_from(
    gadgets: Fixture, built: str
) -> None:
    service = gadgets.service()
    app, revision = _blank(service)
    if built == "published":
        service.publish(app["id"], {"revision_id": revision, "if_version": None})
    elif built == "adopted":
        service.set_gadget_session(app["id"], {"session_id": "s_gadget"})

    result = service.discard_unbuilt(app["id"])

    assert result["deleted"] is (built == "nothing")
    assert (gadgets.store.get(app["id"]) is None) is (built == "nothing")


def test_publishing_waits_out_a_discard_that_is_checking(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another tab publishes while the discard has checked and is deleting: the publish
    # can't land between the two, so it is told the gadget is busy, not silently lost.
    import omnigent.stores.gadget_store.local as local_module

    monkeypatch.setattr(local_module, "_EXCLUSIVE_WAIT_S", 0.05)
    service = gadgets.service()
    app, revision = _blank(service)
    other_tab = gadgets.service()
    real_delete = gadgets.store.delete
    raced: list[BaseException] = []

    def publish_from_the_other_tab() -> None:
        try:
            other_tab.publish(app["id"], {"revision_id": revision, "if_version": None})
        except Conflict as error:
            raced.append(error)

    def delete(gadget_id: str) -> bool:
        # Another request, so another thread, while the discard holds the gadget.
        with ThreadPoolExecutor(1) as pool:
            pool.submit(publish_from_the_other_tab).result()
        return real_delete(gadget_id)

    monkeypatch.setattr(gadgets.store, "delete", delete)
    assert service.discard_unbuilt(app["id"])["deleted"] is True
    assert len(raced) == 1 and raced[0].reason == "busy"


def test_a_unique_rule_refuses_duplicates_and_frees_a_deleted_value(gadgets: Fixture) -> None:
    service = gadgets.service()
    rule = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    app = publish_ruled_board(service, [rule])
    first = rpc(
        service,
        app,
        "create",
        id="a",
        value={"title": "Ship", "column": "todo"},
        operation_id="c-a",
    )
    with pytest.raises(ConstraintViolation) as error:
        rpc(
            service,
            app,
            "create",
            id="b",
            value={"title": "Ship", "column": "doing"},
            operation_id="c-b",
        )
    assert str(error.value) == "Another cards record already has the same title"
    kept = move(service, app, first, "doing", "m-a")
    rpc(service, app, "delete", id="a", if_version=kept["version"], operation_id="d-a")
    assert rpc(
        service,
        app,
        "create",
        id="b",
        value={"title": "Ship", "column": "doing"},
        operation_id="c-b2",
    )


def test_rule_state_is_bounded_by_its_bytes_not_a_record_count(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The guard holds every unique key with its record's ID, plus the write in flight, in one
    # record: what fits depends on IDs and values, so a write can be refused below max_records.
    monkeypatch.setattr(constraints_module, "MAX_GUARD_BYTES", 2048)
    service = gadgets.service()
    rule = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    app = publish_ruled_board(service, [rule])

    # One write whose own record is too large for the guard is refused as capacity.
    with pytest.raises(CapacityExceeded, match="too large"):
        rpc(
            service,
            app,
            "create",
            id="big",
            value={"title": "x" * 2048, "column": "todo"},
            operation_id="c-big",
        )

    created: list[dict] = []
    with pytest.raises(CapacityExceeded):
        for index in range(1000):
            created.append(
                rpc(
                    service,
                    app,
                    "create",
                    id=f"card-{index:04d}-{'i' * 24}",
                    value={"title": f"T{index}", "column": "todo"},
                    operation_id=f"c-{index}",
                )
            )
    # Well under the store's record limit, and what was written stays readable.
    assert 0 < len(created) < 100
    assert len(rpc(service, app, "list", limit=100)["items"]) == len(created)
    # Deleting a record frees its key's bytes for the next write.
    rpc(
        service,
        app,
        "delete",
        id=created[0]["id"],
        if_version=created[0]["version"],
        operation_id="d-0",
    )
    assert rpc(
        service,
        app,
        "create",
        id="after",
        value={"title": "Again", "column": "todo"},
        operation_id="c-after",
    )


_ITEMS_WITH_PARENT = {"type": "object", "properties": {"parent": {"type": "string"}}}


def _publish_any_board(
    service: GadgetService, constraints: list, items: dict | None = None
) -> dict:
    """A board whose ``parents`` (and ``items``, unless given) take any JSON value."""
    app = service.create("Synthetic open board")
    revision = service.create_revision(
        app["id"],
        {
            "html": "<main>Synthetic open board</main>",
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write"],
            "collections": {"parents": {}, "items": items if items is not None else {}},
            "constraints": constraints,
        },
    )["id"]
    return service.publish(app["id"], {"revision_id": revision, "if_version": None})


def _put(service: GadgetService, app: dict, collection: str, record_id: str, value, op: str):  # type: ignore[no-untyped-def]
    return rpc(
        service, app, "create", collection=collection, id=record_id, value=value, operation_id=op
    )


@pytest.mark.parametrize("on_delete", ["restrict", "cascade", "unset"])
def test_a_reference_to_a_record_whose_value_is_null_follows_its_delete_rule(
    gadgets: Fixture, on_delete: str
) -> None:
    # A record whose value is null is live: replacing a parent's value with null is no
    # delete, and deleting a parent whose value is null still applies the rule.
    service = gadgets.service()
    rule = {
        "name": "item_parent",
        "kind": "reference",
        "collection": "items",
        "field": "parent",
        "target": "parents",
        "on_delete": on_delete,
    }
    app = _publish_any_board(service, [rule], _ITEMS_WITH_PARENT)
    parent = _put(service, app, "parents", "p", {"name": "P"}, "p")
    _put(service, app, "items", "i", {"parent": "p", "n": 1}, "i")

    nulled = rpc(
        service,
        app,
        "replace",
        collection="parents",
        id="p",
        value=None,
        if_version=parent["version"],
        operation_id="p-null",
    )
    assert nulled["value"] is None
    assert rpc(service, app, "get", collection="items", id="i")["value"] == {"parent": "p", "n": 1}

    if on_delete == "restrict":
        with pytest.raises(ConstraintViolation):
            rpc(
                service,
                app,
                "delete",
                collection="parents",
                id="p",
                if_version=nulled["version"],
                operation_id="p-del",
            )
        return
    rpc(
        service,
        app,
        "delete",
        collection="parents",
        id="p",
        if_version=nulled["version"],
        operation_id="p-del",
    )
    left = rpc(service, app, "get", collection="items", id="i")
    if on_delete == "cascade":
        assert left is None
    else:
        assert left["value"] == {"n": 1}


_NUMBERED = {"type": "object", "properties": {"n": {}}}


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [(1, 1.0, True), (0.0, -0.0, True), (0, -0.0, True), (1, True, False), (0, False, False)],
)
def test_rules_take_equal_json_numbers_as_one_value(
    gadgets: Fixture, first, second, same: bool
) -> None:
    service = gadgets.service()
    unique = {"name": "one_n", "kind": "unique", "collection": "items", "fields": ["n"]}
    app = _publish_any_board(service, [unique], _NUMBERED)
    _put(service, app, "items", "a", {"n": first}, "a")
    if same:
        with pytest.raises(ConstraintViolation):
            _put(service, app, "items", "b", {"n": second}, "b")
    else:
        assert _put(service, app, "items", "b", {"n": second}, "b")["id"] == "b"


def test_a_record_limit_filter_matches_equal_json_numbers(gadgets: Fixture) -> None:
    service = gadgets.service()
    limit = {
        "name": "one",
        "kind": "max_records",
        "collection": "items",
        "max": 1,
        "where": {"n": 1},
    }
    app = _publish_any_board(service, [limit], _NUMBERED)
    _put(service, app, "items", "a", {"n": 1}, "a")
    with pytest.raises(ConstraintViolation):
        _put(service, app, "items", "b", {"n": 1.0}, "b")
    # A boolean is not the number one.
    assert _put(service, app, "items", "c", {"n": True}, "c")["id"] == "c"


def test_an_unfiltered_record_limit_counts_every_value(gadgets: Fixture) -> None:
    service = gadgets.service()
    limit = {"name": "two", "kind": "max_records", "collection": "items", "max": 2}
    app = _publish_any_board(service, [limit])
    seven = _put(service, app, "items", "a", 7, "a")
    _put(service, app, "items", "b", None, "b")
    with pytest.raises(ConstraintViolation):
        _put(service, app, "items", "c", [], "c")
    # Replacing a record with a scalar frees no place; deleting one does.
    rpc(
        service,
        app,
        "replace",
        collection="items",
        id="a",
        value={"x": 1},
        if_version=seven["version"],
        operation_id="a-obj",
    )
    with pytest.raises(ConstraintViolation):
        _put(service, app, "items", "c", "text", "c2")
    current = rpc(service, app, "get", collection="items", id="a")
    rpc(
        service,
        app,
        "delete",
        collection="items",
        id="a",
        if_version=current["version"],
        operation_id="a-del",
    )
    assert _put(service, app, "items", "c", "text", "c3")["id"] == "c"
    # Published again, the rules recount the same way.
    revision = service.create_revision(
        app["id"],
        {
            "html": "<main>Synthetic open board</main>",
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write"],
            "collections": {"parents": {}, "items": {}},
            "constraints": [{**limit, "message": "Two at most."}],
        },
    )["id"]
    app = _republish(service, app, revision)
    with pytest.raises(ConstraintViolation):
        _put(service, app, "items", "d", 0, "d")


@pytest.mark.parametrize("on_delete", ["restrict", "cascade", "unset"])
def test_a_reference_rule_checks_targets_and_applies_its_delete_rule(
    gadgets: Fixture, on_delete: str
) -> None:
    service = gadgets.service()
    rule = {
        "name": "run_card",
        "kind": "reference",
        "collection": "runs",
        "field": "cardId",
        "target": "cards",
        "on_delete": on_delete,
    }
    app = publish_ruled_board(service, [rule])

    def run(record_id: str, value: dict) -> dict:
        return rpc(
            service,
            app,
            "create",
            collection="runs",
            id=record_id,
            value=value,
            operation_id=record_id,
        )

    with pytest.raises(ConstraintViolation):
        run("r0", {"cardId": "missing", "state": "done"})
    target = card(service, app, "a", "doing")
    run("r1", {"cardId": "a", "state": "done"})
    run("r2", {"state": "done"})
    if on_delete == "restrict":
        with pytest.raises(ConstraintViolation):
            rpc(service, app, "delete", id="a", if_version=target["version"], operation_id="d-a")
        assert rpc(service, app, "get", id="a") is not None
        return
    rpc(service, app, "delete", id="a", if_version=target["version"], operation_id="d-a")
    remaining = rpc(service, app, "get", collection="runs", id="r1")
    if on_delete == "cascade":
        assert remaining is None
    else:
        assert remaining["value"] == {"state": "done"}
    assert rpc(service, app, "get", collection="runs", id="r2")["value"] == {"state": "done"}


_UNSET_RUN_CARD = {
    "name": "run_card",
    "kind": "reference",
    "collection": "runs",
    "field": "cardId",
    "target": "cards",
    "on_delete": "unset",
}


def test_a_rule_never_leaves_a_dependent_that_breaks_its_schema(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = service.create("Synthetic ruled board")
    collections = {
        **_RULED_COLLECTIONS,
        "notes": {
            "type": "object",
            "required": ["label"],
            "minProperties": 2,
            "properties": {"label": {"type": "string"}, "card": {"type": "string"}},
        },
    }
    revision = service.create_revision(
        app["id"],
        {
            "html": "<main>Synthetic ruled board</main>",
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write"],
            "collections": collections,
            "constraints": [{**_UNSET_RUN_CARD, "collection": "notes", "field": "card"}],
        },
    )["id"]
    app = service.publish(app["id"], {"revision_id": revision, "if_version": None})
    target = card(service, app, "a", "todo")
    rpc(
        service,
        app,
        "create",
        collection="notes",
        id="n1",
        value={"label": "b", "card": "a"},
        operation_id="n1",
    )
    # Unsetting `card` would leave {"label": "b"}, which minProperties refuses.
    with pytest.raises(InvalidInput, match="notes record n1"):
        rpc(service, app, "delete", id="a", if_version=target["version"], operation_id="d-a")
    assert rpc(service, app, "get", id="a") is not None
    assert rpc(service, app, "get", collection="notes", id="n1")["value"] == {
        "label": "b",
        "card": "a",
    }


def _fail_dependent_update_once(monkeypatch: pytest.MonkeyPatch, error: Exception) -> list[str]:
    """Make the first update of a ``runs`` record fail, after the change's own write."""
    real_replace = documents_module.ImmutableDocumentStore.replace
    failed: list[str] = []

    def replace(self, collection, record_id, *args, **kwargs):
        if collection == "runs" and not failed:
            failed.append(record_id)
            raise error
        return real_replace(self, collection, record_id, *args, **kwargs)

    monkeypatch.setattr(documents_module.ImmutableDocumentStore, "replace", replace)
    return failed


def test_a_rule_update_that_fails_after_the_change_is_finished_later(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = gadgets.service()
    app = publish_ruled_board(service, [_UNSET_RUN_CARD])
    target = card(service, app, "a", "todo")
    rpc(
        service,
        app,
        "create",
        collection="runs",
        id="r1",
        value={"cardId": "a", "state": "done"},
        operation_id="r1",
    )
    failed = _fail_dependent_update_once(monkeypatch, CapacityExceeded("full"))
    # The card is deleted, but the run still names it: the change says it isn't finished.
    with pytest.raises(constraints_module.IncompleteChange):
        rpc(service, app, "delete", id="a", if_version=target["version"], operation_id="d-a")
    assert failed == ["r1"]
    assert rpc(service, app, "get", id="a") is None
    assert rpc(service, app, "get", collection="runs", id="r1")["value"]["cardId"] == "a"
    # Retrying the same delete finishes it, without deleting anything twice.
    rpc(service, app, "delete", id="a", if_version=target["version"], operation_id="d-a")
    assert rpc(service, app, "get", collection="runs", id="r1")["value"] == {"state": "done"}


def test_a_dependent_changed_after_the_plan_is_updated_from_its_current_version(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = gadgets.service()
    app = publish_ruled_board(service, [_UNSET_RUN_CARD])
    target = card(service, app, "a", "todo")
    run = rpc(
        service,
        app,
        "create",
        collection="runs",
        id="r1",
        value={"cardId": "a", "state": "done"},
        operation_id="r1",
    )
    real_replace = documents_module.ImmutableDocumentStore.replace
    raced: list[str] = []

    def replace(self, collection, record_id, value, if_version, *args, **kwargs):
        if collection == "runs" and not raced:
            # Someone else updates the run between the plan and the rule's update of it.
            raced.append(record_id)
            real_replace(
                self,
                "runs",
                "r1",
                {"cardId": "a", "state": "again"},
                run["version"],
                "r1-again",
                "x",
            )
        return real_replace(self, collection, record_id, value, if_version, *args, **kwargs)

    monkeypatch.setattr(documents_module.ImmutableDocumentStore, "replace", replace)
    rpc(service, app, "delete", id="a", if_version=target["version"], operation_id="d-a")
    assert raced == ["r1"]
    # Unset from the current version, keeping the other change.
    assert rpc(service, app, "get", collection="runs", id="r1")["value"] == {"state": "again"}


_RESTRICT_RUN_CARD = {**_UNSET_RUN_CARD, "on_delete": "restrict"}


def _run(service: GadgetService, app: dict, record_id: str) -> dict:
    return rpc(service, app, "get", collection="runs", id=record_id)


def _point_run(service: GadgetService, app: dict, record_id: str, card_id: str, op: str) -> dict:
    current = _run(service, app, record_id)
    return rpc(
        service,
        app,
        "replace",
        collection="runs",
        id=record_id,
        value={"cardId": card_id, "state": "done"},
        if_version=current["version"],
        operation_id=op,
    )


def test_pruning_a_stale_index_entry_never_drops_one_its_record_needs_again(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A delete finds an index entry whose run has moved to another card and drops it. If the
    # run moves back between that check and the drop, the entry is needed again: dropping it
    # would let a restricted delete through while the run still names the card.
    service = gadgets.service()
    app = publish_ruled_board(service, [_RESTRICT_RUN_CARD])
    target = card(service, app, "x", "todo")
    card(service, app, "y", "todo")
    rpc(
        service,
        app,
        "create",
        collection="runs",
        id="d",
        value={"cardId": "x", "state": "done"},
        operation_id="d",
    )
    # Moved to y by a store that can't delete: its entry under x stays behind, stale.
    with monkeypatch.context() as patch:
        patch.setattr(HookedFiles, "supports_delete", property(lambda self: False))
        _point_run(service, app, "d", "y", "d-to-y")

    real_get = documents_module.ImmutableDocumentStore.get
    moved: list[str] = []

    def get(self, collection, record_id):
        found = real_get(self, collection, record_id)
        if collection == "runs" and record_id == "d" and not moved and found is not None:
            # Another writer points the run back at x right after the delete read it.
            moved.append(record_id)
            _point_run(gadgets.service(), app, "d", "x", "d-back")
        return found

    monkeypatch.setattr(documents_module.ImmutableDocumentStore, "get", get)
    with pytest.raises(ConstraintViolation):
        rpc(service, app, "delete", id="x", if_version=target["version"], operation_id="d-x")
    assert moved == ["d"]
    monkeypatch.setattr(documents_module.ImmutableDocumentStore, "get", real_get)
    assert rpc(service, app, "get", id="x") is not None
    assert _run(service, app, "d")["value"]["cardId"] == "x"


def test_a_create_racing_a_delete_of_its_record_never_lands_a_duplicate(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A create of an existing ID finds the record, then another writer deletes it before the
    # create acts on what it found. The create must not then land over the tombstone outside
    # the rules: x would take the title y holds.
    service = gadgets.service()
    rule = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    app = publish_ruled_board(service, [rule])
    rpc(
        service,
        app,
        "create",
        id="y",
        value={"title": "dup", "column": "todo"},
        operation_id="c-y",
    )
    x = rpc(
        service,
        app,
        "create",
        id="x",
        value={"title": "other", "column": "todo"},
        operation_id="c-x",
    )
    real_get = documents_module.ImmutableDocumentStore.get
    deleted: list[str] = []

    def get(self, collection, record_id):
        found = real_get(self, collection, record_id)
        if collection == "cards" and record_id == "x" and not deleted:
            deleted.append(record_id)
            rpc(
                gadgets.service(),
                app,
                "delete",
                id="x",
                if_version=x["version"],
                operation_id="d-x",
            )
        return found

    monkeypatch.setattr(documents_module.ImmutableDocumentStore, "get", get)
    with contextlib.suppress(Conflict, ConstraintViolation):
        rpc(
            service,
            app,
            "create",
            id="x",
            value={"title": "dup", "column": "todo"},
            operation_id="c-x2",
        )
    monkeypatch.setattr(documents_module.ImmutableDocumentStore, "get", real_get)
    # Either x is gone, or it was created through the rules; "dup" stays y's alone.
    current = rpc(service, app, "get", id="x")
    assert current is None or current["value"]["title"] != "dup"
    with pytest.raises(ConstraintViolation):
        rpc(
            service,
            app,
            "create",
            id="z",
            value={"title": "dup", "column": "todo"},
            operation_id="c-z",
        )


@pytest.mark.parametrize("rule_kind", ["max_records", "unique"])
def test_retrying_a_create_whose_record_was_deleted_changes_no_rule_state(
    gadgets: Fixture, rule_kind: str
) -> None:
    # The retry gets its first receipt and creates nothing, so the rule state mustn't count
    # it: the collection is empty and a fresh create still fits.
    service = gadgets.service()
    rule = (
        {"name": "one", "kind": "max_records", "collection": "cards", "max": 1}
        if rule_kind == "max_records"
        else {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    )
    app = publish_ruled_board(service, [rule])
    value = {"title": "Ship", "column": "todo"}
    first = rpc(service, app, "create", id="x", value=value, operation_id="op1")
    rpc(service, app, "delete", id="x", if_version=first["version"], operation_id="d-x")
    again = rpc(service, app, "create", id="x", value=value, operation_id="op1")
    assert again["version"] == first["version"]
    assert rpc(service, app, "get", id="x") is None
    fresh = rpc(service, app, "create", id="z", value=value, operation_id="op2")
    assert fresh["id"] == "z"


def test_a_late_index_removal_from_a_finished_change_keeps_a_newer_entry(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A writer moves a run from x to y and stops before dropping its entry under x. Another
    # writer finishes that change, and the run then moves back to x. When the first writer
    # resumes and drops the entry it planned to, the run needs that entry again.
    import threading

    service = gadgets.service()
    app = publish_ruled_board(service, [_RESTRICT_RUN_CARD])
    target = card(service, app, "x", "todo")
    card(service, app, "y", "todo")
    rpc(
        service,
        app,
        "create",
        collection="runs",
        id="d",
        value={"cardId": "x", "state": "done"},
        operation_id="d",
    )
    real_apply = ConstraintGuard._apply
    paused = threading.Event()
    resume = threading.Event()
    late = threading.current_thread()

    def apply(self, operation):
        if (
            operation["kind"] == "ref_remove"
            and threading.current_thread() is not late
            and not paused.is_set()
        ):
            paused.set()
            assert resume.wait(10)
        return real_apply(self, operation)

    monkeypatch.setattr(ConstraintGuard, "_apply", apply)
    late_writer = gadgets.service()
    with ThreadPoolExecutor(1) as pool:
        stopped = pool.submit(_point_run, late_writer, app, "d", "y", "d-to-y")
        assert paused.wait(10)
        # Another writer finishes the change, then points the run back at x.
        card(service, app, "z", "todo")
        _point_run(service, app, "d", "x", "d-back")
        resume.set()
        stopped.result(10)
    with pytest.raises(ConstraintViolation):
        rpc(service, app, "delete", id="x", if_version=target["version"], operation_id="d-x")
    assert _run(service, app, "d")["value"]["cardId"] == "x"


@pytest.mark.parametrize("on_delete", ["cascade", "unset"])
def test_a_late_rule_update_leaves_a_dependent_created_again_alone(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch, on_delete: str
) -> None:
    # A writer deletes a card and stops before its rule updates the run that names it.
    # Another writer finishes that change; the card and the run are then created again under
    # the same IDs and edited past the store's replay window. When the first writer resumes,
    # its stale update must not touch the new run, which is not the record its plan saw.
    import threading

    service = gadgets.service()
    app = publish_ruled_board(service, [{**_UNSET_RUN_CARD, "on_delete": on_delete}])
    target = card(service, app, "x", "todo")
    rpc(
        service,
        app,
        "create",
        collection="runs",
        id="d",
        value={"cardId": "x", "state": "done"},
        operation_id="d",
    )
    real_apply = ConstraintGuard._apply
    paused = threading.Event()
    resume = threading.Event()
    late = threading.current_thread()

    def apply(self, operation):
        if (
            operation["kind"] in ("delete", "unset")
            and operation["collection"] == "runs"
            and threading.current_thread() is not late
            and not paused.is_set()
        ):
            paused.set()
            assert resume.wait(10)
        return real_apply(self, operation)

    monkeypatch.setattr(ConstraintGuard, "_apply", apply)
    late_writer = gadgets.service()
    with ThreadPoolExecutor(1) as pool:
        stopped = pool.submit(
            rpc,
            late_writer,
            app,
            "delete",
            id="x",
            if_version=target["version"],
            operation_id="x-1",
        )
        assert paused.wait(10)
        # Another writer finishes the change (its own write helps finish the pending one).
        card(service, app, "z", "todo")
        if on_delete == "unset":
            gone = _run(service, app, "d")
            rpc(
                service,
                app,
                "delete",
                collection="runs",
                id="d",
                if_version=gone["version"],
                operation_id="d-gone",
            )
        rpc(
            service,
            app,
            "create",
            id="x",
            value={"title": "x again", "column": "todo"},
            operation_id="x-again",
        )
        rpc(
            service,
            app,
            "create",
            collection="runs",
            id="d",
            value={"cardId": "x", "state": "done"},
            operation_id="d-again",
        )
        # Past the replay window, so the stale update is not answered as a repeat.
        for edit in range(18):
            current = _run(service, app, "d")
            rpc(
                service,
                app,
                "replace",
                collection="runs",
                id="d",
                value={"cardId": "x", "state": f"done-{edit}"},
                if_version=current["version"],
                operation_id=f"d-edit-{edit}",
            )
        resume.set()
        # The late worker finishes without an error: it leaves the record alone.
        stopped.result(10)
    after = _run(service, app, "d")
    assert after is not None
    assert after["value"]["cardId"] == "x"
    assert after["value"]["state"] == "done-17"


@pytest.mark.parametrize("on_delete", ["unset", "cascade"])
def test_deleting_a_record_whose_dependent_is_full_is_refused_before_anything_changes(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch, on_delete: str
) -> None:
    # The run naming the card has used every revision it may have: its rule update could
    # never land, so the delete is refused up front instead of waiting on it for good.
    limits = service_module._DATA_STORE_LIMITS
    monkeypatch.setattr(
        service_module,
        "_DATA_STORE_LIMITS",
        StoreLimits(**{**vars(limits), "max_revisions_per_record": 6, "revisions_per_segment": 3}),
    )
    service = gadgets.service()
    app = publish_ruled_board(service, [{**_UNSET_RUN_CARD, "on_delete": on_delete}])
    target = card(service, app, "x", "todo")
    rpc(
        service,
        app,
        "create",
        collection="runs",
        id="d",
        value={"cardId": "x", "state": "s0"},
        operation_id="d",
    )
    for edit in range(5):
        current = _run(service, app, "d")
        rpc(
            service,
            app,
            "replace",
            collection="runs",
            id="d",
            value={"cardId": "x", "state": f"s{edit + 1}"},
            if_version=current["version"],
            operation_id=f"d-{edit}",
        )

    with pytest.raises(CapacityExceeded):
        rpc(service, app, "delete", id="x", if_version=target["version"], operation_id="x-del")

    assert rpc(service, app, "get", id="x") is not None
    assert _run(service, app, "d")["value"]["cardId"] == "x"
    # Nothing is left pending: other ruled writes go on.
    assert card(service, app, "y", "todo")["id"] == "y"


def test_a_late_unset_checks_the_collection_its_deleted_record_was_in(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two rules on runs.cardId target cards and runs, and both hold a record "a". Deleting
    # cards/a must unset the run's reference even though runs/a is still there.
    import threading

    service = gadgets.service()
    to_runs = {**_UNSET_RUN_CARD, "name": "run_run", "target": "runs"}
    app = publish_ruled_board(service, [_UNSET_RUN_CARD, to_runs])
    target = card(service, app, "a", "todo")
    rpc(
        service,
        app,
        "create",
        collection="runs",
        id="a",
        value={"state": "x"},
        operation_id="run-a",
    )
    rpc(
        service,
        app,
        "create",
        collection="runs",
        id="d",
        value={"cardId": "a", "state": "done"},
        operation_id="d",
    )
    real_apply = ConstraintGuard._apply
    paused = threading.Event()
    resume = threading.Event()
    late = threading.current_thread()

    def apply(self, operation):
        if (
            operation["kind"] == "unset"
            and operation["id"] == "d"
            and threading.current_thread() is not late
            and not paused.is_set()
        ):
            paused.set()
            assert resume.wait(10)
        return real_apply(self, operation)

    monkeypatch.setattr(ConstraintGuard, "_apply", apply)
    gadget = gadgets.store.get(app["id"])
    store = documents_module.ImmutableDocumentStore(
        service._files(gadget), "data", limits=service_module._DATA_STORE_LIMITS
    )
    with ThreadPoolExecutor(1) as pool:
        stopped = pool.submit(
            rpc,
            gadgets.service(),
            app,
            "delete",
            id="a",
            if_version=target["version"],
            operation_id="a-del",
        )
        assert paused.wait(10)
        # Requests checked before the delete edit another field of the run, past the
        # store's replay window, still naming "a".
        for edit in range(18):
            current = store.get("runs", "d")
            store.replace(
                "runs",
                "d",
                {"cardId": "a", "state": f"s{edit}"},
                current.version,
                f"edit-{edit}",
                service.viewer.user_id,
            )
        resume.set()
        stopped.result(10)
    after = _run(service, app, "d")
    assert "cardId" not in after["value"]


def test_a_late_unset_leaves_a_reference_restored_to_a_card_created_again(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Like the test above, but the run is kept: another writer finishes the unset, the card
    # is created again, and the same run is pointed back at it and edited past the replay
    # window. The resumed unset must leave that valid reference alone.
    import threading

    service = gadgets.service()
    app = publish_ruled_board(service, [_UNSET_RUN_CARD])
    target = card(service, app, "x", "todo")
    rpc(
        service,
        app,
        "create",
        collection="runs",
        id="d",
        value={"cardId": "x", "state": "done"},
        operation_id="d",
    )
    real_apply = ConstraintGuard._apply
    paused = threading.Event()
    resume = threading.Event()
    late = threading.current_thread()

    def apply(self, operation):
        if (
            operation["kind"] == "unset"
            and operation["collection"] == "runs"
            and threading.current_thread() is not late
            and not paused.is_set()
        ):
            paused.set()
            assert resume.wait(10)
        return real_apply(self, operation)

    monkeypatch.setattr(ConstraintGuard, "_apply", apply)
    late_writer = gadgets.service()
    with ThreadPoolExecutor(1) as pool:
        stopped = pool.submit(
            rpc,
            late_writer,
            app,
            "delete",
            id="x",
            if_version=target["version"],
            operation_id="x-1",
        )
        assert paused.wait(10)
        card(service, app, "z", "todo")  # Finishes the pending change: "d" loses cardId.
        assert "cardId" not in _run(service, app, "d")["value"]
        rpc(
            service,
            app,
            "create",
            id="x",
            value={"title": "x again", "column": "todo"},
            operation_id="x-again",
        )
        for edit in range(18):
            current = _run(service, app, "d")
            rpc(
                service,
                app,
                "replace",
                collection="runs",
                id="d",
                value={"cardId": "x", "state": f"new-{edit}"},
                if_version=current["version"],
                operation_id=f"d-edit-{edit}",
            )
        resume.set()
        stopped.result(10)
    after = _run(service, app, "d")
    assert after["value"] == {"cardId": "x", "state": "new-17"}


@pytest.mark.parametrize("gone", ["deleted", "version_reclaimed"])
def test_a_dependent_removed_while_its_rule_update_waited_counts_as_done(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch, gone: str
) -> None:
    # An unset is planned and paused; a revision without the rule is published, and the run is
    # deleted straight through the records. Finishing the change finds the run gone: that is
    # its work done, not a failure that keeps the gadget busy.
    service = gadgets.service()
    app = publish_ruled_board(service, [_UNSET_RUN_CARD])
    ruled = app["published_revision"]
    target = card(service, app, "a", "todo")
    run = rpc(
        service,
        app,
        "create",
        collection="runs",
        id="r1",
        value={"cardId": "a", "state": "done"},
        operation_id="r1",
    )
    original_run = ConstraintGuard._run

    def stop_after_recording(self, epoch, started):
        raise RuntimeError("writer stopped")

    monkeypatch.setattr(ConstraintGuard, "_run", stop_after_recording)
    with pytest.raises(RuntimeError):
        rpc(service, app, "delete", id="a", if_version=target["version"], operation_id="d-a")
    monkeypatch.setattr(ConstraintGuard, "_run", original_run)

    app = _republish(service, app, ruled_revision(service, app["id"], []))
    rpc(
        service,
        app,
        "delete",
        collection="runs",
        id="r1",
        if_version=run["version"],
        operation_id="d-r1",
    )
    if gone == "version_reclaimed":
        real_get_version = documents_module.ImmutableDocumentStore.get_version

        def get_version(self, collection, record_id, version):
            if collection == "runs":
                raise InvalidInput("The requested version does not exist")
            return real_get_version(self, collection, record_id, version)

        monkeypatch.setattr(documents_module.ImmutableDocumentStore, "get_version", get_version)

    app = _republish(service, app, ruled)
    # The next guarded write finishes the change and goes through.
    assert card(gadgets.service(), app, "b", "todo")
    assert rpc(service, app, "get", id="a") is None
    assert _run(service, app, "r1") is None


def test_a_write_whose_last_mark_failed_still_counts_once_its_reservation_expires(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unguarded write reserves, then rules are published and a guarded write would
    # rebuild before the write lands; the write's mark after it fails. No rebuild commits
    # while the reservation lives, and the one after it counts the landed record.
    import time as real_time

    class Clock:
        now = real_time.time()

        def time(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            pass

    clock = Clock()
    monkeypatch.setattr(constraints_module, "time", clock)
    service = gadgets.service()
    limit = {"name": "one", "kind": "max_records", "collection": "cards", "max": 1}
    app = publish_ruled_board(service, [limit])
    first = card(service, app, "first", "todo")  # Gives the rules their state.
    rpc(service, app, "delete", id="first", if_version=first["version"], operation_id="d-first")
    app = _republish(service, app, ruled_revision(service, app["id"], []))
    gadget = gadgets.store.get(app["id"])
    files = service._files(gadget)
    store = documents_module.ImmutableDocumentStore(
        files, "data", limits=service_module._DATA_STORE_LIMITS
    )
    marker = ConstraintGuard(files, "data/_guard", store, [], service.viewer.user_id)
    marker.mark_stale(unguarded_write=True, reserve="late-write")
    app = _republish(service, app, ruled_revision(service, app["id"], [limit]))
    with pytest.raises(Conflict):  # The rebuild can't commit past the reservation.
        card(service, app, "a", "todo")
    # The unguarded write lands; its release mark never happens.
    store.create("cards", "late", {"title": "late", "column": "todo"}, "late", "x")

    clock.now += constraints_module.UNGUARDED_HOLD_S + 1
    with pytest.raises(ConstraintViolation):
        card(service, app, "b", "todo")  # "late" fills the one place.


def test_marking_the_rule_state_stale_never_fails_a_write_that_landed(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = gadgets.service()
    app = publish_ruled_board(service, [_UNSET_RUN_CARD])
    app = _republish(service, app, ruled_revision(service, app["id"], []))
    real_mark = ConstraintGuard.mark_stale
    calls: list[int] = []

    def mark_stale(self, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise Conflict("The gadget's rule state is busy; try again", reason="busy")
        return real_mark(self, **kwargs)

    monkeypatch.setattr(ConstraintGuard, "mark_stale", mark_stale)
    # Marked before the write and again after it: the second mark failing leaves the write.
    assert card(service, app, "a", "todo")
    assert rpc(service, app, "get", id="a") is not None

    calls.clear()
    calls.append(1)  # The next mark to run is the one before the write; make it fail.
    with pytest.raises(Conflict):
        card(service, app, "b", "todo")
    # Refused before anything was written.
    monkeypatch.setattr(ConstraintGuard, "mark_stale", real_mark)
    assert rpc(service, app, "get", id="b") is None


def test_a_rule_update_that_fails_when_resumed_stays_pending_for_the_next_writer(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = gadgets.service()
    app = publish_ruled_board(service, [_UNSET_RUN_CARD])
    target = card(service, app, "a", "todo")
    rpc(
        service,
        app,
        "create",
        collection="runs",
        id="r1",
        value={"cardId": "a", "state": "done"},
        operation_id="r1",
    )
    original_run = ConstraintGuard._run

    def stop_after_recording(self, epoch, started):
        raise RuntimeError("writer stopped")

    monkeypatch.setattr(ConstraintGuard, "_run", stop_after_recording)
    with pytest.raises(RuntimeError):
        rpc(service, app, "delete", id="a", if_version=target["version"], operation_id="d-a")
    monkeypatch.setattr(ConstraintGuard, "_run", original_run)
    # The next writer resumes the delete; its update of the run fails once. The plan stays
    # pending rather than being dropped, so the writer's next try finishes it.
    failed = _fail_dependent_update_once(monkeypatch, CapacityExceeded("full"))
    card(gadgets.service(), app, "b", "todo")
    assert failed == ["r1"]
    assert rpc(service, app, "get", id="a") is None
    assert rpc(service, app, "get", id="b") is not None
    assert rpc(service, app, "get", collection="runs", id="r1")["value"] == {"state": "done"}


def _republish(service: GadgetService, app: dict, revision_id: str) -> dict:
    return service.publish(app["id"], {"revision_id": revision_id, "if_version": app["version"]})


def _old_revision_replace(
    service: GadgetService, gadgets: Fixture, app: dict, rules: list, record: dict, title: str
) -> None:
    """Replace *record* as a request validated under an older revision with *rules* (none on
    its collection) does: straight to the records, marking the rule state stale around it."""
    gadget = gadgets.store.get(app["id"])
    files = service._files(gadget)
    store = documents_module.ImmutableDocumentStore(
        files, "data", limits=service_module._DATA_STORE_LIMITS
    )
    actor = service.viewer.user_id
    marker = ConstraintGuard(files, "data/_guard", store, rules, actor)
    marker.mark_stale(unguarded_write=True)
    store.replace(
        "cards",
        record["id"],
        {"title": title, "column": "todo"},
        record["version"],
        f"old-{title}",
        actor,
    )
    marker.mark_stale(unguarded_write=True)


_FEW_RUNS = {"name": "few_runs", "kind": "max_records", "collection": "runs", "max": 100}


def test_marking_stale_never_changes_a_sealed_epoch(gadgets: Fixture) -> None:
    # A guarded writer that read the sealed epoch makes its successor from it as sealed, so
    # a mark written into the sealed epoch would be lost: the mark goes to the successor.
    service = gadgets.service()
    unique = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    app = publish_ruled_board(service, [unique])
    card(service, app, "a", "todo")
    gadget = gadgets.store.get(app["id"])
    files = service._files(gadget)
    store = documents_module.ImmutableDocumentStore(
        files, "data", limits=service_module._DATA_STORE_LIMITS
    )
    guard = ConstraintGuard(files, "data/_guard", store, [unique], service.viewer.user_id)
    epoch, state = guard._current()
    guard._seal(epoch, state)
    sealed = guard.guard.get("epochs", epoch)

    ConstraintGuard(files, "data/_guard", store, [], service.viewer.user_id).mark_stale(
        unguarded_write=True
    )

    assert guard.guard.get("epochs", epoch).version == sealed.version
    newest, current = guard._current()
    assert newest != epoch and current.value["rules"] == "stale"
    # The guarded writer resuming its rotation from the sealed state changes nothing.
    guard._advance(epoch, sealed.value)
    assert guard._current()[1].value["rules"] == "stale"


def test_newly_published_rules_take_over_a_rebuild_left_unfinished(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Revision A's rebuild stops part way; B is published before anything finishes it. B's
    # writers rebuild for B rather than being refused for ever by A's leftover progress.
    service = gadgets.service()
    unique = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    app = publish_ruled_board(service, [unique])
    card(service, app, "a", "todo")
    card(service, app, "b", "todo")
    app = _republish(
        service, app, ruled_revision(service, app["id"], [{**unique, "message": "A"}])
    )
    monkeypatch.setattr(constraints_module, "REBUILD_PAGE", 1)
    monkeypatch.setattr(constraints_module, "REBUILD_STEPS_PER_WRITE", 2)
    with pytest.raises(Conflict):
        card(service, app, "z", "todo")
    monkeypatch.undo()
    app = _republish(
        service, app, ruled_revision(service, app["id"], [{**unique, "message": "B"}])
    )

    assert card(service, app, "c", "todo")["id"] == "c"
    with pytest.raises(ConstraintViolation) as refused:
        rpc(
            service,
            app,
            "create",
            id="d",
            value={"title": "a", "column": "todo"},
            operation_id="d-dup",
        )
    assert str(refused.value) == "B"


def test_a_guarded_write_from_an_older_revision_never_lands_behind_a_rebuild(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The older revision has the same unique rule with another message, so its guard writes
    # through the rules; the newer one's rebuild has cleared the live keys and read "a".
    service = gadgets.service()
    unique = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    old_rules = [{**unique, "message": "Titles are unique."}]
    app = publish_ruled_board(service, old_rules)
    first = card(service, app, "a", "todo")
    card(service, app, "b", "todo")
    app = _republish(service, app, ruled_revision(service, app["id"], [unique]))
    monkeypatch.setattr(constraints_module, "REBUILD_PAGE", 1)
    monkeypatch.setattr(constraints_module, "REBUILD_STEPS_PER_WRITE", 2)
    with pytest.raises(Conflict):
        card(service, app, "z", "todo")
    gadget = gadgets.store.get(app["id"])
    files = service._files(gadget)
    store = documents_module.ImmutableDocumentStore(
        files, "data", limits=service_module._DATA_STORE_LIMITS
    )
    # Its revision is no longer the published one.
    old_guard = ConstraintGuard(
        files, "data/_guard", store, old_rules, service.viewer.user_id, is_current=lambda: False
    )

    with pytest.raises(Conflict):
        old_guard.write(
            "replace",
            "cards",
            "a",
            {"title": "taken", "column": "todo"},
            first["version"],
            "old-taken",
        )
    monkeypatch.undo()

    assert rpc(service, app, "get", id="a")["value"]["title"] == "a"
    rpc(
        service,
        app,
        "create",
        id="c",
        value={"title": "taken", "column": "doing"},
        operation_id="c-taken",
    )
    with pytest.raises(ConstraintViolation):
        rpc(
            service,
            app,
            "create",
            id="d",
            value={"title": "taken", "column": "doing"},
            operation_id="d-taken",
        )


@pytest.mark.parametrize("older", ["no_rules", "other_rules"])
def test_a_write_from_an_older_revision_restarts_a_rebuild_under_way(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch, older: str
) -> None:
    # The older revision's marker found the state stale (no rules) or matching its own
    # rules (rules for another collection), and used to leave the rebuild to go on.
    service = gadgets.service()
    unique = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    old_rules = [] if older == "no_rules" else [_FEW_RUNS]
    app = publish_ruled_board(service, [unique])
    card(service, app, "a", "todo")
    app = _republish(service, app, ruled_revision(service, app["id"], old_rules))
    # Written without the unique rule: the next publish of it must rebuild.
    card(service, app, "b", "todo")
    if old_rules:
        # A guarded write commits the state for the older rules.
        rpc(
            service,
            app,
            "create",
            collection="runs",
            id="r",
            value={"state": "x"},
            operation_id="c-r",
        )
    first = rpc(service, app, "get", id="a")
    app = _republish(service, app, ruled_revision(service, app["id"], [unique]))
    # The new rules' rebuild reads one record ("a"), saves where it got to, and stops.
    monkeypatch.setattr(constraints_module, "REBUILD_PAGE", 1)
    monkeypatch.setattr(constraints_module, "REBUILD_STEPS_PER_WRITE", 3)
    with pytest.raises(Conflict):
        card(service, app, "z", "todo")
    # A request validated before the new rules changes "a", which the rebuild already read.
    _old_revision_replace(service, gadgets, app, old_rules, first, "taken")
    monkeypatch.undo()

    with pytest.raises(ConstraintViolation):
        rpc(
            service,
            app,
            "create",
            id="c",
            value={"title": "taken", "column": "doing"},
            operation_id="c-dup",
        )


@pytest.mark.parametrize(
    ("case", "between"),
    [
        *((case, "none") for case in ["count", "unique", "restrict", "cascade", "unset"]),
        ("count", "other"),
        ("unique", "other"),
    ],
)
def test_rules_published_again_count_the_writes_made_without_them(
    gadgets: Fixture, case: str, between: str
) -> None:
    # Rules R, then a revision whose rules don't cover the collection written (none at all,
    # or rules only for another collection), then R again: the state R left behind never saw
    # the writes in between, so R must recount them instead of reusing it.
    service = gadgets.service()
    if case == "count":
        rule = {"name": "few", "kind": "max_records", "collection": "cards", "max": 2}
    elif case == "unique":
        rule = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    else:
        rule = {
            "name": "run_card",
            "kind": "reference",
            "collection": "runs",
            "field": "cardId",
            "target": "cards",
            "on_delete": case,
        }
    app = publish_ruled_board(service, [rule])
    ruled = app["published_revision"]
    target = card(service, app, "a", "todo")
    other = (
        [{"name": "few_runs", "kind": "max_records", "collection": "runs", "max": 100}]
        if between == "other"
        else []
    )
    app = _republish(service, app, ruled_revision(service, app["id"], other))
    if case == "count":
        card(service, app, "b", "todo")
    elif case == "unique":
        card(service, app, "b", "todo")
        rpc(
            service,
            app,
            "create",
            id="c",
            value={"title": "b", "column": "doing"},
            operation_id="c-dup",
        )
    else:
        rpc(
            service,
            app,
            "create",
            collection="runs",
            id="r1",
            value={"cardId": "a", "state": "done"},
            operation_id="r1",
        )
    app = _republish(service, app, ruled)

    if case == "count":
        with pytest.raises(ConstraintViolation):
            card(service, app, "c", "todo")
    elif case == "unique":
        # Recounting finds the duplicate made without the rule; a further one is refused.
        with pytest.raises(ConstraintViolation):
            rpc(
                service,
                app,
                "create",
                id="d",
                value={"title": "b", "column": "todo"},
                operation_id="c-d",
            )
    elif case == "restrict":
        with pytest.raises(ConstraintViolation):
            rpc(service, app, "delete", id="a", if_version=target["version"], operation_id="d-a")
    else:
        rpc(service, app, "delete", id="a", if_version=target["version"], operation_id="d-a")
        run = rpc(service, app, "get", collection="runs", id="r1")
        if case == "cascade":
            assert run is None
        else:
            assert run["value"] == {"state": "done"}


def test_a_rebuild_over_many_unique_keys_keeps_the_collection_writable(gadgets: Fixture) -> None:
    # 201 unique keys fit in the rule state, but the old keys plus a rebuild's first page of
    # new ones would not: a rebuild after a message-only republish must drop the old ones.
    service = gadgets.service()
    rule = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    app = publish_ruled_board(service, [rule])
    created = [
        rpc(
            service,
            app,
            "create",
            id=f"c{index:063d}",
            value={"title": f"t{index:03d}", "column": "todo"},
            operation_id=f"c-{index}",
        )
        for index in range(201)
    ]
    reworded = ruled_revision(service, app["id"], [{**rule, "message": "Titles are unique"}])
    app = _republish(service, app, reworded)

    rpc(
        service,
        app,
        "delete",
        id=created[0]["id"],
        if_version=created[0]["version"],
        operation_id="d-0",
    )
    replaced = rpc(
        service,
        app,
        "replace",
        id=created[1]["id"],
        value={"title": "t001", "column": "doing"},
        if_version=created[1]["version"],
        operation_id="r-1",
    )
    assert replaced["value"]["column"] == "doing"
    with pytest.raises(ConstraintViolation):
        rpc(
            service,
            app,
            "create",
            id="dup",
            value={"title": "t002", "column": "todo"},
            operation_id="c-dup",
        )
    # The deleted record's title is free again.
    rpc(
        service,
        app,
        "create",
        id="again",
        value={"title": "t000", "column": "todo"},
        operation_id="c-again",
    )


def test_a_delete_over_capacity_unsets_dependents_that_share_a_grandfathered_value(
    gadgets: Fixture,
) -> None:
    # More unique keys than the rule state holds, and two runs written before the rule with
    # the same state, both naming card x. Deleting x unsets both: neither changes its state.
    service = gadgets.service()
    app = publish_ruled_board(service, [])
    target = card(service, app, "x", "todo")
    for index in range(340):
        rpc(
            service,
            app,
            "create",
            collection="runs",
            id=f"r{index:063d}",
            value={"state": f"s{index:04d}"},
            operation_id=f"c-{index}",
        )
    for name in ("da", "db"):
        rpc(
            service,
            app,
            "create",
            collection="runs",
            id=name,
            value={"cardId": "x", "state": "dup"},
            operation_id=f"c-{name}",
        )
    unique = {"name": "one_state", "kind": "unique", "collection": "runs", "fields": ["state"]}
    app = _republish(service, app, ruled_revision(service, app["id"], [unique, _UNSET_RUN_CARD]))

    rpc(service, app, "delete", id="x", if_version=target["version"], operation_id="x-del")

    for name in ("da", "db"):
        assert _run(service, app, name)["value"] == {"state": "dup"}


def test_rules_published_over_more_unique_keys_than_fit_still_allow_deletes(
    gadgets: Fixture,
) -> None:
    # 340 long IDs under a unique rule are more keys than the rule state holds: the rule is
    # published anyway, deletes still land, other writes are refused until deletes make room,
    # and then the next write recounts and the rule holds again.
    service = gadgets.service()
    app = publish_ruled_board(service, [])
    created = [
        rpc(
            service,
            app,
            "create",
            id=f"r{index:063d}",
            value={"title": f"t{index:04d}", "column": "todo"},
            operation_id=f"c-{index}",
        )
        for index in range(340)
    ]
    rule = {"name": "one_title", "kind": "unique", "collection": "cards", "fields": ["title"]}
    app = _republish(service, app, ruled_revision(service, app["id"], [rule]))

    def delete(index: int) -> None:
        document = created[index]
        rpc(
            service,
            app,
            "delete",
            id=document["id"],
            if_version=document["version"],
            operation_id=f"d-{index}",
        )

    delete(0)
    assert rpc(service, app, "get", id=created[0]["id"]) is None
    with pytest.raises(CapacityExceeded) as full:
        rpc(
            service,
            app,
            "create",
            id="new",
            value={"title": "fresh", "column": "todo"},
            operation_id="c-new",
        )
    assert full.value.details == {"reason": "rule_state_full"}
    for index in range(1, 100):
        delete(index)
    rpc(
        service,
        app,
        "create",
        id="new",
        value={"title": "fresh", "column": "todo"},
        operation_id="c-new-2",
    )
    with pytest.raises(ConstraintViolation):
        rpc(
            service,
            app,
            "create",
            id="dup",
            value={"title": "t0200", "column": "todo"},
            operation_id="c-dup",
        )


@pytest.mark.parametrize("replacement", [[], 7, "text"])
def test_a_ruled_record_replaced_by_a_value_that_is_not_an_object_names_nothing(
    gadgets: Fixture, replacement: object
) -> None:
    # A collection schema without "type": "object" accepts any JSON value: a record that
    # stops being an object stops naming its target, and the rules keep working after it.
    service = gadgets.service()
    app = service.create("Synthetic loose schema")
    revision = service.create_revision(
        app["id"],
        {
            "html": "<main>Synthetic loose schema</main>",
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write"],
            "collections": {
                "cards": _RULED_COLLECTIONS["cards"],
                "tasks": {"properties": {"project": {"type": "string"}}},
            },
            "constraints": [
                {
                    "name": "task_card",
                    "kind": "reference",
                    "collection": "tasks",
                    "field": "project",
                    "target": "cards",
                    "on_delete": "restrict",
                }
            ],
        },
    )["id"]
    app = service.publish(app["id"], {"revision_id": revision, "if_version": None})
    target = card(service, app, "p1", "todo")
    task = rpc(
        service,
        app,
        "create",
        collection="tasks",
        id="t1",
        value={"project": "p1"},
        operation_id="t1",
    )
    replaced = rpc(
        service,
        app,
        "replace",
        collection="tasks",
        id="t1",
        value=replacement,
        if_version=task["version"],
        operation_id="t1-loose",
    )
    assert replaced["value"] == replacement
    # Another ruled write lands, and the record no longer holds its old target back.
    rpc(
        service,
        app,
        "create",
        collection="tasks",
        id="t2",
        value={"project": "p1"},
        operation_id="t2",
    )
    with pytest.raises(ConstraintViolation):
        rpc(service, app, "delete", id="p1", if_version=target["version"], operation_id="d-p1")
    second = rpc(service, app, "get", collection="tasks", id="t2")
    rpc(
        service,
        app,
        "delete",
        collection="tasks",
        id="t2",
        if_version=second["version"],
        operation_id="d-t2",
    )
    rpc(service, app, "delete", id="p1", if_version=target["version"], operation_id="d-p1-2")
    assert rpc(service, app, "get", id="p1") is None


def test_deleting_a_referenced_record_reads_only_the_records_that_name_it(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = gadgets.service()
    rule = {
        "name": "run_card",
        "kind": "reference",
        "collection": "runs",
        "field": "cardId",
        "target": "cards",
        "on_delete": "cascade",
    }
    app = publish_ruled_board(service, [rule])
    busy = card(service, app, "busy", "doing")
    quiet = card(service, app, "quiet", "doing")
    for index in range(60):
        rpc(
            service,
            app,
            "create",
            collection="runs",
            id=f"r{index:03d}",
            value={"cardId": "busy", "state": "done"},
            operation_id=f"r{index}",
        )
    rpc(
        service,
        app,
        "create",
        collection="runs",
        id="r-quiet",
        value={"cardId": "quiet", "state": "done"},
        operation_id="r-quiet",
    )
    listed: list[str] = []
    for name in ("list_paths", "list_page"):
        original = getattr(LocalGadgetFiles, name)

        def counting(self, path, *args, _original=original, **kwargs):  # type: ignore[no-untyped-def]
            listed.append(path)
            return _original(self, path, *args, **kwargs)

        monkeypatch.setattr(LocalGadgetFiles, name, counting)

    rpc(service, app, "delete", id="quiet", if_version=quiet["version"], operation_id="d-q")

    assert rpc(service, app, "get", collection="runs", id="r-quiet") is None
    # The 60 runs naming the other card are never listed or read.
    assert not any(path.rstrip("/").endswith("/runs") for path in listed)
    # More dependents than one change may touch are refused, not cut short, after reading
    # one more entry than that and listing no index directory, however many there are.
    read: list[str] = []
    original_read = LocalGadgetFiles.read_json

    def counting_read(self, path, *args, **kwargs):  # type: ignore[no-untyped-def]
        read.append(path)
        return original_read(self, path, *args, **kwargs)

    monkeypatch.setattr(LocalGadgetFiles, "read_json", counting_read)
    listed.clear()
    with pytest.raises(CapacityExceeded):
        rpc(service, app, "delete", id="busy", if_version=busy["version"], operation_id="d-b")
    assert not any("/refs/" in path for path in listed)
    entries = [path for path in read if "/refs/" in path and "/n/" in path]
    assert len(entries) == constraints_module.MAX_PLAN_OPERATIONS + 1


_RUN_CARD_RESTRICT = {
    "name": "run_card",
    "kind": "reference",
    "collection": "runs",
    "field": "cardId",
    "target": "cards",
    "on_delete": "restrict",
}


def _runs_naming(service: GadgetService, app: dict, card_id: str, count: int) -> list[dict]:
    return [
        rpc(
            service,
            app,
            "create",
            collection="runs",
            id=f"r{index}",
            value={"cardId": card_id, "state": "done"},
            operation_id=f"run-{index}",
        )
        for index in range(count)
    ]


def _rename_runs(service: GadgetService, app: dict, runs: list[dict], card_id: str) -> None:
    for run in runs:
        rpc(
            service,
            app,
            "replace",
            collection="runs",
            id=run["id"],
            value={**run["value"], "cardId": card_id},
            if_version=run["version"],
            operation_id=f"move-{run['id']}",
        )


def test_references_written_without_rules_leave_no_index_entries_to_read(
    gadgets: Fixture,
) -> None:
    """Runs that moved to another card while no rule applied leave entries under the old
    card; publishing the rule again rebuilds the index into a new generation, so deleting the
    old card reads none of them."""
    service = gadgets.service()
    app = publish_ruled_board(service, [_RUN_CARD_RESTRICT])
    old = card(service, app, "old", "todo")
    card(service, app, "new", "todo")
    runs = _runs_naming(service, app, "old", 6)
    revision = ruled_revision(service, app["id"], [])
    app = service.publish(app["id"], {"revision_id": revision, "if_version": app["version"]})
    _rename_runs(service, app, runs, "new")
    revision = ruled_revision(service, app["id"], [_RUN_CARD_RESTRICT])
    app = service.publish(app["id"], {"revision_id": revision, "if_version": app["version"]})
    card(service, app, "warm", "todo")  # Finishes the rebuild before the delete is measured.

    examined: list[str] = []
    gadgets.store.after_list = lambda path, entries: examined.extend(
        f"{path}/{entry}" for entry in entries if "/refs/" in path and path.endswith("/old")
    )
    rpc(service, app, "delete", id="old", if_version=old["version"], operation_id="d-old")
    assert rpc(service, app, "get", id="old") is None
    assert examined == []


def test_a_late_index_retirement_keeps_the_generation_a_later_rebuild_wrote(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuild that pauses before retiring the generations it replaced, while a later
    rebuild writes the next one, must not remove that newer index when it resumes: the
    references it holds still refuse the delete."""
    delayed: list[tuple[ConstraintGuard, int]] = []
    retire = ConstraintGuard._retire_index_generations

    def pause_first(guard: ConstraintGuard, current: int) -> None:
        if not delayed:
            delayed.append((guard, current))
            return
        retire(guard, current)

    monkeypatch.setattr(ConstraintGuard, "_retire_index_generations", pause_first)
    service = gadgets.service()
    app = publish_ruled_board(service, [_RUN_CARD_RESTRICT])
    target = card(service, app, "target", "todo")
    assert delayed, "the first rebuild retires the generations it replaced"
    # A run written while no rule applied, then the rule published again: the next guarded
    # write rebuilds the index into a newer generation.
    revision = ruled_revision(service, app["id"], [])
    app = service.publish(app["id"], {"revision_id": revision, "if_version": app["version"]})
    _runs_naming(service, app, "target", 1)
    revision = ruled_revision(service, app["id"], [_RUN_CARD_RESTRICT])
    app = service.publish(app["id"], {"revision_id": revision, "if_version": app["version"]})
    card(service, app, "warm", "todo")
    guard, generation = delayed[0]
    retire(guard, generation)  # The paused rebuild finally retires.

    with pytest.raises(ConstraintViolation):
        rpc(
            service,
            app,
            "delete",
            id="target",
            if_version=target["version"],
            operation_id="d-target",
        )


def test_a_lookup_over_too_many_left_behind_entries_rebuilds_the_index(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that can't delete keeps every entry a change left behind; a delete that would
    read more than its budget has the index rebuilt, then reads only live references."""
    monkeypatch.setattr(constraints_module, "MAX_STALE_INDEX_ENTRIES", 3)
    monkeypatch.setattr(HookedFiles, "supports_delete", property(lambda self: False))
    service = gadgets.service()
    app = publish_ruled_board(service, [_RUN_CARD_RESTRICT])
    old = card(service, app, "old", "todo")
    new = card(service, app, "new", "todo")
    _rename_runs(service, app, _runs_naming(service, app, "old", 6), "new")

    examined: list[str] = []
    gadgets.store.after_list = lambda path, entries: examined.extend(
        entries if "/refs/" in path and path.endswith("/old") else []
    )
    rpc(service, app, "delete", id="old", if_version=old["version"], operation_id="d-old")
    assert rpc(service, app, "get", id="old") is None
    # One lookup gave up after its budget; the one after the rebuild read nothing.
    assert len(examined) <= 6
    # The runs still name a live card, so its delete is still refused.
    with pytest.raises(ConstraintViolation):
        rpc(service, app, "delete", id="new", if_version=new["version"], operation_id="d-new")


@pytest.mark.parametrize(
    ("second", "deleted"),
    [("restrict", False), ("unset", True), ("cascade", True)],
)
def test_every_reference_rule_on_one_record_applies(
    gadgets: Fixture, second: str, deleted: bool
) -> None:
    """A run referencing one card through two fields meets both fields' rules, whichever
    the store happens to see first."""
    service = gadgets.service()
    rules = [
        {
            "name": name,
            "kind": "reference",
            "collection": "runs",
            "field": field,
            "target": "cards",
            "on_delete": on_delete,
        }
        for name, field, on_delete in (
            ("run_card", "cardId", "unset"),
            ("run_review", "reviewCardId", second),
        )
    ]
    app = publish_ruled_board(service, rules)
    target = card(service, app, "a", "doing")
    rpc(
        service,
        app,
        "create",
        collection="runs",
        id="r1",
        value={"cardId": "a", "reviewCardId": "a", "state": "done"},
        operation_id="r1",
    )
    delete = {"id": "a", "if_version": target["version"], "operation_id": "d-a"}
    if not deleted:
        with pytest.raises(ConstraintViolation):
            rpc(service, app, "delete", **delete)
        assert rpc(service, app, "get", id="a") is not None
        run = rpc(service, app, "get", collection="runs", id="r1")
        assert run["value"] == {"cardId": "a", "reviewCardId": "a", "state": "done"}
        return
    rpc(service, app, "delete", **delete)
    run = rpc(service, app, "get", collection="runs", id="r1")
    if second == "cascade":
        assert run is None
    else:
        # Both references are gone, in one update: no dangling second reference.
        assert run["value"] == {"state": "done"}


def test_a_change_left_half_done_is_finished_by_the_next_writer(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = gadgets.service()
    app = publish_ruled_board(service, [WIP_LIMIT])
    original_run = ConstraintGuard._run

    def stop_after_recording(self, epoch, started):
        raise RuntimeError("writer stopped")

    monkeypatch.setattr(ConstraintGuard, "_run", stop_after_recording)
    with pytest.raises(RuntimeError):
        card(service, app, "a", "doing")
    assert rpc(service, app, "get", id="a") is None
    monkeypatch.setattr(ConstraintGuard, "_run", original_run)
    card(gadgets.service(), app, "b", "doing")
    assert rpc(service, app, "get", id="a")["value"]["column"] == "doing"
    with pytest.raises(ConstraintViolation):
        card(service, app, "c", "doing")


def _stop_once_before_epoch_revision(monkeypatch: pytest.MonkeyPatch, epoch: str) -> list[str]:
    """Make the first write of *epoch*'s first revision fail after its directory exists."""
    real_create = LocalGadgetFiles.create_json
    stopped: list[str] = []

    def create_json(self, path: str, value: dict) -> None:
        if not stopped and path.endswith(f"_guard/epochs/{epoch}/00000001.json"):
            stopped.append(path)
            raise RuntimeError("writer stopped")
        real_create(self, path, value)

    monkeypatch.setattr(LocalGadgetFiles, "create_json", create_json)
    return stopped


def test_an_epoch_left_without_its_first_revision_is_finished_by_the_next_writer(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = gadgets.service()
    app = publish_ruled_board(service, [WIP_LIMIT])
    stopped = _stop_once_before_epoch_revision(monkeypatch, "e000001")
    with pytest.raises(RuntimeError):
        card(service, app, "a", "doing")
    assert stopped
    assert gadgets.store.gadget_files(app["id"]).list_paths("data/_guard/epochs") == ["e000001"]
    # A new service instance, as after a restart, finishes the epoch and keeps the rule.
    restarted = gadgets.service()
    card(restarted, app, "b", "doing")
    card(restarted, app, "c", "doing")
    with pytest.raises(ConstraintViolation):
        card(restarted, app, "d", "doing")


def test_finding_the_rule_epoch_stays_bounded_while_retirement_is_held_up(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Retirement is held up (a stopped remover's ticket) while writers keep rotating: the
    # epochs pile up, yet each write finds its epoch with a few probes, never a listing.
    monkeypatch.setattr(constraints_module, "EPOCH_WRITES", 2)
    monkeypatch.setattr(
        constraints_module,
        "GUARD_LIMITS",
        StoreLimits(max_records_per_collection=200, max_list_entries=400),
    )
    held_up = {"on": True}
    real_retire = documents_module.ImmutableDocumentStore.retire

    def retire(self, collection, id, actor):  # type: ignore[no-untyped-def]
        return False if held_up["on"] else real_retire(self, collection, id, actor)

    monkeypatch.setattr(documents_module.ImmutableDocumentStore, "retire", retire)
    service = gadgets.service()
    app = publish_ruled_board(service, [{**WIP_LIMIT, "max": 3}])
    document = card(service, app, "a", "todo")
    for n in range(60):
        document = move(service, app, document, "doing" if n % 2 == 0 else "todo", f"m-{n}")
    epochs = gadgets.store.gadget_files(app["id"]).list_paths("data/_guard/epochs")
    assert len(epochs) > 20  # The backlog.

    reads: list[str] = []
    gadgets.store.after_list = lambda path, _entries: reads.append(path)
    document = move(service, app, document, "doing", "m-probe")
    gadgets.store.after_list = None
    epoch_reads = [path for path in reads if path.startswith("data/_guard/epochs")]
    assert "data/_guard/epochs" not in epoch_reads
    assert len(epoch_reads) <= 3 * (2 * len(epochs).bit_length() + 6)

    # Once retirement goes through again, the backlog is retired a couple per write.
    held_up["on"] = False
    for n in range(len(epochs)):
        document = move(service, app, document, "todo" if n % 2 == 0 else "doing", f"r-{n}")
    left = gadgets.store.gadget_files(app["id"]).list_paths("data/_guard/epochs")
    assert len(left) <= 4


def test_the_rule_state_rotates_without_losing_counts(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Room for three epoch records, and a new epoch every three writes: the writes below
    # need many more epochs than that, so old ones must be retired as the state rotates.
    monkeypatch.setattr(constraints_module, "EPOCH_WRITES", 3)
    monkeypatch.setattr(
        constraints_module,
        "GUARD_LIMITS",
        StoreLimits(max_records_per_collection=3, max_list_entries=3),
    )
    service = gadgets.service()
    app = publish_ruled_board(service, [{**WIP_LIMIT, "max": 3}])
    todo = [card(service, app, name, "todo") for name in ("a", "b", "c", "d")]
    for round_number in range(4):
        for index, document in enumerate(todo[:3]):
            document = move(service, app, document, "doing", f"m-{round_number}-{index}")
            todo[index] = move(service, app, document, "todo", f"b-{round_number}-{index}")
    for index, document in enumerate(todo[:3]):
        todo[index] = move(service, app, document, "doing", f"m-last-{index}")
    with pytest.raises(ConstraintViolation):
        move(service, app, todo[3], "doing", "m-3")
    epochs = gadgets.store.gadget_files(app["id"]).list_paths("data/_guard/epochs")
    assert 1 <= len(epochs) <= 3
    assert max(int(name[1:]) for name in epochs) > 3
    # A writer that stops while rotating, before the next epoch's first revision, leaves an
    # empty epoch; the next writer finishes it from its sealed predecessor.
    newest = max(int(name[1:]) for name in epochs)
    stopped = _stop_once_before_epoch_revision(monkeypatch, f"e{newest + 1:06d}")
    restarted = gadgets.service()
    for index, document in enumerate(todo[:3]):
        try:
            todo[index] = move(restarted, app, document, "todo", f"r-{index}")
        except RuntimeError:
            todo[index] = move(gadgets.service(), app, document, "todo", f"r-{index}")
    assert stopped
    for index, document in enumerate(todo[:3]):
        todo[index] = move(gadgets.service(), app, document, "doing", f"d-{index}")
    with pytest.raises(ConstraintViolation):
        move(gadgets.service(), app, todo[3], "doing", "m-4")


def test_the_rule_state_keeps_working_on_a_store_that_cannot_delete(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A store that can't delete never gets an old epoch's record back. With room for three
    # epoch records and a write limit of four, rotating every four writes would fill the
    # guard after a few edits; there an epoch must last until its record is nearly full.
    monkeypatch.setattr(HookedFiles, "supports_delete", property(lambda self: False))
    monkeypatch.setattr(constraints_module, "EPOCH_WRITES", 4)
    monkeypatch.setattr(
        constraints_module,
        "GUARD_LIMITS",
        StoreLimits(max_records_per_collection=3, max_list_entries=3),
    )
    service = gadgets.service()
    one_title = {
        "name": "one_title",
        "kind": "unique",
        "collection": "cards",
        "fields": ["title"],
        "message": "Titles are unique.",
    }
    app = publish_ruled_board(service, [{**WIP_LIMIT, "max": 1}, one_title])
    document = card(service, app, "a", "todo")
    other = card(service, app, "b", "todo")
    for index in range(40):
        document = move(
            service, app, document, "doing" if index % 2 == 0 else "todo", f"e-{index}"
        )
    assert document["value"]["column"] == "todo"
    # The rules still hold after all those edits: one card in progress, titles unique.
    document = move(service, app, document, "doing", "e-last")
    with pytest.raises(ConstraintViolation):
        move(service, app, other, "doing", "o-doing")
    with pytest.raises(ConstraintViolation):
        rpc(
            service,
            app,
            "create",
            id="c",
            value={"title": "a", "column": "todo"},
            operation_id="c-dup",
        )
    epochs = gadgets.store.gadget_files(app["id"]).list_paths("data/_guard/epochs")
    assert len(epochs) == 1


def test_an_epoch_on_a_store_that_cannot_delete_rotates_before_its_record_is_full(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(HookedFiles, "supports_delete", property(lambda self: False))
    monkeypatch.setattr(
        constraints_module,
        "GUARD_LIMITS",
        StoreLimits(
            max_revisions_per_record=30,
            revisions_per_segment=10,
            max_records_per_collection=3,
            max_list_entries=3,
        ),
    )
    service = gadgets.service()
    app = publish_ruled_board(service, [{**WIP_LIMIT, "max": 1}])
    document = card(service, app, "a", "todo")
    for index in range(10):
        document = move(
            service, app, document, "doing" if index % 2 == 0 else "todo", f"e-{index}"
        )
    epochs = gadgets.store.gadget_files(app["id"]).list_paths("data/_guard/epochs")
    # Sealed before each record's revision limit, so no write was refused for a full epoch.
    assert len(epochs) >= 2
    # Finding the current epoch probes numbered epochs: it never reads the epoch list, and
    # the probes grow with the log of the epochs kept, not with their number.
    reads: list[str] = []
    gadgets.store.after_list = lambda path, _entries: reads.append(path)
    move(service, app, document, "doing", "e-last")
    gadgets.store.after_list = None
    epoch_reads = [path for path in reads if path.startswith("data/_guard/epochs")]
    assert "data/_guard/epochs" not in epoch_reads
    probes_per_lookup = 2 * len(epochs).bit_length() + 2
    lookups = 3  # A guarded write looks the epoch up a few times.
    assert len(epoch_reads) <= lookups * probes_per_lookup
    with pytest.raises(ConstraintViolation):
        card(service, app, "b", "doing")


@pytest.mark.parametrize(
    "constraint",
    [
        {"name": "bad", "kind": "max_records", "collection": "missing", "max": 1},
        {"name": "bad", "kind": "max_records", "collection": "cards", "max": -1},
        {
            "name": "bad",
            "kind": "max_records",
            "collection": "cards",
            "max": 1,
            "where": {"owner": "x"},
        },
        {"name": "bad", "kind": "unique", "collection": "cards", "fields": []},
        {
            "name": "bad",
            "kind": "reference",
            "collection": "runs",
            "field": "cardId",
            "target": "people",
        },
        {
            "name": "bad",
            "kind": "reference",
            "collection": "runs",
            "field": "state",
            "target": "cards",
            "on_delete": "unset",
        },
        {
            "name": "bad",
            "kind": "reference",
            "collection": "runs",
            "field": "cardId",
            "target": "cards",
            "on_delete": "drop",
        },
        {"name": "Bad name", "kind": "unique", "collection": "cards", "fields": ["title"]},
        {"name": "bad", "kind": "trigger", "collection": "cards"},
        {"name": "bad", "kind": "unique", "collection": "cards", "fields": ["title"], "sql": "x"},
        # Values of the wrong JSON type are refused, not raised as server errors.
        {"name": "bad", "kind": "unique", "collection": [], "fields": ["title"]},
        {"name": "bad", "kind": "unique", "collection": "cards", "fields": [{}]},
        {
            "name": "bad",
            "kind": "reference",
            "collection": "runs",
            "field": "cardId",
            "target": [],
        },
        {
            "name": "bad",
            "kind": "reference",
            "collection": "runs",
            "field": "cardId",
            "target": "cards",
            "on_delete": [],
        },
    ],
)
def test_rules_that_do_not_match_the_revision_are_refused_at_upload(
    gadgets: Fixture, constraint: dict
) -> None:
    service = gadgets.service()
    app = service.create("Synthetic ruled board")
    with pytest.raises(InvalidInput):
        ruled_revision(service, app["id"], [constraint])


def test_describe_returns_the_guide_and_rules_without_the_source(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = publish_ruled_board(
        service, [WIP_LIMIT], guide="# Board\nIn progress means someone is on it."
    )
    described = service.describe(app["id"])
    assert described["guide"].startswith("# Board")
    assert described["constraints"][0]["name"] == "wip_limit"
    assert described["collections"]["cards"]["required"] == ["title", "column"]
    assert described["gadget"]["browse_url"] == f"/gadget/{app['id']}"
    assert "html" not in described
    assert "gadget skill" in described["next_steps"]


# --- Gadget actions and gadget sessions -------------------------------------------------------

RUN_CARD_ACTION = {
    "name": "run_card",
    "description": "Work on a card in a child session.",
    "params": {
        "type": "object",
        "required": ["cardId"],
        "properties": {"cardId": {"type": "string"}},
    },
}


def publish_actions_board(service: GadgetService, actions: list | None = None) -> dict:
    app = service.create("Synthetic action board")
    revision = service.create_revision(
        app["id"],
        {
            "html": "<main>Synthetic action board</main>",
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write"],
            "collections": _RULED_COLLECTIONS,
            "actions": [RUN_CARD_ACTION] if actions is None else actions,
        },
    )
    return service.publish(app["id"], {"revision_id": revision["id"], "if_version": None})


def test_an_editor_requests_a_declared_action_and_the_session_records_its_progress(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    app = publish_actions_board(service)
    request = {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "req-1"}
    queued = service.request_action(app["id"], request)
    assert queued["value"]["state"] == "queued"
    assert queued["value"]["requested_by"] == person(1)
    assert service.request_action(app["id"], request) == queued

    def update(version: str, operation_id: str, **changes):
        return service.update_action(
            app["id"],
            {"id": queued["id"], "if_version": version, "operation_id": operation_id, **changes},
        )

    running = update(queued["version"], "u-1", state="running", session_id="child_1")
    done = update(running["version"], "u-2", state="done", summary="Fixed **login**")
    assert done["value"] == {
        **queued["value"],
        "state": "done",
        "session_id": "child_1",
        "summary": "Fixed **login**",
    }
    assert [item["id"] for item in service.list_actions(app["id"])["items"]] == [queued["id"]]
    with pytest.raises(Conflict):
        update(running["version"], "u-3", state="failed")


def test_an_identical_action_retry_after_a_republish_gets_its_first_receipt(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    app = publish_actions_board(service)
    request = {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "req-1"}
    queued = service.request_action(app["id"], request)
    # An HTML-only publication: same actions, a different revision.
    revision = service.create_revision(
        app["id"],
        {
            "html": "<main>Synthetic action board, restyled</main>",
            "sdk_version": 1,
            "capabilities": ["data.read", "data.write"],
            "collections": _RULED_COLLECTIONS,
            "actions": [RUN_CARD_ACTION],
        },
    )
    current = service.get(app["id"])
    service.publish(app["id"], {"revision_id": revision["id"], "if_version": current["version"]})
    assert service.request_action(app["id"], request) == queued
    # The same operation ID for a different request is still refused.
    with pytest.raises(Conflict) as reused:
        service.request_action(app["id"], {**request, "params": {"cardId": "c2"}})
    assert reused.value.reason == "operation_reuse"


def test_retrying_an_action_that_already_finished_returns_its_receipt_without_a_new_one(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    app = publish_actions_board(service)
    request = {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "req-1"}
    first = service.request_action(app["id"], request)

    def update(version: str, operation_id: str, **changes):
        return service.update_action(
            app["id"],
            {"id": first["id"], "if_version": version, "operation_id": operation_id, **changes},
        )

    running = update(first["version"], "u-1", state="running", session_id="child_1")
    update(running["version"], "u-2", state="done", summary="Ran.")
    # Another request moves the finished action out of the viewer's open list.
    service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c2"}, "operation_id": "req-2"}
    )

    retry = service.request_action(app["id"], request)

    assert retry["id"] == first["id"]
    assert retry["delivered"] is True
    assert [item["id"] for item in service.list_actions(app["id"])["items"]].count(
        first["id"]
    ) == 1
    assert len(service.list_actions(app["id"])["items"]) == 2


def test_an_action_past_the_open_limit_is_refused_before_it_is_accepted(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    app = publish_actions_board(service)
    limit = service_module.MAX_OPEN_ACTIONS
    for number in range(limit):
        service.request_action(
            app["id"],
            {
                "action": "run_card",
                "params": {"cardId": f"c{number}"},
                "operation_id": f"r{number}",
            },
        )
    with pytest.raises(CapacityExceeded) as full:
        service.request_action(
            app["id"],
            {"action": "run_card", "params": {"cardId": "extra"}, "operation_id": "r-extra"},
        )
    assert full.value.details == {"reason": "too_many_open_actions", "limit": limit}
    # Nothing was accepted, and every accepted action is still followed and deliverable.
    listed = service.list_actions(app["id"])["items"]
    assert len(listed) == limit
    assert all(item["value"]["params"]["cardId"] != "extra" for item in listed)
    tracked = service.action_progress(app["id"], [])["items"]
    assert sorted(item["id"] for item in tracked) == sorted(item["id"] for item in listed)
    # A retry of an accepted request is still answered while the gadget is full.
    again = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c0"}, "operation_id": "r0"}
    )
    assert again["merged"] is False


def test_a_retried_action_never_pushes_out_another_open_one(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = publish_actions_board(service)
    service.set_gadget_session(app["id"], {"session_id": "s_gadget"})
    limit = service_module.MAX_OPEN_ACTIONS

    def ask(card_id: str, operation_id: str) -> dict:
        return service.request_action(
            app["id"],
            {"action": "run_card", "params": {"cardId": card_id}, "operation_id": operation_id},
        )

    for number in range(limit - 2):
        ask(f"c{number}", f"r{number}")
    # The first try of "late" saves its ledger entry, then fails to save the viewer record.
    failed = {"once": False}

    def fail_viewer_save(path: str) -> None:
        if "viewers" in path and not failed["once"]:
            failed["once"] = True
            raise OSError("viewer record not saved")

    gadgets.store.before_create = fail_viewer_save
    with pytest.raises(OSError):
        ask("late", "r-late")
    gadgets.store.before_create = None
    # Another request takes the last free place meanwhile.
    ask("other", "r-other")
    ask("filler", "r-filler")

    with pytest.raises(CapacityExceeded):
        ask("late", "r-late")
    # Every action that was followed still is, and can still be delivered.
    tracked = {item["id"] for item in service.action_progress(app["id"], [])["items"]}
    assert len(tracked) == limit
    for action_id in tracked:
        service.record_delivery(app["id"], action_id, {"session_id": "s_gadget"})


@pytest.mark.parametrize(
    "request_body",
    [
        {"action": "unknown", "operation_id": "r"},
        {"action": "run_card", "params": {"cardId": 5}, "operation_id": "r"},
        {"action": "run_card", "params": ["c1"], "operation_id": "r"},
        {"action": "run_card", "params": {"cardId": "c1"}},
    ],
)
def test_action_requests_must_match_a_declared_action(
    gadgets: Fixture, request_body: dict
) -> None:
    service = gadgets.service()
    app = publish_actions_board(service)
    with pytest.raises(InvalidInput):
        service.request_action(app["id"], request_body)


@pytest.mark.parametrize(
    "update",
    [
        {"state": "paused"},
        {"summary": "x" * 4001},
        {"session_id": "../other"},
        {"requested_by": "someone@example.com"},
    ],
)
def test_the_ledger_accepts_only_state_summary_and_session_changes(
    gadgets: Fixture, update: dict
) -> None:
    service = gadgets.service()
    app = publish_actions_board(service)
    queued = service.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r"}
    )
    with pytest.raises(InvalidInput):
        service.update_action(
            app["id"],
            {"id": queued["id"], "if_version": queued["version"], "operation_id": "u", **update},
        )


def test_actions_and_gadget_sessions_need_edit_access_to_the_gadget(gadgets: Fixture) -> None:
    owner = gadgets.service()
    app = publish_actions_board(owner)
    queued = owner.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r"}
    )
    gadgets.access.share(app, 2, DATA_EDIT)
    data_editor = gadgets.service(2)
    assert data_editor.list_actions(app["id"])["items"][0]["id"] == queued["id"]
    for attempt in (
        lambda: data_editor.request_action(
            app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r2"}
        ),
        lambda: data_editor.update_action(
            app["id"],
            {
                "id": queued["id"],
                "if_version": queued["version"],
                "operation_id": "u",
                "state": "done",
            },
        ),
        lambda: data_editor.set_gadget_session(app["id"], {"session_id": "s_2"}),
        lambda: data_editor.rename(app["id"], {"name": "Mine now"}),
    ):
        with pytest.raises(GadgetError) as error:
            attempt()
        assert error.value.code == "PERMISSION_DENIED"


def test_the_owner_sees_an_editors_queued_action_but_only_the_editor_carries_it_out(
    gadgets: Fixture,
) -> None:
    owner = gadgets.service()
    app = publish_actions_board(owner)
    gadgets.access.share(app, 2, EDIT)
    editor = gadgets.service(2)
    queued = editor.request_action(
        app["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "r"}
    )

    # Everyone who reads the gadget sees the ledger; it says whose each entry is.
    [seen] = owner.list_actions(app["id"])["items"]
    assert seen["id"] == queued["id"] and seen["yours"] is False
    assert editor.list_actions(app["id"])["items"][0]["yours"] is True

    take = {"id": queued["id"], "if_version": queued["version"], "operation_id": "u"}
    with pytest.raises(GadgetError) as error:
        owner.update_action(app["id"], {**take, "state": "running"})
    assert error.value.code == "PERMISSION_DENIED"
    running = editor.update_action(app["id"], {**take, "state": "running"})
    assert running["value"]["state"] == "running"


def test_each_viewer_keeps_their_own_gadget_session(gadgets: Fixture) -> None:
    owner = gadgets.service()
    app = publish_actions_board(owner)
    gadgets.access.share(app, 2, EDIT)
    teammate = gadgets.service(2)
    assert owner.get_gadget_session(app["id"])["session_id"] is None
    owner.set_gadget_session(app["id"], {"session_id": "s_owner"})
    teammate.set_gadget_session(app["id"], {"session_id": "s_teammate"})
    assert owner.get_gadget_session(app["id"])["session_id"] == "s_owner"
    assert teammate.get_gadget_session(app["id"])["session_id"] == "s_teammate"
    assert (
        owner.set_gadget_session(app["id"], {"session_id": "s_owner_2"})["session_id"]
        == "s_owner_2"
    )
    assert gadgets.service().get_gadget_session(app["id"])["session_id"] == "s_owner_2"
    # Going back to the first session is a change of its own, not a replay of the first.
    assert (
        owner.set_gadget_session(app["id"], {"session_id": "s_owner"})["session_id"] == "s_owner"
    )
    assert owner.get_gadget_session(app["id"])["session_id"] == "s_owner"
    with pytest.raises(InvalidInput):
        owner.set_gadget_session(app["id"], {"session_id": "../s"})


@pytest.mark.parametrize(
    "actions",
    [
        [RUN_CARD_ACTION, RUN_CARD_ACTION],
        [{"name": "run_card"}],
        [{**RUN_CARD_ACTION, "handler": "x"}],
        [{**RUN_CARD_ACTION, "params": {"$ref": "https://example.com/schema"}}],
    ],
)
def test_declared_actions_are_checked_at_upload(gadgets: Fixture, actions: list) -> None:
    service = gadgets.service()
    app = service.create("Synthetic action board")
    with pytest.raises(InvalidInput):
        service.create_revision(
            app["id"],
            {
                "html": "<main>x</main>",
                "sdk_version": 1,
                "capabilities": [],
                "collections": _RULED_COLLECTIONS,
                "actions": actions,
            },
        )


def test_only_a_manager_deletes_a_gadget_and_it_is_gone_for_everyone(gadgets: Fixture) -> None:
    owner = gadgets.service()
    app = publish_board(owner)
    rpc(owner, app, "create", value={"title": "Task"}, operation_id="create:one")
    gadgets.access.share(app, 2, EDIT)
    editor = gadgets.service(2)
    with pytest.raises(GadgetError) as error:
        editor.delete(app["id"])
    assert error.value.code == "PERMISSION_DENIED"
    assert owner.delete(app["id"]) == {"id": app["id"], "deleted": True}
    assert not [path for path in gadgets.snapshot() if app["id"] in path]
    assert not gadgets.access.grants
    for viewer in (owner, editor):
        with pytest.raises(NotFound):
            viewer.get(app["id"])
    assert owner.list_owned() == {"items": [], "next_cursor": None}


def test_a_delete_whose_grant_cleanup_fails_can_be_tried_again(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = gadgets.service()
    app = publish_board(owner)
    gadgets.access.share(app, 2, EDIT)
    forget = gadgets.access.forget
    calls = []

    def failing_once(gadget_id: str) -> None:
        calls.append(gadget_id)
        if len(calls) == 1:
            raise OSError("grant store unavailable")
        forget(gadget_id)

    monkeypatch.setattr(gadgets.access, "forget", failing_once)
    with pytest.raises(OSError):
        owner.delete(app["id"])
    assert owner.get(app["id"])["id"] == app["id"]  # Still there to delete again.
    assert owner.delete(app["id"]) == {"id": app["id"], "deleted": True}
    assert not gadgets.access.grants


def test_a_gadget_is_renamed_for_everyone_by_someone_who_can_edit_it(gadgets: Fixture) -> None:
    owner = gadgets.service()
    app = publish_actions_board(owner)
    gadgets.access.share(app, 2, READ)
    viewer = gadgets.service(2)

    renamed = owner.rename(app["id"], {"name": "  Launch board  "})
    assert renamed["name"] == "Launch board"
    assert viewer.get(app["id"])["name"] == "Launch board"
    assert [item["name"] for item in owner.list_owned()["items"]] == ["Launch board"]
    assert owner.rename(app["id"], {"name": "Launch board"})["name"] == "Launch board"
    with pytest.raises(GadgetError):
        viewer.rename(app["id"], {"name": "Mine now"})
    with pytest.raises(InvalidInput):
        owner.rename(app["id"], {"name": " "})


# --- The shelf and comments -------------------------------------------------------------------


def test_shelf_pins_own_and_shared_gadgets_per_viewer(gadgets: Fixture) -> None:
    owner = gadgets.service()
    mine, shared = owner.create("Mine"), owner.create("Shared")
    gadgets.access.share(shared, 2, READ)
    viewer = gadgets.service(2)
    assert viewer.shelf() == {"pinned": [], "has_owned": False, "next_cursor": None}

    assert viewer.pin(shared["id"], {"pinned": True})["pinned"] == [
        {"id": shared["id"], "name": "Shared"}
    ]
    # Pins are the viewer's own: the owner's shelf and gadget list do not change.
    assert owner.shelf() == {"pinned": [], "has_owned": True, "next_cursor": None}
    assert [item["id"] for item in viewer.list_owned()["items"]] == []
    with pytest.raises(NotFound):
        viewer.pin(mine["id"], {"pinned": True})

    owner.pin(mine["id"], {"pinned": True})
    owner.pin(shared["id"], {"pinned": True})
    assert [pin["id"] for pin in owner.shelf()["pinned"]] == [mine["id"], shared["id"]]
    # A pin follows a rename, and a deleted gadget drops off the shelf.
    owner.rename(shared["id"], {"name": "Renamed"})
    assert viewer.shelf()["pinned"] == [{"id": shared["id"], "name": "Renamed"}]
    owner.delete(shared["id"])
    assert viewer.shelf()["pinned"] == []
    owner.pin(mine["id"], {"pinned": False})
    owner.pin(mine["id"], {"pinned": False})
    assert owner.shelf()["pinned"] == []
    with pytest.raises(InvalidInput):
        owner.pin(mine["id"], {"pinned": "yes"})


def test_session_links_show_readable_gadgets_and_route_actions(gadgets: Fixture) -> None:
    owner = gadgets.service()
    first, second = owner.create("First"), owner.create("Second")
    session = "Session_A1"

    owner.link_session(first["id"], session, {"linked": True})
    linked = owner.link_session(second["id"], session, {"linked": True})
    assert [item["id"] for item in linked["items"]] == [first["id"], second["id"]]
    # Showing one again keeps the order.
    again = owner.link_session(first["id"], session, {"linked": True})
    assert [item["id"] for item in again["items"]] == [first["id"], second["id"]]
    # The session it is shown in also gets the actions requested from the gadget's page.
    assert owner.get_gadget_session(first["id"])["session_id"] == session
    # Showing it in another session, then this one again, moves the actions back here.
    owner.link_session(first["id"], "Session_B2", {"linked": True})
    assert owner.get_gadget_session(first["id"])["session_id"] == "Session_B2"
    owner.link_session(first["id"], session, {"linked": True})
    assert owner.get_gadget_session(first["id"])["session_id"] == session
    # A session the gadget only cites shows it without taking its actions.
    cited = owner.link_session(first["id"], "Cited_C3", {"linked": True, "handles_actions": False})
    assert [item["id"] for item in cited["items"]] == [first["id"]]
    assert owner.get_gadget_session(first["id"])["session_id"] == session
    with pytest.raises(InvalidInput):
        owner.link_session(first["id"], "Cited_C3", {"linked": True, "handles_actions": "no"})
    assert owner.session_gadgets("other")["items"] == []

    # A reader may show a shared gadget beside their own session, without becoming its handler.
    gadgets.access.share(first, 2, READ)
    reader = gadgets.service(2)
    shown = reader.link_session(first["id"], "mine", {"linked": True})["items"]
    assert [item["id"] for item in shown] == [first["id"]]
    assert reader.get_gadget_session(first["id"])["session_id"] is None
    assert owner.session_gadgets("mine")["items"] == []

    owner.delete(second["id"])
    assert [item["id"] for item in owner.session_gadgets(session)["items"]] == [first["id"]]
    assert owner.link_session(first["id"], session, {"linked": False})["items"] == []
    with pytest.raises(InvalidInput):
        owner.session_gadgets("bad id")


def test_a_comment_is_marked_addressed_only_while_it_reads_as_delivered(
    gadgets: Fixture,
) -> None:
    owner = gadgets.service()
    app = owner.create("Reviewed")
    anchor = {"text": "Q3 revenue"}
    comment = owner.create_comment(
        app["id"], {"body": "Use last quarter", "anchor": anchor, "operation_id": "c1"}
    )
    owner.update_comment(app["id"], comment["id"], {"body": "Use this quarter"})
    with pytest.raises(Conflict):
        owner.update_comment(
            app["id"], comment["id"], {"status": "addressed", "if_body": "Use last quarter"}
        )
    assert owner.list_comments(app["id"], None, 10, None)["items"][0]["status"] == "draft"
    marked = owner.update_comment(
        app["id"], comment["id"], {"status": "addressed", "if_body": "Use this quarter"}
    )
    assert marked["status"] == "addressed"


def test_comments_are_shared_with_everyone_who_can_read_the_gadget(gadgets: Fixture) -> None:
    owner = gadgets.service()
    app = owner.create("Reviewed")
    gadgets.access.share(app, 2, DATA_EDIT)
    gadgets.access.share(app, 3, READ)
    editor, reader = gadgets.service(2), gadgets.service(3)

    anchor = {"text": "Q3 revenue", "label": "Revenue card", "ref": "cards/7", "occurrence": 2}
    comment = editor.create_comment(
        app["id"], {"body": " Use last quarter ", "anchor": anchor, "operation_id": "c1"}
    )
    assert comment["body"] == "Use last quarter"
    assert comment["status"] == "draft"
    assert comment["anchor"] == anchor
    assert comment["created_by"] == person(2)
    # Retrying the same create returns the same comment.
    again = editor.create_comment(
        app["id"], {"body": "Use last quarter", "anchor": anchor, "operation_id": "c1"}
    )
    assert again["id"] == comment["id"]

    listed = reader.list_comments(app["id"])
    assert [item["id"] for item in listed["items"]] == [comment["id"]]
    assert listed["can_comment"] is False
    assert owner.list_comments(app["id"])["can_comment"] is True
    with pytest.raises(GadgetError):
        reader.create_comment(app["id"], {"body": "no", "anchor": anchor, "operation_id": "c2"})

    # Only the author edits the text; anyone who can comment marks it addressed.
    with pytest.raises(GadgetError):
        owner.update_comment(app["id"], comment["id"], {"body": "changed"})
    assert (
        owner.update_comment(app["id"], comment["id"], {"status": "addressed"})["status"]
        == "addressed"
    )
    assert owner.list_comments(app["id"], "draft")["items"] == []
    assert editor.update_comment(app["id"], comment["id"], {"body": "Edited"})["body"] == "Edited"
    for status in ("done", [], {}, 1):
        with pytest.raises(InvalidInput):
            owner.update_comment(app["id"], comment["id"], {"status": status})
        with pytest.raises(InvalidInput):
            owner.list_comments(app["id"], status)  # type: ignore[arg-type]

    other = editor.create_comment(
        app["id"], {"body": "Second", "anchor": {"text": "x"}, "operation_id": "c3"}
    )
    # Another editor may not delete someone else's comment.
    gadgets.access.share(app, 4, DATA_EDIT)
    with pytest.raises(GadgetError):
        gadgets.service(4).delete_comment(app["id"], other["id"])
    # The owner manages the gadget, so they can remove anyone's comment.
    owner.delete_comment(app["id"], other["id"])
    assert [item["id"] for item in owner.list_comments(app["id"])["items"]] == [comment["id"]]
    with pytest.raises(InvalidInput):
        editor.create_comment(
            app["id"], {"body": "x", "anchor": {"text": ""}, "operation_id": "c4"}
        )


def test_shared_lists_only_other_peoples_readable_gadgets(gadgets: Fixture) -> None:
    owner = gadgets.service()
    direct, unshared = owner.create("Direct"), owner.create("Unshared")
    gadgets.access.share(direct, 2, READ)
    mine = gadgets.service(2).create("Mine")
    gadgets.access.grants[person(2), mine["id"]] = READ
    items = gadgets.service(2).shared()["items"]
    assert [(item["id"], item["shared_via"], item["owner_email"]) for item in items] == [
        (direct["id"], "user", person(1))
    ]
    assert items[0]["permissions"]["manage"] is False
    assert unshared["id"] not in {item["id"] for item in items}
    assert owner.shared() == {"items": [], "next_cursor": None}


# --- Revisions, schemas, paging and other single-viewer cases ---------------------------------


def test_a_pinned_gadget_the_viewer_lost_access_to_leaves_their_shelf(gadgets: Fixture) -> None:
    owner = gadgets.service()
    app = owner.create("Board")
    gadgets.access.share(app, 2, READ)
    viewer = gadgets.service(2)
    viewer.pin(app["id"], {"pinned": True})
    kept = viewer.pin(owner.create("Other")["id"], {"pinned": False})
    assert kept["pinned"] == [{"id": app["id"], "name": "Board"}]

    gadgets.access.forget(app["id"])
    owner.rename(app["id"], {"name": "Synthetic secret plan"})
    # Neither the gadget nor its new name reaches them.
    assert viewer.shelf()["pinned"] == []
    # The next save of their shelf drops the dead pin.
    mine = viewer.create("Mine")
    viewer.pin(mine["id"], {"pinned": True})
    gadgets.access.share(app, 2, READ)
    assert [pin["id"] for pin in viewer.shelf()["pinned"]] == [mine["id"]]


def test_only_publishers_read_unpublished_revisions(gadgets: Fixture) -> None:
    owner = gadgets.service()
    app = publish_board(owner)
    draft = make_revision(owner, app, CARDS)
    gadgets.access.share(app, 2, READ)
    gadgets.access.share(app, 3, EDIT)
    viewer, editor = gadgets.service(2), gadgets.service(3)

    assert viewer.get_revision(app["id"])["revision_id"] == app["published_revision"]
    assert (
        viewer.get_revision(app["id"], app["published_revision"])["revision_id"]
        == app["published_revision"]
    )
    with pytest.raises(NotFound):
        viewer.get_revision(app["id"], draft["id"])
    assert editor.get_revision(app["id"], draft["id"])["revision_id"] == draft["id"]


def test_describe_answers_for_an_unpublished_gadget_and_points_to_the_skill(
    gadgets: Fixture,
) -> None:
    service = gadgets.service()
    new = service.create("Not built yet")
    unpublished = service.describe(new["id"])
    assert unpublished["gadget"]["name"] == "Not built yet"
    assert unpublished["published_revision"] is None
    assert unpublished["collections"] == {} and unpublished["actions"] == []
    assert unpublished["next_steps"].startswith("Nothing is published yet")
    assert "gadget skill" in unpublished["next_steps"]

    assert unpublished["published_by"] is None
    published = service.describe(publish_actions_board(service)["id"])
    assert "Nothing is published" not in published["next_steps"]
    # The agent can tell who decided what the actions ask it to do.
    assert published["published_by"] == service.viewer.user_id
    assert published["published_at"]
    assert published["actions"][0]["name"] == "run_card"


def test_schema_errors_name_the_keyword_and_the_field(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = service.create("Board")
    with pytest.raises(InvalidInput) as refused:
        make_revision(
            service,
            app,
            {
                "cards": {
                    "type": "object",
                    "properties": {"due": {"type": "string", "format": "date"}},
                }
            },
        )
    message = str(refused.value)
    assert "Unsupported schema keyword: format." in message
    assert "minLength" in message and "additionalProperties" in message

    board = publish_board(service)
    with pytest.raises(InvalidInput) as mismatch:
        rpc(service, board, "create", value={"title": 7}, operation_id="bad-1")
    assert "at value.title" in str(mismatch.value)
    assert mismatch.value.details == {
        "path": ["title"],
        "reason": "7 is not of type 'string'",
        "schema_keyword": "type",
    }


def test_concurrent_writes_to_a_ruled_collection_all_land(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = publish_ruled_board(service, [{**WIP_LIMIT, "max": 100}])

    def create(number: int) -> dict:
        return card(gadgets.service(), app, f"c{number}", "doing")

    with ThreadPoolExecutor(max_workers=12) as pool:
        created = list(pool.map(create, range(12)))
    assert sorted(item["id"] for item in created) == sorted(f"c{n}" for n in range(12))
    listed = rpc(service, app, "list", limit=100)["items"]
    assert len(listed) == 12


def test_a_revision_derived_by_edits_keeps_the_rest_of_its_base(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = service.create("Synthetic derived page")
    base = service.create_revision(
        app["id"],
        {
            "html": "<main><!-- view:start -->plain<!-- view:end --><p>kit</p></main>",
            "sdk_version": 1,
            "capabilities": ["data.read"],
            "collections": CARDS,
            "guide": "Synthetic guide",
            "actions": [{"name": "scan", "description": "Synthetic scan"}],
        },
    )

    derived = service.create_revision(
        app["id"],
        {
            "base_revision_id": base["id"],
            "edits": [
                {"start": "<!-- view:start -->", "end": "<!-- view:end -->", "new": "designed"},
                {"old": "<p>kit</p>", "new": "<p>kit 2</p>"},
            ],
        },
    )

    got = service.get_revision(app["id"], derived["id"])
    assert got["html"] == "<main><!-- view:start -->designed<!-- view:end --><p>kit 2</p></main>"
    original = service.get_revision(app["id"], base["id"])
    for field in ("capabilities", "collections", "guide", "actions", "constraints"):
        assert got[field] == original[field]
    # A field the request gives replaces the base's.
    renamed = service.create_revision(
        app["id"],
        {"base_revision_id": base["id"], "edits": [{"old": "plain", "new": "x"}], "guide": "New"},
    )
    assert service.get_revision(app["id"], renamed["id"])["guide"] == "New"


@pytest.mark.parametrize(
    ("edits", "reason", "message"),
    [
        ([{"old": "missing", "new": "x"}], "not_found", "Edit 1: its old text is not in the HTML"),
        ([{"old": "q", "new": "x"}], "ambiguous", "Edit 1: its old text appears 3 times"),
        (
            [{"old": "<p>", "new": "<p>"}, {"start": "[", "end": "]", "new": "x"}],
            "not_found",
            "Edit 2: its start marker appears 0 times",
        ),
        ([{"start": "q", "end": "z", "old": "c", "new": "x"}], "invalid", "Edit 1: needs new"),
        ([], None, "edits must be a list of 1 to 32 edits"),
    ],
)
def test_a_failed_edit_names_itself_and_creates_nothing(
    gadgets: Fixture, edits: list, reason: str | None, message: str
) -> None:
    service = gadgets.service()
    app = service.create("Synthetic derived page")
    base = service.create_revision(
        app["id"],
        {
            "html": "<p>q q q z</p>",
            "sdk_version": 1,
            "capabilities": [],
            "collections": CARDS,
        },
    )

    before = gadgets.snapshot()
    with pytest.raises(InvalidInput) as refused:
        service.create_revision(app["id"], {"base_revision_id": base["id"], "edits": edits})

    assert str(refused.value).startswith(message)
    if reason is not None:
        assert refused.value.details["reason"] == reason
    assert gadgets.snapshot() == before


def test_derived_revisions_need_their_base_and_keep_the_size_limit(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = service.create("Synthetic derived page")
    base = service.create_revision(
        app["id"],
        {"html": "<main>x</main>", "sdk_version": 1, "capabilities": [], "collections": CARDS},
    )

    with pytest.raises(InvalidInput, match="edits need base_revision_id"):
        service.create_revision(app["id"], {"edits": [{"old": "x", "new": "y"}]})
    with pytest.raises(NotFound):
        service.create_revision(
            app["id"], {"base_revision_id": "0" * 32, "edits": [{"old": "x", "new": "y"}]}
        )
    with pytest.raises(InvalidInput, match="not both"):
        service.create_revision(
            app["id"],
            {"base_revision_id": base["id"], "html": "<p>", "edits": [{"old": "x", "new": "y"}]},
        )
    with pytest.raises(InvalidInput, match="at most 1 MiB"):
        service.create_revision(
            app["id"],
            {
                "base_revision_id": base["id"],
                "edits": [{"old": "x", "new": "y" * (service_module.MAX_HTML_BYTES + 1)}],
            },
        )


def _every_page(read, **params) -> list[list[dict]]:
    pages, cursor = [], None
    while True:
        page = read(cursor=cursor, **params)
        pages.append(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


def test_comments_page_with_their_filter_and_reach_a_draft_past_the_first_page(
    gadgets: Fixture,
) -> None:
    owner = gadgets.service()
    app = publish_board(owner)
    created = [
        owner.create_comment(
            app["id"], {"body": f"Note {n}", "anchor": {"text": "x"}, "operation_id": f"n{n}"}
        )
        for n in range(5)
    ]
    # Comments page in the order they were made; the one draft left is the last made, so it
    # is on the last page.
    draft = created[-1]
    for comment in created:
        if comment is not draft:
            owner.update_comment(app["id"], comment["id"], {"status": "addressed"})

    pages = _every_page(lambda **kw: owner.list_comments(app["id"], **kw), status="draft", limit=2)
    assert [item["id"] for page in pages for item in page] == [draft["id"]]
    assert len(pages) == 3 and pages[0] == []
    every = [
        item
        for page in _every_page(lambda **kw: owner.list_comments(app["id"], **kw), limit=2)
        for item in page
    ]
    assert sorted(item["id"] for item in every) == sorted(comment["id"] for comment in created)
    with pytest.raises(InvalidInput):
        owner.list_comments(app["id"], limit=0)


def test_the_viewers_own_gadgets_page_in_id_order(gadgets: Fixture) -> None:
    owner = gadgets.service()
    made = [owner.create(f"Board {n}")["id"] for n in range(5)]
    pages = _every_page(owner.list_owned, limit=2)
    assert [len(page) for page in pages] == [2, 2, 1]
    assert [item["id"] for item in [item for page in pages for item in page]] == sorted(made)
    assert owner.shelf()["has_owned"] is True
    assert gadgets.service(2).shelf()["has_owned"] is False


def test_gadget_code_never_learns_who_its_viewers_are(gadgets: Fixture) -> None:
    """Gadget code gets an opaque ID per viewer, the same across the gadget's records and
    different in every gadget, and a name only when the server has a real one."""
    owner = gadgets.service()
    app = publish_board(owner)
    other = publish_board(owner)
    gadgets.access.share(app, 2, DATA_EDIT)
    names = {person(1): "Ada Lovelace", person(2): person(2)}
    teammate = GadgetService(
        gadgets.store,
        gadgets.access,
        GadgetViewer(person(2)),
        display_names=lambda ids: {user_id: names.get(user_id, user_id) for user_id in ids},
    )
    named = GadgetService(
        gadgets.store,
        gadgets.access,
        GadgetViewer(person(1)),
        display_names=lambda ids: {user_id: names.get(user_id, user_id) for user_id in ids},
    )

    mine = named.content(app["id"])["viewer"]
    theirs = teammate.content(app["id"])["viewer"]
    assert mine["display_name"] == "Ada Lovelace"
    # A name that is only the login (here an email) is never passed on.
    assert theirs["display_name"] is None
    assert mine["id"] != theirs["id"]
    assert person(2) not in theirs["id"] and person(1) not in mine["id"]
    # Stable within the gadget, different in another.
    assert teammate.content(app["id"])["viewer"]["id"] == theirs["id"]
    assert named.content(other["id"])["viewer"]["id"] != mine["id"]

    created = rpc(teammate, app, "create", id="c1", value={"title": "Mate"}, operation_id="c1")
    listed = rpc(teammate, app, "list", limit=10)
    fetched = rpc(teammate, app, "get", id="c1")
    for record in (created, fetched, *listed["items"]):
        assert record["created_by"] == theirs["id"]
        assert record["updated_by"] == theirs["id"]
    assert person(2) not in json.dumps([created, listed, fetched])
