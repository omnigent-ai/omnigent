# Workspace content search

Users search the session workspace's file contents from the desktop Workspace
panel or the mobile session menu, then open a matching file at its line.
Filename search remains in Files.

## Sub-features

- `search`: submit a literal query with Enter or Search, optionally matching case
  or using a ripgrep regular expression; results show paths, lines and previews.
- `whole-word`: check **Whole word** to skip matches inside longer words. The
  panel sends it as a ripgrep regular expression on the same route; a search
  whose expression would exceed the 4,096-character query limit shows an error
  and disables Search.
- `filter`: click **Filter files** (the funnel button between the query and
  Search) to show or hide **Include** and **Exclude** globs. The button is
  highlighted while filters are set, even when the fields are hidden.
  Comma-separated patterns and brace alternatives work (for example `src/**,
  *.{ts,tsx}`); excludes take precedence. Clear both fields to search all files again.
- `navigate`: open a result at its line and return to the preserved search;
  switching conversations restores each conversation's query, options and results.
- `states`: initial, searching, no matches, errors and partial results.
- `gate`: on by default; `OMNIGENT_FEATURES=-workspace_content_search` hides it.

## How to get to it (user POV)

- Desktop web and desktop app: open a session's Workspace panel and select the
  **Content search** magnifying-glass tab.
- Workspace panel picker: choose **Select panel → Content search**.
- Mobile web and mobile apps: open the session's **Conversation actions** menu
  and choose **Content search**; it opens as a full-screen drawer.
- Keyboard: **Cmd+Shift+F** on macOS (**Ctrl+Shift+F** on Windows/Linux) opens
  the Workspace panel, selects **Content search**, and focuses its input. It
  also works from the editor and terminal. The tab strip's arrows can select the
  tab too, and so can its number: **Ctrl+1–9** on macOS (**Alt+1–9** on
  Windows/Linux, except in a terminal) from anywhere in the app.
- Tabs sidebar: with `workspace_sidebar_v2` on, in a desktop app or desktop
  browser (not a touch client), the sidebar shows tabs instead of the Workspace
  panel, in the Workspace drawer when the window is narrower than 1181px.
  **New tab → Search text**, **Ctrl+Cmd+O** on macOS (**Ctrl+Alt+O** on
  Windows/Linux) and ⌘K's **Search Text in Workspace…** all open Go to File on
  its **Text** tab: it lists matching lines under their files with a preview,
  and Enter opens the file at the line. **Open in Search** (Cmd/Ctrl+Enter)
  hands the search and its results to the Search page, a tab of its own in the
  strip, ready to type into; move between tabs from the strip or tab search. The
  Workspace panel's **Cmd+Shift+F** isn't bound while the tabs show.
- File viewer menu: in the tabs sidebar's file viewer, select text and
  right-click it (or press **Shift+F10** or the Menu key), then choose **Search
  Text in Workspace…** to search for the selection's first non-blank line. The
  row shows only where the session can search text and that line is at most 4,096
  characters.
- Tabs sidebar's Files tab: with a text file open, choose **File actions (⋯) →
  Search Text in Workspace…**, shown while the session can search text. **Go to
  File in Workspace…** beside it needs only the session's file finder.

## Driving it with the repro environment

Preconditions: leave `workspace_content_search` enabled (the default) on the test
server, install `rg` on the runner's PATH, and use a session with
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
With `workspace_sidebar_v2` on, open that file in the tabs sidebar's Files
viewer, select the phrase and right-click it. Where the session can search text,
choose **Search Text in Workspace…** and confirm Go to File opens on its **Text**
tab with the phrase as its query and its line among the results, even when Go to
File was already open on another query. Press **Open in Search** (Cmd/Ctrl+Enter)
and confirm the sidebar's Search page shows the same search. Select only blank
lines and confirm the row is gone. Where the session can't search text, confirm
the menu leaves the row out and keeps its other rows.
Restart the test server with `OMNIGENT_FEATURES=-workspace_content_search` and
reload: the tab, picker entry and shortcut should be unavailable.

- Browser navigation through the tab, picker and search shortcut:
  `tests/browser_ui/files/test_file_line_navigation.py::test_content_search_opens_line_and_preserves_results`.
  This uses a mocked API and proves line navigation and preserved search state.
- Whole word patterns through real ripgrep:
  `tests/runner/test_environment_filesystem.py::test_content_search_whole_word_patterns`
  and `web/src/lib/contentSearch.test.ts` share `web/src/lib/__fixtures__/contentSearchWholeWord.json`.
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
- Whole word follows ripgrep's Unicode word characters. A literal that starts or
  ends with punctuation matches as typed on that side (`C++` also finds `C++x`);
  a regular expression always needs a word boundary at both ends. An expression
  that closes a group it never opened (`foo)|(bar`) or turns on verbose mode
  (`(?x)`) is refused with Whole word on.
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
  runners or missing ripgrep show an error. Search tries the workspace API first
  when the host lists it, and otherwise uses the REST search.
- Workspace file-sharing permissions apply. A shared conversation alone does not
  grant access to its file contents.
- On mobile, tapping a result opens the file viewer; its back button returns
  to the drawer with the preserved search.
