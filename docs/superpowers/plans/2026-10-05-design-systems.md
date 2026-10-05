# Design Page (Phase 3, Design systems) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a design point at a brand design system elsewhere on the user's
host. The New design dialog picks one and saves a pointer in the design's
folder; the agent follows it; the deck viewer applies it (tokens, fonts, and
`ds:` assets for a full system, a name only for a skill-only one).

**Spec:** `designs/DESIGN_PAGE.md`, "Phase 3: Design systems" plus "Phase 3
amendments". "Later phases" (server index, import, export, wireframes, user
or org kits, brand-rule warnings) are not built.

**Architecture:** Pure rules live in `web/src/lib/designSystem.ts` (kinds,
names, pointer, recents, agent instruction) and
`web/src/lib/designSystemInjection.ts` (`ds:` resolution, CSS processing, and
the kind-agnostic `injectDesignSystem` returning `{ style, content }`). The
viewer's branding decision (pointer, else kit) is `loadDeckBranding` in
`web/src/shell/deckBranding.ts`, with reads and the owner check injected, so
`SlidesViewer` only wires fetches and timers. No new server endpoints and no
new dependencies.

**Tech Stack:** React, TypeScript, TanStack Query, Vitest, Testing Library,
pytest.

## Existing paths this relies on (confirmed)

- **Pointer write:** `PUT /v1/sessions/{id}/resources/environments/default/filesystem/{path}`
  (server `routes_resources.py` `write_environment_file`, runner
  `resource_routes.py`), already used by `useWriteFileContent`. The body's
  `create_parents` defaults to true, so `.omnigent/` is created, and the server
  calls `ensure_runner_connected` for writes, so it works right after create.
  The web helper `writeFileContent` is exported for the dialog; no endpoint is
  added.
- **Absolute reads:** `fetchFileContent(session, "/abs/path")` sends
  `?base=host`; the server makes it owner-only, and a confined runner answers
  403 `path_unreachable` when no grant covers the folder. Both refusals are
  403, so the viewer checks ownership first with `getSessionSlim` (permission
  level) and treats an owner's 403 as "not readable from this session".
- **Host listing:** `useHostFilesystem` lists a folder (names only). It cannot
  read file content, so the dialog shows the folder name for a newly chosen
  folder and resolves the manifest `namespace` or `SKILL.md` `name` after
  create through the new session's absolute read (best effort).

## Global constraints

- No new dependencies, no new server endpoints, no new feature flag (same
  `design` flag).
- Never read, copy, or commit anything from the user's real design system;
  tests use the synthetic fixture under `web/src/test/fixtures/design-system/`.
- No U+2014 em-dash in code, comments, or UI strings. Short comments, no PR
  or issue references.
- Commit locally only; no push, no PR.

## Spec point to task map

| Spec point | Task |
| --- | --- |
| Kinds: detected from marker names; folder without markers rejected with "Not a design system: no SKILL.md or _ds_manifest.json"; an external `kit.json`-only folder is unsupported | 2 (rules), 6 (dialog) |
| Name: manifest `namespace`, else `SKILL.md` `name`, else folder name | 2 (rules), 6 (resolved after create) |
| Pointer `.omnigent/design-system.json` `{path, kind, name}`; relative `path` accepted (amendment) | 2 (parse), 5 (viewer), 6 (write) |
| Choosing one: None, folder kit, recents per host in localStorage, Choose folder | 2 (recents), 6 (field) |
| Write through the existing workspace write path | 6 |
| First message adds "Follow the design system at `<path>` (`<kind>`). Read its SKILL.md first." | 2 (text), 6 (sent) |
| slide-decks "Using a design system" section | 8 |
| Viewer: pointer replaces the kit; skill-only shows the name, no injection | 5 |
| Full: `colors_and_type.css`, `@import` stripped, `@font-face` sources inlined, `</style` rejected, injected before the deck's styles with no `!important`; `ds:` in `src`, `href`, CSS `url()` rewritten to data URIs | 3, 4, 5 |
| Kind-agnostic injection returning `{ style, content }` (amendment) | 3 |
| Limits: relative, confined, image or font extension, 2 MB per asset after encoding, 20 MB per deck, load timeout | 3 (caps), 5 (timeout) |
| Failure notice "Design system not applied: <reason>"; non-owner "Design system is only available to the session owner" | 5 |
| Sandbox refusal "Design system folder is not readable from this session; import it" (amendment) | 5 |
| Landing group headers show the design-system name and kind | 7 |
| Security: only CSS and data URIs injected, sandbox kept, owner-only absolute reads | 3, 5 |
| Tests: units, components, synthetic fixture | 2, 3, 5, 6, 7 (fixture in 3) |
| Docs: feature map | 9 |

## File map

