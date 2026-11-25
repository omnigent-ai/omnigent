// What Changes shows while a push's new files and patch are read: the pair it last showed, from
// the same shared record (PR, scope, reader and epoch) at the previous revision, read straight
// from the cache. It holds only that revision, never its data, so a purged or reset half, or any
// move of reader or epoch (a different key), leaves nothing to show.

import type { UseQueryResult } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import {
  pullRequestChangedFilesQueryOptions,
  pullRequestDiffQueryOptions,
  useDesktopPullRequestReads,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { pullRequestRecordOwner, useExactRecord } from "@/lib/pullRequestRecord";

/** The files and patch Changes shows, at `revision`; `outdated` while that's a previous push's. */
export interface ShownChangesPair<Files, Patch> {
  changes: UseQueryResult<Files>;
  prDiff: UseQueryResult<Patch>;
  revision: string;
  outdated: boolean;
}

/**
 * The pair to show for `prUrl` at `revision`, from `changes` and `prDiff` read at it. While both
 * are still reading, and only then, it's the pair last shown at an earlier revision of the same
 * shared record, where both halves are still cached. Only where `keepPrevious`: a view that
 * marks the kept pair as outdated.
 */
export function useShownChangesPair<Files, Patch>(
  conversationId: string | undefined,
  prUrl: string | undefined,
  revision: string,
  changes: UseQueryResult<Files>,
  prDiff: UseQueryResult<Patch>,
  { keepPrevious }: { keepPrevious: boolean },
): ShownChangesPair<Files, Patch> {
  const reads = useDesktopPullRequestReads();
  const complete = changes.data !== undefined && prDiff.data !== undefined;
  const shownRevision = useRef<string | null>(null);
  useEffect(() => {
    if (complete) shownRevision.current = revision;
  }, [complete, revision]);

  const previous = shownRevision.current;
  const owner =
    prUrl && reads.target(prUrl)
      ? pullRequestRecordOwner(conversationId ?? "", reads.scope(prUrl), reads.target(prUrl))
      : null;
  const keeps =
    keepPrevious &&
    !complete &&
    !changes.error &&
    !prDiff.error &&
    previous !== null &&
    previous !== revision &&
    owner?.kind === "mac" &&
    owner.reader !== null;
  const filesKey = keeps
    ? pullRequestChangedFilesQueryOptions(conversationId, prUrl, previous, reads).queryKey
    : null;
  const patchKey = keeps
    ? pullRequestDiffQueryOptions(conversationId, prUrl, previous, reads).queryKey
    : null;
  const keptFiles = useExactRecord<Files>(filesKey);
  const keptPatch = useExactRecord<Patch>(patchKey);

  if (!keeps || keptFiles === undefined || keptPatch === undefined) {
    return { changes, prDiff, revision, outdated: false };
  }
  return {
    changes: kept(changes, keptFiles),
    prDiff: kept(prDiff, keptPatch),
    revision: previous,
    outdated: true,
  };
}

/** `result` as if it held `data`: settled, with the live read's own controls. */
function kept<Data>(result: UseQueryResult<Data>, data: Data): UseQueryResult<Data> {
  return {
    ...result,
    data,
    isLoading: false,
    isPending: false,
    isSuccess: true,
    status: "success",
  } as UseQueryResult<Data>;
}
