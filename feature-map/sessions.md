# Sessions

A session is one conversation with an agent. Users manage sessions from the
sidebar list and from the session header menu: they pin, rename, archive,
unarchive, and delete them, fork or clone them into new sessions, and get them
running again when their runner or host goes away. Most actions are offered in
more than one place (the sidebar row, the right-click menu, bulk selection, and
the header menu), and each place is a separate entry point.

## Sub-features

- `session-grid`: behind `grid_sessions_display_ui`, drag sidebar sessions into a
  conversation to split left/right or top/bottom with a hover preview. Up to four
  independent chats fit in two rows and two columns. Grids live at `/grid/<id>`
  and are saved only in this browser, scoped to server and user.
- `pin`: pinned sessions move to their own section and back.
- `pin-undo`: unpinning shows an Undo toast that re-pins the session into its
  old Pinned slot. Dragging a pinned row into a folder unpins it without one.
- `pin-reorder`: drag a pinned row onto another pinned row to change the Pinned
  order, which persists across reloads; dropping an unpinned row onto a pinned
  row pins it into that slot.
- `rename`: from the row, the header menu, the header title, or Cmd/Ctrl+Alt+R
  on desktop; long titles are limited.
- `archive`: archived sessions leave the main list and appear in the archived
  view, which can be filtered by project and paged. On a server with worktree
  cleanup, archiving a session that has a git worktree first asks whether to
  also delete the worktree (the branch is kept); Remember my choice saves the
  answer, which Settings → Git can change later.
- `stop`: Stop session on a hosted parent ends its runner, including side chats
  and sub-agents sharing that runner. Conversation histories are kept.
- `side-chat-lifecycle`: generic side chats reuse their parent's live runner;
  closing one stops only that chat. Starting one from a stopped generic hosted
  parent relaunches the parent first, then shares its replacement runner. An
  existing side chat whose shared runner exited does the same on its next
  message or Resume.
- `side-chat-quotes`: Ask in side chat adds selected parent text to the visible,
  writable side chat; Reply on side-chat text quotes it in that chat. Both preserve
  interleaved comments and existing attachments; removing a quote keeps its comments.
  Live side chats accept pasted images in every reply text field.
- `unarchive`: offered on archived rows, in bulk selection, and in the header
  menu of an archived session.
- `delete`: confirmed, then removed from the list and the server.
- `export`: Export in the header menu saves the transcript as a local file.
- `unread`: Mark as unread flags a session to revisit and survives a reload,
  including for the session being viewed. Its dot appears once the turn is
  idle; approval and error indicators can take precedence. A row showing the
  unread dot offers Mark as read instead. Bulk selection also offers these actions.
- `project`: Add to project (Move session once filed) puts a session in a
  project folder from the row menu, the header menu, or the header's folder tag.
- `project-rename`: the project menu opens a dialog with the name selected;
  typing replaces it without losing characters as the menu closes.
- `bulk-actions`: select several rows, then mark as read or unread, archive,
  unarchive, or delete them.
- `fork`: fork the whole session or from a message; the fork keeps images and
  their files, elapsed "worked for" time, and can switch agent or host.
  For a native target, the fork dialog also offers model, reasoning effort,
  and permission mode pickers.
- `fork-custom-agent`: switch to one of your custom agents (installed, imported,
  or discovered from an existing session), as well as to a built-in agent; the
  fork uses the chosen agent.
- `fork-access`: require read access to the source and, for a custom target,
  its owning session. The caller owns the fork; source grants are not copied.
- `unsupported-sandbox-actions`: Databricks Sandbox and Arclet fork and
  host-switch controls stay visible but disabled, with hover and keyboard
  explanations. Their forms cannot submit.
- `clone`: copy a session into a new workspace, including a typed `~` path.
- `reconnect`: a stopped or stranded session shows a reconnect affordance and a
  dialog with the command to run; the desktop app can reconnect a local host
  itself or explicitly reconnect a remembered Arca host. States: reconnecting
  (spinner), reconnect failed (retry), host offline.
- `workspace-recovery`: a missing-workspace error offers **Continue in another
  directory…**. Choose an existing host directory, optionally with a new git
  worktree of it, to fork the conversation into a new session; the original
  remains available and missing files are not restored.
- `compaction-model-recovery`: a Codex turn that failed while compacting on the
  previous model shows the provider's error and why on a gray error card, with
  **Fork session…**. A spent-budget card is gray for every harness, since it
  isn't an Omnigent failure. Rate-limit and transient failures keep **Retry**
  instead and point to Fork in the session menu. A fork onto another model
  rebuilds the conversation from its history instead of cloning Codex's rollout,
  and fails rather than starting empty if that rebuild fails; the original is
  unchanged.
- `message-recovery`: a message racing initial runner binding or replacement
  reaches the available runner without a false failed turn. Native sessions
  initialize before delivery, including when an idle reconnect already restored
  the tunnel. Later messages reuse that generation's completed initialization;
  SDK sub-agents reuse their loaded session state.
- `import-progress`: import recent sessions or one session by ID from an online
  machine. Settings shows a spinner and count; the sidebar heading retains
  progress after leaving Settings. Results stay compact, with expandable failure
  details and retry of the original selection.
- `resume-imported`: an imported session can be resumed onto a chosen local host.
- `recent-switcher`: the desktop app opens the five most recent sessions with
  Control+Tab; Tab and Shift+Tab cycle, releasing Control switches, and Escape cancels.
- `browser-storage`: browser soft tabs, including one opened by the agent, share
  cookies within a session; different sessions stay isolated. Navigation stays
  per-tab. On macOS the browser toolbar can also import the current site's
  cookies from a local Chrome profile into that same session jar (Site scope
  picks the host or a parent domain); the copy is in-memory and never written
  back to Chrome.
- `canvas-workspace`: Canvas collapses the shared sidebar on entry; clicking a
  card opens its session beside the project board. Switching cards keeps
  drafts and board state; resize, focus, and close controls adjust the split.
- `canvas-mobile`: the board and selected session use the standard top-left
  hamburger to open navigation. Back to canvas retains the selected project
  across deep links and reloads.
- `canvas-navigation`: Canvas uses the same collapsed-sidebar button and hover
  preview as other pages, including the macOS titlebar controls. Opening it
  reveals the shared destinations, including Usage when enabled.
- `canvas-create`: the plus button on Main or a project opens the new-session
  composer; sending returns to that board with the new session selected.
- `canvas-session-health`: an opened Canvas session receives host health and
  live updates even before it appears in the loaded sidebar pages.
