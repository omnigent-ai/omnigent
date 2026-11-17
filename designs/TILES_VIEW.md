# Tile view

The Tile view shows every active agent at once as a live tile. Clicking a tile
makes it the **active agent**: its composer takes input, while the other tiles
keep streaming read-only. A sidebar button toggles between the single agent
view (`/c/<id>`) and the Tile view (`/tiles`), keeping the same agent active in
both directions.

Gated by the `tiles_view` release flag (default off).

## Motivation

People increasingly run several coding agents in parallel, one per repository
or task, and lose track of which one is working, which one is waiting on them,
and which one finished. The sidebar lists sessions; it does not show them.

The design comes from a standalone multi-agent workbench built for Claude Code,
used daily with ten or more concurrent agents. The habits it formed:

- **Glanceable status.** Every agent visible, with a ring on the one that needs
  you, so approvals don't sit unnoticed.
- **One active agent.** You type to one agent at a time; the rest stay visible
  without accidental input.
- **Switch layouts, not windows.** A dense overview (Cards), a working grid
  (Gallery), and one large agent with the others alongside (Focus).
- **Arrange to your screen.** Bigger tiles for the agents you watch and a column
  count that fits the display.

Omnigent already has the pieces: a harness-neutral session list with live
updates, a reusable chat pane, and Grid's active-pane model. The Tile view
composes them so the experience works for every harness, not just one.

## First PR

- **Route and gating.** `/tiles` behind `tiles_view`. The sidebar button and
  the Settings group appear only when the server advertises the flag.
- **Which sessions get a tile.** The viewer's own non-archived sessions that are
  running, waiting on the viewer, or updated within two days. Rows come from the
  sidebar's existing "mine" data (`useSidebarData`), so the view adds no
  session-list request and shares its live updates.
- **Active agent.** The `active` query parameter. With no valid choice, the first
  tile waiting on the viewer, else the first tile. Clicking a tile body activates
  it; clicks on its controls do not.
- **Layouts.** Gallery (live tiles), Focus (active agent large plus a live strip),
  Cards (status only; picking a card opens Focus with it active). Stored per
  device and reflected in `?view=`.
- **Gallery sizing.** Drag a tile's right edge, bottom edge or corner to span
  whole slots, up to 4 × 3; double-click a handle to reset. A Columns picker
  (Auto, 1–8) caps the column count, remembered per display.
- **Settings → General → Tiles.** *Show Tiles in the sidebar* (on) and
  *Open in Tiles* at launch (off). Both are per device.

### Stream budget

A live tile reuses the side chat pane, which holds one session stream. Over
HTTP/1.1 the browser allows about six connections per origin, shared with the
session-updates socket and any open terminal, so tiles keep two streams there
(the active tile plus one); HTTP/2 uses the conversation registry's full budget.
Tiles beyond the budget render as status cards until activated, and a tile that
stops being live releases its stream immediately rather than waiting for
registry eviction.

## Second PR: Farm features

- **Favorites, filters, search.** A star pins the session (the sidebar's pinned
  label, so favorites match Pinned everywhere) and sorts it first. Status radios
  with counts, a project menu (`?project=`), and search (`/` to focus, Esc to
  clear) narrow the tiles.
- **Stack.** Park a tile to take it off the grid; agents quiet for a day stack
  automatically, and agents waiting on you never do. The Stack drawer lists them
  with Unstack. Per device.
- **Tile colors and title.** A color stripe opens twelve hues or Auto (a stable
  hue per session) for the header tint and border; the title is larger.
- **Workspace drawer.** The session Workspace panel (Files, Changes, Terminal)
  for the active tile only, so one session loads workspace data at a time. It
  follows tile switches, asks before discarding unsaved edits, and opens file
  links from the active tile's chat. It shares Grid's implementation through
  `useSessionWorkspace`, `SessionWorkspacePanel` and
  `useResizableWorkspacePanel`. An agent with no runner (imported or dormant)
  shows "Agent isn't running" instead of requesting files.
- **Agent details.** Clicking a tile's title opens cards: Now, Where, Usage,
  Agent (with inline rename, as in the header) and Latest reply. Usage and the
  reply are fetched only while the details are open.
- **Arranging.** Full screen (button, title double-click or F; Esc returns),
  per-tile text size (85–130%, applied as zoom on the body), drag to reorder by
  the handle or its arrow keys (tiles never moved keep their natural place), and
  `[` / `]` to switch the active agent. Pop-out opens the agent in its own
  browser window; the desktop shell sends `window.open` to the system browser,
  so it is hidden there.

## Follow-ups

1. **Open in browser (desktop).** A shell button that opens the current view in
   the default browser, and a desktop pop-out window.
2. **Metrics.** An Activity Monitor–style view: a sortable per-agent process
   table (CPU, memory, tokens) with a detail inspector. Needs host-reported
   process metrics.
3. **History, Wake and Scan.** Past agents per project, waking a dormant agent,
   and finding Claude Code or Codex sessions on disk to import.
4. **Terminal-first tiles.** A Chat/Terminal switch for native terminal
   sessions, as in Grid.
