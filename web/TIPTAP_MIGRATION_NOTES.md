# TipTap Migration Notes

Migrated from Lexical to TipTap for the markdown rich-text editor
(`MarkdownRichTextViewer`). This file tracks known trade-offs and
follow-up work items.

---

## Why we migrated

The Lexical implementation had two lossy coordinate translations in series:

1. Lexical tree → Lexical-normalised markdown (soft-break collapsing,
   character escaping, table pipes) introduced systematic drift from the
   raw file content stored on the server.
2. Markdown offset → raw file offset via a ±200-char window search that
   failed whenever drift exceeded the window.

The result: comment highlights silently disappeared or anchored to the
wrong range.

TipTap's approach with ProseMirror Decorations fixes this:

- Decorations never touch the document → markdown serialisation is clean.
- `doc.textBetween(0, size, "\n")` is much closer to the raw file than
  Lexical's normalised markdown.
- ProseMirror positions are stable integers that remap automatically
  through `tr.mapping`, no bespoke inverse-offset code needed.

---

## Known downsides / follow-up items

### 1. `@tiptap/markdown` is in beta

**Status:** `@tiptap/markdown` is the official TipTap markdown extension
(part of the `ueberdosis/tiptap` monorepo, same version cadence as all
other `@tiptap/*` packages we use). It is marked **beta** by the team.

**Known gaps called out in the docs:**

- HTML comments are not supported
- Table cells allow only one child node per cell

**Mitigation:** Track their GitHub issues; it's the path-of-least-resistance
to patch since it's in the monorepo.

### 2. Markdown round-trip fidelity is imperfect

`tiptap-markdown` uses markdown-it for parsing and a custom serialiser
for export. It does not guarantee perfect idempotency:

- Setext-style headings (`Heading\n======`) → ATX (`# Heading`)
- Tight vs loose list spacing may normalise on first save
- HTML blocks embedded in markdown are stripped by default (`html` defaults to `false` in `@tiptap/markdown`)
- Thematic breaks (`***`, `- - -`) always serialise as `---`

**Impact:** First save after opening a file may produce minor whitespace
or syntax normalisation even without user edits. The baseline check
(`markdown === baselineRef.current`) prevents spurious dirty-flag triggers,
but a user who opens a file and immediately saves will write a normalised
version.

**Follow-up:** Test round-trip fidelity against real agent-generated
markdown files. Add a post-save diff warning if normalisation occurred.

### 3. Comment anchor search can still miss on duplicate content

The new implementation searches for `anchor_content` in the PM text
content near a scaled `start_index` hint (±500 chars window). If the
same text appears multiple times and the hint doesn't discriminate,
the first match is used.

This is better than the old ±200-char window but not immune.

**Follow-up (tracked):** Add a Tier-3 "context anchoring" strategy that
hashes surrounding context (e.g., previous paragraph heading) to
disambiguate identical phrases.

### 4. Table editing UX is limited

`@tiptap/extension-table` requires explicit row/cell add/delete commands
via the toolbar. The old Lexical implementation had the same limitation.

**Follow-up:** Add table toolbar controls (insert row, insert column,
delete row, merge cells).

### 5. Active comment highlight matched by offsets, not id

`buildDecorations` identifies the active comment by comparing
`activeSelection.start_index` / `end_index` against each comment's
stored offsets. Two comments on the same range would both receive
`md-comment-active`.

**Follow-up:** Add an optional `id` field to `ActiveSelection` and
populate it when activating a saved comment. The extension can then
prefer `id` matching when available, falling back to offset matching
for pending (unsaved) selections.

---

## Files removed

| File                               | Reason                                                                                                                                  |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `MarkdownEditorHelpers.tsx`        | Lexical-specific offset math (`$invertMarkdownOffset`, `computeLexicalMarkdownPointOffset`, `normalizeSoftBreaks`, custom table walker) |
| `MarkdownEditorHelpers.test.ts`    | Tests for the above                                                                                                                     |
| `MarkdownTableTransformer.ts`      | Custom Lexical table node + transformer                                                                                                 |
| `MarkdownTableTransformer.test.ts` | Tests for the above                                                                                                                     |
| `MarkdownTheme.ts`                 | Lexical CSS class theme — replaced by `index.css` prose rules                                                                           |

## Files added

| File                        | Purpose                                                                              |
| --------------------------- | ------------------------------------------------------------------------------------ |
| `TipTapEditorHelpers.ts`    | `findPmRangeForComment`, `computeSelectionData` — text-content ↔ PM position mapping |
| `TipTapCommentExtension.ts` | ProseMirror Plugin + TipTap Extension for Decoration-based comment highlights        |

## Files rewritten

| File                              | Changes                                                                        |
| --------------------------------- | ------------------------------------------------------------------------------ |
| `MarkdownCommentPlugin.tsx`       | No Lexical; uses TipTap `Editor`, dispatches rebuild transactions              |
| `MarkdownEditorToolbar.tsx`       | Replaced `useLexicalComposerContext` + dispatch commands with `editor.chain()` |
| `MarkdownRichTextViewer.tsx`      | Replaced `LexicalComposer` with `useEditor` / `EditorContent`                  |
| `MarkdownRichTextViewer.test.tsx` | Mocks TipTap modules instead of Lexical modules                                |
