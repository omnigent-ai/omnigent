"""GadgetService on a real local store: records, publication, rules, actions, shelf, comments."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from omnigent.gadgets import constraints as constraints_module
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
from tests.gadgets.conftest import DATA_EDIT, EDIT, READ, Fixture, person

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
            "capabilities": ["data.read", "data.write", "sessions.create"],
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


def test_schema_validation_and_session_capability_fail_closed(gadgets: Fixture) -> None:
    service = gadgets.service()
    app = publish_board(service)
    with pytest.raises(InvalidInput):
        rpc(service, app, "create", id="invalid", value={"title": 123}, operation_id="invalid")
    with pytest.raises(InvalidInput):
        rpc(service, app, "get", id="../escape")
    assert "sessions.create" not in service.content(app["id"])["capabilities"]
    with pytest.raises(GadgetError) as error:
        service.rpc(app["id"], app["published_revision"], "sessions.v1.create", {})
    assert error.value.code == "UNSUPPORTED"


def test_new_server_capabilities_do_not_automatically_expand_an_existing_publication(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = gadgets.service()
    app = publish_board(service)
    monkeypatch.setattr(
        service_module,
        "_SUPPORTED_CAPABILITIES",
        frozenset({"data.read", "data.write", "sessions.create"}),
    )
    assert "sessions.create" not in service.content(app["id"])["capabilities"]
    # Approving a new capability needs manage; an editor cannot.
    gadgets.access.share(app, 2, EDIT)
    with pytest.raises(GadgetError):
        gadgets.service(2).publish(
            app["id"], {"revision_id": app["published_revision"], "if_version": app["version"]}
        )
    service.publish(
        app["id"], {"revision_id": app["published_revision"], "if_version": app["version"]}
    )
    assert "sessions.create" in service.content(app["id"])["capabilities"]


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
        "properties": {"cardId": {"type": "string"}, "state": {"type": "string"}},
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


def test_the_rule_state_rotates_without_losing_counts(
    gadgets: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(constraints_module, "EPOCH_WRITES", 3)
    service = gadgets.service()
    app = publish_ruled_board(service, [{**WIP_LIMIT, "max": 3}])
    todo = [card(service, app, name, "todo") for name in ("a", "b", "c", "d")]
    for index, document in enumerate(todo[:3]):
        move(service, app, document, "doing", f"m-{index}")
    with pytest.raises(ConstraintViolation):
        move(service, app, todo[3], "doing", "m-3")
    epochs = gadgets.store.gadget_files(app["id"]).list_paths("data/_guard/epochs")
    assert len(epochs) > 2


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
    assert owner.list_owned() == {"items": []}


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
    assert viewer.shelf() == {"pinned": [], "has_owned": False}

    assert viewer.pin(shared["id"], {"pinned": True})["pinned"] == [
        {"id": shared["id"], "name": "Shared"}
    ]
    # Pins are the viewer's own: the owner's shelf and gadget list do not change.
    assert owner.shelf() == {"pinned": [], "has_owned": True}
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
    assert owner.shared() == {"items": []}


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

    published = service.describe(publish_actions_board(service)["id"])
    assert "Nothing is published" not in published["next_steps"]
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

    with pytest.raises(InvalidInput) as refused:
        service.create_revision(app["id"], {"base_revision_id": base["id"], "edits": edits})

    assert str(refused.value).startswith(message)
    if reason is not None:
        assert refused.value.details["reason"] == reason


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
