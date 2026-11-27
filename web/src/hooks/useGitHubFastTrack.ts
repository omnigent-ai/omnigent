import { skipToken, useQuery } from "@tanstack/react-query";

import { checkGitHubFastTrackAccount, type GitHubFastTrackState } from "@/lib/githubFastTrack";
import { hasGitHubFastTrackBridge, useGitHubFastTrackOpen } from "@/lib/githubFastTrackBridge";
import type { GitHubFastTrackAccount } from "@/lib/nativeBridge";

/** How long a confirmed account stays fresh; sign-ins change rarely. */
const ACCOUNT_STATUS_STALE_MS = 60_000;

/** The account check's query key, so a retry can restart exactly that check. */
export function gitHubFastTrackStatusKey(
  account: GitHubFastTrackAccount | null,
  operations: readonly string[] = [],
): readonly unknown[] {
  return ["github-fast-track", "account_status", account?.host, account?.login, ...operations];
}

/**
 * Whether fast-track features that need `operations` can run for `account` (the
 * session host's login for a PR). Resolves `unsupported` off the desktop shell or without an account, so
 * callers can hide fast-track features without a network round trip.
 */
export function useGitHubFastTrack(
  account: GitHubFastTrackAccount | null,
  operations: readonly string[] = [],
): GitHubFastTrackState {
  // Closed once the page's server moves, which re-renders here so features fall back.
  const bridge = useGitHubFastTrackOpen() && hasGitHubFastTrackBridge();
  const query = useQuery({
    queryKey: gitHubFastTrackStatusKey(account, operations),
    queryFn: bridge && account ? () => checkGitHubFastTrackAccount(account, operations) : skipToken,
    staleTime: ACCOUNT_STATUS_STALE_MS,
    retry: false,
  });
  if (!bridge || !account) return { status: "unsupported" };
  return query.data ?? { status: "checking" };
}
