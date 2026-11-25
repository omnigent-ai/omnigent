// Reads the rest of the stack ahead while a stacked PR is open, so moving to another member
// shows its header, stack and Changes at once: every member's details and stack first, nearest
// first, then each one's changed files and patch, each once its details answer. It uses the
// same reads each member's own view makes, under the same keys, so nothing fresh is read again.

import {
  type QueryClient,
  type QueryKey,
  QueryObserver,
  type QueryObserverOptions,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useContext, useEffect, useMemo, useRef, useSyncExternalStore } from "react";

import { MacPullRequestScopeContext } from "@/hooks/macPullRequestScope";
import { readAhead } from "@/hooks/pullRequestReadPriority";
import { useDesktopFeatureFlag } from "@/hooks/useDesktopFeatureFlag";
import {
  type NativeStackData,
  pullRequestNativeStackQueryOptions,
} from "@/hooks/usePullRequestNativeStack";
import {
  pullRequestStackMembersQueryOptions,
  stackMembersBehind,
} from "@/hooks/usePullRequestStackMembers";
import {
  type PullRequestInfo,
  pullRequestChangedFilesQueryOptions,
  pullRequestDiffQueryOptions,
  pullRequestInfoReadAheadOptions,
  pullRequestPatchRevision,
  useDesktopPullRequestReads,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { githubCooldowns, githubReaderCooldownKey } from "@/lib/githubCooldown";
import { githubReadBudgets } from "@/lib/githubReadBudget";
import { runInIdleSlices } from "@/lib/idleWork";
import { acceptedMacReader, type MacReadScopeKey } from "@/lib/macReadScope";
import { stackFormats } from "@/lib/pullRequestStacks/formats";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import { readNativeStack } from "@/lib/pullRequestStacks/nativeStack";
import type { StackMembersAnswer } from "@/lib/pullRequestStacks/stackMembers";
import { stackPullRequestRef } from "@/lib/pullRequestStacks/stackModel";
import {
  createSpeculativeSlots,
  createStackWarmupQueue,
  STACK_WARMUP_CHANGES_PHASE,
  STACK_WARMUP_DETAILS_PHASE,
  type StackWarmupPacing,
  type StackReadAhead,
  type StackWarmupQueue,
  type StackWarmupRead,
  stackWarmupOrder,
} from "@/lib/pullRequestStacks/stackWarmup";

import {
  stackComparisonPlan,
  stackComparisonQueryOptions,
  stackDiffAccount,
  storedStackDiffIntent,
} from "./stackDiff/stackComparisonRead";
import {
  PULL_REQUEST_STACK_INCREMENTAL_DIFF_FLAG,
  STACK_DIFF_AUTOMATIC_POLICY_FLAG,
} from "./stackDiff/usePullRequestStackDiff";
import {
  nativeStackRevision,
  pullRequestOwnStackView,
  type PullRequestStackView,
} from "./usePullRequestStack";

/** The rollback switch for reading a stack ahead; on unless the shell turns it off. */
export const PULL_REQUEST_STACK_WARMUP_FLAG = "pull-request-stack-warmup";

type PullRequestReads = ReturnType<typeof useDesktopPullRequestReads>;

/** Where the page reads a member: the session that links it, or the page's own scope. */
export type StackReadingSession = (url: string) => string;

const subscribeVisibility = (onChange: () => void) => {
  document.addEventListener("visibilitychange", onChange);
  return () => document.removeEventListener("visibilitychange", onChange);
};
const documentVisible = () => document.visibilityState === "visible";

/** The queue's pace, from the budget and cooldown of the reader `cooldownKey` names. */
export function stackWarmupPacing(cooldownKey: string | null): StackWarmupPacing {
  return {
    budget: () => githubReadBudgets.pace(cooldownKey),
    cooldownUntil: () => (cooldownKey ? githubCooldowns.heldUntil(cooldownKey) : null),
    onBudgetEased: (listener) => githubReadBudgets.subscribe(listener),
    now: () => Date.now(),
    whenIdle: (work) =>
      runInIdleSlices(() => {
        work();
        return true;
      }),
    setTimer: (work, ms) => {
      const handle = setTimeout(work, ms);
      return () => clearTimeout(handle);
    },
  };
}

// One queue per reader, so its pace counts every request made as it, from any view; all of
// them share the window's allowance of requests ahead, stacks and hovered rows alike.
const queues = new Map<string, StackWarmupQueue>();
let windowSlots = createSpeculativeSlots();

/** The queue that reads ahead as the reader `cooldownKey` names. */
export function stackWarmupQueueFor(cooldownKey: string | null): StackWarmupQueue {
  const key = cooldownKey ?? "";
  let queue = queues.get(key);
  if (!queue) {
    queue = createStackWarmupQueue(stackWarmupPacing(cooldownKey), windowSlots);
    queues.set(key, queue);
  }
  return queue;
}

/** Forget every reader's queue, for tests. */
export function resetStackWarmupQueues(): void {
  queues.clear();
  windowSlots = createSpeculativeSlots();
}

/**
 * Whether what `options` reads is cached and fresh, so reading it ahead needs no request: by
 * the same staleness, function or number, that its view's own read goes by.
 */
function fresh<QueryFnData, Data, Key extends QueryKey>(
  queryClient: QueryClient,
  options: QueryObserverOptions<QueryFnData, Error, Data, QueryFnData, Key>,
): boolean {
  const result = new QueryObserver(queryClient, options).getOptimisticResult(
    queryClient.defaultQueryOptions(options),
  );
  return result.data !== undefined && !result.isStale;
}

/** Whether a stacked PR's Changes shows only its own changes here, and Automatic's policy. */
export interface StackComparisonSettings {
  enabled: boolean;
  automaticPolicy: boolean;
}

const NO_COMPARISONS: StackComparisonSettings = { enabled: false, automaticPolicy: false };

type PullRequestInfoPr = NonNullable<PullRequestInfo["pr"]>;

/** What most of a PR's reads ahead spend: GitHub's GraphQL budget. */
const GRAPHQL = ["graphql"] as const;

/**
 * The reads ahead for `members`: each one's details, and once they answer, GitHub's stack for
 * it at the revision they give, and its changed files and patch. Details and stacks go first.
 */
export function stackWarmupReads(
  members: string[],
  {
    queryClient,
    reads,
    sessionFor,
    sessionless,
    comparisons = NO_COMPARISONS,
    stacks = true,
    until,
  }: {
    queryClient: QueryClient;
    reads: PullRequestReads;
    sessionFor: StackReadingSession;
    sessionless: (conversationId: string) => boolean;
    /** Whether Changes shows a stacked PR's own changes here, and Automatic's policy for it. */
    comparisons?: StackComparisonSettings;
    /** False for a PR read ahead alone, as a list row is: its stack isn't read. */
    stacks?: boolean;
    /** Lets go of its reads still waiting, when the interest they serve ends. */
    until?: AbortSignal;
  },
): StackWarmupRead[] {
  /** A read of its own, with nothing waiting on it. */
  const once = (
    phase: number,
    rank: number,
    spends: StackWarmupRead["spends"],
    settled: () => boolean,
    request: () => Promise<unknown>,
  ): StackWarmupRead => ({
    phase,
    rank,
    spends,
    settled,
    run: async () => {
      await request();
      return [];
    },
  });
  /**
   * The comparison of `pr` with its parent that its own Changes would show, worked out as its
   * view does from what's been read of it: its stack, its choice of changes and the policy.
   */
  const comparison = (url: string, rank: number, pr: PullRequestInfoPr): StackWarmupRead => {
    const options = () => {
      const readScope = reads.scope(url);
      const macTarget = reads.target(url);
      const provenReader =
        macTarget && readScope.length > 0 ? acceptedMacReader(readScope as MacReadScopeKey) : null;
      const native = pullRequestNativeStackQueryOptions(url, nativeStackRevision(pr), reads);
      const self = stackPullRequestRef(url);
      const parse = self ? parseStack(stackFormats(), pr.body ?? null, self) : null;
      const describedMembers = parse
        ? queryClient.getQueryData<StackMembersAnswer | null>(
            pullRequestStackMembersQueryOptions(url, parse, reads).options.queryKey,
          )
        : undefined;
      const stack = pullRequestOwnStackView({
        pr,
        native: native.readable
          ? (queryClient.getQueryData<NativeStackData>(native.options.queryKey) ?? null)
          : null,
        describedMembers: describedMembers
          ? { kind: "loaded", answer: describedMembers }
          : { kind: "unavailable" },
        selectInPlace: () => false,
      });
      const plan = stackComparisonPlan({
        pr,
        stack,
        macTarget,
        readScope,
        provenReader,
        intent: storedStackDiffIntent(stackDiffAccount(readScope, macTarget, provenReader), url),
        automaticPolicy: comparisons.automaticPolicy,
      });
      // Only where the member's Changes would show it: wanted, and a comparison to read.
      return plan.wanted && plan.identity
        ? stackComparisonQueryOptions(macTarget, readScope, url, plan.identity)
        : null;
    };
    return {
      phase: STACK_WARMUP_CHANGES_PHASE,
      rank,
      // Its parent's lookup, then REST's compare.
      spends: ["graphql", "core"],
      // Planned only once what it depends on has answered, so "no comparison" is an answer.
      settled: () => {
        const wanted = options();
        return !wanted || fresh(queryClient, wanted);
      },
      run: async () => {
        const wanted = options();
        if (wanted) {
          await readAhead(
            queryClient,
            wanted.queryKey,
            () => queryClient.prefetchQuery(wanted),
            until,
          );
        }
        return [];
      },
    };
  };
  /**
   * What the comparison of `pr` waits on once GitHub has said about its stack: nothing more
   * for GitHub's own stack, else its description's shared members read, as its view reads it.
   */
  const afterStack = (url: string, rank: number, pr: PullRequestInfoPr): StackWarmupRead[] => {
    const native = pullRequestNativeStackQueryOptions(url, nativeStackRevision(pr), reads);
    const self = stackPullRequestRef(url);
    const answer = native.readable
      ? queryClient.getQueryData<NativeStackData>(native.options.queryKey)?.read?.answer
      : undefined;
    const stackedOnGitHub =
      !!self && ["stacked", "stacked-partial"].includes(readNativeStack(answer, self).kind);
    if (stackedOnGitHub) return [comparison(url, rank, pr)];
    const parse = self ? parseStack(stackFormats(), pr.body ?? null, self) : null;
    if (!parse) return [];
    const sharedMembers = pullRequestStackMembersQueryOptions(url, parse, reads);
    // Without members to read there's no parent to compare with.
    if (!sharedMembers.wanted) return [];
    // An answer that records this member at another head or base is read again first, once,
    // as the member's own view would before relying on it.
    const revision = { head_sha: pr.head_sha, base_ref: pr.base_ref, base_sha: pr.base_sha };
    const behind = () =>
      stackMembersBehind(
        queryClient.getQueryData<StackMembersAnswer | null>(sharedMembers.options.queryKey),
        url,
        revision,
      );
    return [
      {
        phase: STACK_WARMUP_DETAILS_PHASE,
        rank,
        spends: GRAPHQL,
        settled: () => fresh(queryClient, sharedMembers.options) && !behind(),
        run: async () => {
          const again = behind();
          await readAhead(
            queryClient,
            sharedMembers.options.queryKey,
            () =>
              queryClient.fetchQuery(
                again ? { ...sharedMembers.options, staleTime: 0 } : sharedMembers.options,
              ),
            until,
          );
          return [comparison(url, rank, pr)];
        },
      },
    ];
  };
  const details = (url: string, rank: number): StackWarmupRead => {
    const conversationId = sessionFor(url);
    const options = pullRequestInfoReadAheadOptions(
      conversationId,
      url,
      reads,
      sessionless(conversationId),
    );
    return {
      phase: STACK_WARMUP_DETAILS_PHASE,
      rank,
      spends: GRAPHQL,
      // A PR this Mac doesn't read is left to its own view.
      settled: () => !options || fresh(queryClient, options),
      run: async () => {
        if (!options) return [];
        const pr = (
          await readAhead(
            queryClient,
            options.queryKey,
            () => queryClient.fetchQuery(options),
            until,
          )
        ).pr;
        if (!pr) return [];
        const revision = pullRequestPatchRevision(pr);
        const native = pullRequestNativeStackQueryOptions(url, nativeStackRevision(pr), reads);
        const files = pullRequestChangedFilesQueryOptions(conversationId, url, revision, reads);
        const patch = pullRequestDiffQueryOptions(conversationId, url, revision, reads);
        // A comparison waits on what its view's stack comes from: GitHub's answer, then the
        // description's shared members where GitHub doesn't stack it.
        const comparing = comparisons.enabled;
        return [
          ...(!stacks
            ? []
            : native.readable
              ? [
                  {
                    phase: STACK_WARMUP_DETAILS_PHASE,
                    rank,
                    spends: GRAPHQL,
                    settled: () => fresh(queryClient, native.options),
                    run: async () => {
                      await readAhead(
                        queryClient,
                        native.options.queryKey,
                        () => queryClient.fetchQuery(native.options),
                        until,
                      );
                      return comparing ? afterStack(url, rank, pr) : [];
                    },
                  },
                ]
              : comparing
                ? afterStack(url, rank, pr)
                : []),
          once(
            STACK_WARMUP_CHANGES_PHASE,
            rank,
            GRAPHQL,
            () => fresh(queryClient, files),
            () =>
              readAhead(queryClient, files.queryKey, () => queryClient.prefetchQuery(files), until),
          ),
          once(
            STACK_WARMUP_CHANGES_PHASE,
            rank,
            // REST's compare, and GraphQL's viewer read that proves who read it.
            ["graphql", "core"],
            () => fresh(queryClient, patch),
            () =>
              readAhead(queryClient, patch.queryKey, () => queryClient.prefetchQuery(patch), until),
          ),
        ];
      },
    };
  };
  return members.map((url, rank) => forMember(url, details(url, rank)));
}

/** `read` and every read its answers let go ahead, marked as reading for `member`. */
function forMember(member: string, read: StackWarmupRead): StackWarmupRead {
  return {
    ...read,
    member,
    run: async () => (await read.run()).map((next) => forMember(member, next)),
  };
}

/**
 * Read `stack`'s other members ahead while `active`, the window shows, and the rollback flag
 * allows. Leaving the stack, hiding the window or going inactive stops what hasn't started.
 * Returns the way to move one member first: what the tab the stack opens members on shows.
 */
export function usePullRequestStackWarmup({
  stack,
  selfUrl,
  active,
  sessionFor,
  comparesStacks = false,
}: {
  stack: PullRequestStackView | null;
  selfUrl: string | null;
  active: boolean;
  sessionFor: StackReadingSession;
  /** Changes here can show a stacked PR's own changes, so its comparison is read ahead too. */
  comparesStacks?: boolean;
}): StackReadAhead {
  const enabled = useDesktopFeatureFlag(PULL_REQUEST_STACK_WARMUP_FLAG, true).value;
  const incremental = useDesktopFeatureFlag(PULL_REQUEST_STACK_INCREMENTAL_DIFF_FLAG, false).value;
  const automaticPolicy = useDesktopFeatureFlag(STACK_DIFF_AUTOMATIC_POLICY_FLAG, false).value;
  const comparing = comparesStacks && incremental;
  const visible = useSyncExternalStore(subscribeVisibility, documentVisible, () => false);
  const queryClient = useQueryClient();
  const reads = useDesktopPullRequestReads();
  const pageScope = useContext(MacPullRequestScopeContext);
  const members = useMemo(() => (stack ? stackWarmupOrder(stack.parse) : []), [stack]);
  // Kept by value, so a new view of the same stack doesn't start the queue over.
  const membersKey = members.join("\n");
  const macTarget = selfUrl ? reads.target(selfUrl) : null;
  const cooldownKey =
    macTarget?.provider === "github"
      ? githubReaderCooldownKey(macTarget.host, macTarget.readAs ?? null)
      : null;
  const running = enabled && visible && active && members.length > 0;
  // This run's queue and owner, while it reads ahead.
  const reading = useRef<{ queue: StackWarmupQueue; owner: object; members: Set<string> } | null>(
    null,
  );
  useEffect(() => {
    if (!running) return;
    // Each run of this view is its own owner: what it asked for and hasn't started goes with it.
    const owner = {};
    const queue = stackWarmupQueueFor(cooldownKey);
    const run = { queue, owner, members: new Set(membersKey.split("\n")) };
    reading.current = run;
    queue.submit(
      owner,
      stackWarmupReads(membersKey.split("\n"), {
        queryClient,
        reads,
        sessionFor,
        sessionless: (conversationId) => pageScope !== null && conversationId === pageScope,
        comparisons: { enabled: comparing, automaticPolicy },
      }),
    );
    return () => {
      if (reading.current === run) reading.current = null;
      queue.release(owner);
    };
    // The reads and session map are rebuilt each render; the members and reader decide.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, membersKey, cooldownKey, queryClient, pageScope, comparing, automaticPolicy]);
  // Members open on the stack's tab: on Changes their changes go first too, else their details.
  const lastPhase =
    stack?.subtab === "changes" ? STACK_WARMUP_CHANGES_PHASE : STACK_WARMUP_DETAILS_PHASE;
  return useCallback(
    (url: string) => {
      const run = reading.current;
      if (!run || !run.members.has(url)) return null;
      return run.queue.prioritize(run.owner, url, lastPhase);
    },
    [lastPhase],
  );
}
