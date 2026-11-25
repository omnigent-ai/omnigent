import {
  hashKey,
  skipToken,
  useQueries,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  desktopCodeHostStatsSupport,
  desktopRowSnapshotsSupport,
  queryCodeHost,
} from "@/lib/codeHosts";
import {
  DesktopPullRequestError,
  readOnThisMac,
  type MacReadAnswer,
} from "@/lib/desktopPullRequests";
import { githubFastTrackSupport, queryGitHubFastTrack } from "@/lib/githubFastTrack";
import { getCurrentUserId } from "@/lib/identity";
import {
  StaleMacReadError,
  acceptMacRead,
  beginMacRead,
  macReadScopeKey,
  refuseMacReadSentAt,
  showsMacReader,
  type MacReadScopeKey,
  useMacReadScopeVersion,
} from "@/lib/macReadScope";
import { hasCodeHostBridge, hasGitHubFastTrackBridge } from "@/lib/nativeBridge";
import { githubCooldownKey, githubCooldowns, githubReaderCooldownKey } from "@/lib/githubCooldown";
import {
  automaticReadsAllowed,
  automaticReadsResumeAt,
  useAutomaticReadsVersion,
} from "@/lib/githubAutomaticReads";
import { githubPollIdle } from "@/lib/githubPollIdle";
import { PullRequestApiError, readPullRequestRpc } from "@/lib/pullRequestRpc";
import { publishPullRequestSummaries, type PullRequestSummaryRow } from "@/lib/pullRequestRecord";
import {
  FAILED_READ_POLL_MS,
  MAX_FAILED_READ_POLL_MS,
  PULL_REQUEST_LIST_POLL_MS,
  consecutiveReadFailures,
  pacedPollInterval,
} from "@/lib/pullRequestPolling";
import {
  PULL_REQUEST_LIST_STATS_OPERATION,
  listStatsDueAt,
  newestListStats,
  type NewestListStats,
  pullRequestListStats,
  pullRequestListStatsBatches,
  pullRequestListStatsKey,
  pullRequestListStatsMember,
  pullRequestListStatsService,
  movedHeads,
  type PullRequestListStats,
  type PullRequestListStatsBatch,
  type PullRequestListStatsEntry,
  type PullRequestListStatsGap,
  type PullRequestListStatsRead,
  type PullRequestListStatsSource,
  type PullRequestListStatsTarget,
} from "@/lib/pullRequestListStats";
import type { GitHubFastTrackResult } from "@/lib/nativeBridge";

/**
 * The list's stats overall, which gates the size and update-time orders: `unsupported`
 * where no source the rows need is offered (browser, older shells, or no row has a
 * reader), and `failed` when every request failed (signed out, rate limited).
 */
export type PullRequestListStatsStatus = "unsupported" | "loading" | "ready" | "failed";

/** A row's newest answered snapshot, read as `reader` and sent at `dispatchedAt`. */
export interface PullRequestListSnapshot {
  fingerprint: string;
  reader: string;
  dispatchedAt: number;
}

export interface PullRequestListStatsResult {
  status: PullRequestListStatsStatus;
  /**
   * Each row's snapshot from its own batch's settled answer: never a placeholder standing in
   * while a new batch reads, and only where the answer named its reader.
   */
  snapshots?: ReadonlyMap<string, PullRequestListSnapshot>;
  /** Stats by PR URL, for the rows GitHub answered. */
  byUrl: ReadonlyMap<string, PullRequestListStats>;
  /** Why a target has no stats, from its own batch; absent while it may still answer. */
  gaps: ReadonlyMap<string, PullRequestListStatsGap>;
}

/**
 * A batch the shell answered with an error. `shellMessage` is the shell's own text for
 * that source, and absent when the client refused the read before sending it.
 */
export class PullRequestListStatsReadError extends Error {
  readonly code: string;
  readonly shellMessage: string | undefined;

  constructor(code: string, shellMessage?: string) {
    super(code);
    this.code = code;
    this.shellMessage = shellMessage;
  }
}

interface StatsAnswer {
  reader?: unknown;
  pull_requests: (PullRequestListStatsEntry | null)[];
}

function statsParams(batch: PullRequestListStatsBatch) {
  return {
    pull_requests: batch.targets.map((target) => ({
      repository: target.repository,
      number: target.number,
    })),
  };
}

