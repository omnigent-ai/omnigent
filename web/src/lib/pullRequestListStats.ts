// Diff size, last-update time and status for the Pull Requests list. The host's PR
// payload doesn't carry them, so they're read in batches: one request per source per
// chunk of the visible list. A source is the session's host itself where it serves
// them, else the desktop shell's GitHub fast track as the host's account.

import type { QueryClient } from "@tanstack/react-query";

import { hasGitHubFastTrackBridge } from "@/lib/githubFastTrackBridge";
import type { GitHubFastTrackAccount } from "@/lib/nativeBridge";
import { namedCodeHostOf, type CodeHostProviderId } from "@/lib/codeHosts";

export const PULL_REQUEST_LIST_STATS_OPERATION = "pull_request_list_stats";

/** PRs per request; the shell accepts up to 50, and smaller batches keep each query light. */
export const PULL_REQUEST_LIST_STATS_BATCH_SIZE = 25;

/** One PR's stats as the shell answers them. */
export interface PullRequestListStatsEntry {
  repository: string;
  number: number;
  additions: number | null;
  deletions: number | null;
  changed_files: number | null;
  updated_at: string | null;
  head_sha: string | null;
  /** The PR's base commit; absent from shells that predate it. */
  base_sha?: string | null;
  /** Absent from shells that predate them; see {@link PullRequestListStats.extended}. */
  state?: string | null;
  is_draft?: boolean | null;
  ci?: string | null;
  ci_head_sha?: string | null;
  /**
   * What the row's overview shows, as far as the batch sees it: check counts by state, the
   * review decision and review totals. Only from newer shells, and only when complete.
   */
  snapshot?: {
    check_runs: Record<string, number>;
    status_contexts: Record<string, number>;
    review_decision: string | null;
    review_requests: number;
    latest_reviews: number;
  };
}

/** A head commit's check rollup, as the stats read reports it. */
export type PullRequestCiRollup = "passing" | "failing" | "pending";

const CI_ROLLUPS = new Set<string>(["passing", "failing", "pending"]);
const LIFECYCLE_STATES = new Set<string>(["OPEN", "CLOSED", "MERGED"]);

/** The stats a list row shows. */
export interface PullRequestListStats {
  additions: number | undefined;
  deletions: number | undefined;
  changedFiles: number | undefined;
  /** Epoch milliseconds of GitHub's `updatedAt`. */
  updatedAt: number | undefined;
  /** The PR's head when GitHub answered. */
  headSha: string | undefined;
  /** Its base then, where the shell reads it. */
  baseSha?: string;
  /** Client time the read was sent, which orders it against other status reads. */
  dispatchedAt: number;
  /** False from a shell that predates state, draft and CI; then the fields below are absent. */
  extended: boolean;
  state?: "OPEN" | "CLOSED" | "MERGED";
  isDraft?: boolean;
  /** Null means no known rollup (no checks, or none reported), never "passing". */
  ci?: PullRequestCiRollup | null;
  /** The commit the rollup belongs to. */
  ciHeadSha?: string;
  /**
   * The PR as its overview would show it, as one comparable string: head, update time, state,
   * draft and the batch's snapshot. Absent where the shell sent no complete snapshot.
   */
  fingerprint?: string;
}

/** Who reads a PR's stats: the GitHub fast track as the account the session's host reports. */
export type PullRequestListStatsSource =
  | { kind: "github-fast-track"; account: GitHubFastTrackAccount }
  /** A GitHub PR on `host`, read by the session's host as its own account. */
  | { kind: "host"; sessionId: string; host: string };

/** A PR the list wants stats for, and who reads them. */
export interface PullRequestListStatsTarget {
  url: string;
  source: PullRequestListStatsSource;
  repository: string;
  number: number;
  /** The head the host reports; stats read at another head are stale. */
  headSha: string | undefined;
  /** Merged and closed PRs rarely change, so a batch of only those stops polling. */
  open: boolean;
}

/** One request: PRs that share a source, at most {@link PULL_REQUEST_LIST_STATS_BATCH_SIZE}. */
export interface PullRequestListStatsBatch {
  source: PullRequestListStatsSource;
  targets: PullRequestListStatsTarget[];
}

function sourceId(source: PullRequestListStatsSource): string {
  if (source.kind === "host") return `host\n${source.sessionId}\n${source.host.toLowerCase()}`;
  return `fast-track\n${source.account.host}\n${source.account.login.toLowerCase()}`;
}