- `view-options`: the one filter menu, on the Projects header, with Grouping
  (Default / Status / Created / Updated), Display (All / My / Shared / Archived), Ordering
  (Updated / Created) and Show (Updated / Environment / Repo / Branch) submenus,
  saved per device. Created ordering keeps sessions newest-created first as messages
  arrive;
  requests, pagination, and project folders use that order too. Switching order
  clears the cached rows and shows loading while session timestamps refresh,
  including project folders, pins, and Archived. With the flag off,
  sessions always use Updated regardless of the saved preference. Shared sessions
  get their own section above Sessions. Status groups are Needs attention, Unread,
  Working, Draft, and Done. Working stays visible with muted “No sessions” text
  when empty. Starting or running sessions enter Working immediately; idle must
  last 10 seconds before they leave it. Errors and approvals bypass that delay.
  This only affects grouping, and existing hover/rename holds still apply.
  Created and Updated group by their respective date, independently of ordering,
  with the Created/Updated prefix only on the first date section, followed by
  headings such as “Yesterday” and “Earlier”. Environment metadata shows an icon
  with its label available to screen readers. Branch also resolves the workspace's
  checked-out branch when none was recorded at session creation, without hovering.
  All groupings replace
  Projects, Shared and Sessions with titled groups; the first group's header takes
  the Projects header's place. Select sessions sits beside the filter and selects every list below it at once. Behind
  the `sidebar_grouping_ui` release flag; with it off, the Sessions header keeps
  a flat Display filter and the tooltip keeps its plain layout.

## How to get to it (user POV)

**Session grids (desktop only):** enable `OMNIGENT_FEATURES=grid_sessions_display_ui`, open a
session, and drag another sidebar row over it. Hover **Split left/right/top/bottom**
to preview, then drop. Split a remaining full-width or full-height pane again
to reach three or four panes. Alternatively, use a row's menu or right-click
menu → **Open in grid** and choose the pane and direction. Each pane has its own
transcript and composer; terminal-capable sessions have a Chat/Terminal switch in
each cell header. The shortcut ⌘/Ctrl+Alt+\ switches
only the active cell. Choices survive reloads and follow the session into full
screen; drafts and terminal connections survive view changes. Shared-session
terminals are read-only for non-owners. One shared Workspace panel follows the active cell. Open the session full screen
for additional controls; close a pane to expand its neighbor. Closing down to
one pane returns to `/c/<id>`. Reopen saved grids in **Grids**, above Projects and Pinned, or use
their `/grid/<id>` URL on the same desktop browser. All sessions in the open grid
are highlighted in the sidebar. Mobile-width viewports, touch-primary devices, and
the iOS/Android apps hide grid menus and saved entries; grid URLs show a desktop-only
message without changing saved grids. Narrowing an open grid unmounts its cells
and disconnects terminal views; widening it restores the saved layout and reconnects.
The coarse-primary-pointer check also blocks tablets with attached keyboards;
touch-capable desktops whose primary pointer is fine remain eligible.
Double-click a grid title or use
its Rename menu action to edit it inline.
Enter or blur saves; Escape cancels. Grid names persist locally. Click anywhere
in a cell (or focus its controls) to activate it: its outline and inverted header
identify the session shown in Workspace. The grid header toggles the shared
panel; it spans the full app height beside the grid header and cells. Its width
is saved per grid and stays fixed when switching sessions; the header shrinks
with the cells when the panel opens.
Files, tabs, and unsaved edits remain session-specific. Maximizing Workspace
fills the grid area, including its header. Drag a cell titlebar onto another cell
to swap positions. With exactly two sessions side by side, drop a titlebar
near the whole grid's top/bottom edge to stack them. When stacked, use the
left/right edge to place them side by side, including along the dragged cell's
side. Only edges that change orientation appear; their preview spans the grid.
The center swaps positions and Escape cancels the preview.
Alternatively, focus the title and press Space, choose a cell with the arrow keys, and
press Space again. Escape cancels. The sidebar's Delete grid menu action forgets
only the grid, leaving sessions untouched. Unknown local grids show a recovery link.
Grids wait for account identity; if it is unavailable, use Reload to retry.
Failed sends restore text and supported attachments unless the cell has a newer draft.
Quoted replies recover as serialized text.

**Import:** open Settings → Import sessions, or choose **Import your recent
sessions** on the empty landing. Choose a machine, harness, and recent count
or session ID. After starting, go Back to the session list; its heading shows
progress and links back to Settings. Imported sessions appear in the normal list.
On phones, the progress link opens the Import panel directly; its header arrow
returns to the Settings menu.

**Sidebar row:** hover a row and open its menu, or right-click the row. Both
offer pin, fork, rename, mark as unread or read, add to project (or move
session), archive or unarchive, and delete.

**Project folder:** right-click its header or open its three-dot menu, then
choose Rename project. Start typing as soon as the dialog opens.

**Pinned section drag:** drag a pinned row onto another pinned row to reorder
the Pinned section, or drop an unpinned row onto a pinned row to pin it into
that slot.

**Bulk selection:** select several rows in the sidebar, then use the selection
actions (mark as read or unread, archive, unarchive, delete).

**Session header menu:** open a session and use the menu next to its title for
pin, fork, export, rename, mark as unread, add to project, archive or
unarchive, and delete. Clicking the title also renames, and the title's folder
tag also moves the session into a project. Sub-agent sessions hide owner-only
actions.

**Rename shortcut:** in an owner-managed top-level session on desktop, press
Cmd+Option+R on macOS or Ctrl+Alt+R elsewhere, including while writing a draft.
The header title becomes an input with its text selected. Enter or blur saves;
Escape cancels. The shortcut leaves terminals, code editors, and open menus or
dialogs alone. It is listed in Keyboard shortcuts and Settings.

**Message actions:** fork through a specific assistant message, excluding later
turns. The header's Fork action copies the whole session instead. In either
dialog, keep the agent or choose another built-in or custom agent; for a
native target, the dialog also shows model, reasoning effort, and permission
mode pickers. Your custom
agents (installed with `omnigent agent add`, imported in the Create custom agent
dialog, or uploaded by running a session with their spec) stay available until
you remove them.

**Archive worktree prompt:** archive a worktree session from the row menu,
bulk selection, or header menu; the "Also delete the worktree?" dialog offers
"No, archive only" and "Yes, delete worktree", plus Remember my choice.
Dismissing it cancels the archive. Settings → Git → Worktrees holds the saved
choice only when the server supports cleanup.

**Archived view:** choose Display → Archived sessions in the sidebar filter
menu, then filter by project.

**Sidebar filter menu:** with `sidebar_grouping_ui` on, the funnel on the
Projects header holds Grouping, Display, Ordering and Show. In
Status, Created or Updated grouping, Projects is hidden and the funnel sits in the same spot on the
first group's header. The Select sessions button sits beside it and selects
project, Shared and Sessions rows together; the Sessions header keeps only New
session. Hovering a row opens
its tooltip; the repo and PR rows load from the session's GitHub info only once
the tooltip opens. With the flag off, the funnel sits on the Sessions header and
offers Display only.