function parseRead(
  batch: PullRequestListStatsBatch,
  answer: StatsAnswer | undefined,
  hostHeads: Map<string, string | undefined>,
  dispatchedAt: number,
): PullRequestListStatsRead {
  const answered = Array.isArray(answer?.pull_requests) ? answer.pull_requests : [];
  const stats = new Map<string, PullRequestListStats>();
  batch.targets.forEach((target, index) => {
    const entry = pullRequestListStats(answered[index], dispatchedAt);
    if (entry) stats.set(target.url, entry);
  });
  // Who GitHub says read it, kept for every source, so a row follows only its own reader's answer.
  const reader = typeof answer?.reader === "string" && answer.reader ? answer.reader : undefined;
  return { stats, hostHeads, dispatchedAt, ...(reader ? { reader } : {}) };
}

/** What a batch's stats say of each PR's lifecycle, for its summary. */
function statsSummaryRows(
  stats: ReadonlyMap<string, PullRequestListStats>,
): PullRequestSummaryRow[] {
  return [...stats].map(([url, entry]) => ({
    url,
    fields: {
      ...(entry.state ? { state: entry.state } : {}),
      ...(entry.isDraft !== undefined ? { isDraft: entry.isDraft } : {}),
      ...(entry.updatedAt !== undefined ? { updatedAt: entry.updatedAt } : {}),
      ...(entry.baseSha && entry.headSha
        ? {
            listedCommits: {
              base: entry.baseSha,
              head: entry.headSha,
              readAt: entry.dispatchedAt,
            },
          }
        : {}),
    },
  }));
}

/**
 * Read one batch. A Mac read is numbered in its scope before it's sent and kept
 * only as the reader its own answer names; a newer reader drops the scope's
 * other cached answers, and an answer that predates a change of reader is refused.
 */
async function readBatch(
  batch: PullRequestListStatsBatch,
  queryClient: QueryClient,
  queryKey: readonly unknown[],
): Promise<PullRequestListStatsRead> {
  const hostHeads = new Map(batch.targets.map((target) => [target.url, target.headSha]));
  const { source } = batch;
  // As the reader the rows' own overviews read as, through the same Mac read and its checks.
  if (source.kind === "mac") {
    const dispatchedAt = Date.now();
    let answer: MacReadAnswer<StatsAnswer>;
    try {
      // Rows' stats, a bulk read: they leave the shell's last read slot to a PR the user opens.
      answer = await readOnThisMac<StatsAnswer>(
        source.target,
        PULL_REQUEST_LIST_STATS_OPERATION,
        statsParams(batch),
        { priority: "background" },
      );
    } catch (error) {
      if (error instanceof DesktopPullRequestError) {
        throw new PullRequestListStatsReadError(error.code, error.message);
      }
      throw error;
    }
    const reader = answer.data?.reader;
    // The batch names who read it, and that must be who the shell proved read it.
    if (
      typeof reader !== "string" ||
      !reader ||
      (answer.viewer !== null && reader.toLowerCase() !== answer.viewer.toLowerCase())
    ) {
      throw new PullRequestListStatsReadError("reader_unknown");
    }
    const read = parseRead(batch, answer.data, hostHeads, dispatchedAt);
    // Only this Mac read's own answer says, as the reader it proved; spoke and fast-track reads don't.
    publishPullRequestSummaries(queryClient, answer, statsSummaryRows(read.stats), dispatchedAt);
    return { ...read, reader };
  }
  if (source.kind === "host") return readOnHost(batch, source.sessionId, hostHeads);
  if (source.kind === "github-fast-track") {
    // This route doesn't pass through queryCodeHost, so it waits out and settles the reader's
    // cooldown itself, as every other read as that account does.
    const cooldownKey = githubCooldownKey(source.account.host, source.account.login);
    const refusal = githubCooldowns.refusal(cooldownKey);
    if (refusal) throw new PullRequestListStatsReadError(refusal.code, refusal.message);
    const dispatchedAt = Date.now();
    const result = await queryGitHubFastTrack<StatsAnswer>(
      PULL_REQUEST_LIST_STATS_OPERATION,
      source.account,
      statsParams(batch),
    );
    if (!result.ok && result.error.code === "rate_limited") {
      githubCooldowns.rateLimited(
        cooldownKey,
        result.error.retryAt ?? null,
        result.error.limit ?? null,
      );
    } else githubCooldowns.answered(cooldownKey);
    if (!result.ok)
      throw new PullRequestListStatsReadError(result.error.code, result.error.message);
    return parseRead(batch, result.data, hostHeads, dispatchedAt);
  }
  const spoke = source;
  // The scope the query is cached under, workspace user included: never one with no user.
  const scope = queryKey.slice(0, 4) as unknown as MacReadScopeKey;
  const userId = scope[1];
  // Including a retry for a user who has since signed out or switched.
  if (!userId || userId !== getCurrentUserId()) throw new Error("signed_out");
  const ticket = beginMacRead(scope);
  const dispatchedAt = Date.now();
  const result: GitHubFastTrackResult<StatsAnswer> = await queryCodeHost<StatsAnswer>(
    {
      provider: spoke.provider,
      host: spoke.host,
      operation: PULL_REQUEST_LIST_STATS_OPERATION,
      params: statsParams(batch),
    },
    { priority: "background" },
  );
  // The workspace user changed while this read ran: it isn't theirs to keep.
  if (getCurrentUserId() !== userId) throw new StaleMacReadError();
  if (!result.ok) {
    // Main can't read as the spoke's account anymore: what was read as it goes.
    refuseMacReadSentAt(scope, ticket, result.error.code);
    throw new PullRequestListStatsReadError(result.error.code, result.error.message);
  }
  const reader = result.data?.reader;
  // Main answers `reader_unknown` instead; a malformed answer is refused the same way.
  if (typeof reader !== "string" || !reader)
    throw new PullRequestListStatsReadError("reader_unknown");
  const verdict = acceptMacRead(ticket, reader);
  if (verdict === "rejected") throw new StaleMacReadError();
  if (verdict === "transition") {
    const own = hashKey(queryKey);
    queryClient.removeQueries({
      queryKey: scope,
      predicate: (query) => query.queryHash !== own,
    });
  }
  return { ...parseRead(batch, result.data, hostHeads, dispatchedAt), reader };
}

