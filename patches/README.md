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
