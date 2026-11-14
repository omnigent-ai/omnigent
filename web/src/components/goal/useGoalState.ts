import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { getGoal, type Goal } from "@/lib/goalApi";

const GOAL_POLL_INTERVAL_MS = 5_000;
const GOAL_POLL_MAX_DELAY_MS = 60_000;

function goalsEqual(previous: Goal | null, next: Goal | null): boolean {
  return (
    previous === next ||
    (previous !== null &&
      next !== null &&
      previous.objective === next.objective &&
      previous.status === next.status &&
      previous.tokenBudget === next.tokenBudget &&
      previous.tokensUsed === next.tokensUsed &&
      previous.timeUsedSeconds === next.timeUsedSeconds &&
      previous.createdAt === next.createdAt &&
      previous.updatedAt === next.updatedAt)
  );
}

interface UseGoalStateResult {
  goal: Goal | null;
  goalRevision: number;
  setGoal: (goal: Goal | null) => void;
  refreshGoal: () => Promise<void>;
}

interface GoalOwner {
  conversationId: string;
}

interface GoalBinding extends GoalOwner {
  owner: GoalOwner;
  pollingEnabled: boolean;
}

/** Keep Chat's goal indicator current when commands or the native terminal change it. */
export function useGoalState(
  conversationId: string | null,
  enabled: boolean,
  pollingEnabled = true,
): UseGoalStateResult {
  const [snapshot, setSnapshot] = useState<{
    binding: GoalBinding;
    goal: Goal | null;
    revision: number;
  } | null>(null);
  const updateVersion = useRef(0);
  const lastSuccessfulVersion = useRef(0);
  const lastExplicitReadVersion = useRef(0);
  const activeBinding = useRef<GoalBinding | null>(null);
  const owner = useMemo(
    () => (enabled && conversationId ? { conversationId } : null),
    [conversationId, enabled],
  );
  // Keep accepted state for this session while fencing the previous connection's callbacks.
  const binding = useMemo(
    () => (owner ? { ...owner, owner, pollingEnabled } : null),
    [owner, pollingEnabled],
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
      const version = ++updateVersion.current;
      lastSuccessfulVersion.current = version;
      setSnapshot((current) =>
        activeBinding.current === binding ? { binding, goal, revision: version } : current,
      );
    },
    [binding],
  );
  const readGoal = useCallback(
    async (strict: boolean) => {
      if (!binding || activeBinding.current !== binding) {
        throw new Error("Goal is unavailable for this session.");
      }
      const version = ++updateVersion.current;
      if (strict) lastExplicitReadVersion.current = version;
      const isCurrent = () =>
        activeBinding.current === binding && version > lastSuccessfulVersion.current;
      try {
        const response = await getGoal(binding.conversationId, { strict });
        if (response.available === false) return;
        if (isCurrent()) {
          lastSuccessfulVersion.current = version;
          setSnapshot((current) => {
            if (activeBinding.current !== binding) return current;
            // An equal response still confirms recovery after an explicit read attempt.
            if (
              !strict &&
              current?.binding === binding &&
              current.revision >= lastExplicitReadVersion.current &&
              goalsEqual(current.goal, response.goal)
            ) {
              return current;
            }
            return { binding, goal: response.goal, revision: version };
          });
        }
      } catch (error) {
        if (activeBinding.current === binding && lastSuccessfulVersion.current < version)
          throw error;
      }
    },
    [binding],
  );
  const refreshGoal = useCallback(() => readGoal(true), [readGoal]);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    let timer: number | undefined;
    let pollDelay = GOAL_POLL_INTERVAL_MS;
    if (!enabled || !conversationId) {
      setSnapshot(null);
      return;
    }
    if (!pollingEnabled) return;
    const schedule = () => {
      if (!cancelled) timer = window.setTimeout(() => void refresh(), pollDelay);
    };
    const refresh = async () => {
      if (cancelled || inFlight) return;
      window.clearTimeout(timer);
      if (document.visibilityState === "hidden") {
        schedule();
        return;
      }
      inFlight = true;
      try {
        await readGoal(false);
        pollDelay = GOAL_POLL_INTERVAL_MS;
      } catch {
        // Keep the last snapshot; opening the dialog surfaces read failures.
        pollDelay = Math.min(pollDelay * 2, GOAL_POLL_MAX_DELAY_MS);
      } finally {
        inFlight = false;
        schedule();
      }
    };
    void refresh();
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, [conversationId, enabled, pollingEnabled, readGoal]);

  const currentSnapshot = owner !== null && snapshot?.binding.owner === owner ? snapshot : null;
  return {
    goal: currentSnapshot?.goal ?? null,
    goalRevision: currentSnapshot?.revision ?? 0,
    setGoal,
    refreshGoal,
  };
}
