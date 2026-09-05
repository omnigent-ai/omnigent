import { useCallback, useEffect, useRef, useState } from "react";
import { onBrowserActionRequest } from "@/lib/browserActionBus";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

export function browserViewId(conversationId: string, tabId: string | null): string {
  return tabId === null
    ? conversationId
    : `browser-tab:${encodeURIComponent(conversationId)}:${tabId}`;
}

export function useBrowserTabs(conversationId: string) {
  const [state, setState] = useState(() => {
    const saved = readSessionWorkspaceState(conversationId);
    const tabs = saved.openBrowsers ?? [];
    return {
      tabs,
      selected: tabs.includes(saved.selectedBrowserId ?? "") ? saved.selectedBrowserId! : null,
    };
  });
  const stateRef = useRef(state);
  const update = useCallback(
    (next: typeof state) => {
      stateRef.current = next;
      writeSessionWorkspaceState(conversationId, {
        openBrowsers: next.tabs,
        selectedBrowserId: next.selected,
      });
      setState(next);
    },
    [conversationId],
  );

  const select = (selected: string | null) => {
    update({ ...stateRef.current, selected });
  };

  useEffect(
    () =>
      onBrowserActionRequest((event, sourceConversationId) => {
        if (sourceConversationId === conversationId && event.action === "navigate") {
          update({ ...stateRef.current, selected: null });
        }
      }),
    [conversationId, update],
  );

  const add = () => {
    const selected = crypto.randomUUID();
    const tabs = [...stateRef.current.tabs, selected];
    update({ tabs, selected });
  };

  const close = async (tabId: string) => {
    const desktop = (
      window as unknown as {
        omnigentDesktop?: {
          browserClose?: (viewId: string) => Promise<{ ok: boolean }>;
        };
      }
    ).omnigentDesktop;
    const result = await desktop
      ?.browserClose?.(browserViewId(conversationId, tabId))
      .catch(() => ({ ok: false }));
    if (result && !result.ok) return;
    const previous = stateRef.current;
    const tabs = previous.tabs.filter((id) => id !== tabId);
    const selected =
      previous.selected === tabId
        ? (tabs[Math.max(0, previous.tabs.indexOf(tabId) - 1)] ?? null)
        : previous.selected;
    update({ tabs, selected });
  };

  return { ...state, add, close, select, viewId: browserViewId(conversationId, state.selected) };
}
