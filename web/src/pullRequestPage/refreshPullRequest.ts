// Read one pull request again, and only it: its overview and checks, comments, commits, changes,
// merge options, reviewers, pending review, merge readiness and stack, as the session reads them.
// A refresh is one attempt with one deadline. It's done only once what the user is looking at has
// been read again: each reader they had open re-read at the revision the fresh overview names.

import type { Query, QueryCache, QueryClient } from "@tanstack/react-query";

import {
  isPullRequestMergeReadinessKey,
  mergeReadinessReadOf,
  mergeReadinessRequirements,
  onMergeReadinessRequirementsChanged,
  refreshPullRequestMergeReadiness,
} from "@/hooks/useMergeReadiness";
import { commitDiffShownFor } from "@/hooks/usePullRequestCommits";
import { clearHostTransportRecovery } from "@/lib/hostTransportRecovery";
import { beginFreshReads } from "@/lib/pullRequestFreshReads";
import { isSessionPullRequestRead, pullRequestReadRestAt } from "@/lib/pullRequestRecord";
import { NATIVE_STACK_QUERY } from "@/lib/pullRequestStacks/nativeStackHost";
import { pendingReviewQueryPrefix } from "@/pullRequestReviews/usePendingReview";

/** How long one refresh may take, all of it, before it says so instead of spinning. */
export const PULL_REQUEST_REFRESH_DEADLINE_MS = 30_000;

/** The reads of one PR a session shows: its own `[root, session, prUrl, …]`, or the shared record. */
const SESSION_READS = [
  "pr-page-github-info",
  "pr-page-github-review-threads",
  "pr-page-github-pr-commits",
  "pr-page-github-pr-commit-diff",
  "pr-page-github-changed-files",
  "pr-page-github-pr-diff",
  "pr-page-github-merge-options",
  "pr-page-github-reviewers",
] as const;
type SessionRead = (typeof SESSION_READS)[number];

interface Revision {
  base: string;
  head: string;
}

/**
 * Reads keyed by the revision shown, and where: the changes and threads by `base:head`, the
 * commits and a merge check by the head, the Commits tab's list by its head and base. A PR read's
 * place counts from where its family's own parts start (see `pullRequestReadRestAt`).
 */
const REVISION_PARTS: Partial<
  Record<ReaderRoot, (revision: Revision) => readonly (readonly [number, string])[]>
> = {
  "pr-page-github-changed-files": ({ base, head }) => [[0, `${base}:${head}`]],
  "pr-page-github-pr-diff": ({ base, head }) => [[0, `${base}:${head}`]],
  "pr-page-github-pr-commits": ({ head }) => [[0, head]],
  "pr-page-github-review-threads": ({ base, head }) => [[1, `${base}:${head}`]],
  "merge-checks": ({ head }) => [[3, head]],
  "commit-list": ({ base, head }) => [
    [6, head],
    [7, base],
  ],
};

/**
 * A refresh's reader roots: the session reads, the pending review, the two halves of merge
 * readiness (GitHub's answer, then its checks for that head), and the native stack.
 */
type ReaderRoot =
  | SessionRead
  | "pending-review"
  | "merge-readiness"
  | "merge-checks"
  | "native-stack"
  | "commit-list"
  | "commit-diff";

export interface PullRequestRefreshTarget {
  conversationId: string;
  prUrl: string;
}

/**
 * Which reader of `target` a query is, or null when it's none of this PR's. A commit's diff
 * is keyed by its repository alone, so it's this PR's only where `shown`'s details show it for this PR.
 */
