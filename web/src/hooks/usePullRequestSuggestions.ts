import { skipToken, useQuery } from "@tanstack/react-query";

import { useGitHubFastTrack } from "@/hooks/useGitHubFastTrack";
import { readOnThisMac, type MacReadTarget } from "@/lib/desktopPullRequests";
import {
  desktopReadSupportKnown,
  readsHostOnThisMac,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
import { macReadScopeKey } from "@/lib/macReadScope";
import type { GitHubFastTrackAccount } from "@/lib/nativeBridge";
import {
  fetchPullRequestSuggestions,
  PULL_REQUEST_SUGGESTIONS_OPERATION,
  type PullRequestSuggestions,
} from "@/lib/pullRequestSuggestions";
import { useMacReadScopes } from "@/hooks/useMacReadScopes";

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
 * PRs to suggest in the link picker. Read on this Mac as its own identity where
 * it reads `mac`'s host, else with the fast track as the session host's account.
 * `unsupported` wherever neither can run, so the picker keeps its paste-a-link
 * behavior there.
 */
export function usePullRequestSuggestions(
  account: GitHubFastTrackAccount | null,
  repository: string | null,
  enabled: boolean,
  mac: MacReadTarget | null = null,
): PullRequestSuggestionsState {
  useDesktopReadPathVersion();
  useMacReadScopes();
  const onMac =
    !!mac && readsHostOnThisMac(mac.provider, mac.host, PULL_REQUEST_SUGGESTIONS_OPERATION);
  const macQuery = useQuery({
    queryKey: [
      "code-host-mac",
      "pull_request_suggestions",
      mac?.provider,
      mac?.host,
      repository,
      ...(onMac && mac && mac.provider !== "narnia" ? macReadScopeKey(mac.provider, mac.host) : []),
    ],
    queryFn:
      onMac && enabled && mac
        ? async () =>
            (
              await readOnThisMac<PullRequestSuggestions>(
                mac,
                PULL_REQUEST_SUGGESTIONS_OPERATION,
                repository ? { repository } : {},
              )
            ).data
        : skipToken,
    staleTime: SUGGESTIONS_STALE_MS,
    retry: false,
  });
  // Until the shell says what it reads, don't start the host account's read it may replace.
  const known = !mac || desktopReadSupportKnown();
  // A shell that predates the suggestions read reports unsupported, keeping paste-only linking.
  const fastTrack = useGitHubFastTrack(
    enabled && known && !onMac ? account : null,
    SUGGESTION_OPERATIONS,
  );
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
  if (onMac) {
    if (macQuery.error) return { status: "error", message: macQuery.error.message };
    if (macQuery.data) return { status: "ready", suggestions: macQuery.data };
    return enabled ? { status: "loading" } : { status: "checking" };
  }
  if (!known) return { status: "checking" };
  if (fastTrack.status === "unsupported") return { status: "unsupported" };
  if (fastTrack.status === "checking") return { status: "checking" };
  if (fastTrack.status === "unavailable") {
    return { status: "error", message: fastTrack.error.message };
  }
  if (query.error) return { status: "error", message: query.error.message };
  if (query.data) return { status: "ready", suggestions: query.data };
  return { status: "loading" };
}
