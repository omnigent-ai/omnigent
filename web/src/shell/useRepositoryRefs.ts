// Branch and tag names the comparison picker suggests for Base and Head, read
// through the desktop app only while the picker can use them. They belong to
// the session's workspace, so a session moved to another one never suggests
// the previous repository's names.

import { useQuery } from "@tanstack/react-query";

import { useComparisonRepository } from "@/hooks/useComparisonOwner";
import type { ComparisonWorkspace } from "@/lib/customComparison";
import { fetchRepositoryRefs, type RepositoryRefs } from "@/lib/workingCopyComparison";

export const REPOSITORY_REFS_KEY = "repository-refs";

/** How long listed names are reused before the picker asks again. */
const REFS_STALE_MS = 30_000;

/** The workspace's branch and tag names; undefined until listed, or when they can't be. */
export function useRepositoryRefs(
  sessionId: string,
  workspace: ComparisonWorkspace | null,
  { enabled }: { enabled: boolean },
): RepositoryRefs | undefined {
  // A review of a repository folder suggests that repository's names.
  const repository = useComparisonRepository();
  const query = useQuery({
    queryKey: [
      REPOSITORY_REFS_KEY,
      sessionId,
      workspace?.hostId ?? null,
      workspace?.root ?? null,
      repository,
    ],
    queryFn: ({ signal }) => fetchRepositoryRefs(sessionId, signal, repository),
    enabled: enabled && workspace !== null,
    staleTime: REFS_STALE_MS,
    retry: false,
  });
  return workspace ? query.data : undefined;
}
