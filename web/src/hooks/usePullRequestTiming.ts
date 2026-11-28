import { useCallback, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { reportPullRequestTiming } from "@/lib/nativeBridge";
import {
  PullRequestTiming,
  type PullRequestReadPath,
  type PullRequestTimingMilestone,
} from "@/lib/pullRequestTiming";

export interface PullRequestTimingInput {
  sessionId: string;
  /** False while the panel is hidden; each reopening is timed on its own. */
  active: boolean;
  path: PullRequestReadPath;
  /** The panel's state kind, e.g. "loading" or "ready". */
  stateKind: string;
  /** Info was already cached when this render started. */
  cached: boolean;
}

/**
 * Reports a pull request view's own milestones to the panel's timer. Changes showing or not is
 * reported each time it changes, as `changesShown` and `changesHidden`.
 */
export type PullRequestTimingMark = (
  milestone: Exclude<PullRequestTimingMilestone, "state"> | "changesHidden",
) => void;

/**
 * Time one opening of the Pull Requests panel and report it once to the
 * desktop diagnostics log: when it settles, or with what it reached when the
 * session changes, the panel hides, or it unmounts. The panel owns the clock, so
 * selecting a PR or switching to the list never restarts it; the PR view reports
 * its header and diff through the returned mark, whose identity changes with
 * each opening.
 */
export function usePullRequestTiming({
  sessionId,
  active,
  path,
  stateKind,
  cached,
}: PullRequestTimingInput): PullRequestTimingMark {
  const timing = useRef<{ opening: object; clock: PullRequestTiming } | null>(null);
  // One token per opening: the clock and the mark handed to PR views share it.
  // oxlint-disable-next-line react-hooks/exhaustive-deps -- the deps define an opening
  const opening = useMemo(() => ({}), [sessionId, active, path]);
  // Read in the opening effect only, so a refetch never restarts the clock.
  const cachedAtOpen = useRef(cached);
  cachedAtOpen.current = cached;

  const settle = useCallback((current: PullRequestTiming) => {
    if (!current.complete) return;
    const report = current.takeReport();
    if (report) reportPullRequestTiming(report);
  }, []);

  // A layout effect, so the clock exists before any child view's effect reports a milestone.
  useLayoutEffect(() => {
    if (!active) return;
    const current = {
      opening,
      clock: new PullRequestTiming(sessionId, path, cachedAtOpen.current, performance.now()),
    };
    timing.current = current;
    return () => {
      const report = current.clock.takeReport();
      if (report) reportPullRequestTiming(report);
      if (timing.current === current) timing.current = null;
    };
  }, [opening, sessionId, active, path]);

  useEffect(() => {
    const current = timing.current;
    if (!current || stateKind === "loading") return;
    current.clock.mark("state", performance.now(), stateKind);
    settle(current.clock);
  }, [stateKind, sessionId, active, settle]);

  // A new identity for each opening, so a PR view's effects replay what it already
  // shows when a hidden panel reopens; selecting another PR keeps the same opening.
  return useCallback<PullRequestTimingMark>(
    (milestone) => {
      const current = timing.current;
      if (current?.opening !== opening) return;
      const now = performance.now();
      if (milestone === "changesShown" || milestone === "changesHidden") {
        current.clock.showChanges(milestone === "changesShown", now);
      } else {
        current.clock.mark(milestone, now);
      }
      settle(current.clock);
    },
    [opening, settle],
  );
}
