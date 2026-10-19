# Dependency patches

`@tiptap/react@3.31.4` adapts the selection containment check from
https://github.com/ueberdosis/tiptap/pull/8436
(head `8a38167d0b74cc52834d61430d7c9c1e5e10958e`). Reading the DOM selection
while mounting every React node view forces repeated browser layout. Only the
node containing the editor selection needs to capture and restore it when
moving its content DOM. The source and both shipped JavaScript entry points
are patched; source maps remain upstream's.

Remove this patch when upgrading to a TipTap release containing the fix.
Validate code-block editing and selection preservation, including read-only
selection, when changing it.