**Canvas:** choose Canvas in the sidebar, select Main or a project, and click
a session card once. Expand navigation manually to keep it open while switching
cards. Choosing Canvas in the sidebar returns to the selected board.
Drag the divider to resize, or focus it and use Left/Right arrows to
adjust the split and Home/End to reach its limits. Double-click the divider to
restore the default split. Focus the session to use the full area; Escape
restores the board. Close the session to return to the board. Opening, closing,
or maximizing Workspace keeps the canvas visible. Session focus is independent: Show canvas
restores the board without closing Workspace. Each card remembers whether Workspace is open.
On mobile, the top-left hamburger opens the sidebar from the board or a selected
session. Back to canvas returns to the selected project. A Canvas session link
opens the same layout directly. If a session is replaced, for example after `/clear`,
its replacement stays beside the same board.
The plus button starts a new session in the selected canvas. After sending,
the new session opens beside that board; on narrow screens, Back to canvas
returns to it. Reloading a temporary session before creation finishes returns to
the same board. Open sidebar appears on the board or, when the board is hidden,
in the conversation header. On desktop, hovering previews the sidebar and clicking
opens it. At md and wider, the macOS app uses its existing titlebar toggle, Search,
and Settings; in a narrower window it uses the conversation header's Open sidebar
button and the drawer's Search and Settings, all right of or below the window buttons.
On macOS desktop, the board and its controls stay below the window title bar,
including while focusing a conversation or using a narrow window.
Open a Canvas session outside the loaded sidebar pages to see its current host
status and live session updates, including when the app is embedded.

**Reconnect:** in a session whose agent stopped, use the reconnect affordance
in the chat; the dialog shows the command for this situation (for example
`omnigent host` when the host is offline, or the harness's `--resume` command
when a local session is stranded). In the desktop app, reconnect acts directly
for this machine; an Arca-hosted session offers an explicit **Reconnect Arca**
action in the same dialog.

**Message recovery:** send the first prompt while a runner is starting, or send
another message after its runner restarts. Also open a sub-agent's conversation
and send a follow-up after its parent runner is replaced.

**Missing workspace, desktop and mobile:** open a conversation whose working
directory is no longer available and choose **Continue in another directory…**
on the error card. The dialog explains the new session and missing files, then
lets you type or browse for an existing directory on a connected host. Optionally
name a branch under **Git worktree** to continue in a new worktree of that
repository instead. Choose **Continue** to open the new session with the copied
conversation. A missing destination stays in the dialog with an error and
creates no session.
The action also appears on an eligible error card inside a Canvas conversation
or side chat; it continues that conversation, including when another parent
conversation is open alongside it.

**Codex compaction on the previous model:** in a Codex session whose turn failed
while compacting on the model its last turn used, choose **Fork session…** on
the error card. The fork dialog opens for that session; pick the model to
continue on. A rate-limit or transient failure shows **Retry** instead; fork
from the session header menu. The action also appears on such a card inside a
Canvas conversation or side chat, and forks that displayed conversation rather
than the main session.

**Mobile:** the header menu and the sidebar drawer offer the same actions; touch
devices fold some row controls into the menu.

**Databricks Sandbox and Arclet sessions:** Fork is disabled in the header,
sidebar menus, and message actions. The composer's host menu shows a disabled
**Switch host…** item when you have write access. Read-only viewers retain the
existing host menu without switching controls. Hover or focus a disabled action
to read why it is unsupported.
The reconnect dialog also disables Clone and Switch host; directory selection
cannot enable either action.
While support is being checked, actions stay disabled. If that check fails,
the explanation asks you to reload. A missing source-host record keeps switching
disabled while ordinary shared sessions remain forkable. Direct dialogs show a loading status without
an action button until the check finishes; supported forms then receive keyboard focus.
If the reconnect dialog is already on Clone when an unsupported result arrives,
it selects Reconnect and explains the restriction.
An open switch dialog closes when switching becomes unavailable and stays closed
if support returns; choose Switch host again to reopen it.

**Stop session:** open the parent's sidebar row menu or right-click the row and
choose Stop session while a side chat or sub-agent is working. On mobile, open
the sidebar drawer and long-press the row. The current-turn interrupt control is
a separate action that leaves the session connected.

**Side-chat lifecycle:** use **Workspace → + → Side chat**, type `/side` in the
parent composer, or choose **Start a new side chat** from the composer's add
tray. Selecting assistant text also offers **Ask in side chat** unless the
composer is read-only. On mobile,
side chats open in a drawer; the header's **Conversation actions → Side
chats** reopens it. A generic hosted parent can start a new side chat
after stopping; this relaunches the parent and both use one runner. Sending to
an existing side chat or choosing Resume after the parent's runner exited (for
example, on its idle timeout) also relaunches the parent, then moves the side
chat to that runner. Close a side
chat with its tab's close button; the parent and sibling chats keep running. A
chat-only side chat can also send messages from its direct `/c/<child_id>` URL
without choosing a workspace. The side-chat composer opens the same `/` menu
affordance as the main composer but with a narrower set — `/help` and the
session's skills: `/help` is answered locally and a recognised skill runs
in-process through `slash_command`; a native terminal side chat shows no menu
and forwards slash text to the vendor TUI.

**Side-chat quotes:** select text in the parent conversation and choose **Ask in
side chat**. It appends a removable quote to the visible side-chat tab, including
an unsent tab, while keeping the draft. With no eligible tab visible, it opens a
new unsent tab. In the desktop side-chat pane or mobile drawer, select transcript
text and choose **Reply** to quote it in that chat's composer. Adding a quote
keeps existing comments before that quote. Edit between quotes or after the last
one, then send when ready. Paste an image into any reply text field after the
side chat has started. The first question is text-only; an image paste there
explains that the chat must be started first. Grid sessions use the same reply
composer and support interleaved comments and pasted images too. A viewer whose
composer is read-only (for example a Read grant) gets neither **Reply** nor
**Ask in side chat** when selecting parent text.

**Desktop browser:** choose **+ → Browser** in the Workspace panel or press
⌘/Ctrl+Alt+B. Agent browser requests and chat links with in-app opening enabled
create or select a closable Browser soft tab automatically.

**Desktop recent sessions:** hold Control and press Tab to open the five most
recent sessions. Continue pressing Tab (or Shift+Tab) to cycle, release Control
to switch, or press Escape to cancel.

## Driving it with the repro environment

Preconditions: a running instance (`verify-env start`, then `verify-env
doctor`), the built web UI, and at least one session (the `seeded_session`
fixture creates one). Run each test through the instance:

