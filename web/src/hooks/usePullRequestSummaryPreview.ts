// What the panel lays out while a PR's details read, from what list reads on this Mac said of it:
// the PR's summary under the same owner its overview reads as. Only a proven reader's shared
// record has one, so a session read, another reader or an older epoch shows no preview.

import { useMemo } from "react";

import {
  pullRequestPatchRevision,
  useDesktopPullRequestReads,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  pullRequestRecordOwner,
  pullRequestSummaryKey,
  useExactRecord,
  type PullRequestSummary,
} from "@/lib/pullRequestRecord";
import type { PullRequestPreview } from "@/pullRequestPage/detailsLayout";

/**
 * `prUrl`'s summary as a preview for `conversationId`'s panel, or undefined while none is cached
 * for the record its overview reads now.
 */
export function usePullRequestSummaryPreview(
  conversationId: string | undefined,
  prUrl: string | null | undefined,
): PullRequestPreview | undefined {
  const { summary, macTarget } = useOverviewSummary(conversationId, prUrl);
  const repository = macTarget?.repository;
  const number = macTarget?.number;
  return useMemo(
    () =>
      summary && prUrl && repository && number !== undefined
        ? summaryPreview(summary, { url: prUrl, repository, number })
        : undefined,
    [summary, prUrl, repository, number],
  );
}

/** `prUrl`'s summary under the owner its overview reads as now, and the Mac target it reads. */
function useOverviewSummary(conversationId: string | undefined, prUrl: string | null | undefined) {
  const { target, scope } = useDesktopPullRequestReads();
  const macTarget = prUrl ? target(prUrl) : null;
  const queryKey =
    prUrl && macTarget
      ? pullRequestSummaryKey(
          prUrl,
          pullRequestRecordOwner(conversationId ?? "", scope(prUrl), macTarget),
        )
      : null;
  return { summary: useExactRecord<PullRequestSummary>(queryKey), macTarget };
}

/** A list read older than this may predate a push, so its commits don't start a patch. */
export const LISTED_COMMITS_MAX_AGE_MS = 5 * 60_000;

/**
 * The `base:head` revision a list read sent at most {@link LISTED_COMMITS_MAX_AGE_MS} before
 * `now` saw for the PR, or undefined.
 */
export function listedPatchRevision(
  summary: PullRequestSummary | undefined,
  now: number,
): string | undefined {
  const listed = summary?.listedCommits?.value;
  if (!listed || now - listed.readAt > LISTED_COMMITS_MAX_AGE_MS) return undefined;
  return pullRequestPatchRevision({ base_sha: listed.base, head_sha: listed.head });
}

/**
 * The patch revision a recent list read on this Mac saw for `prUrl`, as the reader its overview
 * reads as, so its patch can be read before that overview answers; undefined without one.
 */
export function useListedPatchRevision(
  conversationId: string | undefined,
  prUrl: string | null | undefined,
): string | undefined {
  const { summary } = useOverviewSummary(conversationId, prUrl);
  return useMemo(() => listedPatchRevision(summary, Date.now()), [summary]);
}

/** A preview from `summary`, naming the PR by its link where no read named it. */
export function summaryPreview(
  summary: PullRequestSummary,
  pr: { url: string; repository: string; number: number },
): PullRequestPreview | undefined {
  if (Object.keys(summary).length === 0) return undefined;
  return {
    url: pr.url,
    repository: summary.repository?.value ?? pr.repository,
    number: summary.number?.value ?? pr.number,
    title: summary.title?.value ?? null,
    state: summary.state?.value ?? null,
    isDraft: summary.isDraft?.value ?? false,
    author: summary.author?.value ?? null,
  };
}
