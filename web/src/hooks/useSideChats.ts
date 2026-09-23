import { useCallback, useEffect, useState } from "react";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

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

/**
 * Per-session side-chat tabs for the Workspace rail — a browser-local list of
 * child conversation ids, mirroring {@link useBrowserTabs}. The references live
 * in `sessionWorkspaceState` (localStorage), never on the server, so they are
 * per-device and per-browser: they persist across app restarts and reloads but
 * do not follow the user to another device.
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
      const next = mutate(readSideChatTabsState(conversationId));
      writeSessionWorkspaceState(conversationId, {
        openSideChats: next.tabs,
        selectedSideChatId: next.selected,
      });
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
    const id = `pending:${crypto.randomUUID()}`;
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

  /** Close a tab. Drops only the client-side reference — an ephemeral Codex
   *  fork dies with its process, and a generic child stays hidden server-side. */
  const close = useCallback(
    (childId: string) =>
      update((current) => {
        const index = current.tabs.indexOf(childId);
        if (index === -1) return current;
        const tabs = current.tabs.filter((id) => id !== childId);
        const selected =
          current.selected === childId ? (tabs[Math.max(0, index - 1)] ?? null) : current.selected;
        return { tabs, selected };
      }),
    [update],
  );

  return { tabs: state.tabs, selected: state.selected, open, openPending, rekey, close, select };
}
