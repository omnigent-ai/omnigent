import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import type { ReactNode } from "react";

import { useChatStore } from "@/store/chatStore";

/**
 * Drives the background flush of the client-side message queue.
 *
 * The composer's own effect flushes the queue for the conversation the user is
 * *viewing*. This provider covers the rest: a message queued in a conversation
 * the user has navigated away from (whose SSE stream is gone) still needs to
 * send when that conversation next goes idle.
 *
 * It calls `flushBackgroundQueues` — which reads each queued conversation's
 * status from the live `["conversations"]` cache and POSTs the head of any that
 * are idle — whenever either the queue or that cache changes. Mounted app-wide
 * so it fires regardless of the current route. Level-triggered and idempotent
 * (the action skips the active conversation and no-ops when nothing is ready),
 * so over-firing is harmless.
 */
export function QueueFlushProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const queuedMessages = useChatStore((s) => s.queuedMessages);
  const flushBackgroundQueues = useChatStore((s) => s.flushBackgroundQueues);

  // Re-evaluate whenever the queue itself changes (e.g. a message enqueued in
  // another conversation, or one drained here).
  useEffect(() => {
    flushBackgroundQueues();
  }, [queuedMessages, flushBackgroundQueues]);

  // Re-evaluate whenever the sidebar/conversations cache changes — this is how
  // a navigated-away conversation's idle transition (WS overlay or poll)
  // reaches us without its live SSE stream.
  useEffect(() => {
    const cache = queryClient.getQueryCache();
    const unsubscribe = cache.subscribe((event) => {
      const key = event.query.queryKey;
      if (Array.isArray(key) && key[0] === "conversations") {
        flushBackgroundQueues();
      }
    });
    return unsubscribe;
  }, [queryClient, flushBackgroundQueues]);

  return <>{children}</>;
}
