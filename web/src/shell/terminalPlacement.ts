// Where each terminal of a session is shown, so none is attached twice. The main
// Terminal view comes first: its saved split owns its shells, and it publishes
// the panes it mounts now (such as a shell it shows on its own). The sidebar
// leaves all of those out as it renders, before any of them could attach.

import { useCallback, useId, useLayoutEffect, useMemo, useSyncExternalStore } from "react";

import { terminalLayoutKeys } from "@/lib/paneSplitLayout";

import { usePaneSplit } from "./paneSplitStore";

// Session → publishing view → the terminal keys it mounts.
const published = new Map<string, Map<string, readonly string[]>>();
const listeners = new Set<() => void>();
const snapshots = new Map<string, string>();

function changed(): void {
  snapshots.clear();
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The keys the main view mounts for ``conversationId``, joined so a snapshot compares by value. */
function publishedSnapshot(conversationId: string): string {
  let snapshot = snapshots.get(conversationId);
  if (snapshot === undefined) {
    const keys = new Set([...(published.get(conversationId)?.values() ?? [])].flat());
    snapshot = [...keys].sort().join("\n");
    snapshots.set(conversationId, snapshot);
  }
  return snapshot;
}

/** Tell the sidebar which terminals this main view mounts now; cleared when it unmounts. */
export function usePublishMainTerminalKeys(conversationId: string, keys: readonly string[]): void {
  const viewId = useId();
  const joined = keys.join("\n");
  // Before paint, so the sidebar drops a pane in the same frame this view takes it.
  useLayoutEffect(() => {
    const views = published.get(conversationId) ?? new Map<string, readonly string[]>();
    views.set(viewId, joined ? joined.split("\n") : []);
    published.set(conversationId, views);
    changed();
    return () => {
      views.delete(viewId);
      if (views.size === 0) published.delete(conversationId);
      changed();
    };
  }, [conversationId, viewId, joined]);
}

/**
 * The terminals the main view owns in ``conversationId``: those it mounts now, and
 * ``splitMembers``, its saved split's shells where it shows that split (`useMainSplitMembers`).
 */
export function useMainOwnedTerminalKeys(
  conversationId: string,
  { splitMembers }: { splitMembers: ReadonlySet<string> },
): ReadonlySet<string> {
  const getSnapshot = useCallback(() => publishedSnapshot(conversationId), [conversationId]);
  const mounted = useSyncExternalStore(subscribe, getSnapshot);
  return useMemo(
    () => new Set([...(mounted ? mounted.split("\n") : []), ...splitMembers]),
    [mounted, splitMembers],
  );
}

/** No shells, for a main view that shows no split. */
export const NO_MAIN_SPLIT_MEMBERS: ReadonlySet<string> = new Set();

/**
 * The shells of the Terminal view's saved split where that view shows splits, so the sidebar
 * leaves them out even while Chat shows. Where it can't, they keep their tabs.
 */
export function useMainSplitMembers(
  conversationId: string | null,
  splitsShown: boolean,
): ReadonlySet<string> {
  const split = usePaneSplit(conversationId ?? "");
  const joined =
    splitsShown && conversationId && split ? terminalLayoutKeys(split.layout).join("\n") : "";
  return useMemo(() => (joined ? new Set(joined.split("\n")) : NO_MAIN_SPLIT_MEMBERS), [joined]);
}

/** Forget every published view, for tests. */
export function resetTerminalPlacement(): void {
  published.clear();
  changed();
}
