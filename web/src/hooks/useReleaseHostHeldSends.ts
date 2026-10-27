import { useEffect, useRef } from "react";
import { useSessionHostOnline } from "@/hooks/RunnerHealthProvider";
import { useChatStore } from "@/store/chatStore";

/**
 * How long a held send waits while the host already reads online. The health
 * poll can miss a short outage, so the send retries after this even without
 * having seen the host offline; if the host is still down the server refuses
 * it again and it goes back to waiting.
 */
export const HOST_HOLD_RECHECK_MS = 60_000;

/**
 * Release a session's sends that were held because its host was offline,
 * once the host is back. The queue flush then sends them in order.
 *
 * @param sessionId The open session, or `null` while none (or a temp id) is open.
 */
export function useReleaseHostHeldSends(sessionId: string | null): void {
  const hostOnline = useSessionHostOnline(sessionId ?? undefined);
  const hasHeldSends = useChatStore((s) =>
    s.queuedMessages.some((m) => m.conversationId === sessionId && m.waitingForHost),
  );
  const releaseHostHeldMessages = useChatStore((s) => s.releaseHostHeldMessages);
  const seenOffline = useRef<{ sessionId: string | null; offline: boolean }>({
    sessionId: null,
    offline: false,
  });

  useEffect(() => {
    if (sessionId === null || !hasHeldSends) return;
    if (seenOffline.current.sessionId !== sessionId) {
      seenOffline.current = { sessionId, offline: false };
    }
    if (hostOnline === false) {
      seenOffline.current.offline = true;
      return;
    }
    if (hostOnline !== true) return;
    if (seenOffline.current.offline) {
      seenOffline.current.offline = false;
      releaseHostHeldMessages(sessionId);
      return;
    }
    const timer = window.setTimeout(() => releaseHostHeldMessages(sessionId), HOST_HOLD_RECHECK_MS);
    return () => window.clearTimeout(timer);
  }, [sessionId, hasHeldSends, hostOnline, releaseHostHeldMessages]);
}
