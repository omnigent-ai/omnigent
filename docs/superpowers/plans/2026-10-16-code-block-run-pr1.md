# Code Block Run PR 1 (Saved Item Type and Migration) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `code_run` conversation item type (type code 12) and the migration that lets the database store it.

**Architecture:** One pydantic data class, `CodeRunData`, carries both phases of a run (`started` and `finished`), mirroring how `TerminalCommandData` carries `input` and `output` records under one type. It is registered in every item-type registry; the import-time codec guard and the id/data parity test keep those registries in lock-step. One Alembic migration widens `ck_conversation_items_type` with the batch-operation recipe from `u1a2b3c4d5e6_enums_varchar_to_smallint`.

**Tech Stack:** Python 3, pydantic v2, SQLAlchemy, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-code-block-run-design.md` (section "Saved items" and its "Migration" subsection; "Testing" bullets "Entities and codecs" and "Migration").

## Global Constraints

- Type name `code_run`, type code `12`. Codes are append-only; never renumber.
- `started` fields: `run_id`, `phase: "started"`, `message_id`, `block_hash`, `language`, `command`, `timeout`, `approved_by` (nullable), `started_at`.
- `finished` fields: `run_id`, `phase: "finished"`, `status` (`exited`, `stopped`, `timed_out`, `failed`), `exit_code` (nullable), `duration_ms`, `output` as a list of `{stream: "stdout" | "stderr", text}` segments capped at 100,000 characters total, `truncated`.
- `code_run` is in `NON_CONTENT_ITEM_TYPES`; it never enters model history directly.
- Search text: command plus the start of the output.
- One migration, single Alembic head, upgrade and downgrade pass on SQLite and Postgres.
- Out of scope (PRs 2 to 4): runner code, routes, events, feature flag, agent delivery, web UI. Truncation itself (keeping start and end) is the runner's job in PR 2; this PR only rejects over-cap payloads.
- Every commit `git commit -s`. No em-dash characters anywhere.

## File Structure

- Modify `omnigent/entities/conversation.py`: `CodeRunOutputSegment`, `CodeRunData`, `CODE_RUN_OUTPUT_MAX_CHARS`, registry entries.
- Modify `omnigent/entities/__init__.py`: export the new names.
- Modify `omnigent/db/enum_codecs.py`: `"code_run": 12`.
- Modify `omnigent/db/utils.py`: `_ITEM_TYPES` entry and `extract_search_text` branch.
- Modify `omnigent/db/db_models.py`: CHECK widened to 12.
- Create `omnigent/db/migrations/versions/nn1a2b3c4d5e_conversation_items_type_code_run.py`.
- Regenerate `openapi.json` (the item `data` union is part of the API schema; `tests/server/test_openapi_drift.py` guards it).
- Tests: `tests/entities/test_conversation_extended.py`, `tests/db/test_enum_codecs.py`, `tests/db/test_utils.py`, new `tests/db/test_migration_conversation_items_type_code_run.py`.

---

### Task 1: `CodeRunData` and item-type registration

**Files:**
- Modify: `omnigent/entities/conversation.py` (after `TerminalCommandData`; `ItemData`, `ITEM_TYPE_TO_DATA_CLS`, `NON_CONTENT_ITEM_TYPES`)
- Modify: `omnigent/entities/__init__.py`
- Modify: `omnigent/db/enum_codecs.py` (`ITEM_TYPE`)
- Modify: `omnigent/db/utils.py` (`_ITEM_TYPES`)
- Regenerate: `openapi.json`
- Test: `tests/entities/test_conversation_extended.py`, `tests/db/test_enum_codecs.py`

These registrations ship together: the codec guard raises at import if `ITEM_TYPE` and `ITEM_TYPE_TO_DATA_CLS` diverge, and `test_item_type_id_and_data_registries_cover_the_same_types` fails if `_ITEM_TYPES` diverges.

**Interfaces:**
- Produces: `CodeRunOutputSegment(stream: Literal["stdout", "stderr"], text: str)`; `CodeRunData` with the fields above (`timeout: int` seconds, `started_at: int` Unix epoch seconds, `duration_ms: int`, `output: list[CodeRunOutputSegment]`, `truncated: bool`); `CODE_RUN_OUTPUT_MAX_CHARS = 100_000`. Phase-specific fields are optional on the model; a validator requires the fields of the declared phase.

- [ ] **Step 1: Write the failing tests**

In `tests/entities/test_conversation_extended.py`, import `CodeRunData`, `CODE_RUN_OUTPUT_MAX_CHARS`, add `"code_run"` to the expected sets in `test_non_content_item_types_complete` and `test_item_type_map_covers_all_types`, and add:

```python
def test_code_run_started() -> None:
    data = CodeRunData(
        run_id="run_1", phase="started", message_id="msg_1", block_hash="abc",
        language="bash", command="ls", timeout=60, started_at=1753900000,
    )
    assert data.approved_by is None
    assert data.status is None


