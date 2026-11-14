// Per-PR Changes tab view state, kept while the app is open (across session
// switches) but not across reloads.

import { useMemo, useSyncExternalStore } from "react";

export interface FindState {
  open: boolean;
  query: string;
  matchCase: boolean;
  regex: boolean;
}

/** What the Changes tab shows: the PR's changed files, its commit list, or one commit's patch. */
export type ChangesMode = "changes" | "commits" | "commit";

interface ChangesView {
  pathFilter: string;
  hideViewed: boolean;
  /** The commit list's search, kept while the reader moves between commits and the list. */
  commitQuery: string;
  /** The Changes tab's source; `commit` needs `commitSha`. */
  mode: ChangesMode;
  commitSha: string | null;
  /** In `changes` mode, show only the changes after this PR commit, up to the head. */
  sinceSha: string | null;
  /** Files expanded temporarily (find, deep links), apart from the reader's collapse state. */
  revealedPaths: ReadonlySet<string>;
  shownEarlierCommentPaths: ReadonlySet<string>;
  find: FindState;
  /** The head whose "changed files lost their viewed marks" notice the reader dismissed. */
  dismissedResetHead: string | null;
}

export interface ChangesViewState extends ChangesView {
  setPathFilter: (query: string) => void;
  setCommitQuery: (query: string) => void;
  setHideViewed: (hide: boolean) => void;
  reveal: (path: string) => void;
  clearReveal: (path: string) => void;
  setEarlierCommentsShown: (path: string, shown: boolean) => void;
  setFind: (patch: Partial<FindState>) => void;
  setDismissedResetHead: (head: string | null) => void;
  /** Shows the changed files or the commit list, leaving any open commit. */
  setMode: (mode: "changes" | "commits") => void;
  openCommit: (commitSha: string) => void;
  setSinceSha: (sinceSha: string | null) => void;
}

const EMPTY_VIEW: ChangesView = {
  pathFilter: "",
  hideViewed: false,
  commitQuery: "",
  mode: "changes",
  commitSha: null,
  sinceSha: null,
  revealedPaths: new Set(),
  shownEarlierCommentPaths: new Set(),
  find: { open: false, query: "", matchCase: false, regex: false },
  dismissedResetHead: null,
};
// Never evicted: records are small and per PR; add eviction if this grows.
const views = new Map<string, ChangesView>();
const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function update(key: string, change: (view: ChangesView) => Partial<ChangesView> | null) {
  const view = views.get(key) ?? EMPTY_VIEW;
  const patch = change(view);
  if (!patch) return;
  views.set(key, { ...view, ...patch });
  for (const listener of listeners) listener();
}

function withRevealed(view: ChangesView, path: string, revealed: boolean) {
  if (view.revealedPaths.has(path) === revealed) return null;
  const revealedPaths = new Set(view.revealedPaths);
  if (revealed) revealedPaths.add(path);
  else revealedPaths.delete(path);
  return { revealedPaths };
}

export function useChangesViewState(conversationId: string, prUrl: string): ChangesViewState {
  const key = JSON.stringify([conversationId, prUrl]);
  const view = useSyncExternalStore(subscribe, () => views.get(key) ?? EMPTY_VIEW);
  // Actions depend only on the key, so callbacks built on them stay stable.
  const actions = useMemo<Omit<ChangesViewState, keyof ChangesView>>(
    () => ({
      setPathFilter: (pathFilter) => update(key, () => ({ pathFilter })),
      setCommitQuery: (commitQuery) => update(key, () => ({ commitQuery })),
      setHideViewed: (hideViewed) => update(key, () => ({ hideViewed })),
      reveal: (path) => update(key, (current) => withRevealed(current, path, true)),
      clearReveal: (path) => update(key, (current) => withRevealed(current, path, false)),
      setEarlierCommentsShown: (path, shown) =>
        update(key, (current) => {
          if (current.shownEarlierCommentPaths.has(path) === shown) return null;
          const shownEarlierCommentPaths = new Set(current.shownEarlierCommentPaths);
          if (shown) shownEarlierCommentPaths.add(path);
          else shownEarlierCommentPaths.delete(path);
          return { shownEarlierCommentPaths };
        }),
      setFind: (patch) => update(key, (current) => ({ find: { ...current.find, ...patch } })),
      setDismissedResetHead: (dismissedResetHead) => update(key, () => ({ dismissedResetHead })),
      setMode: (mode) => update(key, () => ({ mode, commitSha: null })),
      openCommit: (commitSha) => update(key, () => ({ mode: "commit", commitSha })),
      setSinceSha: (sinceSha) => update(key, () => ({ sinceSha })),
    }),
    [key],
  );
  return useMemo(() => ({ ...view, ...actions }), [view, actions]);
}
