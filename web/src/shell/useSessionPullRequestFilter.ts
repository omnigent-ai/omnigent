// The sidebar's pull request filter while the sidebar_saved_views flag is on. Off, it keeps
// every session and never reads or writes the stored choice.

import { useCallback, useMemo, useState } from "react";

import {
  readSessionPullRequestFilter,
  writeSessionPullRequestFilter,
} from "@/lib/sessionListFilterPreferences";
import type { SessionPullRequestFilter } from "@/lib/sessionListFilters";

export interface SessionPullRequestFilterState {
  /** Whether the menu offers the filter. */
  enabled: boolean;
  /** The effective filter: All while the flag is off. */
  value: SessionPullRequestFilter;
  choose: (value: SessionPullRequestFilter) => void;
}

export function useSessionPullRequestFilter(enabled: boolean): SessionPullRequestFilterState {
  // The choice made in this view; until one is, the stored one, read once the flag is on.
  const [chosen, setChosen] = useState<SessionPullRequestFilter | null>(null);
  const value = useMemo(
    () => (!enabled ? "all" : (chosen ?? readSessionPullRequestFilter())),
    [enabled, chosen],
  );
  const choose = useCallback(
    (next: SessionPullRequestFilter) => {
      if (!enabled) return;
      setChosen(next);
      writeSessionPullRequestFilter(next);
    },
    [enabled],
  );
  return { enabled, value, choose };
}
