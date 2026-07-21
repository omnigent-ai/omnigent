# Database Schema Design

Five tables in the default schema. DBOS manages its own tables (workflow_status,
operation_outputs, streams, etc.) in a separate `dbos` schema within the same database.

Tasks and conversation_items MUST share the same database — the steering handshake
(try_deliver + close_inbox) requires single-transaction atomicity.

Schema is managed by Alembic migrations in `alembic/`. SQLAlchemy models live
in `omnigent/db/db_models.py`.

---

## agents

| Column | Type | Notes |
|---|---|---|
| id | String(64) PK | "ag_" + uuid4().hex |
| created_at | Integer NOT NULL | Unix epoch seconds |
| name | String(256) UNIQUE NOT NULL | Used as `model` in inference requests |
| description | Text | nullable |

**Indexes:** `uq_agents_name` (unique on name), `ix_agents_created_at`

---

## files

| Column | Type | Notes |
|---|---|---|
| id | String(64) PK | "file_" + uuid4().hex |
| created_at | Integer NOT NULL | |
| filename | String(512) NOT NULL | Original filename |
| bytes | Integer NOT NULL | File size |
| content_type | String(256) | MIME type, nullable |

**Indexes:** `ix_files_created_at`

---

## conversations

| Column | Type | Notes |
|---|---|---|
| id | String(64) PK | "conv_" + uuid4().hex |
| created_at | Integer NOT NULL | |
| title | Text | nullable, user-settable conversation title |

**Indexes:** `ix_conversations_archived_updated` (backs the default sidebar list)

---

## tasks

Responses. `task_id` = `response_id` = DBOS `workflow_uuid`.

This table stores relationship/identity columns, display dimensions, and the steering
handshake flag. All execution state — status, output, error, usage, etc. — lives in
DBOS (workflow inputs for request params, workflow result for outcomes).
`TaskStore.get()` assembles the full `Task` entity from both the DB row and DBOS state.
If we later need to query by any DBOS-managed field (e.g. filter tasks by status), we
can promote it to a column here.

| Column | Type | Notes |
|---|---|---|
| id | String(64) PK | "resp_" + uuid4().hex (= DBOS workflow_uuid) |
| agent_id | String(64) NOT NULL | FK → agents.id |
| conversation_id | String(64) NOT NULL | FK → conversations.id |
| previous_response_id | String(64) | No FK — allows dangling after mid-chain delete |
| created_at | Integer NOT NULL | |
| inbox_closed | Integer NOT NULL | Default 0 (0=open, 1=closed) |
| agent_name | String(256) NOT NULL | Denormalized — stable model name even if agent is renamed/deleted |
| background | Boolean NOT NULL | Default 0 — display dimension for API responses |

**Stored in DBOS** (workflow inputs): `instructions`, `reasoning`

**Stored in DBOS** (workflow result): `status`, `output`, `completed_at`, `error`,
`incomplete_details`, `usage`

**Indexes:** `ix_tasks_conversation_id`, `ix_tasks_agent_id` (for agent deletion cascade),
`ix_tasks_created_at`

### Task/workflow invariant

For any task, both a `tasks` table row AND a DBOS `workflow_status` entry must exist,
OR NEITHER. This is enforced by a compensating transaction in `TaskStore.start()`:

1. `create()` writes the task row and commits.
2. `start()` launches the DBOS workflow. On failure, it deletes the orphaned row.
3. If the process crashes between `create()` committing and `start()` succeeding,
   a reaper on startup can detect orphaned rows (rows with no matching DBOS workflow)
   and clean them up.

---

## conversation_items

Conversation items — messages, function calls, function call outputs, reasoning, etc.
Single table with a `type` discriminator and a JSON `data` blob for type-specific fields.

| Column | Type | Notes |
|---|---|---|
| id | String(64) PK | Prefixed by type: msg_, fc_, fco_, rs_ |
| conversation_id | String(64) NOT NULL | FK → conversations.id |
| response_id | String(64) NOT NULL | References tasks.id; no FK — must survive task deletion |
| created_at | Integer NOT NULL | |
| status | String(32) NOT NULL | Default "completed" |
| position | Integer NOT NULL | Ordering within conversation |
| type | String(32) NOT NULL | message, function_call, function_call_output, reasoning |
| data | Text NOT NULL | JSON blob — type-specific fields (see below) |
| search_text | Text NOT NULL | Extracted plain text for full-text search (see below) |

