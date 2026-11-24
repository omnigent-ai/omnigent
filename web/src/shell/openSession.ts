// The sessions the page shows now, as the app shell publishes them, so a request that settles
// after its row unmounted, such as a Leave, acts only on a session still shown.

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

/** Whether the page shows `sessionId` now, itself, as an open sub-agent's root or as a draft's source. */
export function isOpenSession(sessionId: string): boolean {
  return openSessionIds.includes(sessionId);
}
