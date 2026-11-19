# Workspace sidebar v2

Sidebar v2 rebuilds the workspace sidebar around tabs that behave the way
browser tabs do. We want Omnigent to work for general knowledge work as well as
coding, and browser-style navigation is something every new user already knows.

## Principles

- **Everything is a tab.** Every page opens in a tab: the new Files, Diffs and
  Pull Requests pages, and the pages the sidebar already has, such as
  terminals, the browser and side chats. "+" opens a New tab that lists them.
  The sidebar always has at least one tab.
- **Everything has a link.** Each place a tab can show has a link that names
  it exactly, such as a file at a line or a pull request's changes. Agents can use
  these links to open tabs for you, and you can share or reopen them. See
  [Links](#links).
- **Navigation stays in the current tab.** Choosing a file in the tree, a
  commit in the Commits list or a pull request in the list changes what this
  tab shows.
  ⌘-click (Ctrl-click off macOS) or a middle click in a file tree, or **Open in
  new tab** in a file's menu, opens a new tab instead. A link to something
  already open in another tab selects that tab rather than opening a
  duplicate.
- **One back and forward history per session, across tabs.** Unlike a browser,
  history isn't kept per tab. Back and Forward walk every step in order, both
  within a tab and from one tab to another. When an agent's link opens a file
  in another tab, Back returns you to the tab you were reading.
- **Mobile first.** The pages are laid out for a narrow screen from the start.
  Phones are still early, but narrow desktop windows and split views need the
  same layouts, so the desktop gets the benefit as well. On a phone the sidebar
  opens in the existing full-screen drawer.

## Pages

New in v2:

- **Files**: the workspace tree, with a file editor beside it. Markdown files
  open in the existing file viewer, with its rich preview and edit modes, as do
  images, PDFs and other binary files. Below a certain width
  the tree opens as a popover from the toolbar.
- **Diffs**: the working copy's changes as a review, including rendered
  Markdown diffs. Rendered Markdown is sanitized before it reaches the page, as
  in the existing file viewer. Where the host can compare revisions, a Commits
  list shows the branch's commits, each opening what it changed.
- **Pull Requests**: the session's pull requests and a page for each one.

Existing pages, which come along unchanged as tabs: terminals, the browser,
side chats, agents, tasks and text search.

## Links

A link is `omnigent://<kind>[/<id>]?<parameters>`, and a plain `https://`
address opens in a browser tab. Some examples:

| Link | Shows |
| --- | --- |
| `omnigent://files?path=src/parser.ts&line=42` | A file at a line. `line=40-48` cites a range, and `column` a column. |
| `omnigent://changes?path=src/parser.ts&line=42` | That file in the session's diff. |
| `omnigent://changes?base=main&head=feature` | Another comparison: `base` against `head`, or against the working copy without `head`. |
| `omnigent://pull?url=<pull request URL>&view=changes` | A pull request's changes. `view` is `summary`, `comments`, `commits` or `changes`. The link can also name a `path`, `line` and `commit`, but the page can't show those yet: it shows the whole view with a note saying so. |
| `omnigent://terminal/<key>`, `omnigent://agent/<id>`, `omnigent://chat/<id>` | A terminal, an agent or a side chat. |
| `omnigent://search?q=<text>` | Text search. |

An `omnigent://` link can add `session=<id>` to open in another session's
sidebar, or `tab=<id>` to open in a specific tab. A web page addressed that way
is `omnigent://web?url=<encoded URL>&session=<id>`. A pull request is named by its URL, and a
sub-page URL such as `…/pull/12/files` becomes the matching view. Each link has
one canonical text, so the same place always gets the same link.

Opening a link picks a tab by a few rules:

- A tab already showing the same thing (a file, a comparison, a pull request)
  is reused and moves to the cited place. A file link with no such tab shows in
  the Files tab if that's the tab you're viewing. Otherwise a new tab opens
  beside the selected one.
- When you open the link, the tab is selected and the step joins the session's
  history, so Back returns you to where you were. Moving to another line of
  the file the selected Files or Diffs tab already shows replaces the step
  instead of adding one. Switching tabs, and moves within a pull request, add a
  step.
- When an agent opens one, it never takes over what you're looking at. A tab
  you aren't viewing moves quietly, or a new one opens behind yours. If the tab
  is the one you're viewing, has unsaved edits or is your Files tab, it shows a
  bar offering to go to the agent's place instead.

What's settled and what's open:

- **In the first tabs change:** places, the link syntax above, parsing and
  canonical formatting, how an open picks a tab, and the shared history, which
  is saved with the session's tabs and survives a reload.
- **Planned:** showing a file, line or commit inside a pull request's page;
  agents emitting these links in chat and having them open in the sidebar; and
  a way for an agent to open a place directly.
- **Open:** how these links sit beside the existing session links
  (`omnigent://<host>/c/<id>`), which use the same scheme; whether code-host
  URLs and absolute paths in chat resolve to places; and whether the syntax
  becomes a stable, documented contract.

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