/**
 * Read a batch through the session's host, as its own GitHub account. It answers the PRs it
 * found by repository and number, leaving out the ones it can't see.
 */
async function readOnHost(
  batch: PullRequestListStatsBatch,
  sessionId: string,
  hostHeads: Map<string, string | undefined>,
): Promise<PullRequestListStatsRead> {
  const dispatchedAt = Date.now();
  let answer: { reader?: unknown; data?: unknown };
  try {
    answer = await readPullRequestRpc(sessionId, "getPullRequestListStats", (client) =>
      client.getPullRequestListStats({ prUrls: batch.targets.map((target) => target.url) }),
    );
  } catch (error) {
    if (error instanceof PullRequestApiError) {
      throw new PullRequestListStatsReadError(`http_${error.status}`, error.message);
    }
    throw error;
  }
  const found = new Map<string, PullRequestListStatsEntry>();
  for (const entry of Array.isArray(answer.data) ? answer.data : []) {
    const row = entry as Partial<PullRequestListStatsEntry> | null;
    if (typeof row?.repository !== "string" || typeof row.number !== "number") continue;
    found.set(
      pullRequestListStatsMember(row as PullRequestListStatsEntry).toLowerCase(),
      row as PullRequestListStatsEntry,
    );
  }
  const inOrder = batch.targets.map(
    (target) => found.get(pullRequestListStatsMember(target).toLowerCase()) ?? null,
  );
  return parseRead(
    batch,
    { reader: answer.reader, pull_requests: inOrder },
    hostHeads,
    dispatchedAt,
  );
}

type SupportLevel = "supported" | "outdated" | "unavailable";

interface SourceSupport {
  /** Undefined until the shell says. */
  fastTrack: SupportLevel | undefined;
  gitSpoke: SupportLevel | undefined;
  mac: SupportLevel | undefined;
  /** Still asking a shell that's probably capable, so its rows stay blank, not a gap. */
  fastTrackChecking: boolean;
  gitSpokeChecking: boolean;
  macChecking: boolean;
}

const rowSnapshotsSupportQuery = {
  queryKey: ["code-host", "github", "row-snapshots-support"],
  queryFn: desktopRowSnapshotsSupport,
  staleTime: Infinity,
  // Kept while no list shows, so a list mounting again follows its rows at once.
  gcTime: Infinity,
  retry: false,
} as const;

/**
 * Whether this shell reads list stats a row read on this Mac can follow. Until it says, and on
 * older shells, such rows join no batch and keep polling as before.
 */
export function useRowSnapshotsSupported(): boolean {
  return useQuery(rowSnapshotsSupportQuery).data === "supported";
}

