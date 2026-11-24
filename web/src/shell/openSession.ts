// The sessions the page shows now, as the app shell publishes them, so a request that settles
// after its row unmounted, such as a Leave, acts only on a session still shown.

import { useEffect } from "react";
import { matchPath } from "react-router-dom";

import { useActiveRootSessionId } from "@/hooks/useSession";
import { useLocation, useRebasePath } from "@/lib/routing";
import { PROJECT_PANEL_SESSION_PARAM } from "@/projectHome/projectHomeNavigation";

const NOTHING_OPEN: readonly (string | null)[] = [];
let openSessionIds: readonly (string | null)[] = NOTHING_OPEN;

/** Records what the page shows. Mount once, in the app shell. */
export function usePublishOpenSession(routedSessionId: string | null): void {
  const rootId = useActiveRootSessionId(routedSessionId);
  const location = useLocation();
  const rebasePath = useRebasePath();
  const search = new URLSearchParams(location.search);
  const draftSourceId = location.pathname === rebasePath("/") ? search.get("sibling") : null;
  // Only the project home route shows a board; an extension's path can also end in /projects/<name>.
  const onProjectHome = matchPath(rebasePath("/projects/:projectName"), location.pathname) !== null;
  const quickViewId = onProjectHome ? search.get(PROJECT_PANEL_SESSION_PARAM) : null;
  useEffect(() => {
    openSessionIds = [routedSessionId, rootId, draftSourceId, quickViewId];
    return () => {
      openSessionIds = NOTHING_OPEN;
    };
  }, [routedSessionId, rootId, draftSourceId, quickViewId]);
}

/**
 * Whether the page shows `sessionId` now: itself, as an open sub-agent's root, as a draft's
 * source, or in a project board's quick view.
 */
export function isOpenSession(sessionId: string): boolean {
  return openSessionIds.includes(sessionId);
}
