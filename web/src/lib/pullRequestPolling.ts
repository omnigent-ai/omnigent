import { githubCooldowns, isRateLimitFailure, type GitHubCooldowns } from "@/lib/githubCooldown";
import { githubPollFocus, type GitHubPollFocus } from "@/lib/githubPollFocus";
import { githubPollIdle, type GitHubPollIdle } from "@/lib/githubPollIdle";
import { githubReadBudgets, type GitHubReadBudgets } from "@/lib/githubReadBudget";
import type { HeldPoll } from "@/lib/heldPolls";

/** How often the Pull Requests list re-reads open PRs while it's visible. */
export const PULL_REQUEST_LIST_POLL_MS = 30_000;

/** The first wait before a read that failed polls again; it doubles with each failure in a row. */
export const FAILED_READ_POLL_MS = 60_000;
export const MAX_FAILED_READ_POLL_MS = 15 * 60_000;
/** Rows held by a cooldown wake within this window after it, not all at once. */
export const COOLDOWN_WAKE_SPREAD_MS = 30_000;

/** The parts of a TanStack query a poll's pace reads, and its observers to wake it. */
export interface PolledQuery extends HeldPoll {
  /** TanStack's hash of the query's key, which places it within a cooldown's wake window. */
  queryHash?: string;
  state: {
    data: unknown;
    error: unknown;
    fetchStatus?: string;
    dataUpdateCount: number;
    errorUpdateCount: number;
    dataUpdatedAt: number;
    errorUpdatedAt: number;
  };
}

interface FailureStreak {
  dataUpdates: number;
  errorUpdates: number;
  failures: number;
}

// Per query, the failures since its last answer; TanStack's own counters reset per fetch or never.
const streaks = new WeakMap<object, FailureStreak>();

/** Whether the latest settled read of `query` failed, other than for a rate limit. */
function latestFailed(state: PolledQuery["state"]): boolean {
  return (
    state.error != null &&
    !isRateLimitFailure(state.error) &&
    state.errorUpdatedAt >= state.dataUpdatedAt
  );
}

/** Whether the latest settled read of `query` was held back by a rate limit. */
function latestRateLimited(state: PolledQuery["state"]): boolean {
  return (
    state.error != null &&
    isRateLimitFailure(state.error) &&
    state.errorUpdatedAt >= state.dataUpdatedAt
  );
}

/** The wait after `failures` failed reads in a row: 60 s doubling to 15 min. */
function failureBackoff(failures: number): number {
  return Math.min(FAILED_READ_POLL_MS * 2 ** (Math.max(failures, 1) - 1), MAX_FAILED_READ_POLL_MS);
}

/**
 * How many reads of `query` failed in a row since it last answered. A rate limit is the
 * cooldown's to pace, so it neither counts nor resets the streak.
 */
export function consecutiveReadFailures(query: PolledQuery): number {
  const { state } = query;
  let streak = streaks.get(query);
  if (!streak) {
    streak = {
      dataUpdates: state.dataUpdateCount,
      errorUpdates: state.errorUpdateCount,
      failures: latestFailed(state) ? 1 : 0,
    };
    streaks.set(query, streak);
    return streak.failures;
  }
  if (state.dataUpdateCount !== streak.dataUpdates) {
    streak.dataUpdates = state.dataUpdateCount;
    streak.failures = 0;
  }
  if (state.errorUpdateCount !== streak.errorUpdates) {
    streak.errorUpdates = state.errorUpdateCount;
    if (latestFailed(state)) streak.failures += 1;
  }
  return streak.failures;
}

/** Where in the wake window after a cooldown `query` reads: the same place every time asked. */
function stableSpread(query: PolledQuery): number {
  const text = query.queryHash ?? "";
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash = Math.imul(hash ^ text.charCodeAt(index), 16777619);
  }
  return (hash >>> 0) / 2 ** 32;
}

/**
 * How a poll's `interval` counts: a `poll` waits that long between reads, and idle or a low
 * budget stretch it; a `deadline` is the time left until a read that must happen by then, which
 * only a cooldown, the budget's stop or an unfocused window can hold back.
 */
export type PollKind = "poll" | "deadline";

