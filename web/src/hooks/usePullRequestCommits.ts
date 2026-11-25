import {
  hashKey,
  queryOptions,
  type QueryCache,
  type QueryClient,
  skipToken,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { useDelayedFlag } from "@/hooks/useDelayedFlag";
import { usePullRequestRefreshOwner } from "@/pullRequestPage/pullRequestRefreshOwner";
import type { PullRequestRefreshOwner } from "@/pullRequestPage/refreshPullRequest";
import type { GitHubFastTrackState } from "@/lib/githubFastTrack";

import {
  readAhead,
  readAtObservedPriority,
  readPriorityMeta,
} from "@/hooks/pullRequestReadPriority";
import { useSecondaryReadPriority } from "@/hooks/secondaryReadPriority";
import { type CodeHostReadScheduling, desktopReadsByPriority } from "@/lib/codeHosts";
import { readOnThisMac } from "@/lib/desktopPullRequests";
import { readCommitDiffOnHost, readPullRequestCommitsOnHost } from "@/lib/hostPullRequestReads";
import { createSpeculativeSlots, type SpeculativeSlots } from "@/lib/pullRequestStacks/stackWarmup";
import { createReadAheadWindow, type ReadAheadWindow } from "@/lib/readAheadWindow";
import { queryGitHubFastTrack } from "@/lib/githubFastTrack";
import { PULL_REQUEST_LIST_STATS_OPERATION } from "@/lib/pullRequestListStats";
import type { GitHubFastTrackAccount, GitHubFastTrackError } from "@/lib/nativeBridge";
import {
  type CommitDiff,
  commitCiTone,
  COMMITS_SLOW_AFTER_MS,
  type CommitsReadPath,
  commitListRevision,
  commitsTabStatus,
  type CommitsTabStatus,
  type PullRequestRevision,
  type PullRequestCommits,
} from "@/lib/pullRequestCommits";

/** How often to re-read the list while a commit's checks are still running. */
const PENDING_CHECKS_POLL_MS = 30_000;

/** A fast-track read that failed; `message` is ready to show the user. */
export class GitHubFastTrackQueryError extends Error {
  readonly code: GitHubFastTrackError["code"];

  constructor(error: GitHubFastTrackError) {
    super(error.message);
    this.name = "GitHubFastTrackQueryError";
    this.code = error.code;
  }
}

async function read<Data>(
  operation: string,
  account: GitHubFastTrackAccount,
  params: Record<string, unknown>,
  mac?: PullRequestCommitsSource["mac"],
  scheduling: CodeHostReadScheduling = {},
): Promise<Data> {
  if (mac) {
    return (
      await readOnThisMac<Data>(
        { provider: mac.provider, host: account.host, readAs: mac.readAs },
        operation,
        params,
        scheduling,
      )
    ).data;
  }
  const result = await queryGitHubFastTrack<Data>(operation, account, params);
  if (!result.ok) throw new GitHubFastTrackQueryError(result.error);
  return result.data;
}

/** The pull request being read and the account to read it as. */
export interface PullRequestCommitsSource {
  account: GitHubFastTrackAccount;
  repository: string;
  number: number;
  /** Part of the cache key, so a push refreshes the list. */
  headSha: string | null | undefined;
  /** Also in the key, so a base that moves or a retarget refreshes it with the head unchanged. */
  baseSha: string | null | undefined;
  /**
   * Read on this Mac as its own identity rather than as `account`, which then
   * only names the host. `scope` keeps answers apart by account and user.
   */
  mac?: {
    provider: "github" | "gitspoke";
    scope: readonly unknown[];
    /** The Pull Requests page's own login, where it reads the PR as one; `scope` names it too. */
    readAs?: string;
  };
  /** Read through the session's host instead, where this shell reads neither way. */
  host?: { conversationId: string; prUrl: string };
}

function routeScope(source: Pick<PullRequestCommitsSource, "mac" | "host"> | null): unknown[] {
  if (source?.mac) return [source.mac.provider, ...source.mac.scope];
  return source?.host ? ["host", source.host.conversationId] : [];
}

/** The commit list's query key: the account, PR, head and Mac read scope it was read as. */
export function pullRequestCommitsQueryKey(
  source: PullRequestCommitsSource | null,
): readonly unknown[] {
  return [
    "github-fast-track",
    "pull_request_commits",
    source?.account.host,
    source?.account.login,
    source?.repository,
    source?.number,
    source?.headSha,
    source?.baseSha,
    ...routeScope(source),
  ];
}

/** One commit's diff key, scoped like the list so another account's answer never shows. */
export function commitDiffQueryKey(
  source: Pick<PullRequestCommitsSource, "account" | "repository" | "mac" | "host"> | null,
  sha: string | undefined,
): readonly unknown[] {
  return [
    "github-fast-track",
    "commit_diff",
    source?.account.host,
    source?.account.login,
    source?.repository,
    sha,
    ...routeScope(source),
  ];
}

/**
 * Start `queryKey`'s read over, even while its first read is still pending. A plain
 * refetch would join that pending read; cancelling drops its late answer.
 */
export async function restartQuery(
  queryClient: QueryClient,
  queryKey: readonly unknown[],
): Promise<void> {
  await queryClient.cancelQueries({ queryKey, exact: true });
  await queryClient.refetchQueries({ queryKey, exact: true });
}

/**
 * The PR's commits, oldest first. `source` null leaves the query idle; `enabled`
 * false pauses reads and polling (a hidden panel) while keeping the cached list.
 */
export function usePullRequestCommits(
  source: PullRequestCommitsSource | null,
  { enabled = true }: { enabled?: boolean } = {},
) {
  const priority = useSecondaryReadPriority();
  return useQuery({
    queryKey: pullRequestCommitsQueryKey(source),
    queryFn: source?.host
      ? () =>
          readPullRequestCommitsOnHost(
            source.host!.conversationId,
            source.host!.prUrl,
            source.headSha ?? "",
          )
      : source
        ? ({ client, queryKey }) =>
            readAtObservedPriority(client, queryKey, (observed) =>
              read<PullRequestCommits>(
                "pull_request_commits",
                source.account,
                { repository: source.repository, number: source.number },
                source.mac,
                { priority: observed },
              ),
            )
        : skipToken,
    meta: readPriorityMeta(priority),
    // CI can finish without a new push, so re-read while any commit's checks run.
    refetchInterval: (query) =>
      query.state.data?.commits.some((commit) => commitCiTone(commit.ci_state) === "pending")
        ? PENDING_CHECKS_POLL_MS
        : false,
    refetchIntervalInBackground: false,
    enabled,
    retry: false,
  });
}

/**
 * Re-read the fast-track state the PR panel shows: account checks, commit lists, and
 * the list's diff stats. A commit's diff never changes, so it stays cached.
 */
export function refreshPullRequestFastTrack(queryClient: QueryClient): Promise<void[]> {
  return Promise.all(
    ["account_status", "pull_request_commits", PULL_REQUEST_LIST_STATS_OPERATION].map((operation) =>
      queryClient.invalidateQueries({ queryKey: ["github-fast-track", operation] }),
    ),
  );
}

/**
 * A commit diff on screen: whose cache, which read exactly, the pull request it's shown for,
 * and the details showing it, whose refresh reads it again.
 */
interface ShownCommitDiff {
  cache: QueryCache;
  queryHash: string;
  pullRequest: { host: string; repository: string; number: number };
  owner: symbol | null;
}

const shownCommitDiffs = new Set<ShownCommitDiff>();

/**
 * Whether the commit diff read `queryKey` in `cache` is on screen in `owner`'s details for the
 * pull request `host`, `repository` and `number`: a commit's diff is keyed by its repository alone.
 */
export function commitDiffShownFor(
  cache: QueryCache,
  queryKey: readonly unknown[],
  pullRequest: { host: string; repository: string; number: number },
  owner: PullRequestRefreshOwner | null,
): boolean {
  const queryHash = hashKey(queryKey);
  return [...shownCommitDiffs].some(
    (shown) =>
      shown.cache === cache &&
      shown.queryHash === queryHash &&
      shown.pullRequest.host === pullRequest.host.toLowerCase() &&
      shown.pullRequest.repository === pullRequest.repository.toLowerCase() &&
      shown.pullRequest.number === pullRequest.number &&
      shown.owner === (owner?.id ?? null),
  );
}

/**
 * Say `owner`'s details show commit `sha`'s diff for `source`'s pull request, read in
 * `queryClient`. Returns the undo.
 */
export function showCommitDiff(
  queryClient: QueryClient,
  source: Pick<PullRequestCommitsSource, "account" | "repository" | "number" | "mac" | "host">,
  sha: string,
  owner: PullRequestRefreshOwner | null,
): () => void {
  const shown: ShownCommitDiff = {
    cache: queryClient.getQueryCache(),
    queryHash: hashKey(commitDiffQueryKey(source, sha)),
    pullRequest: {
      host: source.account.host.toLowerCase(),
      repository: source.repository.toLowerCase(),
      number: source.number,
    },
    owner: owner?.id ?? null,
  };
  shownCommitDiffs.add(shown);
  return () => {
    shownCommitDiffs.delete(shown);
  };
}

type CommitDiffSource = Pick<
  PullRequestCommitsSource,
  "account" | "repository" | "number" | "mac" | "host"
>;

/**
 * Whether the shell that reads `source` places reads by priority and can let go of a waiting
 * one. The fast track and older shells can't, so their reads go out plain.
 */
function commitReadsBySchedule(source: CommitDiffSource): Promise<boolean> {
  return source.mac ? desktopReadsByPriority(source.mac.provider) : Promise.resolve(false);
}

/**
 * One commit's patch read, for the open commit and the commits read ahead of it. A shell that
 * schedules reads gets the priority whoever wants it asks for, and the query's signal, so a read
 * let go of leaves its queue; elsewhere one plain read goes out, which the open commit joins.
 * A commit never changes, so it stays cached.
 */
export function commitDiffQueryOptions(source: CommitDiffSource | null, sha: string | undefined) {
  return queryOptions({
    queryKey: commitDiffQueryKey(source, sha),
    queryFn:
      source && sha
        ? async (context) => {
            if (source.host) {
              return readCommitDiffOnHost(source.host.conversationId, source.host.prUrl, sha);
            }
            const params = { repository: source.repository, sha };
            if (!(await commitReadsBySchedule(source))) {
              return read<CommitDiff>("commit_diff", source.account, params, source.mac);
            }
            // Only a shell that can let go of a read takes the signal: elsewhere cancelling it
            // would only drop an answer the shell still reads.
            const { client, queryKey, signal } = context;
            return readAtObservedPriority(client, queryKey, (priority) =>
              read<CommitDiff>("commit_diff", source.account, params, source.mac, {
                priority,
                signal,
              }),
            );
          }
        : skipToken,
    staleTime: Infinity,
    retry: false,
  });
}

/** One commit's patch, shown for `source`'s pull request. */
export function useCommitDiff(source: CommitDiffSource | null, sha: string | undefined) {
  const queryClient = useQueryClient();
  const owner = usePullRequestRefreshOwner();
  // Kept by value: the source is built anew on each render.
  const shownKey =
    source && sha ? JSON.stringify([commitDiffQueryKey(source, sha), source.number]) : null;
  const latest = useRef(source);
  latest.current = source;
  useEffect(() => {
    const shownSource = latest.current;
    if (!shownKey || !shownSource || !sha) return undefined;
    return showCommitDiff(queryClient, shownSource, sha, owner);
  }, [queryClient, shownKey, sha, owner]);
  return useQuery({
    ...commitDiffQueryOptions(source, sha),
    meta: readPriorityMeta("interactive"),
  });
}

/** The most commit patches read ahead at once, across every Commits tab on a cache. */
export const COMMIT_READ_AHEAD_IN_FLIGHT = 2;

const readAheadSlots = new WeakMap<QueryClient, SpeculativeSlots>();

function commitReadAheadSlots(client: QueryClient): SpeculativeSlots {
  let slots = readAheadSlots.get(client);
  if (!slots) {
    slots = createSpeculativeSlots(COMMIT_READ_AHEAD_IN_FLIGHT);
    readAheadSlots.set(client, slots);
  }
  return slots;
}

/** One tab's reads ahead for one source: what it wants now, in order, until it stops. */
export type CommitReadAhead = ReadAheadWindow;

/**
 * Read commit patches ahead for `source`, a few at a time across the cache. A read waits for a
 * slot, and holds it until the shell answers, even after it was let go of. Letting go drops a read
 * that hasn't started; one that has is cancelled where the shell `schedulable` can let go of it,
 * unless the open commit wants it too, and elsewhere runs on, since cancelling would only lose it.
 */
export function createCommitReadAhead(
  client: QueryClient,
  source: CommitDiffSource,
  { schedulable }: { schedulable: boolean },
): CommitReadAhead {
  return createReadAheadWindow({
    slots: commitReadAheadSlots(client),
    done: (sha) => client.getQueryData(commitDiffQueryKey(source, sha)) !== undefined,
    read: (sha, signal) => {
      const options = commitDiffQueryOptions(source, sha);
      return readAhead(
        client,
        options.queryKey,
        () => client.fetchQuery(options),
        schedulable ? signal : undefined,
      );
    },
  });
}

/** Whether the shell reading `source` schedules reads; null until it has said. */
function useCommitReadsBySchedule(source: CommitDiffSource | null): boolean | null {
  const provider = source ? (source.mac?.provider ?? "fast-track") : null;
  const [answer, setAnswer] = useState<{ provider: string; schedulable: boolean } | null>(null);
  const latest = useRef(source);
  latest.current = source;
  useEffect(() => {
    const asked = latest.current;
    if (!provider || !asked) return undefined;
    let live = true;
    void commitReadsBySchedule(asked).then(
      (schedulable) => live && setAnswer({ provider, schedulable }),
      () => live && setAnswer({ provider, schedulable: false }),
    );
    return () => {
      live = false;
    };
  }, [provider]);
  return answer && answer.provider === provider ? answer.schedulable : null;
}

/**
 * Read `shas`' patches ahead of a step to them, so ↑ and ↓ show the next commit at once, as
 * {@link createCommitReadAhead} does. A null source, as on a hidden tab, lets go of them all.
 */
export function useCommitDiffReadAhead(source: CommitDiffSource | null, shas: readonly string[]) {
  const queryClient = useQueryClient();
  const schedulable = useCommitReadsBySchedule(source);
  const latest = useRef(source);
  latest.current = source;
  const sourceKey = source ? JSON.stringify(commitDiffQueryKey(source, undefined)) : null;
  const shasKey = shas.join(",");
  const owner = useRef<CommitReadAhead | null>(null);
  useEffect(() => {
    const readSource = latest.current;
    if (!sourceKey || !readSource || schedulable === null) return undefined;
    const created = createCommitReadAhead(queryClient, readSource, { schedulable });
    owner.current = created;
    return () => {
      created.stop();
      if (owner.current === created) owner.current = null;
    };
  }, [queryClient, sourceKey, schedulable]);
  // After the owner above, so a new source's owner hears the window it opens with.
  useEffect(() => {
    owner.current?.want(shasKey ? shasKey.split(",") : []);
  }, [shasKey, sourceKey, schedulable]);
}

/**
 * The Commits tab's state, with the time bound on a pending check or read, and a
 * retry that re-checks the fast track or re-reads the list, restarting the bound.
 */
export function useCommitsTabStatus({
  path,
  fastTrack,
  fastTrackKey,
  source,
  commits,
  onTab,
}: {
  path: CommitsReadPath | null;
  fastTrack: GitHubFastTrackState;
  /** The account check's query key, from {@link gitHubFastTrackStatusKey}. */
  fastTrackKey: readonly unknown[];
  /** The list's source, whose key names the read a retry restarts. */
  source: PullRequestCommitsSource | null;
  commits: ReturnType<typeof usePullRequestCommits>;
  onTab: boolean;
}): { status: CommitsTabStatus; retry: () => void } {
  const queryClient = useQueryClient();
  const [attempt, setAttempt] = useState(0);
  const checking = path === "fast-track" && fastTrack.status === "checking";
  const listPending = !commits.data && !commits.error;
  // The bound restarts for each new request, not only for a retry of the same one.
  const pendingKey = checking ? fastTrackKey : pullRequestCommitsQueryKey(source);
  const timedOut = useDelayedFlag(
    path !== null && (checking || listPending),
    COMMITS_SLOW_AFTER_MS,
    `${attempt}:${JSON.stringify(pendingKey)}`,
  );
  const status = commitsTabStatus({
    path,
    fastTrack:
      fastTrack.status === "unavailable"
        ? { status: "unavailable", message: fastTrack.error.message }
        : { status: fastTrack.status },
    list: { hasData: !!commits.data, error: commits.error },
    timedOut,
    onTab,
  });
  const recheckAccount = path === "fast-track" && (checking || status.kind === "unavailable");
  const retryKey = useRef<readonly unknown[]>([]);
  retryKey.current = recheckAccount ? fastTrackKey : pullRequestCommitsQueryKey(source);
  const retry = useCallback(() => {
    setAttempt((current) => current + 1);
    void restartQuery(queryClient, retryKey.current);
  }, [queryClient]);
  return { status, retry };
}

/**
 * When the list answers at a head and base the panel doesn't have, re-read the pull
 * request so the two agree. Once per answered pair, so two disagreeing answers can't loop.
 */
export function useCommitListReconciliation(
  list: PullRequestCommits | undefined,
  current: PullRequestRevision,
  rereadPullRequest: () => void,
): void {
  const reconciled = useRef(new Set<string>());
  const rereadRef = useRef(rereadPullRequest);
  rereadRef.current = rereadPullRequest;
  const answered =
    list && commitListRevision(list, current) === "other"
      ? `${list.head_sha}:${list.base_sha}`
      : null;
  useEffect(() => {
    if (!answered || reconciled.current.has(answered)) return;
    reconciled.current.add(answered);
    rereadRef.current();
  }, [answered]);
}
