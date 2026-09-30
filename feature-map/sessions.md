# Sessions

A session is one conversation with an agent. Users manage sessions from the
sidebar list and from the session header menu: they pin, rename, archive,
unarchive, and delete them, fork or clone them into new sessions, and get them
running again when their runner or host goes away. Most actions are offered in
more than one place (the sidebar row, the right-click menu, bulk selection, and
the header menu), and each place is a separate entry point.

## Sub-features

- `pin`: pinned sessions move to their own section and back.
- `rename`: from the row, the header menu, or the header title; long titles are
  limited.
- `archive`: archived sessions leave the main list and appear in the archived
  view, which can be filtered by project and paged.
- `unarchive`: offered on archived rows, in bulk selection, and in the header
  menu of an archived session.
- `delete`: confirmed, then removed from the list and the server.
- `bulk-actions`: select several rows, then archive, unarchive, or delete them.
- `fork`: fork the whole session or from a message; the fork keeps images and
  their files, elapsed "worked for" time, and can switch agent or host.
- `clone`: copy a session into a new workspace, including a typed `~` path.
- `reconnect`: a stopped or stranded session shows a reconnect affordance and a
  dialog with the command to run; the desktop app can reconnect a local host
  itself. States: reconnecting (spinner), reconnect failed (retry), host offline.
- `resume-imported`: an imported session can be resumed onto a chosen local host.

## How to get to it (user POV)

**Sidebar row:** hover a row and open its menu, or right-click the row. Both
offer pin, rename, archive or unarchive, and delete.

**Bulk selection:** select several rows in the sidebar, then use the selection
actions (archive, unarchive, delete).

**Session header menu:** open a session and use the menu next to its title for
pin, fork, rename, archive or unarchive, and delete. Clicking the title also
renames. Sub-agent sessions hide owner-only actions.

**Message actions:** fork from a specific assistant message.

**Archived view:** switch the sidebar to archived sessions and filter by project.

**Reconnect:** in a session whose agent stopped, use the reconnect affordance
in the chat; the dialog shows the command for this situation (for example
`omnigent host` when the host is offline, or the harness's `--resume` command
when a local session is stranded). In the desktop app, reconnect acts directly.

**Mobile:** the header menu and the sidebar drawer offer the same actions; touch
devices fold some row controls into the menu.

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

- **`pin`:**
  `tests/e2e_ui/sessions/test_sidebar_pin_unpin.py::test_unpin_moves_session_back_to_recent`
- **`rename`:**
  `tests/e2e_ui/sessions/test_sidebar_rename.py::test_rename_session_enforces_user_title_limit`,
  `tests/e2e_ui/sessions/test_header_session_menu.py::test_header_session_menu_renames_owner_and_hides_for_subagent`
- **`archive`:**
  `tests/e2e_ui/sessions/test_sidebar_bulk_actions.py::test_bulk_archive_moves_session_to_archived`,
  `tests/e2e_ui/sessions/test_archived_project_filter.py::test_archived_project_filter_narrows_and_resets`,
  `tests/e2e_ui/sessions/test_archived_project_filter.py::test_archived_project_filter_load_more_pages_through`
- **`unarchive`, header menu:**
  `tests/e2e_ui/sessions/test_archived_session_header_menu.py::test_archived_session_header_menu_offers_unarchive`.
  The sidebar row and bulk unarchive have web unit coverage only: archive a
  session, open the archived view, choose Unarchive on the row, and expect the
  session back in the main list.
- **`delete`:**
  `tests/e2e_ui/sessions/test_sidebar_delete.py::test_delete_session_removes_row_and_from_store`,
  `tests/e2e_ui/sessions/test_sidebar_bulk_actions.py::test_bulk_delete_removes_sessions`
- **Right-click menu:**
  `tests/e2e_ui/sessions/test_sidebar_context_menu.py::test_right_click_opens_session_actions_menu`
- **`fork`:**
  `tests/e2e_ui/fork_session/test_fork_from_middle.py::test_fork_from_middle_truncates_history`,
  `tests/e2e_ui/fork_session/test_fork_preserves_image_attachment.py::test_fork_carries_image_reference_and_its_resource`,
  `tests/e2e_ui/fork_session/test_fork_retains_worked_for.py::test_fork_retains_worked_for_duration`,
  `tests/e2e_ui/fork_session/test_fork_switch_agent.py::test_fork_switch_agent_carries_history`
- **`clone`:**
  `tests/e2e_ui/sessions/test_clone_session.py::test_clone_session_copies_transcript_and_navigates`,
  `tests/e2e_ui/fork_session/test_typed_workspace_enables_clone.py::test_typed_tilde_workspace_enables_clone`
- **`reconnect`, spinner:**
  `tests/e2e_ui/chat/test_reconnecting_spinner.py::test_reconnecting_state_shows_spinner`
- **`reconnect`, stopped session (own environment):**
  `tests/e2e_ui/sessions/test_sidebar_stop.py::test_stopped_session_shows_reconnect_affordance`
- **`reconnect`, desktop app (own environment):**
  `tests/e2e_ui/sessions/test_reconnect_local_host_from_app.py::test_desktop_reconnect_performs_local_host_reconnect`,
  `tests/e2e_ui/sessions/test_reconnect_local_host_from_app.py::test_desktop_reconnect_failure_offers_retry`
- **`resume-imported` (own environment):**
  `tests/e2e_ui/sessions/test_imported_session_resume.py::test_imported_session_resumes_onto_chosen_local_host`

## Gotchas

- Archive and unarchive exist on the row, in bulk selection, and in the header
  menu. A fix to one of these does not reach the others; check each, and check
  that the undo toast restores the session.
- The header menu of an archived session must offer Unarchive, not Archive.
  Open an archived session directly to see it.
- Forking copies files and images into the new session. After a fork, open the
  forked session and confirm the image still loads; the transcript text alone
  does not prove the file came along.
- The reconnect command depends on why the session stopped (host offline vs. a
  stranded local session vs. a sandbox). Reproduce the reporter's reason, not
  just any stopped session.
- The desktop app's direct reconnect is not available in a browser; a browser
  shows the command instead.
- Tests marked "own environment" fail under `verify-env run` with an explicit
  message. That is expected; run them with plain `uv run pytest`.
