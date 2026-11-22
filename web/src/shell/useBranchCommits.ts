// The session's branch commits for the comparison picker, read through the
// desktop app only while the picker can show them. They belong to the
// session's workspace, so a session moved to another one lists afresh.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import {
  BRANCH_COMMITS_KEY,
  type BranchCommitsView,
  branchCommitsView,
  fetchBranchCommits,
} from "@/lib/branchCommits";
import type { ComparisonWorkspace } from "@/lib/customComparison";

/** How long a listing is reused before the picker asks again. */
const COMMITS_STALE_MS = 30_000;

/**
 * The Commits section's view, and how to list again. Listed while `enabled`; a listing older than
 * the reuse window is read again each time the section is `shown`.
 */
export function useBranchCommits(
  sessionId: string,
  workspace: ComparisonWorkspace | null,
  { enabled, shown }: { enabled: boolean; shown: boolean },
): { view: BranchCommitsView; retry: () => void } {
  const query = useQuery({
    queryKey: [BRANCH_COMMITS_KEY, sessionId, workspace?.hostId ?? null, workspace?.root ?? null],
    queryFn: ({ signal }) => fetchBranchCommits(sessionId, signal),
    enabled: enabled && workspace !== null,
    staleTime: COMMITS_STALE_MS,
    retry: false,
  });
  // Only as the section appears: a listing that goes stale while shown waits for a refresh.
  const showing = enabled && shown && workspace !== null;
  const wasShowing = useRef(false);
  const { isStale, isFetching, refetch } = query;
  useEffect(() => {
    if (showing && !wasShowing.current && isStale && !isFetching) void refetch();
    wasShowing.current = showing;
  }, [showing, isStale, isFetching, refetch]);
  return {
    view: branchCommitsView(workspace ? query.data : undefined, query.error),
    retry: () => void query.refetch(),
  };
}
