// The Files tree's name filter and include/exclude globs, remembered per session
// so they survive the tree remounting: a narrow page's tree popover closing after
// a pick, or the tree moving between docked and popover as the page resizes.

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import type { TreeFilterValues } from "./treeFilterSummary";

export const NO_TREE_FILTERS: TreeFilterValues = { nameContains: "", include: "", exclude: "" };

/** How long typing settles before the tree searches. */
export const TREE_FILTER_DEBOUNCE_MS = 300;

/** A session's filters, and whether trees should search with them at once (a clear). */
interface RememberedFilters {
  values: TreeFilterValues;
  immediate: boolean;
}

const UNFILTERED: RememberedFilters = { values: NO_TREE_FILTERS, immediate: true };
const rememberedFilters = new Map<string, RememberedFilters>();
const listeners = new Set<() => void>();

function recall(conversationId: string | undefined): RememberedFilters {
  return (conversationId && rememberedFilters.get(conversationId)) || UNFILTERED;
}

function remember(
  conversationId: string | undefined,
  values: TreeFilterValues,
  immediate: boolean,
): void {
  if (!conversationId) return;
  if (values.nameContains || values.include || values.exclude) {
    rememberedFilters.set(conversationId, { values, immediate });
  } else {
    rememberedFilters.delete(conversationId);
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Forgets every session's filters; for tests. */
export function forgetTreeFiltersForTests(): void {
  rememberedFilters.clear();
}

export interface TreeFilters {
  /** What the fields show. */
  edited: TreeFilterValues;
  /** What the tree searches with: the fields once typing settles. */
  applied: TreeFilterValues;
  change: (change: Partial<TreeFilterValues>) => void;
  /** Clears every filter at once, dropping any pending debounce. */
  clear: () => void;
  /** Leaves name search at once, keeping the globs. */
  clearNameContains: () => void;
}

/** The session's filters, shared by every tree that shows it, such as two mounted file tabs. */
export function useTreeFilters(conversationId: string | undefined): TreeFilters {
  const remembered = useSyncExternalStore(subscribe, () => recall(conversationId));
  const edited = remembered.values;
  // A mount, or a session switch, searches with the remembered filters at once.
  const [applied, setApplied] = useState(edited);
  const [appliedFor, setAppliedFor] = useState(conversationId);
  if (appliedFor !== conversationId) {
    setAppliedFor(conversationId);
    setApplied(edited);
  }
  useEffect(() => {
    if (remembered.immediate) {
      setApplied(edited);
      return;
    }
    const timer = setTimeout(() => setApplied(edited), TREE_FILTER_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [remembered, edited]);

  const change = useCallback(
    (fields: Partial<TreeFilterValues>) =>
      remember(conversationId, { ...recall(conversationId).values, ...fields }, false),
    [conversationId],
  );
  const clear = useCallback(() => {
    remember(conversationId, NO_TREE_FILTERS, true);
    setApplied(NO_TREE_FILTERS);
  }, [conversationId]);
  const clearNameContains = useCallback(() => {
    const next = { ...recall(conversationId).values, nameContains: "" };
    remember(conversationId, next, true);
    setApplied(next);
  }, [conversationId]);
  return { edited, applied, change, clear, clearNameContains };
}
