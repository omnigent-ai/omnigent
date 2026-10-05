# Design page

The Design page (`/design`, "Design" in the sidebar under Inbox and Canvas)
shows every `*.slides.html` deck found in the workspaces of the 50 most recent
top-level sessions as cards grouped by workspace, lets the user ask for a new
deck without leaving the page, and opens any deck in a studio: the session's
compact chat beside a live preview that updates as the agent writes. It is
behind the default-off `design` release feature (`OMNIGENT_FEATURES=design`)
and uses only the existing session, host filesystem, and file APIs.

## Sub-features

- `design-nav`: the sidebar "Design" link is shown only while the flag is on and
  is highlighted on `/design`, where New session is not highlighted.
- `design-landing`: the header ("Design", "Slides your agents made, on brand",
  Refresh, New design) above deck cards in a responsive grid, one group per
  workspace labelled with the session's project name or the folder name. A card
  shows the deck name, its path, and the session title. Decks under
  `.worktrees/` or `node_modules/` are left out.
- `design-search`: the search box filters cards by deck name, workspace label,
  or session title; nothing matching shows "No designs match".
- `design-suggestions`: "Pitch deck from my notes", "Weekly status update", and
  "Product launch" chips under the cards and in the empty state open New design
  with that prompt.
- `design-kit-indicator`: a group header shows the kit name when
  `.omnigent/design-kit/kit.json` parses, "No kit" (a link to the sample kit
  instructions) when there is none, or "Kit invalid" when it does not parse.
  When the workspace has `.omnigent/design-system.json`, the header shows that
  design system's name and kind ("Full" or "Skill-only") instead, or "Design
  system invalid" when the pointer does not parse.
- `design-system-field`: the New design dialog's Design system field offers
  None, "Folder kit" (when the folder has a kit), recent design systems for
  the host (kept in localStorage), and "Choose folder" (the host folder
  picker). A folder with `_ds_manifest.json` is a full system, one with only
  `SKILL.md` is skill-only, and anything else shows "Not a design system: no
  SKILL.md or _ds_manifest.json". The default is the folder kit, else the
  host's most recent system, else None. On create the dialog reads the name
  (manifest `namespace`, else `SKILL.md` `name`, else the folder name) through
  the new session, writes `.omnigent/design-system.json` through the existing
  workspace file write, and adds "Follow the design system at `<path>`
  (`<kind>`). Read its SKILL.md first." to the first message. The deck viewer
  behavior is `deck-design-system` in `feature-map/slide-decks.md`.
- `design-new`: the New design dialog takes a prompt, an agent (the last one
  used for a design, else the default), an online host, and a folder (the last
  one used on that host). It shows "Kit found" or "No kit" for the folder,
  creates the session, sends the prompt plus the slide-decks instructions for
  `decks/<slug>.slides.html` (with `-2`, `-3` when the name is taken), and opens
  the studio. A failure keeps the dialog, the prompt, and the error; a failed
  first message retries on the same session.
- `design-studio`: "Back to designs", the session's chat (full history) beside
  the deck preview, and "Open in session".
- `design-full`: the Preview | Full toggle hides the chat; it is kept in the URL
  as `view=full`. The viewer's own fullscreen button still presents.
- `design-waiting`: until the deck exists the preview shows "Waiting for the
  first slide" with the working indicator; after the turn ends without it,
  "The agent has not written `<path>` yet", with the chat still usable.
- `design-live`: the session's changed-files event refetches the open deck
  about a second after each write, and the deck refetches when the turn ends.
  The slide-decks skill writes the title slide first, then one `<section>` per
  edit, so every intermediate version renders.
- `design-url`: `?session=<id>&file=<path>` opens the studio, so a deck link
  reopens it; back from a card's studio returns to the landing.
- `design-phone`: under 768px the cards stack; the studio shows only the
  preview, a Chat button opens the chat full screen (`view=chat`), and Close
  returns to the preview.
- `design-loading`: each group shows a skeleton while its search runs; finished
  groups render without waiting for the rest.
- `design-empty`: no decks anywhere shows "No decks yet..." and the chips.
- `design-unavailable`: a workspace whose search returns 404 or 503 shows
  "Unavailable: open the session to start its runner" with a session link.
- `design-error`: any other search failure shows the error and a Retry button.
- `design-deck-error`: a deck that cannot be read shows the error in the
  preview next to "Open in session".
- `design-refresh`: Refresh reloads the session list, every search, the kit
  indicators, and the open deck; reads also refresh on mount and window focus.
- `design-flag-off`: no nav entry, and `/design` shows Page not found.

## How to get to it (user POV)

**Desktop web:** with the flag on, choose "Design" in the sidebar, or open
`/design` directly. Choose New design or a card.

**Desktop app:** the same sidebar entry and route inside the Electron shell.

**Phone (web or mobile app):** open the sidebar overlay and choose "Design".

**Deck link (any surface):** open `/design?session=<id>&file=<path>` (add
`&view=full` for the preview alone) to land in that deck's studio.

