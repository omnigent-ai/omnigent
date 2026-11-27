// The transient recent-session switcher that ⌃] / ⌃[ and ⌘K open. Its state lives in
// useRecentSessionSwitchStore; this renders it with the shared QuickPick and routes Enter,
// clicks, the arrow keys and Escape back to the store.

import { useQueryClient } from "@tanstack/react-query";
import { MessageSquareIcon } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useState } from "react";

import { QuickPickDialog, type QuickPickItem } from "@/components/ui/quick-pick";
import type { Conversation } from "@/hooks/useConversations";
import {
  cancelRecentSessionSwitcher,
  commitRecentSessionSwitcher,
  reconcileRecentSessionSwitcher,
  selectRecentSession,
  useRecentSessionSwitchStore,
} from "@/hooks/useRecentSessionSwitch";
import { liveSessionsInCache, subscribeToSessionLists } from "@/lib/liveSessions";
import { relativeTime } from "@/lib/relativeTime";

import { conversationDisplayLabel, getConversationAgentType } from "./sidebarNav";

const SESSION_ICON = <MessageSquareIcon aria-hidden className="size-4 text-muted-foreground" />;
const NO_SESSIONS: ReadonlyMap<string, Conversation> = new Map();
const ignoreQuery = () => undefined;

/** The switcher's row for `session`; the one open when it opened says so. */
export function recentSessionItem(
  session: Conversation,
  { current, now }: { current: boolean; now: number },
): QuickPickItem<string> {
  return {
    id: session.id,
    label: conversationDisplayLabel(session),
    icon: SESSION_ICON,
    detail: current ? "Current" : relativeTime(session.updated_at * 1000, now),
    accessory: (
      <span className="text-sm text-muted-foreground">{getConversationAgentType(session)}</span>
    ),
    rememberInHistory: false,
    value: session.id,
  };
}

/** Mount once with quick_pick_palette on. */
export function RecentSessionSwitcher() {
  const switcher = useRecentSessionSwitchStore((state) => state.switcher);
  const queryClient = useQueryClient();
  const open = switcher !== null;
  const order = switcher?.order ?? null;
  const fromSessionId = switcher?.fromSessionId ?? null;

  // The live sessions, kept current while open: one archived or deleted drops out of the
  // list in place, and the selection moves on to a live one.
  const [sessions, setSessions] = useState<ReadonlyMap<string, Conversation>>(NO_SESSIONS);
  // Before paint, so opening never flashes an empty list.
  useLayoutEffect(() => {
    if (!open) return;
    const refresh = () => setSessions(liveSessionsInCache(queryClient));
    refresh();
    return subscribeToSessionLists(queryClient, refresh);
  }, [open, queryClient]);
  useEffect(() => {
    if (open) reconcileRecentSessionSwitcher();
  }, [open, sessions]);

  const items = useMemo<QuickPickItem<string>[]>(() => {
    if (!order) return [];
    const now = Date.now();
    return order.flatMap((sessionId) => {
      const session = sessions.get(sessionId);
      return session
        ? [recentSessionItem(session, { current: sessionId === fromSessionId, now })]
        : [];
    });
  }, [order, fromSessionId, sessions]);

  const holding = switcher?.holding ?? false;
  return (
    <QuickPickDialog<string>
      // Steps with Ctrl-N/Ctrl-P, like ⌘K.
      stepsWithCtrlNP
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) cancelRecentSessionSwitcher();
      }}
      presentation="switcher"
      contentClassName="top-[18vh] sm:max-w-md"
      label="Switch to recent session"
      // Its field is read-only: the rows keep their recency order.
      query=""
      onQueryChange={ignoreQuery}
      description={
        holding
          ? "Keep Control held and press ] or [ to choose a session, then release Control to switch."
          : "Use the arrow keys to choose a session, then press Enter to switch."
      }
      segments={[{ sourceId: "items", ranking: "client", items }]}
      activeId={switcher?.selectedId ?? null}
      onActiveIdChange={selectRecentSession}
      emptyMessage={order ? "No other sessions" : "Loading sessions…"}
      summary={() => (holding ? "Release ⌃ to switch" : null)}
      acceptLabel="Switch"
      onAccept={(item) => commitRecentSessionSwitcher(item.value)}
    />
  );
}
