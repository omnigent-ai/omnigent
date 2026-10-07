# Sessions

A session is one conversation with an agent. Users manage sessions from the
sidebar list and from the session header menu: they pin, rename, archive,
unarchive, and delete them, fork or clone them into new sessions, and get them
running again when their runner or host goes away. Most actions are offered in
more than one place (the sidebar row, the right-click menu, bulk selection, and
the header menu), and each place is a separate entry point.

## Sub-features

- `pin`: pinned sessions move to their own section and back.
- `pin-reorder`: drag a pinned row onto another pinned row to change the Pinned
  order, which persists across reloads; dropping an unpinned row onto a pinned
  row pins it into that slot.
- `rename`: from the row, the header menu, or the header title; long titles are
  limited.
- `archive`: archived sessions leave the main list and appear in the archived
  view, which can be filtered by project and paged.
- `stop`: Stop session ends a host-launched parent and the sub-agents on its
  runner without reporting their expected disconnect as a task failure.
- `unarchive`: offered on archived rows, in bulk selection, and in the header
  menu of an archived session.
- `delete`: confirmed, then removed from the list and the server.
- `bulk-actions`: select several rows, then archive, unarchive, or delete them.
- `fork`: fork the whole session or from a message; the fork keeps images and
  their files, elapsed "worked for" time, and can switch agent or host.
- `fork-custom-agent`: switch to one of your custom agents (installed, imported,
  or discovered from an existing session), as well as to a built-in agent; the
  fork uses the chosen agent.
- `fork-access`: require read access to the source and, for a custom target,
  its owning session. The caller owns the fork; source grants are not copied.
- `clone`: copy a session into a new workspace, including a typed `~` path.
- `reconnect`: a stopped or stranded session shows a reconnect affordance and a
  dialog with the command to run; the desktop app can reconnect a local host
  itself or explicitly reconnect a remembered Arca host. States: reconnecting
  (spinner), reconnect failed (retry), host offline.
- `message-recovery`: a message racing initial runner binding or replacement
  reaches the available runner without a false failed turn. Native sessions
  initialize before delivery; SDK sub-agents reuse their loaded session state.
- `resume-imported`: an imported session can be resumed onto a chosen local host.
- `recent-switcher`: the desktop app opens the five most recent sessions with
  Control+Tab; Tab and Shift+Tab cycle, releasing Control switches, and Escape cancels.
- `browser-storage`: browser soft tabs, including one opened by the agent, share
  cookies within a session; different sessions stay isolated. Navigation stays
  per-tab.

## How to get to it (user POV)

**Sidebar row:** hover a row and open its menu, or right-click the row. Both
offer pin, rename, archive or unarchive, and delete.

**Pinned section drag:** drag a pinned row onto another pinned row to reorder
the Pinned section, or drop an unpinned row onto a pinned row to pin it into
that slot.

**Bulk selection:** select several rows in the sidebar, then use the selection
actions (archive, unarchive, delete).

**Session header menu:** open a session and use the menu next to its title for
pin, fork, rename, archive or unarchive, and delete. Clicking the title also
renames. Sub-agent sessions hide owner-only actions.

**Message actions:** fork through a specific assistant message, excluding later
turns. The header's Fork action copies the whole session instead. In either
dialog, keep the agent or choose another built-in or custom agent. Your custom
agents (installed with `omnigent agent add`, imported in the Create custom agent
dialog, or uploaded by running a session with their spec) stay available until
you remove them.

**Archived view:** switch the sidebar to archived sessions and filter by project.

**Reconnect:** in a session whose agent stopped, use the reconnect affordance
in the chat; the dialog shows the command for this situation (for example
`omnigent host` when the host is offline, or the harness's `--resume` command
when a local session is stranded). In the desktop app, reconnect acts directly
for this machine; an Arca-hosted session offers an explicit **Reconnect Arca**
action in the same dialog.

**Message recovery:** send the first prompt while a runner is starting, or send
another message after its runner restarts. Also open a sub-agent's conversation
and send a follow-up after its parent runner is replaced.

**Mobile:** the header menu and the sidebar drawer offer the same actions; touch
devices fold some row controls into the menu.

**Stop session:** open the native parent's sidebar menu and choose Stop session
while a sub-agent is working. This ends the runner; the current-turn interrupt
control is a separate action that leaves the session connected.

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

- **`pin`:**
  `tests/e2e_ui/sessions/test_sidebar_pin_unpin.py::test_unpin_moves_session_back_to_recent`
