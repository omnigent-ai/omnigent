// The commits of the comparison on screen, or of the session's branch, read only while the
// Changes review can show them. They belong to the session's workspace, so a session moved to
// another one lists afresh.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import {
  BRANCH_COMMITS_KEY,
  BRANCH_RANGE,
  type BranchCommitsView,
  branchCommitsView,
  commitsRange,
  fetchBranchCommits,
} from "@/lib/branchCommits";
import type { ComparisonWorkspace, RevisionSpec } from "@/lib/customComparison";

/** How long a listing is reused before the picker asks again. */
const COMMITS_STALE_MS = 30_000;

/**
 * The Commits section's view, and how to list again. Lists the `custom` comparison's commits, or
 * the branch's without one. Listed while `enabled`; a listing older than the reuse window is read
 * again each time the section is `shown`.
 */
export function useBranchCommits(
  sessionId: string,
  workspace: ComparisonWorkspace | null,
  custom: RevisionSpec | null,
  { enabled, shown }: { enabled: boolean; shown: boolean },
): { view: BranchCommitsView; retry: () => void } {
  // The last range listed, which a commit shown on its own keeps.
  const lastRange = useRef<RevisionSpec>(BRANCH_RANGE);
  const range = commitsRange(custom, lastRange.current);
  useEffect(() => {
    lastRange.current = range;
  }, [range]);
  const query = useQuery({
    queryKey: [
      BRANCH_COMMITS_KEY,
      sessionId,
      workspace?.hostId ?? null,
      workspace?.root ?? null,
      range.base,
      range.head,
    ],
    queryFn: ({ signal }) => fetchBranchCommits(sessionId, range, signal),
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
