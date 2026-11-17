"""Tests for the saved workflow store: ownership, names, pages, edits and limits."""

from __future__ import annotations

import random
import string
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import Update, event, select

from omnigent.db import compression
from omnigent.db.db_models import MAX_STORED_VALUE_BYTES, SqlPreference, SqlWorkflow
from omnigent.db.utils import get_or_create_engine
from omnigent.entities.workflow import InvalidWorkflowError, Workflow
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.workflow_store import (
    WorkflowNameTakenError,
    WorkflowStore,
    workflow_page_cursor,
)

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
    assert local.user_id == RESERVED_USER_LOCAL
    assert store.get(local.id, user_id=None) == local
    assert store.get(local.id, user_id=RESERVED_USER_LOCAL) == local
    with get_or_create_engine(store.storage_location).connect() as connection:
        assert (
            connection.execute(
                select(SqlWorkflow.user_id).where(SqlWorkflow.id == local.id)
            ).scalar_one()
            == RESERVED_USER_LOCAL
        )
        assert (
            connection.execute(
                select(SqlPreference.key)
                .where(SqlPreference.user_id == "__workflow_owners__")
                .limit(1)
            ).first()
            is None
        )


@pytest.mark.parametrize("user_id", [ALICE, None])
def test_names_are_unique_per_owner(store: WorkflowStore, user_id: str | None) -> None:
    deploy = _save(store, "deploy", user_id=user_id)
    with pytest.raises(WorkflowNameTakenError):
        _save(store, "deploy", user_id=user_id)
    _save(store, "deploy", user_id=BOB)
    release = _save(store, "release", user_id=user_id)
    with pytest.raises(WorkflowNameTakenError):
        store.update(release.id, user_id=user_id, name="deploy")
    assert store.get(release.id, user_id=user_id) == release
    assert store.update(deploy.id, user_id=user_id, name="deploy") is not None
    if get_or_create_engine(store.storage_location).dialect.name == "mysql":
        with pytest.raises(WorkflowNameTakenError):
            _save(store, "Deploy", user_id=user_id)
    else:
        assert _save(store, "Deploy", user_id=user_id).name == "Deploy"


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


@pytest.mark.parametrize("user_id", [ALICE, None])
def test_pages_go_by_name_and_filter_by_name(store: WorkflowStore, user_id: str | None) -> None:
    for name in ("deploy-prod", "build", "deploy-staging", "audit", "cleanup"):
        _save(store, name, user_id=user_id)
    _save(store, "deploy-bob", user_id=BOB)
    names: list[str] = []
    after = None
    while True:
        page, has_more = store.list_page(user_id=user_id, limit=2, after=after)
        assert all(
            [step["id"] for step in workflow.steps] == ["build", "test"] for workflow in page
        )
        names += [workflow.name for workflow in page]
        if not has_more:
            break
        after = workflow_page_cursor(page[-1])
    assert names == ["audit", "build", "cleanup", "deploy-prod", "deploy-staging"]
    exact, _ = store.list_page(user_id=user_id, limit=10, name="build")
    assert [workflow.name for workflow in exact] == ["build"]
    with pytest.raises(InvalidWorkflowError, match="one of your workflows"):
        store.list_page(user_id=BOB, limit=10, after=exact[0].id)
    with pytest.raises(InvalidWorkflowError, match="a workflow ID"):
        store.list_page(user_id=user_id, limit=10, after="not-an-id")
    with pytest.raises(InvalidWorkflowError, match="a workflow cursor"):
        store.list_page(user_id=user_id, limit=10, after="name:!!")
    remaining, _ = store.list_page(user_id=user_id, limit=10, after=exact[0].id)
    assert [workflow.name for workflow in remaining] == [
        "cleanup",
        "deploy-prod",
        "deploy-staging",
    ]
    cursor = workflow_page_cursor(exact[0])
    renamed = store.update(exact[0].id, user_id=user_id, name="zz-build")
    assert renamed is not None and renamed.name == "zz-build"
    following, _ = store.list_page(user_id=user_id, limit=2, after=cursor)
    assert [workflow.name for workflow in following] == ["cleanup", "deploy-prod"]
    store.delete(exact[0].id, user_id=user_id)
    following, has_more = store.list_page(user_id=user_id, limit=10, after=cursor)
    assert following == remaining and not has_more


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


def test_a_description_written_meanwhile_outlives_a_stale_replacement(
    store: WorkflowStore,
) -> None:
    saved = _save(store, "deploy")
    assert store.update(saved.id, user_id=ALICE, description="Edited elsewhere.") is not None
    kept = store.update(
        saved.id,
        user_id=ALICE,
        description="From an older proposal.",
        expected_description=saved.description,
        steps=[{"id": "only", "name": "Only"}],
    )
    assert kept is not None and kept.description == "Edited elsewhere."
    assert kept.steps == [{"id": "only", "name": "Only", "goal": None, "depends_on": []}]
    replaced = store.update(
        saved.id,
        user_id=ALICE,
        description="From the newest proposal.",
        expected_description="Edited elsewhere.",
    )
    assert replaced is not None and replaced.description == "From the newest proposal."
    assert store.get(saved.id, user_id=ALICE) == replaced