def test_code_run_finished() -> None:
    data = CodeRunData.model_validate(
        {
            "run_id": "run_1", "phase": "finished", "status": "exited", "exit_code": 0,
            "duration_ms": 12, "truncated": False,
            "output": [{"stream": "stdout", "text": "hi\n"}, {"stream": "stderr", "text": "w"}],
        }
    )
    assert [segment.stream for segment in data.output] == ["stdout", "stderr"]


def test_code_run_phase_requires_its_fields() -> None:
    with pytest.raises(ValidationError, match="command"):
        CodeRunData(run_id="run_1", phase="started")
    with pytest.raises(ValidationError, match="status"):
        CodeRunData(run_id="run_1", phase="finished")


def test_code_run_rejects_unknown_status_and_stream() -> None:
    with pytest.raises(ValidationError):
        CodeRunData(run_id="r", phase="finished", status="crashed", duration_ms=1)
    with pytest.raises(ValidationError):
        CodeRunData.model_validate(
            {"run_id": "r", "phase": "finished", "status": "exited", "duration_ms": 1,
             "output": [{"stream": "stdin", "text": "x"}]}
        )


def test_code_run_rejects_output_over_cap() -> None:
    CodeRunData.model_validate(
        {"run_id": "r", "phase": "finished", "status": "exited", "duration_ms": 1,
         "output": [{"stream": "stdout", "text": "x" * CODE_RUN_OUTPUT_MAX_CHARS}]}
    )
    with pytest.raises(ValidationError, match="100000"):
        CodeRunData.model_validate(
            {"run_id": "r", "phase": "finished", "status": "exited", "duration_ms": 1,
             "output": [{"stream": "stdout", "text": "x" * CODE_RUN_OUTPUT_MAX_CHARS},
                        {"stream": "stderr", "text": "y"}]}
        )
```

In `tests/db/test_enum_codecs.py::test_shipped_codes_are_stable`, add `"code_run": 12` to the `ITEM_TYPE` dict.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/entities/test_conversation_extended.py tests/db/test_enum_codecs.py -q`
Expected: collection ImportError for `CodeRunData`.

- [ ] **Step 3: Implement**

In `omnigent/entities/conversation.py`, after `TerminalCommandData`:

```python
CODE_RUN_OUTPUT_MAX_CHARS = 100_000


class CodeRunOutputSegment(BaseModel):
    """One contiguous chunk of a code run's output from a single stream."""

    stream: Literal["stdout", "stderr"]
    text: str


class CodeRunData(BaseModel):
    """
    Data payload for a chat code block run by the session owner.
    (docstring documents every field and phase)
    """

    run_id: str
    phase: Literal["started", "finished"]
    message_id: str | None = None
    block_hash: str | None = None
    language: str | None = None
    command: str | None = None
    timeout: int | None = None
    approved_by: str | None = None
    started_at: int | None = None
    status: Literal["exited", "stopped", "timed_out", "failed"] | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    output: list[CodeRunOutputSegment] = Field(default_factory=list)
    truncated: bool = False

    @model_validator(mode="after")
    def check_phase_fields(self) -> CodeRunData:
        required = _CODE_RUN_PHASE_FIELDS[self.phase]
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError(f"code_run {self.phase} item requires {', '.join(missing)}")
        total = sum(len(segment.text) for segment in self.output)
        if total > CODE_RUN_OUTPUT_MAX_CHARS:
            raise ValueError(f"code_run output exceeds {CODE_RUN_OUTPUT_MAX_CHARS} characters")
        return self


_CODE_RUN_PHASE_FIELDS = {
    "started": ("message_id", "block_hash", "language", "command", "timeout", "started_at"),
    "finished": ("status", "duration_ms"),
}
```

Add `| CodeRunData` to `ItemData`, `"code_run": CodeRunData` to `ITEM_TYPE_TO_DATA_CLS`, `"code_run"` to `NON_CONTENT_ITEM_TYPES`, export `CODE_RUN_OUTPUT_MAX_CHARS`, `CodeRunData`, `CodeRunOutputSegment` from `omnigent/entities/__init__.py` (import list and `__all__`). Add `"code_run": 12` to `ITEM_TYPE` in `omnigent/db/enum_codecs.py` and `"code_run"` to `_ITEM_TYPES` in `omnigent/db/utils.py`.

Regenerate the API schema: `uv run python scripts/dump_openapi.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/entities/test_conversation_extended.py tests/entities/test_entities.py tests/db/test_enum_codecs.py tests/db/test_utils.py tests/db/test_utils_extended.py tests/server/test_openapi_drift.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/entities/conversation.py omnigent/entities/__init__.py omnigent/db/enum_codecs.py omnigent/db/utils.py openapi.json tests/entities/test_conversation_extended.py tests/db/test_enum_codecs.py
git commit -s -m "feat(entities): add code_run conversation item type"
```

### Task 2: Search text for `code_run`

**Files:**
- Modify: `omnigent/db/utils.py` (`extract_search_text`)
- Test: `tests/db/test_utils.py`

