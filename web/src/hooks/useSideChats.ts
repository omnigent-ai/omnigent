import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";
import { setSessionParent } from "@/lib/sessionHost";
import {
  MAX_OPEN_FILES,
  readSessionWorkspaceState,
  saveSessionWorkspaceState,
} from "@/lib/sessionWorkspaceState";
import { newPendingSideChatId } from "@/lib/sideChat";
import { subagentSessionIdFromTab } from "@/lib/subagentTabs";
import { isTempConvId } from "@/lib/tempConversationId";

interface SideChatTabsState {
  /** Open side-chat child conversation ids, in open order. */
  tabs: string[];
  /** Open sub-agent tabs (`subagentTabId` values), in open order. */
  subagentTabs: string[];
  /** The selected tab of either kind, or null when another rail view is active. */
  selected: string | null;
}

type TabsChange = (current: SideChatTabsState) => SideChatTabsState;

const MAX_UNSAVED_CHANGES = 50;

function readSideChatTabsState(conversationId: string): SideChatTabsState {
  const saved = readSessionWorkspaceState(conversationId);
  const tabs = saved.openSideChats ?? [];
  const subagentTabs = (saved.openSubagentTabs ?? []).filter(
    (id) => subagentSessionIdFromTab(id) !== null,
  );
  const selected = saved.selectedSideChatId ?? "";
  return {
    tabs,
    subagentTabs,
    selected: tabs.includes(selected) || subagentTabs.includes(selected) ? selected : null,
  };
}

interface SideChatRow {
  id: string;
  created_at: number;
  closed?: boolean;
}

function sideChatsQueryKey(conversationId: string): readonly unknown[] {
  return ["conversation", conversationId, "side_chats"];
}

