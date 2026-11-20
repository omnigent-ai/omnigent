// New session opened from a thingy carries the thingy's ID in its URL (`thingy_start`). New
// session creates the session at its usual path; once that path has the real session ID, the
// thingy is linked to the session and shown in its side panel.
//
// A thingy that Build (or the blank template) just created exists only to be built by that
// session. Leaving New session without starting it deletes the thingy again, while it has nothing
// published and no session, so abandoned attempts don't pile up in the viewer's list.
import { useEffect } from "react";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";

import { useLocation, useRebasePath } from "@/lib/routing";
import { requestThingyRailTab } from "@/lib/thingyRailTabs";
import { deleteThingy, getThingy, getThingyThingySession } from "@/thingies/api/thingyApi";
import { THINGY_START_PARAM } from "@/thingies/host/thingyPages";
import {
  adoptStartedThingySession,
  isStartedSessionId,
  thingyRailKey,
} from "@/thingies/host/thingySession";
import { invalidateThingies } from "./thingyQueries";

// The thingy whose New session this tab is on, until it starts a session or goes elsewhere.
let starting: string | null = null;
// Thingies this tab created for New session to build, until a session adopts them.
const unbuilt = new Set<string>();

/** Marks a thingy as created for New session to build; it goes again if New session is left. */
export function rememberUnbuiltThingy(thingyId: string): void {
  unbuilt.add(thingyId);
}

async function discardIfUnbuilt(thingyId: string, queryClient: QueryClient): Promise<void> {
  if (!unbuilt.delete(thingyId)) return;
  try {
    const thingy = await getThingy(thingyId);
    if (thingy.published_revision !== null || !thingy.permissions.manage) return;
    if ((await getThingyThingySession(thingyId)) !== null) return;
    await deleteThingy(thingyId);
    invalidateThingies(queryClient);
  } catch {
    // It stays in the list, unpublished; the viewer can delete it there.
  }
}

/**
 * Stops treating New session as started from `thingyId`, as when its panel is closed or the
 * thingy is deleted, so the next session started there stays plain.
 */
export function leaveThingyStart(thingyId: string, queryClient: QueryClient): void {
  if (starting === thingyId) starting = null;
  void discardIfUnbuilt(thingyId, queryClient);
}

/** Forgets every started thingy; for tests. */
export function resetThingyStarts(): void {
  starting = null;
  unbuilt.clear();
}

export function useAdoptStartedThingySession(): void {
  const { pathname, search } = useLocation();
  const rebasePath = useRebasePath();
  const queryClient = useQueryClient();
  const sessionPrefix = rebasePath("/c/");
  useEffect(() => {
    const marked = new URLSearchParams(search).get(THINGY_START_PARAM);
    if (marked) {
      if (starting !== null && starting !== marked) leaveThingyStart(starting, queryClient);
      starting = marked;
      return;
    }
    const thingyId = starting;
    if (thingyId === null) return;
    if (!pathname.startsWith(sessionPrefix)) {
      // Left New session without starting it: for another page, or a plain New session.
      leaveThingyStart(thingyId, queryClient);
      return;
    }
    const sessionId = decodeURIComponent(pathname.slice(sessionPrefix.length).replace(/\/$/, ""));
    // The temporary ID while the session is created; the real one replaces it.
    if (!isStartedSessionId(sessionId)) return;
    starting = null;
    unbuilt.delete(thingyId);
    adoptStartedThingySession(thingyId, sessionId)
      .then(() => requestThingyRailTab(sessionId, thingyRailKey(thingyId)))
      // The session still runs; the thingy stays on its own page.
      .catch(() => undefined);
  }, [pathname, queryClient, search, sessionPrefix]);
}
