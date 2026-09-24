# OLTP database best practices

Use this guide when designing or reviewing Omnigent's application schemas,
queries, transactions, and storage code. It is the database reference for Polly
and its reviewers, and is maintained alongside the code it governs.

## Review contract

**Required** rules are blocking when the PR demonstrably violates them.
**Recommended** practices require judgment about the workload; report them as
non-blocking unless there is also a concrete correctness, security, or required
contract violation. **Optional** practices are design choices. Respect the
conditions attached to each rule.

Apply this guide to new tables and changes introduced by the PR. Existing tables
do not need a wholesale migration to satisfy it. Do not report unchanged
historical definitions, code moves, test-only fixtures, illustrative examples,
or downgrade-only restorations as new schema violations. A newly introduced
unsafe query or destructive migration is still in scope even on an old table.

For every finding, cite the guide section, changed file and line, affected
table/column/query, and concrete evidence. Follow ORM types, migration helpers,
constants, validation, and dialect variants before concluding that a rule is
violated. Engine-specific explanations apply only to that engine; evaluate the
schema across the engines supported by the changed code. When requirements
conflict or a recommendation does not fit the workload, describe the tradeoff
for maintainers instead of inventing an exception or a speculative blocker.

## Quick checklist

- Reuse the application storage layer and managed migrations.
- Bound reads, writes, and transactions. Paginate every list API from launch,
  with an index that bounds scanned rows as well as returned rows.
- Start with third normal form. Prefer hard deletion when product behavior
  does not require soft deletion.
- Give every table an explicit, stable primary key; favor compact, owner-first
  keys and preserve parent-child locality when the ownership is immutable.
- Build named indexes for actual queries, including the primary-key columns
  needed for deterministic pagination. Avoid redundant and unused indexes.
- Keep every new column value **below 16,384 bytes**, including serialization
  and encoding overhead. Store large payloads in the artifact store.
- Use compact types, explicit nullability, binary UUIDs, and exact numeric
  representations. Avoid the prohibited types and constraints below.
- Treat encryptable data as opaque and keep its storage format unambiguous.
- Stage destructive changes with a reversible observation period, and keep
  schema and application rollouts independently deployable and reversible.

## Storage layer and workload design

