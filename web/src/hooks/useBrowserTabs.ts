import { useCallback, useEffect, useState } from "react";
import { onBrowserActionRequest } from "@/lib/browserActionBus";
import { AGENT_BROWSER_TAB_ID, browserViewId } from "@/lib/browserTabs";
import { onInAppLinkOpen } from "@/lib/openLinkInApp";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

export { AGENT_BROWSER_TAB_ID, browserViewId, browserViewOwnerId } from "@/lib/browserTabs";

interface BrowserTabsState {
  tabs: string[];
  selected: string | null;
}

const agentNavigationEpochs = new Map<string, number>();

function agentNavigationEpoch(conversationId: string): number {
  return agentNavigationEpochs.get(conversationId) ?? 0;
}

function readBrowserTabsState(conversationId: string): BrowserTabsState {
  const saved = readSessionWorkspaceState(conversationId);
  const tabs = saved.openBrowsers ?? [];
  return {
    tabs,
    selected: tabs.includes(saved.selectedBrowserId ?? "") ? saved.selectedBrowserId! : null,
  };
}

/** Persist a browser view as a selected soft tab for its owning session. */
export function openBrowserTab(conversationId: string, tabId: string): BrowserTabsState {
  if (tabId === AGENT_BROWSER_TAB_ID) {
    agentNavigationEpochs.set(conversationId, agentNavigationEpoch(conversationId) + 1);
  }
  const current = readBrowserTabsState(conversationId);
  const next = {
    tabs: current.tabs.includes(tabId) ? current.tabs : [...current.tabs, tabId],
    selected: tabId,
  };
  writeSessionWorkspaceState(conversationId, {
    openBrowsers: next.tabs,
    selectedBrowserId: next.selected,
  });
  return next;
}

/** Persist the agent browser as a selected soft tab for a session. */
export function openAgentBrowserTab(conversationId: string): BrowserTabsState {
  return openBrowserTab(conversationId, AGENT_BROWSER_TAB_ID);
}

export function useBrowserTabs(conversationId: string) {
  const [state, setState] = useState(() => readBrowserTabsState(conversationId));
  useEffect(() => {
    setState(readBrowserTabsState(conversationId));
  }, [conversationId]);

  const update = useCallback(
    (mutate: (current: BrowserTabsState) => BrowserTabsState) => {
      const next = mutate(readBrowserTabsState(conversationId));
      writeSessionWorkspaceState(conversationId, {
        openBrowsers: next.tabs,
        selectedBrowserId: next.selected,
      });
      setState(next);
    },
    [conversationId],
  );

  const select = (selected: string | null) => {
    update((current) => ({ ...current, selected }));
  };

  useEffect(() => {
    const selectBrowser = (sourceConversationId: string, tabId: string) => {
      if (sourceConversationId === conversationId) {
        setState(openBrowserTab(conversationId, tabId));
      }
    };
    const unsubscribeAction = onBrowserActionRequest((event, sourceConversationId) => {
      if (sourceConversationId === conversationId && event.action === "navigate") {
        selectBrowser(sourceConversationId, AGENT_BROWSER_TAB_ID);
      }
    });
    const unsubscribeLink = onInAppLinkOpen(selectBrowser);
    return () => {
      unsubscribeAction();
      unsubscribeLink();
    };
  }, [conversationId]);

  const add = () => {
    const selected = crypto.randomUUID();
    update((current) => ({ tabs: [...current.tabs, selected], selected }));
  };

  const close = async (tabId: string): Promise<boolean> => {
    const navigationEpoch = agentNavigationEpoch(conversationId);
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
    if (result && !result.ok) return false;
    // A fresh agent navigation supersedes an older close of the reserved
    // agent view. Keep the recreated tab selected when the close resolves late.
    if (
      tabId === AGENT_BROWSER_TAB_ID &&
      agentNavigationEpoch(conversationId) !== navigationEpoch
    ) {
      return true;
    }
    update((previous) => {
      const index = previous.tabs.indexOf(tabId);
      if (index === -1) return previous;
      const tabs = previous.tabs.filter((id) => id !== tabId);
      const selected =
        previous.selected === tabId ? (tabs[Math.max(0, index - 1)] ?? null) : previous.selected;
      return { tabs, selected };
    });
    return true;
  };

  return {
    ...state,
    add,
    close,
    select,
    viewId: state.selected === null ? null : browserViewId(conversationId, state.selected),
    agentBrowser: state.selected === AGENT_BROWSER_TAB_ID,
  };
}
