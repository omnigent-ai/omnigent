// The commits of the comparison on screen, or of the session's branch, read only while the
// Changes review can show them. They belong to the session's workspace, so a session moved to
// another one lists afresh.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { useComparisonRepository } from "@/hooks/useComparisonOwner";
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
  // A review of a repository folder lists that repository's commits.
  const repository = useComparisonRepository();
  // The last range listed, which a commit shown on its own keeps within the same session,
  // workspace and folder; in another one it starts from the branch.
  const owner = `${sessionId}\0${workspace?.hostId ?? ""}\0${workspace?.root ?? ""}\0${repository}`;
  const lastRange = useRef<{ owner: string; range: RevisionSpec }>({ owner, range: BRANCH_RANGE });
  const previous = lastRange.current.owner === owner ? lastRange.current.range : BRANCH_RANGE;
  const range = commitsRange(custom, previous);
  useEffect(() => {
    lastRange.current = { owner, range };
  }, [owner, range]);
  const query = useQuery({
    queryKey: [
      BRANCH_COMMITS_KEY,
      sessionId,
      workspace?.hostId ?? null,
      workspace?.root ?? null,
      range.base,
      range.head,
      repository,
    ],
    queryFn: ({ signal }) => fetchBranchCommits(sessionId, range, signal, repository),
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
