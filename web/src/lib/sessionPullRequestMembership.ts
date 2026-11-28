// Which pull requests a session links, decided in one place so the panel, the link
// picker, chat links and ⌘K can't disagree. Once the server has synced the session,
// its stored list is the truth; the live payload counts only where no such list exists.

import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import { pullRequestKey } from "@/lib/pullRequestReference";

/** The most PRs the server links to one session; it refuses another past this. */
export const MAX_SESSION_PULL_REQUESTS = 50;

export const SESSION_PULL_REQUEST_LIMIT_REASON = `This session links ${MAX_SESSION_PULL_REQUESTS} pull requests, the most it can. Unlink one to link this.`;

type MemberRow = Pick<PullRequestAssociation, "url" | "relationship">;

export interface SessionPullRequestSources {
  /**
   * The server's stored list, with this Mac's own links: the last one it answered, kept
   * through a refresh that failed. Undefined until it has answered once.
   */
  stored: { prs: readonly MemberRow[]; synced: boolean } | undefined;
  /** The session's live payload's PRs, from its host or the server. */
  live: readonly MemberRow[] | undefined;
  /** Links and unlinks asked for that the lists don't show yet, by PR key; they show at once. */
  intents?: Readonly<Record<string, { action: "link" | "unlink" }>>;
}

export interface SessionPullRequestMembership {
  /** Each linked PR's key, however its link is written. */
  readonly keys: ReadonlySet<string>;
  /** Whether the server is the one deciding, rather than the live payload standing in. */
  readonly authoritative: boolean;
}

/**
 * The PRs linked to the session. A synced stored list decides alone: a live payload
 * can carry the host's raw rows when the server couldn't store them, or rows an unlink
 * has since removed. A PR only inferred from the branch is never linked.
 */
export function sessionPullRequestMembership({
  stored,
  live,
  intents,
}: SessionPullRequestSources): SessionPullRequestMembership {
  // A synced list stays the truth when a later refresh fails; the live payload only
  // stands in where the server never answered with one.
  const authoritative = !!stored?.synced;
  // Until the server has synced, both lists only add what each has seen.
  const rows = authoritative ? stored.prs : [...(live ?? []), ...(stored?.prs ?? [])];
  const keys = new Set(
    rows.filter((pr) => pr.relationship !== "inferred").map((pr) => pullRequestKey(pr.url)),
  );
  for (const [key, intent] of Object.entries(intents ?? {})) {
    if (intent.action === "link") keys.add(key);
    else keys.delete(key);
  }
  return { keys, authoritative };
}

/** Whether the membership has the PR at `url`, however either link is written. */
export function linksPullRequest(membership: SessionPullRequestMembership, url: string): boolean {
  return membership.keys.has(pullRequestKey(url));
}

/** Whether the session has no room left to link the PR at `url`. */
export function sessionPullRequestsFull(
  membership: SessionPullRequestMembership,
  url: string,
): boolean {
  return membership.keys.size >= MAX_SESSION_PULL_REQUESTS && !linksPullRequest(membership, url);
}