/** Group targets by source in first-seen order, then split each group into batches. */
export function pullRequestListStatsBatches(
  targets: readonly PullRequestListStatsTarget[],
  batchSize = PULL_REQUEST_LIST_STATS_BATCH_SIZE,
): PullRequestListStatsBatch[] {
  const bySource = new Map<string, PullRequestListStatsBatch>();
  for (const target of targets) {
    const key = sourceId(target.source);
    const group = bySource.get(key) ?? { source: target.source, targets: [] };
    group.targets.push(target);
    bySource.set(key, group);
  }
  return [...bySource.values()].flatMap((group) => {
    const batches: PullRequestListStatsBatch[] = [];
    for (let start = 0; start < group.targets.length; start += batchSize) {
      batches.push({
        source: group.source,
        targets: group.targets.slice(start, start + batchSize),
      });
    }
    return batches;
  });
}

const definedCount = (value: number | null | undefined) =>
  typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : undefined;

const text = (value: unknown) => (typeof value === "string" && value ? value : undefined);

/** The row-facing stats for one shell entry, read at `dispatchedAt`; undefined when GitHub had none. */
export function pullRequestListStats(
  entry: PullRequestListStatsEntry | null | undefined,
  dispatchedAt = 0,
): PullRequestListStats | undefined {
  if (!entry) return undefined;
  const updatedAt = entry.updated_at ? Date.parse(entry.updated_at) : Number.NaN;
  const stats: PullRequestListStats = {
    additions: definedCount(entry.additions),
    deletions: definedCount(entry.deletions),
    changedFiles: definedCount(entry.changed_files),
    updatedAt: Number.isFinite(updatedAt) ? updatedAt : undefined,
    headSha: text(entry.head_sha),
    ...(text(entry.base_sha) ? { baseSha: text(entry.base_sha) } : {}),
    dispatchedAt,
    // An older shell leaves the keys out entirely, unlike a PR with no rollup.
    extended: "state" in entry && "ci" in entry,
  };
  const fingerprint = snapshotFingerprint(entry);
  if (fingerprint) stats.fingerprint = fingerprint;
  if (!stats.extended) return stats;
  const state = text(entry.state);
  if (state && LIFECYCLE_STATES.has(state)) stats.state = state as PullRequestListStats["state"];
  if (typeof entry.is_draft === "boolean") stats.isDraft = entry.is_draft;
  stats.ci = entry.ci && CI_ROLLUPS.has(entry.ci) ? (entry.ci as PullRequestCiRollup) : null;
  const ciHeadSha = text(entry.ci_head_sha);
  if (ciHeadSha) stats.ciHeadSha = ciHeadSha;
  return stats;
}

const isCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 0;

/**
 * Counts by state in a fixed order, so two answers with the same counts compare equal; null
 * unless every entry is a state name and a count. An empty map, a head with no checks, counts.
 */
function sortedCounts(counts: unknown): [string, number][] | null {
  if (!counts || typeof counts !== "object" || Array.isArray(counts)) return null;
  const entries = Object.entries(counts as Record<string, unknown>);
  if (!entries.every(([state, count]) => state !== "" && isCount(count))) return null;
  return (entries as [string, number][]).sort(([a], [b]) => a.localeCompare(b));
}

/**
 * The entry's comparable snapshot, or undefined unless the shell sent all of it: every
 * snapshot field present and well formed, with the PR's head, update time, state and draft.
 * A decision GitHub left empty arrives as null; one left out means a partial answer.
 */
export function snapshotFingerprint(entry: PullRequestListStatsEntry): string | undefined {
  const snapshot: unknown = entry.snapshot;
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) return undefined;
  const fields = snapshot as Record<string, unknown>;
  const checkRuns = sortedCounts(fields.check_runs);
  const statusContexts = sortedCounts(fields.status_contexts);
  if (!checkRuns || !statusContexts) return undefined;
  const decision = fields.review_decision;
  if (
    !Object.hasOwn(fields, "review_decision") ||
    (decision !== null && typeof decision !== "string")
  )
    return undefined;
  if (!isCount(fields.review_requests) || !isCount(fields.latest_reviews)) return undefined;
  const updatedAt: unknown = entry.updated_at;
  if (typeof entry.head_sha !== "string" || !entry.head_sha) return undefined;
  if (typeof entry.state !== "string" || typeof entry.is_draft !== "boolean") return undefined;
  if (updatedAt !== null && typeof updatedAt !== "string") return undefined;
  return JSON.stringify([
    entry.head_sha,
    updatedAt,
    entry.state,
    entry.is_draft,
    checkRuns,
    statusContexts,
    decision,
    fields.review_requests,
    fields.latest_reviews,
  ]);
}

