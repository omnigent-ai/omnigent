# Reusable user agents: install once, keep in the picker

> **PROPOSED.** Implemented by a stack of PRs: this schema change, then switch-agent retirement,
> shared agent rows on fork, user-agent install/list/remove, and the UI import.

## 1. Summary

Users can already create a custom agent (the composer's "Create custom agent" dialog, or
`omnigent run ./my-agent`), but it only stays in the new-session picker while one of their 30 most
recent sessions uses it. There is no way to install an agent once and keep it, and no way to remove
one.

This work makes user agents first-class and reusable:

- `omnigent agent add <dir | yaml | tar.gz>` and a UI "Import bundle" button install an agent owned
  by the caller. `omnigent agent list` and `omnigent agent remove` manage them.
- The picker gains one new source: the caller's own user agents, appended after everything it shows
  today, so existing ordering does not change.
- One agent row is shared by every session that uses it. Forking your own session reuses your row;
  forking someone else's session gives you your own copy. The unused in-place "switch agent" API is
  removed (it also deletes agents other sessions still use). Deleting a session never deletes an
  agent; only an explicit removal does.
- One new index makes the "my agents" query a bounded, ordered index seek.

## 2. Problem statement

### 2.1 What users hit

1. A user builds a custom agent (coordinator instructions, worker sub-agents, skills, guardrails) and
   runs it successfully, but it never appears under Agents in the picker. If it shows up at all, it
   is only because a recent session uses it, and it disappears once those sessions age out of the
   30-session window.
2. The only ways to register an agent that always shows are server-operator paths
   (`omnigent server --agent <dir>` or the `OMNIGENT_BUILTIN_AGENT_DIRS` environment variable), both
   of which need a server restart and make the agent visible to every user.
3. Agents created through ad-hoc entry points can collide with or hide each other, and per-run
   uploads (one upload per automated run) produce many near-identical picker entries.

### 2.2 What the code does today (measured on clean `main`, see Appendix A)

| Action | `agents` rows | `conversations.agent_id` |
|---|---|---|
| Create custom agent, first message (multipart `POST /v1/sessions`) | +1 row, kind 2, `created_by` = user | new session points at it |
| Pick that agent again in the picker (JSON `POST /v1/sessions {agent_id}`) | no new row | second session points at the **same** row |
| Start a session from a built-in (JSON create) | no new row | points at the kind-1 row |
| Fork a session | +1 row, kind 2, **same name and same `bundle_location` as the source** | fork points at the copy |
| Switch a session's agent (`POST /v1/sessions/{id}/switch-agent`) | +1 row named `"<target> (switch <id>)"`, and the session's **old row is deleted unconditionally** | session points at the new row |
| Delete a session | its kind-2 row is deleted if no other session references it | n/a |

Two defects follow:

- **Switch deletes shared agents.** With two sessions sharing one custom agent, switching one of them
  deleted the shared row and the other session failed with `Agent not found` (reproduced, Appendix A).
- **Fork creates duplicate agent rows** for an identical bundle, which is why per-session discovery in
  the picker needs name-based dedupe and why "my agents" cannot simply be "my kind-2 rows".

### 2.3 Root cause

The data model has two kinds: kind 1 (server agents) and kind 2 (everything users upload, plus fork
and switch copies). There is no concept of a reusable agent owned by a user that exists independently
of sessions, and no bounded way to list a user's own agents. The picker approximates it by scanning
the user's 30 most recent sessions.

## 3. Goals and non-goals

### Goals

- Install a complete agent bundle once (directory, single YAML, or `.tar.gz`), via CLI or UI, owned
  by the caller, visible only to the caller, on every host they use, with no server restart.
- Preserve workers, skills, guardrails, and supporting files exactly as bundled.
- Keep the picker's current ordering and grouping; only add the caller's agents.
- Remove an installed agent explicitly, with a warning when sessions still use it.
- One agent row per agent: no new duplicate rows from fork or from rerunning the same files, and no
  deletion of agents that other sessions use.
- Every new or changed query is bounded and index-backed; changes to existing behavior are additive
  unless a decision below explicitly says otherwise.

### Non-goals

- Sharing agents with other users or teams ("shared agents"); only server agents are global.
- Installing from a git URL (follow-up PR).
- A UI to remove agents (CLI only in this stack).
- Editing MCP servers of server (kind-1) agents.
- Dropping `ix_agents_created_at` (D5), until no older server uses it.
- Cleaning up legacy duplicate rows already in databases (they are hidden, not deleted).

## 4. Terminology

- **Server agent**: `agents.kind = 1`. Seeded built-ins, `server --agent`, `OMNIGENT_BUILTIN_AGENT_DIRS`.
  Visible to everyone. `created_by` is always NULL.
- **User agent**: `agents.kind = 2`. Uploaded by a user (Create custom agent, `omnigent run`, install),
  plus legacy fork and switch copies. `created_by` = owner (NULL on rows created before ownership was
  recorded).
- **Owner**: `agents.created_by`, the authenticated user id that created the row (`"local"` on a
  single-user local server).
- **Original**: a user-agent row whose `bundle_location` begins with its own id followed by `/`.
  Uploads, installs, reinstalls, and MCP edits write `"<own id>/<sha256>"` (for uploads and
  installs, the hash of the bundle's files, decision 23).
- **Derived copy**: a row whose `bundle_location` begins with a different agent's id (legacy fork and
  switch copies). Hidden from the "my agents" listing.
