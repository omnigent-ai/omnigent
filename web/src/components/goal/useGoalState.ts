import { useCallback, useEffect, useRef, useState } from "react";
import { getGoal, type Goal } from "@/lib/goalApi";

interface UseGoalStateResult {
  goal: Goal | null;
  setGoal: (goal: Goal | null) => void;
}

/**
 * Keep the current goal snapshot for a composer session.
 *
 * The hook intentionally fails closed to ``null`` on read errors, including a
 * failed turn-edge read: a goal it cannot confirm may have been cleared or
 * completed. The next successful read restores the marker, and the dialog
 * performs its own read and surfaces the error text when the user opens it.
 *
 * @param conversationId - Session whose goal to read, or ``null``.
 * @param enabled - ``false`` clears the snapshot without reading.
 * @param turnActive - Whether a turn is running. Each change re-reads the
 *   goal: a typed ``/goal`` sets it when its turn starts, and the harness can
 *   complete or limit it during a turn. The turn-start read can finish before
 *   the harness stores a typed goal, so the goal may only appear at turn end.
 * @returns The goal snapshot and a setter for dialog-driven updates. A read
 *   still in flight when the setter runs is discarded.
 */
export function useGoalState(
  conversationId: string | null,
  enabled: boolean,
  turnActive = false,
): UseGoalStateResult {
  const [goal, setGoalSnapshot] = useState<Goal | null>(null);
  const dialogUpdates = useRef(0);

  useEffect(() => {
    let cancelled = false;
    if (!enabled || !conversationId) {
      setGoalSnapshot(null);
      return;
    }
    const updatesAtStart = dialogUpdates.current;
    const apply = (next: Goal | null) => {
      if (!cancelled && dialogUpdates.current === updatesAtStart) setGoalSnapshot(next);
    };
    void getGoal(conversationId)
      .then((response) => apply(response.goal))
      .catch(() => apply(null));
    return () => {
      cancelled = true;
    };
  }, [conversationId, enabled, turnActive]);

  // Stable identity: the dialog lists it as a dependency of its own read.
  const setGoal = useCallback((next: Goal | null) => {
    dialogUpdates.current += 1;
    setGoalSnapshot(next);
  }, []);

  return { goal, setGoal };
}
