import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { getGoal, type Goal } from "@/lib/goalApi";

const GOAL_POLL_INTERVAL_MS = 5_000;

interface UseGoalStateResult {
  goal: Goal | null;
  setGoal: (goal: Goal | null) => void;
  refreshGoal: () => Promise<void>;
}

interface GoalBinding {
  conversationId: string;
}

/** Keep Chat's goal indicator current when commands or the native terminal change it. */
export function useGoalState(conversationId: string | null, enabled: boolean): UseGoalStateResult {
  const [snapshot, setSnapshot] = useState<{ binding: GoalBinding; goal: Goal | null } | null>(
    null,
  );
  const updateVersion = useRef(0);
  const lastSuccessfulVersion = useRef(0);
  const activeBinding = useRef<GoalBinding | null>(null);
  const binding = useMemo(
    () => (enabled && conversationId ? { conversationId } : null),
    [conversationId, enabled],
  );
  useLayoutEffect(() => {
    activeBinding.current = binding;
    return () => {
      if (activeBinding.current === binding) {
        activeBinding.current = null;
        updateVersion.current += 1;
      }
    };
  }, [binding]);
  const setGoal = useCallback(
    (goal: Goal | null) => {
      if (!binding || activeBinding.current !== binding) return;
      updateVersion.current += 1;
      lastSuccessfulVersion.current = updateVersion.current;
      setSnapshot((current) => (activeBinding.current === binding ? { binding, goal } : current));
    },
    [binding],
  );
  const refreshGoal = useCallback(async () => {
    if (!binding || activeBinding.current !== binding) {
      throw new Error("Goal is unavailable for this session.");
    }
    const version = ++updateVersion.current;
    const isCurrent = () => activeBinding.current === binding && version === updateVersion.current;
    try {
      const response = await getGoal(binding.conversationId);
      if (isCurrent()) {
        lastSuccessfulVersion.current = version;
        setSnapshot((current) => (isCurrent() ? { binding, goal: response.goal } : current));
      }
    } catch (error) {
      if (activeBinding.current === binding && lastSuccessfulVersion.current < version) throw error;
    }
  }, [binding]);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    if (!enabled || !conversationId) {
      setSnapshot(null);
      return;
    }
    const refresh = async () => {
      if (cancelled || inFlight || document.visibilityState === "hidden") return;
      inFlight = true;
      try {
        await refreshGoal();
      } catch {
        // Keep the last snapshot; opening the dialog surfaces read failures.
      } finally {
        inFlight = false;
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), GOAL_POLL_INTERVAL_MS);
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, [conversationId, enabled, refreshGoal]);

  const goal =
    enabled &&
    snapshot?.binding === activeBinding.current &&
    snapshot?.binding.conversationId === conversationId
      ? snapshot.goal
      : null;
  return { goal, setGoal, refreshGoal };
}
