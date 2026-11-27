// How much of each GitHub reader's hourly budget is left, as the shell reports with each answer,
// and how background polls pace on it: slower once it runs low, paused until it refills.
// Refresh and other explicit reads never wait on it; GitHub's own limits are the cooldown's.

import { subscribeDesktopFeatureFlag } from "@/lib/desktopFeatureFlags";
import { createHeldPolls, type HeldPoll } from "@/lib/heldPolls";
import type { GitHubReadBudget } from "@/lib/nativeBridge";

/** Below this share left, polls run this many times slower. */
export const BUDGET_SLOW_BELOW = 0.25;
export const BUDGET_SLOW_FACTOR = 4;
/** Below this share left, polls pause until the budget refills. */
export const BUDGET_STOP_BELOW = 0.1;

/**
 * A read nobody asked for yet, such as one ahead of a hover, goes only while at least this share
 * of every one of its reader's budgets is known to be left.
 */
export const BUDGET_SPECULATIVE_ABOVE = 0.5;

/** The shell's rollback flag, which pages watch so turning it off resumes paused polls at once. */
export const GITHUB_READ_BUDGET_FLAG = "github-read-budget";

// A held poll whose pace its caller didn't say waits for the reset, as a stopped one does.
const STOPPED_UNTIL_RESET: GitHubBudgetPace = { pace: "stopped", until: Number.POSITIVE_INFINITY };

/** How a poll paces on its reader's budget: unchanged, slower, or held until `until`. */
export type GitHubBudgetPace =
  { pace: "normal" } | { pace: "slow"; factor: number } | { pace: "stopped"; until: number };

const NORMAL: GitHubBudgetPace = { pace: "normal" };
// Browsers fire a longer timer at once; a reset further out is checked again then.
const MAX_TIMER_MS = 2 ** 31 - 1;

