// The session the page shows, as the app shell last published it: the routed session, its
// sub-agents' root, or the session a new sibling is being drafted from. A request that settles
// after its row unmounted, such as a Leave, reads it then and acts only on the session still
// shown, whatever the row's callback captured.

import { useEffect } from "react";

import { useActiveRootSessionId } from "@/hooks/useSession";
import { useLocation, useRebasePath } from "@/lib/routing";

const NOTHING_OPEN: readonly (string | null)[] = [];
let openSessionIds: readonly (string | null)[] = NOTHING_OPEN;

/** Records what the page shows. Mount once, in the app shell. */
export function usePublishOpenSession(routedSessionId: string | null): void {
  const rootId = useActiveRootSessionId(routedSessionId);
  const location = useLocation();
  const rebasePath = useRebasePath();
  const draftSourceId =
    location.pathname === rebasePath("/")
      ? new URLSearchParams(location.search).get("sibling")
      : null;
  useEffect(() => {
    openSessionIds = [routedSessionId, rootId, draftSourceId];
    return () => {
      openSessionIds = NOTHING_OPEN;
    };
  }, [routedSessionId, rootId, draftSourceId]);
}

/** `usePublishOpenSession` as a component, so the shell can mount it only with ⌘K's QuickPick on. */
export function OpenSessionPublisher({ sessionId }: { sessionId: string | null }) {
  usePublishOpenSession(sessionId);
  return null;
}

/** Whether the page shows `sessionId` now, itself, as an open sub-agent's root or as a draft's source. */
export function isOpenSession(sessionId: string): boolean {
  return openSessionIds.includes(sessionId);
}
