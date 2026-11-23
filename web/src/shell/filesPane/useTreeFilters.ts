// The Files tree's name filter and include/exclude globs, remembered per session
// so they survive the tree remounting: a narrow page's tree popover closing after
// a pick, or the tree moving between docked and popover as the page resizes.

import { useCallback, useEffect, useRef, useState } from "react";

import type { TreeFilterValues } from "./treeFilterSummary";

export const NO_TREE_FILTERS: TreeFilterValues = { nameContains: "", include: "", exclude: "" };

/** How long typing settles before the tree searches. */
export const TREE_FILTER_DEBOUNCE_MS = 300;

const rememberedFilters = new Map<string, TreeFilterValues>();

function recall(conversationId: string | undefined): TreeFilterValues {
  return (conversationId && rememberedFilters.get(conversationId)) || NO_TREE_FILTERS;
}

function remember(conversationId: string | undefined, filters: TreeFilterValues): void {
  if (!conversationId) return;
  if (filters.nameContains || filters.include || filters.exclude) {
    rememberedFilters.set(conversationId, filters);
  } else {
    rememberedFilters.delete(conversationId);
  }
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

export function useTreeFilters(conversationId: string | undefined): TreeFilters {
  const [edited, setEdited] = useState(() => recall(conversationId));
  // A remount applies the remembered filters immediately rather than after the debounce.
  const [applied, setApplied] = useState(edited);
  // On an in-place session switch, show the new session's filters, never the previous ones.
  const filtersForRef = useRef(conversationId);
  useEffect(() => {
    if (filtersForRef.current === conversationId) return;
    filtersForRef.current = conversationId;
    const next = recall(conversationId);
    setEdited(next);
    setApplied(next);
  }, [conversationId]);
  useEffect(() => {
    const timer = setTimeout(() => setApplied(edited), TREE_FILTER_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [edited]);

  const change = useCallback(
    (fields: Partial<TreeFilterValues>) => {
      setEdited((previous) => {
        const next = { ...previous, ...fields };
        remember(conversationId, next);
        return next;
      });
    },
    [conversationId],
  );
  const clear = useCallback(() => {
    remember(conversationId, NO_TREE_FILTERS);
    setEdited(NO_TREE_FILTERS);
    setApplied(NO_TREE_FILTERS);
  }, [conversationId]);
  const clearNameContains = useCallback(() => {
    const withoutName = (filters: TreeFilterValues) => ({ ...filters, nameContains: "" });
    setEdited((previous) => {
      const next = withoutName(previous);
      remember(conversationId, next);
      return next;
    });
    setApplied(withoutName);
  }, [conversationId]);
  return { edited, applied, change, clear, clearNameContains };
}