def test_a_description_keeps_its_proposal_until_written_otherwise(store: WorkflowStore) -> None:
    saved = _save(store, "deploy")
    assert saved.description_source is None
    proposal, newer = "a" * 32, "b" * 32
    proposed = store.update(
        saved.id, user_id=ALICE, description="From a proposal.", description_source=proposal
    )
    assert proposed is not None and proposed.description_source == proposal
    kept = store.update(saved.id, user_id=ALICE, steps=[{"id": "only", "name": "Only"}])
    assert kept is not None and kept.description_source == proposal
    stale = store.update(
        saved.id,
        user_id=ALICE,
        description="From a stale page.",
        expected_description="Not the description.",
        description_source=newer,
    )
    assert stale is not None
    assert (stale.description, stale.description_source) == ("From a proposal.", proposal)
    edited = store.update(saved.id, user_id=ALICE, description="From a proposal.")
    assert edited is not None and edited.description_source is None
    assert store.get(saved.id, user_id=ALICE) == edited
    # A new planning chat's journal holds none of the old chat's proposals.
    store.update(saved.id, user_id=ALICE, description="Again.", description_source=proposal)
    replanned = store.update(saved.id, user_id=ALICE, planning_session_id="c" * 32)
    assert replanned is not None and replanned.description_source is None
    # A description saved with the new chat keeps the source it names.
    moved = store.update(
        saved.id,
        user_id=ALICE,
        description="From the new chat.",
        description_source=newer,
        planning_session_id="d" * 32,
    )
    assert moved is not None and moved.description_source == newer


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


class _SealedWorkflows(WorkflowStore):
    """A fake key service, using reversible bytes to exercise the batch codec contract."""

    def __init__(self, storage_location: str, *, overheads: tuple[int, int] = (4, 4)) -> None:
        super().__init__(storage_location)
        self.overheads = overheads
        self.calls: list[tuple[str, int]] = []

    def _encode_texts(self, texts: Sequence[str]) -> list[bytes]:
        self.calls.append(("encrypt", len(texts)))
        return [
            compression.encode_encrypted(
                compression.EncryptedTextPayload(
                    text.encode()[::-1], {"key": "k" * self.overheads[index % 2]}
                )
            )
            for index, text in enumerate(texts)
        ]

    def _decode_texts(self, stored: Sequence[bytes]) -> list[bytes | str]:
        self.calls.append(("decrypt", len(stored)))
        return [
            (
                compression.decode(
                    value, decrypt=lambda payload: payload.ciphertext[::-1].decode()
                )
                or ""
            ).encode()
            for value in stored
        ]


def test_a_deployment_encodes_workflow_texts_a_page_at_a_time(db_uri: str) -> None:
    plain = WorkflowStore(db_uri)
    sealed = _SealedWorkflows(db_uri)
    first = _save(plain, "a")
    second = sealed.create(user_id=ALICE, name="b", description="Résumé du travail.", steps=STEPS)
    third = _save(sealed, "c")
    assert sealed.calls == [("encrypt", 2), ("encrypt", 2)]

    sealed.calls.clear()
    assert sealed.list_page(user_id=ALICE, limit=2) == ([first, second], True)
    assert sealed.calls == [("decrypt", 4)]
    sealed.calls.clear()
    assert sealed.list_page(user_id=ALICE, limit=2, after=second.id) == ([third], False)
    assert sealed.calls == [("decrypt", 2)]
    sealed.calls.clear()
    assert sealed.get(second.id, user_id=ALICE) == second
    assert sealed.calls == [("decrypt", 2)]

    engine = get_or_create_engine(db_uri)
    updates: list[set[str]] = []

    def capture_update(_connection, _cursor, _statement, _parameters, context, _executemany):
        compiled = context.compiled
        query = getattr(compiled, "statement", None)
        if isinstance(query, Update) and query.table.name == "workflows":
            updates.append(set(compiled.params))

    sealed.calls.clear()
    event.listen(engine, "before_cursor_execute", capture_update)
    try:
        updated = sealed.update(second.id, user_id=ALICE, description="Updated résumé.")
    finally:
        event.remove(engine, "before_cursor_execute", capture_update)
    assert updated is not None and updated.description == "Updated résumé."
    assert updated.steps == second.steps
    assert sealed.calls == [("decrypt", 2), ("encrypt", 1)]
    assert len(updates) == 1 and "plan" not in updates[0]
    assert sealed.get(second.id, user_id=ALICE) == updated

    sealed.calls.clear()
    replanned = sealed.update(second.id, user_id=ALICE, steps=[{"id": "only", "name": "Only"}])
    assert sealed.calls == [("decrypt", 2), ("encrypt", 2)]
    assert replanned is not None and [step["id"] for step in replanned.steps] == ["only"]
    assert sealed.get(second.id, user_id=ALICE) == replanned
    with engine.connect() as connection:
        stored = connection.execute(
            select(SqlWorkflow.description, SqlWorkflow.plan).where(SqlWorkflow.id == second.id)
        ).one()
        assert [value[:2] for value in stored] == [b"\x00\x02", b"\x00\x02"]
    with pytest.raises(ValueError, match="decryptor"):
        plain.get(second.id, user_id=ALICE)
    with pytest.raises(ValueError, match="decryptor"):
        plain.list_page(user_id=ALICE, name="b", limit=1)


@pytest.mark.parametrize(
    ("field", "limit", "index"), [("description", 2000, 0), ("steps", 16_383, 1)]
)
def test_workflow_limits_count_the_encrypted_envelope(
    db_uri: str, field: str, limit: int, index: int
) -> None:
    sealed = _SealedWorkflows(db_uri)
    saved = _save(sealed, "saved")
    overheads = [4, 4]
    overheads[index] = limit
    bloated = _SealedWorkflows(db_uri, overheads=(overheads[0], overheads[1]))
    with pytest.raises(InvalidWorkflowError, match=rf"{field}:.*limit is {limit}"):
        _save(bloated, "too large")
    with pytest.raises(InvalidWorkflowError, match=rf"{field}:.*limit is {limit}"):
        bloated.update(saved.id, user_id=ALICE, description="A new description.", steps=STEPS)
    assert sealed.get(saved.id, user_id=ALICE) == saved
