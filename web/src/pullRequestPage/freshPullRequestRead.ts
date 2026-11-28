// Check Again after a change GitHub may or may not have made: a read of the PR through the
// session's host that this check started, and that answered for this PR, is the only thing that
// settles it.

import type { QueryClient } from "@tanstack/react-query";

import {
  fetchPullRequestInfo,
  type PullRequestAutoMerge,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { readSettlingMerge } from "@/lib/pullRequestStateHolds";
import type { FreshPullRequestFacts } from "./changeAttempts";
import { autoMergeKnown } from "@/lib/pullRequestAutoMerge";
import { currentWorkspaceAuthGeneration } from "@/lib/workspaceDraftOwner";

/**
 * - `confirmed`: a fresh read of this PR through the host answered with a known state.
 * - `failed`: no such read, or it didn't answer; the hold stays.
 * - `stale`: the workspace user changed meanwhile, so the read can't speak for the change;
 *   nothing is said.
 */
export type FreshReadResult = "confirmed" | "failed" | "stale";

/** A fresh read's result, and where it confirmed the PR stands. */
export interface FreshRead {
  result: FreshReadResult;
  /** Set only where `confirmed`: the state and draft flag this very read answered. */
  facts: FreshPullRequestFacts | null;
}

export async function confirmFreshPullRequestRead(
  ...args: Parameters<typeof readFreshPullRequest>
): Promise<FreshReadResult> {
  return (await readFreshPullRequest(...args)).result;
}

/** Check Again for a change the session's host made, read through that same host. */
export async function readFreshPullRequest(
  queryClient: QueryClient,
  conversationId: string,
  prUrl: string,
): Promise<FreshRead> {
  return readFreshPullRequestOnHost(queryClient, conversationId, prUrl);
}

interface ReadPullRequestInfo {
  selected_pr_url?: string | null;
  pr?: {
    url?: string | null;
    state?: string | null;
    is_draft?: boolean | null;
    reviewers?: unknown;
    auto_merge?: PullRequestAutoMerge | null;
  } | null;
}

function factsOf(info: ReadPullRequestInfo | undefined): FreshPullRequestFacts {
  return {
    state: info?.pr?.state?.toUpperCase() as FreshPullRequestFacts["state"],
    isDraft: info?.pr?.is_draft === true,
    reviewers: reviewersOf(info?.pr?.reviewers),
    autoMerge: autoMergeKnown(info?.pr),
  };
}

/**
 * Check Again for a change the session's host made: a read through the host that this check
 * sent, answering for this PR with a known state; the page's own reads follow it.
 */
async function readFreshPullRequestOnHost(
  queryClient: QueryClient,
  conversationId: string,
  prUrl: string,
): Promise<FreshRead> {
  const generation = currentWorkspaceAuthGeneration();
  let info: ReadPullRequestInfo;
  try {
    info = (await readSettlingMerge(prUrl, () =>
      fetchPullRequestInfo(conversationId, prUrl),
    )) as ReadPullRequestInfo;
  } catch {
    return FAILED;
  }
  // Another workspace user since: this read can't speak for the change.
  if (generation !== currentWorkspaceAuthGeneration()) return STALE;
  const state = info.pr?.state?.toUpperCase();
  const confirmed =
    info.selected_pr_url === prUrl &&
    (!info.pr?.url || info.pr.url.toLowerCase() === prUrl.toLowerCase()) &&
    (state === "OPEN" || state === "CLOSED" || state === "MERGED");
  if (!confirmed) return FAILED;
  void queryClient.invalidateQueries({ queryKey: ["pr-page-github-info", conversationId] });
  return { result: "confirmed", facts: factsOf(info) };
}

/**
 * The answer's reviewers; null where it sent none, or any it can't read, since a list missing
 * one can't say who is still asked.
 */
function reviewersOf(value: unknown): FreshPullRequestFacts["reviewers"] {
  if (!Array.isArray(value)) return null;
  const reviewers: { login: string; kind: string; state: string }[] = [];
  for (const reviewer of value) {
    const { login, kind, state } = (reviewer ?? {}) as Record<string, unknown>;
    if (typeof login !== "string" || typeof state !== "string") return null;
    if (kind !== "user" && kind !== "team") return null;
    reviewers.push({ login, kind, state });
  }
  return reviewers;
}

const STALE: FreshRead = { result: "stale", facts: null };
const FAILED: FreshRead = { result: "failed", facts: null };