/** Whether the shell reads a source's stats; GitSpoke only where answers name their reader. */
function useSourceSupport(): SourceSupport {
  const fastTrack = useQuery({
    queryKey: ["github-fast-track", "support", PULL_REQUEST_LIST_STATS_OPERATION],
    queryFn: () => githubFastTrackSupport(PULL_REQUEST_LIST_STATS_OPERATION),
    staleTime: Infinity,
    retry: false,
  });
  const gitSpoke = useQuery({
    queryKey: ["code-host", "gitspoke", "stats-support"],
    queryFn: () => desktopCodeHostStatsSupport("gitspoke"),
    staleTime: Infinity,
    retry: false,
  });
  const mac = useQuery(rowSnapshotsSupportQuery);
  const fastTrackLevel = fastTrack.data;
  const gitSpokeLevel = gitSpoke.data;
  const macLevel = mac.data;
  const fastTrackChecking = fastTrack.isPending && hasGitHubFastTrackBridge();
  const gitSpokeChecking = gitSpoke.isPending && hasCodeHostBridge();
  const macChecking = mac.isPending && hasCodeHostBridge();
  return useMemo(
    () => ({
      fastTrack: fastTrackLevel,
      gitSpoke: gitSpokeLevel,
      mac: macLevel,
      fastTrackChecking,
      gitSpokeChecking,
      macChecking,
    }),
    [fastTrackLevel, gitSpokeLevel, macLevel, fastTrackChecking, gitSpokeChecking, macChecking],
  );
}

// A host source is chosen only where the session's host serves the read.
const supportLevel = (support: SourceSupport, source: PullRequestListStatsSource) =>
  source.kind === "host"
    ? "supported"
    : source.kind === "github-fast-track"
      ? support.fastTrack
      : source.kind === "mac"
        ? support.mac
        : support.gitSpoke;

const supports = (support: SourceSupport, source: PullRequestListStatsSource) =>
  supportLevel(support, source) === "supported";

const stillChecking = (support: SourceSupport, source: PullRequestListStatsSource) =>
  source.kind === "host"
    ? false
    : source.kind === "github-fast-track"
      ? support.fastTrackChecking
      : source.kind === "mac"
        ? support.macChecking
        : support.gitSpokeChecking;

/**
 * The GitHub reader a batch's polls pace on: a fast-track account, or the login a Mac batch
 * reads as, named or Automatic's once it's proven. GitSpoke reads as a certificate, by no login.
 */
export function listStatsReaderKey(source: PullRequestListStatsSource): string | null {
  if (source.kind === "github-fast-track") {
    return githubCooldownKey(source.account.host, source.account.login);
  }
  if (source.kind === "mac" && source.target.provider === "github") {
    return githubReaderCooldownKey(source.target.host, source.target.readAs ?? null);
  }
  return null;
}

/** Whether a batch reads now: Mac reads at once, fast-track ones once their rows settle. */
function readsNow(
  batch: PullRequestListStatsBatch,
  enabled: boolean,
  fastTrackSettled: boolean,
): boolean {
  if (!enabled) return false;
  // Mac reads belong to a workspace user; with none known, nothing is read.
  // The host's batches, like the fast track's, read the heads the rows answered.
  return batch.source.kind === "github-fast-track" || batch.source.kind === "host"
    ? fastTrackSettled
    : getCurrentUserId() !== null;
}

/**
 * Diff size, last update and status for the list's PRs, one request per source per
 * batch. `enabled` false (a hidden panel) pauses reads and polling and keeps what was
 * read. Fast-track batches wait while any row that could join them is still answering
 * (`fastTrackSettled` false), so a refresh that moves several heads re-reads once;
 * Mac reads need nothing from the rows and start at once.
 */
