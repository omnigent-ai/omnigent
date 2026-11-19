# File tree

A virtualized file tree for React, used by the workspace sidebar's Files tree
and the diff review's file tree. It has no runtime dependencies besides React.

```tsx
const { model } = useFileTree({ paths, initialExpansion: "open", onSelectionChange });
return <FileTree model={model} className="file-tree" aria-label="Changed files" />;
```

`useFileTree` creates a `FileTreeModel` once; later option changes are ignored.
Drive the tree through the model instead: `getItem(path)` returns a handle to
select, expand, or collapse a row, `scrollToPath` reveals a row, `setSearch`
filters rows, and `addPaths` grows the tree, for example after a lazily listed
directory loads. Directory paths end with `/`.

Hosts theme the tree with the `--trees-*-override` custom properties listed at
the top of `file-tree.css`.

## Provenance

This is a reduced copy of `@pierre/trees` and its internal `@pierre/path-store`
package from [pierrecomputer/pierre](https://github.com/pierrecomputer/pierre)
at commit `cbc028ca863d6e662706dba87b50df3eeaebbfa7`, licensed under Apache 2.0
(`LICENSE.md`, `NOTICE.md`).

- `path-store/` is upstream's path store with unused features removed: move and
  remove mutations, asynchronous child loading, cleanup, prepared and presorted
  input, and benchmark instrumentation.
- `model.ts` is upstream's `FileTreeController`, without renaming, drag and
  drop, sticky folders, and mutation events. Search always hides rows that
  don't match.
- `FileTree.tsx` and `file-tree.css` replace upstream's Preact view, custom
  element, shadow DOM, and server rendering with a plain React component.
- `icons.ts` keeps only the colored "complete" icon set.

The files are formatted and linted with this repository's tools, which only
change their layout and type syntax, plus one renamed local variable.
