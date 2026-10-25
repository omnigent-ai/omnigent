import { useCallback, useEffect, useState } from "react";
import { onBrowserActionRequest } from "@/lib/browserActionBus";
import { onInAppLinkOpen } from "@/lib/openLinkInApp";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

/** Stable soft-tab id for the browser view driven by agents and opted-in links. */
export const AGENT_BROWSER_TAB_ID = "agent-browser";

export function browserViewId(conversationId: string, tabId: string): string {
  return tabId === AGENT_BROWSER_TAB_ID
    ? conversationId
    : `browser-tab:${encodeURIComponent(conversationId)}:${tabId}`;
}

/** Owning session ID for a browser view ID; mirrors the Electron view-registry parser. */
export function browserViewOwnerId(viewId: string): string {
  const tab = /^browser-tab:([^:]+):[^:]+$/.exec(viewId);
  if (!tab) return viewId;
  try {
    return decodeURIComponent(tab[1]);
  } catch {
    return viewId;
  }
}

interface BrowserTabsState {
  tabs: string[];
  selected: string | null;
}

const agentNavigationEpochs = new Map<string, number>();

function agentNavigationEpoch(conversationId: string): number {
  return agentNavigationEpochs.get(conversationId) ?? 0;
}

// Mounted hooks re-read a session's tabs when an agent or another hook changes them.
const tabListeners = new Set<(conversationId: string) => void>();

function readBrowserTabsState(conversationId: string): BrowserTabsState {
  const saved = readSessionWorkspaceState(conversationId);
  const tabs = saved.openBrowsers ?? [];
  return {
    tabs,
    selected: tabs.includes(saved.selectedBrowserId ?? "") ? saved.selectedBrowserId! : null,
  };
}

function writeBrowserTabsState(
  conversationId: string,
  next: BrowserTabsState,
  agentBrowserId?: string | null,
): BrowserTabsState {
  writeSessionWorkspaceState(conversationId, {
    openBrowsers: next.tabs,
    selectedBrowserId: next.selected,
    ...(agentBrowserId === undefined ? {} : { agentBrowserId }),
  });
  for (const listener of tabListeners) listener(conversationId);
  return next;
}

/** The tab agents act on: the one they last opened or selected, else the agent browser. */
export function agentBrowserTabId(conversationId: string): string {
  const saved = readSessionWorkspaceState(conversationId);
  const chosen = saved.agentBrowserId;
  return chosen && (saved.openBrowsers ?? []).includes(chosen) ? chosen : AGENT_BROWSER_TAB_ID;
}

/** The session's open browser tab ids, in rail order. */
export function browserTabIds(conversationId: string): string[] {
  return readBrowserTabsState(conversationId).tabs;
}

/** Persist an agent-driven browser tab (by default the agent's current one) as
 *  a selected soft tab for a session. In-app links use the reserved tab. */
export function openAgentBrowserTab(
  conversationId: string,
  tabId: string = agentBrowserTabId(conversationId),
): BrowserTabsState {
  agentNavigationEpochs.set(conversationId, agentNavigationEpoch(conversationId) + 1);
  const current = readBrowserTabsState(conversationId);
  return writeBrowserTabsState(conversationId, {
    tabs: current.tabs.includes(tabId) ? current.tabs : [...current.tabs, tabId],
    selected: tabId,
  });
}

/** Open a new selected tab; `forAgent` also makes it the tab agents act on. */
export function addBrowserTab(conversationId: string, { forAgent = false } = {}): string {
  const tabId = crypto.randomUUID();
  const current = readBrowserTabsState(conversationId);
  writeBrowserTabsState(
    conversationId,
    { tabs: [...current.tabs, tabId], selected: tabId },
    forAgent ? tabId : undefined,
  );
  return tabId;
}

/** Make an open tab the one agents act on, and show it. Returns false if it isn't open. */
export function selectAgentBrowserTab(conversationId: string, tabId: string): boolean {
  const current = readBrowserTabsState(conversationId);
  if (!current.tabs.includes(tabId)) return false;
  writeBrowserTabsState(conversationId, { ...current, selected: tabId }, tabId);
  return true;
}

/** Close a tab's view and drop it from the rail; false when the view would not close. */
export async function closeBrowserTab(conversationId: string, tabId: string): Promise<boolean> {
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
  // A fresh agent/link navigation supersedes an older close of the reserved
  // agent view. Keep the recreated tab selected when the close resolves late.
  if (tabId === AGENT_BROWSER_TAB_ID && agentNavigationEpoch(conversationId) !== navigationEpoch) {
    return true;
  }
  const previous = readBrowserTabsState(conversationId);
  const index = previous.tabs.indexOf(tabId);
  if (index === -1) return true;
  const tabs = previous.tabs.filter((id) => id !== tabId);
  const selected =
    previous.selected === tabId ? (tabs[Math.max(0, index - 1)] ?? null) : previous.selected;
  // Agents move to the newly shown tab when theirs closes, like a browser does.
  const agentClosed = agentBrowserTabId(conversationId) === tabId;
  writeBrowserTabsState(conversationId, { tabs, selected }, agentClosed ? selected : undefined);
  return true;
}

export function useBrowserTabs(conversationId: string) {
  const [state, setState] = useState(() => readBrowserTabsState(conversationId));
  useEffect(() => {
    setState(readBrowserTabsState(conversationId));
    const listener = (changed: string) => {
      if (changed === conversationId) setState(readBrowserTabsState(conversationId));
    };
    tabListeners.add(listener);
    return () => {
      tabListeners.delete(listener);
    };
  }, [conversationId]);

  const select = useCallback(
    (selected: string | null) => {
      writeBrowserTabsState(conversationId, { ...readBrowserTabsState(conversationId), selected });
    },
    [conversationId],
  );

  useEffect(() => {
    const unsubscribeAction = onBrowserActionRequest((event, sourceConversationId) => {
      if (sourceConversationId === conversationId && event.action === "navigate") {
        openAgentBrowserTab(conversationId);
      }
    });
    const unsubscribeLink = onInAppLinkOpen((sourceConversationId) => {
      if (sourceConversationId === conversationId) {
        openAgentBrowserTab(conversationId, AGENT_BROWSER_TAB_ID);
      }
    });
    return () => {
      unsubscribeAction();
      unsubscribeLink();
    };
  }, [conversationId]);

  const add = () => {
    addBrowserTab(conversationId);
  };

  const close = (tabId: string): Promise<boolean> => closeBrowserTab(conversationId, tabId);

  return {
    ...state,
    add,
    close,
    select,
    viewId: state.selected === null ? null : browserViewId(conversationId, state.selected),
    agentBrowser: state.selected === AGENT_BROWSER_TAB_ID,
  };
}
