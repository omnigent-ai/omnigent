# Dependency patches

`@tiptap/react@3.31.4` adapts the selection containment check from
https://github.com/ueberdosis/tiptap/pull/8436
(head `8a38167d0b74cc52834d61430d7c9c1e5e10958e`). Reading the DOM selection
while mounting every React node view forces repeated browser layout. Only the
node containing the editor selection needs to capture and restore it when
moving its content DOM. The source and both shipped JavaScript entry points
are patched; source maps remain upstream's.

Remove the selection hunks when upgrading to a TipTap release containing the fix.
Validate code-block editing and selection preservation, including read-only
selection, when changing it.

The same package patch also contains a locally authored renderer-registry
optimization. Upstream copies the entire registry on every registration and
removal, making a batch of node views quadratic. A mutable Map with a cached
immutable object snapshot makes writes constant-time and copies only when React
reads after a write. Published snapshots remain unchanged, repeated reads return
the same object, and subscriber notification timing is unchanged. Interleaving
every write with a read still requires a full snapshot per write.

Remove the registry hunks when upstream provides equivalent batching without
per-write registry copies. Validate snapshot identity, batched notifications,
and mounted code-block replacement, removal, and undo with
`TipTapRendererRegistry.test.tsx`, `TipTapNodeViewSelection.test.tsx`, and the
standard journeys in `feature-map/markdown-editor.md`.

## `@pierre/diffs@1.3.6`

Adds an engine-owned scroll signal to `CodeView`. `subscribeToScroll` listeners
get a third argument, `{ engineOwned }`, which is true when a scroll landed
exactly where the view's own scroll correction (`applyScrollFix`) put the DOM.
The engine records the actual, rounded position after a write that moved the
view; a write that didn't move it claims nothing, and `reset()` forgets it.
Both `dist/components/CodeView.js` and its `.d.ts` are patched; source maps
remain upstream's.

The rich diff review uses it to keep a reader's place when a preview's width
changes. Without it, the review told the view's own late corrections from
someone else's scroll (find in page, assistive technology, a script) only by
time, and on a slow machine it gave way to the view's own correction.

Remove the patch once `@pierre/diffs` ships an equivalent signal, and switch
`DiffReview` to it.
`engineScrollOwnership.test.ts` fails if a version bump drops the signal. Once
the diff review lands, `previewWidthAnchor.test.ts` also covers its use.

The same patch, marked `OMNIGENT-PATCH(missing-diff-row)`, turns a diff row with no line
on either side into a failure for that file. Upstream throws a bare message, and `FileDiff`
prints it with its stack inside the file and logs it on every render. Patched, the renderer
throws a `DiffMissingRowError` whose `missingRow` names the row's type and which side was
short, and `FileDiff` hands any render error to a new `onRenderError(error, fileDiff)` option
instead, when one is set. `CodeView` passes it through as a shared item callback. The review
shows a note in place of that file's diff and logs the failure's shape to the console.
Remove these hunks once upstream lets a caller own render errors.
`renderErrorHandling.test.ts` fails if a version bump drops them, and
`DiffReview.library.test.tsx` ("a file the library can't draw") covers the review's use.