| File | Responsibility |
| --- | --- |
| `web/src/lib/designSystem.ts` (+ test) | Pure: `detectDesignSystemKind`, `designSystemName`, `parseDesignSystemPointer`, `serializeDesignSystemPointer`, `designSystemInstruction`, recents per host. |
| `web/src/lib/designSystemInjection.ts` (+ test) | Pure: `resolveDsPath`, `processDesignSystemCss`, `rewriteDsReferences`, `injectDesignSystem(content, read)` with caps. |
| `web/src/test/fixtures/design-system/` | Synthetic full system: `_ds_manifest.json`, `SKILL.md`, `colors_and_type.css`, `fonts/fixture-sans.woff2` (stub), `assets/logo.svg`. |
| `web/src/shell/codeViewerHelpers.ts` | `prepareSlidesDoc` gains a style injected before the deck's own styles; exports the mime tables and data URI helper for reuse. |
| `web/src/shell/deckBranding.ts` (+ test) | `loadDeckBranding`: pointer, else kit; notices; owner and sandbox handling. |
| `web/src/shell/SlidesViewer.tsx` (+ test) | Wires branding: timeouts, asset read cache, badge, notice. |
| `web/src/lib/designStudio.ts` (+ test) | `firstDesignMessage` takes an optional design system. |
| `web/src/hooks/useWriteFileContent.ts` | Export `writeFileContent`. |
| `web/src/pages/design/NewDesignDialog.tsx` (+ test) | Design system field, name resolution, pointer write, recents. |
| `web/src/lib/designDeckApi.ts`, `designDecks.ts`, `pages/DesignPage.tsx` (+ tests) | Pointer-aware group indicator and badge. |
| `omnigent/resources/skills/slide-decks/SKILL.md`, `tests/tools/builtins/test_load_skill.py` | "Using a design system" section and its pin. |
| `feature-map/design-page.md`, `feature-map/slide-decks.md` | New sub-features and how to drive them. |

## Tasks

### Task 1: Plan

- [ ] Write this plan and commit it.

### Task 2: Design-system rules (`designSystem.ts`)

- [ ] Tests first: kind from markers (`_ds_manifest.json` wins, `SKILL.md`
  alone is skill, `kit.json` alone and nothing are `null`); name precedence
  and the 80-character cap; pointer parsing (valid absolute and relative,
  `..` segments, empty or non-string path, unknown kind, bad JSON, missing
  name falls back to the folder name); serialize round-trips; instruction
  text; recents per host (most recent first, deduped by path, capped at 5,
  corrupt storage ignored).
- [ ] Implement; `firstDesignMessage(prompt, path, system?)` appends the
  instruction (test in `designStudio.test.ts`).
- [ ] Commit.

### Task 3: Kind-agnostic injection (`designSystemInjection.ts`) and fixture

- [ ] Add the synthetic fixture.
- [ ] Tests first: `resolveDsPath` (relative only, no `.`/`..`, no absolute or
  scheme, allowed image or font extension); CSS processing (`@import`
  stripped, `@font-face` and other relative `url()` inlined, remote `url()`
  dropped, `data:` kept, `</style` rejected); `ds:` rewriting in `src`,
  `href`, and `url()`; per-asset 2 MB after encoding and 20 MB per deck
  (shared assets counted once); missing stylesheet means no style; a missing
  asset errors; `{ style, content }` from the fixture end to end.
- [ ] Implement; commit.

### Task 4: Head injection in `prepareSlidesDoc`

- [ ] Test first: a third argument lands right after the preview `<base>`,
  before the deck's own `<style>`, for a document with a head, without one,
  and a bare fragment; the kit style still lands before `</body>`.
- [ ] Implement; commit.

### Task 5: Viewer branding (`deckBranding.ts`, `SlidesViewer.tsx`)

- [ ] Tests first (`deckBranding.test.ts`): no pointer uses the kit; a
  pointer replaces the kit; skill-only gives a badge and no style; full
  injects and rewrites; invalid pointer and asset failures give
  "Design system not applied: <reason>"; a non-owner gets the owner notice
  without any absolute read; an owner's 403 gives the "not readable" notice;
  a relative path reads through the workspace with no owner check.
- [ ] Component tests (`SlidesViewer.test.tsx`): skill-only badge, full mode
  srcdoc (style before deck styles, data URIs), failure and non-owner notices,
  the design-system timeout.
- [ ] Implement; the kit timeout stays for pointer and kit reads, a longer
  design-system timeout starts once a full system is found; commit.

### Task 6: New design dialog field and pointer write

- [ ] Tests first: options (None, Folder kit when found, recents for the host,
  Choose folder); default (folder kit, else the host's most recent system,
  else None); choose folder accepts a full or skill folder and rejects one
  without markers with the spec message; create writes
  `.omnigent/design-system.json` with the resolved name through the workspace
  write, then sends the first message with the instruction; recents updated;
  a failed write keeps the dialog, prompt, and error; None and Folder kit
  write nothing.
- [ ] Export `writeFileContent`; implement; commit.

### Task 7: Landing badges

- [ ] Tests first: `fetchKitIndicator` returns the pointer's name and kind
  when it exists, "invalid" when it does not parse, else the kit as today;
  the group header shows the design-system name and kind.
- [ ] Implement; commit.

### Task 8: slide-decks skill

- [ ] Test first in `test_load_skill.py`: the section names the files to
  read, the template layout, dropping `<deck-stage>`, `ds:` references, no
  base64 copies, and the ignored folders.
- [ ] Write the section; commit.

### Task 9: Feature map and gates

- [ ] Update `feature-map/design-page.md` and `feature-map/slide-decks.md`.
- [ ] Run: `cd web && pnpm type-check && pnpm lint && pnpm test`;
  `uv run pytest tests/tools/builtins/test_load_skill.py tests/server/test_feature_flags.py tests/dev/test_verify_omnigent_feature_map.py`
  (and the skill injection tests); `pre-commit run --files <changed files>`.
- [ ] Verify in a real browser; commit.

## Not in this PR

Everything under "Later phases". Known follow-ups: choosing None or Folder kit
does not remove an existing pointer in that folder; an e2e test for the
design-system field.
