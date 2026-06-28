# MySQL / MariaDB Compatibility

## What this is

Omnigent was built for SQLite (local dev) and PostgreSQL (production). This document covers everything done to add MySQL/MariaDB as a third supported database backend, why each change was needed, and how to run the stack locally.

---

## Local dev setup (no Docker Desktop required)

We use **Colima** — a lightweight Linux VM that runs Docker without Docker Desktop — to host MariaDB 11. The omnigent server runs directly on the Mac.

### What's running

| Process | Where | Port |
|---------|-------|------|
| MariaDB 11 | Colima Docker container | 3306 |
| Omnigent server | Mac host (Python 3.12 venv) | 6767 |

### Start everything

```bash
# Start the MariaDB container (if not already running)
DOCKER_HOST="unix://${HOME}/.colima/default/docker.sock" docker start omnigent-mariadb

# Start the omnigent server
MARIADB_PASSWORD=$(grep ^MARIADB_PASSWORD ~/omnigent/deploy/docker/.env | cut -d= -f2)
cd ~/omnigent
nohup .venv-omnigent/bin/python -m omnigent server \
  --database-uri "mysql+pymysql://omnigent:${MARIADB_PASSWORD}@127.0.0.1:3306/omnigent?charset=utf8mb4" \
  > /tmp/omnigent-server.log 2>&1 &
```

Then open http://localhost:6767 in your browser.

### Connect a runner

After the server is up, open a new terminal and run:

```bash
omnigent
```

The CLI will prompt you to connect to a server — point it at `http://localhost:6767`. The runner handles actual agent execution; without it, sessions show "internal error" when you try to use them.

### Query the database interactively

```bash
MARIADB_PASSWORD=$(grep ^MARIADB_PASSWORD ~/omnigent/deploy/docker/.env | cut -d= -f2)
DOCKER_HOST="unix://${HOME}/.colima/default/docker.sock" \
  docker exec -it -e MYSQL_PWD="${MARIADB_PASSWORD}" omnigent-mariadb \
  mariadb -u omnigent omnigent
```

### Stop everything

```bash
kill $(cat /tmp/omnigent-server.pid 2>/dev/null)
DOCKER_HOST="unix://${HOME}/.colima/default/docker.sock" docker stop omnigent-mariadb
colima stop
```

---

## Code changes made

### 1. Driver dependency (`pyproject.toml`)

Added `PyMySQL` as an optional dependency. PyMySQL is a pure-Python MySQL/MariaDB driver — no system libraries needed.

```toml
mysql = ["PyMySQL>=1.1,<2"]
```

Also added it explicitly to the Docker server image (`deploy/docker/Dockerfile`) so the container works with both PostgreSQL and MariaDB without a separate build.

---

### 2. Upsert syntax

**The biggest change.** PostgreSQL uses `ON CONFLICT DO UPDATE` for upserts (insert-or-update atomically). MySQL/MariaDB uses `ON DUPLICATE KEY UPDATE`. These are not interchangeable — you have to use the dialect-specific SQLAlchemy insert object.

**5 places were affected:**

| File | Method | What it upserts |
|------|--------|----------------|
| `permission_store/sqlalchemy_store.py` | `grant()` | User→session permission level |
| `permission_store/sqlalchemy_store.py` | `ensure_user()` | User row (insert if not exists) |
| `conversation_store/sqlalchemy_store.py` | `_dialect_upsert_labels()` | Conversation policy labels |
| `conversation_store/sqlalchemy_store.py` | `_upsert_daily_cost_dialect()` | Per-user daily LLM spend (atomic increment) |
| `conversation_store/sqlalchemy_store.py` | `set_daily_ask_approved()` | Cost approval checkpoint |

Each had a SQLite branch and a PostgreSQL branch. A MySQL/MariaDB branch was added to each using `sqlalchemy.dialects.mysql.insert` with `.on_duplicate_key_update()`.

The callers that gate which dialect gets the fast path (e.g. `if dialect in ("sqlite", "postgresql"):`) were also updated to include `"mysql"` and `"mariadb"`.

---

### 3. Search query (`::text` cast and `ILIKE`)

The full-text search fallback for PostgreSQL used two PostgreSQL-specific SQL features:

- `ci.data::text` — PostgreSQL shorthand for casting to text. In MySQL the `data` column is already `TEXT`, so no cast is needed.
- `ILIKE` — PostgreSQL case-insensitive LIKE. MySQL doesn't have it, but `LIKE` is case-insensitive by default with `utf8mb4_general_ci` collation.

A MySQL/MariaDB branch was added in `conversation_store/sqlalchemy_store.py` `search()` that uses plain `LIKE` with no cast.

---

### 4. Transaction isolation (`db/utils.py`)

MySQL and MariaDB default to `REPEATABLE READ` transaction isolation. This caused error 1020 ("Record has changed since last read") when two concurrent transactions touched the same conversation row — for example, the server auto-generating a title while the session was also being updated.

PostgreSQL defaults to `READ COMMITTED`, which doesn't have this problem. We explicitly set `READ COMMITTED` for MySQL/MariaDB in `_create_engine()`:

```python
**({"isolation_level": "READ COMMITTED"} if is_mysql else {})
```

---

## Migration changes

Alembic migrations were written and tested only against SQLite and PostgreSQL. Running them fresh on MySQL/MariaDB exposed four categories of incompatibility:

---

### Migration issue 1: Drop index before drop table (FK constraint)

**Affected migrations:**
- `e3b1f2a4c9d7_drop_pending_tool_calls_table.py`
- `b9c1d2e3f4a5_drop_tasks_table.py`

**What failed:** MySQL/MariaDB refuses to drop an index that a foreign key constraint depends on while the constraint still exists. SQLite and PostgreSQL handle this automatically.

**Example error:**
```
(1553, "Cannot drop index 'ix_pending_tool_calls_task_id':
        needed in a foreign key constraint")
```

**Fix:** For MySQL/MariaDB, skip the index drops and just call `op.drop_table()` directly. MySQL removes all indexes and FK constraints when a table is dropped — so the separate `drop_index` calls are redundant anyway.

```python
def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name in ("mysql", "mariadb"):
        op.drop_table("pending_tool_calls")  # drops indexes + FKs automatically
        return
    op.drop_index("ix_pending_tool_calls_task_id", ...)
    op.drop_table("pending_tool_calls")
```

---

### Migration issue 2: FK constraint blocks index drop during ALTER TABLE

**Affected migration:** `a3b4c5d6e7f8_add_session_policy_columns.py`

**What failed:** This migration restructures the `policies` table — it drops the `agent_id` column (which had a FK to `agents.id`) and adds a `session_id` column. Part of the restructure drops `ix_policies_agent_id`, but MySQL won't drop that index while the FK on `agent_id` is still alive.

**Fix:** For MySQL/MariaDB, dynamically discover the auto-generated FK constraint name using SQLAlchemy's inspector and drop it first before the main batch alter:

```python
if bind.dialect.name in ("mysql", "mariadb"):
    from sqlalchemy import inspect as sa_inspect
    fks = sa_inspect(bind).get_foreign_keys("policies")
    agent_fk = next((fk for fk in fks if "agent_id" in fk["constrained_columns"]), None)
    if agent_fk and agent_fk.get("name"):
        with op.batch_alter_table("policies") as pre_op:
            pre_op.drop_constraint(agent_fk["name"], type_="foreignkey")
```

MySQL auto-names FK constraints (e.g. `policies_ibfk_1`) so the name can't be hardcoded — it has to be discovered at migration time.

---

### Migration issue 3: `CAST(x AS BIGINT)` syntax

**Affected migration:** `ecc0e25727b0_add_updated_at_to_comments.py`

**What failed:** A raw SQL string used PostgreSQL/SQLite syntax for integer casting:

```sql
UPDATE comments SET updated_at = CAST(created_at AS BIGINT) * 1000000
```

MySQL/MariaDB uses `SIGNED` or `UNSIGNED` instead of `BIGINT` inside `CAST()`.

**Fix:** Detect the dialect and use the right keyword:

```python
cast_expr = "CAST(created_at AS SIGNED)" if mysql else "CAST(created_at AS BIGINT)"
op.execute(f"UPDATE comments SET updated_at = {cast_expr} * 1000000 WHERE updated_at IS NULL")
```

---

### Migration issue 4: `CHECK` constraint syntax in batch mode

**Affected migration:** `b8c4f2e7a9d1_add_workspace_to_conversations.py`

