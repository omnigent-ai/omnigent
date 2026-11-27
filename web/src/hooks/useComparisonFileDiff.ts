// One file's diff under the session's chosen comparison, for a viewer that should show what
// the Changes review shows. A branch-point or typed comparison reads through the review's
// cache entry and recovery; under the last commit, the viewer's own diff is already the same.

import { type UseQueryResult, useQuery } from "@tanstack/react-query";

import { useComparisonOwner } from "@/hooks/useComparisonOwner";
import { fileDiffQueryKey, type FileDiffResponse } from "@/hooks/useFileDiff";
import { useForgetDiffTextWhenRevoked, useWorkingCopyChanges } from "@/hooks/useWorkingCopyChanges";
import {
  type ComparisonFileDiffBase,
  comparisonFileDiffQueryKey,
  fetchComparisonFileDiff,
} from "@/lib/branchPointFileDiff";
import {
  type ComparisonEntry,
  ComparisonFileError,
  comparisonHeadIsWorkingCopy,
  hostListing,
  isSettledFileReason,
  needsRelist,
  StaleComparisonReadError,
} from "@/lib/workingCopyComparison";

export type ComparisonFileDiff =
  /** The session compares with the last commit: the viewer's own diff is the review's. */
  | { base: "head" }
  | {
      base: "comparison";
      /** The file is in the comparison's listing, so it has a diff to show. */
      changed: boolean;
      /** The comparison lists the file as deleted. */
      deleted: boolean;
      /** The after side is the working copy, which comments and attachments refer to. */
      headIsWorkingCopy: boolean;
      /** Disabled while the comparison is still being decided or can't be listed. */
      query: UseQueryResult<FileDiffResponse>;
    };

/** Whether a failed comparison read is worth trying again on its own. */
function retryComparisonRead(failureCount: number, error: Error): boolean {
  if (error instanceof StaleComparisonReadError) return false;
  if (error instanceof ComparisonFileError) {
    return !isSettledFileReason(error.reason) && !needsRelist(error.reason) && failureCount < 2;
  }
  return failureCount < 3;
}

/** `path`'s diff under the session's effective comparison, as the Changes review reads it. */
export function useComparisonFileDiff(
  conversationId: string,
  path: string | null,
): ComparisonFileDiff {
  const owner = useComparisonOwner();
  const changes = useWorkingCopyChanges(conversationId, { kind: "file" });
  useForgetDiffTextWhenRevoked(conversationId, changes.permitted);
  const { comparison } = changes;
  const listing = hostListing(comparison);
  const entry =
    listing && path ? listing.entries.find((file: ComparisonEntry) => file.path === path) : null;
  const base: ComparisonFileDiffBase | null =
    listing && entry && changes.permitted
      ? { comparisonId: listing.id, fingerprint: entry.fingerprint, generation: changes.generation }
      : null;
  const query = useQuery({
    queryKey: base
      ? comparisonFileDiffQueryKey(conversationId, path, base)
      : ([...fileDiffQueryKey(conversationId, path), "no-comparison"] as const),
    queryFn: ({ signal }) => fetchComparisonFileDiff(owner, conversationId, path!, base!, signal),
    enabled: base !== null && changes.readable,
    retry: retryComparisonRead,
    // A listing's text never changes: a new version gets a new fingerprint, and so a new key.
    staleTime: Infinity,
  });
  // The runner lists the last commit's changes, except in a repository folder, where the host does.
  if (comparison.base === "head" && !listing) return { base: "head" };
  return {
    base: "comparison",
    changed: !!entry,
    deleted: entry?.status === "deleted",
    headIsWorkingCopy: comparisonHeadIsWorkingCopy(comparison),
    query,
  };
}