**Interfaces:**
- Consumes: `CodeRunData` from Task 1.
- Produces: `extract_search_text` returns the command for a `started` item and the first 1,000 characters of joined output text for a `finished` item (the finished phase carries no command, so the pair of items together indexes command plus the start of the output).

- [ ] **Step 1: Write the failing tests**

```python
def test_extract_search_text_for_code_run_started() -> None:
    item = NewConversationItem(
        type="code_run", response_id="resp_1",
        data=CodeRunData(run_id="r", phase="started", message_id="m", block_hash="h",
                         language="bash", command="echo hello", timeout=60, started_at=1),
    )
    assert extract_search_text(item) == "echo hello"


def test_extract_search_text_for_code_run_finished_keeps_output_start() -> None:
    item = NewConversationItem(
        type="code_run", response_id="resp_1",
        data=CodeRunData.model_validate(
            {"run_id": "r", "phase": "finished", "status": "exited", "duration_ms": 1,
             "output": [{"stream": "stdout", "text": "hello "},
                        {"stream": "stderr", "text": "x" * 5000}]}
        ),
    )
    text = extract_search_text(item)
    assert text.startswith("hello x")
    assert len(text) == 1000
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_utils.py -q -k code_run`
Expected: FAIL with `ValueError: unknown item type: 'code_run'`.

- [ ] **Step 3: Implement** (before the final `raise` in `extract_search_text`)

```python
    if item.type == "code_run":
        # Index the command (started) and the start of the output (finished)
        # so FTS can find a run by what was run or what it printed.
        if data["command"]:
            return data["command"]
        return "".join(segment["text"] for segment in data["output"])[:_CODE_RUN_SEARCH_CHARS]
```

with `_CODE_RUN_SEARCH_CHARS = 1_000` near the function. Add `"code_run"` to the docstring's list of types.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/db/test_utils.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/db/utils.py tests/db/test_utils.py
git commit -s -m "feat(db): index code_run command and output start for search"
```

### Task 3: Migration widening `ck_conversation_items_type`

**Files:**
- Create: `omnigent/db/migrations/versions/nn1a2b3c4d5e_conversation_items_type_code_run.py` (revision `nn1a2b3c4d5e`, down_revision `mm1a2b3c4d5e`, the current single head)
- Modify: `omnigent/db/db_models.py` (`ck_conversation_items_type` CHECK adds 12)
- Test: `tests/db/test_migration_conversation_items_type_code_run.py`

**Interfaces:**
- Consumes: type code 12 from Task 1.
- Produces: schema where `conversation_items.type = 12` is accepted.

Migration body follows `u1a2b3c4d5e6`: `batch_alter_table(table, recreate="always" if sqlite else "auto")`, `drop_constraint("ck_conversation_items_type", type_="check")`, `create_check_constraint(...)`, codes pinned in the migration. Downgrade deletes `type = 12` rows before restoring the narrower CHECK (older code cannot decode code 12, and Postgres would refuse the narrower CHECK while such rows exist); the docstring states this.

- [ ] **Step 1: Write the failing test** (SQLite always; Postgres when `OMNIGENT_TEST_DB_URI` is a `postgresql` URL, same skip pattern as `tests/db/test_migration_agents_owner_index.py`)

Tests: at `mm1a2b3c4d5e` inserting type 12 raises `IntegrityError`; after upgrade to `nn1a2b3c4d5e` it succeeds, type 13 still raises, and the `conversation_items` indexes and primary key are unchanged by the rebuild; after downgrade the type-12 row is gone, other rows remain, and type 12 is rejected again.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_migration_conversation_items_type_code_run.py -q`
Expected: FAIL (revision `nn1a2b3c4d5e` unknown).

- [ ] **Step 3: Implement** the migration and the `db_models.py` CHECK.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/db/test_migration_conversation_items_type_code_run.py tests/db -q`, then the same test file with `OMNIGENT_TEST_DB_URI=postgresql://...` against a throwaway Postgres 16 container, and `uv run alembic -c omnigent/db/alembic.ini heads` showing one head.

- [ ] **Step 5: Commit**

```bash
git add omnigent/db/migrations/versions/nn1a2b3c4d5e_conversation_items_type_code_run.py omnigent/db/db_models.py tests/db/test_migration_conversation_items_type_code_run.py
git commit -s -m "feat(db): allow code_run item type code in conversation_items"
```

## Spec Coverage

| Spec requirement | Task |
| --- | --- |
| `code_run`, code 12, started/finished fields, segments, truncated, 100,000 cap | 1 |
| `ITEM_TYPE_TO_DATA_CLS`, `ItemData`, `NON_CONTENT_ITEM_TYPES`, item id validation, `ITEM_TYPE` codec | 1 |
| Search text: command plus start of output | 2 |
| Migration widening CHECK, batch recipe, single head, SQLite and Postgres | 3 |
| Testing: entities and codecs; migration | 1, 2, 3 |
