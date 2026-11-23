# Gadgets

A gadget is a small web app an agent builds for a person or a team: a board, a tracker, a report.
It lives next to the sessions that work on it. People use it in the browser; its buttons hand work
to an agent session; agents read and change its records, publish new versions and answer its
comments.

## What a gadget is

- **Revisions.** Immutable HTML documents written against the gadget SDK, each declaring the
  capabilities it uses (`data.read`, `data.write`, `sessions.read`, `sessions.create`), its
  collections (JSON-schema shaped records with rules) and its actions. Publishing points the
  gadget at one revision.
- **Records.** Documents in the gadget's collections, versioned for optimistic concurrency
  (`if_version`) and idempotent writes (`operation_id`). Rules from the revision are enforced on
  every write.
- **Action ledger.** A button in the gadget queues an action (name plus params). The web hands
  it to the viewer's *gadget session* as a message; the agent sets it running, done or failed.
  Progress is derived from the ledger and the state of the sessions doing the work.
- **Comments.** Anchored to a selection in the gadget; agents list them and mark them addressed.
- **Shelf.** Per user: pinned gadgets (listed in the sidebar) and the gadgets each session
  shows in its side panel. The session a person last opened a gadget in becomes their gadget
  session for it.

## Where it runs

- **Server** (`omnigent/gadgets/`, routes under `/v1/gadgets`, feature `gadgets`): the
  service, record rules, action progress, comments, shelf, and sharing routes
  (`/v1/gadgets/{id}/permissions`, the same shape as workflow sharing).
- **Agents** get `sys_gadget_*` builtin tools from the runner, which call those routes as the
  session's owner (like the journal tools), so no extra credentials or MCP server are needed.
  An agent's call from a session shared with edit access is refused, reads included, so a
  collaborator cannot read the owner's gadgets through the owner's agent. The tools'
  descriptions stay short; how to work as a gadget session, build a page and use the SDK is in
  the `gadget` framework skill (`omnigent/resources/skills/gadget/SKILL.md`), which agents get
  with the tools.
- **Web**: the Gadget list and New gadget dialog, a gadget tab in a session's side panel
  (docked, or full screen with only the gadget and an Open session button), the gadget beside
  New session when there is no session yet, pinned gadgets under Gadget in the sidebar,
  sharing, comments, and Reply / Ask in side chat from a selection in the gadget.
