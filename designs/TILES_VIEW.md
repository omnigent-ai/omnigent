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

## Follow-ups

1. **Farm features.** Favorites (on pinned sessions), status and project
   filters, search, a Stack for parked or quiet agents, per-project history and
   Wake, the Workspace panel following the active tile, an expandable details
   panel, full screen, pop-out, per-tile text size, tile color, and drag to
   reorder.
2. **Metrics.** An Activity Monitor–style view: a sortable per-agent process
   table (CPU, memory, tokens) with a detail inspector. Needs host-reported
   process metrics.
3. **Terminal-first tiles.** A Chat/Terminal switch for native terminal
   sessions, as in Grid.
