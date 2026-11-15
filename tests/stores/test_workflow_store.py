"""Tests for the saved workflow store: ownership, names, pages, edits and limits."""

from __future__ import annotations

import random
import string
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from omnigent.db.db_models import MAX_STORED_VALUE_BYTES
from omnigent.entities.workflow import InvalidWorkflowError, Workflow
from omnigent.stores.workflow_store import WorkflowNameTakenError, WorkflowStore

ALICE = "alice@example.com"
BOB = "bob@example.com"
STEPS = [
    {"id": "build", "name": "Build"},
    {"id": "test", "name": "Test", "goal": "Run the suite", "depends_on": ["build"]},
]


@pytest.fixture()
def store(db_uri: str) -> WorkflowStore:
    return WorkflowStore(db_uri)


def _save(store: WorkflowStore, name: str, *, user_id: str | None = ALICE) -> Workflow:
    return store.create(user_id=user_id, name=name, description=f"Run {name}.", steps=STEPS)


def test_a_saved_workflow_reads_back_only_for_its_owner(store: WorkflowStore) -> None:
    saved = store.create(
        user_id=ALICE, name="  nightly \n build ", description=" Build main.\n", steps=STEPS
    )
    assert (saved.name, saved.description) == ("nightly build", "Build main.")
    assert saved.steps == [
        {"id": "build", "name": "Build", "goal": None, "depends_on": []},
        {"id": "test", "name": "Test", "goal": "Run the suite", "depends_on": ["build"]},
    ]
    assert saved.created_at == saved.updated_at
    assert store.get(saved.id, user_id=ALICE) == saved
    assert store.get(saved.id, user_id=BOB) is None
    assert store.get(saved.id, user_id=None) is None
    local = _save(store, "local", user_id=None)
    assert store.get(local.id, user_id=None) == local


@pytest.mark.parametrize("user_id", [ALICE, None])
def test_names_are_unique_per_owner(store: WorkflowStore, user_id: str | None) -> None:
    deploy = _save(store, "deploy", user_id=user_id)
    with pytest.raises(WorkflowNameTakenError):
        _save(store, "deploy", user_id=user_id)
    _save(store, "deploy", user_id=BOB)
    release = _save(store, "release", user_id=user_id)
    with pytest.raises(WorkflowNameTakenError):
        store.update(release.id, user_id=user_id, name="deploy")
    assert store.update(deploy.id, user_id=user_id, name="deploy") is not None


@pytest.mark.parametrize("user_id", [ALICE, None])
@pytest.mark.parametrize("operation", ["create", "rename", "mixed"])
def test_competing_saves_cannot_claim_the_same_name(
    store: WorkflowStore, db_uri: str, user_id: str | None, operation: str
) -> None:
    originals = [
        _save(store, f"original {index}", user_id=user_id)
        if operation == "rename" or (operation == "mixed" and index % 2)
        else None
        for index in range(4)
    ]
    ready = Barrier(len(originals))

    def save(index: int) -> Workflow | None:
        competing = WorkflowStore(db_uri)
        ready.wait(timeout=10)
        try:
            original = originals[index]
            if original is not None:
                return competing.update(original.id, user_id=user_id, name="shared name")
            return _save(competing, "shared name", user_id=user_id)
        except WorkflowNameTakenError:
            return None

    with ThreadPoolExecutor(max_workers=len(originals)) as pool:
        results = list(pool.map(save, range(len(originals))))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    page, has_more = store.list_page(user_id=user_id, name="shared name", limit=2)
    assert page == winners and not has_more


def test_pages_go_by_name_and_filter_by_name(store: WorkflowStore) -> None:
    for name in ("deploy-prod", "build", "deploy-staging", "audit", "cleanup"):
        _save(store, name)
    _save(store, "deploy-bob", user_id=BOB)
    names: list[str] = []
    after = None
    while True:
        page, has_more = store.list_page(user_id=ALICE, limit=2, after=after)
        assert all(
            [step["id"] for step in workflow.steps] == ["build", "test"] for workflow in page
        )
        names += [workflow.name for workflow in page]
        if not has_more:
            break
        after = page[-1].id
    assert names == ["audit", "build", "cleanup", "deploy-prod", "deploy-staging"]
    exact, _ = store.list_page(user_id=ALICE, limit=10, name="build")
    assert [workflow.name for workflow in exact] == ["build"]
    with pytest.raises(InvalidWorkflowError, match="one of your workflows"):
        store.list_page(user_id=BOB, limit=10, after=exact[0].id)
    with pytest.raises(InvalidWorkflowError, match="a workflow ID"):
        store.list_page(user_id=ALICE, limit=10, after="not-an-id")