**Indexes:** `ix_conversation_items_conversation_id_position` (composite), `ix_conversation_items_response_id`

### data column by type

**message:** `{"role": "user", "content": [{"type": "input_text", "text": "..."}]}`

**function_call:** `{"name": "get_weather", "arguments": "{...}", "call_id": "call_001"}`

**function_call_output:** `{"call_id": "call_001", "output": "{...}"}`

**reasoning:** `{"summary": [...], "content": null, "encrypted_content": null}`

---

## Design Decisions

### Foreign key strategy

`conversation_id` on both tasks and conversation_items has a FK to `conversations.id`.
This is safe because the deletion order (tasks → conversation_items → conversation)
always removes children before the parent. The FK acts as a safety net against
orphaned rows.

`tasks.agent_id → agents.id` — FK. Agent deletion handler cancels tasks,
deletes task records, then deletes the agent row.

No FK for these relationships:
- `conversation_items.response_id → tasks.id` — items must survive task deletion
- `tasks.previous_response_id → tasks.id` — dangling pointers are allowed after mid-chain delete

### Single conversation_items table with JSON data column

We never filter by item-internal fields — all queries are by conversation_id, response_id,
or position. A discriminated union via `type` + JSON is simpler than separate tables per
item type and extends to future item types (compaction, mcp_tool_call, etc.) without
schema changes.

### position for item ordering

App-managed integer, assigned via `SELECT MAX(position) + 1` within the same
transaction as the INSERT. Guarantees strict, gapless ordering within each conversation.

Why not alternatives:
- **Autoincrement**: global, not per-conversation — creates gaps and arbitrary numbers across conversations.
- **Timestamps**: ties are possible when multiple items are inserted in one transaction (e.g., batch append).
- **Time-sortable IDs (ULID/UUIDv7)**: our type-prefixed IDs (`msg_`, `fc_`) break lexicographic sorting.
- **Compute on read** (`ROW_NUMBER()`): slower reads, makes cursor pagination ugly.

The SELECT + INSERT cost is negligible since the steering handshake already requires
a transaction. Cursor pagination is clean: `WHERE conversation_id = ? AND position > ?`.

### TEXT for JSON, Integer for booleans

Portable across SQLite and PostgreSQL. Application-level json.loads/json.dumps.
SQLite stores Boolean as INTEGER internally, so Integer(0/1) avoids ORM coercion
differences.

### agent (model) lives in the data blob, not as a column

The `agent`/`model` field is already type-specific inside the JSON `data` blob for
item types that need it (assistant messages, function calls, reasoning). No queries
filter conversation_items by model, so a top-level column would be redundant.

### Full-text search on conversation items

Search needs to work within a single conversation and across all conversations.
The searchable content lives inside the JSON `data` blob, so we extract it into
a dedicated `search_text` column at write time and index that column for FTS.

#### search_text extraction

Populated by `ConversationStore.append()` before inserting. Extraction by item type:

- **message**: concatenate all `text` values from the `content` array
  (input_text, output_text entries)
- **function_call**: `"{name} {arguments}"` — the function name and its arguments
- **function_call_output**: the `output` value
- **reasoning**: concatenate all `text` values from the `summary` array

This is a shared code path — both backends populate the same `search_text` column.

#### Backend-specific indexing

**PostgreSQL — tsvector + GIN index:**

```sql
ALTER TABLE conversation_items
  ADD COLUMN search_vector tsvector
  GENERATED ALWAYS AS (to_tsvector('english', search_text)) STORED;

CREATE INDEX ix_conversation_items_search
  ON conversation_items USING GIN (search_vector);
```

Queries use `tsquery`:
```sql
SELECT * FROM conversation_items
WHERE conversation_id = :conv_id
  AND search_vector @@ plainto_tsquery('english', :query)
ORDER BY ts_rank(search_vector, plainto_tsquery('english', :query)) DESC;
```

**SQLite — FTS5 virtual table:**

```sql
CREATE VIRTUAL TABLE conversation_items_fts USING fts5(
  search_text,
  content='conversation_items',
  content_rowid='rowid'
);
```

Kept in sync via triggers on INSERT/DELETE against conversation_items.
Queries use `MATCH`:
```sql
SELECT ci.* FROM conversation_items ci
JOIN conversation_items_fts fts ON ci.rowid = fts.rowid
WHERE ci.conversation_id = :conv_id
  AND fts.search_text MATCH :query
ORDER BY fts.rank;
```