/** One batch read: the stats GitHub answered and the host heads the read started from. */
export interface PullRequestListStatsRead {
  stats: Map<string, PullRequestListStats>;
  /** Each PR's head as its host reported it when the read began. */
  hostHeads: Map<string, string | undefined>;
  /** The login GitHub says read the batch, from the same request, where it named one. */
  reader?: string;
  /** Client time the batch was sent. */
  dispatchedAt?: number;
}

/**
 * The batch's PRs whose host head moved since the read began. GitHub's own head can be
 * newer than the host's, so only a move on the host side makes the stats older.
 */
export function movedHeads(
  batch: PullRequestListStatsBatch,
  read: Pick<PullRequestListStatsRead, "hostHeads"> | undefined,
): string[] {
  if (!read) return [];
  return batch.targets.flatMap((target) =>
    read.hostHeads.has(target.url) && read.hostHeads.get(target.url) !== target.headSha
      ? [target.url]
      : [],
  );
}

/** How a batch's key names one of its PRs, e.g. `acme/app#7`. */
export function pullRequestListStatsMember(pr: { repository: string; number: number }): string {
  return `${pr.repository}#${pr.number}`;
}

/** A batch's cache identity: its source and its PRs, not their heads. */
export function pullRequestListStatsKey(batch: PullRequestListStatsBatch): readonly unknown[] {
  const pullRequests = batch.targets.map(pullRequestListStatsMember);
  const { source } = batch;
  if (source.kind === "host") {
    return [
      "pull-request-host",
      source.sessionId,
      PULL_REQUEST_LIST_STATS_OPERATION,
      source.host.toLowerCase(),
      ...pullRequests,
    ];
  }
  return [
    "github-fast-track",
    PULL_REQUEST_LIST_STATS_OPERATION,
    source.account.host,
    source.account.login,
    ...pullRequests,
  ];
}

/** Compact counts for the Diff column: 940, 2.2k, 12k. */
export function compactCount(value: number): string {
  if (value >= 10_000) return `${Math.round(value / 1000)}k`;
  if (value >= 1_000) return `${(value / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(value);
}

/** Where a row's stats would come from, for wording that names the right service. */
export type PullRequestListStatsService = CodeHostProviderId;

/**
 * Why a row's stats can't be shown. Loading isn't a gap: the cell stays blank then.
 * - `outdated`: this shell predates the read (update the app).
 * - `unavailable`: the shell can't read it here (platform, packaging or policy).
 * - `no-account`: the session's host answered without naming a GitHub account.
 * - `read-failed`: this row's own batch failed, with the shell's message.
 * - `not-reported`: the batch answered without this row.
 * - `unsupported-host`: the row's code host has no stats read.
 */
export type PullRequestListStatsGap =
  | { kind: "outdated" | "unavailable" | "not-reported"; service: PullRequestListStatsService }
  | { kind: "no-account" }
  | { kind: "unsupported-host" }
  | {
      kind: "read-failed";
      service: PullRequestListStatsService;
      code: string;
      message: string | undefined;
    };

/** Why the Diff and Updated headers can't sort until the list has stats. */
export const PULL_REQUEST_LIST_STATS_SORT_UNAVAILABLE =
  "Sorting needs diff sizes and update times, which aren't available yet.";

/** One field a row shows from its stats. */
export type PullRequestListStatsField = "diff" | "updated";

/** What a stats cell shows: the value, nothing yet, or a gap with its reason. */
export type PullRequestListStatsCell =
  | { kind: "value"; stats: PullRequestListStats }
  | { kind: "blank" }
  | { kind: "gap"; label: string; reason: string };

const FIELD_LABELS: Record<PullRequestListStatsField, string> = {
  diff: "Diff size unavailable",
  updated: "Update time unavailable",
};

function serviceName(service: PullRequestListStatsService): string {
  return service === "github" ? "GitHub" : (namedCodeHostOf(service)?.copy.label ?? service);
}

function hasField(stats: PullRequestListStats, field: PullRequestListStatsField): boolean {
  return field === "diff"
    ? stats.additions !== undefined && stats.deletions !== undefined
    : stats.updatedAt !== undefined;
}

/** The short reason a gap gives, in words true for its own service. */
export function pullRequestListStatsGapReason(gap: PullRequestListStatsGap): string {
  switch (gap.kind) {
    case "outdated":
      return "Update the app to see it here.";
    case "unavailable":
      return "This app can't read it on this Mac.";
    case "no-account":
      return "The session's host didn't report a GitHub account.";
    case "unsupported-host":
      return "This code host doesn't report it.";
    case "not-reported":
      return `${serviceName(gap.service)} didn't report it.`;
    case "read-failed":
      if (gap.service === "github" && gap.code === "rate_limited") {
        return "GitHub is limiting requests from this Mac.";
      }
      return gap.message ?? `Couldn't read it from ${serviceName(gap.service)}.`;
  }
}