/**
 * The pace for a poll that would run every `interval`. A read GitHub rate-limited wakes at a
 * fixed place within `wakeSpreadMs` after its reader's latest deadline, or right at it for the
 * poll that `recovers`, whatever else failed before. Any other failure backs off on its own,
 * 60 s doubling to 15 min, even where the poll would otherwise rest without an answer; and
 * every read waits out its reader's cooldown. A reader low on its hourly budget polls 4 times
 * slower, and one nearly out of it waits for the reset. A window unfocused for 2 minutes stops
 * polling until it's focused again, and an `idlePaced` poll runs 4 times slower in a window
 * nobody has used for 3 minutes; the longest hold wins. Anchored to absolute times, so asking
 * again on a re-render never moves a wake.
 */
export function pacedPollInterval(
  query: PolledQuery,
  interval: number | false,
  cooldownKey: string | null,
  {
    cooldowns = githubCooldowns,
    budgets = githubReadBudgets,
    focus = githubPollFocus,
    idle = githubPollIdle,
    now = Date.now,
    spreadOf = stableSpread,
    wakeSpreadMs = COOLDOWN_WAKE_SPREAD_MS,
    recovers = false,
    kind = "poll",
    idlePaced = false,
  }: {
    cooldowns?: GitHubCooldowns;
    budgets?: GitHubReadBudgets;
    focus?: GitHubPollFocus;
    idle?: GitHubPollIdle;
    now?: () => number;
    /** Where in the wake window this query reads, from 0 to 1. */
    spreadOf?: (query: PolledQuery) => number;
    wakeSpreadMs?: number;
    /** This poll is the one that reads first once the cooldown ends, as the page's list is. */
    recovers?: boolean;
    kind?: PollKind;
    /** One of the list polls an idle window slows: never a deadline, or the open PR's own. */
    idlePaced?: boolean;
  } = {},
): number | false {
  const { state } = query;
  const failures = consecutiveReadFailures(query);
  // A read in flight paces the next one when it settles, never a timer meanwhile.
  if (state.fetchStatus === "fetching") return false;
  const unanswered = state.data === undefined && state.error != null;
  if (interval === false && !unanswered) return false;
  // A window left unfocused stops polling; focused again, a poll reads once if nothing else holds it.
  const readableNow = () =>
    consecutiveReadFailures(query) === 0 &&
    budgets.pace(cooldownKey).pace === "normal" &&
    (cooldownKey === null || cooldowns.heldUntil(cooldownKey) === null);
  if (focus.pollPaused(query, readableNow)) return false;
  const deadline = cooldownKey ? cooldowns.lastDeadline(cooldownKey) : null;
  const heldUntil = cooldownKey ? cooldowns.heldUntil(cooldownKey) : null;
  const wakeAt = (until: number) =>
    recovers ? until : until + Math.round(spreadOf(query) * wakeSpreadMs);
  if (latestRateLimited(state)) {
    // A deadline that's passed and whose cooldown an answer ended belongs to an earlier limit,
    // so it can't wake this one; the failure backoff paces it instead of a tight retry.
    const obsolete =
      deadline !== null &&
      wakeAt(deadline) <= now() &&
      cooldownKey !== null &&
      cooldowns.phase(cooldownKey) === null;
    if (deadline !== null && !obsolete) return Math.max(wakeAt(deadline) - now(), 1);
    return Math.max(interval || 0, failureBackoff(failures));
  }
  if (heldUntil !== null && recovers) return Math.max(heldUntil - now(), 1);
  let paced = interval === false ? failureBackoff(failures) : interval;
  if (state.error != null && failures > 0) paced = Math.max(paced, failureBackoff(failures));
  const budget = budgets.pace(cooldownKey);
  if (budget.pace !== "normal") budgets.hold(query, budget);
  if (budget.pace === "slow" && kind === "poll") paced *= budget.factor;
  if (idlePaced && kind === "poll" && interval !== false) {
    // Woken by input, it reads at once only once it's had its usual wait and nothing holds it.
    const dueNow = () =>
      readableNow() &&
      !focus.paused() &&
      now() - Math.max(query.state.dataUpdatedAt, query.state.errorUpdatedAt) >= interval;
    paced = Math.max(paced, interval * idle.pollFactor(query, dueNow));
  }
  if (budget.pace === "stopped") paced = Math.max(paced, wakeAt(budget.until) - now());
  if (heldUntil !== null) paced = Math.max(paced, wakeAt(heldUntil) - now());
  return paced;
}
