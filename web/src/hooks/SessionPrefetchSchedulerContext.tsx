import { type ReactNode, createContext, useContext, useMemo } from "react";
import { createSessionPrefetchScheduler } from "@/lib/sessionPrefetchScheduler";
import type { SessionPrefetchScheduler } from "@/lib/sessionPrefetchScheduler";

const SessionPrefetchSchedulerContext = createContext<SessionPrefetchScheduler | null>(null);

/** One shared hover-prefetch scheduler for all session rows in the sidebar. */
export function SessionPrefetchSchedulerProvider({ children }: { children: ReactNode }) {
  const scheduler = useMemo(() => createSessionPrefetchScheduler(), []);
  return (
    <SessionPrefetchSchedulerContext.Provider value={scheduler}>
      {children}
    </SessionPrefetchSchedulerContext.Provider>
  );
}

export function useSessionPrefetchScheduler(): SessionPrefetchScheduler {
  const scheduler = useContext(SessionPrefetchSchedulerContext);
  if (scheduler === null) {
    throw new Error(
      "useSessionPrefetchScheduler must be used within SessionPrefetchSchedulerProvider",
    );
  }
  return scheduler;
}
