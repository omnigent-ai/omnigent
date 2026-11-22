// With quick_pick_palette on, ⌃] / ⌃[ switch sessions by recency: this records each session
// the user opens, offers the switcher every live session the client holds, and binds the chord.

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useContext } from "react";

import {
  useRecentSessionHotkey,
  useRecordRecentSessionVisits,
  useRegisterLiveSessions,
} from "@/hooks/useRecentSessionSwitch";
import { SidebarDataContext } from "@/hooks/useSidebarData";
import { useSidebarSessionOrderStore } from "@/hooks/useSidebarSessionOrder";
import { liveSessionIdsInCache } from "@/lib/liveSessions";

import { RecentSessionSwitcher } from "../RecentSessionSwitcher";

/** Mount once with quick_pick_palette on, beside the palette. */
export function RecentSessionSwitchHost() {
  const queryClient = useQueryClient();
  // Every live session the client has cached, including ones in a collapsed folder or
  // outside the sidebar's filter, not only the rows it shows.
  const readLiveSessionIds = useCallback(() => liveSessionIdsInCache(queryClient), [queryClient]);
  // Until the first page arrives, or fails, an empty cache doesn't mean there are no sessions.
  const mine = useContext(SidebarDataContext)?.mine;
  const known = mine === undefined || mine.data !== undefined || mine.isError;
  const failed = mine !== undefined && mine.data === undefined && mine.isError;
  useRegisterLiveSessions(readLiveSessionIds, known, failed);
  // The sidebar's open session: a sub-agent's visit counts as its root session's.
  useRecordRecentSessionVisits(
    useSidebarSessionOrderStore((state) => state.activeSessionId) ?? undefined,
  );
  useRecentSessionHotkey();
  return <RecentSessionSwitcher />;
}
