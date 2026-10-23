# Workspace content search

Users search the session workspace's file contents from the desktop Workspace
panel, then open a matching file at its line. Filename search remains in Files.

## Sub-features

- `search`: submit a literal query with Enter or Search, optionally matching case
  or using a ripgrep regular expression; results show paths, lines and previews.
- `filter`: click **Filter files** (the funnel button between the query and
  Search) to show or hide **Include** and **Exclude** globs. The button is
  highlighted while filters are set, even when the fields are hidden.
  Comma-separated patterns and brace alternatives work (for example `src/**,
  *.{ts,tsx}`); excludes take precedence. Clear both fields to search all files again.
- `navigate`: open a result at its line and return to the preserved search;
  switching conversations restores each conversation's query, options and results.
- `states`: initial, searching, no matches, errors and partial results.
- `gate`: the feature is absent unless `workspace_content_search` is enabled.

## How to get to it (user POV)

- Desktop web and desktop app: open a session's Workspace panel and select the
  **Content search** magnifying-glass tab.
- Workspace panel picker: choose **Select panel → Content search**.
- Keyboard: **Cmd+Shift+F** on macOS (**Ctrl+Shift+F** on Windows/Linux) opens
  the Workspace panel, selects **Content search**, and focuses its input. It
  also works from the editor and terminal. The tab strip's arrows and numbered
  shortcuts can select the tab too.

## Driving it with the repro environment

Preconditions: enable `OMNIGENT_FEATURES=workspace_content_search` on the test
server before launch, install `rg` on the runner's PATH, and use a session with
filesystem access and a connected runner. Follow the
[verification skill](skills/verify-omnigent/SKILL.md).

For the tab and picker, put a distinctive phrase on a known line of a
workspace text file. Search for the phrase, click its result, and confirm that
the viewer scrolls to that line. Choose Content search again and confirm the
query and results remain. Try a nonexistent phrase and an invalid regex.
With a file editor focused, press the content-search shortcut and confirm the
input receives focus. Collapse Workspace and press it again: Workspace should
reopen with the input focused and the previous query and results intact.
Switch sessions and confirm results from the previous session are absent, then
switch back and confirm its query, options and results return without a new search.
Click Filter files, include `src/**` and exclude `**/tests/**`, then submit.
Confirm results respect both filters, including after collapsing the filters or
switching conversations. Clear both fields and submit to restore the full search.
Restart the test server without the flag and reload: the tab, picker entry and
shortcut should be unavailable.

- Browser navigation through the tab, picker and search shortcut:
  `tests/browser_ui/files/test_file_line_navigation.py::test_content_search_opens_line_and_preserves_results`.
  This uses a mocked API and proves line navigation and preserved search state.
- Runner search and limits:
  `tests/runner/test_environment_filesystem.py::test_content_search_live_workspace`,
  `tests/runner/test_environment_filesystem.py::test_content_search_limits_errors_and_hidden_files`,
  `tests/runner/test_environment_filesystem.py::test_content_search_process_boundaries`.
- Server access and feature gating:
  `tests/server/routes/test_shell_permission_gate.py`.
- Component states, options and tab/picker gating:
  `web/src/shell/ContentSearchPanel.test.tsx` and
  `web/src/shell/WorkspacePanel.test.tsx`.

## Gotchas

- **Find files by name** in Files searches paths; **Content search** searches text.
- Search state stays in memory per conversation until the page reloads. Returning
  to a conversation restores its last search; submit again to refresh the results.
- Search covers the session workspace on disk, including hidden and untracked
  files allowed by ignore rules. Unsaved editor changes are not searched.
- Binary files, `.git` and symlinks are excluded. Ignore rules apply unless an
  explicit Include glob overrides them, as in ripgrep. `.gitignore` follows
  ripgrep's normal repository detection; `.ignore` works outside Git.
- Read errors retain partial results with a warning. Only sandbox-readable ignore
  rules apply; inaccessible ignore files can leave normally ignored paths in results.
- Searches return at most 500 matching lines and stop after 15 seconds. Partial
  results are labeled; exceptionally long matching lines may be omitted with a warning.
- Older runners show guidance to update the host and restart the session. Offline
  runners or missing ripgrep show an error. Search has no host-daemon fallback.
- Workspace file-sharing permissions apply. A shared conversation alone does not
  grant access to its file contents.
- This entry is in the desktop Workspace panel. Mobile file drawers do not
  expose content search yet.