export function pullRequestRefreshRoot(
  queryKey: readonly unknown[],
  target: PullRequestRefreshTarget,
  shown?: { cache: QueryCache; owner: PullRequestRefreshOwner | null },
): ReaderRoot | null {
  const { conversationId, prUrl } = target;
  const root = queryKey[0];
  if (
    (SESSION_READS as readonly unknown[]).includes(root) &&
    isSessionPullRequestRead(queryKey, conversationId, prUrl)
  ) {
    return root as SessionRead;
  }
  const pending = pendingReviewQueryPrefix(prUrl);
  if (pending.every((part, index) => queryKey[index] === part)) return "pending-review";
  const readiness = mergeReadinessReadOf(queryKey, prUrl);
  if (readiness) return readiness === "checks" ? "merge-checks" : "merge-readiness";
  // The stack is read through the session's host, keyed by the session as well as the PR.
  if (root === NATIVE_STACK_QUERY && queryKey[1] === prUrl && queryKey[3] === conversationId) {
    return "native-stack";
  }
  // The Commits tab reads through the account's fast track, keyed by host, repository and number.
  if (root === "github-fast-track") {
    const pr = pullRequestIdentity(prUrl);
    if (!pr || queryKey[2] !== pr.host) return null;
    const repository = typeof queryKey[4] === "string" ? queryKey[4].toLowerCase() : null;
    if (repository !== pr.repository) return null;
    if (queryKey[1] === "pull_request_commits" && queryKey[5] === pr.number) return "commit-list";
    if (queryKey[1] === "commit_diff") {
      return shown && commitDiffShownFor(shown.cache, queryKey, pr, shown.owner)
        ? "commit-diff"
        : null;
    }
  }
  return null;
}

/** `host`, lowercased `owner/name` and number of a pull request's link, or null. */
function pullRequestIdentity(prUrl: string) {
  const match = /^https:\/\/([^/]+)\/([^/]+\/[^/]+)\/pull\/(\d+)/.exec(prUrl);
  return match
    ? { host: match[1].toLowerCase(), repository: match[2].toLowerCase(), number: Number(match[3]) }
    : null;
}

/** Whether a reader's key is for the revision shown, where its key names one. */
function atRevision(root: ReaderRoot, queryKey: readonly unknown[], revision: Revision | null) {
  const parts = REVISION_PARTS[root];
  const offset = (SESSION_READS as readonly unknown[]).includes(root)
    ? pullRequestReadRestAt(queryKey)
    : 0;
  return (
    !parts || !revision || parts(revision).every(([at, value]) => queryKey[offset + at] === value)
  );
}

/** A commit's diff never changes: one read is kept, and only a failed or pending one is read again. */
const keptAsRead = (root: ReaderRoot | null, query: Query) =>
  root === "commit-diff" && query.state.status === "success";

/** How a refresh of one pull request ended. */
export type PullRequestRefreshResult =
  { kind: "ok" } | { kind: "failed"; error: Error } | { kind: "timed-out" };

/**
 * Who asks for a refresh: one PR's details on screen. Their surface says what they show now, so
 * a reader that's gone because the user moved on can be told from one being replaced.
 */
export interface PullRequestRefreshOwner {
  readonly id: symbol;
}

/** What an owner's details show: the PR, whether its details are open, and which tab. */
export interface PullRequestRefreshSurface {
  prUrl: string | null;
  showingDetails: boolean;
  tab: string | null;
}

const surfaces = new Map<symbol, PullRequestRefreshSurface>();
const surfaceKey = (surface: PullRequestRefreshSurface | undefined) =>
  surface ? JSON.stringify([surface.prUrl, surface.showingDetails, surface.tab]) : "";

/** Say what `owner`'s details show now; null once they're gone. */
export function setPullRequestRefreshSurface(
  owner: PullRequestRefreshOwner,
  surface: PullRequestRefreshSurface | null,
): void {
  if (surface) surfaces.set(owner.id, surface);
  else surfaces.delete(owner.id);
}

/** What a part of the page holding its own state, such as the stack comparison, owes an attempt. */
export interface PullRequestRefreshAttempt {
  startedAt: number;
  /** The overview the refresh read, once it's in: what the page shows from now on. */
  overview: Promise<unknown>;
  /** Every "members" part of this attempt, once each has read its stack's members again. */
  members: Promise<void>;
  /** Aborted when the attempt ends, however it ends: a part stops what it started for it. */
  signal: AbortSignal;
}

