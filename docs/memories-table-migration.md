# Memories table schema rollout

Revision `ffeec66eadb5`, following `a8be64286ffc`, adds the `memories` table for
long-term memory ([#13503](https://github.com/omnigent-ai/omnigent/issues/13503)).
Nothing in this change reads or writes it.

| Column | Type | Notes |
| --- | --- | --- |
| `workspace_id` | `BIGINT NOT NULL` | Primary key, part 1. No database default: the application supplies it |
| `actor_id` | `VARCHAR(160) NOT NULL` | Primary key, part 2: whose memory it is, such as `user:<id>` for a user id of up to 128 characters. Compared byte for byte |
| `path` | `VARCHAR(512) NOT NULL` | Primary key, part 3. Compared byte for byte |
| `content` | Envelope, at most 16,383 stored bytes | `VARBINARY(16383)` on MySQL |
| `description` | Envelope, at most 2,100 stored bytes, nullable | `VARBINARY(2100)` on MySQL. A one-line summary of up to 512 characters |
| `created_at`, `updated_at` | `BIGINT NOT NULL` | Unix epoch seconds |

`actor_id` and `path` use `utf8mb4_0900_bin` on MySQL and `COLLATE "C"` on
PostgreSQL (`path` only; equality on `actor_id` is already exact there). SQLite and
CockroachDB compare bytes by default. Comparisons are case-sensitive, ignore no
trailing spaces, and order control characters by byte, so `utf8mb4_0900_bin`
needs MySQL 8.0.17 or later.

The primary key serves both access patterns the memory feature needs: a read by
`(workspace_id, actor_id, path)`, and a listing of one actor's paths in key order,
paged by the last path returned. Listing under a prefix reads the key range
`[prefix + "/", prefix + "0")`, which is exact because paths compare by byte.
There is no secondary index.

SQLite ignores declared lengths and types, so on SQLite the table also has CHECK
constraints bounding every column: the strings at four bytes per declared
character, the envelopes at their stored sizes, and the `BIGINT` columns to
integers. Other engines enforce the declared types themselves.

## Storage envelope

`content` and `description` may be encrypted later, so they are stored as opaque
bytes, never filtered, sorted or indexed in SQL. Each value starts with a format
version byte (`1`) and a variant byte:

- `0`, plaintext: followed by the text's `CompressedText` frame (a NUL marker, a
  codec byte, then the raw or zstd-compressed UTF-8);
- `1`, ciphertext: followed by a one-byte key-reference length, the UTF-8 key
  reference naming the key that encrypted it, and the ciphertext.

Nothing writes ciphertext yet. The `EnvelopedText` column type writes plaintext
envelopes, rejects any write whose envelope or text is over the column's stored
limit (so every accepted value can be read back), and on read raises an error for
ciphertext, or for an unknown version or variant, rather than returning bytes as
text.

The `SqlMemory` model ships with the migration because fresh CockroachDB databases
are created from table metadata and then stamped at the current head. Without it
they would silently lack the table.

## Deployment order

1. Merge and release this schema-only change. Run the normal database migration
   (`omnigent debug db-upgrade <database-uri>`), or let server startup run it,
   and verify the database reached `ffeec66eadb5`.
2. Deploy the memory feature. It is off by default; `OMNIGENT_FEATURES=memory`
   turns it on for a server.

## Rollback

The feature release can roll back to this schema release at any time: the table
stays, and nothing reads it. Turning the feature flag off has the same effect
without a deploy. A binary predating this release rejects the newer migration
revision on startup, so it is not a rollback target once a database has migrated.

The migration's downgrade drops the table and every stored memory. It is for
development recovery, not production rollback. A later production removal follows
the staged procedure in
[Database Best Practices](DATABASE_BEST_PRACTICES.md#deployment-practices).

## Verification

`uv run --no-sync pytest tests/db/test_migration_memories.py tests/db/test_envelope.py
tests/db/test_migration_connections.py` checks that:

- the chain has one head; the upgrade creates the table with no defaults, and the
  downgrade removes it;
- a table created from the model matches the migrated one, CHECK constraints
  included, and both reject out-of-bound raw values on SQLite;
- envelope writes over each column's stored limit are rejected, and the widest
  description fits;
- the envelope distinguishes plaintext from ciphertext, and readers reject
  ciphertext, unknown versions and variants, and truncated or oversized frames;
- paths and owners compare byte for byte.

The CI jobs that run `tests/db` on PostgreSQL, CockroachDB and MySQL run the
byte-order check on each of those engines.