def test_an_update_keeps_what_it_leaves_out_and_checks_the_result(store: WorkflowStore) -> None:
    saved = _save(store, "deploy")
    renamed = store.update(saved.id, user_id=ALICE, name="ship")
    assert renamed is not None
    assert (renamed.name, renamed.description, renamed.steps) == (
        "ship",
        saved.description,
        saved.steps,
    )
    replaced = store.update(saved.id, user_id=ALICE, steps=[{"id": "only", "name": "Only"}])
    assert replaced is not None and replaced.steps == [
        {"id": "only", "name": "Only", "goal": None, "depends_on": []}
    ]
    stored = store.get(saved.id, user_id=ALICE)
    assert stored is not None and stored.steps == replaced.steps
    cycle = [
        {"id": "a", "name": "A", "depends_on": ["b"]},
        {"id": "b", "name": "B", "depends_on": ["a"]},
    ]
    with pytest.raises(InvalidWorkflowError, match="cycle"):
        store.update(saved.id, user_id=ALICE, steps=cycle)
    with pytest.raises(InvalidWorkflowError, match="description must be valid Unicode text"):
        store.update(saved.id, user_id=ALICE, description="\ud800")
    assert store.get(saved.id, user_id=ALICE) == stored
    assert store.update(saved.id, user_id=BOB, name="mine") is None


def test_invalid_or_oversized_workflows_are_refused(store: WorkflowStore) -> None:
    with pytest.raises(InvalidWorkflowError, match="description must not be empty"):
        store.create(user_id=ALICE, name="x", description=" ", steps=STEPS)
    with pytest.raises(InvalidWorkflowError, match="at most 2000"):
        store.create(user_id=ALICE, name="x", description="d" * 2001, steps=STEPS)
    with pytest.raises(InvalidWorkflowError, match="description must be valid Unicode text"):
        store.create(user_id=ALICE, name="x", description="\ud800", steps=STEPS)
    with pytest.raises(InvalidWorkflowError, match="unknown fields: agent"):
        store.create(
            user_id=ALICE,
            name="x",
            description="d",
            steps=[{"id": "a", "name": "A", "agent": "x"}],
        )
    # Random text barely compresses: within the raw plan limit, but not the stored one.
    noise = random.Random(7)
    steps = [
        {
            "id": f"s{index}",
            "name": f"Step {index}",
            "goal": "".join(noise.choices(string.ascii_letters + string.digits, k=300)),
        }
        for index in range(90)
    ]
    with pytest.raises(InvalidWorkflowError, match=f"limit is {MAX_STORED_VALUE_BYTES}"):
        store.create(user_id=ALICE, name="big", description="d", steps=steps)
    assert store.list_page(user_id=ALICE, limit=10) == ([], False)


def test_a_workflow_deletes_once_and_only_for_its_owner(store: WorkflowStore) -> None:
    saved = _save(store, "deploy")
    assert store.delete(saved.id, user_id=BOB) is False
    assert store.delete(saved.id, user_id=ALICE) is True
    assert store.delete(saved.id, user_id=ALICE) is False
    assert store.get(saved.id, user_id=ALICE) is None


def test_description_storage_is_capped_at_2000_bytes(store: WorkflowStore) -> None:
    saved = store.create(user_id=ALICE, name="review", description="d" * 2000, steps=STEPS)
    noise = random.Random(0)
    description = "".join(chr(noise.randrange(0x4E00, 0x9FFF)) for _ in range(2000))
    with pytest.raises(InvalidWorkflowError, match=r"description.*limit is 2000"):
        store.create(user_id=ALICE, name="oversized", description=description, steps=STEPS)
    with pytest.raises(InvalidWorkflowError, match=r"description.*limit is 2000"):
        store.update(saved.id, user_id=ALICE, description=description)
    assert store.get(saved.id, user_id=ALICE) == saved
    assert store.list_page(user_id=ALICE, limit=10) == ([saved], False)


def test_a_draft_becomes_runnable_when_its_plan_is_saved(store: WorkflowStore) -> None:
    draft = store.create(user_id=ALICE, name="draft", description="Plan a release.", steps=[])
    assert draft.steps == []
    assert store.get(draft.id, user_id=ALICE) == draft
    page, _ = store.list_page(user_id=ALICE, limit=10)
    assert page == [draft]

    renamed = store.update(draft.id, user_id=ALICE, name="release")
    assert renamed is not None and renamed.steps == []
    saved = store.update(draft.id, user_id=ALICE, steps=STEPS)
    assert saved is not None and saved.steps
    assert store.get(draft.id, user_id=ALICE) == saved
    assert store.list_page(user_id=ALICE, limit=10)[0] == [saved]
