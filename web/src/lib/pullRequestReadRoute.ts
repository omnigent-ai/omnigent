// Where one pull request is read: this Mac, the session's machine, or, when the user chose
// this Mac and it can't read the PR, nowhere. Keeps the automatic policy, the user's choice
// and the effective route apart; the store in desktopReadPath holds the choices.

import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { pullRequestIntegrations } from "@/lib/pullRequestIntegrations";

/** The user's choice for one PR. Nothing chosen means Automatic. */
export type ReadChoice = "automatic" | "mac" | "session";

/** Where a PR's reads go now. */
export type ReadRoute =
  | { kind: "mac"; target: CodeHostPullRequest }
  | { kind: "host" }
  /**
   * Nothing reads the PR, so nothing stale or another account's shows: this Mac was chosen but
   * can't read it, or its repository's chosen login can't be read as now (`repo_account`).
   */
  | { kind: "blocked"; reason: "mac_unsupported" | "repo_account" };

/**
 * The effective route from the choice and what this Mac can do. `automatic` is the policy's
 * Mac target (null when Automatic reads on the host); `forced` is the Mac target with the
 * user's Settings and the release flag set aside, as an explicit "This Mac" asks.
 */
export function decideReadRoute(
  choice: ReadChoice,
  {
    automatic,
    forced,
  }: { automatic: CodeHostPullRequest | null; forced: CodeHostPullRequest | null },
): ReadRoute {
  if (choice === "session") return { kind: "host" };
  if (choice === "mac") {
    return forced
      ? { kind: "mac", target: forced }
      : { kind: "blocked", reason: "mac_unsupported" };
  }
  return automatic ? { kind: "mac", target: automatic } : { kind: "host" };
}

/** A read refused because this Mac was chosen and can't read the PR; no host was asked. */
export class ReadSourceUnavailableError extends Error {
  readonly prUrl: string;

  constructor(prUrl: string) {
    super("This Mac can’t read this pull request.");
    this.name = "ReadSourceUnavailableError";
    this.prUrl = prUrl;
  }
}

// A PR page and any of its subpages, such as /files, /commits or /checks.
const PULL_PAGE = /^\/([^/]+)\/([^/]+)\/pull\/(\d+)(?:\/[^?#]*)?$/;

/**
 * The PR's own URL for any of its pages: no subpage, query or fragment, and a lowercase
 * host. Owner and repository keep their case. Null when `url` isn't a PR page.
 */
export function pullRequestPageUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" || parsed.port || parsed.username || parsed.password)
    return null;
  const match = PULL_PAGE.exec(parsed.pathname);
  if (!match) return null;
  const [, owner, repo, number] = match;
  return `https://${parsed.hostname.toLowerCase()}/${owner}/${repo}/pull/${number}`;
}

/** The key one PR's choice is kept under, the same for every spelling of its URL. */
export function readChoiceKey(url: string | null | undefined): string | null {
  const pageUrl = pullRequestPageUrl(url);
  if (!pageUrl) return null;
  // GitHub owners and repositories don't differ by case alone; a host GitHub doesn't run may.
  const host = new URL(pageUrl).hostname;
  const named = pullRequestIntegrations().namedCodeHosts.find((entry) => entry.matchesHost(host));
  return named && !named.hostedOnGitHub ? pageUrl : pageUrl.toLowerCase();
}

/** Part of a read's query key while the route is blocked, so no host answer is reused. */
export const BLOCKED_READ_SCOPE = ["read-source", "blocked"] as const;
