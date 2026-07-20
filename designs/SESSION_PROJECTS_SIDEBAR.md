# Design: Organize sessions into Projects in the sidebar

- Issue: [#863](https://github.com/omnigent-ai/omnigent/issues/863)
- Builds on: PR [#869](https://github.com/omnigent-ai/omnigent/pull/869) (community implementation of "collections")
- Status: Draft
- Author: Serena Ruan

## 1. Summary

Let users group related sessions into a named **Project** and render each project
as its own collapsible section in the sidebar. A session belongs to at most one
project. A project can be set at **session-start time** (optional picker in the new
chat flow) or later from the **session row kebab menu**.

Projects are *implicit*: a project exists as long as at least one session references
it, and disappears once its last session leaves. There is no separate
create/delete/rename lifecycle and **no DB migration** — membership is stored as a
row in the existing `conversation_labels` table under a reserved key.

This design adopts PR #869's backend and sidebar-grouping mechanics wholesale,
renames the user-facing/storage term from "collection" to **"project"**, and adds the
session-start entry point that #869 lacks.

## 2. Goals / Non-goals

### Goals
- Set a session's project optionally at session start, and change/remove it later via
  the row kebab.
- Group sessions by project in the sidebar, with per-project counts.
- One project per session. No nesting.
- Project membership is internal (a label) — never surfaced as a generic "label"
  chip in the UI.
- Server-side filtering: `GET /v1/sessions?project=<name>` (incl. `""` = unfiled) and
  `GET /v1/sessions/projects` for the distinct, ACL-scoped name list + counts.
- No schema migration; no new dependencies.

### Non-goals
- No multi-project membership, no nested projects.
- No explicit project entity / rename / color / description in v1. (Rename is
  achievable by moving every member to a new name; see §7.)
- No automatic grouping by repo/workspace/host — grouping is purely user-defined
  (per issue discussion consensus).

## 3. Terminology

User-facing term: **`project`**. Internal reserved label key: **`omni_project`**.

The label key is namespaced (`omni_*`) to keep the internal storage key distinct from
the user-facing term and from any future reserved keys; it is never shown in the UI.

- The issue forbids "folder" (collides with runner workspace folders in the file
  pickers). #869 chose "collection"; we choose **"project"** to match the kebab UX in
  the reference screenshot and the "create/select a project at session start" model.
- Collision check: `project` does not appear as a code concept in the server or web
  UI today — the only matches are example filesystem paths (`~/projects`) in the
  workspace pickers, a different context. The minor residual risk is conceptual
  (workspace dirs are colloquially "projects"); we accept it since the feature is
  explicitly about user-defined grouping, not directories.

> Migration note from #869: rename the reserved key `"collection"` → `"omni_project"`,
> the endpoint `/sessions/collections` → `/sessions/projects`, the query param
> `?collection=` → `?project=`, and the hooks/components accordingly. Since #869 is
> not merged, this is a straight rename, not a data migration.

## 4. Storage

Reuse `conversation_labels` (`SqlConversationLabel`, `db_models.py:507`):

| column          | value                          |
|-----------------|--------------------------------|
| conversation_id | the session id                 |
| key             | `"omni_project"` (reserved)    |
| value           | the project name               |
| updated_at      | last write (epoch seconds)     |

- A session is **in a project** iff it has a `(key="omni_project")` row; the project
  name is that row's `value`.
- A session is **unfiled** iff it has no `omni_project` row.
- "Removing from a project" = deleting the row (not upserting an empty string).
- Implicit lifecycle falls out for free: distinct `value`s (where `key="omni_project"`)
  = the set of projects;
  when the last member is moved/deleted, no rows remain and the project vanishes.

### Label invisibility
`omni_project` is a reserved key and must be excluded from any surface that renders
generic session labels (the `labels` dict flows into `SessionListItem` and is used for
guardrail/sensitivity display). Audit and filter `omni_project` out of those surfaces so
it never appears as a label chip. (This is the one gap #869 did not explicitly address.)

## 5. Backend

Adopted from #869 (renamed `collection` → `project`):

### 5.1 Store (`conversation_store/sqlalchemy_store.py`)
- `list_projects(accessible_by) -> list[str]` — distinct `value` where
  `key="omni_project"`, ordered alphabetically, ACL-scoped to sessions the user has a
  permission row for (mirrors `list_conversations`'s ACL filter).
- `delete_label(conversation_id, key)` — no-op if absent; used for "remove from
  project" (`key="omni_project"`).
- `list_conversations(..., project: str | None)`:
  - `None` → filter disabled.
  - `""` → only sessions with **no** `omni_project` label (unfiled).
  - non-empty → only sessions whose `omni_project` label equals it.

**Add (new vs #869):** per-project **counts**. `list_projects` should return
`list[{name, count}]` (ACL-scoped `GROUP BY value`) so the sidebar can show accurate
counts and the start-time picker can rank by size without paging. This is the key fix
for the pagination problem in §8.

### 5.2 Routes (`server/routes/sessions.py`)
- `GET /v1/sessions/projects` → `[{name, count}]`, ACL-scoped. **Must be registered
  before `GET /sessions/{session_id}`** (FastAPI matches in registration order, else
  `projects` is captured as a `session_id` and 404s).
- `GET /v1/sessions?project=<name>` — filter, incl. `""` for unfiled.
- `PATCH /v1/sessions/{id}` with `{labels:{omni_project:"X"}}` to set;
  `{labels:{omni_project:""}}` is special-cased to `delete_label(id, "omni_project")`
  before the bulk label upsert so other labels are untouched. (The web API uses the
  internal key in the `labels` map; the user-facing query param / endpoint stay
  `project`.)
- Permission: setting/removing a project requires **edit** (not owner) — it is not the
  archive path. Confirm against `update_session`'s `required_level` logic.

### 5.3 Set-at-creation
`POST /v1/sessions` should accept the project in its `labels` (as
`{omni_project: "X"}`) so the start-time picker sets membership atomically at creation
rather than racing a follow-up PATCH. If the
create path already threads `labels`, reuse it; otherwise PATCH immediately after
create (acceptable fallback).

## 6. Frontend (`web`)

### 6.1 Hooks (`hooks/useConversations.ts`) — from #869, renamed
- `useProjects()` → `GET /v1/sessions/projects`, `queryKey: ["projects"]`,
  `staleTime: 30_000`. Returns `{name, count}[]`.
- `useMoveToProject()` → `PATCH /v1/sessions/{id}` with `{labels:{omni_project}}`; on
  success invalidate **both** `["conversations"]` (rows re-group) and `["projects"]`
  (counts/section list refresh). Empty value removes.

### 6.2 Sidebar (`shell/Sidebar.tsx`, `shell/sidebarNav.ts`) — from #869, renamed
- Section order / precedence: **Archived > Pinned > Project > Recent** (see §7).
- Project sections render between Pinned and Recent, one per name from `useProjects()`,
  driven by the **server project list** (a stale label with no matching project entry
  stays in Recent — projects are list-driven, not label-driven).
- Collapsible, persisted in the existing `omnigent:collapsed-sidebar-sections`
  localStorage key. Default: **collapsed** (projects can be numerous).
- Per-section count from `useProjects()` (server-authoritative, not the loaded page).
- A collapsed project surfaces the aggregate `SessionStateBadge` of its hidden rows
  (unread / needs-response / running), dropped once expanded — keep #869's behavior.
- Pinned-inside-a-project: a pinned session that is in a project stays in the project,
  sorted first; the global Pinned section holds only **unfiled** pins (see §7).

### 6.3 Session-start picker (`shell/NewChatDialog.tsx`) — **new vs #869**
- Optional "Project" control in the new chat flow: typeahead over `useProjects()` +
  "Create new…" inline (typing a new name) + "No project" (default).
- Mirrors the kebab UX in the issue screenshot (search existing + create new).
- On submit, pass `labels:{project}` into `POST /v1/sessions` (§5.3).

### 6.4 Kebab menu (`ConversationRow` in `Sidebar.tsx`) — from #869, renamed
- "Add to project ▸" (unfiled) / "Change project ▸" (filed) submenu: search existing
  projects, "New project…" inline, and "Remove from project". `data-testid`
  `move-to-project`.
- **Remove is confirmed only when it deletes the project.** Because projects are
  implicit, removing the *last* session deletes the project. "Remove from project" first
  checks server-side (`fetchProjectSessionIds`, archived included — accurate regardless
  of the loaded window or pin placement) whether this is the only session; if so it opens
  a confirmation that says so explicitly ("the project will be removed as well; the
  session itself is kept"). When other sessions remain, removal applies immediately. So
  does moving a session to a *different* project.

## 7. Precedence (pinned / archived / project)

A session can simultaneously be archived, pinned, and in a project. Exactly one
section owns each row. Order, highest wins:

**Archived > Pinned > Project > Chats**

- **Archived** sessions always go to the Archived section, regardless of project/pin
  (archiving is the strongest signal; an archived session should not clutter a project).
- **Pinned (filed or unfiled):** always rendered in the flat global Pinned section.
  Pinning a session in a project **moves it out** of that project into Pinned (issue
  item 6: "once a session is pinned it moves into Pinned; no nested grouping under
  projects"). A project whose only member gets pinned shows "No chats" until unpinned.
  Unpinning returns the session to its project (the project label is never touched by
  pinning).
- Everything else: Chats (or Shared with me, by ACL).

Rename, in the implicit model, is "move every member to a new name" — out of scope as
a first-class action in v1, but the move-to-new-name path makes it possible manually.

## 8. Pagination & correctness

The session list is cursor-paginated (default 20/page). Pure client-side grouping over
the loaded window would under-count projects and hide members on unloaded pages.
Mitigations:

1. **Counts** come from `GET /v1/sessions/projects` (server `GROUP BY`), never from the
   loaded page — so a collapsed project shows the true count even with one page loaded.
2. **Section membership** when expanded: a project section must show *all* its members,
   not just those in the loaded window. Two options:
   - (a) Lazy-fetch on expand via `GET /v1/sessions?project=<name>` (its own paged
     query), like the pinned-backfill pattern (`usePinnedConversationBackfill`).
   - (b) Backfill project members into the main list the way pins are backfilled.
   - **Recommendation:** (a) — fetch a project's rows on first expand. Keeps the main
     infinite query simple and scales to many projects without over-fetching collapsed
     ones.
3. The shared-with-me section is ACL-driven; project ACL scoping already matches the
   session-list ACL (store filter), so a shared+filed session appears under its project
   only if the user can access it.

## 9. Edge cases / decisions to confirm

- **Name semantics:** trim whitespace; reject empty/whitespace-only names; max length
  (propose 100 chars). **Case sensitivity:** the screenshot shows "Test" and "test" as
  distinct — propose **case-sensitive, exact-match** names (simplest, matches distinct
  `value`). Flag for confirmation.
- **Uniqueness scope:** per-user (ACL-scoped list), so two users' identically named
  projects are independent.
- **Search:** while a search query is active, flatten results (no project sections) —
  search is a global find, grouping resumes when cleared.
- **Ordering:** projects alphabetical (server `order_by(value)`); sessions within a
  project by the list's existing sort (updated_at desc), pinned-first.
- **Empty state:** no projects → no project sections; sidebar looks exactly as today.

## 10. Testing

Reuse #869's suite (renamed), plus the new start-time path:

- **Store:** `list_projects` (distinct/sorted/ACL/counts), `delete_label`,
  `list_conversations(project=...)` for specific / `""` / `None`.
- **Routes:** `GET /v1/sessions/projects`, `?project=` incl. unfiled, PATCH set/remove,
  OpenAPI drift regenerated. Permission level for set/remove = edit.
- **Hooks:** `useProjects` (GET + error), `useMoveToProject` (PATCH body + dual
  invalidation).
- **Sidebar:** grouping vs Recent, default-collapsed + count, pinned-in-project
  ordering, no-global-Pinned-for-filed-pins, collapsed-project aggregate marker,
  list-driven (stale label stays in Recent), precedence with archived.
- **NewChatDialog (new):** project picker — select existing, create new, none; project
  set on the created session.
- **E2E (`tests/e2e_ui/sessions/`):** kebab move into a new project + remove (from
  #869), plus create-with-project at session start.

## 11. Rollout

Single PR on top of #869's branch (build-on, not reimplement), with the rename +
counts + start-time picker + label-invisibility audit folded in. No flag needed (purely
additive UI); behind nothing since there's no migration and the sidebar degrades to
today's behavior when no projects exist.

## 12. Open questions

1. Case-sensitive project names (§9) — confirm.
2. Max name length — propose 100.
3. Expand-time fetch (8.2a) vs backfill (8.2b) — propose 8.2a.
4. Should `POST /v1/sessions` thread `labels` natively, or is create-then-PATCH
   acceptable for v1? (Affects atomicity of start-time assignment.)