export type PullRequestRefreshPart = Promise<PullRequestRefreshResult>;

/**
 * The parts a refresh waits for beyond its queries: a stack's members, read before anything that
 * compares against them, then its comparison.
 */
export type PullRequestRefreshPartKind = "members" | "comparison";

interface Participant {
  prUrl: string;
  kind: PullRequestRefreshPartKind;
  client: QueryClient;
  /** The details it belongs to; null outside any. */
  owner: symbol | null;
  run: (attempt: PullRequestRefreshAttempt) => PullRequestRefreshPart;
}

const participants = new Set<Participant>();

/**
 * Take part in refreshes of the pull request at `prUrl` asked for by `owner` with `client`, the
 * details this part renders in: another view of the same PR isn't refreshed by them. Returns the
 * unsubscribe.
 */
export function onPullRequestRefreshRequested(
  where: {
    prUrl: string;
    kind: PullRequestRefreshPartKind;
    client: QueryClient;
    owner: PullRequestRefreshOwner | null;
  },
  run: Participant["run"],
): () => void {
  const participant: Participant = {
    prUrl: where.prUrl,
    kind: where.kind,
    client: where.client,
    owner: where.owner?.id ?? null,
    run,
  };
  participants.add(participant);
  return () => {
    participants.delete(participant);
  };
}

const settledAt = (query: Query) => Math.max(query.state.dataUpdatedAt, query.state.errorUpdatedAt);
const observed = (query: Query) => query.getObserversCount() > 0;

/** The revision the overview's data names, or null before it has one. */
function revisionOf(data: unknown): Revision | null {
  const pr = (data as { pr?: { base_sha?: unknown; head_sha?: unknown } } | undefined)?.pr;
  return typeof pr?.base_sha === "string" && typeof pr.head_sha === "string"
    ? { base: pr.base_sha, head: pr.head_sha }
    : null;
}

/**
 * Read `target` again, and resolve once every reader the user had open has been read again at
 * the revision the fresh overview names, its own parts (the stack) included, or the deadline passed.
 * Whatever ends it, everything it started for itself stops then.
 */
