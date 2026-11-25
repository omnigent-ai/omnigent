// GitSpoke pull requests are read-only: this Mac can't change them yet, so every change names
// GitSpoke itself as the place to make it.

import { isGitSpokeHost } from "@/lib/codeHosts";

/** Why a change leaves a GitSpoke pull request alone. */
export const GITSPOKE_WRITE_REASON = "Change this pull request on GitSpoke.";

/** Whether `url` is a pull request on a GitSpoke host. */
export function isGitSpokePullRequest(url: string): boolean {
  try {
    return isGitSpokeHost(new URL(url).host);
  } catch {
    return false;
  }
}

/** The reason no change from this Mac applies to `url`, or null where one may. */
export function gitSpokeWriteReason(url: string): string | null {
  return isGitSpokePullRequest(url) ? GITSPOKE_WRITE_REASON : null;
}