- **Uses an agent**: a session whose `conversations.agent_id` equals the agent id.

## 5. Decisions record

| # | Decision | Rationale |
|---|---|---|
| 1 | Installs are kind-2 rows with `created_by` = installer. No new kind, no constraint change. | Kind 2 already means user-provided; sessions already bind to it directly. |
| 2 | Rename the Python codec names `template` to `server` and `session` to `user`. Stored integers unchanged. | Names match meaning; no migration. |
| 3 | Install id is derived: `sha256("installed:" + (owner or "") + "\0" + name)[:32]`. Reinstall of the same owner and name updates that row in place (stable id, version + 1). | Primary key makes owner+name identity atomic under concurrent installs. |
| 4 | Install rejects a name that matches any server agent (409). | A same-named user agent would sit behind the server agent in the picker. |
| 5 | Install bundles over 100 MiB are rejected before being read (413). Bundles are validated like any tenant upload and never executed at install. | Trust boundary. |
| 6 | Sessions bind directly to the installed row. Reinstall therefore updates running sessions from their next turn, through any server process (6.18). A native terminal session keeps its launch configuration until it relaunches. | One agent, many sessions. |
| 7 | Fork of **your own** session reuses your row. Fork of **someone else's** session (or of a row with no owner) creates a new row owned by you with the bundle **copied under the new id**, so it appears in your picker and is isolated from later changes to the original. | Prevents another user from changing code that runs in your sessions ("dependency injection"). |
| 8 | Remove the switch-agent implementation (store method, request schema, web dialog, tests, docs). Keep the path reserved as an empty endpoint that returns 410 Gone, with a code comment that in-place switching will return with a new shape in a future release (section 16). Intentional API break. | No UI entry point, no other callers, and it deletes agents other sessions use. Fork covers the use case today; the path is kept for the planned rework. |
| 9 | Deleting a session never deletes agent rows. Only `DELETE /v1/agents/{id}` deletes an agent. | Agents outlive the sessions using them. |
| 10 | Removal warns: 409 with `sessions_in_use` while sessions use the agent; `force=true` removes anyway. The count reads at most 101 rows, unordered (N3). The CLI asks before forcing (`--yes` skips). | User chose "break with a warning" over blocking or copying. |
| 11 | After removal, sessions keep running for as long as the runner can (cached spec). The error appears only when the agent cannot be loaded at all. No per-message server check. | Verified live: SDK sessions keep working from the runner cache (Appendix A). |
| 12 | Picker: the caller's user agents join the existing newest-wins merge by root name, ranked by last change (`max(updated_at, created_at)`), so a fresh install or import is the entry shown for its name. Built-in names keep the built-in. Sorting unchanged. | The shown entry is always a selectable id. |
| 13 | "My agents" listing returns every original the caller owns, each by its own id: names may repeat and nothing is hidden by name. Legacy copies are skipped. | Management addresses ids, so no agent is hidden from its owner. |
| 14 | "My agents" reads at most 50 rows per request. | Bounded work per picker open. |
| 15 | New index `ix_agents_kind_owner_created (workspace_id, kind, created_by, created_at, id)`, non-unique, built concurrently on PostgreSQL, in its own schema-only PR. | Bounded keyset listing per owner. |
| 16 | The server-agent listing also filters `created_by IS NULL` (server agents never have an owner, and `update()` never stamps one on them), so it walks the shared `ix_agents_kind_owner_created` in order (D1). `ix_agents_created_at` stays until no older server uses it, then drops (D5). | One index serves both listings; the server listing no longer walks every agent. |
| 17 | MCP edits: no copy-on-write. Editing a user agent's MCP servers changes that agent for all sessions using it (all of which belong to its owner under decision 7). Server agents stay read-only. | Matches "one agent"; kind-1 edits are not a supported flow. |
| 18 | Changes are additive: existing rows (kind 1, kind 2 bound to sessions, kind 2 with NULL owner) keep today's behavior on every path except the explicitly changed ones (decisions 7, 8, 9). | Risk control. |
| 19 | No reviewers requested on the stack except the schema PR (code owners of migrations). | Requested. |
| 20 | Starting a new session (not a fork) from another user's kind-2 agent, when allowed by the existing READ rule, copies the agent for the caller exactly as a fork does. A child session stays on its tree's agent only when it names its parent's agent; a scheduled task takes its copy when created or switched to the agent (a task saved earlier moves to its owner's copy at its next run). | Another user can never change code that runs in your sessions, on any creation path. |
| 21 | The runner's existing missing-agent text is unified to the new guidance ("This agent no longer exists. Fork this session into another agent to continue."). Text-only change; tests updated. | One message for one condition. |
| 22 | The picker follows the "my agents" cursor until it has 50 agent names, at most 5 pages, so neither a page of skipped copies nor many rows of one name (one per run, from servers before decision 23) hides an older agent; `omnigent agent list` pages through all of them. | Bounded picker load. |
| 23 | A top-level session upload binds the uploader's row that holds the same files under the same name, creating it on first use. Its id is derived: `sha256("uploaded:" + (owner or "") + "\0" + name + "\0" + files + "\0" + attempt)[:32]`, where `files` hashes each entry's path, type, executable bit, and bytes, not archive timestamps, owners, or order. A row an MCP edit has changed is passed over for the next `attempt` (at most 3, then a fresh row), so a session always starts on the uploaded files. A sub-agent child upload keeps a row of its own: binding an existing row would add the per-parent title check that child creates never had. Installs name their blob by the same hash. | Agents outlive sessions (decision 9), so a row per `omnigent run` would pile up in "my agents" and the picker; rerunning the same files is one agent. |

