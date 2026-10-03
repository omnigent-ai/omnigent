# Design Page (Phase 1, Gallery) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a flag-gated `/design` page that lists every `*.slides.html` deck
found in the workspaces of the 50 most recent sessions, grouped by workspace
with a kit indicator, and renders the selected deck in the existing deck viewer.

**Spec:** `designs/DESIGN_PAGE.md` (the contract; nothing it excludes is built).

**Architecture:** A pure list builder (`web/src/lib/designDecks.ts`) turns
session rows, projects, and per-workspace query results into render-ready
groups. A small API module (`web/src/lib/designDeckApi.ts`) wraps the existing
session-scoped `/search` and file-content reads and surfaces 404/503 as an
"unavailable" value instead of an empty list. `DesignPage` wires these to
`useCanvasSessions`, `useProjects`, `useQueries`, and `SlidesViewer`, keeping
the selection in `?session=&file=`. No server endpoints are added.

**Tech Stack:** React, TypeScript, TanStack Query, Tailwind CSS, Vitest,
Testing Library, FastAPI (flag only), pytest, Playwright.

## Global Constraints

- No new dependencies and no new server endpoints; every read is the existing
  session-scoped file API.
- No studio, wireframes, thumbnails, server index, or kit uploads.
- The list builder stays pure (no React, no fetch) and fully unit tested.
- Accessible markup: nav `aria-current="page"`, list rows are links reachable
  by keyboard, the selected row carries `aria-current="true"`.
- No U+2014 em-dash in code, comments, or UI strings. Short comments.

## File Map

| File | Responsibility |
| --- | --- |
| `omnigent/server/feature_flags.py` | `Feature.DESIGN` plus its `FeatureDefinition` (owner, review release). |
| `omnigent/server/app.py` | `design` in `_WEB_UI_ROUTE_PREFIXES` so a base path cannot shadow `/design`. |
| `designs/FEATURE_FLAGS.md` | Inventory row for `design`. |
| `tests/server/test_feature_flags.py` | Pins the default-off set and that `design` is frontend visible. |
| `tests/server/integration/test_utility_endpoints.py` | Pins `/v1/info` `features`. |
| `tests/server/integration/test_base_path.py` | `/design` base path is rejected. |
| `web/src/lib/capabilities.ts` | `"design"` in `FeatureKey`. |
| `web/src/shell/Sidebar.tsx` | Design nav link, active state, New session exclusion. |
| `web/src/shell/Sidebar.test.tsx` | Nav hidden while off; shown and active on `/design`; New session not lit. |
| `web/src/hooks/useWorkspaceChangedFiles.ts` | Export the raw `/search` request and response reader that the Files panel hook already uses. |
| `web/src/hooks/useWorkspaceChangedFiles.test.tsx` | The exported request builds the same URL; the hook's 404/503 mapping is unchanged. |
| `web/src/lib/designDecks.ts` | Pure: session cap, workspace dedupe, labels, path exclusions, kit indicator, group building, empty state. |
| `web/src/lib/designDecks.test.ts` | Unit tests for the builder. |
| `web/src/lib/designDeckApi.ts` | `fetchDeckSearch` (ok / unavailable / throws) and `fetchKitIndicator` (kit.json only). |
| `web/src/lib/designDeckApi.test.ts` | Status mapping and kit.json-only reads. |
| `web/src/pages/DesignPage.tsx` | The page: header, Refresh, grouped list, viewer pane, phone layout, URL state. |
| `web/src/pages/DesignPage.test.tsx` | Component states, URL selection, phone layout, kit indicator, Open in session. |
| `web/src/App.tsx` | Lazy `/design` route inside `FeatureGatedPage feature="design"`. |
| `web/src/App.test.tsx` | Route loading, off, and on. |
| `feature-map/design-page.md` | Feature recipe (four H2 sections, Preconditions, every entry point). |
| `feature-map/README.md` | Index line. |
| `tests/e2e_ui/design/__init__.py`, `tests/e2e_ui/design/test_design_page.py` | E2E: flag off hides nav and route; flag on lists a stubbed deck and renders it. |

## Spec Coverage

| Spec section | Task |
| --- | --- |
| Feature flag | 1 (server), 2 (web key) |
| Nav (link, hidden when off, highlight, New session exclusion) | 2 |
| Data flow 3 (search through the Files panel client, 404/503 surfaced) | 3 |
| Data flow 1, 2, 3 (exclusions), group header label, list row, Empty state rule | 4 |
| Data flow 4 (kit.json only), kit indicator | 4 (pure), 5 (fetch) |
| Data flow 5 (deck read through `fetchFileContent`, viewer with session id) | 6 |
| Desktop two-pane, phone layout, URL state, all States, Refresh, refetch on mount/focus | 6 |
| Flag off route behavior | 7 |
| Security (no new endpoints, sandboxed viewer, validated loader) | 3, 5, 6 reuse only existing APIs and `SlidesViewer` |
| Feature map, E2E | 8 |
| Testing (unit, component, server, E2E, manual) | 1 to 8, manual steps in Task 8's feature file |

