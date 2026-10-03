# Design page

A top-level "Design" entry in the left nav, next to Inbox and Canvas, where a
user sees every slide deck their agents produced across recent sessions and
renders any of them on brand, without first finding the right chat and opening
its Files panel.

This is phase 1 (gallery). It builds on the `*.slides.html` deck viewer and
workspace design kits. A studio (start a design from this page), wireframes,
a server-side index, and user or org-level kits are later phases.

## Goals

- One place to find and open decks across sessions, on desktop web, the
  desktop app, and phones.
- Decks render exactly as in the session file viewer, including the
  workspace design kit.
- No new server API: reuse the session-scoped file endpoints, so access
  control is unchanged.

## Non-goals (phase 1)

- Creating or editing designs from this page.
- Wireframes or other artifact types.
- Thumbnails. The list is text; the selected deck renders in the viewer.
- Searching every session ever created. Only recent sessions are scanned.
- Uploading or managing kits from the page.

## User experience

**Nav.** A "Design" link sits under Inbox and Canvas in the shared sidebar,
which every surface loads. It is hidden when the `design` feature flag is off.
It highlights on `/design` and New session does not.

**Desktop and desktop app (768px and wider).** Two panes. The left pane lists
decks grouped by workspace; the right pane renders the selected deck in the
existing deck viewer, with an "Open in session" link that goes to
`/c/<session>?file=<path>`. With nothing selected, the right pane shows a short
hint.

**Phone.** The list fills the screen. Choosing a deck opens the viewer full
screen with a Back control; the browser back button also returns to the list.

**URL.** The selection lives in the URL, `/design?session=<id>&file=<path>`,
so a deck is linkable and back and forward work on every surface.

**Group header.** The workspace label (the session's project name when it has
one, otherwise the workspace folder name) and a kit indicator: the kit name
when `.omnigent/design-kit/kit.json` parses, "No kit" otherwise, or
"Kit invalid" when it does not parse. "No kit" links to the copy instructions
in `examples/design-kits/sample/README.md`.

**List row.** The deck name (file name without `.slides.html`), its path
relative to the workspace, and the title of the session it was found through.

## Data flow

1. Take the 50 most recent non-archived top-level sessions the user can see,
   from the session list the sidebar and Canvas already load.
   The cap is deliberate; the server-side index in a later phase replaces
   the scan when it gets slow or misses older decks.
2. Deduplicate by workspace path. For each workspace, the most recent session
   is the one used for every read in that group.
3. For each workspace, call the existing file search endpoint with
   `include=**/*.slides.html`, through the same client hook the Files panel
   uses. Drop matches with a `.worktrees/` or `node_modules/` path segment so
   nested worktrees do not show duplicate decks.
4. For each workspace with at least one deck, read
   `.omnigent/design-kit/kit.json` only (no assets) for the kit indicator.
5. When a deck is selected, read its content with the existing
   `fetchFileContent(session, path)` and render the existing deck viewer with
   that session id, so the kit is applied exactly as in the file viewer.

Queries refetch on mount and window focus, and a Refresh button refetches
everything.

## States

- **Loading:** each group shows a skeleton while its search runs; groups that
  finish render without waiting for the rest.
- **Empty:** no decks anywhere shows "No decks yet. Ask an agent for a slide
  deck; files ending in `.slides.html` appear here."
- **Workspace unavailable:** a 404 or 503 from search (no file environment or
  runner offline) shows the group with "Unavailable: open the session to start
  its runner" and a link to the session, rather than hiding it.
- **Error:** any other failure shows the group with a short error and a retry
  button.
- **Deck read fails:** the viewer pane shows the error and the "Open in
  session" link.
- **Flag off:** no nav entry, and `/design` uses the existing feature-gated
  page behavior.

## Feature flag

A `design` flag, off by default, declared the same way as `canvas` (server
feature enum and definition with owner and review release, mirrored in the web
capability keys), and turned on with `OMNIGENT_FEATURES`. The page route is
wrapped in the existing feature gate.

## Security

No new endpoints. Every read goes through the session-scoped file APIs, so a
user only sees decks in sessions whose workspace files they can already read.
Decks render in the existing sandboxed iframe with no same-origin access. Kit
handling is the existing validated loader.

## Expected changes

- `web/src/App.tsx`: lazy route for `/design` behind the feature gate.
- `web/src/shell/Sidebar.tsx`: the nav link and active-item logic, including
  the New session route exclusion.
- `omnigent/server/app.py`: add `design` to the web UI route prefixes.
- `omnigent/server/feature_flags.py`, `web/src/lib/capabilities.ts`, and the
  flag tests that pin the expected flag set.
- New `web/src/pages/DesignPage.tsx` plus a small pure helper that builds the
  grouped list (dedupe, exclusions, grouping, states).
- `feature-map/design-page.md` and its line in the feature-map index.
- Tests below.

## Testing

- **Unit:** the list helper (workspace dedupe, most recent session wins,
  path exclusions, grouping labels, unavailable and error groups, cap of 50).
- **Component:** DesignPage states (loading, empty, unavailable, error,
  populated), selection through the URL, the phone layout, the kit indicator,
  and the "Open in session" link.
- **Server:** the flag tests and route-prefix behavior.
- **E2E:** `tests/e2e_ui/design/test_design_page.py`, following the Canvas page
  test: stub `/v1/info` to turn the flag on, seed a session with a deck, open
  Design, select the deck, and assert it renders; plus flag off hides the nav
  entry. The e2e harness cannot start a runner on the current dev host, so this
  may be verified by reading until it runs in CI.
- **Manual:** desktop two-pane, phone list and back, a kit applied, an offline
  session's group.

## Later phases

1. **Studio:** "New design" picks a kit and a prompt, starts a session with
   the slide-decks skill, and shows the live deck beside the chat.
2. **Wireframes:** `*.wireframe.html` files shown in desktop, tablet, and phone
   frames, listed on the same page.
3. **Server index:** a per-session deck count or a cross-session search so the
   page stops scanning sessions and covers older ones.
4. **Kits beyond a workspace:** user or org-level kits, building on the
   operator branding config.