## 6. Design

### 6.1 Data model

No schema change except the index (6.2). `agents` columns used:

| Column | Meaning in this design |
|---|---|
| `workspace_id`, `id` | primary key |
| `kind` | 1 server, 2 user |
| `created_by` | owner of a user agent; NULL for server agents and legacy rows |
| `name` | display and dedupe key (root name) |
| `bundle_location` | `"<agent id that first stored the blob>/<sha256>"`; drives the original/copy rule |
| `created_at` | listing order and cursor |
| `version`, `updated_at`, `description` | unchanged semantics |

Invariant enforced in code: server agents (kind 1) never have `created_by` set.

### 6.2 Index

`ix_agents_kind_owner_created ON agents (workspace_id, kind, created_by, created_at, id)`

| Column | Role |
|---|---|
| `workspace_id` | tenant key, first primary-key column, equality in every query |
| `kind` | equality, separates server from user rows |
| `created_by` | equality (`= :owner`); NULLs are indexed on PostgreSQL, MySQL, and SQLite |
| `created_at` | order and range for keyset paging; never updated |
| `id` | primary-key suffix: unique entries, deterministic tie-break for same-second rows |

- Not included: `name` (no query filters or sorts the listing by name; install uniqueness is the
  primary key), `bundle_location` (the original check compares against the row's own id, which is
  not expressible as an index condition).
- Non-unique: legacy rows legitimately share owner, kind, and name.
- Write cost: one entry per agent insert (uploads, installs, cross-user fork copies). `update()`
  changes only unindexed columns, except that an MCP edit can set `created_by` on a legacy row that
  has none, which moves that row's entry.
- Build: PostgreSQL uses `CREATE INDEX CONCURRENTLY` in an autocommit block, first dropping an
  INVALID leftover of this index on the `agents` table the connection resolves (never a same-named
  index in another schema), concurrently too; other dialects use `op.create_index`. Downgrade drops it.
- Rollout: additive and compatible with old code. Must exist before the "my agents" listing is
  enabled. Deployments that apply schema outside alembic must create it themselves.
- Overlap: also serves the server-agent listing (Q4). `ix_agents_created_at` is then unused by this
  release and drops in a later one (D5).

### 6.3 Query inventory

| # | Path | Shape | Index | Rows examined | Status |
|---|---|---|---|---|---|
| Q1 | create server agent, name check | `ws, name, kind=1` | `ix_agents_name` | 1 | unchanged |
| Q2 | `get(id)` | primary key | PK | 1 | unchanged |
| Q3 | `get_by_name` (server) | `ws, name, kind=1` | `ix_agents_name` | 1 | unchanged |
| Q4 | `list` server agents (picker catalog) | `ws, kind=1, created_by IS NULL ORDER BY created_at DESC, id DESC` + bounded cursor | new index | `limit` + timestamp ties | **changed** (D1) |
| Q5 | `get_names(ids)` | PK `IN` | PK | n | unchanged |
| Q6 | `update(id)` | PK | PK | 1 | unchanged; also used by reinstall |
| Q7 | `delete(id)` | PK | PK | 1 | unchanged; used by removal |
| Q8 | `_session_id_for_agent` | `conversations: ws, agent_id LIMIT 1` | `ix_conversations_agent_id` | 1 | unchanged |
| Q9 | session create with uploaded bundle | top level: `get(derived id)`, `INSERT` kind 2 on the first upload (decision 23); child: `INSERT` | PK | at most 3 gets and 1 insert | **changed** |
| Q10 | fork | `INSERT` copy row | | 1 | **changed**: only for cross-user or ownerless sources (decision 7) |
| Q11 | switch | delete old + insert copy | | 2 | **removed** (decision 8) |
| Q12 | session delete cleanup | 2 `conversations` lookups + `DELETE agents WHERE kind=2` | `ix_conversations_agent_id` | subtree | **removed** (decision 9) |
| Q13 | `list_conversations(agent_name=)` | `ws, name` then `IN` | `ix_agents_name` | all rows with that name | unchanged (stops growing for new forks of your own sessions) |
| N1 | `list_user_agents(owner)` | `ws, kind=2, created_by=:owner` + cursor (`created_at <= :ts` plus the tie-break, so the seek starts at the cursor), `ORDER BY created_at DESC, id DESC LIMIT 51` | new index | at most 51 + timestamp ties | **new** |
| N2 | install upsert | `get(derived id)`, then `INSERT` or `update` | PK | 1 to 2 | **new** |
| N3 | removal | `get`; `conversations: ws, agent_id LIMIT 101`; `DELETE` | PK, `ix_conversations_agent_id` | at most 103 | **new** |
| N4 | cross-user fork source check | `get(source agent)` (already loaded by the fork route) | PK | 0 extra | **new logic, no new query** |
| N5 | other sessions using a shared agent (non-owner binding, only after the Q8 root denies) | `conversations: ws, agent_id` in `id` pages of 200, deduped to 50 roots | `ix_conversations_agent_id` | at most 1,000 | **new** |

### 6.4 HTTP API

All routes require the same authentication as `GET /v1/agents`.

