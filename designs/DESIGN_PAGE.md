# Design page

A top-level "Design" entry in the left nav, next to Inbox and Canvas, where a
user sees every slide deck their agents produced across recent sessions and
renders any of them on brand, without first finding the right chat and opening
its Files panel.

It builds on the `*.slides.html` deck viewer and workspace design kits, in
three phases:

1. **Gallery** (below): find and open decks across sessions.
2. **Studio** ([Phase 2](#phase-2-studio)): ask for a deck from this page and
   watch it build live, refining it in a compact chat. Its landing page
   replaces the phase 1 two-pane layout.
3. **Design systems** ([Phase 3](#phase-3-design-systems)): point a design at
   a brand design system stored elsewhere on disk, from a single `SKILL.md`
   to a full exported design system.

## Goals

- One place to find and open decks across sessions, on desktop web, the
  desktop app, and phones.
- Decks render exactly as in the session file viewer, including the
  workspace design kit.
- No new server API: reuse the session-scoped file endpoints, so access
  control is unchanged.

## Non-goals (phase 1)

- Creating or editing designs from this page (phase 2).
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

## Phase 2: Studio

Ask for a deck from the Design page, watch the slides appear as the agent
writes them, and refine the deck in a compact chat beside the preview. The
page keeps the visual language of the Automations page (header with a primary
action, search, cards, suggestion chips, a create dialog) and adds a studio
view for one deck. Same `design` flag as phase 1.

### Landing

Replaces the phase 1 two-pane list.

- **Header:** "Design", the subtitle "Slides your agents made, on brand", a
  Refresh button, and a primary **New design** button.
- **Search:** filters cards by deck name, workspace label, or session title.
- **Cards:** one per deck, in a responsive grid grouped under the phase 1
  workspace headers (label plus kit or design-system badge). A card shows the
  deck name, its path, and the session title. Clicking it opens the studio for
  that deck. Phase 1 data flow, cap, exclusions, and states are unchanged.
- **Suggestion chips:** "Pitch deck from my notes", "Weekly status update",
  "Product launch". A chip opens New design with the prompt prefilled. Shown
  under the cards and in the empty state.

### New design dialog

Modeled on the "New automation" dialog.

- **Prompt** (required, multi-line).
- **Agent:** the New session agent picker, defaulting to the last agent used
  for a design, otherwise the web default (Claude Code).
- **Host and folder:** the New session workspace picker, defaulting to the
  last folder used for a design on that host (kept in localStorage). Only
  online hosts the user owns are offered. Under the folder, a hint shows "Kit
  found" or "No kit", from listing `.omnigent/design-kit/` with the host
  filesystem API.
- **Create** creates the session through the same session-create request the
  New session dialog sends (agent, host, workspace), then sends the first
  message, without leaving `/design`. On failure the dialog stays open with
  the error and the prompt kept.
- **First message:** the user's prompt followed by: "Use the slide-decks
  skill. Write the deck to `decks/<slug>.slides.html`. Write a complete
  document with only the title slide first, then add one complete slide per
  edit." The slug is the prompt's first words in kebab case, at most 40
  characters, with `-2`, `-3`, and so on if that workspace already lists a
  deck with that name.
- After create, the page goes to the studio at
  `/design?session=<id>&file=decks/<slug>.slides.html`.

### Studio view

- **Desktop, Preview mode:** a "Back to designs" link, then two columns: the
  compact chat (the existing side chat pane for that session: transcript plus
  composer, showing the full history) and the live deck preview (the existing
  deck viewer).
- **Full mode:** a Preview | Full toggle in the studio header, kept in the URL
  as `view=full`. Full hides the chat so the preview takes the whole page. The
  viewer's own fullscreen button stays for presenting.
- **Phone:** the preview fills the screen. A Chat button opens the compact
  chat full screen; closing it returns to the preview. Back returns to the
  landing. The URL drives every state, as in phase 1.
- **Waiting:** until the deck file exists, the preview shows "Waiting for the
  first slide" with the agent's working indicator. If the turn ends and the
  file still does not exist, it shows "The agent has not written
  `decks/<slug>.slides.html` yet" and the chat stays available.
- **Existing decks:** opening a card shows the same studio for that deck's
  session, so any deck can be refined.

### Live preview

- While the studio is open, the side chat pane keeps that session's stream
  bound. The existing changed-files event for the session (already debounced)
  also refetches the open deck's content, so the preview updates about a
  second after each write. It also refetches when the turn ends.
- The deck viewer already handles growing content: the counter updates, the
  current slide is kept, and the kit is reapplied.
- **Skill change:** the slide-decks skill's Workflow tells agents to write a
  complete document with the title slide first, then add one complete
  top-level `<section>` per edit, never leaving unclosed tags between edits.

### Phase 2 tests

- Component: landing (search, chips, cards, states), dialog (defaults,
  validation, create then send, slug collisions, error keeps the prompt),
  studio (Preview | Full in the URL, waiting states, refetch on the
  changed-files event, existing deck opens its session), phone layout.
- E2E: flag on, open New design, create with a seeded host, and assert the
  studio opens with the chat and the waiting preview; open an existing deck
  card. Runner-dependent steps may only run in CI.

## Phase 3: Design systems

Point a design at a brand design system stored elsewhere on the user's own
host. Importing a copy into the workspace is a later phase.

### Kinds

The kind is detected from marker files in the chosen folder, listed with the
host filesystem API.

| Kind | Detected by | Agent | Viewer |
|---|---|---|---|
| Kit | `.omnigent/design-kit/kit.json` in the design's own folder | follows the kit (today's skill) | enforces it (today's behavior) |
| Skill-only | the folder has a `SKILL.md` and no `_ds_manifest.json` | reads and follows `SKILL.md` | enforces nothing; toolbar shows the name |
| Full | the folder has `_ds_manifest.json` | reads `SKILL.md`, `README.md`, the manifest, and one template | injects tokens and fonts and resolves `ds:` assets, without forcing colors |

A folder with none of these markers is rejected in the picker with "Not a
design system: no SKILL.md or _ds_manifest.json". A kit stays in the design's
own folder; an external folder that only has a `kit.json` is not supported
until import exists.

### Choosing one

- The New design dialog gets a **Design system** field: None, the folder's
  kit (when found), recent design systems for that host, or "Choose folder"
  (the same host folder picker). Recents are kept in localStorage per host.
- The name shown is the manifest `namespace`, otherwise the `SKILL.md`
  frontmatter `name`, otherwise the folder name.
- After the session is created, the page writes
  `.omnigent/design-system.json` in the design's folder with
  `{"path": "<absolute folder>", "kind": "full" | "skill", "name": "<name>"}`
  through the existing workspace file write path, so no new server endpoint
  is needed.

### Agent instructions

- The first message adds: "Follow the design system at `<path>` (`<kind>`).
  Read its SKILL.md first."
- The slide-decks skill gains a **Using a design system** section, so it
  applies outside the studio too:
  - Read `SKILL.md`; for a full design system also read `README.md`,
    `_ds_manifest.json`, and the one template under `templates/` that fits.
  - Copy that template's layout CSS into the deck; drop `<deck-stage>` and the
    template's loader scripts; put sections directly in `<body>`; do not
    position sections absolutely.
  - Reference fonts and images as `ds:<path relative to the design system>`
    (for example `ds:assets/brand/databricks-logo-white.svg`). Never embed
    base64 copies of design-system files.
  - Ignore `uploads/`, `ui_kits/`, and `preview/`.

### Viewer behavior

- When `.omnigent/design-system.json` exists in the deck's workspace, it
  replaces the kit for that deck. Without it, phase 1 kit behavior applies.
- **Skill-only:** no injection; the toolbar shows "Design system: <name>".
- **Full:** the viewer reads the system's `colors_and_type.css` and the files
  it references through the session file API with an absolute path (session
  owner only). It strips `@import` rules, inlines `@font-face` sources as
  data URIs, rejects CSS that contains `</style`, and injects the result
  before the deck's own styles, with no `!important`. It then rewrites every
  `ds:` reference in `src`, `href`, and CSS `url()` to a data URI.
- **Limits:** `ds:` paths must be relative, stay inside the design-system
  folder, and use an allowed image or font extension. Each asset is capped at
  2 MB after encoding, and all design-system assets for one deck at 20 MB.
  Loading has a timeout like the kit's.
- **Failure:** any error renders the deck without the design system and shows
  "Design system not applied: <reason>". A viewer who is not the session
  owner gets "Design system is only available to the session owner".
- **Group headers** on the landing show the design-system name and kind in
  place of the kit badge.

### Security

The design-system folder is untrusted input chosen by the user. The viewer
only injects CSS and data URIs from it, never scripts, and keeps the existing
sandbox. Absolute reads go through the existing owner-only file API, so no new
access is granted. The agent treats the design system's files as guidance
from the user's chosen source.

### Phase 3 tests

- Unit: kind detection by markers, pointer parsing and validation, `ds:`
  resolution (confinement, extensions, per-asset and per-deck caps), CSS
  processing (`@import` stripped, `@font-face` inlined, `</style` rejected).
- Component: dialog design-system field (recents, choose folder, rejection
  message), viewer skill-only and full modes, failure and non-owner notices,
  landing badges.
- Fixture: a small synthetic full design system in the test tree (manifest,
  CSS, one font stub, one SVG), never the user's real folder.

### Phase 3 amendments

- Full-mode reads of an outside folder only succeed for sessions without a
  sandbox or with a read grant for that folder. When the read is refused,
  the viewer shows "Design system folder is not readable from this session;
  import it" instead of failing silently.
- The pointer accepts a relative `path` from the start (import adds the
  writer).
- Design-system injection is kind-agnostic (tokens, fonts, and `ds:`
  rewriting, no slide section rules) and returns `{ style, content }`, so
  wireframes and the standalone export reuse it.

## Later phases

Planned for the same `design` flag, including the new server surface listed
in the build order. Each item is its own PR, in the build order
at the end of this section.

### Server deck index

Lists every deck (and wireframe) across sessions from the database, so the
page stops scanning recent sessions and covers older sessions and offline
runners.

- **Table `design_artifacts`** (one alembic revision): `workspace_id`,
  `session_id`, `path`, `kind` (deck or wireframe), `updated_at`, `deleted`.
  Primary key `(workspace_id, session_id, path)`, index
  `(workspace_id, session_id)`, no database foreign key (same rule as other
  session-scoped tables). Store methods live on the conversation store with
  named sessions such as `record_design_artifact` and `list_design_artifacts`.
- **Recording:** wherever the runner already records a file change with a
  path (`sys_os_write`, `sys_os_edit`, the file PUT/PATCH/DELETE routes, and
  native harness file hooks), a path ending in `.slides.html` or
  `.wireframe.html` also emits a `session.design_artifact.changed` event. The
  server relay upserts the row off the event loop, the same way the pending
  approval count is persisted.
- **Gap and reconcile:** shell writes and changes made outside the agent are
  not recorded. When the page's existing per-workspace scan succeeds, it
  sends the full path set for that session to
  `PUT /v1/sessions/{id}/design-artifacts`, which replaces that session's
  rows. An optional `kind` in the body limits the replace to that kind, so
  the deck-only scan sends `kind: "deck"` and leaves wireframe rows alone.
  Opening a deck that returns 404 marks its row deleted.
- **List endpoint:** `GET /v1/design/artifacts?kind=deck|wireframe` returns
  session id, path, kind, updated time, session title, and workspace for
  sessions the caller can see, using the session list's visibility rules.
  Behind the `design` flag. Opening a deck still reads through the existing
  session file API, so file access rules are unchanged.
- **Web:** the landing calls the index first and falls back to the phase 1
  scan when the server does not have it. The scan then only reconciles
  sessions with a live runner or host. Offline sessions are listed from the
  index and marked "Unavailable" until their runner is back.
- **Tests:** store and migration, recording from each write path, the list
  endpoint's visibility (owner, shared, not shared), reconcile replace and
  404 delete, and the web fallback.

### Import a design system

Copy a design system's slide-relevant files into the design's workspace, so
it renders for collaborators, survives the original folder moving, and does
not need outside-folder reads.

- **Who copies:** the web client, through existing routes. It lists the
  source with the host filesystem API, reads each file through the design
  session's absolute read (owner only), and writes it with the workspace
  write route to `.omnigent/design-system/<relative path>`.
- **Runner fix:** the workspace write route's `encoding` field accepts
  `base64` so fonts and images can be written (reads already return base64
  for binary files). The web write helper passes the encoding. This changes an
  existing contract and is called out for review.
- **What is copied:** only `SKILL.md`, `README.md`, `_ds_manifest.json`,
  `colors_and_type.css`, `fonts/`, `assets/`, `templates/`, and `slides/`, with
  allowed extensions, skipping files over 2 MB and capping the total at 20 MB.
  `uploads/`, `ui_kits/`, and `preview/` are never copied.
- **Flow:** "Import" in the New design dialog's design-system field (and on a
  pointed design) shows a confirm step with the file count, total size, and
  skipped files; copies about four files at a time with progress and per-file
  errors; and writes the pointer last, so a half-finished import is never
  used.
- **Pointer:** `.omnigent/design-system.json` gets
  `"path": ".omnigent/design-system"` plus `"imported_from": "<absolute>"`. A
  relative path means imported: reads use the normal workspace file read
  (read access, not owner only), the owner-only notice is skipped, and
  collaborators with shared workspace files see the branding. Deck and
  wireframe search exclude `.omnigent/design-system/`.
- **Known limit:** the host and runner listings resolve symlinks, so a
  symlink inside the source cannot be detected and would be copied as its
  target. The allowlist, extension filter, caps, and owner-only confirm step
  narrow this; surfacing symlinks in listings is a separate decision.
- **Managed sandboxes** are out of scope: they cannot see a laptop folder.
  Committing `.omnigent/design-system/` to the repo (about 20 MB) is the
  workaround.
- **Tests:** the runner base64 write, the import plan (allowlist, extension
  filter, caps, skip list), copy progress and errors, pointer written last,
  relative pointer handling in the viewer, and the search exclusion.

### Standalone HTML export

A **Download HTML** button in the deck toolbar, next to Print, saves one
self-contained file that opens in any browser.

- **Built by** a helper next to `prepareSlidesDoc` that takes the deck and
  exactly the style the viewer rendered (kit style, or the design-system
  style with `ds:` references already rewritten), so the file matches the
  preview and the same size caps hold. Phase 3's loader therefore returns
  `{ style, content }` with content already rewritten, not only a srcDoc.
- **Contents:** the deck, the print rules (one slide per landscape page),
  the injected style, and a small standalone navigation script: arrow keys,
  PageUp, PageDown, Space, click, and `#n` in the URL, ignoring keys typed in
  form fields. The "show one slide" rule applies only once that script runs,
  so with JavaScript off the file reads as a stacked, scrolling document.
- **Left out:** the viewer's frame script and its message protocol, external
  `@import` rules, and any design-system loader scripts.
- **Disabled** when the content is truncated, or when the kit or design system
  did not load (error or non-owner notice), so no file ships with unresolved
  assets.
- **Saved** with the existing browser download helper as `<deck>.html`. The
  app never navigates to the generated document.
- **Deck's own references:** `@import` rules and `<script src>` the deck itself
  contains are kept as written, so the file matches the preview; such decks
  still need the network. Links keep the preview's `<base target="_blank">`,
  and slides fill the window rather than a scaled 16:9 stage.
- **Tests:** no frame script in the output, style injected, the stacked
  fallback without the script, navigation keys, and the disabled states.

### Wireframes

`*.wireframe.html` files open in a wireframe viewer with device frames and
appear on the Design page next to decks.

- **Viewer:** a lazy `WireframeViewer` routed by file name next to the deck
  viewer. A device picker offers Desktop 1440x900, Tablet 834x1194, and Phone
  390x844. The frame is rendered at the device size, so the wireframe's media
  queries respond to it, then scaled to fit, and it scrolls inside the frame.
  It reuses the sandbox, the kit and design-system loading gate, and
  fullscreen.
- **Screens:** each top-level `<section data-screen="id" data-title="...">`
  is a screen, chosen with a screen picker. A file with no sections is one
  screen. Links to `#id` and elements with `data-goto="id"` switch screens
  inside the frame (the preview's `<base target="_blank">` would otherwise
  open a new tab).
- **Branding:** wireframes get only fonts and tokens from a kit or design
  system, not the deck section rules (no forced section background, no logo
  on every screen). The kit style builder gains a flag for this, and Phase 3
  injection is built kind-agnostic so wireframes reuse it.
- **Design page:** search includes `**/*.wireframe.html`; items carry a kind
  (slides or wireframe) with a badge on cards; the studio picks the viewer by
  kind; the New design dialog gets a Slides | Wireframe toggle that writes to
  `wireframes/<slug>.wireframe.html` and names the right skill.
- **Skill:** a separate framework skill, `wireframes` (screens, `data-goto`
  links, responsive CSS, grayscale first), shipped like slide-decks. The
  "Using a design system" text lives in one shared place both skills point
  to.
- **Tests:** routing, device sizes and scaling, screen picker and links,
  branding without section rules, Design page kind handling, and the dialog
  toggle.

### Kits per user or organization

- **User default:** one per-user preference (key `design_default`) holding a
  design-system reference (`host_id`, `path`, `name`, kind) or "none", read
  and written with `GET` and `PUT /v1/me/preferences/design-default`, modeled
  on the existing project order preference. The New design dialog offers
  "Make this my default"; the stored default replaces the localStorage
  default from phase 3 (recents stay local).
- **Org kit:** an operator sets `design_kit: true` in the server config file
  passed with `omnigent server -c <file>` and puts the kit in a `design-kit/`
  folder next to that file: a `kit.json` with a `name`, plus its assets. The
  value is only `true` and the folder is fixed, so a path such as `.` can never
  expose the config file. Like branding, the setting is read only from a `-c`
  config file. It is validated
  at startup with the branding asset checks (no path escape, no symlinks),
  2 MB per asset and 20 MB total, advertised in `GET /v1/info` as
  `design_kit: {name} | null`, and served to signed-in users from
  `GET /v1/design-kit/{path}`. Config ships with each replica, so this works
  on multi-replica and Databricks-hosted servers. An admin upload API is out
  of scope until operators need it.
- **Materialization:** when a design is created with the org kit, the page
  copies the kit into the design folder's `.omnigent/design-kit/` through the
  workspace write path. The agent and the viewer then see a normal workspace
  kit, so decks render the same for every viewer.
- **Precedence, applied once in the New design dialog:** the explicit
  choice, then the folder's existing kit, then the user default, then the org
  kit, then none. At render time the phase 3 rule is unchanged.
- **Tests:** preference endpoints, config validation (escape, symlink, size),
  the asset endpoint's auth, `/v1/info` advertising, dialog precedence, and
  materialization.

### Brand-rule warnings

For full design systems only, the viewer flags raw hex colors, raw pixel
sizes, and font families outside the system's allow-list.

- **Rules and allow-list** come from the system's `_adherence.oxlintrc.json`:
  the three patterns in its `no-restricted-syntax` rule and the token names
  and `fontFamilies` in its `x-omelette` block. Omnigent does not run oxlint;
  it reuses the patterns on CSS.
- **Scope:** a pure function scans only `<style>` text and `style`
  attributes, parsed inertly, never slide text. It runs once per content.
- **Avoiding noise:** ignore custom property definitions, `var()` fallbacks,
  `0px`, and `1px` borders and outlines; allow every property and value pair
  that appears in the system's own `templates/*` CSS, so template CSS copied
  verbatim is clean; count each distinct value once.
- **UI:** a toolbar badge "N brand warnings" that opens a list of the value,
  the property, and where it first appears. It never blocks rendering.
  Kit decks and skill-only systems have no allow-list, so they show no badge.
- **Skill:** the "Using a design system" section tells agents to use the
  system's tokens and listed fonts.
- **Tests:** each rule, each noise rule, the template baseline, and no badge
  outside full mode.

### Build order

Two builders are available, so later work runs on two tracks, each PR
reviewed by the other builder:

| Order | PR | Depends on | New server surface |
|---|---|---|---|
| 6 | Studio (Phase 2) | PR 5 | none |
| 7 | Design systems (Phase 3, with the amendments) | PR 6 | none |
| 8 | Server deck index | PR 6 | `design_artifacts` table and migration, `GET /v1/design/artifacts`, `PUT /v1/sessions/{id}/design-artifacts`, a runner event |
| 9 | Import | PR 7 | runner write accepts base64 |
| 10 | Standalone HTML export | PR 7 | none |
| 11 | Wireframes | PR 7 | none (new `wireframes` framework skill) |
| 12 | Kits per user or organization | PR 9 | `GET`/`PUT /v1/me/preferences/design-default`, `GET /v1/design-kit/{path}`, `design_kit:` server config |
| 13 | Brand-rule warnings | PR 7 | none |

PR 8 runs in parallel with PR 7 on its own branch from PR 6; the two are
rebased into one line before certification.
