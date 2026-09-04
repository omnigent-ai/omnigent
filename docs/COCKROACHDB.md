# CockroachDB

Omnigent supports CockroachDB through its SQLAlchemy dialect. Install the
optional dependencies before configuring a CRDB connection:

```bash
pip install 'omnigent[cockroachdb]'
```

Use either URI form. Omnigent normalizes the short form to psycopg 3:

```text
cockroachdb://user:password@host:26257/database
cockroachdb+psycopg://user:password@host:26257/database
```

## Compatibility

Omnigent requires CockroachDB v23.2.28 or newer. CI tests v23.2.28,
v24.3.20, v25.2.10, and v25.4.5.

CockroachDB v23.2 provides `READ COMMITTED` as an opt-in preview. Enable it
before starting Omnigent:

```sql
SET CLUSTER SETTING sql.txn.read_committed_isolation.enabled = true;
```

Omnigent checks the effective isolation level at startup and fails with an
actionable error instead of silently running v23.2 transactions as
`SERIALIZABLE`.

The engine uses `READ COMMITTED`. Omnigent preserves explicit row locks and
can replay database-only transaction callbacks after SQLSTATE `40001` errors.
It never retries constraint failures or external side effects.

An empty CRDB database is required for the first startup. Omnigent creates the
current schema directly and stamps the current Alembic revision. It does not
run the historical PostgreSQL migration chain. A database left at a partial
revision by an earlier migration attempt is unsupported and must be replaced
with a new empty database.

Existing CRDB databases created by a supported Omnigent release use normal
Alembic upgrades for subsequent migrations.

## Connection pool

Server databases default to 200 pooled connections, 20 overflow connections,
and a 10-second pool wait, matching the server's worker concurrency. These
global settings override the defaults for PostgreSQL, MySQL, and CRDB:

- `OMNIGENT_DB_POOL_SIZE`
- `OMNIGENT_DB_MAX_OVERFLOW`
- `OMNIGENT_DB_POOL_TIMEOUT`

Blank values use the defaults. `OMNIGENT_DB_POOL_SIZE=0` removes the base-pool
limit, `OMNIGENT_DB_MAX_OVERFLOW=-1` permits unlimited overflow, and the pool
timeout accepts non-negative fractional seconds.

## Persistent local matrix

OrbStack supports the Docker Compose commands used by these recipes:

```bash
just crdb-up
just crdb-test
just crdb-stop
```

The four services use named volumes, so `crdb-stop` and later `crdb-up` retain
all data. SQL ports are 26323, 26324, 26325, and 26326 in version order.

To delete every local CRDB volume and start clean, run this explicitly
destructive command:

```bash
just crdb-reset
```