**`GET /v1/info`** adds `agent_install: bool`, true when the server's agent store supports user agents.
It also advertises `agent_detail: true` when `GET /v1/agents/{id}` is available;
clients treat its absence on older servers as unsupported.

**`POST /v1/agents`** (install or reinstall)
- Body: multipart, one part `bundle` (`.tar.gz`; the CLI converts directories and YAML).
- Rejects a request whose `Origin` is present but untrusted (403), like multipart session create:
  a multipart post skips the CORS preflight.
- Steps: reject over 100 MiB (413), counted while the body streams in so an oversized or chunked
  upload is cut off before it is spooled; `validate_agent_bundle` (same checks as session uploads; 400 on
  failure); reject a name equal to any server agent (409); upsert by derived id (decision 3).
- Concurrency: a primary-key conflict on insert falls back to update of the winner's row.
- Response 200: the agent object (`id`, `name`, `version`, `description`, `harness`, `skills`, ...).

**`GET /v1/agents?scope=user`** ("my agents")
- Params: `limit` (at most 50; larger values are capped), `after` (cursor: id of the last row read).
  The listing pages forward only: `before` or `order=asc` returns 400.
- Server reads up to 50 rows (N1), applies 6.7, returns `data` (at most `limit`, names may repeat), `has_more`, and
  `last_id` = id of the **last row read** (may not appear in `data`) to continue.
- A cursor row that no longer exists returns `stale_cursor` (existing error code).
- Default `GET /v1/agents` (no `scope`) is byte-for-byte unchanged.

On a store without user-agent support, `scope=user`, `POST`, and `DELETE` return 404.

**`GET /v1/agents/{id}`** (single-agent summary)
- Returns the same redacted agent object as the list routes, using a direct lookup by ID.
- Allows server agents and the caller's own user agents in the current workspace.
  Unlike discovery, this also permits reading an owned legacy fork/switch copy by its ID.
  Session sharing does not grant access through this route.
- Missing and inaccessible agents return 404. Without authentication configured, the caller
  owns user agents whose owner is `None`, matching `scope=user`.
- Stores without user-agent support can still return server agents; user agents return 404.
- An unreadable bundle falls back to stored metadata, as in the list routes.

**`DELETE /v1/agents/{id}`** (removal)
- Allowed only for the caller's own user agents; anything else (server agents, other users' agents,
  ownerless rows) returns 404.
- If sessions use the agent and `force` is not true: 409
  `{"error": {"code": "agent_in_use", "message": "..."}, "sessions_in_use": "N" or "100+"}`, counted
  by an unordered read of at most 101 conversations (N3).
- Otherwise deletes the row (blob kept: sessions may share it) and evicts the server cache.
  Response `{"id": ..., "deleted": true}`.

**Removed:** `POST /v1/sessions/{id}/switch-agent` and its request schema.

### 6.5 Store API (additive)

Added to the abstract `AgentStore` with safe defaults, so stores that do not implement them keep
working and report `agent_install: false`:

- `supports_user_agents: bool` (default False; SQLAlchemy store True).
- `create_user_agent(agent_id, name, bundle_location, owner, description=None) -> Agent`: inserts
  kind 2 with `created_by = owner`. Raises the store's integrity error on primary-key conflict.
  Default raises "unsupported".
- `list_user_agents(owner, limit, after) -> PagedList[Agent]`: N1 plus 6.7. Default returns empty.

Existing methods (`create`, `get`, `get_by_name`, `list`, `get_names`, `update`, `delete`) keep their
signatures and behavior. The `Agent` entity gains `kind` (`"server"` or `"user"`, populated by the
converter), so server-agent checks never infer "server" from a missing session or owner: a user agent
whose sessions were all deleted stays a user agent.

### 6.6 Install semantics

1. Client builds a `.tar.gz`: a directory is bundled with `${VAR}` references resolved from the
   caller's shell (same as `omnigent run`); a single YAML is materialized into a bundle; a `.tar.gz`
   is uploaded as is (`${VAR}` left literal).
2. Server validation (6.4) never imports or runs bundle code.
3. Id = derived id (decision 3); the blob key is `"<id>/<files hash>"` (decision 23). Existing row
   with that id: store the new blob, `update` (version + 1), evict cache (other processes follow,
   6.18). No row: store blob, `create_user_agent`.
4. Reinstalling the same files is a no-op, however the client tarred them.
5. Single-user local servers stamp owner `"local"` (the auth layer's local identity).

### 6.7 "My agents" listing algorithm

For each row read by N1, in order:

1. **Original only**: keep if `bundle_location` starts with `"<row id>/"` (accepting the legacy
   `ag_`-prefixed form of the id). Skip derived copies.
2. Stop when `limit` rows are kept or 50 rows are read.

Nothing else is filtered: two of the caller's agents may share a name (an install and an upload, or
one named like a server agent), and each is listed by its own id so it can be selected and removed.

Ceiling (documented in code): a user with more than 50 derived copies newer than an original sees that
original only on a later page. Upgrade path if it matters: a column marking derived copies.

### 6.8 Using an agent (binding rules, additive)

Applies to session create (JSON), fork targets, schedules, skills discovery, and sandbox previews.

- Existing rules unchanged: server agents bindable by anyone; a kind-2 row used by a session requires
  READ on a session's spawn-tree root (Q8).