```sh
verify-env run -- python -m pytest <test> --ui-skip-build --video=on \
  --output="$VERIFY_EVIDENCE/sessions"
```

Tests that stop a runner, restart the server, or read the database cannot run
through `verify-env run`. They are marked "own environment" below; run them with
plain `uv run pytest`, which starts a private server for the test.

- **`session-grid` (browser contract):**
  `tests/browser_ui/chat/test_session_grid.py` drives sidebar drops, previews,
  the four-pane limit, independent sends, rejected-send recovery and live updates, reloads, pane closure,
  per-cell terminal input, switching without reattaching, active-cell shortcuts,
  keyboard menu creation, inline rename/cancel/persistence, active-cell selection,
  shared workspace contents and width persistence, grid-wide maximize, rejected
  drops without navigation, sidebar
  reopening/deletion, highlighting all grid members, titlebar rearrangement and cancellation,
  mobile and tablet entry-point gating (including resize and reload), identity recovery, and missing/disabled grid routes. With the flag absent or false,
  ordinary session routes retain native link dragging and expose no grid menus,
  drop targets, or saved-grid sidebar entries. Run with `--browser-ui-skip-build --video=on` after building.
  Layout combinations and malformed local storage have unit coverage in
  `web/src/grid/gridLayout.test.ts`. For a real-server drive, enable
  `grid_sessions_display_ui` on the isolated server. Also check a collapsed sidebar manually.

- **`pin`:**
  `tests/e2e_ui/sessions/test_sidebar_pin_unpin.py::test_unpin_moves_session_back_to_recent`
- **`pin-undo`, sidebar row:**
  `tests/e2e_ui/sessions/test_sidebar_pin_unpin.py::test_undo_unpin_restores_pinned_slot`.
  The row menu (mobile) and header menu Unpin have web unit coverage only:
  unpin a pinned session, click Undo on the toast, and expect it back in the
  same Pinned position.
- **`pin-reorder`:**
  `tests/e2e_ui/sessions/test_sidebar_pin_unpin.py::test_drag_reorders_pinned_sessions`.
  Dropping an unpinned row onto a pinned row has web unit coverage only.
- **`rename`:**
  `tests/e2e_ui/sessions/test_sidebar_rename.py::test_rename_session_enforces_user_title_limit`,
  `tests/e2e_ui/sessions/test_header_session_menu.py::test_header_session_menu_renames_owner_and_hides_for_subagent`.
  The desktop shortcut, including draft preservation, cancellation, and reload:
  `tests/e2e_ui/sessions/test_header_session_menu.py::test_rename_hotkey_from_composer_persists_and_cancels`.
  The iOS drawer rename under the soft keyboard:
  `tests/e2e_ui/mobile/test_rename_keeps_sidebar_sessions_reachable.py::test_rename_keeps_sidebar_sessions_reachable`,
  `tests/e2e_ui/mobile/test_rename_keeps_sidebar_sessions_reachable.py::test_rename_scrolls_edit_row_into_view_above_keyboard`
- **`archive`:**
  `tests/e2e_ui/sessions/test_sidebar_bulk_actions.py::test_bulk_archive_moves_session_to_archived`,
  `tests/e2e_ui/sessions/test_archived_project_filter.py::test_archived_project_filter_narrows_and_resets`,
  `tests/e2e_ui/sessions/test_archived_project_filter.py::test_archived_project_filter_load_more_pages_through`
- **`archive`, worktree prompt:**
  `tests/server/integration/test_sessions_archive.py::test_archive_stop_removes_worktree_when_requested`
  and
  `tests/server/integration/test_sessions_archive.py::test_delete_worktree_requires_archive`
  (plain `uv run pytest`) cover the opt-in teardown that keeps the branch and
  the rejected request without archive. The prompt, remembered choice, and the
  archive-only fallback on a server without cleanup have component coverage in
  `web/src/shell/Sidebar.archive.test.tsx` and
  `web/src/shell/HeaderConversationMenu.test.tsx`. Manually: archive a
  worktree session from the row menu, bulk selection, and header menu, and
  expect the dialog each time; a session without a worktree archives silently.
- **`unarchive`, header menu:**
  `tests/e2e_ui/sessions/test_archived_session_header_menu.py::test_archived_session_header_menu_offers_unarchive`.
  The sidebar row and bulk unarchive have web unit coverage only: archive a
  session, open the archived view, choose Unarchive on the row, and expect the
  session back in the main list.
- **`unarchive`, Undo toast:**
  `tests/e2e_ui/sessions/test_sidebar_lifecycle.py::test_sidebar_session_organization_round_trip`
  archives two sessions and restores both to Mine through Undo, including after
  a reload.
- **`delete`:**
  `tests/e2e_ui/sessions/test_sidebar_delete.py::test_delete_session_removes_row_and_from_store`,
  `tests/e2e_ui/sessions/test_sidebar_bulk_actions.py::test_bulk_delete_removes_sessions`
- **`export`, header menu (own environment):**
  `tests/e2e_ui/sessions/test_export_chat_menu.py::test_header_session_menu_offers_export`,
  `tests/e2e_ui/sessions/test_export_chat_menu.py::test_header_export_downloads_transcript`
  seed the transcript into the spawned server's database; run them with plain
  `uv run pytest`, outside `verify-env run`.
- **`unread`, sidebar row menu:**
  `tests/e2e_ui/sessions/test_sidebar_mark_unread.py::test_mark_unread_lights_the_dot_and_persists_across_reload`,
  `tests/e2e_ui/sessions/test_sidebar_mark_unread.py::test_unread_dot_survives_reload_from_localStorage_when_server_seed_is_empty`.
  The header-menu rename test above lists Mark as unread; manually choose it
  and expect the row's dot. The row dropdown's Mark as read action has coverage
  in `web/src/shell/Sidebar.readStateActions.test.tsx`. Right-click read-state
  actions need a manual check: mark an idle row unread, expect its dot, then
  choose Mark as read and expect it cleared.
- **`unread`, bulk selection:** coverage in
  `web/src/shell/Sidebar.readStateActions.test.tsx`, without a browser test. Select
  idle sessions with at least one unread row, choose Mark as read, and expect
  their dots to clear and selection mode to close. Select only read sessions,
  choose Mark as unread, and expect their dots to appear and selection to close.
- **`project`, sidebar row and header folder tag:**
  `tests/e2e_ui/sessions/test_move_to_project.py::test_move_session_into_project_regroups_sidebar_row`
  and, seeding its own environment,
  `tests/e2e_ui/sessions/test_header_move_to_project.py::test_header_folder_tag_moves_session_into_existing_project`.
  Repeat from the header menu's Add to project by hand and expect the row to
  regroup under that project.
