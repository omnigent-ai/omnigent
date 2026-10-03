# Design Page (Phase 2, Studio) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `/design` into a studio: an Automations-style landing (header,
search, deck cards, suggestion chips), a New design dialog that creates a
session and sends the first message without leaving the page, and a studio
view that shows the compact chat beside a deck preview that updates while the
agent writes.

**Spec:** `designs/DESIGN_PAGE.md`, section "Phase 2: Studio" (the contract;
Phase 3 and Later phases are not built).

**Architecture:** Pure logic lives in `web/src/lib/designStudio.ts` (slug and
collisions, first message, URL state, last-used defaults, preview state) and
`designDecks.ts` (card search). `NewDesignDialog` reuses `AgentHarnessPicker`,
`WorkspacePickerDialog`, `useHosts`, `useHostFilesystem`, and the existing
`createSession` / `postEvent` requests. `DesignStudio` reuses `SideChatPane`
(with a new `fullHistory` prop), `SlidesViewer`, and the conversation registry
for turn state. The chat store's debounced changed-files invalidation also
invalidates `["design-deck", sessionId]`, so the open deck refetches about a
second after each write; the studio refetches again when the turn ends. No new
server endpoints and no new dependencies.

**Tech Stack:** React, TypeScript, TanStack Query, Tailwind CSS, Vitest,
Testing Library, pytest, Playwright.

## Global Constraints

- No new dependencies, no new server endpoints.
- Phase 1 data flow, cap, exclusions, and group states stay as they are.
- Pure helpers have no React and no fetch, and are unit tested.
- Accessible markup: cards are links, the Preview | Full toggle is a pair of
  `aria-pressed` buttons, dialogs have labelled fields, errors use `role="alert"`.
- No U+2014 em-dash in code, comments, or UI strings. Short comments.

## File Map

| File | Responsibility |
| --- | --- |
| `web/src/lib/designStudio.ts` | Pure: `deckSlug`, `designDeckPath`, `firstDesignMessage`, `readStudioParams` / `studioHref` (`session`, `file`, `view`), last-used agent, host, and folder per host in localStorage, `deckPreviewState`, `DESIGN_SUGGESTIONS`. |
| `web/src/lib/designStudio.test.ts` | Unit tests for the above. |
| `web/src/lib/designDecks.ts` | `filterDesignGroups(groups, query)` for the landing search. |
| `web/src/lib/designDecks.test.ts` | Search filter tests. |
| `omnigent/resources/skills/slide-decks/SKILL.md` | Workflow: complete document with the title slide first, then one complete `<section>` per edit. |
| `tests/tools/builtins/test_load_skill.py` | Pins the incremental-write instruction in the loaded skill. |
| `web/src/store/chatStore.ts` | Changed-files invalidation also invalidates `["design-deck", sessionId]`. |
| `web/src/store/chatStore.test.ts` | The debounced flush includes the deck key. |
| `web/src/components/chat/SideChatPane.tsx` | `fullHistory` prop turns off the forked-history filter. |
| `web/src/components/chat/SideChatPane.test.tsx` | Full history is shown with the prop. |
| `web/src/lib/sessionsApi.ts` | `createSession` options gain `hostId`, `workspace`, `labels`. |
| `web/src/lib/sessionsApi.test.ts` | Body carries `host_id`, `workspace`, `labels`. |
| `web/src/pages/design/NewDesignDialog.tsx` | The New design dialog. |
| `web/src/pages/design/NewDesignDialog.test.tsx` | Defaults, validation, create then send, slug collisions, error keeps the prompt, kit hint. |
| `web/src/pages/design/DesignStudio.tsx` | Studio view: header, Preview / Full, phone chat toggle, waiting states, live refetch. |
| `web/src/pages/design/DesignStudio.test.tsx` | Studio states and refetch behavior. |
| `web/src/pages/DesignPage.tsx` | Landing (header, search, cards, chips, dialog) and routing to the studio. |
| `web/src/pages/DesignPage.test.tsx` | Landing states, search, chips, cards open the studio, phone. |
| `tests/e2e_ui/design/test_design_page.py` | Updated phase 1 flows; New design creates and opens the studio; a card opens the studio. |
| `feature-map/design-page.md` | New sub-features and entry points. |

