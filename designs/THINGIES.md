# Thingies

A thingy is a small web app an agent builds for a person or a team: a board, a tracker, a report.
It lives next to the sessions that work on it. People use it in the browser; its buttons hand work
to an agent session; agents read and change its records, publish new versions and answer its
comments.

## What a thingy is

- **Revisions.** Immutable HTML documents written against the thingy SDK, each declaring the
  capabilities it uses (`data.read`, `data.write`, `sessions.read`, `sessions.create`), its
  collections (JSON-schema shaped records with rules) and its actions. Publishing points the
  thingy at one revision.
- **Records.** Documents in the thingy's collections, versioned for optimistic concurrency
  (`if_version`) and idempotent writes (`operation_id`). Rules from the revision are enforced on
  every write.
- **Action ledger.** A button in the thingy queues an action (name plus params). The web hands
  it to the viewer's *thingy session* as a message; the agent sets it running, done or failed.
  Progress is derived from the ledger and the state of the sessions doing the work.
- **Comments.** Anchored to a selection in the thingy; agents list them and mark them addressed.
- **Shelf.** Per user: pinned thingies (listed in the sidebar) and the thingies each session
  shows in its side panel. The session a person last opened a thingy in becomes their thingy
  session for it.

## Where it runs

- **Server** (`omnigent/thingies/`, routes under `/v1/thingies`, feature `thingies`): the
  service, record rules, action progress, comments, shelf, and sharing routes
  (`/v1/thingies/{id}/permissions`, the same shape as workflow sharing).
- **Agents** get `sys_thingy_*` builtin tools from the runner, which call those routes as the
  session's owner (like the journal tools), so no extra credentials or MCP server are needed.
  An agent's call from a session shared with edit access is refused, reads included, so a
  collaborator cannot read the owner's thingies through the owner's agent. The tools'
  descriptions stay short; how to work as a thingy session, build a page and use the SDK is in
  the `thingy` framework skill (`omnigent/resources/skills/thingy/SKILL.md`), which agents get
  with the tools.
- **Web**: the Thingy list and New thingy dialog, a thingy tab in a session's side panel
  (docked, or full screen with only the thingy and an Open session button), the thingy beside
  New session when there is no session yet, pinned thingies under Thingy in the sidebar,
  sharing, comments, and Reply / Ask in side chat from a selection in the thingy.
- **The frame**: published HTML runs in a sandboxed iframe (`sandbox="allow-scripts"`, opaque
  origin, a CSP that blocks network access) and reaches its data only through the SDK's
  postMessage bridge, which the web checks against the viewer's permissions.

## Extension points

A deployment can replace three pieces without forking:

| Piece | Default | Interface |
| --- | --- | --- |
| Storage | JSON files under `<data_dir>/thingies` | `ThingyStore` (`omnigent/stores/thingy_store`) — thingy metadata plus a small create-only JSON file interface the record store is built on |
| Access | owner plus grants in the `PermissionStore`, like sessions and workflows | `ThingyAccess` — a viewer's `read` / `write_data` / `publish` / `manage` on a thingy, and the thingies shared with them |
| Frame | sandboxed `srcdoc` iframe | the web host config's thingy frame, for hosts that serve thingies from an isolated origin |

`create_app(thingy_store=..., thingy_access=...)` takes custom implementations; the CLI passes
the defaults.

## Permissions

| Grant | read | write_data | publish | manage |
| --- | --- | --- | --- | --- |
| Can view (`LEVEL_READ`) | yes | | | |
| Can edit (`LEVEL_EDIT`) | yes | yes | yes | |
| Can manage (`LEVEL_MANAGE`), owner | yes | yes | yes | yes |

Only those who may publish read unpublished revisions. Deleting an account deletes the thingies
it owns, with their grants and its shelf, and its username cannot be registered again until
they are gone, as for workflows.

The frame's write and action controls follow these: the SDK disables controls marked
`data-omnigent-requires="write"` or `"action"` for viewers without that access.

## Not in scope

- Server-side execution of thingy code (thingies run only in the viewer's browser).
- Cross-thingy queries, and data beyond a thingy's own collections.