export function createGitHubReadBudgets({
  now = () => Date.now(),
  watchEnabled = () => {},
}: {
  now?: () => number;
  /** Starts watching whether pacing is on, once a budget first arrives; returns how to stop. */
  watchEnabled?: (setEnabled: (enabled: boolean) => void) => (() => void) | void;
} = {}) {
  // Per reader, each hourly resource's budget in its current window.
  const budgets = new Map<string, Map<GitHubReadBudget["resource"], GitHubReadBudget>>();
  // Stopped polls wait until their reset; slowed ones until their slowed interval ends.
  const held = createHeldPolls();
  const slowed = createHeldPolls();
  let enabled = true;
  let stopWatching: (() => void) | null = null;
  // Which watch the flag's answers belong to: one handed out before a clear can't act after it.
  let lifecycle = 0;
  let resetTimer: ReturnType<typeof setTimeout> | null = null;

  // Work paced by hand, such as reading ahead, hears each wake too.
  const listeners = new Set<() => void>();
  // Reads nobody asked for yet hear every change, so a budget running low stops them at once.
  const changeListeners = new Set<() => void>();
  const changed = () => {
    for (const listener of [...changeListeners]) listener();
  };

  /** Let every held poll work out its pace again, now that a budget holds less. */
  const wake = () => {
    held.reschedule();
    slowed.reschedule();
    for (const listener of [...listeners]) listener();
  };

  // Paused polls don't read, so no answer reports a reset: a timer tells work paced by hand, and
  // slowed polls pace as usual again. A stopped poll's own interval already ends at the reset.
  function scheduleReset() {
    if (resetTimer !== null) clearTimeout(resetTimer);
    resetTimer = null;
    const at = now();
    let next = Number.POSITIVE_INFINITY;
    for (const byResource of budgets.values()) {
      for (const budget of byResource.values()) {
        if (budget.resetAt > at) next = Math.min(next, budget.resetAt);
      }
    }
    if (next === Number.POSITIVE_INFINITY) return;
    resetTimer = setTimeout(
      () => {
        resetTimer = null;
        slowed.reschedule();
        for (const listener of [...listeners]) listener();
        changed();
        scheduleReset();
      },
      Math.min(next - at, MAX_TIMER_MS),
    );
  }

  /** Hear when paces may have eased: a budget's window reset, or pacing turned off. */
  function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  function setEnabled(next: boolean) {
    if (next === enabled) return;
    enabled = next;
    if (!next) {
      budgets.clear();
      scheduleReset();
      wake();
    }
    changed();
  }

  /**
   * Keep the budget an answer as `key` reported. A later reset starts a new window for that
   * resource; within one, an answer that arrives late can't restore spent budget.
   */
  function observe(key: string, budget: GitHubReadBudget | null | undefined) {
    if (!budget || !(budget.limit > 0) || !Number.isFinite(budget.resetAt)) return;
    if (!stopWatching) {
      const current = lifecycle;
      stopWatching =
        watchEnabled((next) => {
          if (current === lifecycle) setEnabled(next);
        }) || (() => {});
    }
    if (!enabled) return;
    let byResource = budgets.get(key);
    if (!byResource) {
      byResource = new Map();
      budgets.set(key, byResource);
    }
    const known = byResource.get(budget.resource);
    if (known && budget.resetAt < known.resetAt) return;
    const sameWindow = known !== undefined && budget.resetAt === known.resetAt;
    if (sameWindow && budget.remaining >= known.remaining) return;
    byResource.set(budget.resource, { ...budget });
    if (!sameWindow) scheduleReset();
    if (known && !sameWindow) wake();
    changed();
  }

  /** Hear every change to what's known of the budgets; returns the way to stop listening. */
  function subscribeChanges(listener: () => void): () => void {
    changeListeners.add(listener);
    return () => changeListeners.delete(listener);
  }

  /** How polls as `key` pace now, on its tightest budget whose window hasn't reset yet. */
  function pace(key: string | null): GitHubBudgetPace {
    if (!enabled || !key) return NORMAL;
    let share = Number.POSITIVE_INFINITY;
    // A stop lasts until every resource low enough to stop has reset, not just the tightest.
    let until = 0;
    for (const budget of budgets.get(key)?.values() ?? []) {
      if (budget.resetAt <= now()) continue;
      const left = budget.remaining / budget.limit;
      share = Math.min(share, left);
      if (left < BUDGET_STOP_BELOW) until = Math.max(until, budget.resetAt);
    }
    if (share < BUDGET_STOP_BELOW) return { pace: "stopped", until };
    if (share < BUDGET_SLOW_BELOW) return { pace: "slow", factor: BUDGET_SLOW_FACTOR };
    return NORMAL;
  }

  /**
   * Whether `key` may spend budget on reads nobody asked for yet that spend `resources`: only
   * when each of them is known in its current window, and every budget known there has
   * {@link BUDGET_SPECULATIVE_ABOVE} or more left. A resource not seen, or pacing off, may not.
   */
  function speculativeAllowed(
    key: string | null,
    resources: readonly GitHubReadBudget["resource"][],
  ): boolean {
    if (!enabled || !key || resources.length === 0) return false;
    const current = new Map(
      [...(budgets.get(key)?.entries() ?? [])].filter(([, budget]) => budget.resetAt > now()),
    );
    return (
      resources.every((resource) => current.has(resource)) &&
      [...current.values()].every(
        (budget) => budget.remaining / budget.limit >= BUDGET_SPECULATIVE_ABOVE,
      )
    );
  }

  /** Remember a poll its budget slowed or paused, at `heldAt`, to wake it when that budget lets go. */
  function hold(query: HeldPoll, heldAt: GitHubBudgetPace = STOPPED_UNTIL_RESET) {
    // Only its latest pace holds a poll: one slowed, then stopped, keeps its stopped timer.
    if (heldAt.pace === "stopped") {
      slowed.release(query);
      held.hold(query);
    } else if (heldAt.pace === "slow") {
      held.release(query);
      slowed.hold(query);
    }
  }

  /** Forget every budget, listener and flag watch, for tests. */
  function clear() {
    budgets.clear();
    held.clear();
    slowed.clear();
    scheduleReset();
    listeners.clear();
    changeListeners.clear();
    lifecycle += 1;
    stopWatching?.();
    stopWatching = null;
    enabled = true;
  }

  return { observe, pace, speculativeAllowed, hold, clear, subscribe, subscribeChanges };
}

export type GitHubReadBudgets = ReturnType<typeof createGitHubReadBudgets>;

/** The page's one view of its readers' budgets, paced only while the shell's flag is on. */
export const githubReadBudgets = createGitHubReadBudgets({
  watchEnabled: (setEnabled) =>
    subscribeDesktopFeatureFlag(
      GITHUB_READ_BUDGET_FLAG,
      { fallback: true, mode: "resolved" },
      ({ value }) => setEnabled(value),
    ),
});
