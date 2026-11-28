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
- `legacy-automation`: when the server reports `shareable: false` for an
  automation, such as one created before its store could keep grants, Share stays
  visible but disabled in the same places, with the tooltip "This is a legacy
  automation. Clone it, then share the copy." The server refuses a grant with the
  same message. Against a server without `scheduled_task_clone` the tooltip reads
  "This is a legacy automation. Recreate it to share it." Sharing off's reason wins when both apply; a server that sends no
  `shareable` leaves Share enabled.
- `shared-list`: a Shared with me tab beside Your automations lists the
  automations shared with the viewer, directly or through a group, only on a
  multi-user server advertising `scheduled_task_shared_with_me`; older servers
  advertise `scheduled_task_sharing` without it. Cards look like the viewer's
  own, with the same search and Active/Paused filter, and the subline
  starts Shared by and the owner's name. It loads 50 at a time with Show more,
  moving on by itself past a page that came back empty. Empty: No automations
  are shared with you yet.
- `clone`: on a server advertising `scheduled_task_clone` (which needs
  `automations_v2`), Clone in an automation's row ⋯ and right-click menus makes a
  new automation named after it with (copy), with its settings and schedule (on
  the same days for an every-other-day rule) and none of its runs, and opens it.
  The automation's page has no Clone. The copy has its own ID, so it can be
  shared where the original can't. A continue-session automation's copy starts its own
  session on its first run. The copy is paused when the original is (toast: The
  copy is paused like the original.). A copy of a finite schedule with no runs
  left says so (Its schedule has no runs left.); otherwise the toast says to pause
  or delete the original so they don't both run. A refused clone shows the
  server's reason in a toast and stays put.
- `palette-automation-list`: with `quick_pick_palette` on (off by default), the
  Automations list offers its actions in ⌘K on Your automations: New automation…,
  an Automations filter setting (All, Active, Paused) while the filter tabs show,
  and for each of your automations Run “name” now (not for trigger automations),
  Edit “name”…, Share “name”…, Clone “name” (where the server clones) and Pause or
  Resume “name”. Each runs the row menu's own handler, greyed by the same rules as
  the automation's header: while a change to it is saving, every row but Share is
  greyed, and Share stays available, as the header's button does. Delete stays on
  the row menu, which deletes without asking, and automations shared with you add
  no rows. With the flag off the page mounts none of it
  (`web/src/components/scheduled/automationListPaletteCommands.test.tsx`,
  `web/src/pages/TasksPage.test.tsx`,
  `tests/e2e_ui/scheduled/test_quick_pick_automation_commands.py`).
- `read-only-view`: on those cards' ⋯ and right-click menus and on the
  automation's page, Run now, Edit, Share, Clone (menus only), Pause/Resume and Delete are visible
  but disabled, each with a tooltip such as Only the automation's owner can edit
  it. The page shows Shared by and the owner under the title. Its run history
  lists every run's time, outcome and error code, and a run opens its session
  (if it's shared with the viewer) for the details: no session cost, error
  details, approvals, unread dots, tasks or pull requests. The owner's host
  reads The owner's host, the Project field is hidden, and Starts from names no
  session.
- `sidebar-folder`: in the sidebar's All sessions and My sessions views (and
  Archived), another user's run session shared with the viewer, or a session
  launched from a run's session tree, sits in the viewer's Omnigent Automation
  folder instead of Shared with me, wherever its owner filed it. Its row stays
  on one line with the people icon owners see on a shared session; hovering it
  reads Shared with you. A fork of a run's session stays under Shared with
  me. With no project of that name, a folder with the same name shows where a
  new project would go (by name, or last under a manual order) without creating
  one: it has no project menu or New session button, takes no drops and can't be
  dragged. The viewer's first own automation creates the project, which takes
  its place. My sessions lists no other shared session, and a pinned run stays
  in Pinned. The Shared with me filter lists every shared session flat. Grouped
  views have no folders, so grouped My sessions leaves the runs out. Shared runs
  can't be moved to a project.