- **The frame**: published HTML runs in a sandboxed iframe (`sandbox="allow-scripts"`, opaque
  origin, a CSP that blocks network requests) and reaches its data only through the SDK's
  postMessage bridge, which the web checks against the viewer's permissions. See [What gadget
  code can send out](#what-gadget-code-can-send-out) for what that boundary does not cover.

## Extension points

A deployment can replace three pieces without forking:

| Piece | Default | Interface |
| --- | --- | --- |
| Storage | JSON files under `<data_dir>/gadgets` | `GadgetStore` (`omnigent/stores/gadget_store`) — gadget metadata plus a small create-only JSON file interface the record store is built on |
| Access | owner plus grants in the `PermissionStore`, like sessions and workflows | `GadgetAccess` — a viewer's `read` / `write_data` / `publish` / `manage` on a gadget, and the gadgets shared with them |
| Frame | sandboxed `srcdoc` iframe | the web host config's gadget frame, for hosts that serve gadgets from an isolated origin |

`create_app(gadget_store=..., gadget_access=...)` takes custom implementations; the CLI passes
the defaults.

### Why a document store, not the relational stores

Everything a gadget keeps beyond its metadata (revisions, publication, records, comments, the
action ledger, rule state, and each viewer's shelf) is a revisioned JSON document on
`GadgetFiles`, a create-only file interface, rather than rows in the server's SQL tables:

- **The data belongs to generated apps, not to the server.** A gadget declares its own
  collections and JSON schemas at publish time and changes them between revisions; the server
  validates records against those schemas but has no table shape of its own for them. Mapping
  each gadget's collections to tables would mean schema migrations driven by agent output.
- **The guarantees the SDK promises fit create-only files.** Each change is a new file named
  by its revision number, so concurrent writers collide on one atomic create:
  `if_version` replaces, `operation_id` retries, tombstones that keep history, and
  digest-chained revisions that fail closed on damage need no transactions or locks, and the
  interface a deployment implements is four calls (`read_json`, `create_json`, `list_paths`,
  `mkdirs`, plus an optional `delete_tree`).
- **Deployments replace it whole.** The default keeps files under `<data_dir>/gadgets`; a
  deployment that stores user content elsewhere (for example in a workspace file system,
  where gadgets inherit that system's permissions and quotas) implements `GadgetStore` without
  touching the server's database. A database-backed `GadgetFiles` is a table of
  `(scope, path, value)` rows with a unique key.
- **Every read and write is bounded.** Documents are at most 32 KiB, HTML at most 1 MiB,
  collections and records are capped, listings page with cursors and examine a fixed number
  of entries per call, and a record's revisions roll into segments so finding its head reads
  at most two directory listings. Deleted records give their capacity back after a
  retention period (seven days by default) when the store can delete.
- **Lifecycle follows the data directory.** Backing up or moving `<data_dir>` carries gadgets
  with sessions; deleting a gadget or an account removes its scope in one rename.

The server's relational stores keep what the rest of the server queries across users: the
grants that share gadgets live in the `PermissionStore` (`gadget_shares`), like session and
workflow shares. Moving the documents to SQL would be worth it if gadgets needed queries
across gadgets or users (search over records, reporting), or transactions across records;
both are out of scope below.

Alternatives considered:

- **Fixed metadata and index tables, with record values in the `ArtifactStore`.** A table of
  `(gadget, collection, record, revision)` rows plus artifact blobs splits each write across
  two systems: the create-only revision slot that makes `if_version` and `operation_id` safe
  would become a SQL insert, the value an artifact write, and a failure between them leaves a
  row without its value or an orphan blob, which the file store never does. It also puts
  every record write of every gadget on the shared database, for data no server query reads.
- **Revisions' HTML in the `ArtifactStore`.** Artifacts are keyed blobs without create-only
  writes, ordering or a link between versions; a revision would still need a document naming
  it, its capabilities and its digest, which is exactly the revision file. The HTML is part of
  that one immutable file (at most 1 MiB), so publishing never points at a missing blob.
- **Session-style tables per gadget concept** (revisions, records, comments, actions, shelf).
  The record values remain app-defined JSON, so the tables would hold opaque JSON columns
  with the same bounds, while adding migrations and cross-dialect work for each concept.

This is a new persistence path beside the relational stores, so it asks for a maintainer
design review before it is relied on; the `GadgetStore` interface keeps the choice
replaceable either way.

## Permissions

| Grant | read | write_data | publish | manage |
| --- | --- | --- | --- | --- |
| Can view (`LEVEL_READ`) | yes | | | |
| Can edit (`LEVEL_EDIT`) | yes | yes | yes | |
| Can manage (`LEVEL_MANAGE`), owner | yes | yes | yes | yes |

Only those who may publish read unpublished revisions. Deleting an account deletes the gadgets
it owns, with their grants and its shelf, and its username cannot be registered again until
they are gone, as for workflows.

The frame's write and action controls follow these: the SDK disables controls marked
`data-omnigent-requires="write"` or `"action"` for viewers without that access.

## What gadget code can send out

The frame's CSP blocks every network request a page can make on its own: `fetch`, XHR,
WebSockets, scripts, styles, fonts, images and other subresources, and forms. It cannot stop the
gadget's document from navigating itself (setting `location`, for example), and browsers offer
no policy that does: that navigation's request leaves the browser before the web app can react,
and its URL can carry anything the gadget had read. So treat gadget code as able to send out
anything it can read:

- the gadget's own records and comments, which everyone the gadget is shared with can read
  anyway;
- the viewer's session list and session details, only after the viewer agreed to let that
  gadget read them (asked once per page load).

When the frame loads a second document, the web app closes its port at once, stops the frame
and tells the viewer, who can reload it; it does not reload it on its own. A gadget is written by
an agent working for someone who can publish it, so the risk is a page that an agent was
manipulated into writing; review a gadget before granting it session access.

## Not in scope

- Server-side execution of gadget code (gadgets run only in the viewer's browser).
- Cross-gadget queries, and data beyond a gadget's own collections.
