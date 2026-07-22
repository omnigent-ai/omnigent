import type { QueryClient } from "@tanstack/react-query";
import { prefetchSessionForSwitch } from "./sessionsApi";

export const SESSION_HOVER_PREFETCH_DELAY_MS = 100;

export type SessionPrefetchFn = (queryClient: QueryClient, sessionId: string) => void;

/**
 * Debounced hover prefetch for session rows. One instance should be shared
 * across all rows so skimming N rows schedules at most one prefetch (the last).
 */
export interface SessionPrefetchScheduler {
  /** Debounced hover prefetch; each call replaces the pending target. */
  scheduleHover: (queryClient: QueryClient, sessionId: string) => void;
  /** Drop a pending hover without prefetching. */
  cancelHover: () => void;
  /** Immediate prefetch (e.g. keyboard focus); cancels any pending hover. */
  prefetchNow: (queryClient: QueryClient, sessionId: string) => void;
}

/**
 * Create a scheduler with closed-over timer state (no module-level mutable vars).
 *
 * :param options.delayMs: hover debounce, default {@link SESSION_HOVER_PREFETCH_DELAY_MS}.
 * :param options.prefetch: prefetch implementation, default {@link prefetchSessionForSwitch}.
 */
export function createSessionPrefetchScheduler(
  options: {
    delayMs?: number;
    prefetch?: SessionPrefetchFn;
  } = {},
): SessionPrefetchScheduler {
  const delayMs = options.delayMs ?? SESSION_HOVER_PREFETCH_DELAY_MS;
  const prefetch = options.prefetch ?? prefetchSessionForSwitch;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const cancelHover = (): void => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };

  return {
    scheduleHover(queryClient, sessionId) {
      cancelHover();
      timer = setTimeout(() => {
        timer = null;
        prefetch(queryClient, sessionId);
      }, delayMs);
    },
    cancelHover,
    prefetchNow(queryClient, sessionId) {
      cancelHover();
      prefetch(queryClient, sessionId);
    },
  };
}