## Interfaces

```ts
// designStudio.ts
export const DESIGN_VIEW_PARAM = "view";
export type StudioView = "preview" | "full" | "chat";
export interface StudioParams { sessionId: string; path: string; view: StudioView }
export function readStudioParams(params: URLSearchParams): StudioParams | null;
export function studioHref(sessionId: string, path: string, view?: StudioView): string;
export const DECK_SLUG_MAX = 40;
export function deckSlug(prompt: string, taken: Iterable<string>): string;
export function designDeckPath(slug: string): string; // decks/<slug>.slides.html
export function firstDesignMessage(prompt: string, path: string): string;
export interface DesignDefaults { agentId?: string; hostId?: string; folders?: Record<string, string> }
export function readDesignDefaults(): DesignDefaults;
export function rememberDesignDefaults(agentId: string, hostId: string, folder: string): void;
export type DeckPreviewState = "loading" | "deck" | "waiting" | "not-written" | "error";
export function deckPreviewState(input: {
  file: "loading" | "ok" | "missing" | "error";
  turnEnded: boolean;
}): DeckPreviewState;
export const DESIGN_SUGGESTIONS: readonly { id: string; title: string }[];

// designDecks.ts
export function filterDesignGroups(groups: DesignGroup[], query: string): DesignGroup[];

// sessionsApi.ts (existing function, new optional fields)
createSession(agentId, [], { hostId?, workspace?, labels? });

// SideChatPane
<SideChatPane childId fullHistory? />
```

## Spec Coverage

| Spec bullet | Task |
| --- | --- |
| Landing: header, subtitle, Refresh, New design | 9 |
| Landing: search by deck name, workspace label, session title | 2 (pure), 9 |
| Landing: cards in a responsive grid under phase 1 headers; click opens studio; phase 1 data flow and states unchanged | 9 |
| Landing: suggestion chips under cards and in the empty state, prefill the dialog | 1 (list), 9 |
| Dialog: prompt required, multi-line | 7 |
| Dialog: agent picker defaulting to last design agent else web default | 1 (storage), 7 |
| Dialog: host and folder pickers, last folder per host, online owned hosts only | 1 (storage), 7 |
| Dialog: "Kit found" / "No kit" from listing `.omnigent/design-kit/` | 7 |
| Dialog: Create sends the New session create request, then the first message, stays on `/design`; failure keeps dialog, error, prompt | 6, 7 |
| Dialog: first message text and slug (kebab, 40 chars, `-2`, `-3` collisions) | 1, 7 |
| Dialog: after create, go to the studio URL | 1, 7, 9 |
| Studio: Back to designs, compact chat (full history) beside the preview | 5, 8 |
| Studio: Preview / Full toggle in the URL as `view=full`; viewer fullscreen kept | 1, 8 |
| Studio: phone preview, Chat button, close returns, Back to landing, URL drives state | 1, 8 |
| Studio: waiting and not-written states | 1, 8 |
| Studio: existing deck cards open the same studio | 8, 9 |
| Live preview: stream stays bound; changed-files event refetches the deck; refetch at turn end | 4, 8 |
| Live preview: growing content (already in `SlidesViewer`) | reused |
| Skill change: Workflow writes title slide first, one `<section>` per edit | 3 |
| Phase 2 tests (component, E2E) | 7, 8, 9, 10 |
| Feature map | 10 |

---

### Task 1: Pure studio helpers

**Files:** Create `web/src/lib/designStudio.ts`, `web/src/lib/designStudio.test.ts`.

