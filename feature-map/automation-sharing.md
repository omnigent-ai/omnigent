# Automation sharing

An automation's owner can share it with users and groups at Read access. Each
run's session is then shared with them, and a continue-session automation keeps
its one session's sharing in step with the automation's list. The session's own
share dialog warns its owner when an automation manages that session's sharing.

## Sub-features

- `share-action`: Share appears on an automation's detail page and in its row's
  ⋯ and right-click menus only when the server advertises
  `scheduled_task_sharing`, the server is not single-user, and the viewer owns
  the automation.
- `share-dialog`: Share opens the same dialog as session sharing, titled Share
  this automation. It lists grantees by display name, with the user or group ID
  beside a name that differs, and adds or removes one at a time. Loading and
  empty (No grants yet.) states match the session dialog.
- `read-only`: Read is the only level. Rows and the add form show a fixed Read,
  with no level picker.
- `no-public`: there is no General access, Copy link or Open in mobile app.
- `refusals`: the server's message appears under the form when it refuses a
  grant (the owner, `__public__` or a reserved ID; a sharing-policy refusal) or a
  removal (a session revoke that failed, retried by removing again).
- `continue-session`: for a continue-session automation the dialog says that
  session's sharing is kept in step with the list and that sharing changes made
  on the session are overwritten.
- `managed-session-warning`: when a shared continue-session automation the viewer
  owns created a session on its first run, that session's share dialog shows a
  Sharing is managed by an automation warning naming it. Changes made there are overwritten on its next
  run. The automation's name links to its detail page with its share dialog
  open, closing the session dialog. Editing the session's sharing stays allowed.
- `sharing-off`: with sharing off on the server, Share opens the Sharing
  unavailable notice, as for sessions.

## How to get to it (user POV)

- **Automation detail page:** open Automations, select an automation you own,
  then the Share button left of Edit in the header.
- **Automations list:** open an owned automation's ⋯ menu, or right-click its
  card, then choose Share. The dialog opens over the list.
- **Session share dialog:** open Share on the session a shared continue-session
  automation created on its first run, then follow the automation's name in the
  warning. The automation's page opens with its share dialog; closing it removes
  the request from the URL, so reloading does not reopen it.
- **Mobile web / managed embedded:** the same page, row menu and dialog, under the
  host's mount path.

## Driving it with the repro environment

Preconditions: a multi-user server (not single-user) with `automations_v2` on,
which is the default, and a database permission store, so `/v1/info` reports
`scheduled_task_sharing: true`. Sign in as two users with distinct IDs such as
`alice@example.com` and `bob@example.com`. Browser drives need a server origin
other than loopback, as the
[multi-user collaboration helper](../tests/e2e_ui/collaboration/_multi_user_server.py)
uses, because the session Share button is disabled on a local server.

- **`share-action`, `share-dialog`, `read-only`, `no-public`, `refusals`,
  `continue-session`, `managed-session-warning` (component tests):**
  `web/src/components/PermissionsModal.test.tsx` (sharing an automation; a session
  an automation manages), `web/src/pages/TaskDetailPage.test.tsx` (sharing),
  `web/src/pages/TasksPage.test.tsx` (sharing from the row menu),
  `web/src/components/scheduled/ScheduledTaskRow.test.tsx` and
  `web/src/lib/permissionsApi.test.ts`. Run them with `pnpm exec vitest run`
  from `web/`.
- **Server contract:**
  `tests/server/integration/test_scheduled_tasks_routes.py::test_owner_shares_lists_and_revokes_an_automation`,
  `::test_grant_refuses_what_an_automation_cannot_be_shared_with`,
  `::test_sharing_is_advertised_and_served_together` and
  `::test_session_permissions_name_the_automation_that_manages_them` cover the
  routes, refusals, capability and the managed-session marker. Run them with
  plain `uv run --no-sync pytest`.
- **Detail page and row menu, manually:** as alice, open an automation you own
  and choose Share. Add bob: he appears with Read, and the dialog offers no level
  picker or General access. Add `__public__`: the refusal appears under the form.
  Remove bob. Repeat from the row's ⋯ menu and right-click menu. As bob, the
  automation is not listed, so Share is not offered.
- **Managed-session warning, manually:** as alice, create a continue-session
  automation, run it once and share it with bob. Open that run's session and
  choose Share: the warning names the automation. Follow its name; the
  automation's share dialog opens. Close it and reload: it stays closed. Share a
  new-session automation and check a run's session shows no warning.

## Gotchas

- Only the owner sees an automation, its Share action, its grants and the
  managed-session warning; grantees see only the sessions shared with them.
- A new-session automation shares each run's new session. Removing someone
  leaves the sessions of earlier runs shared with them.
- A continue-session automation keeps its session's sharing in step only while
  it is shared with anyone. That includes a session picked for it with
  `session_id`, but only a session its first run created shows the warning: the
  server does not name the automation for a picked session.
- The managed session's General access is not overwritten; only its user and
  group grants follow the automation.
- Agents cannot list or change an automation's sharing, and cannot move a shared
  automation's runs to another session, host, workspace or execution target.
