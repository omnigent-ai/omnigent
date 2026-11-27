// A PR link the session's host reads from its code host. A GitHub Enterprise host isn't known
// on this device, so the PR's own host counts as the GitHub host it's served from.

import { type CodeHostPullRequest, parseCodeHostPullRequestUrl } from "@/lib/codeHosts";

/** The session's PR at `url`, on its own host as a GitHub one unless a named host serves it. */
export function parseSessionPullRequestUrl(
  url: string | null | undefined,
): CodeHostPullRequest | null {
  let host: string;
  try {
    host = url ? new URL(url).hostname : "";
  } catch {
    return null;
  }
  return host ? parseCodeHostPullRequestUrl(url, [host]) : null;
}
