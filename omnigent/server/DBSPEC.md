# Database Schema Design

This doc covers four core tables: agents, files, conversations,
conversation_items. They are part of a larger schema — 17 tables in all, also
including conversation labels, comments, policies, session permissions,
conversation metadata, projects, hosts, users, and scheduled tasks. No model
sets an explicit schema, so every table lives in the default one.

Schema is managed by Alembic migrations in `alembic/`. SQLAlchemy models live
in `omnigent/db/db_models.py`, which is the source of truth for the full table
list.

A `tasks` table existed in an earlier design — DBOS-backed workflow execution,
with a `try_deliver`/`close_inbox` steering handshake requiring transactional
atomicity with `conversation_items`. It was never populated by production code
and was dropped in migration `b9c1d2e3f4a5_drop_tasks_table`; DBOS itself has
been removed as a dependency. The authoritative turn/steering state today lives
in-memory in the runner process (`_active_turns`, `_session_message_buffers` in
`omnigent/runner/app.py`) and is never written to the DB. Only a coarse mirror
is persisted — `omnigent_conversation_metadata.live_status` /
`pending_elicitation_count`, written by the relay via
`session_live_state.persist_live_status` so any replica can render session
status.

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

## tasks (removed)

Dropped in migration `b9c1d2e3f4a5_drop_tasks_table` — see the note at the top
of this doc. `conversation_items.response_id` still exists as a plain
turn/response grouping id (harness- or app-generated; see below); it no longer
references any table.

---

## conversation_items

Conversation items — messages, function calls, function call outputs, reasoning, etc.
Single table with a `type` discriminator and a JSON `data` blob for type-specific fields.

| Column | Type | Notes |
|---|---|---|
| id | String(64) PK | Prefixed by type: msg_, fc_, fco_, rs_ |
| conversation_id | String(64) NOT NULL | References conversations.id (by convention — no DB FK; see Foreign key strategy) |
| response_id | String(64) NOT NULL | Turn/response grouping id (harness- or app-generated); no matching table since `tasks` was dropped |
| created_at | Integer NOT NULL | |
| status | String(32) NOT NULL | Default "completed" |
| position | Integer NOT NULL | Ordering within conversation |
| type | String(32) NOT NULL | message, function_call, function_call_output, reasoning |
| data | Text NOT NULL | JSON blob — type-specific fields (see below) |
| search_text | Text NOT NULL | Extracted plain text for full-text search (see below) |
| created_by | String(128) | Nullable — identity of the human actor who authored the item; `None` for agent/tool/system items |

**Indexes:** `ix_conversation_items_conversation_id_position` (composite), `ix_conversation_items_response_id`

### data column by type

**message:** `{"role": "user", "content": [{"type": "input_text", "text": "..."}]}`

**function_call:** `{"name": "get_weather", "arguments": "{...}", "call_id": "call_001"}`

**function_call_output:** `{"call_id": "call_001", "output": "{...}"}`

**reasoning:** `{"summary": [...], "content": null, "encrypted_content": null}`

---

## Design Decisions

### Foreign key strategy

There are no database-enforced foreign keys anywhere in the schema. Migration
`p1a2b3c4d5e6_remove_all_fks` dropped every FK constraint (per internal DB
standard Rule R032, which forbids DB-enforced foreign keys); the application
is solely responsible for cascading deletes and referential cleanup.
`conversation_items.conversation_id` references `conversations.id` by
convention only.

Deletion order is therefore an explicit application-code responsibility.
`ConversationStore.delete_conversation` collects the conversation's full
subtree, then in one transaction deletes FTS rows, items, and labels before the
conversation rows themselves — children before parent. A second transaction
then cleans up the Omnigent-side rows: comments, policies, session permissions,
conversation metadata, and session-scoped agents. That transaction runs after
the conversation is already gone and is best-effort — if it fails, orphaned
Omnigent rows survive a conversation that no longer exists. Do not remove any
of this cleanup logic on the assumption a DB cascade covers it; none does.

`conversation_items.response_id` references no table at all — it's a
turn/response grouping id (see the `tasks (removed)` note above) with no
backing table since `tasks` was dropped.

### Single conversation_items table with JSON data column

We never filter by item-internal fields — all queries are by conversation_id, response_id,
or position. A discriminated union via `type` + JSON is simpler than separate tables per
item type and extends to future item types (compaction, mcp_tool_call, etc.) without
schema changes.

### position for item ordering

App-managed integer, allocated from the `conversations.next_position` counter,
read and advanced under `_lock_conversation` in the same transaction as the
INSERT (a one-time `MAX(position)` scan backfills the counter for conversations
created before it existed). Guarantees strict, gapless ordering within each conversation.

Why not alternatives:
- **Autoincrement**: global, not per-conversation — creates gaps and arbitrary numbers across conversations.
- **Timestamps**: ties are possible when multiple items are inserted in one transaction (e.g., batch append).
- **Time-sortable IDs (ULID/UUIDv7)**: our type-prefixed IDs (`msg_`, `fc_`) break lexicographic sorting.
- **Compute on read** (`ROW_NUMBER()`): slower reads, makes cursor pagination ugly.

Allocation is O(1) under the conversation row lock `ConversationStore.append()`
already holds, so it costs nothing extra. Cursor pagination is clean:
`WHERE conversation_id = ? AND position > ?`.

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

`TaskStore` was removed along with the `tasks` table. Turn lifecycle (start,
steering/injection, cancel, status) is runner-owned, in-memory state today —
see `_active_turns` / `_session_message_buffers` / `_check_and_start_next_turn`
in `omnigent/runner/app.py`; none of it is a DB operation.

### ConversationStore

| Method | DB Operation |
|---|---|
| `create_conversation()` | INSERT INTO conversations |
| `get_conversation_id(response_id)` | SELECT conversation_id FROM conversation_items WHERE response_id = ? LIMIT 1 |
| `get_latest_response_id(conversation_id)` | SELECT response_id FROM conversation_items WHERE conversation_id = ? ORDER BY position DESC LIMIT 1 |
| `search_messages(conversation_id, after, ...)` | SELECT FROM conversation_items WHERE conversation_id = ? [AND position > ?] ORDER BY position LIMIT ? |
| `append(conversation_id, messages)` | **Txn:** lock conversation, allocate from `next_position`; INSERT conversation_items (with search_text extracted from data) with incrementing position |
| `search(query, conversation_id?, limit)` | FTS query against search_vector (Postgres) or conversation_items_fts (SQLite), optionally scoped to a conversation |

### API-Level (not in runtime stores)

| Operation | DB Operation |
|---|---|
| List conversations | SELECT FROM conversations ORDER BY created_at with cursor pagination |
| Delete conversation | Cancel any in-flight turn on the runner, DELETE conversation_items, DELETE conversation |
| List agents | SELECT FROM agents ORDER BY created_at with cursor pagination |
| Delete agent | `AgentStore.delete` → DELETE FROM agents. No API endpoint exposes this today; session-scoped agent rows are removed by `delete_conversation` |
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
