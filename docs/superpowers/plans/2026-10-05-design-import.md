# Design page: import a design system

Contract: `### Import a design system` under `## Later phases` in
`designs/DESIGN_PAGE.md`. Branch `polly/design-import`, on the server deck
index (which sits on design systems).

## Findings that shape the plan

- The workspace write route (`PUT .../filesystem/{path}` on the runner)
  encodes `content` with `encoding` and hands bytes to
  `CallerProcessFilesystem.write`, which decodes them as UTF-8 and sends text
  to the sandboxed helper's `write` op (`_write_impl` uses `write_text`). So
  base64 has to reach the helper and `_write_impl`, not just the route.
- The server PUT proxies the JSON body unchanged and maps a runner 4xx to the
  same status, so the server needs no change; a test pins the 400.
- PR 8's index paths (runner `publish_design_artifact_change`, server relay
  `persist_design_artifact`, store reconcile) all classify paths with
  `design_artifact_kind`; one guard there excludes `.omnigent/design-system/`.
- The viewer's `loadDeckBranding` already reads a relative pointer through the
  workspace read and only applies the owner check to absolute paths.
- PR 7 rules to reuse: `IMAGE_MIME`, `FONT_MIME`, `DS_ASSET_MAX_BYTES` (2 MB),
  `DS_DECK_MAX_BYTES` (20 MB). Templates and slides are HTML and CSS, so the
  copy also allows the text types the agent and viewer read: css, html, md,
  json. Scripts are never copied.

## Tasks (tests first, one commit each)

1. **Runner base64 write.** `_write_impl` and the helper `write` op take an
   optional `encoding` (`base64` writes bytes); `CallerProcessOSEnvironment.write`
   and `CallerProcessFilesystem.write` pass it; the route accepts
   `encoding: "base64"`, decodes with `validate=True`, returns 400
   `invalid_content` for bad base64 or an unknown encoding, and keeps
   recording design artifacts. Tests: valid binary bytes on disk, invalid
   base64 is 400 and writes nothing, default text unchanged, deck event still
   emitted; server PUT surfaces the runner 400.
2. **Index exclusion.** `design_artifact_kind` returns `None` for paths under
   `.omnigent/design-system/`. Tests: entity unit test (new file) and a runner
   write under the folder emits no event.
3. **Web write encoding, pointer, search.** `writeFileContent` takes an
   optional encoding; the pointer parses and serializes `imported_from`;
   `fetchDeckSearch` drops `.omnigent/design-system/` paths (this also keeps
   them out of the index reconcile). Tests for each, plus viewer tests for a
   relative pointer (workspace read, no owner check, non-owner gets branding).
4. **Import plan and copy** (`web/src/lib/designSystemImport.ts`): walk the
   source with the host filesystem API (only the allowlisted folders), build
   the plan (allowlist, extensions, 2 MB per file, 20 MB total, never-copied
   folders reported as skipped), copy four at a time with progress and
   per-file errors, write the pointer only when every file copied. Tests use
   a synthetic listing and the existing fixture.
5. **UI.** A shared import panel (confirm summary, progress, errors). New
   design dialog: "Import" next to a chosen absolute system shows the
   confirm step; Create copies after the session exists, then writes the
   relative pointer and sends the first message. Studio: owners of a design
   with an absolute pointer get "Import design system" in the header, which
   runs the same flow and reloads the preview. Component tests.
6. **Docs.** Feature map and slide-decks skill note for imported systems.

## Out of scope

Export, wireframes, user and org kits, brand rules, symlink detection,
managed sandboxes, removing files left over from an earlier import.