- **Right-click menu:**
  `tests/e2e_ui/sessions/test_sidebar_context_menu.py::test_right_click_opens_session_actions_menu`
- **`project-rename`, project header right-click and three-dot menus:**
  `tests/e2e_ui/sessions/test_project_rename_dialog_focus.py::test_rename_dialog_preselects_name_so_typing_replaces`
  covers typing across the menu's exit animation from both menus.
- **`fork`, message cutoff:**
  `tests/e2e_ui/fork_session/test_fork_from_middle.py::test_fork_from_middle_truncates_history`,
  plus agent switching (own environment):
  `tests/e2e_ui/fork_session/test_fork_switch_agent.py::test_fork_switch_agent_carries_history`.
  Its SDK-to-SDK target is a built-in agent that only the test's own server
  loads; run it with plain `uv run pytest` so that target is available.
- **`workspace-recovery`, desktop and mobile conversation, Canvas, and side-chat cards:**
  `tests/browser_ui/chat/test_workspace_missing_recovery.py::test_continue_missing_workspace_in_another_directory`
  runs without a repro instance using `uv run pytest <test>
--browser-ui-skip-build --video=on --output=<evidence-directory>`.
  The browser contract covers the error action, visible explanation, blank
  destination, rejected missing directory, directory browser, preflight before
  fork and launch, and navigation to copied history while the original remains
  available. The new session renders the source's workspace failure as a
  historical notice without another recovery action, including after reload.
  All three surfaces run at desktop and phone widths; the side-chat pane and
  mobile drawer recover the child while leaving the neighboring parent intact.
  Host filesystem and fork/launch responses are mocked; this does not prove a
  native CLI received history or started in that directory.
- **`compaction-model-recovery`:**
  `tests/harnesses/codex_native/forwarder/test_turn_errors.py::test_error_during_compaction_reports_the_previous_model_it_ran_on`
  covers detection;
  `tests/server/integration/test_sessions_endpoints.py::test_native_failures_are_coded_live_and_after_reload`
  (plain `uv run pytest`) covers the card live and after reload, including
  retryable and expired-login failures that keep their own codes;
  `tests/runner/test_codex_model_pick_fallback.py::test_fork_onto_another_model_rebuilds_history_instead_of_cloning`
  covers the rebuilt fork, and
  `tests/runner/test_codex_model_pick_fallback.py::test_fork_onto_another_model_fails_when_its_history_cannot_be_rebuilt`
  its failed rebuild. The card's hint and **Fork session…** action have
  component coverage in `web/src/components/blocks/StatusBlocks.test.tsx`.
  No browser test drives the card into the fork dialog.
- **`fork`, whole session and resources:**
  `tests/e2e_ui/fork_session/test_fork_preserves_image_attachment.py::test_fork_carries_image_reference_and_its_resource`.
  This uses the header menu and checks that the fork's image URL loads.
  The dialog's permission-mode picker for native targets has component
  coverage in `web/src/shell/ForkSessionDialog.test.tsx`; drive the model and
  effort pickers by hand: fork a Codex session, pick another effort, and expect
  the new session's pill to show it.
  Duration coverage:
  `tests/e2e_ui/fork_session/test_fork_retains_worked_for.py::test_fork_retains_worked_for_duration`,
  and transcript-copy integration coverage (plain `uv run pytest`):
  `tests/server/integration/test_sessions_fork.py::test_fork_copies_transcript_content_with_fresh_ids`
- **`fork-custom-agent`, message action:**
  `tests/e2e_ui/fork_session/test_fork_session_scoped_custom_agent.py::test_fork_switches_onto_session_scoped_custom_agent`
  creates a custom-agent session, selects it as the target, and checks the
  copied transcript and bound agent. The header/custom-agent combination needs
  a manual drive: choose that target from the header's Fork dialog and check
  that the full transcript and chosen agent survive in the new session. Record
  the selection, navigation to a different session, transcript readback, and
  the target agent returned by the session API. Repeat from a message with a
  later turn present to distinguish message cutoff from the whole-session fork.
- **`fork-access` (server integration, plain `uv run pytest`):**
  `tests/server/integration/test_sessions_permissions.py::test_fork_session_requires_read_access`,
  `tests/server/integration/test_sessions_permissions.py::test_fork_switch_binds_session_scoped_target_with_access`,
  `tests/server/integration/test_sessions_permissions.py::test_fork_switch_denies_session_scoped_target_without_access`.
  These exercise authenticated routes and stores; the single-user browser
  recipe cannot prove cross-user access rules.
- **`clone`:**
  `tests/e2e_ui/sessions/test_clone_session.py::test_clone_session_copies_transcript_and_navigates`,
  `tests/e2e_ui/fork_session/test_typed_workspace_enables_clone.py::test_typed_tilde_workspace_enables_clone`
- **`unsupported-sandbox-actions`:**
  `tests/e2e_ui/fork_session/test_sandbox_disabled_controls.py::test_sandbox_fork_and_switch_host_disabled`
  covers the header, sidebar context menu, message action, and composer host menu
  at desktop and phone widths, plus the desktop sidebar dropdown. It checks
  hover, keyboard focus, and ignored activation.
  The server and transcript are real; Databricks Sandbox and Arclet metadata is
  patched at the browser boundary, and no live sandbox is provisioned. Direct form
  and reconnect guards have component coverage in `web/src/shell/ForkSessionDialog.test.tsx`,
  `web/src/shell/SwitchHostDialog.test.tsx`, and
  `web/src/shell/ReconnectSessionDialog.test.tsx`. Header fallbacks, including
  mobile, are covered by `web/src/shell/ChatHeader.test.tsx`.
  Failed lookups, recovery, hostless sessions, and shared sessions with unlisted
  hosts are covered by `web/src/hooks/useSessionActionRestrictions.test.tsx`.
  Host-menu loading/error explanations and the default status badge's disabled
  action have component coverage in `web/src/components/HostBadge.test.tsx`.
  Loading-to-enabled keyboard focus is covered there and in
  `web/src/components/DisabledActionTooltip.test.tsx`. The fork and switch-host
  dialog suites also cover loading-to-form focus; the tooltip suite checks that a
  cleared explanation does not reopen without interaction.
  Supported host-switch UI coverage:
  `tests/e2e_ui/sessions/test_host_badge.py::test_host_badge_switches_the_session_to_another_host`
  checks the release/launch requests with stubbed host APIs.
- **`reconnect`, spinner (own environment):**
  `tests/e2e_ui/chat/test_reconnecting_spinner.py::test_reconnecting_state_shows_spinner`
  seeds an error item directly into the spawned server's database.
