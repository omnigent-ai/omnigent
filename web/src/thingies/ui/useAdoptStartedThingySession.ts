// New session opened from a thingy carries the thingy's ID in its URL (`thingy_start`). New
// session creates the session at its usual path; once that path has the real session ID, the
// thingy is linked to the session and shown in its side panel.
import { useEffect, useRef } from "react";

import { useLocation, useRebasePath } from "@/lib/routing";
import { requestThingyRailTab } from "@/lib/thingyRailTabs";
import { THINGY_START_PARAM } from "@/thingies/host/thingyPages";
import {
  adoptStartedThingySession,
  isStartedSessionId,
  thingyRailKey,
} from "@/thingies/host/thingySession";

export function useAdoptStartedThingySession(): void {
  const { pathname, search } = useLocation();
  const rebasePath = useRebasePath();
  // The thingy whose New session this tab is on, until it starts a session or goes elsewhere.
  const starting = useRef<string | null>(null);
  const sessionPrefix = rebasePath("/c/");
  const landing = rebasePath("/").replace(/\/$/, "");
  useEffect(() => {
    const marked = new URLSearchParams(search).get(THINGY_START_PARAM);
    if (marked) {
      starting.current = marked;
      return;
    }
    const thingyId = starting.current;
    if (thingyId === null) return;
    if (!pathname.startsWith(sessionPrefix)) {
      // Left New session without starting it.
      if (pathname.replace(/\/$/, "") !== landing) starting.current = null;
      return;
    }
    const sessionId = decodeURIComponent(pathname.slice(sessionPrefix.length).replace(/\/$/, ""));
    // The temporary ID while the session is created; the real one replaces it.
    if (!isStartedSessionId(sessionId)) return;
    starting.current = null;
    adoptStartedThingySession(thingyId, sessionId)
      .then(() => requestThingyRailTab(sessionId, thingyRailKey(thingyId)))
      // The session still runs; the thingy stays on its own page.
      .catch(() => undefined);
  }, [landing, pathname, search, sessionPrefix]);
}
