// The PRs a `#` completion searches for: one page from this Mac shell's completion search, as the
// account the pull request is read as, or from the session's host where the Mac can't search. Answers are cached by that scope and the query, so
// every editor of the PR shares them, and a late answer only fills its own entry.

import { useQuery } from "@tanstack/react-query";

import { useMacReadAs } from "@/hooks/macReadAs";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { CompletionKind } from "@/gen/omnigent/api/workspace/v1alpha1/workspace_pb";
import { type CodeHostPullRequest, parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { readOnThisMac } from "@/lib/desktopPullRequests";
import { macOffers, macReadTargetAs, readScope } from "@/lib/desktopReadPath";
import type { MacNamedReader } from "@/lib/macNamedReads";
import { readPullRequestRpc } from "@/lib/pullRequestRpc";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";

import {
  type PullRequestCandidate,
  type PullRequestCompletionQuery,
  pullRequestCompletionQuery,
} from "./markdownCompletion";

export const PULL_REQUEST_COMPLETION_SEARCH_OPERATION = "pull_request_completion_search";

/** One pull request the shell's completion search lists, in the fields a candidate shows. */
export interface CompletionSearchPullRequest {
  url: string;
  number: number;
  title: string;
  state: "OPEN" | "CLOSED" | "MERGED";
  is_draft: boolean;
}

/** The shell's answer to a completion search: the first matches GitHub lists. */
interface CompletionSearch {
  pull_requests: CompletionSearchPullRequest[];
}

/** How long typing rests before a `#` searches. */
export const COMPLETION_SEARCH_DELAY_MS = 300;

/** Where `prUrl`'s `#` completion searches, or null where this Mac's shell can't. */
export function completionSearchTarget(
  prUrl: string | null,
  readAs: MacNamedReader | null,
): CodeHostPullRequest | null {
  const target = macReadTargetAs(prUrl, readAs);
  return target?.provider === "github" &&
    macOffers("github", PULL_REQUEST_COMPLETION_SEARCH_OPERATION)
    ? target
    : null;
}

export function pullRequestCandidate(
  pullRequest: CompletionSearchPullRequest,
): PullRequestCandidate {
  return {
    url: pullRequest.url,
    number: pullRequest.number,
    title: pullRequest.title,
    state: pullRequest.state,
    isDraft: pullRequest.is_draft,
  };
}

/** One search of `query`, which the shell scopes to its repository, as `target` reads. */
export async function fetchPullRequestCompletions(
  target: CodeHostPullRequest,
  query: PullRequestCompletionQuery,
): Promise<PullRequestCandidate[]> {
  const { data } = await readOnThisMac<{ viewer: string; search: CompletionSearch }>(
    target,
    PULL_REQUEST_COMPLETION_SEARCH_OPERATION,
    { repository: query.repository, text: query.text },
  );
  return data.search.pull_requests.map(pullRequestCandidate);
}

/** One search of `query` in `prUrl`'s repository, through the session's host. */
export async function fetchHostPullRequestCompletions(
  conversationId: string,
  prUrl: string,
  query: PullRequestCompletionQuery,
): Promise<PullRequestCandidate[]> {
  const answer = await readPullRequestRpc<{ pull_requests?: unknown }>(
    conversationId,
    "searchCompletions",
    (client) =>
      client.searchCompletions({ prUrl, kind: CompletionKind.REFERENCE, text: query.text }),
  );
  const found = Array.isArray(answer.pull_requests) ? answer.pull_requests : [];
  return found
    .filter(
      (pullRequest): pullRequest is CompletionSearchPullRequest =>
        typeof pullRequest?.url === "string" &&
        Number.isInteger(pullRequest.number) &&
        typeof pullRequest.title === "string",
    )
    .map(pullRequestCandidate);
}

export interface PullRequestCompletions {
  pullRequests: readonly PullRequestCandidate[];
  /** A search for what's typed now is on its way. */
  searching: boolean;
}

const NO_PULL_REQUESTS: readonly PullRequestCandidate[] = [];

/**
 * The PRs `typed` finds in `prUrl`'s repository, once typing rests; null while nothing is being
 * completed, which searches nothing. A new query lists nothing searched until its answer lands.
 */
export function usePullRequestCompletions(
  prUrl: string | null,
  typed: string | null,
  conversationId = "",
): PullRequestCompletions {
  const readAs = useMacReadAs();
  const macTarget = completionSearchTarget(prUrl, readAs);
  // Where this Mac can't search, the session's host searches the PR's repository.
  const hostServes = usePullRequestMethodServed(conversationId, "searchCompletions");
  const hostTarget = !macTarget && hostServes && prUrl ? parseCodeHostPullRequestUrl(prUrl) : null;
  const onHost = hostTarget?.provider === "github";
  const target = macTarget ?? (onHost ? hostTarget : null);
  const rested = useDebouncedValue(typed, COMPLETION_SEARCH_DELAY_MS);
  // Closing the menu ends its search at once; a pending one never sends.
  const settled = typed === null ? null : rested;
  const query =
    target && settled !== null ? pullRequestCompletionQuery(target.repository, settled) : null;
  const search = useQuery({
    // Kept under the Mac read scope, so a change of account drops these answers as any read's.
    // No placeholder: only an answer cached for this very key is ever its suggestions.
    queryKey: [
      "pull-request-completion-search",
      ...(onHost ? ["host", conversationId] : readScope(prUrl, readAs)),
      target?.repository.toLowerCase() ?? "",
      query?.text ?? "",
    ],
    queryFn: () =>
      onHost
        ? fetchHostPullRequestCompletions(conversationId, prUrl!, query!)
        : fetchPullRequestCompletions(target!, query!),
    enabled: target !== null && query !== null,
    retry: false,
    // A repository's PRs change little while someone types a reference.
    staleTime: 5 * 60_000,
  });
  const searching =
    target !== null &&
    typed !== null &&
    typed.trim() !== "" &&
    (search.isFetching || rested !== typed);
  return { pullRequests: query ? (search.data ?? NO_PULL_REQUESTS) : NO_PULL_REQUESTS, searching };
}
