// ⌘K's commands that move between sessions: one step down or up the sidebar's list, as the
// numbered session shortcuts count it, and the recent-session switcher.

import { useQueryClient } from "@tanstack/react-query";
import { ArrowDownIcon, ArrowUpIcon, HistoryIcon } from "lucide-react";
import { useCallback, useMemo, useSyncExternalStore } from "react";

import {
  openRecentSessionSwitcher,
  reachableRecentSessionIds,
  useRecentSessionSwitchStore,
} from "@/hooks/useRecentSessionSwitch";
import {
  type SessionStepDirection,
  stepSidebarSession,
  useSidebarSessionOrderStore,
} from "@/hooks/useSidebarSessionOrder";
import { subscribeToSessionLists } from "@/lib/liveSessions";
import { useNavigate } from "@/lib/routing";
import { AVAILABLE, HIDDEN, type PaletteCommand } from "@/store/paletteCommands";

import { useCommandContext } from "./commandContext";

/** Whether the switcher has a session to open besides the ones in `openIds`. */
export function hasOtherLiveSession(
  liveIds: Iterable<string>,
  openIds: readonly (string | null)[],
): boolean {
  for (const id of liveIds) if (!openIds.includes(id)) return true;
  return false;
}

/**
 * Whether ⌃] has a session to switch to: a live visited session or sidebar row, including ones a
 * collapsed folder or the sidebar's filter hides, as the switcher's order holds them.
 */
function useCanSwitchToRecentSession(openId: string | null): boolean {
  const queryClient = useQueryClient();
  const openRootId = useSidebarSessionOrderStore((state) => state.activeSessionId);
  const subscribe = useCallback(
    (onChange: () => void) => {
      const stops = [
        subscribeToSessionLists(queryClient, onChange),
        useRecentSessionSwitchStore.subscribe(onChange),
        useSidebarSessionOrderStore.subscribe(onChange),
      ];
      return () => {
        for (const stop of stops) stop();
      };
    },
    [queryClient],
  );
  return useSyncExternalStore(subscribe, () =>
    hasOtherLiveSession(reachableRecentSessionIds(), [openId, openRootId]),
  );
}

export function useSessionNavigationCommands(): PaletteCommand[] {
  const navigate = useNavigate();
  const listed = useSidebarSessionOrderStore((state) => state.orderedSessionIds.length > 0);
  const openId = useCommandContext().session?.id ?? null;
  const canSwitchToRecent = useCanSwitchToRecentSession(openId);

  return useMemo<PaletteCommand[]>(() => {
    // Read when run, so a step taken after the list changed lands on the row shown now.
    const step = (direction: SessionStepDirection) => () => {
      stepSidebarSession(direction, navigate);
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
        shortcut: "session.recent",
        icon: HistoryIcon,
        keywords: ["last", "previous", "back", "recent", "toggle", "session", "switcher"],
        availability: canSwitchToRecent ? AVAILABLE : HIDDEN,
        // The switcher, one step back through the visits, opens once the palette has closed.
        movesFocus: true,
        run: () => {
          openRecentSessionSwitcher(1, { holding: false, navigate });
        },
      },
    ];
  }, [navigate, listed, canSwitchToRecent]);
}