/**
 * The cell for one field: the value when the row's stats carry it, a gap when a read
 * answered without it or couldn't run, and blank while a read may still answer.
 */
export function pullRequestListStatsCell(
  field: PullRequestListStatsField,
  stats: PullRequestListStats | undefined,
  gap: PullRequestListStatsGap | undefined,
  service: PullRequestListStatsService | undefined,
): PullRequestListStatsCell {
  if (stats && hasField(stats, field)) return { kind: "value", stats };
  const label = FIELD_LABELS[field];
  // The read answered, just not with this field.
  if (stats && service) {
    return { kind: "gap", label, reason: `${serviceName(service)} didn't report it.` };
  }
  if (gap) return { kind: "gap", label, reason: pullRequestListStatsGapReason(gap) };
  return { kind: "blank" };
}

/**
 * Whether the desktop shell's fast track could lay out the Diff and Updated columns, so a
 * value it can't read shows why. Elsewhere (the browser) only the session's host fills them.
 */
export function pullRequestListStatsDisplayed(): boolean {
  return hasGitHubFastTrackBridge();
}

/** A row's newest answered stats from any batch read, and who read it, when. */
export interface NewestListStats {
  stats: PullRequestListStats;
  dispatchedAt: number;
  reader?: string;
}

const startsWithKey = (key: readonly unknown[], prefix: readonly unknown[]) =>
  prefix.every((part, index) => key[index] === part);

/**
 * Each of `batch`'s rows' newest stats among every answered batch of the same source this
 * QueryClient holds, so a row keeps what any read found for it after its batch changed.
 * `accepts` says whether an answer's reader may still show, for a source whose reader can change.
 */
export function newestListStats(
  client: QueryClient,
  batch: PullRequestListStatsBatch,
  accepts: (read: PullRequestListStatsRead) => boolean = () => true,
): Map<string, NewestListStats> {
  const prefix = pullRequestListStatsKey({ source: batch.source, targets: [] });
  const wanted = new Set(batch.targets.map((target) => target.url));
  const newest = new Map<string, NewestListStats>();
  for (const query of client.getQueryCache().findAll({ queryKey: prefix })) {
    if (!startsWithKey(query.queryKey, prefix) || query.state.status !== "success") continue;
    const read = query.state.data as PullRequestListStatsRead | undefined;
    if (!read || read.dispatchedAt === undefined || !accepts(read)) continue;
    for (const [url, stats] of read.stats) {
      if (!wanted.has(url)) continue;
      const known = newest.get(url);
      if (known && known.dispatchedAt >= read.dispatchedAt) continue;
      newest.set(url, {
        stats,
        dispatchedAt: read.dispatchedAt,
        ...(read.reader ? { reader: read.reader } : {}),
      });
    }
  }
  return newest;
}

/**
 * When a windowed list's batch reads next: at once for a row nothing has answered, else when
 * its open rows' newest answer turns `freshForMs` old. Finished rows don't need re-reading.
 */
export function listStatsDueAt(
  batch: PullRequestListStatsBatch,
  newest: ReadonlyMap<string, NewestListStats>,
  freshForMs: number,
): number {
  let due = Number.POSITIVE_INFINITY;
  for (const target of batch.targets) {
    const answered = newest.get(target.url);
    if (!answered) return 0;
    if (target.open) due = Math.min(due, answered.dispatchedAt + freshForMs);
  }
  return due;
}
