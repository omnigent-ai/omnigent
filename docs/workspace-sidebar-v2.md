# Workspace sidebar v2

Sidebar v2 rebuilds the workspace sidebar around tabs that behave the way
browser tabs do. We want Omnigent to work for general knowledge work as well as
coding, and browser-style navigation is something every new user already knows.

## Principles

- **Everything is a tab.** Files, Diffs and Pull Requests are pages that open in
  a tab, and so are the session's terminals, agents and other panels. "+" opens
  a New tab that lists the pages. The sidebar always has at least one tab.
- **Pages link to each other.** A file, a diff, a commit and a pull request
  each have a link, and following one shows that page the way a link does on
  the web.
- **Navigation stays in the current tab.** Choosing a file in the tree or a
  pull request in the list replaces what the tab shows. ⌘-click (Ctrl-click
  off macOS) or **Open in new tab** in a row's context menu opens a new tab
  instead. A link to something already open in another tab selects that tab
  rather than opening a duplicate.
- **One back and forward history per session, across tabs.** Unlike a browser,
  history isn't kept per tab. Back and Forward walk every step in order, both
  within a tab and from one tab to another. When an agent's link opens a file
  in another tab, Back returns you to the tab you were reading.
- **Mobile first.** The pages are laid out for a narrow screen from the start.
  Phones are still early, but narrow desktop windows and split views need the
  same layouts, so the desktop gets the benefit as well. On a phone the sidebar
  opens in the existing full-screen drawer.

## Pages

- **Files**: the workspace tree, with the file viewer and editor beside it.
  Below a certain width the tree opens as a popover from the toolbar.
- **Diffs**: the working copy's changes as a review, including rendered
  Markdown diffs. Rendered Markdown is sanitized before it reaches the page, as
  in the existing file viewer. Where the host can compare revisions, a Commits
  list shows the branch's commits, each opening what it changed.
- **Pull Requests**: the session's pull requests and a page for each one. On a
  wide enough tab the list stays beside the open pull request.

## Two journeys

**Following an agent's citation.** You're reviewing a pull request in a tab.
The agent's reply cites `src/parser.ts:42`. Clicking the citation shows the
file at that line, in the tab already showing that file or else in a new one.
Back returns you to the pull request where you left it, and Forward goes to the
file again.

**Reading around a change.** In a Files tab you pick `README.md` in the tree,
then `src/parser.ts`; the tab steps from one to the other. You ⌘-click
`src/lexer.ts`, which opens in a new tab and leaves `src/parser.ts` where it
was. Back walks the same steps in reverse: to `src/parser.ts` in the first tab,
then to `README.md`.

## Flag

The change that routes the sidebar to v2 adds one server feature flag,
`workspace_sidebar_v2`, in `omnigent/server/feature_flags.py`. It is off by
default and listed in `/v1/info`; `OMNIGENT_FEATURES=workspace_sidebar_v2`
turns it on. With it off, the sidebar is unchanged.

## Data

The sidebar reads through the workspace API
(`omnigent/api/workspace/v1alpha1/workspace.proto`), which the session's host
serves over the routes described in `docs/workspace-fast-track.md`. Clients ask
the host which methods it serves before calling one. Until hosts serve search
and the git methods, search uses the existing REST routes and Diffs shows only
uncommitted changes, with a notice where commits would be. Those methods will
run in Python on the host, with the programs a repository's git config can name
(external diff commands, textconv drivers, filesystem-monitor hooks) turned off,
so a read never runs code the repository chose.
