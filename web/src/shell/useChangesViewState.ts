// Per-PR Changes tab view state, kept while the app is open (across session
// switches) but not across reloads.

import { useMemo, useSyncExternalStore } from "react";

interface ChangesView {
  pathFilter: string;
  hideViewed: boolean;
  /** Files expanded temporarily (find, deep links), apart from the reader's collapse state. */
  revealedPaths: ReadonlySet<string>;
}

export interface ChangesViewState extends ChangesView {
  setPathFilter: (query: string) => void;
  setHideViewed: (hide: boolean) => void;
  reveal: (path: string) => void;
  clearReveal: (path: string) => void;
}

const EMPTY_VIEW: ChangesView = { pathFilter: "", hideViewed: false, revealedPaths: new Set() };
// ponytail: never evicted; a few small records per PR opened, add eviction if that grows.
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
  return useMemo<ChangesViewState>(
    () => ({
      ...view,
      setPathFilter: (pathFilter) => update(key, () => ({ pathFilter })),
      setHideViewed: (hideViewed) => update(key, () => ({ hideViewed })),
      reveal: (path) => update(key, (current) => withRevealed(current, path, true)),
      clearReveal: (path) => update(key, (current) => withRevealed(current, path, false)),
    }),
    [key, view],
  );
}
