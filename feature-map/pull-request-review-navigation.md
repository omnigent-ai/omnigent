# Pull request review navigation

Move through a pull request's Changes tab: filter the changed files by path, find
text in the fetched patch, copy and open files at the reviewed revision, and mark
files viewed with progress and hide-viewed controls. Viewed marks are kept in this
browser only and reset when the pull request gets a new head.

## Sub-features

- `path-filter`: **Filter changed files** narrows the file list, tree, counts and
  Expand/Collapse all by path or previous path. Empty state reads
  `No changed files match "<query>"` with **Clear filter**. Escape clears it. Each
  file keeps its collapse state. When the file list is hidden (hidden by default on
  narrow screens), a toolbar button opens the filter above the list.
- `find`: **Find in changes** bar over the fetched patch. Toggles for match case and
  regular expression. Status reads `k of N`, with `+` after N at the 10,000-match
  cap, `No results`, or `Invalid`. Opening the bar seeds the first match at or below
  the top of the viewport. Enter and Shift+Enter step through matches and wrap at the
  ends. Matches on file paths scroll to the file header. Escape or **Close find** closes the bar and keeps the query.
  Shows `+M in hidden files` with **Show** when matches sit in files the filters hide.
- `copy-path`: Copies the repo-relative current path. Renamed files also get **Copy
  previous path**. Success and failure show a toast.
- `open-revision`: **Open in** the provider opens the file at the revision the list
  shows. Deleted files open at the base revision. Disabled reasons: `Revision unknown`,
  `Source repository unavailable`, `Repository URL isn’t secure`, and
  `Opening files isn’t supported for <provider>`.
- `viewed`: Per-file **Viewed** checkbox in each file header. Marking collapses the
  file; unmarking leaves it as it is. Viewed files show a check and a dimmed name in
  the file list. Viewed controls are hidden when viewed state is unavailable.
- `progress`: `X / Y files viewed`, with `(partial)` when the changed-file list is
  truncated. A bar sits beside it outside the mobile layout; mobile shows the text only.
- `hide-viewed`: **Hide viewed** toggle, off by default. When every file the path
  filter still matches is viewed, the list reads `All N files viewed` with
  **Show viewed**.
- `head-reset`: A new head starts with no marks. A notice reads `New commits since
  you last viewed — viewed marks were reset.` until **Dismiss** or until you mark a
  file on the new head. Files collapsed by
  the previous head's marks open again once the new diff renders.

## How to get to it (user POV)

- `rail`: Open a session, expand Workspace, choose Pull Requests, then open Changes.
  With several pull requests, select one from the list first.
- `composer-desktop`: Click the desktop composer's PR link, then open Changes.
- `composer-mobile`: The composer shows no PR link on narrow screens. Open the
  Session actions menu, choose Pull Requests, then open Changes in the drawer.
- `canvas`: Open a conversation beside Canvas, click its composer PR link, then
  open Changes.

The Changes tab is the same on every entry. Its toolbar holds the find button,
the thread navigation buttons, the filter button when the file list is hidden,
and the progress row. The toolbar appears once the list has files or an unresolved
conversation exists. The viewed controls need a pinned list and a fetched diff that
reports the same head as that list (see the gotchas). While the two heads differ
during a refresh, the Viewed checkboxes, the progress row, Hide viewed and the reset
notice are hidden.

## Driving it with the repro environment

Preconditions: Build this checkout and launch the isolated instance with
[Verify Omnigent](skills/verify-omnigent/SKILL.md). The browser journeys stub the
GitHub responses, so no live GitHub credentials are needed. Run journeys one at a
time, because they share mock state. Open the panel for each entry as described
under How to get to it.

Each sub-feature below lists its entry points. The E2E journeys cover `rail` and
`composer-mobile`; `composer-desktop` and `canvas` have no journey for these
features, so drive them by hand with the steps given.

### path-filter, copy-path and open-revision

`tests/e2e_ui/github/test_github_tab.py::test_changes_filter_copy_open` covers
`rail` and `composer-mobile`. It filters, checks the empty state and Clear filter,
checks Escape, copies a path through the icon (desktop) or the overflow menu
(mobile), and checks that **Open in GitHub** links to the fork at the pinned head.

- `composer-desktop` and `canvas`: Open Changes. Type `readme` in **Filter changed
  files** and confirm only `README.md` remains. Clear the filter (**Clear filter** or
  Escape) so the other file is listed again. Click **Copy path** on it and paste it;
  the path should be repo-relative. Open that file's overflow menu and
  confirm **Open in <provider>** links to the head repository at the list's head.

### find

`tests/e2e_ui/github/test_github_tab.py::test_changes_find_reveals_collapsed_and_filtered`
covers `rail` and `composer-mobile`. It collapses a file, opens **Find in changes**,
types a query, checks `1 of 2`, Enter to step, a highlight on the matching row, the
highlight surviving a switch between split and unified layouts, Escape, and
Ctrl/Cmd+F reopening the bar.

