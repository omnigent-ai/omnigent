import { useEffect, useState } from "react";
import { getCodexGoal, type CodexGoal } from "@/lib/sessionsApi";

interface UseCodexGoalStateResult {
  goal: CodexGoal | null;
  setGoal: (goal: CodexGoal | null) => void;
}

/**
 * Keep the current Codex goal snapshot for a composer session.
 *
 * The hook intentionally fails closed to ``null`` on read errors: the dialog
 * performs its own read and surfaces the error text when the user opens it.
 */
export function useCodexGoalState(
  conversationId: string | null,
  enabled: boolean,
): UseCodexGoalStateResult {
  const [goal, setGoal] = useState<CodexGoal | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!enabled || !conversationId) {
      setGoal(null);
      return;
    }
    void getCodexGoal(conversationId)
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