- **`reconnect`, offline-host cause (own environment):**
  `tests/e2e_ui/sessions/test_session_host_offline.py::test_offline_native_host_preserves_failed_turn_and_retry`
  creates Claude/Codex native-wrapper sessions on a real disposable host, stops
  the host and its runners, and checks the API failure and browser details.
  It covers missing and stale runner bindings, reload persistence, and retry
  without duplicate transcript input. Repeat with `--device='iPhone 13'` for
  the mobile entry point; keep browser evidence outside the checkout.
- **`reconnect`, stopped session (own environment):**
  `tests/e2e_ui/sessions/test_sidebar_stop.py::test_stopped_session_shows_reconnect_affordance`
- **`reconnect`, completed turn after host handoff (own environment):**
  `tests/e2e/test_runner_exit_replica_handoff_e2e.py::test_host_report_after_handoff_preserves_only_completed_turns`
  moves a real host tunnel to a different server replica while its runner
  stays on the original replica, then kills that owned runner. Reloaded API
  snapshots keep completed work idle; interrupted work and runners that never
  connected still fail. The OpenAI Agents harness uses local scripted replies.
- **`reconnect`, idle replica handoff (own environment):**
  `tests/e2e_ui/sessions/test_idle_runner_handoff.py::test_idle_session_stays_healthy_after_unreadable_replica_handoff`
  moves a real runner between two servers sharing storage, then makes the old
  server's session and liveness reads unavailable through its production grace
  period. A completed legacy transcript without saved lifecycle state must
  remain readable without a disconnect error, including when the browser
  returns to the old server after its reads recover.
- **`reconnect`, completed Claude Task child (own environment):**
  `tests/e2e_ui/sessions/test_claude_native_idle_handoff.py::test_completed_claude_child_survives_stale_status_handoff`
  drives a real Claude-native parent and its Agent tool through child completion,
  then stages stale saved `running` state and moves the runner to a fresh server.
  After a real tunnel loss and the production disconnect grace, the child's
  result stays readable without a chat error or failed Agents-row status,
  including after reload. Only model replies and stale persistence are staged.
  Requires Claude Code and tmux; machine-managed Claude credentials need an
  isolated container for the local model endpoint.
- **`stop`, `archive`, active sub-agents (own environment):**
  `tests/e2e/test_parent_stop_subagents_e2e.py::test_native_parent_teardown_preserves_child_outcome`
  drives real Claude and Codex parents, native children, a host daemon, and its
  dedicated runner through the public Stop/Archive APIs. Only model replies are
  scripted. It waits through the production disconnect grace and includes a
  real runner crash that must still report a failure. Requires both native
  CLIs and tmux; Claude's machine-managed credentials require an isolated
  container for the local model endpoint.
- **`stop`, `side-chat-lifecycle`, hosted side chats (own environment):**
  `tests/e2e_ui/sessions/test_stop_side_chats.py::test_stop_session_stops_hosted_side_chats`
  drives both desktop sidebar menus and the mobile long-press menu with a real
  host. Side chats share their parent's runner. Closing one leaves its parent
  and sibling working; stopping the parent stops that shared runner and keeps
  all histories. An unrelated session stays online, and a new side chat
  relaunches its parent before both share the replacement runner.
  Only model replies are scripted.
- **`side-chat-lifecycle`, shared-runner recovery (server integration, plain `uv run pytest`):**
  `tests/server/integration/test_session_host_launch.py::test_side_chat_message_relaunches_exited_source_runner`,
  `tests/server/integration/test_session_host_launch.py::test_side_chat_retry_relaunches_exited_source_runner`,
  and
  `tests/server/integration/test_session_host_launch.py::test_side_chat_message_rebinds_to_already_relaunched_source_runner`
  use a mock host. After the parent's runner exits, a side-chat message or
  Resume relaunches the parent on its host and initializes the side chat on the
  replacement runner. A parent that already has a replacement is reused
  without another launch.
- **`side-chat-lifecycle`, shared-runner recovery (real host, plain `uv run pytest`):**
  `tests/e2e/test_host_e2e.py::test_host_side_chat_recovers_after_shared_runner_exits`
  kills the shared runner while the host stays connected. The side chat's next
  message relaunches the parent's runner and both continue on it; after a
  second kill, Resume on the side chat does the same. Only model replies are
  scripted.
- **`side-chat-lifecycle`, creation entry points:**
  `tests/e2e_ui/chat/test_side_chat_entrypoints.py` covers `/side`, the Workspace
  menu, the composer add tray, selected text's Ask in side chat action, and the
  mobile side-chat drawer. Its stale-branch scenarios simulate a stopped parent
  runner and verify parent recovery before binding; the hosted lifecycle test
  above proves live-parent reuse with actual runner processes.
- **`side-chat-quotes`, desktop Ask in side chat and Reply:**
  `tests/e2e_ui/chat/test_side_chat_entrypoints.py::test_ask_in_side_chat_opens_a_quoted_side_chat_tab`
  checks pending/live tab reuse, interleaved comments, pasted images, and replies
  sent only to the child. Hide Workspace and repeat Ask in side chat to verify
  a new tab opens instead of changing the hidden draft.
  `tests/e2e_ui/collaboration/test_sharing_journey.py::test_share_grant_downgrade_revoke_journey`
  checks that an Edit collaborator is offered Reply on selected text and a
  Read viewer is not.
- **`side-chat-quotes`, mobile drawer Reply:**
  `tests/e2e_ui/chat/test_side_chat_entrypoints.py::test_side_chat_opens_in_a_drawer_on_mobile`
  checks that Reply stays on screen and interleaved comments with a pasted image
  reach only the side chat. Component coverage in `web/src/components/chat/SideChatPane.test.tsx`
  checks side-chat and grid composers, quote removal, preserved text/files, read-only chats, and starting-tab
  eligibility; `web/src/pages/ChatPage.composer.test.tsx` checks target routing
  and parent isolation. In an embedded app, repeat text selection in both the
  parent and side chat and check the popup's theme and button styling.
- **`side-chat-lifecycle`, direct chat-only URL (browser contract):**
  `tests/browser_ui/chat/test_side_chat_resume.py::test_runnerless_side_chat_sends_from_its_direct_url`
  opens a runnerless child directly and sends a message without a directory
  picker. Session metadata and message dispatch are mocked.
- **`side-chat-lifecycle`, native Codex:**
  `tests/e2e_ui/chat/test_native_codex_side_chat.py::test_native_codex_side_chat_inherits_context_and_closes_independently`
  drives a real Codex CLI and app-server with scripted model replies. It checks
  inherited context in the model request, isolated follow-ups, and a parent
  that stays usable after closing its side chat.
- **`reconnect`, desktop app (own environment):**
  `tests/e2e_ui/sessions/test_reconnect_local_host_from_app.py::test_desktop_reconnect_performs_local_host_reconnect`,
  `tests/e2e_ui/sessions/test_reconnect_local_host_from_app.py::test_desktop_reconnect_failure_offers_retry`
