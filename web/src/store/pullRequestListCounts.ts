import { create } from "zustand";

// How many pull requests each session's Pull requests list shows, for its tab's count pill.
// The list publishes the rows it shows, so the pill can't disagree with them.

interface PullRequestListCountsState {
  counts: Readonly<Record<string, number>>;
  publish: (sessionId: string, count: number) => void;
  /** The list is gone, so the count no longer follows its rows. */
  forget: (sessionId: string) => void;
}

export const usePullRequestListCounts = create<PullRequestListCountsState>((set) => ({
  counts: {},
  publish: (sessionId, count) =>
    set((state) =>
      state.counts[sessionId] === count
        ? state
        : { counts: { ...state.counts, [sessionId]: count } },
    ),
  forget: (sessionId) =>
    set((state) => {
      if (!(sessionId in state.counts)) return state;
      const { [sessionId]: _forgotten, ...rest } = state.counts;
      return { counts: rest };
    }),
}));

/** How many pull requests `sessionId`'s list shows, or undefined while no list is showing them. */
export function usePullRequestListCount(sessionId: string): number | undefined {
  return usePullRequestListCounts((state) => state.counts[sessionId]);
}
