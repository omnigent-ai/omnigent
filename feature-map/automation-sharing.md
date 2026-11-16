# Automation sharing

An automation's owner can share it with users and groups at Read access. Each
run's session is then shared with them, and a continue-session automation keeps
its one session's sharing in step with the automation's list. The session's own
share dialog warns its owner when an automation manages that session's sharing.
The people it is shared with see it read-only on the Automations page, and its
runs' sessions in their own Omnigent Automation folder.

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
- `shared-list`: a Shared with me tab beside Your automations lists the
  automations shared with the viewer, directly or through a group, only on a
  multi-user server advertising `scheduled_task_sharing`. Cards look like the
  viewer's own, with the same search and Active/Paused filter, and the subline
  starts Shared by and the owner's name. It loads 50 at a time with Show more,
  moving on by itself past a page that came back empty. Empty: No automations
  are shared with you yet.
- `read-only-view`: on those cards' ⋯ and right-click menus and on the
  automation's page, Run now, Edit, Share, Pause/Resume and Delete are visible
  but disabled, each with a tooltip such as Only the automation's owner can edit
  it. The page shows Shared by and the owner under the title. Its run history
  lists every run's time, outcome and error code, and a run opens its session
  (if it's shared with the viewer) for the details: no session cost, error
  details, approvals, unread dots or tasks. The owner's host reads The owner's
  host, the Project field is hidden, and Starts from names no session.
- `sidebar-folder`: in the sidebar's All sessions view (and Archived), another
  user's run session shared with the viewer, or a session launched from a run's
  session tree, sits in the viewer's Omnigent Automation folder instead of
  Shared with me, wherever its owner filed it, with the owner on the row's
  second line. A fork of a run's session stays under Shared with me. With no
  project of that name, a folder with the same name shows where a new project
  would go (by name, or last under a manual order) without creating one: it has
  no project menu or New session button, takes no drops and can't be dragged.
  The viewer's first own automation creates the project, which takes its place.
  My sessions shows only the viewer's own sessions; the Shared with me filter
  lists every shared session. Shared runs can't be moved to a project.

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
- **Shared with me:** as someone an automation is shared with, open Automations
  and the Shared with me tab, or follow `/tasks?view=shared`. Select a card to
  open its read-only page.
- **Sidebar folder:** as that person, choose All sessions in the sidebar's
  filter menu and expand Omnigent Automation.
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
  and `web/src/lib/permissionsApi.test.ts`. The recipient's side:
  `web/src/pages/TasksPage.test.tsx` (automations shared with the viewer),
  `web/src/pages/TaskDetailPage.test.tsx` (an automation shared with the viewer),
  `web/src/components/scheduled/ScheduledTaskRow.test.tsx`,
  `web/src/lib/scheduledTasksApi.test.ts`, `web/src/shell/sidebarNav.test.ts`
  (automation runs shared with the viewer) and `web/src/shell/Sidebar.test.tsx`
  (the Omnigent Automation folder tests). Run them with `pnpm exec vitest run`
  from `web/`.
- **Server contract:** in
  `tests/server/integration/test_scheduled_tasks_routes.py`, the tests
  `test_owner_shares_lists_and_revokes_an_automation`,
  `test_grant_refuses_what_an_automation_cannot_be_shared_with`,
  `test_sharing_is_advertised_and_served_together` and
  `test_session_permissions_name_the_automation_that_manages_them` cover the
  routes, refusals, capability and the managed-session marker. The tests from
  `test_recipients_read_an_automation_its_runs_and_their_steps` to
  `test_a_shared_run_session_lists_with_its_run_labels` cover recipients' reads,
  refused changes, agents, the capability, the shared list and the run labels;
  `tests/stores/test_permission_store.py` covers the shared list's paging. Run
  them with plain `uv run --no-sync pytest`.
- **Detail page and row menu, manually:** as alice, open an automation you own
  and choose Share. Add bob: he appears with Read, and the dialog offers no level
  picker or General access. Add `__public__`: the refusal appears under the form.
  Remove bob. Repeat from the row's ⋯ menu and right-click menu. With sharing off
  (`OMNIGENT_SHARING_MODE=off`), Share is disabled with its tooltip in both places.
- **Managed-session warning, manually:** as alice, create a continue-session
  automation, run it once and share it with bob. Open that run's session and
  choose Share: the warning names the automation. Follow its name; the
  automation's share dialog opens. Close it and reload: it stays closed. Share a
  new-session automation and check a run's session shows no warning.

- **Recipient, manually:** share an automation with bob and run it once. As
  bob, open Automations → Shared with me: the card says Shared by alice, and
  its menu's actions are disabled with their tooltips. Open it: the page is
  read-only with the run listed, without the session cost alice's page shows.
  In the sidebar, choose All sessions: the run's session is in Omnigent
  Automation with alice on its row, and bob, who has no such project, sees the
  folder anyway. Fork that session as bob: the fork is bob's own and lists with
  his sessions. Remove bob from the automation: the automation leaves his list
  and its page shows not found.

## Gotchas

- Only the owner changes an automation or sees its Share dialog, grants and the
  managed-session warning; people it is shared with only read it and its runs.
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
  They never read an automation through a recipient either, and get 403 from
  the shared list.
- A recipient sees runs whose sessions were not shared with them, such as runs
  from before they were added; opening such a session is refused. That is why
  no run in their history carries its session's data, even when they can open
  that session: the server checks no session's permissions to list runs.
- Groups come from the permission store's `caller_principals`; the built-in
  store has no groups, so a grant to a group reaches nobody there.
- The sidebar only places shared runs it has loaded: the Omnigent Automation
  folder's own page fetch is the viewer's sessions, so shared runs further down
  the All sessions list appear as it loads.
