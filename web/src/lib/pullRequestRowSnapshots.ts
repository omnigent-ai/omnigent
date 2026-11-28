// A Pull Requests list row reads its overview again only when the list's batched snapshot of
// the PR changes, plus an absolute backstop, instead of every 30 seconds. The snapshot a row's
// overview followed lives in that overview's own answer, so a reset or a change of reader
// clears it with the query, and a read that was cancelled or superseded never records one.

import type { QueryClient } from "@tanstack/react-query";

import {
  pullRequestListStatsMember,
  type PullRequestListStatsRead,
} from "@/lib/pullRequestListStats";

/** The rollback flag: off, list rows read every 30 seconds and no Mac batch is read for them. */
export const PULL_REQUEST_ROWS_FOLLOW_SNAPSHOTS_FLAG = "pull-request-rows-follow-snapshots";

/** A followed row still reads this long after its last answer, whatever the snapshot says. */
export const ROW_SNAPSHOT_BACKSTOP_MS = 10 * 60_000;

/** The rollback flag: off, a followed row reads again 10 minutes after its last answer. */
export const PULL_REQUEST_ROW_BACKSTOP_60_FLAG = "pull-request-row-backstop-60";

/** With that flag on, the longest a followed row goes between reads. */
export const ROW_HOURLY_BACKSTOP_MS = 60 * 60_000;

/** A row's Reviewers cell says how old they are once its own read is this old. */
export const ROW_DETAILS_AGE_SHOWN_AFTER_MS = 15 * 60_000;

/** How far before the hour each row's own backstop falls, so the rows' reads spread out. */
export const ROW_BACKSTOP_STAGGER_MS = 10 * 60_000;

/** The row's list reads before it follows a snapshot, and after a failed read. */
export const ROW_UNFOLLOWED_POLL_MS = 30_000;

const STATS_OPERATION = "pull_request_list_stats";

/** Whether `key` starts with every part of `scope`. */
function startsWith(key: readonly unknown[], scope: readonly unknown[]): boolean {
  return scope.length > 0 && scope.every((part, index) => key[index] === part);
}

/** A row's PR: its link, and the repository and number its batch's key names it by. */
export interface RowPullRequest {
  url: string;
  repository: string;
  number: number;
}

/**
 * The snapshot of `pr` in the newest batch this QueryClient read for it under `scope`, the
 * read scope the row's own overview is cached under. Batches that asked about the PR are
 * found by their keys, so one whose first read failed counts too. A failed read counts as
 * one without a snapshot, as of its failure; and when the newest batch has no complete
 * snapshot of the PR, an older one doesn't stand in: undefined, and the row keeps polling.
 */
export function newestRowSnapshot(
  client: QueryClient,
  pr: RowPullRequest,
  scope: readonly unknown[],
): string | undefined {
  return newestRowSnapshots(client, [{ pr, scope }]).get(pr.url);
}

/**
 * {@link newestRowSnapshot} for every row at once, in one pass over the cache: a long list
 * asks for each of its rows every render, and its cache holds a batch per scroll position.
 */
export function newestRowSnapshots(
  client: QueryClient,
  rows: readonly { pr: RowPullRequest; scope: readonly unknown[] }[],
): Map<string, string | undefined> {
  // The rows under each read scope, by the member their batches' keys name them by.
  const scopes = new Map<string, { scope: readonly unknown[]; members: Map<string, string[]> }>();
  for (const { pr, scope } of rows) {
    if (scope.length === 0) continue;
    const id = JSON.stringify(scope);
    let entry = scopes.get(id);
    if (!entry) scopes.set(id, (entry = { scope, members: new Map() }));
    const member = pullRequestListStatsMember(pr).toLowerCase();
    entry.members.set(member, [...(entry.members.get(member) ?? []), pr.url]);
  }
  const newest = new Map<string, { fingerprint: string | undefined; at: number }>();
  if (scopes.size === 0) return new Map();
  for (const query of client.getQueryCache().getAll()) {
    const key = query.queryKey;
    for (const { scope, members } of scopes.values()) {
      if (!startsWith(key, scope) || key[scope.length] !== STATS_OPERATION) continue;
      // Not answered yet: it says nothing until it does.
      if (query.state.status === "pending") continue;
      const failed = query.state.status === "error";
      const read = query.state.data as PullRequestListStatsRead | undefined;
      const at = failed ? query.state.errorUpdatedAt : (read?.dispatchedAt ?? 0);
      // A Mac batch's key is its scope, the operation, "mac", its named reader, then its PRs.
      for (const part of key.slice(scope.length + 3)) {
        if (typeof part !== "string") continue;
        for (const url of members.get(part.toLowerCase()) ?? []) {
          const known = newest.get(url);
          if (known && at <= known.at) continue;
          const fingerprint =
            failed || !read?.reader ? undefined : read.stats.get(url)?.fingerprint;
          newest.set(url, { fingerprint, at });
        }
      }
    }
  }
  return new Map([...newest].map(([url, { fingerprint }]) => [url, fingerprint]));
}