- **Added allow**: the caller may always use a kind-2 row they own.
- **Shared rows**: forks of the owner's sessions share the row, so Q8 may pick any of several roots;
  when it denies a non-owner, READ on another root using the agent is enough (N5: the first 50
  distinct roots, so a source's children never crowd out its forks; a caller who can read only a
  later root is refused, and can still fork that session).
- **Added deny**: a kind-2 row used by no session (an install, or one whose sessions were deleted)
  returns 404 to anyone but its owner, and to everyone when it has no owner.
- **Added copy** (decision 20): when the existing READ rule lets a caller start a new session from a
  kind-2 row owned by someone else (or with no owner), the session is bound to a copy owned by the
  caller, made exactly as in 6.9, instead of to the original row. A child session keeps its tree's
  agent only when it names its parent's agent (collaborators act in the owner's session); naming any
  other agent copies it like a top-level session. A scheduled task binds the copy when it is created
  or switched to the agent, so every run uses it; a task saved before this rule is moved to its
  owner's copy at its next run. On a store without user-agent support the session binds as today.

### 6.9 Fork semantics

Let the source session use agent A; the caller is C; optional target agent T (fork into another agent).

- No target, A is kind 1, or A is owned by C: the fork uses A (no new row).
- No target, A owned by another user or ownerless: copy A for C. The bundle bytes are written under a
  new id (`"<new id>/<sha>"`), and a new kind-2 row is inserted inside the fork transaction (existing
  atomic clone path) with `name` = A's name, `description` = A's description, `created_by` = C.
- With target T: T must pass 6.8. T is kind 1 or owned by C: use T. T owned by someone else: copy as
  above.
- The bundle is copied after every fork validation, right before the fork transaction, and deleted
  if the fork fails.

### 6.10 Switch-agent removal

Deleted: `SessionSwitchAgentRequest`, `ConversationStore.switch_conversation_agent` (abstract and
SQLAlchemy), the switch-back label constant and its handling, the runner-reset helper used only by
switch, the web dialog and its API client function, the browser and e2e tests for switch, and the
API docs for it.

Kept as an empty, reserved endpoint: `POST /v1/sessions/{id}/switch-agent` stays registered (hidden
from the OpenAPI schema) and always returns **410 Gone** with the message "Switching a session's agent
in place is not available. Fork the session into another agent instead." A code comment marks it as a
placeholder: in-place switching will return with a new request shape in a future release (section
16), so the path stays reserved. One route test asserts the 410.

Kept: the `session.agent_changed` event and its web and REPL handling (MCP edits still publish it);
fork-into-another-agent; the runtime guards that react if a session's binding ever changes (comments
updated, logic untouched).

### 6.11 Session deletion

`delete_conversation` no longer collects or deletes agent rows. Everything else it deletes is
unchanged.

### 6.12 Agent removal and broken sessions

- Removal: 6.4 `DELETE`.
- Sessions using a removed agent keep running while the runner holds the spec (decision 11).
- When the agent cannot be loaded: `GET /v1/sessions/{id}/agent` and `.../agent/contents` return 404
  with the message "This agent no longer exists. Fork this session into another agent to continue."
  (404 is required: the runner's spec resolver treats only 404 as "agent missing"). The runner then
  reports its existing `session_agent_missing` failure.
- Forking such a session without a target returns 410 `session_agent_missing` with the same message;
  forking into another agent works.
- The web error card gains a description for `session_agent_missing`.

### 6.13 Environment expansion

`${VAR}` in a spec may be expanded against the server process environment only for operator-authored
agents. The rule moves from "no owning session" to "a server agent (`kind` server) with no session
and no owner" (`Agent.operator_authored`), applied at every load site and in the runner's
`X-Agent-Session-Scoped` header. Server agents behave exactly as before; user agents never expand,
including unused installs and agents whose sessions were deleted.

### 6.14 MCP server edits

No change in behavior (decision 17). Editing is still limited to non-native user agents and to the
agent's owner. With decision 7, all sessions using a user agent belong to its owner, so an edit
applies across the owner's sessions. No code change is needed in the editability check: it is reached
only from a session that uses the agent, so the reverse lookup always resolves a session and server
agents stay read-only. Tests lock this in.

### 6.15 Picker (web)

- `useAvailableAgents` keeps its two sources (server catalog, recent-session scan) and merge.
- New third source, only when `/v1/info` reports `agent_install`: `GET /v1/agents?scope=user&limit=50`
  (the picker follows the cursor until it has 50 agent names, at most 5 pages, decision 22).
- Merged by root name with the same newest-wins rule as recent-session uploads, ranked by last change
  (`max(updated_at, created_at)`), so the entry shown for a name is the agent the user installed,
  imported, or edited last, and its id is always selectable. Built-in names keep the built-in. Other
  same-named agents stay reachable from their sessions and through `omnigent agent list`.
  A user agent the session scan also finds is ranked only by these agent timestamps, so starting or
  forking a session never changes which same-named agent is shown.
- Sorting and grouping (`sortAgentsForDisplay`, `partitionAgentsByKind`) unchanged; user agents land
  under Agents, "Other..." as custom agents do today, whatever their harness: one on a native CLI
  harness is still the user's agent, so it never joins the Harnesses rows (nor stands in for the
  Claude Code wrapper that Smart Routing binds).
- The earlier draft's merge changes (an "installed" flag beating session copies, native-dedupe
  exemption) are reverted.

### 6.16 CLI