#### Store layer abstraction

`ConversationStore` exposes a single `search()` method:

```python
def search(
    self,
    query: str,
    conversation_id: str | None = None,
    limit: int = 20,
) -> list[ConversationItem]:
```

The SQLAlchemy store implementation detects the backend at init time
(`engine.dialect.name`) and dispatches to the appropriate query. The caller
never knows which FTS engine is running underneath.

On Postgres, the `search_vector` generated column is automatic — no extra
write-time work beyond populating `search_text`. On SQLite, the FTS5 virtual
table and its sync triggers are created during `ConversationBase.metadata.create_all()`
(the conversations table lives on the Conversation base) via an `after_create`
DDL event listener.

---

## Store Method → DB Operation Mapping

### TaskStore

| Method | DB Operation |
|---|---|
| `create(conversation_id, agent_id, agent_name, ...)` | INSERT INTO tasks (no instructions/reasoning — those are workflow inputs) |
| `start(task_id, instructions, reasoning)` | Launch DBOS workflow; compensating delete of task row on failure |
| `get(task_id)` | SELECT FROM tasks WHERE id = ?, then DBOS.retrieve_workflow() for status/output/etc. |
| `wait(task_id)` | DBOS.retrieve_workflow().get_result(), then assemble Task from DB row + workflow result |
| `stream(task_id)` | DBOS.read_stream(task_id, "output") |
| `try_deliver(task_id, conversation_id, msg)` | **Txn:** SELECT tasks.inbox_closed FOR UPDATE; if open → INSERT INTO conversation_items, return True; if closed → return False |
| `close_inbox(task_id, conversation_id, last_seen)` | **Txn:** SELECT conversation_items WHERE conversation_id = ? AND position > ?; if found → return them; if not → UPDATE tasks SET inbox_closed = 1, return [] |
| `cancel(task_id)` | DBOS.cancel_workflow() (status lives in DBOS) |
| `delete(task_id)` | Cancel if in-progress, then DELETE FROM tasks WHERE id = ? (items untouched) |
| `list_tasks(conversation_id, agent_id)` | SELECT FROM tasks WHERE conversation_id = ? AND/OR agent_id = ? |

### ConversationStore

| Method | DB Operation |
|---|---|
| `create_conversation()` | INSERT INTO conversations |
| `get_conversation_id(response_id)` | SELECT conversation_id FROM conversation_items WHERE response_id = ? LIMIT 1 |
| `get_latest_response_id(conversation_id)` | SELECT response_id FROM conversation_items WHERE conversation_id = ? ORDER BY position DESC LIMIT 1 |
| `search_messages(conversation_id, after, ...)` | SELECT FROM conversation_items WHERE conversation_id = ? [AND position > ?] ORDER BY position LIMIT ? |
| `append(conversation_id, messages)` | **Txn:** SELECT MAX(position); INSERT conversation_items (with search_text extracted from data) with incrementing position |
| `search(query, conversation_id?, limit)` | FTS query against search_vector (Postgres) or conversation_items_fts (SQLite), optionally scoped to a conversation |

### API-Level (not in runtime stores)

| Operation | DB Operation |
|---|---|
| List conversations | SELECT FROM conversations ORDER BY created_at with cursor pagination |
| Delete conversation | Cancel in-flight tasks, DELETE tasks, DELETE conversation_items, DELETE conversation |
| List agents | SELECT FROM agents ORDER BY created_at with cursor pagination |
| Delete agent | Cancel in-flight tasks (by model), DELETE FROM agents |
| CRUD files | TBD — may be backed by artifact store instead of DB |

---

## Cursor-Based Pagination

All list endpoints use the same pattern. For a sort column (created_at for
agents/files/conversations, position for conversation_items):

```
after cursor:  WHERE sort_col > (SELECT sort_col FROM table WHERE id = :after_id)
before cursor: WHERE sort_col < (SELECT sort_col FROM table WHERE id = :before_id)
order "asc":   ORDER BY sort_col ASC LIMIT :limit + 1
order "desc":  ORDER BY sort_col DESC LIMIT :limit + 1
```

Fetch `limit + 1` rows. If more than `limit` returned, set `has_more = true`
and discard the extra row. `first_id` / `last_id` taken from the returned page.
