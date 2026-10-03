# Design page

The Design page (`/design`, "Design" in the sidebar under Inbox and Canvas)
lists every `*.slides.html` deck found in the workspaces of the 50 most recent
top-level sessions, grouped by workspace with a design kit indicator, and
renders the selected deck in the same viewer and kit as the session file viewer.
It is behind the default-off `design` release feature
(`OMNIGENT_FEATURES=design`) and uses only the existing session file APIs.

## Sub-features

- `design-nav`: the sidebar "Design" link is shown only while the flag is on and
  is highlighted on `/design`, where New session is not highlighted.
- `design-list`: one group per workspace, labelled with the session's project
  name or the workspace folder name. Each row shows the deck name, its path in
  the workspace, and the title of the session it was found through. Decks under
  `.worktrees/` or `node_modules/` are left out.
- `design-kit-indicator`: a group header shows the kit name when
  `.omnigent/design-kit/kit.json` parses, "No kit" (a link to the sample kit
  instructions) when there is none, or "Kit invalid" when it does not parse.
- `design-select`: on desktop, choosing a row renders the deck in the right
  pane with an "Open in session" link to the file in its session. With nothing
  selected the pane shows a hint.
- `design-url`: the selection is in the URL (`?session=<id>&file=<path>`), so a
  deck link reopens it and back and forward move between decks.
- `design-phone`: under 768px the list fills the screen; a deck opens full
  screen with a Back control, and the browser back button also returns.
- `design-loading`: each group shows a skeleton while its search runs; finished
  groups render without waiting for the rest.
- `design-empty`: no decks anywhere shows "No decks yet...".
- `design-unavailable`: a workspace whose search returns 404 or 503 shows
  "Unavailable: open the session to start its runner" with a session link.
- `design-error`: any other search failure shows the error and a Retry button.
- `design-deck-error`: a deck that cannot be read shows the error in the viewer
  pane next to "Open in session".
- `design-refresh`: Refresh reloads the session list, every search, the kit
  indicators, and the open deck; reads also refresh on mount and window focus.
- `design-flag-off`: no nav entry, and `/design` shows Page not found.

## How to get to it (user POV)

**Desktop web:** with the flag on, choose "Design" in the sidebar, or open
`/design` directly.

**Desktop app:** the same sidebar entry and route inside the Electron shell.

**Phone (web or mobile app):** open the sidebar overlay and choose "Design".

**Deck link (any surface):** open `/design?session=<id>&file=<path>` to land on
that deck.

## Driving it with the repro environment

Preconditions: a running instance started with `OMNIGENT_FEATURES=design`
(`verify-env start`, then `verify-env doctor`), the built web UI, and at least
one session whose workspace holds a `*.slides.html` file. The E2E tests stub
`/v1/info` themselves, so they do not need the flag set.

- Flag off (no nav, Page not found):
  `tests/e2e_ui/design/test_design_page.py::test_design_page_is_absent_while_the_feature_is_off`.
- Desktop web nav, list, selection, viewer, URL, and reload with a real seeded
  deck:
  `tests/e2e_ui/design/test_design_page.py::test_design_page_lists_and_renders_a_seeded_deck`.
- Phone list, full-screen deck, browser back and forward, Back control, the
  unavailable group, and "No kit", against a stubbed API:
  `tests/e2e_ui/design/test_design_page.py::test_design_page_on_a_phone_shows_unavailable_and_returns_to_the_list`.
- Component states (loading, empty, unavailable, error and Retry, kit
  indicator, deck read failure, Refresh, phone layout):
  `cd web && pnpm exec vitest run src/pages/DesignPage.test.tsx`. List rules
  (cap, dedupe, labels, exclusions): `pnpm exec vitest run src/lib/designDecks.test.ts`.
- Nav and route gating: `cd web && pnpm exec vitest run src/shell/Sidebar.test.tsx src/App.test.tsx`.
- Desktop app: no test. Launch with `just electron-dev` against the instance,
  choose Design, select a deck, and check the two panes and Open in session.
- Kit applied, by hand: copy `examples/design-kits/sample` to
  `.omnigent/design-kit` in a deck's workspace, choose Refresh, and check the
  header shows the kit name and the deck uses the kit's colors and logo.
- Offline session, by hand: stop the runner of a session that has decks,
  choose Refresh, and check its group shows the Unavailable row and link.

## Gotchas

- Only the 50 most recent top-level, non-archived sessions are scanned; decks
  only in older sessions do not appear.
- Sessions without a workspace path are skipped. Several sessions in one
  workspace make one group, read through the most recent of them, so the row's
  session title is that session's title.
- The project label applies only to sessions the viewer owns, as in the
  sidebar; a shared session's group uses its folder name.
- The indicator reads `kit.json` alone, so a kit whose assets are missing can
  show its name here and still show "Design kit not applied" in the viewer.
- Search returns at most 500 matches per workspace.
- The Files panel's own search hides 404 and 503; on this page they are the
  Unavailable row, not an empty workspace.
- The flag is read at page boot; reload after changing `OMNIGENT_FEATURES`.
