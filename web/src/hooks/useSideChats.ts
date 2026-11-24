import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  MAX_OPEN_TABS,
  readSessionWorkspaceState,
  saveSessionWorkspaceState,
} from "@/lib/sessionWorkspaceState";
import { newPendingSideChatId } from "@/lib/sideChat";
import { setSessionParent } from "@/lib/sessionHost";
import { subagentTabId, subagentSessionIdFromTab } from "@/lib/subagentTabs";

interface SideChatTabsState {
  /** Open side-chat child conversation ids, in strip order. */
  tabs: string[];
  /** Open sub-agent tabs (`subagentTabId` values), in open order. */
  subagentTabs: string[];
  /** Side chats closed on this device, excluded from automatic discovery. */
  closed: string[];
  /** The selected tab of either kind, or null when another rail view is active. */
  selected: string | null;
  /** Each open side chat's label number ("Side chat 3"), kept until it closes. */
  numbers: Record<string, number>;
  /** Pinned side chats; always the leading entries of `tabs`. */
  pinned: string[];
}

type TabsChange = (current: SideChatTabsState) => SideChatTabsState;

const MAX_UNSAVED_CHANGES = 50;

/**
 * Keep pinned side chats first and give every open side chat a label number.
 * A new tab takes the next number after the highest open one, so closing a tab
 * never renumbers the rest; tabs saved before numbering keep their old labels.
 * A tab missing from `state.numbers` keeps its `displayed` number when no other
 * open tab holds it.
 */
function normalizeSideChatTabs(
  state: SideChatTabsState,
  displayed: Record<string, number> = {},
): SideChatTabsState {
  const pinned = state.pinned.filter((id) => state.tabs.includes(id));
  const tabs = [...pinned, ...state.tabs.filter((id) => !pinned.includes(id))];
  const numbers: Record<string, number> = {};
  const used = new Set<number>();
  const assign = (id: string, number: number | undefined) => {
    if (numbers[id] !== undefined || number === undefined || used.has(number)) return;
    numbers[id] = number;
    used.add(number);
  };
  for (const id of tabs) assign(id, state.numbers[id]);
  for (const id of tabs) assign(id, displayed[id]);
  let highest = Math.max(0, ...used);
  for (const id of tabs) {
    if (numbers[id] === undefined) numbers[id] = ++highest;
  }
  return { ...state, tabs, pinned, numbers };
}

function readSideChatTabsState(conversationId: string): SideChatTabsState {
  const saved = readSessionWorkspaceState(conversationId);
  const tabs = saved.openSideChats ?? [];
  const subagentTabs = (saved.openSubagentTabs ?? []).filter(
    (id) => subagentSessionIdFromTab(id) !== null,
  );
  const selected = saved.selectedSideChatId ?? "";
  return normalizeSideChatTabs({
    tabs,
    subagentTabs,
    closed: saved.closedSideChats ?? [],
    selected: tabs.includes(selected) || subagentTabs.includes(selected) ? selected : null,
    numbers: saved.sideChatNumbers ?? {},
    pinned: saved.pinnedSideChats ?? [],
  });
}

/** Whether `id` and `target` are open side chats in the same (pinned or unpinned) group. */
function sameSideChatGroup(state: SideChatTabsState, id: string, target: string): boolean {
  return (
    state.tabs.includes(id) &&
    state.tabs.includes(target) &&
    state.pinned.includes(id) === state.pinned.includes(target)
  );
}