- **`pin-reorder`:**
  `tests/e2e_ui/sessions/test_sidebar_pin_unpin.py::test_drag_reorders_pinned_sessions`.
  Dropping an unpinned row onto a pinned row has web unit coverage only.
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
- **`fork`, message cutoff:**
  `tests/e2e_ui/fork_session/test_fork_from_middle.py::test_fork_from_middle_truncates_history`,
  plus agent switching:
  `tests/e2e_ui/fork_session/test_fork_switch_agent.py::test_fork_switch_agent_carries_history`
- **`fork`, whole session and resources:**
  `tests/e2e_ui/fork_session/test_fork_preserves_image_attachment.py::test_fork_carries_image_reference_and_its_resource`.
  This uses the header menu and checks that the fork's image URL loads.
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
- **`reconnect`, spinner:**
  `tests/e2e_ui/chat/test_reconnecting_spinner.py::test_reconnecting_state_shows_spinner`
- **`reconnect`, offline-host cause (own environment):**
  `tests/e2e_ui/sessions/test_session_host_offline.py::test_offline_native_host_preserves_failed_turn_and_retry`
  creates Claude/Codex native-wrapper sessions on a real disposable host, stops
  the host and its runners, and checks the API failure and browser details.
  It covers missing and stale runner bindings, reload persistence, and retry
  without duplicate transcript input. Repeat with `--device='iPhone 13'` for
  the mobile entry point; keep browser evidence outside the checkout.
- **`reconnect`, stopped session (own environment):**
  `tests/e2e_ui/sessions/test_sidebar_stop.py::test_stopped_session_shows_reconnect_affordance`
- **`reconnect`, idle replica handoff (own environment):**
  `tests/e2e_ui/sessions/test_idle_runner_handoff.py::test_idle_session_stays_healthy_after_unreadable_replica_handoff`
  moves a real runner between two servers sharing storage, then makes the old
  server's session and liveness reads unavailable through its production grace
  period. A completed legacy transcript without saved lifecycle state must
  remain readable without a disconnect error, including when the browser
  returns to the old server after its reads recover.
- **`stop`, `archive`, active sub-agents (own environment):**
  `tests/e2e/test_parent_stop_subagents_e2e.py::test_native_parent_teardown_preserves_child_outcome`
  drives real Claude and Codex parents, native children, a host daemon, and its
  dedicated runner through the public Stop/Archive APIs. Only model replies are
  scripted. It waits through the production disconnect grace and includes a
  real runner crash that must still report a failure. Requires both native
  CLIs and tmux; Claude's machine-managed credentials require an isolated
  container for the local model endpoint.
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
- **`message-recovery`, SDK sub-agent (server integration, plain `uv run pytest`):**
  `tests/server/integration/test_sessions_child_sessions.py::test_sdk_subagent_recovery_skips_session_init`
  covers recovery during ancestor healing and during the final binding refresh,
  without initializing the SDK child's already-loaded session again.
- **`resume-imported` (own environment):**
  `tests/e2e_ui/sessions/test_imported_session_resume.py::test_imported_session_resumes_onto_chosen_local_host`
- **`recent-switcher` (manual Electron):** open at least six sessions, hold
  Control and press Tab to show the five most recent, cycle with Tab and
  Shift+Tab, release Control to switch, then reopen and press Escape to cancel.
- **`browser-storage` (real Electron, own environment):**
  `web/electron/e2e/desktop_cookie_isolation.e2e.js`. Sign into a site in one
  tab, open it in another tab and the agent browser, and confirm both are signed
  in. Another session should be signed out. Log out and refresh the same-session
  tabs; all should be signed out.

## Gotchas

- Browser storage sharing is limited to one desktop window and app run;
  restarting the app clears it. Closing an individual tab does not.
- Archive and unarchive exist on the row, in bulk selection, and in the header
  menu. A fix to one of these does not reach the others; check each, and check
  that the undo toast restores the session.
- The header menu of an archived session must offer Unarchive, not Archive.
  Open an archived session directly to see it.
- Forking copies files and images into the new session. After a fork, open the
  forked session and confirm the image still loads; the transcript text alone
  does not prove the file came along.
- A custom agent outlives its sessions: forks of your own sessions share it,
  and forking someone else's session gives you your own copy. Appearing in the
  picker does not prove the fork API accepts it; check the bound agent after
  navigation.
- A copied web transcript does not prove the native CLI received that history.
  Native variants of the agent-switch test skip without `LLM_API_KEY`; record
  those skips and use a configured test harness before claiming native coverage.
- The reconnect command depends on why the session stopped (host offline vs. a
  stranded local session vs. a sandbox). Reproduce the reporter's reason, not
  just any stopped session.
- The desktop app's direct reconnect is not available in a browser; a browser
  shows the command instead.
- Tests marked "own environment" fail under `verify-env run` with an explicit
  message. That is expected; run them with plain `uv run pytest`.