/** The rows whose newest batch has a snapshot of their PR to follow. */
export function followableRowUrls(
  client: QueryClient,
  rows: readonly { pr: RowPullRequest; scope: readonly unknown[] }[],
): Set<string> {
  const snapshots = newestRowSnapshots(client, rows);
  return new Set(rows.flatMap(({ pr }) => (snapshots.get(pr.url) !== undefined ? [pr.url] : [])));
}

/** Whether the row's newest batch has a snapshot of its PR to follow. */
export function rowSnapshotFollowable(
  client: QueryClient,
  pr: RowPullRequest,
  scope: readonly unknown[],
): boolean {
  return newestRowSnapshot(client, pr, scope) !== undefined;
}

/** The parts of a row overview query's state the row's pace reads. */
export interface RowOverviewState {
  data:
    | { followed_snapshot?: string; dispatched_at?: number; pr?: { state?: string } | null }
    | undefined;
  error: unknown;
  dataUpdatedAt: number;
  errorUpdatedAt: number;
  fetchStatus?: string;
}

/** Whether the row's latest settled read failed: its failure pacing then decides, not snapshots. */
export function latestRowReadFailed(state: RowOverviewState): boolean {
  return state.error != null && state.errorUpdatedAt >= state.dataUpdatedAt;
}

/** Whether the row follows snapshots now: its latest answer followed one, and nothing failed since. */
export function rowFollowsSnapshot(state: RowOverviewState): boolean {
  return state.data?.followed_snapshot !== undefined && !latestRowReadFailed(state);
}

/** Where in the last 10 minutes of the hour `url`'s backstop falls: the same every time asked. */
export function rowBackstopStagger(url: string): number {
  const text = url.toLowerCase();
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash = Math.imul(hash ^ text.charCodeAt(index), 16777619);
  }
  // Mixed once more, so links that differ only in their number still land far apart.
  hash = Math.imul(hash ^ (hash >>> 16), 0x85ebca6b);
  hash = Math.imul(hash ^ (hash >>> 13), 0xc2b2ae35);
  hash ^= hash >>> 16;
  return Math.floor(((hash >>> 0) / 2 ** 32) * ROW_BACKSTOP_STAGGER_MS);
}

/**
 * How long after its last answer a followed row reads anyway: 10 minutes, or with `hourly`
 * between 50 and 60 minutes, by its link.
 */
export function rowBackstopAfter(url: string, hourly: boolean): number {
  return hourly ? ROW_HOURLY_BACKSTOP_MS - rowBackstopStagger(url) : ROW_SNAPSHOT_BACKSTOP_MS;
}

/**
 * How long until a followed row's backstop, counted from its last answer: absolute, so an
 * unchanged batch or a re-render never moves it. At least 1 ms once overdue.
 */
export function rowBackstopIn(
  state: RowOverviewState,
  now: number,
  backstopAfter = ROW_SNAPSHOT_BACKSTOP_MS,
): number {
  return Math.max(state.dataUpdatedAt + backstopAfter - now, 1);
}

/**
 * Whether a changed snapshot should read the row's overview now. Never while it reads, after
 * a failed read (its own backoff reads next, and picks up the newest snapshot), while its
 * reader is held, or for a batch read as another login than the row's own answer named.
 */
export function snapshotReadDue({
  state,
  snapshot,
  rowReader,
  held,
}: {
  state: RowOverviewState;
  snapshot: { fingerprint: string; reader: string; dispatchedAt: number } | undefined;
  /** The login the row's own overview was read as. */
  rowReader: string | null | undefined;
  /** The reader's cooldown, budget stop or an unfocused window holds automatic reads. */
  held: boolean;
}): boolean {
  if (!snapshot || !state.data || state.fetchStatus === "fetching" || held) return false;
  if (latestRowReadFailed(state)) return false;
  if (!rowReader || snapshot.reader.toLowerCase() !== rowReader.toLowerCase()) return false;
  const followed = state.data.followed_snapshot;
  // A row that follows nothing yet reads once to start following the batch.
  if (followed === undefined) return true;
  // A batch sent before the row's own last read knows nothing newer than that read.
  if (snapshot.dispatchedAt <= (state.data.dispatched_at ?? 0)) return false;
  return followed !== snapshot.fingerprint;
}
