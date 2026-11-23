// One file's diff in a pinned branch-point or revision comparison, read through
// the session's comparison owner and cached apart from the last-commit diff.

import { fileDiffQueryKey, type FileDiffResponse } from "@/hooks/useFileDiff";
import {
  ComparisonFileError,
  fetchComparisonFileText,
  needsRelist,
  StaleComparisonReadError,
} from "@/lib/workingCopyComparison";
import type { WorkingCopyComparisonOwner } from "@/lib/workingCopyComparisonOwner";

/** Which listing a cached comparison diff belongs to. */
export interface ComparisonFileDiffBase {
  comparisonId: string;
  /** The file's fingerprint in that listing, so changed text is never read from an older entry. */
  fingerprint: string;
  /** The comparison owner's generation, so nothing outlives a change of signed-in user. */
  generation: number;
}

/** The query key a comparison diff is cached under, beside the file's last-commit diff. */
export function comparisonFileDiffQueryKey(
  conversationId: string | undefined,
  path: string | null,
  base: ComparisonFileDiffBase,
) {
  return [...fileDiffQueryKey(conversationId, path), base] as const;
}

/**
 * Reads one file of a pinned comparison. A refusal saying the comparison is
 * gone, or the file isn't in it, lists again once and retries if the file is
 * still current; anything else is the reader's to show.
 */
export async function fetchComparisonFileDiff(
  owner: WorkingCopyComparisonOwner,
  conversationId: string,
  path: string,
  base: ComparisonFileDiffBase,
  signal?: AbortSignal,
): Promise<FileDiffResponse> {
  const read = () => {
    // Nobody may read the workspace now, as while it can't be reached: start nothing.
    if (!owner.mayRead(conversationId)) throw new StaleComparisonReadError();
    return fetchComparisonFileText(conversationId, base.comparisonId, path, signal);
  };
  const isCurrent = () =>
    owner.isCurrent(conversationId, base.generation, base.comparisonId, path, base.fingerprint);
  // Which listing this read began under, so a reader joining another's recovery gets its retry.
  const readToken = owner.readToken(conversationId);
  let text;
  try {
    text = await read();
  } catch (error) {
    if (!(error instanceof ComparisonFileError) || !needsRelist(error.reason)) throw error;
    const relisted = await owner.recover(conversationId, base.comparisonId, path, readToken);
    // A new comparison or fingerprint re-keys the reader; this read is over.
    if (!relisted || !isCurrent()) throw error;
    text = await read();
  }
  // An answer for a version the session no longer shows is never published.
  if (!isCurrent()) throw new StaleComparisonReadError();
  owner.readSucceeded(conversationId, base.comparisonId, path);
  return {
    object: "session.environment.filesystem.file_diff",
    path,
    before: text.before,
    after: text.after,
  };
}
