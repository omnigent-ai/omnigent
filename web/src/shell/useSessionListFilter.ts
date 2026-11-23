// The sidebar's filters while the sidebar_saved_views flag is on. Off, they keep every session
// and never read or write the stored choice.

import { useCallback, useMemo, useRef, useState } from "react";

import { readSessionListFilter, writeSessionListFilter } from "@/lib/sessionListFilterPreferences";
import {
  DEFAULT_SESSION_LIST_FILTER,
  type SessionListFilter,
  sessionListFilterCount,
} from "@/lib/sessionListFilters";

export interface SessionListFilterState {
  /** Whether the menu offers the filters. */
  enabled: boolean;
  /** The effective filters: All while the flag is off. */
  value: SessionListFilter;
  /** How many filters narrow the list. */
  count: number;
  /** Changes the filters named in `patch`, keeping the others. */
  choose: (patch: Partial<SessionListFilter>) => void;
  /** Sets every filter back to All. */
  clear: () => void;
}

export function useSessionListFilter(enabled: boolean): SessionListFilterState {
  // The choice made in this view; until one is, the stored one, read once the flag is on.
  const [chosen, setChosen] = useState<SessionListFilter | null>(null);
  const value = useMemo(
    () => (!enabled ? DEFAULT_SESSION_LIST_FILTER : (chosen ?? readSessionListFilter())),
    [enabled, chosen],
  );
  // The latest filters, so two changes before a render both keep the other's.
  const latest = useRef(value);
  latest.current = value;
  const choose = useCallback(
    (patch: Partial<SessionListFilter>) => {
      if (!enabled) return;
      const next = { ...latest.current, ...patch };
      latest.current = next;
      setChosen(next);
      writeSessionListFilter(next);
    },
    [enabled],
  );
  const clear = useCallback(() => choose(DEFAULT_SESSION_LIST_FILTER), [choose]);
  return { enabled, value, count: sessionListFilterCount(value), choose, clear };
}
