// ⌘K's commands that move between sessions: one step down or up the sidebar's list, as the
// numbered session shortcuts count it, and back to the session that was active last.

import { ArrowDownIcon, ArrowUpIcon, HistoryIcon } from "lucide-react";
import { useContext, useMemo } from "react";

import type { Conversation } from "@/hooks/useConversations";
import { SidebarDataContext } from "@/hooks/useSidebarData";
import {
  type SessionStepDirection,
  steppedSessionId,
  useSidebarSessionOrderStore,
} from "@/hooks/useSidebarSessionOrder";
import { useNavigate } from "@/lib/routing";
import { AVAILABLE, HIDDEN, type PaletteCommand } from "@/store/paletteCommands";

import { useCommandContext } from "./commandContext";

/** The most recently active session other than `openId`, as the recent-sessions switcher picks. */
export function recentSessionId(
  rows: readonly Conversation[],
  openId: string | null,
): string | undefined {
  let recent: Conversation | undefined;
  for (const row of rows) {
    if (row.id === openId || row.archived || row.provisional === true) continue;
    if (!recent || row.updated_at > recent.updated_at) recent = row;
  }
  return recent?.id;
}

export function useSessionNavigationCommands(): PaletteCommand[] {
  const navigate = useNavigate();
  const listed = useSidebarSessionOrderStore((state) => state.orderedSessionIds.length > 0);
  const openId = useCommandContext().session?.id ?? null;
  const loadedRows = useContext(SidebarDataContext)?.loadedRows;
  const recentId = useMemo(() => recentSessionId(loadedRows ?? [], openId), [loadedRows, openId]);

  return useMemo<PaletteCommand[]>(() => {
    // Read when run, so a step taken after the list changed lands on the row shown now.
    const step = (direction: SessionStepDirection) => () => {
      const { orderedSessionIds, activeSessionId } = useSidebarSessionOrderStore.getState();
      const target = steppedSessionId(orderedSessionIds, activeSessionId, direction);
      if (target && target !== activeSessionId) navigate(`/c/${target}`);
    };
    return [
      {
        id: "next-session",
        label: "Next session",
        shortcut: "session.next",
        icon: ArrowDownIcon,
        keywords: ["down", "below", "sidebar", "switch", "session"],
        // Only while a sidebar lists sessions to step through.
        availability: listed ? AVAILABLE : HIDDEN,
        run: step(1),
      },
      {
        id: "previous-session",
        label: "Previous session",
        shortcut: "session.previous",
        icon: ArrowUpIcon,
        keywords: ["up", "above", "sidebar", "switch", "session"],
        availability: listed ? AVAILABLE : HIDDEN,
        run: step(-1),
      },
      {
        id: "recent-session",
        label: "Switch to recent session",
        icon: HistoryIcon,
        keywords: ["last", "previous", "back", "recent", "toggle", "session"],
        availability: recentId ? AVAILABLE : HIDDEN,
        run: () => {
          if (recentId) navigate(`/c/${recentId}`);
        },
      },
    ];
  }, [navigate, listed, recentId]);
}