- **`message-recovery`, native binding races (own environment):**
  `tests/e2e/test_native_runner_binding_races_e2e.py::test_native_send_rechecks_binding_after_runner_miss`
  drives real servers, runners, and Claude/Codex CLIs with a mock model. It
  covers first binding on a sibling replica and local runner replacement,
  including explicit retry on the owner and exactly-once prompt/reply checks.
- **`message-recovery`, native host crash (own environment, live model):**
  `tests/e2e/test_native_host_reconnect_e2e.py::test_native_message_survives_host_restart`
  uses a real server, host daemon, host-launched runners, and Codex CLI. After
  a successful tool-using turn it kills the daemon, sends one follow-up, and
  restarts the same host beyond the ten-second runner grace. The original
  input must complete its file write exactly once on a new runner, without
  resending or persisting a failed turn. Requires `codex`, `tmux`,
  `OMNIGENT_E2E_CODEX_NATIVE=1`, and live `--llm-api-key` credentials (optionally
  `--profile`). Logs and timing evidence remain in pytest's temporary directory.
- **`message-recovery`, host relaunch (server integration, plain `uv run pytest`):**
  `tests/server/integration/test_session_host_launch.py::test_message_relaunch_classifies_replacement_runner_liveness`
  distinguishes a replacement live on another replica from a failed launch
  with an old heartbeat or a local heartbeat. This controls liveness evidence;
  it does not drive an actual cross-replica host migration.
- **`message-recovery`, already-connected native runner (server integration):**
  `tests/server/integration/test_session_host_launch.py::test_relaunch_posts_session_init_before_forwarding_message`
  checks Claude and Codex first-input initialization, suppression of replaying
  persisted input, and reuse on the next message without repeating child recovery.
- **`message-recovery`, rotated Codex session (own environment):**
  `tests/e2e/test_codex_rotated_bridge_relaunch_e2e.py::test_rotated_codex_session_relaunches_into_its_bridge_directory`
  drives `/new` in real Codex, stops that session's host-launched runner, then
  sends a chat message to relaunch it. Chat and terminal replies must both
  reach the rotated conversation with retained or missing bridge state. Only
  model replies are scripted; no browser interaction is claimed.
- **`message-recovery`, SDK sub-agent (server integration, plain `uv run pytest`):**
  `tests/server/integration/test_sessions_child_sessions.py::test_sdk_subagent_recovery_skips_session_init`
  covers recovery during ancestor healing and during the final binding refresh,
  without initializing the SDK child's already-loaded session again.
- **`import-progress`:** run
  `tests/e2e_ui/sessions/test_import_sessions.py` for Settings recent imports,
  exact-ID imports, and the empty-landing entry point (mocked import responses).
  Run `tests/e2e_ui/sessions/test_import_progress_in_session_list.py` for a real
  isolated host reading transcripts, streaming to the server, leaving Settings,
  progress beside the sidebar heading, and clearing it on completion. Desktop
  and mobile cases follow the indicator back to the panel and verify that the
  content can receive clicks, including behind the mobile drawer boundary.
  `tests/e2e_ui/sessions/test_import_oversized_session.py` verifies recent and
  exact-ID imports through a real host when a transcript exceeds the tunnel's
  message cap; the batch summary links to all three sessions in the sidebar.
  Component coverage checks compact large batches, failure expansion, retry
  after navigation, and collapsed headings.

- **`resume-imported` (own environment):**
  `tests/e2e_ui/sessions/test_imported_session_resume.py::test_imported_session_resumes_onto_chosen_local_host`
- **`recent-switcher` (manual Electron):** open at least six sessions, hold
  Control and press Tab to show the five most recent, cycle with Tab and
  Shift+Tab, release Control to switch, then reopen and press Escape to cancel.
- **`browser-storage` (real Electron, own environment):**
  `web/electron/e2e/desktop_cookie_isolation.e2e.js`. Sign into a site in one
  tab, open it in another tab and the agent browser, and confirm both are signed
  in. Another session should be signed out. Log out and refresh the same-session
  tabs; all should be signed out. Chrome cookie import needs a real Keychain and
  Chrome profile, so it is manual: sign into a site in Chrome, open that site in
  the pane, click the cookie button, confirm the native prompt, and check the
  page is signed in while an unrelated site in the same session is not.
- **`canvas-workspace` (sidebar entry, project cards, chat, focus, and history):**
  `tests/e2e_ui/sessions/test_canvas_workspace.py::test_canvas_keeps_board_drafts_and_sidebar_while_switching_sessions`.
  Workspace open/close, independent focus, maximizing, and pane sizing on wide
  and smaller desktops:
  `tests/e2e_ui/sessions/test_canvas_workspace.py::test_canvas_stays_visible_when_workspace_opens`.
  Grouping, feature gating, drag persistence, and live updates also run in
  `tests/e2e_ui/sessions/test_canvas_page.py`, with selection churn in
  `tests/e2e_ui/sessions/test_canvas_selected_highlight_persists.py::test_clicked_card_keeps_selected_highlight_across_live_updates`.
  The selection journey covers duplicate titles and a long list whose nodes
  move outside the rendered viewport after a live update.
  Unit tests in `web/src/canvas/canvasNavigation.test.ts` distinguish Canvas
  from similarly named routes under standalone and embedded mount paths.
  `web/src/canvas/CanvasWorkspace.test.tsx` verifies that dismissing a nested
  dialog with Escape preserves conversation focus and that resizing respects
  both panes' minimum widths; double-click restores and saves the default split.
  It also checks that the sidebar menu stays usable while the board is loading.
  `web/src/pages/CanvasPage.test.tsx` checks viewport
  translation across successive resizes, hiding, and restoring the pane,
  refitting untouched boards after card changes, and preserving queued viewports
  during rapid project switches and resizes. `web/src/shell/AppShell.test.tsx`
  verifies Workspace's saved open/closed choice across card changes.
  `web/src/hooks/useConversationRedirect.test.tsx` delivers session replacement
  events and checks the same board, query parameters, and replaced history under
  standalone and embedded routes.
- **`canvas-session-health` (card selection and direct session links):** component
  tests in `web/src/hooks/useActiveConversationId.test.tsx` cover standalone and
  embedded session routes, switching cards, and returning to the board.
  `web/src/hooks/RunnerHealthProvider.test.tsx` and
  `web/src/hooks/SessionUpdatesProvider.test.tsx` verify that a Canvas session
  outside the loaded sidebar pages enters health polling and the live watch
  set; changing cards updates the watched session and encoded temporary ids
  are excluded.
