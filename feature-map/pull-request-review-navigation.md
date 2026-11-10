# Pull request review navigation

Move through a pull request's Changes tab: filter the changed files by path, find
text in the fetched patch, copy and open files at the reviewed revision, and mark
files viewed with progress and hide-viewed controls, and browse the pull request's
commits one at a time. Viewed marks are kept in this browser only. A file stays viewed on a new head while its diff hunks are unchanged,
and a changed file is flagged.

## Sub-features

- `path-filter`: **Filter changed files** narrows the file list, tree, counts and
  Expand/Collapse all by path or previous path. Empty state reads
  `No changed files match "<query>"` with **Clear filter**. Escape clears it. Each
  file keeps its collapse state. When the file list is hidden (hidden by default on
  narrow screens), a toolbar button opens the filter above the list.
- `find`: **Find in changes** bar over the fetched patch. Toggles for match case and
  regular expression. Status reads `k of N`, with `+` after N at the 10,000-match
  cap, `No results`, or `Invalid`. Opening the bar selects the first match in the file at
  the top of the viewport or a later file, wrapping to the first match if neither has one. Enter and Shift+Enter step through matches and wrap at the
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
- `head-reset`: A new head keeps each file's mark while its hunks are unchanged. A
  file whose hunks changed loses its mark, shows **Changed since viewed** until you
  mark it on the new head, and opens for review. A notice reads `New commits since
  you last viewed — changed files lost their viewed marks.` while a listed file has
  changed, until **Dismiss** or until every changed file is marked. A file that can't
  be compared (binary, pure rename, missing from the fetched diff, or cut short) loses
  its mark without a badge or notice.
- `commits`: A **Changes | Commits** switch starts the toolbar on GitHub pull requests
  whose list is pinned to a head. The commit list runs oldest first with title, short
  sha, author and date; **Search commits** matches sha, title or author, ignoring case.
  More than 250 commits shows `History limited to 250 commits`. Selecting a commit
  shows its patch with the same file list, diff and find bar, plus **Back to commits**,
  **Previous commit** and **Next commit**. A commit view has no viewed controls, review
  conversations or drafts, or context expansion, and **Open in** uses the commit's sha.

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
during a refresh, the Viewed checkboxes, the progress row, Hide viewed and the changed-files
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
covers `rail` and `composer-mobile`. It marks both files (which collapses them),
checks `2 / 2 files viewed`, reloads and checks the marks survive, then switches to a
new head where only `main.py` changes. `README.md` keeps its mark, `main.py` shows
**Changed since viewed**, and the notice appears. It dismisses the notice, marks
`main.py` again, then uses **Hide viewed** until `All 2 files viewed` appears and
**Show viewed** brings the files back.

- `composer-desktop` and `canvas`: Open Changes and tick **Viewed** on one file.
  The file should collapse and the progress should read `1 / N files viewed`.
  Reload the page, and the mark should still be there. On a live PR, push a new
  commit that changes that file and reload. Its mark should be gone, the file should
  show **Changed since viewed**, and the notice should show. Files the commit leaves
  alone keep their marks.

### commits

`tests/e2e_ui/github/test_github_tab.py::test_changes_commits_view_lists_and_opens_a_commit`
covers `rail`. It checks the viewed controls in Changes, switches to Commits, searches
the list, opens a commit and checks that only its file is listed and the viewed
controls are gone, steps with **Next commit**, and returns with **Back to commits**.

- `composer-desktop`, `composer-mobile` and `canvas`: Open Changes on a GitHub pull
  request with several commits and choose **Commits**. Search by an author's name and
  confirm the list narrows. Open a commit: only its files should be listed, with no
  **Viewed** checkbox or **Hide viewed**. Use **Next commit**, then **Back to commits**,
  then **Changes**, and confirm your viewed marks are unchanged.

Component coverage for these helpers and views lives in
`web/src/shell/PullRequestChangesTab.test.tsx`,
`web/src/shell/PullRequestCommitsList.test.tsx`,
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
- **Open and viewed need a pinned list.** GitHub, GitLab and Azure DevOps lists report
  the revision they show, so **Open in** and the Viewed checkbox work for each. A
  workspace-branch list (no pull request selected) reports no revision, so **Open in**
  shows `Revision unknown`, and the Viewed checkbox, progress row and Hide viewed are
  all hidden. Real GitLab and Azure DevOps pull requests have not been exercised; these
  results come from the code paths.
- **Viewed marks are browser-local.** They do not sync to GitHub or other browsers.
  They are separate for each Omnigent server, signed-in user and pull request, and
  clearing site data removes them. If the browser refuses storage, your changes stay in
  this tab over the stored marks, so other tabs' writes still show. A reload drops them,
  with the toast `Viewed files can’t be saved on this device.` Clearing site data also
  discards them.
- **Marks follow the content, not the head.** Each mark is the hash of a file's hunks
  as you viewed them. One mark per file covers every head of the pull request, and a
  tab still open on an older head adds its own hash without disturbing the newer
  head's mark. A file keeps its three most recently marked hashes. A file that changes
  back to a version beyond those three shows as changed.
- **Unmarking forgets every head.** Unmarking a file clears all of its marks, including
  the hashes an older head's tab recorded. The file then reads as unviewed on every
  head, with no **Changed since viewed** badge.
- **Unverified marks drop silently.** A file the fetched diff leaves out, a file with
  no text hunks, and a file whose hunks were cut short can't be compared. Its mark
  matches only on the head it was made for, and a mismatch shows no badge and no
  notice. A mark recorded only that way stays silent even after the file's hunks
  become comparable; the badge needs at least one comparable mark. Stand-ins from other
  heads are discarded before the current mark is recorded, and the current mark counts
  toward the three-hash limit.
- **Filter and Hide viewed are not kept across reloads.** Hide viewed starts off.
- **Thread navigation can clear a filter.** Previous and Next unresolved
  conversation reveal a conversation on a file the path filter or Hide viewed hides,
  clearing that filter first and showing `Filter cleared to show <file>`. Other
  conversations on hidden files stay hidden with their files.
- **Known rough edges.** Marks made in another tab on the same head show as
  checked but do not collapse the file until the Changes view remounts (for example
  after a reload) or the head changes.
- **Commits need GitHub and a current server.** GitLab and Azure DevOps show Changes
  only. If the Omnigent server predates the commit view, the switch disappears for that
  pull request and the toast `Commits need an updated Omnigent server.` appears.
- **The commit list follows the list's head.** A newer push does not reload it until
  the Changes list reloads. A commit's patch is fixed by its sha. GitHub truncates the
  diff of a commit that touches more than 300 files, with no warning shown.
- **Canvas requires its release feature.** Check it is enabled before using the
  `canvas` entry.