**Required:** New features must use the existing application storage
abstractions. In Omnigent, these are the stores under `omnigent/stores/`, managed
SQLAlchemy sessions, and Alembic migrations. Use
`make_named_managed_session_maker` and stable operation names as described in
[`AGENTS.md`](../AGENTS.md#database-query-names). Use the artifact store for
large opaque payloads and retain small references in relational tables.

Transactional consistency with existing storage can justify retaining that
storage path. Before introducing a separate persistence path or bypassing the
managed client/migration layer, document why the existing abstraction cannot
support the feature, why the feature cannot be redesigned to use it, and obtain
maintainer design review. This is a requirement to reuse and extend the
repository's storage layer, not to adopt another storage service.

**Recommended:** Design the schema around known and plausible future workloads.
List insert, lookup, ordering, pagination, update, and deletion operations before
choosing keys. For a messaging application, both conversations and messages need
paginated chronological lists from the first release; deleting a conversation
must handle arbitrarily many messages in bounded batches.

The workload may also need to change to make a safe schema possible. For
example, group-based permissions avoid updating every individual user's access
when creating a conversation shared with a large department. Discuss such
product changes before accepting unbounded database work.

## Bounded queries and pagination

**Required:** Every read and write must examine, return, and affect a bounded
number of rows. Bound transactions and background jobs with batches as well.
A bound can come from a finite set of full unique keys, an explicit limit, or a
true invariant independent of product growth, such as the seven days of a week.
A current tenant size or configurable product quota is not such an invariant.

**Required:** Every list operation must be paginated from its first release,
including previews and APIs whose current product limits are small. Do not defer
pagination until the data grows.

**Required:** Design queries and indexes so `numRowsScanned = O(limit)`.
Returning few rows with `LIMIT` does not bound an otherwise unbounded scan.

- Paginate over an index containing the filter and ordering columns, plus
  enough key columns to identify each row uniquely. The pagination order must
  match a supported traversal of the index.
- Use a deterministic continuation key, including tie-breakers for duplicate
  sort values. Deep offset pagination that scans all prior pages does not meet
  the scan bound.
- Include limits in subqueries when an outer limit alone does not bound their
  work. Apply the same analysis to updates, deletions, and purge jobs.
- Verify execution plans against the intended indexes and constrain plans where
  supported and needed to preserve the bound. A covering index for the filters
  and ordering is the starting point; verify the actual plan and residual
  filters rather than assuming the index guarantees bounded work.

## Deletion, normalization, and vertical splits

**Required:** Use hard deletion unless application behavior requires soft
deletion. When soft deletion is necessary, design the active-row queries and
eventual purge operations with appropriate indexes and bounded batches.

**Recommended:** Start in third normal form: every non-key column depends on
the whole primary key, and no non-key column depends on another non-key column.
Deviate only for a documented, compelling reason. Normalization reduces
duplication and the work needed to keep copies consistent, and makes familiar
relational tools and query patterns easier to use.

**Recommended:** Consider vertically splitting small, frequently accessed fields
from larger, rarely accessed fields into tables with identical primary keys.
This can improve buffer-pool locality and reduce disk I/O. With full-row binary
logging or change-data capture, it also avoids copying unchanged large fields on
every update to a small field. Account for the cost of keeping both tables
consistent. Separate metadata or lifecycle entities can preserve third normal
form. A vertical split does not waive the column-size limit.

## Primary keys

**Required:** Every table must have an explicit primary key. It identifies a row
uniquely and stably. Treat it as immutable; model a key change as a delete and
insert with the necessary consistency guarantees.

**Recommended:** Choose keys with physical locality in mind. In clustered
storage engines, rows are stored in primary-key order. MySQL/InnoDB has this
property; for TiDB, explicitly choose a clustered primary key when appropriate
and account for limitations on later primary-key changes. Do not assume that a
primary key physically clusters rows on every supported engine.

- Put the immutable owning entity first where possible, such as `workspace_id`,
  `account_id`, or `user_id`. This keeps an owner's rows together and helps active
  owners' working sets fit in the buffer pool. Do not put mutable ownership in
  the key merely to achieve this layout.
- Reflect immutable parent-child relationships in keys. For example, users can
  have `(user_id)`, conversations `(user_id, conversation_id)`, and messages
  `(user_id, conversation_id, message_id)`. Moving a message between conversations
  then requires delete-and-insert semantics; omit the parent component if that
  conflicts with the required identity model.
- Keep key types short: appropriately sized integers and 16-byte binary IDs are
  good choices. Long strings consume index space and length budgets; clustered
  primary keys are also carried in secondary indexes.
- Consider ordering within binary IDs. Random UUIDs or uniform hashes scatter
  an owner's rows; use an ordered UUID scheme when approximate insertion order
  is useful. If a long hierarchical path needs a compact key, consider preserving
  prefix locality rather than hashing the whole path uniformly. Any abbreviated
  hash scheme also needs collision handling to preserve unique row identity.

**Required:** Do not use auto-incrementing primary keys. A leading auto-increment
key groups inserts by statement time rather than ownership, can concentrate
inserts at the end of the table, and complicates online migrations. As part of a
composite key it can also require an additional globally unique index.

## Secondary indexes

**Required:** Indexes must support real query patterns and the bounded-scan
requirement. Give each index an explicit name, since generated names can differ
across engines and break queries that reference an index by name.

**Required:** Include the full primary key at the end of non-unique ordered
secondary indexes, such as B-tree indexes, appending only key columns not already
present. This provides unique tie-breakers for pagination. Prefer explicit
columns even on engines that implicitly append the primary key; the optimizer
may not reliably use implicit suffixes, and other engines may not provide them.
Preserve the intended key of a unique constraint: appending primary-key columns
to it can weaken the uniqueness it enforces. Add a separate pagination index if
the unique index cannot support the required traversal.

Specialized search or inverted indexes, such as GIN, have different structures;
do not mechanically append primary-key columns or assign a sort direction to
them. Queries using them still need evidence of bounded work and a supported
pagination path; a search index alone does not establish either property.

**Recommended:** Put the owning entity first where the query permits it. Some
cross-owner workloads need a different leading column. Fine-grained timestamps
usually belong later in the index, after the columns used to narrow the query.
For purging, use the coarsest time buckets that still meet application retention
requirements, while keeping each purge bounded.

**Required:** Do not add an index that is redundant with the leading columns of
another index. Before removing a prefix index, verify equivalent uniqueness,
predicates, ordering, collation, and query support: a shorter unique index, for
example, may enforce an invariant that a longer index does not. Remove unused
indexes through the staged process in [Deployment practices](#deployment-practices).

**Recommended:** Scrutinize tables with more than **10 indexes**. This is a review
trigger, not an automatic rejection. Each index consumes disk and cache space
and adds write I/O. Updating an indexed column also moves its entry, potentially
to a different disk page. Frequently updated, fine-grained timestamps can be
particularly expensive; coarser values can reduce this churn.

**Optional:** Use unique indexes when needed for application invariants. First
check whether the primary key should express that identity instead. Where
possible, include the owning entity as a prefix. Account for added locking and
deadlock risk; in MySQL, unique-index writes also bypass the change-buffer
optimization available to eligible non-unique secondary-index writes. Unique
indexes are permitted, not categorically banned.

**Required:** Do not introduce descending indexes. For ordered indexes, use
ascending order and a backward traversal where appropriate, and verify the query plan.
Descending-index support and semantics vary across engine versions; a descending
query order alone does not require a descending index.

## Columns

### Size and layout

**Required:** Every column must have a size bound. For every **new** application
column, each stored value must be **less than 16 KB (16,384 bytes)**, including
columns added to existing tables and columns in new tables. The maximum allowed
size is **16,383 bytes**. This per-value limit also applies to binary payloads;
there is no larger BLOB allowance.

The bound may come from a fixed-size type, an enforced type/constraint limit, or
application validation on every write path. Check actual dialect behavior:
declaring a string length is insufficient on an engine that does not enforce
it. Account for multibyte encoding, serialization, compression, and encryption
overhead in the bytes actually stored. Expected compression ratios, small
defaults, sample data, nullability, and current usage are not upper bounds.

An unbounded type without such enforcement is blocking. Require a strict byte
cap or move the payload to the artifact store with a small database reference.
Passing the size check does not permit a type prohibited elsewhere in this guide.

| Definition or enforced storage limit | Size-policy result |
| --- | --- |
| At most 16,383 bytes on every write path | Passes the size check |
| At most 16,384 bytes | Blocking: includes the prohibited boundary |
| Unbounded binary without a byte cap | Blocking |
| 4,096 characters at up to four bytes each, without a byte cap | Blocking: permits 16,384 bytes |

**Recommended:** Keep columns as short as the application needs. Choose string
lengths deliberately; an arbitrary `255` is not a justification. Scrutinize
tables with more than **20 columns**, without treating that count as a hard cap.

### Nullability, defaults, and timestamps

**Required:** Mark a column `NOT NULL` if the value can never be absent. Use `NULL`
for missing or unspecified data instead of non-null sentinel values. For a
numeric enum, prefer an application-supplied zero value for unspecified/default
unless `NULL` has a meaning distinct from every enum value.

**Required:** Do not declare database `DEFAULT` values on new tables, except
`CURRENT_TIMESTAMP` for timestamp columns. Application code must intentionally
supply values. Defaults may be necessary when adding columns to existing tables
so that existing rows and older application versions remain compatible; explain
that migration requirement. Application enum defaults do not authorize a new
table's SQL `DEFAULT 0`.

**Optional:** Include a last-updated timestamp. On MySQL, a non-null
`TIMESTAMP(6)` with `DEFAULT CURRENT_TIMESTAMP(6)` and
`ON UPDATE CURRENT_TIMESTAMP(6)` can maintain it automatically. Implement and
test equivalent behavior for other supported engines rather than copying
engine-specific syntax unconditionally.

**Required:** Never store or default a timestamp to `0000-00-00 00:00:00`.
Although some engines accept it, it is not a valid date and can break replication
and downstream tools.

### Types and constraints

**Required:** Follow these rules for newly introduced definitions:

| Avoid | Use instead or reason |
| --- | --- |
| Generated columns | Maintain an ordinary column in application code when needed, with explicit consistency handling. This is a justified normalization tradeoff and avoids engine/tooling compatibility problems. |
| Foreign-key constraints | Maintain relationships through bounded application operations. Cascading deletes can create unbounded transactions, and online table replacement is difficult with incoming foreign keys. |
| `FLOAT` or `DOUBLE` | Use an appropriately sized integer, scaled integer, or `DECIMAL` with deliberate precision and scale. Binary floating point cannot represent most decimal fractions exactly. |
| Native SQL `ENUM` | Use compact integer codes with a single application/protobuf definition and stable numeric values. Avoid string spellings, capitalization errors, and conflicting lexical versus enum ordering. |
| `LONGBLOB` or `LONGTEXT` | These permit excessively large values. Use bounded fields or artifact storage. |
| Native SQL `JSON` | Normalize repeated entries into a child table, or use a bounded opaque binary representation when relational access is unnecessary. |
| `TEXT` or `MEDIUMTEXT` | Use bounded strings for actual text, or a bounded binary payload for serialized/compressed data. Charset checks on text columns are incompatible with arbitrary compressed or encrypted bytes. |
| String UUIDs | Use 16-byte binary UUID storage. In Omnigent, reuse `Uuid16` and its dialect variants rather than storing formatted UUID strings. |

For exact quantities, choose the smallest sufficient integer type or an exact
decimal. For example, store 1.53 currency units as 153 minor units; name the
column to make the scale clear. A percentage with two fractional decimal places
can use integer basis points from 0 through 10,000. Choose enum storage large
enough for its codes, such as an unsigned byte for 0 through 255 or an unsigned
small integer for 0 through 65,535 on engines that support those types; provide
compatible mappings on other engines.

Use dedicated diagnostics and observability for human-readable enum or payload
inspection instead of compromising the storage representation for direct
database browsing.

### Opaque binary payloads

**Recommended:** Avoid BLOB and MEDIUMBLOB when structured columns or artifact
storage suffice. Depending on engine and row format, large values can require
off-page I/O, increase cache pressure, and force disk-backed temporary tables.
Full-row binary logging can copy an unchanged payload every time another field
in the row changes. If a binary column is needed, enforce the strict size cap
above, far below its type's nominal capacity.

**Required:** Do not filter on BLOB payloads in `WHERE` clauses. Extract queried
attributes into dedicated typed columns where they are not encryptable. Do not
combine values with substantially different read or update frequencies into one
payload: retrieving or rewriting the entire parcel wastes resources when only
one small component is needed. Consider separate columns or a vertical split.

Repeated/list values usually belong in child tables with one row per element.
Opaque JSON-like parcels make partial updates expensive, complicate indexing,
vary widely in size, and invite unbounded growth. Parse and transform payloads
in application code rather than spending database CPU on JSON parsing. Reuse
the storage abstraction for simple key-value needs.

**Recommended, not currently a blocking requirement:** Compress serialized BLOB
payloads in application code before storing them. Record the compression scheme
in a column name or, preferably, a versioned envelope/protobuf field so readers
can identify it per row. An opaque representation should not need database-side
inspection. Compression does not replace an enforced stored-byte limit.

### Data that may be encrypted

**Required:** Treat any data that could be encrypted as opaque to the database,
even when encryption is currently disabled. Do not index it or build filters and
features that will fail once encryption is enabled.

Store such data as bounded binary/BLOB bytes, with an explicit envelope such as
a protobuf `oneof` distinguishing plaintext from ciphertext. Include the
encryption configuration/metadata needed to interpret the encrypted variant.
The structure must make the variant clear without parsing arbitrary payload
headers, keep at most one representation, and require readers to handle both.
Keep this envelope private to the storage layer rather than exposing it as the
public API representation.

Do not store arbitrary ciphertext in `TEXT` or `VARCHAR`: it may not be valid in
the column's character set. Do not maintain separate plaintext and ciphertext
columns whose mutual exclusivity every caller must remember to enforce.
Combining encryptable fields into one envelope is reasonable when they are
accessed together; separate envelopes can suit independently accessed subsets.
Encryption and envelope overhead count toward the same 16,383-byte maximum.

## Deployment practices

**Required:** Before dropping a column or table, establish that no code uses it
and its data is no longer needed. First rename it to a deprecated name, then
observe the deployment for a stated period of days or weeks, and only then drop
it in a later migration. Unknown readers can be recovered quickly by reverting
the rename; recovering dropped data requires backups. Coordinate the rename
with application compatibility and document the observation/rollback evidence.

**Required where supported:** Make an index `INVISIBLE` before dropping it.
Start in development/staging, then a representative production deployment, and
observe for several days before broader removal. Visibility can be restored
quickly; rebuilding an index may take hours or days. On engines or versions
without invisible indexes, provide a staged validation and rollback plan that
demonstrates queries remain supported. Never emit unsupported DDL merely to
follow this process.

**Required:** Put schema changes in a standalone PR. Do not bundle them with
business logic, protobuf definitions, or other changes to the application
binary. Migration tests and documentation belong with the schema change.
Document deployment order and compatibility so schema and application releases
can be deployed and rolled back independently. Plan additive changes and
backfills before switching callers; defer destructive cleanup until the
compatibility window has elapsed. Follow the
[database migration review requirements](../CONTRIBUTING.md#database-migration-reviews).

## Stored procedures

**Required:** Do not introduce stored procedures. Keep application logic in the
managed application data-access layer, where it can be reviewed, tested, and
deployed consistently across supported databases.
