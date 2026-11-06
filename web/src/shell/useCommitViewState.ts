// The open commit's own Changes view state: its collapse and reveal sets. A commit starts with
// none, and opening a different commit replaces them. Nothing here is shared with the PR's view.

import { useCallback, useState } from "react";

const NONE: ReadonlySet<string> = new Set();

export interface CommitViewState {
  /** Files the reader collapsed in this commit. */
  collapsedPaths: ReadonlySet<string>;
  /** Files expanded temporarily in this commit, by find or a sidebar jump. */
  revealedPaths: ReadonlySet<string>;
  setCollapsedPaths: (update: (prev: ReadonlySet<string>) => ReadonlySet<string>) => void;
  reveal: (path: string) => void;
  clearReveal: (path: string) => void;
}

interface Slice {
  commitSha: string | null;
  collapsedPaths: ReadonlySet<string>;
  revealedPaths: ReadonlySet<string>;
}

const emptySlice = (commitSha: string | null): Slice => ({
  commitSha,
  collapsedPaths: NONE,
  revealedPaths: NONE,
});

/** The view state of the commit at `commitSha`, fresh whenever a different commit is opened. */
export function useCommitViewState(commitSha: string | null): CommitViewState {
  const [stored, setStored] = useState<Slice>(() => emptySlice(commitSha));
  // Opening another commit, or coming back to this one, starts it fresh: the stored state is
  // replaced during render, before anything reads it.
  if (stored.commitSha !== commitSha) setStored(emptySlice(commitSha));
  const current = stored.commitSha === commitSha ? stored : emptySlice(commitSha);
  const write = useCallback(
    (change: (slice: Slice) => Slice) =>
      setStored((prev) => change(prev.commitSha === commitSha ? prev : emptySlice(commitSha))),
    [commitSha],
  );
  const setCollapsedPaths = useCallback(
    (update: (prev: ReadonlySet<string>) => ReadonlySet<string>) =>
      write((slice) => ({ ...slice, collapsedPaths: update(slice.collapsedPaths) })),
    [write],
  );
  const reveal = useCallback(
    (path: string) =>
      write((slice) =>
        slice.revealedPaths.has(path)
          ? slice
          : { ...slice, revealedPaths: new Set(slice.revealedPaths).add(path) },
      ),
    [write],
  );
  const clearReveal = useCallback(
    (path: string) =>
      write((slice) => {
        if (!slice.revealedPaths.has(path)) return slice;
        const revealedPaths = new Set(slice.revealedPaths);
        revealedPaths.delete(path);
        return { ...slice, revealedPaths };
      }),
    [write],
  );
  return {
    collapsedPaths: current.collapsedPaths,
    revealedPaths: current.revealedPaths,
    setCollapsedPaths,
    reveal,
    clearReveal,
  };
}