- `palette-automation-actions`: with `quick_pick_palette` on (off by default),
  an automation's page lists its header actions in ⌘K: Run automation now
  (absent for trigger automations), Edit automation…, Share automation…, and
  Pause or Resume automation. Each runs the header button's handler. Run now,
  Edit, Pause and Resume are greyed while a change is saving; Share stays
  available then, as its button does. A viewer of a shared automation sees them
  greyed with the owner-only reason, as the header's buttons are; the owner sees Share
  only where sharing applies. Delete stays on its button, which deletes without
  asking (`web/src/components/scheduled/AutomationPaletteCommands.test.tsx`,
  `tests/e2e_ui/scheduled/test_quick_pick_automation_commands.py`).

## How to get to it (user POV)

- **Automation detail page:** open Automations, select an automation you own,
  then the Share button left of Edit in the header.
- **Automations list:** open an owned automation's ⋯ menu, or right-click its
  card, then choose Share. The dialog opens over the list.
- **⌘K:** with `quick_pick_palette` on, open an automation's page, press
  Cmd/Ctrl+K and type "share automation", then press Enter.
- **Clone:** open an owned automation's ⋯ menu on the Automations list, or
  right-click its card, then choose Clone. The copy's page opens.
- **Session share dialog:** open Share on the session a shared continue-session
  automation created on its first run, then follow the automation's name in the
  warning. The automation's page opens with its share dialog; closing it replaces
  the URL without the request, so reloading or going back does not reopen it.
  Without automation pages (`automations_v2` off) the name opens the automations
  list instead.
- **Shared with me:** as someone an automation is shared with, open Automations
  and the Shared with me tab, or follow `/tasks?view=shared`. Select a card to
  open its read-only page.
- **Sidebar folder:** as that person, choose All sessions or My sessions in the
  sidebar's filter menu and expand Omnigent Automation.
- **Mobile web / managed embedded:** the same page, row menu and dialog, under the
  host's mount path.
- **⌘K on the list:** with `quick_pick_palette` on, open Automations, press
  Cmd/Ctrl+K and type an action and the automation's name, such as "pause digest".

## Driving it with the repro environment

Preconditions: a multi-user server (not single-user) with `automations_v2` on,
which is the default, and a database permission store, so `/v1/info` reports
`scheduled_task_sharing: true` and `scheduled_task_shared_with_me: true`. Sign
in as two users with distinct IDs such as `alice@example.com` and
`bob@example.com`. Browser drives need a server origin other than loopback, as
the
[multi-user collaboration helper](../tests/e2e_ui/collaboration/_multi_user_server.py)
uses, because the session Share button is disabled on a local server.

- **Every sub-feature (component tests):**
  `web/src/components/PermissionsModal.test.tsx` (sharing an automation; a grants
  list that fails to load; a session an automation manages),
  `web/src/pages/TaskDetailPage.test.tsx` (sharing),
  `web/src/pages/TasksPage.test.tsx` (sharing from the row menu; cloning from the
  row menu),
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
  `test_sharing_is_advertised_and_served_together`,
  `test_an_automation_its_store_cannot_share_is_reported_and_refused` and
  `test_session_permissions_name_the_automation_that_manages_them` cover the
  routes, refusals, capability, the `shareable` field and the managed-session
  marker. The tests from
  `test_clone_copies_an_automations_settings_under_a_new_id` to
  `test_a_reminder_cannot_be_cloned` cover cloning. The tests from
  `test_recipients_read_an_automation_its_runs_and_their_steps` to
  `test_a_shared_run_session_lists_with_its_run_labels` cover recipients' reads,
  refused changes, agents, the capability, the shared list and the run labels;
  `tests/stores/test_permission_store.py` covers the shared list's paging, and
  `tests/stores/test_scheduled_task_store.py::test_a_schedule_anchor_keeps_count_from_the_anchor`
  the creation time a clone keeps. Run
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
  folder anyway. Choose My sessions: the run stays in that folder, and no other
  session alice shared lists. Fork that session as bob: the fork is bob's own
  and lists with his sessions. Remove bob from the automation: the automation
  leaves his list and its page shows not found.

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
  the All sessions list appear as it loads. My sessions fetches nothing for
  them: it places runs from the shared sessions already loaded for the inbox,
  and pages only the viewer's own. An embedding host that sets
  `inboxIncludesShared: false` loads none there, so My sessions shows none.
- Only a permission store that cannot keep some automation's grants reports
  `shareable: false`. The built-in stores never do, so `legacy-automation` appears
  only on deployments with such a store and is covered by the component tests.