- **`canvas-mobile` (deep link, back button, and reload):**
  `tests/e2e_ui/sessions/test_canvas_workspace.py::test_canvas_deep_link_and_mobile_return_keep_the_project`.
  At phone width and just below the desktop breakpoint, the journey compares the
  hamburger with an ordinary session, opens navigation from both Canvas views,
  and returns to the selected project.
- **`canvas-navigation` (collapsed/expanded, Usage enabled/disabled):**
  `tests/e2e_ui/sessions/test_canvas_creation.py::test_canvas_uses_standard_sidebar_navigation`.
  The journey compares the toggle's icon, position, and size with an ordinary
  session, previews and opens navigation, and checks that split and focused
  conversations retain one toggle. It compares destinations and returns through
  Canvas, then reloads to confirm the selected project remains remembered.
  macOS title-bar clearance, sidebar opening from the titlebar toggle or mobile
  hamburger, and conversation focus/return at wide and narrow widths run in
  `tests/e2e_ui/sessions/test_canvas_workspace.py::test_canvas_controls_clear_the_macos_titlebar`.
  This Chromium journey emulates the macOS preload bridge and user agent; it
  verifies the app's layout around the title bar, not native traffic-light rendering.
  `web/src/embed.test.tsx` checks that the compiled Canvas clearance selector
  matches the embedded app's Mac scope root. For an embedded desktop build,
  enable Canvas only in the isolated test host, enter through the sidebar and a
  Canvas deep link, then collapse/preview/reopen navigation and focus/restore a
  session. Check that the Canvas heading and restore control stay below the
  native title bar at 14px/16px host roots and 80%, 100%, 110%, and 125% page zoom.
  `web/src/shell/ChatHeader.test.tsx` checks the shared toggle's desktop/mobile
  icons, peek cancellation, and hover/keyboard tooltip behavior.
- **`canvas-create` (Main/project plus button, desktop/mobile):**
  `tests/e2e_ui/sessions/test_canvas_creation.py::test_canvas_plus_creates_session_on_the_selected_board`.
  The composer uses a fixture host; session creation, binding to the isolated
  runner, and the mock-backed first reply use the real server. Temporary-id
  replacement, server-first creation, feature gating, and failed-create draft
  recovery also run in `web/src/shell/NewChatDialog.flow.test.tsx`.
  Stale temporary routes return to their board in
  `tests/e2e_ui/sessions/test_canvas_creation.py::test_stale_canvas_creation_url_returns_to_its_board`;
  ordinary sessions still return to the new-session page in
  `tests/e2e_ui/sessions/test_canvas_creation.py::test_stale_regular_creation_url_returns_to_new_session`.
- **`view-options`:**
  `tests/e2e_ui/sessions/test_sidebar_view_options.py::test_grouping_and_show_toggles_persist_across_reload`,
  `tests/e2e_ui/sessions/test_sidebar_view_options.py::test_tooltip_fetches_repo_and_pr_only_when_opened`,
  `tests/e2e_ui/sessions/test_sidebar_view_options.py::test_created_ordering_persists_and_flag_off_uses_updated`
  (enable the flag by patching `GET /v1/info`). Unit tests in
  `web/src/shell/Sidebar.test.tsx` ("sidebar_grouping_ui release flag") check
  that the flag-off sidebar ignores stored view options.

## Gotchas

- Existing side chats on separate runners must still be closed individually.
  Hostless CLI Stop keeps its existing per-conversation behavior.
- Starting a side chat after its parent stopped relaunches the parent. The new
  chat shares that replacement runner and stops with the parent again.
- A side chat has no host of its own. When its shared runner is gone, recovery
  goes through the parent. It requires edit access to the parent and, as for
  attaching a runner, ownership of the parent's runner. Another editor gets an
  error saying the parent runs on another user's runner, and their side chat
  stays unbound.
- Ask in side chat reuses only a visible, writable tab belonging to the current
  parent. Hidden, ended, and currently starting tabs are ineligible; another
  selection opens a new unsent tab. Ended chats do not offer Reply, and a
  read-only parent composer offers neither Reply nor Ask in side chat.
- A phone-width browser run does not prove the mobile side-chat drawer in the
  embedded web app or the native apps. Both style the drawer only inside the
  app's own page area: placed outside it, the drawer stays off screen when
  embedded and sits under the status bar in the native apps.
- Canvas is feature-gated, including conversation deep links. Its browser
  tests enable that flag explicitly; they do not enable it on a deployed app.
- Canvas entry collapses navigation once. Selecting another card must not
  collapse navigation that the user has manually expanded. Card dragging must
  not open a session. Workspace must not hide the canvas or change session
  focus; both panels resize to leave room for the conversation controls.

- Browser storage sharing is limited to one desktop window and app run;
  restarting the app clears it. Closing an individual tab does not.
- Archive and unarchive exist on the row, in bulk selection, and in the header
  menu. A fix to one of these does not reach the others; check each, and check
  that the undo toast restores the session.
- The header menu of an archived session must offer Unarchive, not Archive.
  Open an archived session directly to see it.
- The worktree prompt appears only when the server advertises worktree cleanup
  and the session has a worktree. A remembered answer skips it on every later
  archive; reset the choice in Settings → Git before re-testing.
- Forking copies files and images into the new session. After a fork, open the
  forked session and confirm the image still loads; the transcript text alone
  does not prove the file came along.
- Databricks Sandbox and Arclet restrictions apply to managed sources, including
  shared sessions. Other sandbox providers support forking. Verify the supported
  path with
  `tests/e2e_ui/fork_session/test_fork_managed_sandbox.py::test_fork_onto_managed_sandbox_with_no_host_online`.
- A custom agent outlives its sessions: forks of your own sessions share it,
  and forking someone else's session gives you your own copy. Appearing in the
  picker does not prove the fork API accepts it; check the bound agent after
  navigation.
- A copied web transcript does not prove the native CLI received that history.
  Native variants of the agent-switch test skip without `LLM_API_KEY`; record
  those skips and use a configured test harness before claiming native coverage.
- Continuing in another directory preserves conversation history, not files
  from the missing workspace. A new worktree starts from the `HEAD` of the
  chosen repository's main checkout, never from the missing worktree's branch.
  Existing worktree recreation and sandbox creation belong to the ordinary fork
  dialog, not this recovery action.
- The reconnect command depends on why the session stopped (host offline vs. a
  stranded local session vs. a sandbox). Reproduce the reporter's reason, not
  just any stopped session.
- The desktop app's direct reconnect is not available in a browser; a browser
  shows the command instead.
- Tests marked "own environment" fail under `verify-env run` with an explicit
  message. That is expected; run them with plain `uv run pytest`.
- Grouping and ordering apply to the loaded pages only: an older session joins
  its group once it is paged in. Grouped views hide Projects, so dragging a
  session onto the list never removes it from its project there.
