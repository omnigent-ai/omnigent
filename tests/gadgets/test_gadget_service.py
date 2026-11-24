"""GadgetService on a real local store: records, publication, rules, actions, shelf, comments."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from omnigent.gadgets import constraints as constraints_module
from omnigent.gadgets import documents as documents_module
from omnigent.gadgets import service as service_module
from omnigent.gadgets.constraints import ConstraintGuard, ConstraintViolation
from omnigent.gadgets.documents import StoreLimits
from omnigent.gadgets.service import GadgetError, GadgetService
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
    assert shared["created_by"] == person(2)
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


@pytest.mark.parametrize("case", ["count", "unique", "restrict", "cascade", "unset"])
def test_rules_published_again_count_the_writes_made_without_them(
    gadgets: Fixture, case: str
) -> None:
    # Rules R, then a revision without rules, then R again: the state R left behind never saw
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
    app = _republish(service, app, ruled_revision(service, app["id"], []))
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
    # More dependents than one change may touch are refused, not cut short.
    with pytest.raises(CapacityExceeded):
        rpc(service, app, "delete", id="busy", if_version=busy["version"], operation_id="d-b")


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
    with pytest.raises(InvalidInput):
        owner.update_comment(app["id"], comment["id"], {"status": "done"})

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


# --- Bug bash regressions ------------------------------------------------------------------------


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
    # The one draft left is the comment with the last ID, so it is on the last page.
    draft = max(created, key=lambda comment: comment["id"])
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
