// Whether the app may read GitHub on its own for a reader now: a row scrolled into view, a
// batch of its stats or the list's next page. Polls are paced on their own; this holds the
// first and stale reads that a scroll or a remount would otherwise start. Refresh, opening a
// pull request and the user's own changes read anyway.

import { useCallback, useEffect, useReducer, useRef, useSyncExternalStore } from "react";

import { githubCooldowns, type GitHubCooldowns } from "@/lib/githubCooldown";
import { githubPollFocus, type GitHubPollFocus } from "@/lib/githubPollFocus";
import { githubReadBudgets, type GitHubReadBudgets } from "@/lib/githubReadBudget";
import { COOLDOWN_WAKE_SPREAD_MS } from "@/lib/pullRequestPolling";

export interface AutomaticReadPacers {
  budgets: GitHubReadBudgets;
  cooldowns: GitHubCooldowns;
  focus: Pick<GitHubPollFocus, "paused" | "subscribe">;
  now: () => number;
}

const PACERS: AutomaticReadPacers = {
  budgets: githubReadBudgets,
  cooldowns: githubCooldowns,
  focus: githubPollFocus,
  now: Date.now,
};

/**
 * When a reader's ended cooldown stops holding its automatic reads: the list's own read goes
 * first at the deadline, and its answer ends the cooldown; failing that, the polls' wake spread.
 */
function recoveringUntil(cooldownKey: string | null, pacers: AutomaticReadPacers): number | null {
  if (cooldownKey === null) return null;
  const phase = pacers.cooldowns.phase(cooldownKey);
  if (phase?.phase !== "recovering") return null;
  const until = phase.until + COOLDOWN_WAKE_SPREAD_MS;
  return pacers.now() < until ? until : null;
}

/**
 * Whether reads `cooldownKey`'s reader didn't ask for may start: not while its budget is
 * nearly spent, GitHub has it waiting or its list hasn't read since, or the window has been
 * unfocused for a while.
 */
export function automaticReadsAllowed(
  cooldownKey: string | null,
  pacers: AutomaticReadPacers = PACERS,
): boolean {
  if (pacers.budgets.pace(cooldownKey).pace === "stopped") return false;
  if (cooldownKey !== null && pacers.cooldowns.heldUntil(cooldownKey) !== null) return false;
  if (recoveringUntil(cooldownKey, pacers) !== null) return false;
  return !pacers.focus.paused();
}

/**
 * When a reader's held automatic reads may start again on their own: the end of its spent budget,
 * of its cooldown or of the wait after it, whichever is latest; null when nothing that ends by
 * itself holds them.
 */
export function automaticReadsResumeAt(
  cooldownKey: string | null,
  pacers: AutomaticReadPacers = PACERS,
): number | null {
  const pace = pacers.budgets.pace(cooldownKey);
  const budgetUntil = pace.pace === "stopped" ? pace.until : null;
  const cooldownUntil =
    cooldownKey !== null
      ? (pacers.cooldowns.heldUntil(cooldownKey) ?? recoveringUntil(cooldownKey, pacers))
      : null;
  if (budgetUntil === null && cooldownUntil === null) return null;
  return Math.max(budgetUntil ?? 0, cooldownUntil ?? 0);
}

/** {@link automaticReadsAllowed}, re-rendering when a budget, cooldown or focus pause changes. */
export function useAutomaticReadsAllowed(
  cooldownKey: string | null,
  pacers: AutomaticReadPacers = PACERS,
): boolean {
  const subscribe = useCallback(
    (listener: () => void) => {
      const stops = [
        pacers.budgets.subscribe(listener),
        pacers.cooldowns.subscribe(listener),
        pacers.focus.subscribe(listener),
      ];
      return () => {
        for (const stop of stops) stop();
      };
    },
    [pacers],
  );
  const allowed = useCallback(
    () => automaticReadsAllowed(cooldownKey, pacers),
    [cooldownKey, pacers],
  );
  const current = useSyncExternalStore(subscribe, allowed, allowed);
  // A hold that ends by itself, with nothing to announce it, is read again when it does.
  const [, wake] = useReducer((count: number) => count + 1, 0);
  const resumeAt = current ? null : automaticReadsResumeAt(cooldownKey, pacers);
  useEffect(() => {
    if (resumeAt === null) return;
    const timer = setTimeout(wake, Math.max(0, resumeAt - pacers.now()));
    return () => clearTimeout(timer);
  }, [resumeAt, pacers]);
  return current;
}

/** Re-render whenever any reader's budget, cooldown or the focus pause changes. */
export function useAutomaticReadsVersion(pacers: AutomaticReadPacers = PACERS): number {
  const subscribe = useCallback(
    (listener: () => void) => {
      const stops = [
        pacers.budgets.subscribe(listener),
        pacers.cooldowns.subscribe(listener),
        pacers.focus.subscribe(listener),
      ];
      return () => {
        for (const stop of stops) stop();
      };
    },
    [pacers],
  );
  const version = useRef(0);
  const snapshot = useCallback(() => version.current, []);
  const counted = useCallback(
    (listener: () => void) =>
      subscribe(() => {
        version.current += 1;
        listener();
      }),
    [subscribe],
  );
  return useSyncExternalStore(counted, snapshot, snapshot);
}