/** The parent's side chats on the server, oldest first, split by whether they were closed. */
async function fetchSideChats(parentId: string): Promise<{ open: string[]; closed: string[] }> {
  const rows: SideChatRow[] = [];
  let after: string | null = null;
  // Sequential by design: each page's cursor comes from the previous page.
  /* oxlint-disable no-await-in-loop */
  do {
    const query = new URLSearchParams({ limit: "100", ...(after ? { after } : {}) });
    const res = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(parentId)}/side_chats?${query.toString()}`,
    );
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const page = (await res.json()) as {
      data: SideChatRow[];
      has_more?: boolean;
      last_id?: string | null;
    };
    rows.push(...page.data);
    after = page.has_more && page.last_id ? page.last_id : null;
  } while (after !== null);
  /* oxlint-enable no-await-in-loop */
  // Pages come in id order; tabs open in creation order.
  rows.sort((a, b) => a.created_at - b.created_at || a.id.localeCompare(b.id));
  const open: string[] = [];
  const closed: string[] = [];
  for (const row of rows) {
    // Side chats run on the parent's runner, so route them by its host.
    setSessionParent(row.id, parentId);
    (row.closed ? closed : open).push(row.id);
  }
  return { open, closed };
}

/**
 * Per-session side-chat tabs for the Workspace rail, mirroring
 * {@link useBrowserTabs}. Tab order and selection live in `sessionWorkspaceState`
 * (localStorage). Whenever the app loads or regains focus, the parent's
 * side-chat list on the server adds side chats opened on another device and
 * drops ones closed there.
 *
 * Opening (`open`) just records an already-created child id as a tab — the
 * child itself is created by the server (Codex's native ephemeral fork, or the
 * generic `POST /v1/sessions/{id}/side-chat`) and surfaced here once its
 * `session_created` event arrives. Sub-agent tabs (`subagentTabId` ids) are
 * kept in their own list and listed after the side chats.
 *
 * @param conversationId The parent (main) conversation whose rail owns these tabs.
 */
export function useSideChats(conversationId: string) {
  const [state, setState] = useState(() => readSideChatTabsState(conversationId));
  // Changes a failed storage write couldn't save. Each update replays them on
  // fresh storage, so they survive without undoing other windows' changes.
  const unsaved = useRef<{ conversationId: string; changes: TabsChange[] } | null>(null);
  // WorkspacePanel isn't remounted when the user navigates to another
  // conversation (conversationId is a prop, not a key), so reload this
  // conversation's own tabs on change — otherwise the previous conversation's
  // side-chat tabs would linger in the new one's rail.
  useEffect(() => {
    unsaved.current = null;
    setState(readSideChatTabsState(conversationId));
  }, [conversationId]);
  const update = useCallback(
    (mutate: TabsChange) => {
      const pending =
        unsaved.current?.conversationId === conversationId ? unsaved.current.changes : [];
      const changes = [...pending, mutate];
      const stored = readSideChatTabsState(conversationId);
      const next = changes.reduce((current, change) => change(current), stored);
      if (pending.length === 0 && next === stored) {
        // Nothing to save; still show what other windows saved.
        setState(stored);
        return;
      }
      const saved = saveSessionWorkspaceState(conversationId, {
        openSideChats: next.tabs,
        // Omitted when empty, so a session without sub-agent tabs is stored as before.
        openSubagentTabs: next.subagentTabs.length > 0 ? next.subagentTabs : undefined,
        selectedSideChatId: next.selected,
      });
      // Past the cap, keep only the result so a long storage outage can't grow the replay.
      const kept = changes.length > MAX_UNSAVED_CHANGES ? [() => next] : changes;
      unsaved.current = saved ? null : { conversationId, changes: kept };
      setState(next);
    },
    [conversationId],
  );

  const queryClient = useQueryClient();
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
    const syncedTabs = (tabs: string[]): string[] => {
      // A tab the list doesn't name (pending, just created, or an older side chat) stays.
      const kept = tabs.filter((id) => !closed.includes(id));
      // Storage holds at most MAX_OPEN_FILES tabs, so only the newest listed ones
      // fill the free slots and a kept tab is never pushed out.
      const room = MAX_OPEN_FILES - kept.length;
      const missing = open.filter((id) => !kept.includes(id));
      return room > 0 ? [...kept, ...missing.slice(-room)] : kept;
    };
    // Through update, so tabs a failed storage write is still holding are synced too.
    update((latest) => {
      const tabs = syncedTabs(latest.tabs);
      const selected =
        latest.selected !== null && closed.includes(latest.selected) ? null : latest.selected;
      const same =
        tabs.length === latest.tabs.length && tabs.every((id, index) => id === latest.tabs[index]);
      return same && selected === latest.selected ? latest : { ...latest, tabs, selected };
    });
  }, [serverSideChats, conversationId, update]);

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
    update((current) => ({ ...current, tabs: [...current.tabs, id], selected: id }));
    return id;
  }, [update]);

  /** Add a child id as a tab and select it. Idempotent: an already-open child
   *  is re-selected, not duplicated (a create both returns the id and fires a
   *  `session_created`, so `open` can be reached twice for one child). */
  const open = useCallback(
    (childId: string) =>
      update((current) => {
        const key = subagentSessionIdFromTab(childId) === null ? "tabs" : "subagentTabs";
        const list = current[key];
        return {
          ...current,
          [key]: list.includes(childId) ? list : [...list, childId],
          selected: childId,
        };
      }),
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
          return { ...current, tabs: current.tabs.filter((id) => id !== oldId), selected };
        }
        const tabs = [...current.tabs];
        tabs[index] = newId;
        return { ...current, tabs, selected };
      }),
    [update],
  );

  /** Close a tab, after the server has closed a real side chat. A closed
   *  selected tab hands the selection to its left neighbour in the strip. */
  const close = useCallback(
    (childId: string) => {
      update((current) => {
        const strip = [...current.tabs, ...current.subagentTabs];
        const index = strip.indexOf(childId);
        if (index === -1) return current;
        const remaining = strip.filter((id) => id !== childId);
        const selected =
          current.selected === childId
            ? (remaining[Math.max(0, index - 1)] ?? null)
            : current.selected;
        return {
          tabs: current.tabs.filter((id) => id !== childId),
          subagentTabs: current.subagentTabs.filter((id) => id !== childId),
          selected,
        };
      });
      // A list fetched before the close must not reopen the tab.
      void queryClient.invalidateQueries({ queryKey: sideChatsQueryKey(conversationId) });
    },
    [update, queryClient, conversationId],
  );

  // Side chats, then sub-agent tabs: the order the strip shows them in.
  const tabs = useMemo(() => [...state.tabs, ...state.subagentTabs], [state]);
  return {
    tabs,
    selected: state.selected,
    open,
    openPending,
    rekey,
    close,
    select,
  };
}