export async function refreshPullRequest(
  queryClient: QueryClient,
  target: PullRequestRefreshTarget,
  {
    deadlineMs = PULL_REQUEST_REFRESH_DEADLINE_MS,
    owner = null,
  }: { deadlineMs?: number; owner?: PullRequestRefreshOwner | null } = {},
): Promise<PullRequestRefreshResult> {
  const cache = queryClient.getQueryCache();
  const startedAt = Date.now();
  // What this Mac kept for the PR doesn't answer reads made for the refresh.
  const endFreshReads = beginFreshReads(target.prUrl);
  const ended = new AbortController();
  const shown = { cache, owner };
  const rootOf = (query: Query) => pullRequestRefreshRoot(query.queryKey, target, shown);
  const mine = (query: Query) => rootOf(query) !== null;
  // The readers on screen now, and able to read: those are what the user waits for.
  const awaited = new Set<ReaderRoot>(
    cache
      .findAll({ predicate: mine })
      .filter((query) => observed(query) && !query.isDisabled())
      .map((query) => rootOf(query)!),
  );
  awaited.add("pr-page-github-info");
  const surfaceAtStart = owner ? surfaceKey(surfaces.get(owner.id)) : null;
  const surfaceMoved = () => !!owner && surfaceKey(surfaces.get(owner.id)) !== surfaceAtStart;

  const overviewHashes = cache
    .findAll({ predicate: (query) => mine(query) && query.queryKey[0] === "pr-page-github-info" })
    .map((query) => query.queryHash);
  // A used-up host episode refuses every read of it; asking again starts this PR's anew, only its.
  for (const query of cache.findAll({ predicate: mine })) {
    if (query.queryKey[0] === "pr-page-github-info") clearHostTransportRecovery(query.queryHash);
  }

  let settleOverview!: (data: unknown) => void;
  const overview = new Promise<unknown>((resolve) => {
    settleOverview = resolve;
  });
  let settleMembers!: () => void;
  const members = new Promise<void>((resolve) => {
    settleMembers = resolve;
  });
  const attempt: PullRequestRefreshAttempt = {
    startedAt,
    overview,
    members,
    signal: ended.signal,
  };
  const ownParts = [...participants].filter(
    (part) =>
      part.prUrl === target.prUrl &&
      part.client === queryClient &&
      part.owner === (owner?.id ?? null),
  );
  const memberParts = ownParts
    .filter((part) => part.kind === "members")
    .map((part) => part.run(attempt));
  void Promise.all(memberParts).then(() => settleMembers());
  const comparisonParts = ownParts
    .filter((part) => part.kind === "comparison")
    .map((part) => part.run(attempt));
  const parts = [...memberParts, ...comparisonParts];

  const work = (async (): Promise<PullRequestRefreshResult> => {
    const readiness = refreshPullRequestMergeReadiness(queryClient, target.prUrl);
    await queryClient
      .invalidateQueries({
        predicate: (query) =>
          mine(query) &&
          !isPullRequestMergeReadinessKey(query.queryKey, target.prUrl) &&
          !keptAsRead(rootOf(query), query),
      })
      .catch(() => {});
    await readiness.catch(() => {});
    const readers = await readersSettled(queryClient, target, awaited, startedAt, {
      onOverview: settleOverview,
      surfaceMoved,
      signal: ended.signal,
      overviewHashes,
      shown,
    });
    const results = await Promise.all(parts);
    const failed = [readers, ...results].find((result) => result.kind !== "ok");
    return failed ?? { kind: "ok" };
  })();

  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<PullRequestRefreshResult>((resolve) => {
    timer = setTimeout(() => resolve({ kind: "timed-out" }), deadlineMs);
  });
  try {
    return await Promise.race([work, deadline]);
  } finally {
    clearTimeout(timer);
    endFreshReads();
    // The attempt is over: its watchers and its parts stop, and nothing late continues it.
    ended.abort();
    settleOverview(undefined);
    settleMembers();
  }
}

/**
 * Resolve once each awaited reader of `target` has been read again since `startedAt`: the
 * overview first, then every current reader of each other root, a revision-keyed one at the
 * overview's fresh revision. A root without a current reader is waited for while the details
 * still show what they showed, since its reader is being replaced; once they show something
 * else, it's closed. Stops when `signal` aborts.
 */