export function usePullRequestListStats(
  targets: readonly PullRequestListStatsTarget[],
  {
    enabled,
    fastTrackSettled,
    followsWindow = false,
    readUrls,
    idlePaced = false,
  }: {
    enabled: boolean;
    fastTrackSettled: boolean;
    /** With `followsWindow`: the rows whose batches read; every target shows its newest stats. */
    readUrls?: ReadonlySet<string>;
    /**
     * The targets are a windowed list's read targets, which change as it scrolls: a batch reads
     * only when one of its rows has no answer or a stale one, at most once per
     * {@link WINDOW_BATCH_SPACING_MS}, and each row shows the newest answer any batch found.
     */
    followsWindow?: boolean;
    /** Its batches read 4 times less often while nobody uses the window. */
    idlePaced?: boolean;
  },
): PullRequestListStatsResult {
  const queryClient = useQueryClient();
  const support = useSourceSupport();
  // A change of reader re-renders, so answers cached as another one stop showing.
  useMacReadScopeVersion();
  const batches = useMemo(
    () =>
      pullRequestListStatsBatches(
        followsWindow && readUrls ? targets.filter((target) => readUrls.has(target.url)) : targets,
      ),
    [followsWindow, readUrls, targets],
  );
  // Every target of a windowed list, a source at a time, to show its newest stats from.
  const shownBySource = useMemo(
    () => (followsWindow ? pullRequestListStatsBatches(targets, Number.POSITIVE_INFINITY) : []),
    [followsWindow, targets],
  );
  const lastSentAt = useRef(Number.NEGATIVE_INFINITY);
  // Batches a Refresh asked to read, until their read starts.
  const refreshes = useRef(new Set<string>());
  const [, setWake] = useState(0);
  // The app's own reads waiting on a budget, cooldown or focus resume as soon as they may.
  useAutomaticReadsVersion();
  // A windowed list's batches slow while nobody uses the window, and are due again at first input.
  useEffect(
    () =>
      followsWindow && idlePaced
        ? githubPollIdle.subscribe(() => setWake((value) => value + 1))
        : undefined,
    [followsWindow, idlePaced],
  );
  const now = Date.now();
  const windowed = followsWindow
    ? oneReadPerSource(
        batches,
        batches.map((batch) =>
          windowedBatch(
            queryClient,
            batch,
            lastSentAt.current,
            refreshes.current.has(hashKey(pullRequestListStatsKey(batch))),
            now,
            idlePaced ? githubPollIdle.factor() : 1,
          ),
        ),
      )
    : null;
  const shown = followsWindow
    ? shownBySource.map((group) => newestListStats(queryClient, group, acceptsReaderOf(group)))
    : null;
  const checking = batches.some(
    (batch) => !supports(support, batch.source) && stillChecking(support, batch.source),
  );
  // Stable while the batches and support stand, so the result keeps its identity between renders
  // until a batch's read changes.
  const combine = useCallback(
    (results: UseQueryResult<PullRequestListStatsRead>[]): PullRequestListStatsResult => {
      const byUrl = new Map<string, PullRequestListStats>();
      const gaps = new Map<string, PullRequestListStatsGap>();
      const snapshots = new Map<string, PullRequestListSnapshot>();
      // When the answer each row shows was sent, so a newer one from another batch replaces it.
      const shownAt = new Map<string, number>();
      // Each target's gap comes from its own batch, so one source's error never names another's rows.
      const markAll = (batch: PullRequestListStatsBatch, gap: PullRequestListStatsGap) => {
        for (const target of batch.targets) gaps.set(target.url, gap);
      };
      // A windowed batch its source's failure holds back shows that failure, not a read to come.
      const heldBy = (result: UseQueryResult<PullRequestListStatsRead>, index: number) =>
        !result.isError && (result.data === undefined || result.isPlaceholderData)
          ? (windowed?.[index]?.failure ?? null)
          : null;
      results.forEach((result, index) => {
        const batch = batches[index];
        const service = pullRequestListStatsService(batch.source);
        const level = supportLevel(support, batch.source);
        if (level !== "supported") {
          if (level !== undefined && !stillChecking(support, batch.source)) {
            markAll(batch, { kind: level, service });
          }
          return;
        }
        const held = heldBy(result, index);
        if (result.isError || held) {
          // A read refused before sending (stale reader, no workspace user) explains nothing.
          const error = held ?? result.error;
          if (error instanceof PullRequestListStatsReadError && error.shellMessage !== undefined) {
            markAll(batch, {
              kind: "read-failed",
              service,
              code: error.code,
              message: error.shellMessage,
            });
          }
          return;
        }
        if (!result.data) return;
        // A Mac read shows only as the reader its scope keeps now.
        if (
          batch.source.kind === "code-host" &&
          !showsMacReader(
            macReadScopeKey(batch.source.provider, batch.source.host),
            result.data.reader,
          )
        ) {
          return;
        }
        for (const [url, stats] of result.data.stats) {
          byUrl.set(url, stats);
          shownAt.set(url, result.data.dispatchedAt ?? Number.NEGATIVE_INFINITY);
        }
        // The previous batch's answer stands in while a new row's read runs.
        if (result.isPlaceholderData) return;
        const { reader, dispatchedAt } = result.data;
        if (reader && dispatchedAt !== undefined) {
          for (const [url, stats] of result.data.stats) {
            if (stats.fingerprint)
              snapshots.set(url, { fingerprint: stats.fingerprint, reader, dispatchedAt });
          }
        }
        for (const target of batch.targets) {
          if (!result.data.stats.has(target.url))
            gaps.set(target.url, { kind: "not-reported", service });
        }
      });
      // A windowed list's rows show the newest answer any of its batches found.
      for (const each of shown ?? []) {
        for (const [url, newest] of each) {
          const newer = newest.dispatchedAt > (shownAt.get(url) ?? Number.NEGATIVE_INFINITY);
          if (newer || !byUrl.has(url)) byUrl.set(url, newest.stats);
          if (newer) {
            shownAt.set(url, newest.dispatchedAt);
            snapshots.delete(url);
          }
          if (!snapshots.has(url) && newest.reader && newest.stats.fingerprint) {
            snapshots.set(url, {
              fingerprint: newest.stats.fingerprint,
              reader: newest.reader,
              dispatchedAt: newest.dispatchedAt,
            });
          }
        }
      }
      const status = combinedStatus(
        results.map((result, index) =>
          heldBy(result, index) ? { isError: true, data: undefined } : result,
        ),
        batches,
        support,
        checking,
      );
      return {
        status: status === "loading" && windowed && byUrl.size > 0 ? "ready" : status,
        byUrl,
        gaps,
        snapshots,
      };
    },
    [batches, support, checking, windowed, shown],
  );
  const statsResult = useQueries({
    queries: batches.map((batch) => ({
      queryKey: pullRequestListStatsKey(batch),
      queryFn: supports(support, batch.source)
        ? ({ queryKey }: { queryKey: readonly unknown[] }) => {
            lastSentAt.current = Date.now();
            refreshes.current.delete(hashKey(queryKey));
            return readBatch(batch, queryClient, queryKey);
          }
        : skipToken,
      enabled:
        readsNow(batch, enabled, fastTrackSettled) &&
        (windowed === null || windowed[batches.indexOf(batch)]!.readsNow),
      // The list's cadence: open PRs every 30 seconds, finished ones only on demand, paced like
      // every GitHub poll by failures, cooldowns, the reader's budget and window focus, and
      // slower while nobody uses the window.
      staleTime: PULL_REQUEST_LIST_POLL_MS,
      // A Mac batch says which rows follow it, so it stays cached as long as their overviews.
      ...(batch.source.kind === "mac" ? { gcTime: 30 * 60_000 } : {}),
      // A windowed list's batches read when they fall due, worked out below, not on an interval.
      refetchInterval:
        windowed !== null
          ? false
          : (query: Parameters<typeof pacedPollInterval>[0]) =>
              pacedPollInterval(
                query,
                batch.targets.some((target) => target.open) ? PULL_REQUEST_LIST_POLL_MS : false,
                listStatsReaderKey(batch.source),
                { idlePaced },
              ),
      refetchIntervalInBackground: false,
      // A new row changes the batch; keep showing the last answer until the next one.
      placeholderData: (previous: PullRequestListStatsRead | undefined) => previous,
      // An answer refused for predating a change of reader reads again once, as the new one.
      retry: (failures: number, error: Error) => error instanceof StaleMacReadError && failures < 1,
      retryDelay: 0,
    })),
    combine,
  });

  // A push moves a PR's head on its host: re-read that batch once. A read already in
  // flight (a poll, or the one re-enabling starts) is joined, not restarted, and a failed
  // read waits for the next poll rather than retrying here.
  const heads = targets.map((target) => `${target.url}@${target.headSha ?? ""}`).join("\n");
  // After useQueries, so a batch it just re-enabled counts as active and as fetching.
  useEffect(() => {
    // A windowed list's moved heads make their batch due instead, under its source's bound.
    if (followsWindow) return;
    for (const batch of batches) {
      if (!supports(support, batch.source) || !readsNow(batch, enabled, fastTrackSettled)) continue;
      const key = pullRequestListStatsKey(batch);
      const state = queryClient.getQueryState<PullRequestListStatsRead>(key);
      if (!state || state.fetchStatus === "fetching" || state.status === "error") continue;
      if (movedHeads(batch, state.data).length === 0) continue;
      void queryClient.refetchQueries({ queryKey: key, exact: true }, { cancelRefetch: false });
    }
  }, [enabled, fastTrackSettled, followsWindow, support, batches, heads, queryClient, statsResult]);

  // A windowed list works its batches out again when one of its reads settles, wherever it ran.
  useEffect(() => {
    if (!followsWindow) return;
    return queryClient.getQueryCache().subscribe((event) => {
      if (event.type !== "updated") return;
      if (!event.query.queryKey.includes(PULL_REQUEST_LIST_STATS_OPERATION)) return;
      // A Refresh invalidates the batches it means to read again.
      if (event.action.type === "invalidate") refreshes.current.add(event.query.queryHash);
      // Any change, a read cancelled included, may end what held the next batch.
      setWake((value) => value + 1);
    });
  }, [followsWindow, queryClient]);
  // A batch that fell due reads now: enabling it alone reads only data its cache counts stale,
  // and an answer lands after its batch was sent, so it wouldn't be yet.
  const dueKeys = windowed
    ? batches.flatMap((batch, index) =>
        windowed[index]!.readsNow && readsNow(batch, enabled, fastTrackSettled)
          ? [pullRequestListStatsKey(batch)]
          : [],
      )
    : [];
  const dueSignature = dueKeys.map((key) => hashKey(key)).join("\n");
  useEffect(() => {
    for (const key of dueKeys) {
      if (queryClient.getQueryState(key)?.fetchStatus === "fetching") continue;
      void queryClient.refetchQueries({ queryKey: key, exact: true }, { cancelRefetch: false });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the due batches, by their keys.
  }, [dueSignature, queryClient]);
  // And when the next one falls due; set as it renders, so a render a read's answer caused sets it too.
  const wakeAt = windowed ? Math.min(...windowed.map((each) => each.wakeAt)) : Infinity;
  useLayoutEffect(() => {
    if (!Number.isFinite(wakeAt)) return;
    const timer = setTimeout(() => setWake((value) => value + 1), Math.max(0, wakeAt - Date.now()));
    return () => clearTimeout(timer);
  }, [wakeAt]);

  // A windowed list works out its reads afresh each render, so keep the last result while it
  // reads the same: a new one every render re-derives every row and loops the list's updates.
  const kept = useRef(statsResult);
  if (!sameListStatsResult(kept.current, statsResult)) kept.current = statsResult;
  return kept.current;
}

/** Whether a cached answer of `batch`'s source may still show: a Mac read only as its reader now. */
function acceptsReaderOf(batch: PullRequestListStatsBatch) {
  const source = batch.source;
  return (read: PullRequestListStatsRead) =>
    source.kind === "code-host"
      ? showsMacReader(macReadScopeKey(source.provider, source.host), read.reader)
      : true;
}

/** The least time between a windowed list's batch reads, so a scroll sends a few, not many. */
export const WINDOW_BATCH_SPACING_MS = 5_000;

/**
 * One read at a time for each source, however many batches its rows in view make: of the
 * batches due together, the most overdue reads, and the others wait for it to settle and for
 * the spacing after it. A Refresh's batches all read, and hold the source's others back.
 */
function oneReadPerSource<
  T extends { readsNow: boolean; wakeAt: number; dueAt: number; refreshed: boolean },
>(batches: readonly PullRequestListStatsBatch[], windowed: T[]): T[] {
  const chosen = new Map<string, number>();
  windowed.forEach((each, index) => {
    if (!each.readsNow) return;
    const source = hashKey(
      pullRequestListStatsKey({ source: batches[index]!.source, targets: [] }),
    );
    const current = chosen.get(source);
    if (each.refreshed) {
      chosen.set(source, -1);
      return;
    }
    if (current === -1) return;
    if (current === undefined || each.dueAt < windowed[current]!.dueAt) chosen.set(source, index);
  });
  return windowed.map((each, index) => {
    if (!each.readsNow || each.refreshed) return each;
    const source = hashKey(
      pullRequestListStatsKey({ source: batches[index]!.source, targets: [] }),
    );
    // The one waiting is woken by the cache once the source's read settles.
    return chosen.get(source) === index ? each : { ...each, readsNow: false, wakeAt: Infinity };
  });
}

/**
 * A source's stats reads across all its batches, whatever rows a scroll put in them: whether one
 * is in flight, even one no row watches any more, and the failures since its last answer.
 */
function sourceReads(client: QueryClient, batch: PullRequestListStatsBatch) {
  const prefix = pullRequestListStatsKey({ source: batch.source, targets: [] });
  const queries = client
    .getQueryCache()
    .findAll({ queryKey: prefix })
    .filter((query) => prefix.every((part, index) => query.queryKey[index] === part));
  const inFlight = queries.some((query) => query.state.fetchStatus === "fetching");
  const settledAt = (query: (typeof queries)[number]) =>
    Math.max(query.state.dataUpdatedAt, query.state.errorUpdatedAt);
  let failures = 0;
  let failedAt = 0;
  let lastError: Error | null = null;
  for (const query of [...queries].sort((a, b) => settledAt(b) - settledAt(a))) {
    if (settledAt(query) === 0) continue;
    if (query.state.status !== "error" || query.state.errorUpdatedAt < query.state.dataUpdatedAt)
      break;
    failures += Math.max(1, consecutiveReadFailures(query));
    failedAt = Math.max(failedAt, query.state.errorUpdatedAt);
    lastError ??= query.state.error;
  }
  return { inFlight, failures, failedAt, lastError };
}

const failureBackoffMs = (failures: number) =>
  Math.min(FAILED_READ_POLL_MS * 2 ** (failures - 1), MAX_FAILED_READ_POLL_MS);

/**
 * Whether a windowed list's batch reads now, what each of its rows already shows, and when to
 * look again. A Refresh reads at once. Otherwise a source reads one batch at a time, and after
 * a failure waits as a failed poll would, whichever rows its next batch holds; and nothing
 * reads while its reader's budget, cooldown or an unfocused window holds the app's own reads.
 */
function windowedBatch(
  client: QueryClient,
  batch: PullRequestListStatsBatch,
  lastSentAt: number,
  refreshed: boolean,
  now: number,
  /** How many times slower than usual the batch reads, while nobody uses the window. */
  paceFactor: number,
): {
  newest: Map<string, NewestListStats>;
  readsNow: boolean;
  wakeAt: number;
  /** When the batch fell due, or falls due; the most overdue of a source's batches goes first. */
  dueAt: number;
  refreshed: boolean;
  /** The source's latest failure, while it holds this batch's first read back. */
  failure: Error | null;
} {
  const source = batch.source;
  const newest = newestListStats(client, batch, acceptsReaderOf(batch));
  let dueAt = listStatsDueAt(batch, newest, PULL_REQUEST_LIST_POLL_MS * paceFactor);
  // A push moved a head since the batch's last answer: it reads again once, as soon as it may.
  const state = client.getQueryState<PullRequestListStatsRead>(pullRequestListStatsKey(batch));
  if (state?.status === "success" && movedHeads(batch, state.data).length > 0) dueAt = 0;
  const reads = sourceReads(client, batch);
  if (reads.failures > 0)
    dueAt = Math.max(dueAt, reads.failedAt + failureBackoffMs(reads.failures));
  // A read in flight wakes the list when it settles.
  const inFlight = reads.inFlight;
  const spacedAt = lastSentAt + WINDOW_BATCH_SPACING_MS;
  const due = refreshed || (!inFlight && now >= dueAt && now >= spacedAt);
  const readerKey = listStatsReaderKey(source);
  const allowed = refreshed || automaticReadsAllowed(readerKey);
  // A held read wakes when its hold ends on its own; a focus pause re-renders when it lifts.
  const resumesAt = due && !allowed ? automaticReadsResumeAt(readerKey) : null;
  return {
    newest,
    readsNow: due && allowed,
    dueAt,
    refreshed,
    failure: reads.lastError,
    wakeAt: resumesAt ?? (due || inFlight ? Infinity : Math.max(dueAt, spacedAt)),
  };
}

/**
 * `read` with stats the caller already holds, such as a search on this Mac, for rows no
 * read answered. A read's own answer is fresher and wins; a row with known stats has no gap.
 */
export function withKnownStats(
  read: PullRequestListStatsResult,
  known: ReadonlyMap<string, PullRequestListStats> | undefined,
): PullRequestListStatsResult {
  if (!known?.size) return read;
  const byUrl = new Map(known);
  for (const [url, stats] of read.byUrl) byUrl.set(url, stats);
  const gaps = new Map([...read.gaps].filter(([url]) => !byUrl.has(url)));
  return { status: "ready", byUrl, gaps };
}

/** Whether two stats results show the same: the same status, and the same values for each row. */
export function sameListStatsResult(
  left: PullRequestListStatsResult,
  right: PullRequestListStatsResult,
): boolean {
  return (
    left.status === right.status &&
    sameValues(left.byUrl, right.byUrl) &&
    sameValues(left.gaps, right.gaps) &&
    sameValues(left.snapshots ?? new Map(), right.snapshots ?? new Map())
  );
}

function sameValues<Value extends object>(
  left: ReadonlyMap<string, Value>,
  right: ReadonlyMap<string, Value>,
): boolean {
  if (left.size !== right.size) return false;
  for (const [key, value] of left) {
    const other = right.get(key);
    if (other === undefined || !sameFields(value, other)) return false;
  }
  return true;
}

function sameFields(left: object, right: object): boolean {
  if (left === right) return true;
  const leftEntries = Object.entries(left);
  return (
    leftEntries.length === Object.keys(right).length &&
    leftEntries.every(([key, value]) => Object.is(value, (right as Record<string, unknown>)[key]))
  );
}

/** The list's overall stats state, which decides the size and update-time orders. */
function combinedStatus(
  results: readonly { isError: boolean; data: unknown }[],
  batches: readonly PullRequestListStatsBatch[],
  support: SourceSupport,
  checking: boolean,
): PullRequestListStatsStatus {
  if (results.length === 0) return "unsupported";
  const offered = results.filter((_, index) => supports(support, batches[index].source));
  if (offered.length === 0) return checking ? "loading" : "unsupported";
  // A failed read (signed out, rate limited) drops its earlier answer: a gap beats stale.
  if (offered.every((result) => result.isError)) return checking ? "loading" : "failed";
  return offered.some((result) => result.data && !result.isError) ? "ready" : "loading";
}

/** Re-read the list stats this Mac reads as its own identity, in every scope. */
export function refreshMacListStats(queryClient: QueryClient): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: ["mac-read"],
    predicate: (query) => query.queryKey[4] === PULL_REQUEST_LIST_STATS_OPERATION,
  });
}
