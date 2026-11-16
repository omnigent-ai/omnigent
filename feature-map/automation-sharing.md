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
- `load-error`: when the grants list fails to load, the automation and session
  dialogs say they couldn't load who has access, with the server's message,
  instead of No grants yet.
- `read-only`: Read is the only level. Rows and the add form show a fixed Read,
  with no level picker.
- `no-public`: there is no General access, Copy link or Open in mobile app.
- `refusals`: the server's message appears under the form when it refuses a
  grant (the owner, `__public__` or a reserved ID; a sharing-policy refusal) or a
  removal (a session revoke that failed, retried by removing again).
- `continue-session`: for a continue-session automation the dialog says sharing
  it replaces the session's shares with the list, including people added there by
  hand at Edit or Manage, and keeps the owner and General access.
- `managed-session-warning`: when a shared continue-session automation the viewer
  owns created a session on its first run, that session's share dialog shows a
  Sharing is managed by an automation warning naming it, loaded with the grants in
  the same request and on any server, with or without `scheduled_task_sharing`. It
  says shares added or removed there are replaced with the automation's list on
  each run and whenever its sharing changes, and that General access isn't
  affected. The name links to the automation's detail page with its share dialog
  open, or to the automations list without automation pages, and closes the
  session dialog. Editing the session's sharing stays allowed. Editing the
  automation, deleting it or changing its sharing refreshes the warning.
- `sharing-off`: with sharing off on the server, Share stays visible but disabled
  on the detail page and in the row menus, with the session Share's tooltip
  (Sharing has been disabled for this Omnigent server.), and a session's link
  does not open the automation's share dialog.
- `legacy-automation`: when the server reports `shareable: false` for an
  automation, such as one created before its store could keep grants, Share stays
  visible but disabled in the same places, with the tooltip "This is a legacy
  automation. Recreate it to share it." The server refuses a grant with the same
  message. Sharing off's reason wins when both apply; a server that sends no
  `shareable` leaves Share enabled.

## How to get to it (user POV)

- **Automation detail page:** open Automations, select an automation you own,
  then the Share button left of Edit in the header.
- **Automations list:** open an owned automation's ⋯ menu, or right-click its
  card, then choose Share. The dialog opens over the list.
- **Session share dialog:** open Share on the session a shared continue-session
  automation created on its first run, then follow the automation's name in the
  warning. The automation's page opens with its share dialog; closing it replaces
  the URL without the request, so reloading or going back does not reopen it.
  Without automation pages (`automations_v2` off) the name opens the automations
  list instead.
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

- **Every sub-feature (component tests):**
  `web/src/components/PermissionsModal.test.tsx` (sharing an automation; a grants
  list that fails to load; a session an automation manages),
  `web/src/pages/TaskDetailPage.test.tsx` (sharing),
  `web/src/pages/TasksPage.test.tsx` (sharing from the row menu),
  `web/src/components/scheduled/ScheduledTaskRow.test.tsx`,
  `web/src/hooks/usePermissions.test.tsx`, `web/src/hooks/useScheduledTasks.test.tsx`
  and `web/src/lib/permissionsApi.test.ts`. Run them with `pnpm exec vitest run`
  from `web/`.
- **Server contract:** in
  `tests/server/integration/test_scheduled_tasks_routes.py`, the tests
  `test_owner_shares_lists_and_revokes_an_automation`,
  `test_grant_refuses_what_an_automation_cannot_be_shared_with`,
  `test_sharing_is_advertised_and_served_together`,
  `test_an_automation_its_store_cannot_share_is_reported_and_refused` and
  `test_session_permissions_name_the_automation_that_manages_them` cover the
  routes, refusals, capability, the `shareable` field and the managed-session
  marker. Run them with plain `uv run --no-sync pytest`.
- **Detail page and row menu, manually:** as alice, open an automation you own
  and choose Share. Add bob: he appears with Read, and the dialog offers no level
  picker or General access. Add `__public__`: the refusal appears under the form.
  Remove bob. Repeat from the row's ⋯ menu and right-click menu. As bob, the
  automation is not listed, so Share is not offered. With sharing off
  (`OMNIGENT_SHARING_MODE=off`), Share is disabled with its tooltip in both places.
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
- The managed session's owner and General access are kept; every other user and
  group grant, at any level, is replaced with the automation's list.
- With `automations_v2` off the server refuses new grants and does not advertise
  `scheduled_task_sharing`, so Share is not offered, but existing grants still
  apply to runs and the managed-session warning still appears.
- Agents cannot list or change an automation's sharing, and cannot move a shared
  automation's runs to another session, host, workspace or execution target.
- Only a permission store that cannot keep some automation's grants reports
  `shareable: false`. The built-in stores never do, so `legacy-automation` appears
  only on deployments with such a store and is covered by the component tests.
