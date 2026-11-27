// The keys of a view's pull request reads, each a session's own: the session whose host reads
// the PR, its URL and the family's own parts (revision, head…).
//
// Every key of a PR read is built here, and every write that patches one names its exact owner.

import type { Query, QueryClient, QueryFilters, QueryKey } from "@tanstack/react-query";

/** The read families a PR's record holds. */
export type PullRequestReadFamily =
  | "pr-page-github-info"
  | "pr-page-github-changed-files"
  | "pr-page-github-pr-diff"
  | "pr-page-github-pr-commits"
  | "pr-page-github-pr-commit-diff"
  | "pr-page-github-review-threads"
  | "pr-page-github-merge-options"
  | "pr-page-github-reviewers";

export const PULL_REQUEST_READ_FAMILIES: readonly PullRequestReadFamily[] = [
  "pr-page-github-info",
  "pr-page-github-changed-files",
  "pr-page-github-pr-diff",
  "pr-page-github-pr-commits",
  "pr-page-github-pr-commit-diff",
  "pr-page-github-review-threads",
  "pr-page-github-merge-options",
  "pr-page-github-reviewers",
];

/** Who a PR read belongs to: the session whose host reads it. */
export interface PullRequestRecordOwner {
  session: string;
}

/** A read through `session`'s host, or one of its own: kept apart from every other view's. */
export function sessionRecordOwner(session: string): PullRequestRecordOwner {
  return { session };
}

/** `family`'s key for `prUrl` as `owner`; `rest` is the family's own parts (revision, head…). */
export function pullRequestRecordKey(
  family: PullRequestReadFamily,
  prUrl: string,
  owner: PullRequestRecordOwner,
  ...rest: readonly unknown[]
): QueryKey {
  return [family, owner.session, prUrl, ...rest];
}

/** Where a family's own parts (`rest`: revision, head…) start in a PR read's key. */
export function pullRequestReadRestAt(_queryKey: QueryKey): number {
  return 3;
}

/** Whether `queryKey` is a read, in a PR read family, of `prUrl` that `session` shows. */
export function isSessionPullRequestRead(
  queryKey: QueryKey,
  session: string,
  prUrl: string,
): boolean {
  if (!PULL_REQUEST_READ_FAMILIES.includes(queryKey[0] as PullRequestReadFamily)) return false;
  if (queryKey[2] !== prUrl) return false;
  return queryKey[1] === session;
}

/** The PR URL any of this module's keys is for, or null for another key. */
export function pullRequestRecordUrl(queryKey: QueryKey): string | null {
  if (!PULL_REQUEST_READ_FAMILIES.includes(queryKey[0] as PullRequestReadFamily)) return null;
  return typeof queryKey[2] === "string" ? queryKey[2] : null;
}

/**
 * The entries of `family` for `prUrl` that belong to exactly `owner`, every revision of them:
 * the only filter a write may patch, cancel before patching, or set data through.
 */
export function pullRequestRecordFilter(
  family: PullRequestReadFamily,
  prUrl: string,
  owner: PullRequestRecordOwner,
): QueryFilters {
  return { queryKey: pullRequestRecordKey(family, prUrl, owner) };
}

/**
 * Every view's entries of `prUrl`, any owner, in `families` (all by default): for reading the PR
 * again, never for patching it.
 */
export function invalidatePullRequestEverywhere(
  client: QueryClient,
  prUrl: string,
  families: readonly PullRequestReadFamily[] = PULL_REQUEST_READ_FAMILIES,
): Promise<void> {
  return client.invalidateQueries({
    predicate: (query) => readsPullRequest(query, prUrl, families),
  });
}

/** Which queries of `families` `session` shows. For reading again only, never for patching. */
export function sessionPullRequestReads(
  session: string,
  families: readonly PullRequestReadFamily[] = PULL_REQUEST_READ_FAMILIES,
): (query: Pick<Query, "queryKey">) => boolean {
  return ({ queryKey }) =>
    families.includes(queryKey[0] as PullRequestReadFamily) && queryKey[1] === session;
}

/** Read again everything of `families` `session` shows; see {@link sessionPullRequestReads}. */
export function invalidateSessionPullRequestReads(
  client: QueryClient,
  session: string,
  families: readonly PullRequestReadFamily[] = PULL_REQUEST_READ_FAMILIES,
): Promise<void> {
  return client.invalidateQueries({ predicate: sessionPullRequestReads(session, families) });
}

/** Read `prUrl` again as `session` shows it, in `families`. For reading again only. */
export function invalidateSessionPullRequest(
  client: QueryClient,
  session: string,
  prUrl: string,
  families: readonly PullRequestReadFamily[] = PULL_REQUEST_READ_FAMILIES,
): Promise<void> {
  return client.invalidateQueries({
    predicate: ({ queryKey }) =>
      families.includes(queryKey[0] as PullRequestReadFamily) &&
      isSessionPullRequestRead(queryKey, session, prUrl),
  });
}

/** Whether `query` is a read of `prUrl` in one of `families`, under any owner. */
export function readsPullRequest(
  query: Pick<Query, "queryKey">,
  prUrl: string,
  families: readonly PullRequestReadFamily[] = PULL_REQUEST_READ_FAMILIES,
): boolean {
  const [family] = query.queryKey;
  return (
    families.includes(family as PullRequestReadFamily) &&
    pullRequestRecordUrl(query.queryKey) === prUrl
  );
}

/** A view's read of one PR family: the owner it goes to and the key it's cached under. */
export interface PullRequestRecordRead {
  owner: PullRequestRecordOwner;
  queryKey: QueryKey;
}

/** `family`'s read of `prUrl` from `session`, with the family's `rest` parts. */
export function pullRequestRecordRead(
  family: PullRequestReadFamily,
  session: string,
  prUrl: string,
  ...rest: readonly unknown[]
): PullRequestRecordRead {
  const owner = sessionRecordOwner(session);
  return { owner, queryKey: pullRequestRecordKey(family, prUrl, owner, ...rest) };
}
