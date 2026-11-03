import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { readSessionWorkspaceState, saveSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { newPendingSideChatId } from "@/lib/sideChat";
import { subagentSessionIdFromTab } from "@/lib/subagentTabs";

interface SideChatTabsState {
  /** Open side-chat child conversation ids, in open order. */
  tabs: string[];
  /** Open sub-agent tabs (`subagentTabId` values), in open order. */
  subagentTabs: string[];
  /** The selected tab of either kind, or null when another rail view is active. */
  selected: string | null;
  /** The server's side-chat list at the last {@link useSideChats} `sync`. */
  synced: string[];
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
    synced: saved.syncedSideChats ?? [],
  };
}

/** Remove `childId`'s tab, handing a selection to its left neighbour in the strip. */
function withoutTab(current: SideChatTabsState, childId: string): SideChatTabsState {
  const strip = [...current.tabs, ...current.subagentTabs];
  const index = strip.indexOf(childId);
  if (index === -1) return current;
  const remaining = strip.filter((id) => id !== childId);
  const selected =
    current.selected === childId ? (remaining[Math.max(0, index - 1)] ?? null) : current.selected;
  return {
    ...current,
    tabs: current.tabs.filter((id) => id !== childId),
    subagentTabs: current.subagentTabs.filter((id) => id !== childId),
    selected,
  };
}

/**
 * Per-session side-chat tabs for the Workspace rail — a list of child
 * conversation ids, mirroring {@link useBrowserTabs}. The tabs live in
 * `sessionWorkspaceState` (localStorage); `sync` merges in the generic side
 * chats the server lists for the user, so those follow them to other devices.
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
      if (next === stored) {
        setState(next);
        return;
      }
      const saved = saveSessionWorkspaceState(conversationId, {
        openSideChats: next.tabs,
        // Omitted when empty, so a session without sub-agent tabs is stored as before.
        openSubagentTabs: next.subagentTabs.length > 0 ? next.subagentTabs : undefined,
        selectedSideChatId: next.selected,
        syncedSideChats: next.synced.length > 0 ? next.synced : undefined,
      });
      // Past the cap, keep only the result so a long storage outage can't grow the replay.
      const kept = changes.length > MAX_UNSAVED_CHANGES ? [() => next] : changes;
      unsaved.current = saved ? null : { conversationId, changes: kept };
      setState(next);
    },
    [conversationId],
  );

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

  /** Close a tab. Drops only the client-side reference — an ephemeral Codex
   *  fork dies with its process, and a generic child stays hidden server-side.
   *  A closed selected tab hands the selection to its left neighbour in the strip. */
  const close = useCallback(
    (childId: string) => update((current) => withoutTab(current, childId)),
    [update],
  );

  /** Reconcile with the server's side-chat list (oldest first): open tabs for
   *  side chats new to the list without changing the selection, and close
   *  those another device removed from it. Tabs the list never named (Codex
   *  forks, pending tabs, older side chats) are left alone. */
  const sync = useCallback(
    (listed: string[]) =>
      update((current) => {
        const synced = new Set(current.synced);
        const listedSet = new Set(listed);
        let next = current;
        for (const id of current.synced) {
          if (!listedSet.has(id)) next = withoutTab(next, id);
        }
        const added = listed.filter((id) => !synced.has(id) && !next.tabs.includes(id));
        const sameList =
          listed.length === current.synced.length && listed.every((id) => synced.has(id));
        if (next === current && added.length === 0 && sameList) return current;
        return { ...next, tabs: [...next.tabs, ...added], synced: listed };
      }),
    [update],
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
    sync,
    select,
  };
}
