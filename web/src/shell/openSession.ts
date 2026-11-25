// The sessions the page shows, as the shell last published them: routed, its root, a draft's
// source. A Leave that settles after its row unmounted reads them then, not what the row captured.

import { useEffect } from "react";

import { useActiveRootSessionId } from "@/hooks/useSession";
import { useLocation, useNavigate, useRebasePath } from "@/lib/routing";

const NOTHING_OPEN: readonly (string | null)[] = [];
let openSessionIds: readonly (string | null)[] = NOTHING_OPEN;

/** A path without its trailing slashes, so a mount root matches written either way. */
function withoutTrailingSlash(path: string): string {
  return path.replace(/\/+$/, "") || "/";
}

/** Records what the page shows. Mount once, in the app shell. */
export function usePublishOpenSession(routedSessionId: string | null): void {
  const rootId = useActiveRootSessionId(routedSessionId);
  const location = useLocation();
  const rebasePath = useRebasePath();
  const draftSourceId =
    withoutTrailingSlash(location.pathname) === withoutTrailingSlash(rebasePath("/"))
      ? new URLSearchParams(location.search).get("sibling")
      : null;
  useEffect(() => {
    const published = [routedSessionId, rootId, draftSourceId];
    openSessionIds = published;
    return () => {
      // A second publisher may have taken over; only the latest one's ids are cleared.
      if (openSessionIds === published) openSessionIds = NOTHING_OPEN;
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

/** Leaves `sessionId`'s view for home, if the page still shows it when this runs. */
export function useLeaveOpenSessionView(sessionId: string): () => void {
  const navigate = useNavigate();
  return () => {
    if (isOpenSession(sessionId)) navigate("/", { replace: true });
  };
}