- [ ] **Step 1: Failing tests.** Slug: kebab case, punctuation and accents
  dropped, words kept whole up to 40 characters, a single long word cut at 40,
  empty prompt gives `deck`, `-2` then `-3` when taken. Path and first message
  match the spec text exactly. URL: `readStudioParams` needs both `session` and
  `file`, unknown `view` is `preview`; `studioHref` omits `view=preview`.
  Defaults: round trip, per-host folders, broken storage reads as `{}`.
  Preview state: ok is `deck`; missing is `waiting` until `turnEnded`, then
  `not-written`; error and loading pass through.
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/lib/designStudio.test.ts`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(web): add Design studio helpers`.

### Task 2: Card search filter

**Files:** Modify `web/src/lib/designDecks.ts`, `web/src/lib/designDecks.test.ts`.

- [ ] **Step 1: Failing tests.** Empty query returns the groups unchanged;
  a query matches deck name, workspace label, or session title, case
  insensitive; a group whose label matches keeps all its decks; groups with no
  match, and non-ready groups, are dropped while searching.
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/lib/designDecks.test.ts`
- [ ] **Step 3: Implement `filterDesignGroups`.**
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(web): filter Design cards by name, workspace, or session`.

### Task 3: slide-decks Workflow writes incrementally

**Files:** Modify `omnigent/resources/skills/slide-decks/SKILL.md`, `tests/tools/builtins/test_load_skill.py`.

- [ ] **Step 1: Failing test.** The loaded framework skill tells agents to write
  a complete document with the title slide first and add one complete top-level
  `<section>` per edit, never leaving unclosed tags.
- [ ] **Step 2: RED.** `python -m pytest tests/tools/builtins/test_load_skill.py -q`
- [ ] **Step 3: Edit Workflow step 3.**
- [ ] **Step 4: GREEN**, plus `tests/e2e/omnigent/test_example_polly.py`.
- [ ] **Step 5: Commit** `feat(skills): write slide decks one slide per edit`.

### Task 4: Changed-files event refreshes the open deck

**Files:** Modify `web/src/store/chatStore.ts`, `web/src/store/chatStore.test.ts`.

- [ ] **Step 1: Failing test.** The existing coalescing test also expects
  `{ queryKey: ["design-deck", "conv_abc"] }` and six calls.
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/store/chatStore.test.ts -t "changed_files"`
- [ ] **Step 3: Implement** one invalidation in `scheduleWorkspaceFilesystemInvalidation`.
- [ ] **Step 4: GREEN** (whole suite).
- [ ] **Step 5: Commit** `feat(web): refresh the open Design deck on file changes`.

### Task 5: SideChatPane full history

**Files:** Modify `web/src/components/chat/SideChatPane.tsx`, `SideChatPane.test.tsx`.

- [ ] **Step 1: Failing test.** With `fullHistory`, blocks present at hydration
  stay visible; without it they are hidden (existing behavior).
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/components/chat/SideChatPane.test.tsx`
- [ ] **Step 3: Implement** `fullHistory` gating `filterHistory`.
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(web): let the side chat pane show full history`.

### Task 6: Session create with host and workspace

**Files:** Modify `web/src/lib/sessionsApi.ts`, `web/src/lib/sessionsApi.test.ts`.

- [ ] **Step 1: Failing test.** `createSession(id, [], { hostId, workspace, labels })`
  posts `host_id`, `workspace`, and `labels`; omitted options are absent.
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/lib/sessionsApi.test.ts -t createSession`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(web): create sessions on a host workspace from createSession`.

### Task 7: New design dialog

**Files:** Create `web/src/pages/design/NewDesignDialog.tsx`, `NewDesignDialog.test.tsx`.

**Interfaces:** `NewDesignDialog({ open, onOpenChange, initialPrompt?, takenDeckNames(folder): string[], onCreated(sessionId, path) })`.
Consumes `useAvailableAgents`, `selectableSessionAgents`, `AgentHarnessPicker`,
`useHosts` (online only; the list is owner-scoped server side),
`WorkspacePickerDialog`, `useHostFilesystem` for `<folder>/.omnigent/design-kit`
(kit hint) and `<folder>/decks` (collisions), `createSession`, `postEvent`,
`nativeWrapperLabelsForAgent`, `shouldGuardDialogDismiss`.

- [ ] **Step 1: Failing tests:** defaults (last agent and host and folder from
  storage, else first agent and first online host; offline hosts not offered);
  Create disabled until prompt, agent, host, and folder are set; Create posts
  the session then the first message with the slug path, remembers defaults,
  and calls `onCreated`; slug collisions from the landing names and the host
  `decks/` listing; a create failure keeps the dialog, shows the error, keeps the
  prompt; a send failure retries the send on the same session; kit hint.
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/pages/design/NewDesignDialog.test.tsx`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: GREEN**, then type-check.
- [ ] **Step 5: Commit** `feat(web): add the New design dialog`.

