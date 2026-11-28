// The PRs a `#` completion searches for: one page from the session's host's completion search.
// Answers are cached by the session and the query, so every editor of the PR shares them, and a
// late answer only fills its own entry.

import { useQuery } from "@tanstack/react-query";

import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { CompletionKind } from "@/gen/omnigent/api/workspace/v1alpha1/workspace_pb";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { readPullRequestRpc } from "@/lib/pullRequestRpc";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";

import {
  type PullRequestCandidate,
  type PullRequestCompletionQuery,
  pullRequestCompletionQuery,
} from "./markdownCompletion";

/** One pull request the host's completion search lists, in the fields a candidate shows. */
export interface CompletionSearchPullRequest {
  url: string;
  number: number;
  title: string;
  state: "OPEN" | "CLOSED" | "MERGED";
  is_draft: boolean;
}

/** How long typing rests before a `#` searches. */
export const COMPLETION_SEARCH_DELAY_MS = 300;

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
  // Without the host's search, typing a reference still works; nothing pops up.
  const hostServes = usePullRequestMethodServed(conversationId, "searchCompletions");
  const parsed = hostServes && prUrl ? parseCodeHostPullRequestUrl(prUrl) : null;
  const target = parsed?.provider === "github" ? parsed : null;
  const rested = useDebouncedValue(typed, COMPLETION_SEARCH_DELAY_MS);
  // Closing the menu ends its search at once; a pending one never sends.
  const settled = typed === null ? null : rested;
  const query =
    target && settled !== null ? pullRequestCompletionQuery(target.repository, settled) : null;
  const search = useQuery({
    // No placeholder: only an answer cached for this very key is ever its suggestions.
    queryKey: [
      "pull-request-completion-search",
      "host",
      conversationId,
      target?.repository.toLowerCase() ?? "",
      query?.text ?? "",
    ],
    queryFn: () => fetchHostPullRequestCompletions(conversationId, prUrl!, query!),
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
