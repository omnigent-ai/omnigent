import { useCallback, useEffect, useRef, useState } from "react";
import { getGoal, type Goal } from "@/lib/goalApi";

interface UseGoalStateResult {
  goal: Goal | null;
  setGoal: (goal: Goal | null) => void;
}

/** Keep Chat's goal indicator current when commands or the native terminal change it. */
export function useGoalState(conversationId: string | null, enabled: boolean): UseGoalStateResult {
  const [snapshot, setSnapshot] = useState<{ sessionId: string; goal: Goal | null } | null>(null);
  const updateVersion = useRef(0);
  const activeSession = useRef<string | null>(null);
  activeSession.current = enabled ? conversationId : null;
  const setGoal = useCallback(
    (goal: Goal | null) => {
      if (!conversationId || activeSession.current !== conversationId) return;
      updateVersion.current += 1;
      setSnapshot({ sessionId: conversationId, goal });
    },
    [conversationId],
  );

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    if (!enabled || !conversationId) return;
    const refresh = async () => {
      if (cancelled || inFlight || document.visibilityState === "hidden") return;
      inFlight = true;
      const version = updateVersion.current;
      try {
        const response = await getGoal(conversationId);
        if (!cancelled && version === updateVersion.current) {
          setSnapshot({ sessionId: conversationId, goal: response.goal });
        }
      } catch {
        // Keep the last snapshot; opening the dialog surfaces read failures.
      } finally {
        inFlight = false;
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5_000);
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, [conversationId, enabled]);

  const goal = enabled && snapshot?.sessionId === conversationId ? snapshot.goal : null;
  return { goal, setGoal };
}