### Task 8: Studio view

**Files:** Create `web/src/pages/design/DesignStudio.tsx`, `DesignStudio.test.tsx`.

**Interfaces:** `DesignStudio({ sessionId, path, view, fresh, onView(view), onBack })`.
Reads the deck with `["design-deck", sessionId, path]`, binds the stream with
`ensureConversationStreamed`, reads turn state with `useConversationEntryState`,
renders `SideChatPane fullHistory`, `SlidesViewer`, `WorkingIndicator`.

- [ ] **Step 1: Failing tests:** desktop Preview shows chat and preview, Full
  hides the chat and the toggle calls `onView`; 404 while working shows
  "Waiting for the first slide"; after the turn ends it shows the not-written
  message and the chat stays; a hydrated idle session that is not fresh shows
  not-written; invalidating `["design-deck", id]` refetches; the turn ending
  refetches; phone shows the preview with a Chat button, `view=chat` shows the
  chat full screen with Close; Back calls `onBack`.
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/pages/design/DesignStudio.test.tsx`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Commit** `feat(web): add the Design studio view`.

### Task 9: Landing page

**Files:** Modify `web/src/pages/DesignPage.tsx`, `web/src/pages/DesignPage.test.tsx`.

- [ ] **Step 1: Failing tests:** header subtitle and New design; cards grouped
  under workspace headers with the kit badge; search filters and shows a
  no-match line; chips under cards and in the empty state open the dialog with
  the prompt; a card opens the studio URL; a studio URL renders the studio
  (mocked) for that session; `view=full` is passed through; phase 1 states
  (loading, unavailable, error and Retry, Refresh) still hold; phone Back
  returns to the landing.
- [ ] **Step 2: RED.** `pnpm --dir web exec vitest run src/pages/DesignPage.test.tsx`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: GREEN**, then type-check and lint.
- [ ] **Step 5: Commit** `feat(web): turn the Design page into a studio landing`.

### Task 10: Feature map and E2E

**Files:** Modify `tests/e2e_ui/design/test_design_page.py`, `feature-map/design-page.md`.

- [ ] **Step 1:** Update the phase 1 E2E flows to cards and the studio; add
  `test_new_design_creates_a_session_and_opens_the_studio` (stubbed agents,
  host, filesystem, session create, and event post; asserts the request bodies,
  the studio URL, the chat, and the waiting preview) and a card-opens-studio
  check.
- [ ] **Step 2:** Feature file: new sub-features (landing, search, chips,
  dialog, studio, Full, phone chat, waiting, live preview) and entry points.
- [ ] **Step 3: GREEN** `python -m pytest tests/dev/test_verify_omnigent_feature_map.py -q`;
  run the E2E file if the harness starts, else verify in a browser.
- [ ] **Step 4: Commit** `test(e2e): cover the Design studio and map it`.

### Final gates

```bash
pnpm --dir web type-check
pnpm --dir web lint
pnpm --dir web exec vitest run src/lib/designStudio.test.ts src/lib/designDecks.test.ts \
  src/store/chatStore.test.ts src/components/chat/SideChatPane.test.tsx src/lib/sessionsApi.test.ts \
  src/pages/design src/pages/DesignPage.test.tsx
python -m pytest tests/tools/builtins/test_load_skill.py tests/e2e/omnigent/test_example_polly.py \
  tests/dev/test_verify_omnigent_feature_map.py -q
pre-commit run --files <changed files>
```
