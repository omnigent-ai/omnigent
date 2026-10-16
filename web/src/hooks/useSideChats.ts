import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { fetchChildSessions } from "@/hooks/useChildSessions";
import { authenticatedFetch } from "@/lib/identity";
import { setSessionParent } from "@/lib/sessionHost";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { newPendingSideChatId } from "@/lib/sideChat";
import { isTempConvId } from "@/lib/tempConversationId";

interface SideChatTabsState {
  /** Open side-chat child conversation ids, in open order. */
  tabs: string[];
  /** The selected side-chat tab, or null when another rail view is active. */
  selected: string | null;
}

function readSideChatTabsState(conversationId: string): SideChatTabsState {
  const saved = readSessionWorkspaceState(conversationId);
  const tabs = saved.openSideChats ?? [];
  return {
    tabs,
    selected: tabs.includes(saved.selectedSideChatId ?? "") ? saved.selectedSideChatId! : null,
  };
}

function sideChatsQueryKey(conversationId: string): readonly unknown[] {
  return ["conversation", conversationId, "side_chats"];
}

/** The parent's side chats on the server, split by whether they were closed. */
async function fetchSideChats(parentId: string): Promise<{ open: string[]; closed: string[] }> {
  const params = new URLSearchParams({
    side_chat_parent_id: parentId,
    // The caller's own side chats only: tabs are personal.
    visibility: "mine",
    limit: "100",
  });
  const [res, children] = await Promise.all([
    authenticatedFetch(`/v1/sessions?${params.toString()}`),
    // Codex side chats are sub-agent children, readable through the parent.
    fetchChildSessions(parentId),
  ]);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as { data: { id: string; labels?: Record<string, string> }[] };
  const open: string[] = [];
  const closed: string[] = [];
  // Newest first, so reverse it to add tabs in the order they were opened.
  for (const row of [...json.data.reverse(), ...children.reverse()]) {
    // Only side chats carry this. Ordinary sub-agents don't, and an older
    // server ignores the filter and returns the sidebar list instead.
    if (row.labels?.["omnigent.side_chat.parent_id"] !== parentId) continue;
    // Side chats run on the parent's runner, so route them by its host.
    setSessionParent(row.id, parentId);
    (row.labels?.["omnigent.closed"] === "true" ? closed : open).push(row.id);
  }
  return { open, closed };
}

/**
 * Per-session side-chat tabs for the Workspace rail: a list of child
 * conversation ids, mirroring {@link useBrowserTabs}. The order and selection
 * live in `sessionWorkspaceState` (localStorage). The server lists the parent's
 * side chats too, so a side chat opened on another device is added here, and
 * one closed elsewhere is dropped, whenever this app loads or regains focus.
 *
 * Opening (`open`) just records an already-created child id as a tab — the
 * child itself is created by the server (Codex's native ephemeral fork, or the
 * generic `POST /v1/sessions/{id}/side-chat`) and surfaced here once its
 * `session_created` event arrives.
 *
 * @param conversationId The parent (main) conversation whose rail owns these tabs.
 */
export function useSideChats(conversationId: string) {
  const [state, setState] = useState(() => readSideChatTabsState(conversationId));
  // WorkspacePanel isn't remounted when the user navigates to another
  // conversation (conversationId is a prop, not a key), so reload this
  // conversation's own tabs on change — otherwise the previous conversation's
  // side-chat tabs would linger in the new one's rail.
  useEffect(() => {
    setState(readSideChatTabsState(conversationId));
  }, [conversationId]);
  const update = useCallback(
    (mutate: (current: SideChatTabsState) => SideChatTabsState) => {
      const current = readSideChatTabsState(conversationId);
      const next = mutate(current);
      if (next === current) return;
      writeSessionWorkspaceState(conversationId, {
        openSideChats: next.tabs,
        selectedSideChatId: next.selected,
      });
      setState(next);
    },
    [conversationId],
  );

  const { data: serverSideChats } = useQuery({
    queryKey: sideChatsQueryKey(conversationId),
    queryFn: () => fetchSideChats(conversationId),
    enabled: !isTempConvId(conversationId),
    staleTime: 0,
    refetchOnWindowFocus: true,
    retry: false,
  });
  useEffect(() => {
    if (!serverSideChats) return;
    const { open, closed } = serverSideChats;
    update((current) => {
      const added = open.filter((id) => !current.tabs.includes(id));
      if (added.length === 0 && !current.tabs.some((id) => closed.includes(id))) return current;
      const tabs = [...current.tabs.filter((id) => !closed.includes(id)), ...added];
      return { tabs, selected: tabs.includes(current.selected ?? "") ? current.selected : null };
    });
  }, [serverSideChats, update]);
  const queryClient = useQueryClient();

  /** Select a side-chat tab, or clear the selection (null). */
  const select = useCallback(
    (selected: string | null) => update((current) => ({ ...current, selected })),
    [update],
  );

  /** Open an empty (not-yet-created) side-chat tab and select it, returning its
   *  local `pending:` id. The fork is created only when the user sends the
   *  first message (see WorkspacePanel's startPendingSideChat), then this tab is
   *  closed and the real child's tab takes over. */
  const openPending = useCallback((): string => {
    const id = newPendingSideChatId();
    update((current) => ({ tabs: [...current.tabs, id], selected: id }));
    return id;
  }, [update]);

  /** Add a child id as a tab and select it. Idempotent: an already-open child
   *  is re-selected, not duplicated (a create both returns the id and fires a
   *  `session_created`, so `open` can be reached twice for one child). */
  const open = useCallback(
    (childId: string) =>
      update((current) => ({
        tabs: current.tabs.includes(childId) ? current.tabs : [...current.tabs, childId],
        selected: childId,
      })),
    [update],
  );

  /** Replace a tab's id in place, preserving its position and selection. Used
   *  when a `pending:` tab's fork resolves to a real child id, so the tab does
   *  not disappear and reappear. No-op if `oldId` isn't open; if `newId` is
   *  already a tab, the pending one is just dropped. */
  const rekey = useCallback(
    (oldId: string, newId: string) =>
      update((current) => {
        const index = current.tabs.indexOf(oldId);
        if (index === -1) return current;
        const selected = current.selected === oldId ? newId : current.selected;
        if (current.tabs.includes(newId)) {
          return { tabs: current.tabs.filter((id) => id !== oldId), selected };
        }
        const tabs = [...current.tabs];
        tabs[index] = newId;
        return { tabs, selected };
      }),
    [update],
  );

  /** Close a tab. A real side chat's tab closes after the server closes it. */
  const close = useCallback(
    (childId: string) => {
      update((current) => {
        const index = current.tabs.indexOf(childId);
        if (index === -1) return current;
        const tabs = current.tabs.filter((id) => id !== childId);
        const selected =
          current.selected === childId ? (tabs[Math.max(0, index - 1)] ?? null) : current.selected;
        return { tabs, selected };
      });
      // A list fetched before the close must not reopen the tab.
      void queryClient.invalidateQueries({ queryKey: sideChatsQueryKey(conversationId) });
    },
    [update, queryClient, conversationId],
  );

  return { tabs: state.tabs, selected: state.selected, open, openPending, rekey, close, select };
}