```
omnigent agent add <dir | file.yaml | bundle.tar.gz> [--server URL]
omnigent agent list [--server URL]
omnigent agent remove <name | id> [--yes] [--server URL]
```

- Every command first checks `/v1/info`; a server without `agent_install` gets
  "<server> does not support installing agents; upgrade the server to use `omnigent agent`."
- `list` pages `scope=user` to completion and prints a table of name, version, harness, and id.
- `remove` resolves an id or a name in `scope=user`; a name several agents share is refused with
  their ids, so the user removes one by id. It sends `DELETE`; on 409 shows the session count and
  asks "Remove anyway?"; `--yes` sends `force=true` directly.
- Registered in the top-level subcommand allowlist and listed in the feature map.

### 6.17 UI import

- "Import bundle" in the Create custom agent dialog (shown only when `agent_install` is true) uploads
  a `.tar.gz` to `POST /v1/agents`.
- On success, wait for the picker to refresh, then select the new agent (decision 12 makes it the
  entry shown for its name); if it is not selectable after a successful install, say so instead of
  closing silently.
- On failure, keep the dialog open and show the server's message. Cancel and Create are disabled while
  an import is in flight.

### 6.18 Revision changes reach running sessions

A reinstall or MCP edit keeps the agent id and changes `bundle_location`. Every cache keyed by id
must notice:

- **Server `AgentCache`**: entries remember the bundle location they were built from, in memory and
  as a marker file published with each extracted directory. A load naming another location rebuilds,
  so every worker and every process sharing the cache directory follows the change.
- **Runner session state**: each turn forwarded to the runner carries `agent_revision` (the agent's
  current `bundle_location`). When it differs from what the session's caches were built from, the
  runner drops that session's spec, tool schemas, and skills, as the MCP-edit reset does, and resolves
  the new bundle. The harness process keeps running. This needs no fan-out, so it works whichever
  replica the runner is attached to. Kickoffs carry it too (a create's initial items and scheduled
  runs). A spec cached by a turn without a revision (an older server) is rebuilt when the first
  revision arrives.
- **Runner bundle cache**: extracted bundles are keyed by agent id, version, and a digest of the
  bundle bytes, so an agent removed and added again (its version restarts at 1) never reuses the old
  directory.

A native terminal session (a TUI) keeps the configuration it launched with until it relaunches. `omnigent agent add` says so when it
updates an agent.

## 7. Security considerations

- **Server secrets**: user agents never expand `${VAR}` against the server environment (6.13).
- **Untrusted bundles**: validated with the existing upload checks (callable-tool and escaping-cwd
  rejection, safe extraction with size and entry limits) and a 100 MiB upload cap; nothing executes at
  install time.
- **Cross-user code injection**: other users' agents are never shared into your sessions, by fork,
  new session, child session, or schedule; you get a copy with its own blob (decisions 7 and 20).
- **Cross-site install**: a page on another origin cannot install or replace an agent through the
  browser; `POST /v1/agents` applies the same `Origin` check as multipart session create.
- **Visibility and removal**: "my agents" and removal are owner-only. Derived ids are predictable from
  owner and name, but using an agent still requires ownership or READ on a session that uses it.
- **Shared sessions**: collaborators acting in the owner's session run the owner's agent (unchanged);
  only the owner can edit it.

## 8. Compatibility

| Combination | Behavior |
|---|---|
| New server, old web client | Picker as today (no `scope=user` fetch). Installed agents reachable once used in a recent session. |
| Old server, new web client | `agent_install` false: no Import button, no third picker source. |
| Old server, new CLI | Clear upgrade message. |
| Clients calling switch-agent | 410 Gone with fork guidance (intentional API break, labelled as such). |
| Legacy rows | Ownerless kind-2 rows are not listed by "my agents" (still via the session scan) and not removable via CLI. Legacy fork and switch copies are hidden by the original rule. Rows older servers created per run stay and are listed; the picker shows one entry per name (decision 22) and new uploads never bind them. Nothing is deleted. |
| Deployments applying schema outside alembic | Must add the index before enabling the feature; until the store supports user agents the feature reports off. |
| Old and new servers sharing one database (rolling deploy across replicas, or a rollback) | Old servers treat an agent no session uses as unowned. Accepted risk; see section 11. |

## 9. Testing plan

### 9.1 Unchanged behavior (one test per row, asserting today's result)

For each of: kind-1 row; kind-2 row used by a session; kind-2 row with NULL owner.
- `${VAR}` expansion decision and runner header.
- Binding from session create, fork target, schedules, skills, sandbox preview.
- MCP editability (kind 1 rejected; kind 2 owner allowed; non-owner rejected).
- Default `GET /v1/agents` payload and order.
- Picker merge output and order for existing sources.

### 9.2 New behavior

- Store: `create_user_agent`, `list_user_agents` (50-row read cap, originals only, legacy prefix,
  repeated names listed by id, cursor = last read row, stale cursor), defaults on a store without
  support; capped session count.
- Migration: index columns, non-unique, downgrade, concurrent path on PostgreSQL (SQL emitted).
- Routes: install (new, reinstall in place, identical no-op, concurrent race, server-name 409, 413,
  invalid bundle 400, foreign `Origin` 403), `scope=user` (owner isolation, paging, a same-named upload
  and install both listed and removable by id), removal (owner-only, 409 count capped, force),
  `/v1/info`.
- Copies: a child under the caller's own root and a schedule both copy another user's agent; a child
  naming its parent's agent does not; a task saved on another user's agent moves to a copy at its run.