/** Per-session side-chat and sub-agent tabs, with browser-local selection and closure. */
export function useSideChats(conversationId: string) {
  const [state, setState] = useState(() => readSideChatTabsState(conversationId));
  const displayedNumbers = useRef(state.numbers);
  useLayoutEffect(() => {
    displayedNumbers.current = state.numbers;
  }, [state.numbers]);
  // Changes a failed storage write couldn't save. Each update replays them on
  // fresh storage, so they survive without undoing other windows' changes.
  const unsaved = useRef<{ conversationId: string; changes: TabsChange[] } | null>(null);
  // WorkspacePanel isn't keyed by conversationId, so reload tabs during render
  // on a switch; the previous session's tabs never reach the new first frame.
  const [stateConversationId, setStateConversationId] = useState(conversationId);
  if (stateConversationId !== conversationId) {
    setStateConversationId(conversationId);
    setState(readSideChatTabsState(conversationId));
  }
  useEffect(() => {
    unsaved.current = null;
  }, [conversationId]);
  const update = useCallback(
    (mutate: TabsChange) => {
      const pending =
        unsaved.current?.conversationId === conversationId ? unsaved.current.changes : [];
      const changes = [...pending, mutate];
      // Replayed tabs keep the numbers this window shows unless another window took them.
      const displayed = pending.length > 0 ? displayedNumbers.current : {};
      const next = changes.reduce(
        (current, change) => normalizeSideChatTabs(change(current), displayed),
        readSideChatTabsState(conversationId),
      );
      const saved = saveSessionWorkspaceState(conversationId, {
        openSideChats: next.tabs,
        closedSideChats: next.closed.length > 0 ? next.closed : undefined,
        // Omitted when empty, so a session without sub-agent tabs is stored as before.
        openSubagentTabs: next.subagentTabs.length > 0 ? next.subagentTabs : undefined,
        selectedSideChatId: next.selected,
        sideChatNumbers: next.tabs.length > 0 ? next.numbers : undefined,
        pinnedSideChats: next.pinned.length > 0 ? next.pinned : undefined,
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
          closed: current.closed.filter(
            (id) => id !== (subagentSessionIdFromTab(childId) ?? childId),
          ),
          selected: childId,
        };
      }),
    [update],
  );

  /** Add discovered chats without switching panels or reviving locally closed tabs. */
  const restore = useCallback(
    (childIds: string[]) => {
      for (const id of childIds) setSessionParent(id, conversationId);
      update((current) => {
        const added = childIds
          .filter(
            (id) =>
              !current.tabs.includes(id) &&
              !current.closed.includes(id) &&
              !current.subagentTabs.includes(subagentTabId(id)),
          )
          .slice(0, Math.max(0, MAX_OPEN_TABS - current.tabs.length));
        return {
          ...current,
          tabs: [...current.tabs, ...added],
          selected: current.selected ?? added[0] ?? null,
        };
      });
    },
    [conversationId, update],
  );

  /** Replace a tab's id in place, preserving its position and selection. Used
   *  when a `pending:` tab's fork resolves to a real child id, so the tab does
   *  not disappear and reappear. No-op if `oldId` isn't open. The started chat
   *  keeps the pending tab's name and pin; if `newId` was already open (restored
   *  before the fork resolved), that duplicate tab merges into this one. */
  const rekey = useCallback(
    (oldId: string, newId: string) =>
      update((current) => {
        if (!current.tabs.includes(oldId)) return current;
        const replace = (id: string) => (id === oldId ? newId : id);
        return {
          ...current,
          tabs: current.tabs.filter((id) => id !== newId).map(replace),
          selected:
            current.selected === oldId || current.selected === newId ? newId : current.selected,
          numbers: {
            ...current.numbers,
            [newId]: current.numbers[oldId] ?? current.numbers[newId],
          },
          pinned: current.pinned.filter((id) => id !== newId).map(replace),
        };
      }),
    [update],
  );

  /** Close a tab. Drops only the client-side reference — an ephemeral Codex
   *  fork dies with its process, and a generic child stays hidden server-side.
   *  A closed selected tab hands the selection to its left neighbour in the strip. */
  const close = useCallback(
    (childId: string) =>
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
          ...current,
          closed: !childId.startsWith("pending:")
            ? [...new Set([...current.closed, subagentSessionIdFromTab(childId) ?? childId])]
            : current.closed,
          tabs: current.tabs.filter((id) => id !== childId),
          subagentTabs: current.subagentTabs.filter((id) => id !== childId),
          selected,
        };
      }),
    [update],
  );

  /** Pin a side chat at the end of the pinned group, or unpin it to the start of the rest. */
  const setPinned = useCallback(
    (childId: string, pinned: boolean) =>
      update((current) => {
        if (!current.tabs.includes(childId) || current.pinned.includes(childId) === pinned) {
          return current;
        }
        const others = current.pinned.filter((id) => id !== childId);
        return { ...current, pinned: pinned ? [...others, childId] : others };
      }),
    [update],
  );

  /** Move a side chat to `targetId`'s slot. Tabs only move within their pinned or unpinned group. */
  const move = useCallback(
    (childId: string, targetId: string) =>
      update((current) => {
        if (childId === targetId || !sameSideChatGroup(current, childId, targetId)) return current;
        const reorder = (ids: string[]) => {
          if (!ids.includes(childId)) return ids;
          const without = ids.filter((id) => id !== childId);
          const at = ids.indexOf(targetId);
          return [...without.slice(0, at), childId, ...without.slice(at)];
        };
        return { ...current, tabs: reorder(current.tabs), pinned: reorder(current.pinned) };
      }),
    [update],
  );

  // Side chats, then sub-agent tabs: the order the strip shows them in.
  const tabs = useMemo(() => [...state.tabs, ...state.subagentTabs], [state]);
  return {
    tabs,
    selected: state.selected,
    numbers: state.numbers,
    pinned: state.pinned,
    open,
    openPending,
    rekey,
    restore,
    close,
    select,
    setPinned,
    move,
  };
}
