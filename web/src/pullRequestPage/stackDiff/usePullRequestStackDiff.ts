// Whether a stacked PR's Changes show only its own changes, and the comparison behind that.
// It's offered only for a verified stack whose direct parent was read, where the session's host
// can compare; it reads only on the surface shown, Changes or Summary, when that wants it.

import { skipToken, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { useDesktopFeatureFlag } from "@/hooks/useDesktopFeatureFlag";
import {
  cachedStackMembersAnswers,
  refreshPullRequestStackMembers,
} from "@/hooks/usePullRequestStackMembers";
import { parseSessionPullRequestUrl } from "@/lib/sessionPullRequestUrl";
import { StackCompareChangedError } from "@/lib/pullRequestStacks/stackCompare";
import type { StackLandingDiagnostic } from "@/lib/pullRequestStacks/stackLanding";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";
import type { PullRequestStackView } from "@/pullRequestPage/usePullRequestStack";
import {
  type StackDiffPullRequest,
  stackComparisonPlan,
  stackComparisonQueryOptions,
  stackComparisonsKey,
  stackDiffAccount,
} from "./stackComparisonRead";
import {
  IDLE_STACK_COMPARISON,
  sameStackIdentity,
  type StackComparisonIdentity,
  stackComparisonKey,
  stackComparisonReducer,
  type StackComparisonState,
} from "./stackComparisonState";
import { usePullRequestRefreshOwner } from "@/pullRequestPage/pullRequestRefreshOwner";
import {
  onPullRequestRefreshRequested,
  type PullRequestRefreshResult,
} from "@/pullRequestPage/refreshPullRequest";

import { useStackDiffIntent } from "./stackDiffIntent";
import { type EffectiveStackDiff, effectiveStackDiff, type StackDiffIntent } from "./stackDiffMode";

export type { StackDiffPullRequest } from "./stackComparisonRead";

/** Turns a stacked PR's own changes on; off, nothing is read, offered or shown. */
export const PULL_REQUEST_STACK_INCREMENTAL_DIFF_FLAG = "pull-request-stack-incremental-diff";
/** The policy Automatic follows: show only this PR's changes wherever stacks show. */
export const STACK_DIFF_AUTOMATIC_POLICY_FLAG = "pull-request-stacks";

export interface PullRequestStackDiff {
  /**
   * The PR this one is stacked on, such as `#100`, and the repository its commits live in as
   * `owner/repo`: null when its fork is gone.
   */
  parent: { number: number; label: string; headRepository: string | null };
  intent: StackDiffIntent;
  choose: (intent: StackDiffIntent) => void;
  effective: EffectiveStackDiff;
  /**
   * What the comparison found for the PR and reader shown now, whichever changes the person
   * chose to see; null before anything asked for it.
   */
  diagnostic: StackLandingDiagnostic | null;
  /** Read the PR and its stack again, then compare afresh. */
  reload: () => void;
  /** Ask a comparison that failed again, on the same commits. */
  retry: () => void;
}

/** How long Reload waits for the page to show what it read before offering Reload again. */
export const RELOAD_CATCH_UP_MS = 5000;

/** What of the PR a comparison depends on, so a copy of the same PR compares equal. */
function pullRequestInputs(pr: StackDiffPullRequest): string {
  return JSON.stringify([
    pr.url,
    pr.head_sha ?? null,
    pr.base_sha ?? null,
    pr.base_ref ?? null,
    pr.body ?? "",
  ]);
}

/** What Reload saw before it read, and what it read. */
interface ReloadBarrier {
  prBefore: string | null;
  freshPr: string;
  membersBefore: unknown;
  freshMembers: { has: (answer: unknown) => boolean };
}

/**
 * Whether the page shows the reloaded PR, or one newer than before Reload, and members newer
 * than before it or confirmed by it.
 */
export function reloadCaughtUp(
  barrier: ReloadBarrier,
  shownPr: string | null,
  members: unknown,
): boolean {
  const prCaughtUp = shownPr === barrier.freshPr || shownPr !== barrier.prBefore;
  const membersCaughtUp =
    members !== barrier.membersBefore || barrier.freshMembers.has(barrier.membersBefore);
  return prCaughtUp && membersCaughtUp;
}

/**
 * The state as it stands for `identity` now: a comparison for other inputs is never shown,
 * even before its event lands.
 */
function currentState(
  state: StackComparisonState,
  identity: StackComparisonIdentity | null,
): StackComparisonState {
  if (state.kind === "accepted") {
    return sameStackIdentity(state, identity) ? state : { kind: "stale" };
  }
  if (state.kind === "reading" || state.kind === "failed") {
    return sameStackIdentity(state, identity) ? state : IDLE_STACK_COMPARISON;
  }
  return state;
}

/** What the comparison says about landing, from the state as it stands now. */
function landingDiagnostic(state: StackComparisonState): StackLandingDiagnostic | null {
  switch (state.kind) {
    case "idle":
      return null;
    case "reading":
    case "reloading":
      return { kind: "reading" };
    case "stale":
      return { kind: "stale" };
    case "failed":
      return state.unsupported ? null : { kind: "failed" };
    case "accepted": {
      const { separable, reason } = state.comparison;
      if (separable) return { kind: "separable" };
      return reason === "not_carried" || reason === "in_base" ? { kind: reason } : null;
    }
  }
}

/** The PR page's surface the reader is on, where a comparison may be read. */
export type StackDiffSurface = "changes" | "summary" | null;

/**
 * `pr`'s stack diff, or null where none is offered: the flag is off, the stack isn't verified,
 * its parent isn't read, or the session's host can't compare. `surface` is the one shown, if
 * any; Summary reads only when `diagnose` says its merge area wants the comparison.
 * `reloadPullRequest` reads the PR itself again, resolving the PR it read, or null when it couldn't.
 */
export function usePullRequestStackDiff({
  conversationId,
  pr,
  stack,
  surface,
  diagnose = false,
  reloadPullRequest,
}: {
  /** The session whose host compares the PR. */
  conversationId: string;
  pr: StackDiffPullRequest | null;
  stack: PullRequestStackView | null;
  surface: StackDiffSurface;
  diagnose?: boolean;
  reloadPullRequest: () => Promise<StackDiffPullRequest | null>;
}): PullRequestStackDiff | null {
  const enabled = useDesktopFeatureFlag(PULL_REQUEST_STACK_INCREMENTAL_DIFF_FLAG, false).value;
  const automaticPolicy = useDesktopFeatureFlag(STACK_DIFF_AUTOMATIC_POLICY_FLAG, false).value;
  const queryClient = useQueryClient();
  const hostCompares = usePullRequestMethodServed(conversationId, "comparePullRequestRange");
  const url = pr?.url ?? null;
  const target = useMemo(() => {
    const parsed = enabled && hostCompares && url ? parseSessionPullRequestUrl(url) : null;
    return parsed?.provider === "github" ? parsed : null;
  }, [enabled, hostCompares, url]);
  const account = stackDiffAccount(target);
  const [intent, storeIntent] = useStackDiffIntent(account, url);

  const plan = useMemo(
    () =>
      stackComparisonPlan({
        pr,
        stack: enabled ? stack : null,
        target,
        conversationId,
        intent,
        automaticPolicy,
      }),
    [enabled, pr, stack, target, conversationId, intent, automaticPolicy],
  );
  const { parent, offered, baseIsParent, wanted } = plan;
  const identity = useMemo(
    () => plan.identity,
    // The same comparison again is the same identity, whatever object holds it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [plan.identity ? JSON.stringify(plan.identity) : null],
  );

  const [stored, dispatch] = useReducer(stackComparisonReducer, IDLE_STACK_COMPARISON);
  const state = currentState(stored, identity);
  // Only the surface shown reads, when it wants the comparison; leaving it, or choosing all
  // changes, keeps what was read, but a shown comparison still goes stale on any change.
  const read =
    offered && ((surface === "changes" && wanted) || (surface === "summary" && diagnose));
  useEffect(() => {
    // The same identity again changes nothing; after a return to idle it reads anew.
    dispatch({ type: "identity", identity, read });
  }, [identity, read, stored.kind]);

  const prQueries = useMemo(() => stackComparisonsKey(conversationId, url), [conversationId, url]);
  const reading = state.kind === "reading" ? state.identity : null;
  const query = useQuery(stackComparisonQueryOptions(conversationId, url, reading));
  // A comparison already read for exactly these inputs, as reading ahead leaves it, shows on
  // the first render instead of after the reading state's effects catch up.
  const cached = useQuery({
    ...stackComparisonQueryOptions(conversationId, url, read ? identity : null),
    queryFn: skipToken,
  }).data;
  const shown: StackComparisonState =
    read && identity && cached && (state.kind === "idle" || state.kind === "reading")
      ? currentState({ kind: "accepted", identity, comparison: cached }, identity)
      : state;
  useEffect(() => {
    if (!reading) return;
    if (query.status === "success") {
      dispatch({ type: "answer", identity: reading, comparison: query.data });
    } else if (query.status === "error") {
      const changed = query.error instanceof StackCompareChangedError;
      dispatch({ type: "failed", identity: reading, changed });
    }
  }, [reading, query.status, query.data, query.error]);

  // Reload compares again only once this hook renders what it read: the PR the reload returned,
  // or a newer one, and members read since it started or confirmed by it.
  const membersAnswer = stack?.members.kind === "loaded" ? stack.members.answer : undefined;
  const shownPr = pr ? pullRequestInputs(pr) : null;
  const latest = useRef({ members: membersAnswer, pr: shownPr });
  latest.current = { members: membersAnswer, pr: shownPr };
  const [barrier, setBarrier] = useState<ReloadBarrier | null>(null);
  const caughtUp = !!barrier && reloadCaughtUp(barrier, shownPr, membersAnswer);
  useEffect(() => {
    if (!barrier) return undefined;
    if (caughtUp) {
      setBarrier(null);
      dispatch({ type: "reload-settled", ok: true });
      return undefined;
    }
    // What the page shows never caught up: Reload is left to try again.
    const timer = setTimeout(() => {
      setBarrier(null);
      dispatch({ type: "reload-settled", ok: false });
    }, RELOAD_CATCH_UP_MS);
    return () => clearTimeout(timer);
  }, [barrier, caughtUp]);

  const forgetComparisons = useCallback(
    () => queryClient.removeQueries({ queryKey: prQueries }),
    [queryClient, prQueries],
  );
  const reload = useCallback(() => {
    forgetComparisons();
    setBarrier(null);
    dispatch({ type: "reload-started" });
    const before = latest.current;
    void Promise.all([reloadPullRequest(), refreshPullRequestStackMembers(queryClient)]).then(
      ([fresh]) => {
        if (!fresh) dispatch({ type: "reload-settled", ok: false });
        else {
          setBarrier({
            prBefore: before.pr,
            freshPr: pullRequestInputs(fresh),
            membersBefore: before.members,
            freshMembers: cachedStackMembersAnswers(queryClient),
          });
        }
      },
      () => dispatch({ type: "reload-settled", ok: false }),
    );
  }, [forgetComparisons, reloadPullRequest, queryClient]);
  const retry = useCallback(() => {
    forgetComparisons();
    dispatch({ type: "retry" });
  }, [forgetComparisons]);

  // A refresh of the PR compares again, once what the page shows has caught up with what the
  // refresh read: only then is the comparison it asks for the one of the refreshed inputs.
  const [refreshing, setRefreshing] = useState<{
    barrier: ReloadBarrier | null;
    pinned: string | null;
    settle: (result: PullRequestRefreshResult) => void;
  } | null>(null);
  const refreshOwner = usePullRequestRefreshOwner();
  useEffect(() => {
    if (!url) return undefined;
    const where = {
      prUrl: url,
      kind: "comparison" as const,
      client: queryClient,
      owner: refreshOwner,
    };
    return onPullRequestRefreshRequested(where, (attempt) => {
      const before = latest.current;
      return new Promise<PullRequestRefreshResult>((resolve) => {
        // An attempt that ended (its deadline, or the PR left) leaves nothing of itself here.
        const settle = (result: PullRequestRefreshResult) => {
          attempt.signal.removeEventListener("abort", abandon);
          resolve(result);
        };
        const abandon = () => {
          setRefreshing((current) => (current?.settle === settle ? null : current));
          resolve({ kind: "timed-out" });
        };
        attempt.signal.addEventListener("abort", abandon, { once: true });
        setRefreshing({ barrier: null, pinned: null, settle });
        void Promise.all([attempt.overview, attempt.members]).then(([overview]) => {
          if (attempt.signal.aborted) return;
          const fresh = (overview as { pr?: StackDiffPullRequest } | undefined)?.pr;
          setRefreshing((current) =>
            current?.settle === settle
              ? {
                  ...current,
                  barrier: {
                    prBefore: before.pr,
                    freshPr: fresh ? pullRequestInputs(fresh) : (before.pr ?? ""),
                    membersBefore: before.members,
                    freshMembers: cachedStackMembersAnswers(queryClient),
                  },
                }
              : current,
          );
        });
      });
    });
  }, [url, queryClient, refreshOwner]);
  const refreshCaughtUp =
    !!refreshing?.barrier && reloadCaughtUp(refreshing.barrier, shownPr, membersAnswer);
  useEffect(() => {
    if (!refreshing?.barrier || refreshing.pinned !== null) return undefined;
    if (refreshCaughtUp) {
      forgetComparisons();
      dispatch({ type: "refresh" });
      setRefreshing((current) => (current ? { ...current, barrier: null, pinned: "" } : current));
      return undefined;
    }
    const { settle } = refreshing;
    const timer = setTimeout(() => {
      setRefreshing(null);
      settle({ kind: "timed-out" });
    }, RELOAD_CATCH_UP_MS);
    return () => clearTimeout(timer);
  }, [refreshing, refreshCaughtUp, forgetComparisons]);
  // Once reset, the attempt follows the first comparison asked for, and the newest one asked
  // for before any answers; it ends when that one settles, or when none is wanted.
  useEffect(() => {
    if (!refreshing || refreshing.pinned === null) return;
    const done = (result: PullRequestRefreshResult) => {
      setRefreshing(null);
      refreshing.settle(result);
    };
    if (state.kind === "reading") {
      const key = stackComparisonKey(state.identity);
      if (refreshing.pinned !== key) setRefreshing({ ...refreshing, pinned: key });
      return;
    }
    if (refreshing.pinned === "") {
      if (state.kind === "idle" && (!identity || !read)) done({ kind: "ok" });
      return;
    }
    if (state.kind === "accepted" || state.kind === "stale") done({ kind: "ok" });
    else if (state.kind === "failed")
      done(
        state.unsupported
          ? { kind: "ok" }
          : { kind: "failed", error: new Error("Couldn’t compare with the parent pull request.") },
      );
    // Idle for an older identity while a newer one is wanted: follow that one once it reads.
    else if (state.kind === "idle" && (!identity || !read)) done({ kind: "ok" });
  }, [refreshing, state, identity, read]);
  // Leaving the PR while it refreshes answers its attempt: nothing here is wanted any more.
  const settleOnLeave = useRef(refreshing?.settle);
  settleOnLeave.current = refreshing?.settle;
  useEffect(() => () => settleOnLeave.current?.({ kind: "ok" }), [url]);
  const failed = shown.kind === "failed";
  const choose = useCallback(
    (next: StackDiffIntent) => {
      storeIntent(next);
      // Choosing again is a way to ask a failed comparison again.
      if (failed && next !== "all") retry();
    },
    [storeIntent, failed, retry],
  );

  const effective = useMemo(
    () => effectiveStackDiff({ intent, automaticPolicy, baseIsParent, comparison: shown }),
    [intent, automaticPolicy, baseIsParent, shown],
  );
  const diagnostic = useMemo(() => landingDiagnostic(shown), [shown]);
  // A comparison answered as unsupported offers nothing.
  const unsupported = shown.kind === "failed" && shown.unsupported;

  return useMemo(() => {
    if (!offered || !parent || unsupported) return null;
    return {
      parent: {
        number: parent.number,
        label: `#${parent.number}`,
        headRepository: parent.head_repository,
      },
      intent,
      choose,
      effective,
      diagnostic,
      reload,
      retry,
    };
  }, [offered, parent, unsupported, intent, choose, effective, diagnostic, reload, retry]);
}