function readersSettled(
  queryClient: QueryClient,
  target: PullRequestRefreshTarget,
  awaited: Set<ReaderRoot>,
  startedAt: number,
  {
    onOverview,
    surfaceMoved,
    signal,
    overviewHashes,
    shown,
  }: {
    onOverview: (data: unknown) => void;
    surfaceMoved: () => boolean;
    signal: AbortSignal;
    /** The overview reads this attempt started, which settle it once the user left the PR. */
    overviewHashes: readonly string[];
    /** Where the details asking show their commit diffs. */
    shown: { cache: QueryCache; owner: PullRequestRefreshOwner | null };
  },
): Promise<PullRequestRefreshResult> {
  const cache = queryClient.getQueryCache();
  return new Promise((resolve) => {
    let recheck: ReturnType<typeof setTimeout> | undefined;
    let unsubscribe = () => {};
    const stop = () => {
      unsubscribe();
      clearTimeout(recheck);
    };
    const finish = (result: PullRequestRefreshResult) => {
      stop();
      resolve(result);
    };
    if (signal.aborted) {
      finish({ kind: "timed-out" });
      return;
    }
    signal.addEventListener("abort", () => finish({ kind: "timed-out" }), { once: true });
    const evaluate = () => {
      if (signal.aborted) return;
      const rootOf = (query: Query) => pullRequestRefreshRoot(query.queryKey, target, shown);
      const readers = cache
        .findAll({ predicate: (query) => rootOf(query) !== null })
        .filter(observed);
      const of = (root: ReaderRoot) => readers.filter((query) => rootOf(query) === root);
      const fresh = (query: Query) =>
        query.state.fetchStatus === "idle" && settledAt(query) >= startedAt;

      const overviews = of("pr-page-github-info").filter((query) => !query.isDisabled());
      if (overviews.length === 0) {
        // Replaced, the details still show this PR: wait for the new reader.
        if (!surfaceMoved()) return schedule();
        // Left: what the user moved on from needs nothing more, beyond this attempt's own read.
        const own = overviewHashes.map((hash) => cache.get(hash)).filter((query) => !!query);
        if (!own.every((query) => query.state.fetchStatus === "idle")) return schedule();
        const ownFailure = own.find(
          (query) => query.state.status === "error" && settledAt(query) >= startedAt,
        );
        onOverview(own[0]?.state.data);
        return finish(
          ownFailure ? { kind: "failed", error: ownFailure.state.error as Error } : { kind: "ok" },
        );
      }
      if (!overviews.every(fresh)) return schedule();
      const failedOverview = overviews.find((query) => query.state.status === "error");
      if (failedOverview) {
        onOverview(failedOverview.state.data);
        return finish({ kind: "failed", error: failedOverview.state.error as Error });
      }
      onOverview(overviews[0].state.data);
      const revision = revisionOf(overviews[0].state.data);

      let failure: Error | null = null;
      // What the fresh answers showed since the start joins too, such as checks now enabled.
      const roots = new Set<ReaderRoot>(awaited);
      for (const query of readers) {
        if (!query.isDisabled()) roots.add(rootOf(query)!);
      }
      for (const root of roots) {
        if (root === "pr-page-github-info") continue;
        const onScreen = of(root);
        // Closed, as a picker is: nothing on screen reads it any more.
        if (onScreen.length > 0 && onScreen.every((query) => query.isDisabled())) continue;
        const current = onScreen.filter(
          (query) => !query.isDisabled() && atRevision(root, query.queryKey, revision),
        );
        if (current.length === 0) {
          // A reader being replaced (a new head's) has none for a moment; one the user left doesn't come back.
          if (surfaceMoved()) continue;
          return schedule();
        }
        if (!current.every((query) => keptAsRead(root, query) || fresh(query))) return schedule();
        const failedQuery = current.find((query) => query.state.status === "error");
        failure ??= (failedQuery?.state.error as Error | undefined) ?? null;
      }
      // Fresh merge readiness may want checks its reader enables only once it renders that answer.
      for (const github of of("merge-readiness").filter((query) => !query.isDisabled())) {
        const requirements = mergeReadinessRequirements(github.queryHash);
        if (requirements.length === 0) return schedule();
        for (const { appliedAt, checks } of requirements) {
          if (appliedAt < settledAt(github)) return schedule();
          if (checks === null) continue;
          const read = cache.get(checks);
          if (!read || !observed(read) || read.isDisabled() || !fresh(read)) return schedule();
          if (read.state.status === "error") failure ??= read.state.error as Error;
        }
      }
      finish(failure ? { kind: "failed", error: failure } : { kind: "ok" });
    };
    // Mounting doesn't always reach the query cache as an event, and a surface change never does.
    const schedule = () => {
      clearTimeout(recheck);
      recheck = setTimeout(evaluate, 50);
    };
    const stopCache = cache.subscribe(() => evaluate());
    const stopReadiness = onMergeReadinessRequirementsChanged(() => evaluate());
    unsubscribe = () => {
      stopCache();
      stopReadiness();
    };
    evaluate();
  });
}