---

### Task 1: Server `design` flag and route prefix

**Files:**
- Modify: `omnigent/server/feature_flags.py`, `omnigent/server/app.py`, `designs/FEATURE_FLAGS.md`
- Test: `tests/server/test_feature_flags.py`, `tests/server/integration/test_utility_endpoints.py`, `tests/server/integration/test_base_path.py`

**Interfaces:** produces `Feature.DESIGN == "design"`, `/v1/info` `features.design` (default `false`).

- [ ] **Step 1: Failing tests.** Add `"design": False` to both expected
  feature dicts; add `test_design_is_a_frontend_visible_feature` mirroring the
  canvas test; add `"/design"` to the reserved base-path parametrize list.
- [ ] **Step 2: Run, verify RED.**
  `python -m pytest tests/server/test_feature_flags.py tests/server/integration/test_utility_endpoints.py tests/server/integration/test_base_path.py -q`
- [ ] **Step 3: Implement.** `DESIGN = "design"` and a definition (owner
  `web`, review by `0.20.0`); add `"design"` to `_WEB_UI_ROUTE_PREFIXES`; add
  the inventory row.
- [ ] **Step 4: Run, verify GREEN** (same command), then `ruff check` and
  `ruff format --check` on the changed Python.
- [ ] **Step 5: Commit** `feat(server): add the design release flag`.

### Task 2: Web flag key and sidebar nav

**Files:**
- Modify: `web/src/lib/capabilities.ts`, `web/src/shell/Sidebar.tsx`
- Test: `web/src/shell/Sidebar.test.tsx`

**Interfaces:** `FeatureKey` gains `"design"`. Sidebar renders
`PrimaryNavLink to="/design" label="Design" testId="design-nav"` after Canvas
when `isFeatureEnabled(serverInfo, "design")`; `useActiveNavItem` returns
`isDesignPage` and excludes it from `isNewSessionRoute`.

- [ ] **Step 1: Failing tests.** "hides Design navigation while the release
  feature is off" and "renders and highlights the Design nav row without
  lighting New session" (href `/design`, `aria-current="page"`, active class,
  New session not active).
- [ ] **Step 2: Run, verify RED.** `pnpm --dir web exec vitest run src/shell/Sidebar.test.tsx`
- [ ] **Step 3: Implement** the key, the leaf check, the exclusion, and the link.
- [ ] **Step 4: GREEN**, then `pnpm --dir web type-check`.
- [ ] **Step 5: Commit** `feat(web): add Design navigation behind the design flag`.

### Task 3: Status-aware workspace search request

**Files:**
- Modify: `web/src/hooks/useWorkspaceChangedFiles.ts`
- Test: `web/src/hooks/useWorkspaceChangedFiles.test.tsx`

**Interfaces:** produces
`requestWorkspaceFileSearch(conversationId, { query, include?, exclude?, location? }): Promise<Response>`
and `readWorkspaceFileSearch(res, location?): Promise<WorkspaceFileSearchResult>`.
`useWorkspaceFileSearch` keeps its 404/503-to-empty behavior by composing them.