- Revisions: two sessions on one agent both forward the new revision after a reinstall; the runner
  resolves the spec again only when the revision changes; the server cache follows a bundle another
  process replaced; the runner bundle cache keeps an agent added again off the old directory.
- Fork: own session reuses row; other user's session copies with new blob and owner; ownerless source
  copies; target owned by other user copies; copy unaffected by later reinstall of the original.
- Uploads: re-tarred uploads of the same files bind one row; changed files, another owner, and a
  sub-agent child each get their own; runs after an MCP edit share one new row holding their files;
  reinstalling re-tarred files is a no-op; the files hash ignores archive metadata and follows
  extraction for a repeated path.
- Switch removal: the reserved route returns 410 with the fork guidance, the store method is gone,
  fork-into-agent unaffected.
- Session delete: agent rows remain.
- Removed agent: agent reads 404 with message; fork 410; web error card text.
- CLI: add (directory contents uploaded), list (with ids), remove by name or id with prompt and
  `--yes`, an ambiguous name refused, older-server message, entry point dispatch through `main()`.
- Web: picker merge rule (one entry per name, the last-changed user agent shown, built-in names
  kept), paging past a page of one name's rows, import gating, selection, error states.

### 9.3 Live verification (isolated local server)

1. Install an agent; it appears in the picker without restart; survives server restart.
2. Start a session, re-pick it, fork it: one picker entry, one agent row.
3. Second user forks the first user's session: second user gets their own row (visible in their
   picker); first user reinstalls; second user's fork still runs the old bundle.
4. Remove the agent: warning with count, decline, accept; sessions keep running; after runner restart
   the session shows the fork message; forking into another agent works.
5. Older-server path for CLI and UI.
6. Two running sessions on one agent; reinstall with a changed instruction; both use it next turn.
7. Remove and add again with a changed bundle; a new session on the same runner uses the new one.
8. Upload an agent through a session, then import a bundle with the same name: the composer selects
   the import; `agent list` shows both by id and `agent remove <id>` removes the intended one.
9. Second user schedules a run from the first user's agent: the task binds the second user's copy.

### 9.4 CI

Watch each PR; before attributing a failure, check whether it also fails on `main`.

## 10. PR stack (native stacked PRs)