- `composer-desktop` and `canvas`: Open Changes and collapse a file that contains
  a known added line. Press Cmd+F on macOS or Ctrl+F elsewhere with focus in the tab.
  Type the line's text. The status should read `1 of N`, the file should expand, and
  the matching text should be highlighted. Press Enter and confirm the next match
  scrolls into view.

### viewed, progress, hide-viewed and head-reset

`tests/e2e_ui/github/test_github_tab.py::test_changes_viewed_progress_persists_and_resets`
covers `rail` and `composer-mobile`. It marks a file (which collapses it), checks
`1 / 2 files viewed`, reloads and checks the mark survives, switches to a new head
and checks the reset notice, dismisses it, then uses **Hide viewed** until
`All 2 files viewed` appears and **Show viewed** brings the files back.

- `composer-desktop` and `canvas`: Open Changes and tick **Viewed** on one file.
  The file should collapse and the progress should read `1 / N files viewed`.
  Reload the page, and the mark should still be there. On a live PR, push a new
  commit and reload; the marks should be gone and the reset notice should show.

Component coverage for these helpers and views lives in
`web/src/shell/PullRequestChangesTab.test.tsx`,
`web/src/shell/PullRequestFileSection.test.tsx`,
`web/src/shell/pullRequestReview.test.ts`,
`web/src/shell/useChangesViewState.test.ts` and
`web/src/lib/pullRequestViewed.test.ts`. Run one with
`cd web && pnpm test -- src/shell/PullRequestChangesTab.test.tsx`.

## Gotchas

- **Two filters, two meanings.** **Filter changed files** hides files from the
  list. **Find in changes** never hides anything; matches in hidden files are only
  counted, under **+M in hidden files**.
- **Find searches only the fetched patch.** It covers the hunks already loaded for
  every file in the list, collapsed or not. It does not search context lines that
  appear only after **expand unchanged lines**, and it cannot find files beyond a
  truncated list. Binary and text-less files match on their path only. The banner
  `Search covers the fetched patch: N of M files` says when coverage is partial.
- **Find never changes filters.** **Show** on the hidden-file hint clears the path
  filter and turns Hide viewed off, together.
- **Find starts from the typed query.** It does not use the page selection. Its query
  and toggles survive closing the bar and switching sessions, but not a reload.
- **Cmd+F is scoped to the Changes tab.** It opens the in-tab bar with focus anywhere
  in the tab, including review comment boxes. On macOS only Cmd+F counts; Ctrl+F still
  moves the caret there. Outside the tab, the browser's own find runs.
- **Regex find runs on the page's main thread.** A pathological pattern can freeze
  the Changes tab until it finishes. Known follow-up, not yet fixed.
- **Highlights need the browser's CSS highlight API.** Without it, the match's row
  still centres but is not painted. Highlight painting was checked only in Chromium.
- **Find's reveal ends with a collapse.** Collapsing a file that find opened keeps it
  closed until you explicitly navigate to a match again, even the same one.
- **Open uses the list's revision, not the newest head.** A commit pushed while you
  read does not change the link until the list reloads. The link goes to the file
  without a line anchor.
- **Open and viewed need a pinned list.** GitLab and Azure DevOps lists do not report
  their revision, so **Open in** shows `Revision unknown`, and the Viewed checkbox,
  progress row and Hide viewed are all hidden. A workspace-branch list (no pull
  request selected) behaves the same way. Real GitLab and Azure DevOps pull requests
  have not been exercised; these results come from the code paths.
- **Viewed marks are browser-local.** They do not sync to GitHub or other browsers.
  They are separate for each Omnigent server and signed-in user, and clearing site
  data removes them. If the browser refuses storage, marks stay in memory and a
  reload drops them, with the toast `Viewed files can’t be saved on this device.`
- **New head, new marks.** Marks from an older head never appear on the new one. The
  notice shows while the pull request has marks recorded on another head and nothing
  recorded on the current one. It ends when you dismiss it or mark a file on the new
  head; unmarking everything afterwards does not bring it back.
- **Filter and Hide viewed are not kept across reloads.** Hide viewed starts off.
- **Thread navigation can clear a filter.** Previous and Next unresolved
  conversation reveal a conversation on a file the path filter or Hide viewed hides,
  clearing that filter first and showing `Filter cleared to show <file>`. Other
  conversations on hidden files stay hidden with their files.
- **Known rough edges.** Marking a file while Hide viewed is on removes it from the
  list and drops keyboard focus. Marks made in another tab on the same head show as
  checked but do not collapse the file until the Changes view remounts (for example
  after a reload) or the head changes.
- **Canvas requires its release feature.** Check it is enabled before using the
  `canvas` entry.