## Driving it with the repro environment

Preconditions: a running instance started with `OMNIGENT_FEATURES=design`
(`verify-env start`, then `verify-env doctor`), the built web UI, an online
host for New design, and at least one session whose workspace holds a
`*.slides.html` file. The E2E tests stub `/v1/info` themselves, so they do not
need the flag set.

- Flag off (no nav, Page not found):
  `tests/e2e_ui/design/test_design_page.py::test_design_page_is_absent_while_the_feature_is_off`.
- Desktop nav, card, studio with chat and preview, Full in the URL, and reload,
  with a real seeded deck:
  `tests/e2e_ui/design/test_design_page.py::test_design_page_opens_a_seeded_deck_in_the_studio`.
- Phone cards, unavailable group, "No kit", the studio preview, Chat and Close,
  browser back and forward, and Back to designs, against a stubbed API:
  `tests/e2e_ui/design/test_design_page.py::test_design_page_on_a_phone_shows_unavailable_and_returns_to_the_list`.
- New design from a chip: remembered folder, "Kit found", the create and
  first-message request bodies, and the waiting studio, against a stubbed API:
  `tests/e2e_ui/design/test_design_page.py::test_new_design_creates_a_session_and_opens_the_studio`.
- Landing, search, chips, and studio routing:
  `cd web && pnpm exec vitest run src/pages/DesignPage.test.tsx`. Dialog
  defaults, validation, collisions, and errors:
  `pnpm exec vitest run src/pages/design/NewDesignDialog.test.tsx`. Studio
  modes, waiting states, and refetches:
  `pnpm exec vitest run src/pages/design/DesignStudio.test.tsx`. Pure rules:
  `pnpm exec vitest run src/lib/designDecks.test.ts src/lib/designStudio.test.ts`.
- Nav and route gating: `cd web && pnpm exec vitest run src/shell/Sidebar.test.tsx src/App.test.tsx`.
- Desktop app: no test. Launch with `just electron-dev` against the instance,
  choose Design, open a card, and check the chat, preview, and Full toggle.
- Live build, by hand: choose New design with a real agent and host, create,
  and watch the preview go from "Waiting for the first slide" to one slide, then
  grow a slide at a time while the counter updates and the current slide stays.
- Kit applied, by hand: copy `examples/design-kits/sample` to
  `.omnigent/design-kit` in a deck's workspace, choose Refresh, and check the
  header shows the kit name and the deck uses the kit's colors and logo.
- Design system header badge and a branded deck in the studio (tokens, font,
  `ds:` logo), against a stubbed API serving the synthetic fixture:
  `tests/e2e_ui/design/test_design_page.py::test_design_system_brands_the_landing_and_the_deck`.
- New design with a recent design system: the field's options, the pointer
  write, and the first-message instruction, against a stubbed API:
  `tests/e2e_ui/design/test_design_page.py::test_new_design_with_a_design_system_writes_the_pointer`.
- Design system field and pointer write:
  `cd web && pnpm exec vitest run src/pages/design/NewDesignDialog.test.tsx`;
  header badges: `pnpm exec vitest run src/pages/DesignPage.test.tsx src/lib/designDeckApi.test.ts`.
- Design system, by hand: make a folder on the host with a `SKILL.md`, a
  `_ds_manifest.json` (`{"namespace": "My Brand"}`), a `colors_and_type.css`,
  and an `assets/logo.svg`. In New design choose "Choose folder", pick it,
  create, and check `.omnigent/design-system.json` appears in the design
  folder, the first message names the folder, the header reads "My Brand"
  with "Full", and the deck uses the tokens and the `ds:` logo. Pick a folder
  without either marker to see the rejection.
- Offline session, by hand: stop the runner of a session that has decks,
  choose Refresh, and check its group shows the Unavailable row and link.

## Gotchas

- Only the 50 most recent top-level, non-archived sessions are scanned; decks
  only in older sessions do not appear.
- Sessions without a workspace path are skipped. Several sessions in one
  workspace make one group, read through the most recent of them, so a card
  opens that session's studio.
- The project label applies only to sessions the viewer owns, as in the
  sidebar; a shared session's group uses its folder name.
- The indicator reads `kit.json` alone, so a kit whose assets are missing can
  show its name here and still show "Design kit not applied" in the viewer.
- Search returns at most 500 matches per workspace.
- The slug check uses the landing's decks for that folder plus the folder's
  `decks/` listing on the host; a deck elsewhere in the folder with the same
  name does not count.
- The changed-files event only arrives while the session's stream is bound;
  the studio keeps it bound even in Full mode and on the phone preview.
- The flag is read at page boot; reload after changing `OMNIGENT_FEATURES`.
- The host folder listing only names files, so a newly chosen design system
  shows its folder name in the dialog until create resolves the real name.
- Choosing None or "Folder kit" writes nothing, so a pointer left in that
  folder by an earlier design still applies.
- An external folder that only has a `kit.json` is not a design system; kits
  stay in the design's own `.omnigent/design-kit/`.