- [ ] **Step 1: Failing test.** `requestWorkspaceFileSearch` hits
  `.../search?limit=500&q=.slides.html&include=**%2F*.slides.html` and returns
  the raw 404 response (status visible to the caller).
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/hooks/useWorkspaceChangedFiles.test.tsx`
- [ ] **Step 3: Implement** by splitting `fetchWorkspaceFileSearch`.
- [ ] **Step 4: GREEN** (whole suite, so the hook's existing search tests still pass).
- [ ] **Step 5: Commit** `refactor(web): expose the raw workspace search request`.

### Task 4: Pure deck list builder

**Files:**
- Create: `web/src/lib/designDecks.ts`, `web/src/lib/designDecks.test.ts`

**Interfaces (produced):**
- `DESIGN_SESSION_CAP = 50`, `DECK_SUFFIX`, `DECK_SEARCH_QUERY`, `DECK_INCLUDE_GLOB`.
- `selectDesignWorkspaces(sessions, projects, viewerId, cap?): DesignWorkspace[]`:
  top-level non-archived, newest first, capped, deduped by workspace path (most
  recent session wins), label is the owned session's project name else the
  folder name; sessions without a workspace are skipped.
- `isDeckPath(path)`, `deckName(path)`.
- `kitIndicator(file: KitFile | null): KitIndicator` using `parseDesignKit`.
- `DeckSearchState = loading | ok(paths) | unavailable | error(message)`.
- `buildDesignGroups(workspaces, searches, kits): DesignGroup[]`: drops
  settled workspaces with no decks, keeps loading / unavailable / error groups,
  sorts decks by path, row title is the workspace session's display label.
- `isDesignListEmpty(groups, sessionsSettled)`.

- [ ] **Step 1: Failing tests:** dedupe, most recent session wins, archived and
  child sessions dropped, cap of 50 applied before dedupe, project vs folder
  label, `.worktrees/` and `node_modules/` exclusions, only `.slides.html`,
  deck name, unavailable and error groups kept, empty settled groups dropped,
  kit none / ok / invalid (bad JSON, binary, oversize), empty rule.
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/lib/designDecks.test.ts`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(web): build the Design page deck list`.

### Task 5: Deck search and kit indicator reads

**Files:**
- Create: `web/src/lib/designDeckApi.ts`, `web/src/lib/designDeckApi.test.ts`

**Interfaces:** consumes Task 3 and `fetchFileContent`; produces
`fetchDeckSearch(sessionId): Promise<{status:"ok", paths} | {status:"unavailable"}>`
(404 or 503 is unavailable, other failures throw) and
`fetchKitIndicator(sessionId): Promise<KitIndicator>` (reads only
`.omnigent/design-kit/kit.json`; 404 is "none", other read errors are invalid).

- [ ] **Step 1: Failing tests** for 200, 404, 503, 500, and that the kit read
  touches only `kit.json`.
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/lib/designDeckApi.test.ts`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(web): read decks and kit status for the Design page`.

### Task 6: DesignPage component

**Files:**
- Create: `web/src/pages/DesignPage.tsx`, `web/src/pages/DesignPage.test.tsx`

**Interfaces:** consumes `useCanvasSessions`, `useProjects`, `useViewerId`,
Tasks 4 and 5, `fetchFileContent`, `SlidesViewer`, `useIsMobileViewport`.
Query keys `["design-deck-search", sessionId]`, `["design-kit", sessionId]`,
`["design-deck", sessionId, path]`, each with `staleTime: 0` and
`refetchOnWindowFocus: true` (the app default is 30 s and off). URL:
`?session=<id>&file=<path>`; row links carry router state so the phone Back
control can pop history.

- [ ] **Step 1: Failing tests:** loading skeleton per group; a finished group
  renders while another loads; empty message; unavailable group with session
  link; error group with Retry that refetches; populated rows (name, relative
  path, session title); kit name / "No kit" link / "Kit invalid"; clicking a row
  sets the URL and renders `SlidesViewer` with the session id; deep link
  selects; "Open in session" href `/c/<id>?file=<path>`; deck read failure shows
  the error and the link; desktop hint; phone list only, then viewer only with
  Back returning to the list; Refresh refetches.
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/pages/DesignPage.test.tsx`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: GREEN**, then type-check and lint.
- [ ] **Step 5: Commit** `feat(web): add the Design page deck gallery`.

### Task 7: `/design` route behind the feature gate

**Files:**
- Modify: `web/src/App.tsx`, `web/src/App.test.tsx`

**Interfaces:** `withPageView("design", lazy(DesignPage))` under
`<FeatureGatedPage feature="design">` inside `AppShell`.

- [ ] **Step 1: Failing tests:** spinner while loading, not found while off,
  page inside the shell while on.
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/App.test.tsx`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(web): route /design behind the design flag`.

### Task 8: Feature map and E2E

**Files:**
- Create: `feature-map/design-page.md`, `tests/e2e_ui/design/__init__.py`, `tests/e2e_ui/design/test_design_page.py`
- Modify: `feature-map/README.md`

**Interfaces:** E2E stubs `**/v1/info` (flag), the session list, `/search`, and
file content, following `tests/e2e_ui/sessions/test_canvas_page.py`.

- [ ] **Step 1: Failing check.** Write the E2E test first and run
  `python -m pytest tests/dev/test_verify_omnigent_feature_map.py -q`: it fails
  because `tests/e2e_ui/design/` is neither mapped nor listed.
- [ ] **Step 2: Write the feature file** (H1, summary, four H2s, Preconditions,
  a test or manual steps per entry point: desktop web, desktop app, phone, deep
  link, flag off) and the index line.
- [ ] **Step 3: GREEN** for the feature-map test; run the E2E test if the
  harness can start here, else record why.
- [ ] **Step 4: Commit** `test(e2e): cover the Design page and map it`.

### Final gates

```bash
pnpm --dir web type-check
pnpm --dir web lint
pnpm --dir web exec vitest run src/lib/designDecks.test.ts src/lib/designDeckApi.test.ts \
  src/pages/DesignPage.test.tsx src/App.test.tsx src/shell/Sidebar.test.tsx \
  src/hooks/useWorkspaceChangedFiles.test.tsx src/lib/capabilities.test.ts
python -m pytest tests/server/test_feature_flags.py tests/server/integration/test_utility_endpoints.py \
  tests/server/integration/test_base_path.py tests/dev/test_verify_omnigent_feature_map.py -q
ruff check <changed .py> && ruff format --check <changed .py>
pre-commit run --files <changed files>
```
