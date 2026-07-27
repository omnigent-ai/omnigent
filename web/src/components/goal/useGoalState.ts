import { useEffect, useState } from "react";
import { getGoal, type Goal } from "@/lib/goalApi";

interface UseGoalStateResult {
  goal: Goal | null;
  setGoal: (goal: Goal | null) => void;
}

/**
 * Keep the current goal snapshot for a composer session.
 *
 * The hook intentionally fails closed to ``null`` on read errors: the dialog
 * performs its own read and surfaces the error text when the user opens it.
 */
export function useGoalState(conversationId: string | null, enabled: boolean): UseGoalStateResult {
  const [goal, setGoal] = useState<Goal | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!enabled || !conversationId) {
      setGoal(null);
      return;
    }
    void getGoal(conversationId)
      .then((response) => {
        if (!cancelled) setGoal(response.goal);
      })
      .catch(() => {
        if (!cancelled) setGoal(null);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, enabled]);

  return { goal, setGoal };
}