| Order | PR | Scope | Base |
|---|---|---|---|
| 1 | Schema: `ix_agents_kind_owner_created` (existing #8777) | model index, migration (concurrent on PostgreSQL), migration tests, deployment note | `main` |
| 2 | Retire the switch-agent API to a reserved 410 stub (new) | 6.10, docs, OpenAPI, feature map; intentional API break label | 1 |
| 3 | Fork reuses your agent; cross-user copy (new) | 6.9, shared-row binding in 6.8 | 2 |
| 4 | User agents: install, list, remove; session delete keeps agents (existing #8677, rebuilt) | 6.4 to 6.8, 6.11 to 6.13, 6.15, 6.16, 6.18, codec rename, `Agent.kind` | 3 |
| 5 | UI import (existing #8680, rebuilt) | 6.17 | 4 |

- Reviewers: only PR 1 (migration code owners). Push top-down to avoid CODEOWNERS churn on children.
- Each PR is independently testable; PRs 2 and 3 are behavior changes with before/after tests.

## 11. Rollout

1. Merge PR 1; deployments apply the index.
2. Merge PRs 2 and 3 (independent of the index).
3. Merge PRs 4 and 5. The feature turns on wherever the store reports support; elsewhere it stays off.

**Mixed versions (accepted risk).** While old and new servers share one database (a rolling deploy
across several replicas, or a rollback past this release), old servers misread agents no session
uses: anyone who knows such an agent's id can bind it, and deleting a session through an old server
deletes the agents its tree uses even when other sessions still use them (installs, forks sharing a
row). A binding made this way outlives the window. Accepted for this release because a single server
process never mixes versions, only derived ids can be computed (an install's from owner and name,
an upload's also from its files, decision 23; copies have random ids), and the window lasts only as
long as the deploy.

- Upgrade all replicas together, and prefer rolling forward over rolling back.
- Before rolling back past this release, delete user agents that no session uses.
- If the risk changes: ship the read side (owner check on binding, no `${VAR}` expansion for owned
  agents, deleting an agent only once no session uses it) one release before the parts that create
  such rows.

## 12. Known limitations and follow-ups

- Install from a git URL (follow-up).
- UI removal of agents (follow-up).
- Dropping `ix_agents_created_at` (D5) once no older server uses it.
- The 50-row read ceiling in 6.7. The picker reads up to 5 pages (decision 22), so an agent behind
  more than 250 newer rows, or older than your 50 newest agents, is missing from it until it is used
  in a recent session; `omnigent agent list` shows all. Ordering the listing by last change would
  lift that.
- Uploads whose files differ on every run (a generated timestamp, say) still get a row each, as do
  sub-agent uploads. Rows older servers created per run stay until removed. If users pile up many,
  a dedupe command could merge identical ones: move their sessions and scheduled tasks onto the row
  new runs bind (runners re-read a session's agent each turn), then delete the rows nothing uses.
- MCP edits apply to every session using the agent; per-session MCP overrides are not supported.

## 13. Open items

None. The former open items are recorded as decisions 20 to 22.

## 14. Appendix A: evidence (clean `main`, isolated local server)

- Create custom agent `orion-ui` → kind 2 row `E4704556`, owner `local`, session A → `E4704556`.
- Re-pick via JSON create → no new row, session B → `E4704556`.
- Fork B → new row `7CC21562`, same name, `bundle_location` `e4704556.../e70d...` (source's prefix).
- Session from Claude Code → binds kind-1 `58A1BC5B`; fork → new kind-2 `863BA7CF`, same
  `bundle_location` as the built-in.
- Switch B to Polly (shared agent case) → old row deleted, new row `"polly (switch ...)"` with NULL
  owner; session A then fails `Agent not found`.
- SDK session (openai-agents, mock model) answered turn 1; after deleting its agent row, turn 2 still
  answered from the runner cache.

## 15. Future: an agent marketplace

Goal of a future release: users publish agents so others can install them, either on the same server
or from a global catalog shared across servers.

### 15.1 What this design already provides

- **Install is the consumer side of a marketplace.** Installing from a listing is the same operation
  as `omnigent agent add`: fetch a bundle, validate it as tenant input, and create a user agent owned
  by the installer. The git-URL install follow-up builds the same "fetch, then `POST /v1/agents`" path.
- **Installed copies are isolated.** Decisions 7 and 20 already establish that using someone else's
  agent gives you your own copy with its own blob. A marketplace install follows the same rule, so a
  publisher changing or removing a listing never changes code running in an installer's sessions.
- **Ownership and visibility are already separate concepts.** Server agents are global, user agents
  are owner-only. A marketplace adds a third visibility (published) without changing either.
- **Trust handling is already tenant-grade.** Installed bundles never expand server `${VAR}`, are
  validated, size-capped, and never executed at install.

### 15.2 What a marketplace would add

- **Listings, not rows in `agents`.** A separate table (for example `agent_listings`: listing id,
  publisher, name, description, immutable published bundle reference, version, visibility scope
  `server` or `global`, created and updated times) keeps the `agents` table meaning "agents someone
  uses". Publishing snapshots the publisher's current bundle into an immutable listing version, so
  later edits by the publisher do not change what others install until a new version is published.
- **Provenance on installed agents.** To support "update available" and "installed from", the
  installed row needs the listing id and version. This is the one likely schema addition (nullable
  columns on `agents`, or a small join table). It is additive: existing rows keep NULL.
- **Install identity.** Today the install id is derived from owner and name, so installing two
  different listings with the same name collides. With a marketplace the derivation should include the
  source (for example owner + listing id), and the picker's one-entry-per-name display needs a
  qualified display name (publisher/name). The current scheme stays valid for local installs.
- **Updates.** Upgrading is a reinstall from a newer listing version (update in place, version + 1),
  which this design already supports. Pinning means simply not upgrading.
- **Listing queries.** Browsing a marketplace is a new bounded query on the listings table (for
  example `(workspace_id, visibility, created_at, id)`), not on `agents`, so it does not affect the
  index added here.
- **Global marketplace.** A cross-server catalog is an external registry; the server only needs
  "install from registry" (download and verify, ideally with signatures and publisher identity), which
  lands in the same install path.
- **Promotion to server agents.** Making a published agent available to everyone on a server without
  per-user installs is an admin action that registers it as a server agent (kind 1), which the server
  already supports for operator agents.

### 15.3 Constraints this spec keeps so the marketplace stays easy

- No sharing of agent rows across users (decisions 7 and 20), so publish/install never has to unwind
  shared rows.
- Blobs are content-addressed and copied per owner, so listings can reference immutable blobs.
- "My agents" lists originals owned by the caller, so marketplace installs (originals with their own
  blob) appear in the picker without changes.

## 16. Future: in-place agent switch (reworked)

Goal of a future release: change the agent of an existing session in place, keeping its transcript,
files, and workspace.

### 16.1 How it fits the new model

With this design, sessions do not own agents, so a switch becomes a pure re-point:

1. Validate the target with the binding rules (6.8). If the target belongs to another user (or has no
   owner), make the caller's copy first (decision 20).
2. In one transaction, set `conversations.agent_id` to the target and apply the session-level deltas
   (reset model settings across provider families, clear the old harness's native session id, update
   presentation labels and carry-history labels).
3. Do **not** create or delete any agent row. The old agent stays in `agents` (other sessions and the
   picker may still use it, and only explicit removal deletes it); the new agent was already there.
4. Reset the old harness's runner resources and publish `session.agent_changed` so clients refresh
   (the event and its client handling are kept by this stack).

This is exactly "both old and new agents still live in `agents`; the session row is just re-pointed".

### 16.2 What is already in place for it

- The reserved `POST /v1/sessions/{id}/switch-agent` path (410 today) can take the new request shape.
- `session.agent_changed`, the web and REPL refresh on that event, the runtime guard that respawns a
  harness when a session's binding changes, and the policy builder's binding check all remain.
- The removed implementation (runner reset, carry-history labels, provider-family checks) is in git
  history and can be reused; only its agent-row copy and delete steps are dropped.
- A "switch back" pointer can be a session label holding the previous agent id, since that agent is
  never deleted by the switch.

### 16.3 Open questions for that release

- Native harnesses: confirm each harness can relaunch on a different agent mid-session and which ones
  can rebuild history (the fork path's carry-history rules are the starting point).
- Concurrency: reject while a turn is running (the removed route did).
- UI entry point and permissions (owner only, or editors too).