**What failed:** This migration adds a `CHECK` constraint to enforce that `workspace` must be set when `host_id` is set. Alembic's `batch_alter_table` emits the CHECK in a form that MariaDB 11 rejects:

```
(1901, "Function or expression 'host_id' cannot be used in the CHECK clause")
```

**Fix:** Skip the CHECK constraint entirely for MySQL/MariaDB. The application already enforces this rule before writing — the DB constraint is a belt-and-suspenders guard that MySQL/MariaDB can't provide here.

```python
if bind.dialect.name not in ("mysql", "mariadb"):
    batch_op.create_check_constraint(
        "ck_conversations_workspace_required_for_host",
        "host_id IS NULL OR workspace IS NOT NULL",
    )
```

---

### New migration: `o1a2b3c4d5e6_mysql_mariadb_partial_index.py`

**Why it exists:** The `agents` table has a partial unique index on `name WHERE session_id IS NULL` — meaning only template agents must have unique names. Session-scoped agents (one per session, e.g. "claude-native") can share names across sessions freely.

MySQL/MariaDB ignores the `WHERE` clause on `Index()` and creates a full unique index on `name` instead. This would block every second session from loading a built-in agent, since two session agents can't share the name "claude-native" under a full unique index.

**What MySQL/MariaDB can't do:** Neither supports partial/filtered indexes (`CREATE INDEX ... WHERE ...`). This is a hard limitation of the database engine.

**Fix:** For MySQL/MariaDB, the migration drops the over-restrictive full unique index entirely. Template-agent name uniqueness is enforced at the application layer instead.

---

## What MySQL/MariaDB doesn't get vs PostgreSQL

These are MySQL/MariaDB limitations — nothing is regressed for PostgreSQL users.

| | PostgreSQL | MySQL/MariaDB |
|--|-----------|---------------|
| Partial unique index on `agents.name` | ✓ DB-level | App-level only |
| `CHECK` constraint on workspace/host_id | ✓ DB-level | App-level only |
| Full-text search | ILIKE fallback | LIKE fallback (same result) |

---

## Files changed

| File | What changed |
|------|-------------|
| `pyproject.toml` | Added `mysql` optional extra with PyMySQL |
| `deploy/docker/Dockerfile` | Added PyMySQL install in server-builder stage |
| `deploy/docker/docker-compose.mariadb.yaml` | New compose file for MariaDB + omnigent |
| `deploy/docker/MARIADB.md` | Quickstart docs for MariaDB setup |
| `omnigent/db/utils.py` | `READ COMMITTED` isolation for MySQL; `_create_engine()` |
| `omnigent/stores/permission_store/sqlalchemy_store.py` | `grant()`, `ensure_user()` — MySQL upsert branches |
| `omnigent/stores/conversation_store/sqlalchemy_store.py` | 3 upsert sites, LIKE search, dialect gate expansions |
| `omnigent/db/migrations/versions/e3b1f2a4c9d7_*` | Drop table directly on MySQL instead of dropping indexes first |
| `omnigent/db/migrations/versions/b9c1d2e3f4a5_*` | Same fix for tasks table |
| `omnigent/db/migrations/versions/a3b4c5d6e7f8_*` | Drop FK before index drop on MySQL |
| `omnigent/db/migrations/versions/b8c4f2e7a9d1_*` | Skip CHECK constraint on MySQL/MariaDB |
| `omnigent/db/migrations/versions/ecc0e25727b0_*` | `CAST(x AS SIGNED)` instead of `CAST(x AS BIGINT)` |
| `omnigent/db/migrations/versions/o1a2b3c4d5e6_*` | New migration: drop over-restrictive unique index on MySQL |

---

## Rule of thumb for future migrations

Any time a new migration does one of these things, add a MySQL/MariaDB check:

1. **`drop_index` followed by `drop_table`** → just `drop_table` for MySQL (it drops everything)
2. **`drop_index` on a column that has a FK** → drop the FK first on MySQL using `sa_inspect`
3. **Raw SQL with `CAST(x AS BIGINT)`** → use `SIGNED` for MySQL
4. **`create_check_constraint` in `batch_alter_table`** → guard with `if dialect not in ("mysql", "mariadb")`
5. **`op.execute()` with PostgreSQL-specific syntax** (`::type`, `ILIKE`, `RETURNING`, `ON CONFLICT`) → add a MySQL branch
