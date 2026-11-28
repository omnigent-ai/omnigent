import { skipToken, useQuery } from "@tanstack/react-query";

import { useGitHubFastTrack } from "@/hooks/useGitHubFastTrack";
import type { GitHubFastTrackAccount } from "@/lib/nativeBridge";
import {
  fetchPullRequestSuggestions,
  PULL_REQUEST_SUGGESTIONS_OPERATION,
  type PullRequestSuggestions,
} from "@/lib/pullRequestSuggestions";

const SUGGESTION_OPERATIONS = [PULL_REQUEST_SUGGESTIONS_OPERATION];

// Long enough to reopen the picker instantly, short enough that a new PR shows soon.
const SUGGESTIONS_STALE_MS = 30_000;

export type PullRequestSuggestionsState =
  | { status: "unsupported" }
  /** Still finding out whether the fast track can run; nothing to show yet. */
  | { status: "checking" }
  | { status: "loading" }
  | { status: "ready"; suggestions: PullRequestSuggestions }
  | { status: "error"; message: string };

/**
 * PRs to suggest in the link picker, read with the fast track as the session host's account.
 * `unsupported` wherever it can't run, so the picker keeps its paste-a-link behavior there.
 */
export function usePullRequestSuggestions(
  account: GitHubFastTrackAccount | null,
  repository: string | null,
  enabled: boolean,
): PullRequestSuggestionsState {
  // A shell that predates the suggestions read reports unsupported, keeping paste-only linking.
  const fastTrack = useGitHubFastTrack(enabled ? account : null, SUGGESTION_OPERATIONS);
  const ready = fastTrack.status === "ready" ? fastTrack.account : null;
  const query = useQuery({
    queryKey: [
      "github-fast-track",
      "pull_request_suggestions",
      ready?.host,
      ready?.login,
      repository,
    ],
    queryFn: ready ? () => fetchPullRequestSuggestions(ready, repository) : skipToken,
    staleTime: SUGGESTIONS_STALE_MS,
    retry: false,
  });
  if (fastTrack.status === "unsupported") return { status: "unsupported" };
  if (fastTrack.status === "checking") return { status: "checking" };
  if (fastTrack.status === "unavailable") {
    return { status: "error", message: fastTrack.error.message };
  }
  if (query.error) return { status: "error", message: query.error.message };
  if (query.data) return { status: "ready", suggestions: query.data };
  return { status: "loading" };
}
