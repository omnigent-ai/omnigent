import { useCallback, useEffect, useState } from "react";
import { onBrowserActionRequest } from "@/lib/browserActionBus";
import { onInAppLinkOpen } from "@/lib/openLinkInApp";
import { normalizeBrowserTabName } from "@/lib/browserTabLabel";
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
  /** Manual tab names keyed by tab id; unnamed tabs follow their URL. */
  names: Record<string, string>;
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
    names: saved.browserNames ?? {},
  };
}

function writeBrowserTabsState(conversationId: string, next: BrowserTabsState): void {
  writeSessionWorkspaceState(conversationId, {
    openBrowsers: next.tabs,
    selectedBrowserId: next.selected,
    browserNames: next.names,
  });
}

function withOpenTabNames(state: BrowserTabsState): BrowserTabsState {
  const names = Object.entries(state.names).filter(([tabId]) => state.tabs.includes(tabId));
  return { ...state, names: Object.fromEntries(names) };
}

function withAgentBrowserSelected(current: BrowserTabsState): BrowserTabsState {
  return {
    ...current,
    tabs: current.tabs.includes(AGENT_BROWSER_TAB_ID)
      ? current.tabs
      : [...current.tabs, AGENT_BROWSER_TAB_ID],
    selected: AGENT_BROWSER_TAB_ID,
  };
}

/** Persist the agent/link browser as a selected soft tab for a session. */
export function openAgentBrowserTab(conversationId: string): BrowserTabsState {
  agentNavigationEpochs.set(conversationId, agentNavigationEpoch(conversationId) + 1);
  const next = withAgentBrowserSelected(readBrowserTabsState(conversationId));
  writeBrowserTabsState(conversationId, next);
  return next;
}

export function useBrowserTabs(conversationId: string) {
  const [state, setState] = useState(() => readBrowserTabsState(conversationId));
  useEffect(() => {
    setState(readBrowserTabsState(conversationId));
  }, [conversationId]);

  const update = useCallback(
    (mutate: (current: BrowserTabsState) => BrowserTabsState) => {
      const current = readBrowserTabsState(conversationId);
      const mutated = mutate(current);
      // A no-op (e.g. a late close of a gone tab) must not recreate a cleared entry.
      if (mutated === current) {
        setState(current);
        return;
      }
      // Drop names of closed tabs so memory matches what is saved.
      const next = withOpenTabNames(mutated);
      writeBrowserTabsState(conversationId, next);
      setState(next);
    },
    [conversationId],
  );

  const select = (selected: string | null) => {
    update((current) => ({ ...current, selected }));
  };

  useEffect(() => {
    const selectAgentBrowser = (sourceConversationId: string) => {
      if (sourceConversationId === conversationId) {
        setState(openAgentBrowserTab(conversationId));
      }
    };
    const unsubscribeAction = onBrowserActionRequest((event, sourceConversationId) => {
      if (sourceConversationId === conversationId && event.action === "navigate") {
        selectAgentBrowser(sourceConversationId);
      }
    });
    const unsubscribeLink = onInAppLinkOpen(selectAgentBrowser);
    return () => {
      unsubscribeAction();
      unsubscribeLink();
    };
  }, [conversationId]);

  const add = () => {
    const selected = crypto.randomUUID();
    update((current) => ({ ...current, tabs: [...current.tabs, selected], selected }));
  };

  /** Set a tab's manual name; a blank name returns it to its URL label. */
  const rename = (tabId: string, name: string) => {
    const trimmed = normalizeBrowserTabName(name);
    update((current) => {
      if (!current.tabs.includes(tabId)) return current;
      const names = Object.fromEntries(
        Object.entries(current.names).filter(([namedTabId]) => namedTabId !== tabId),
      );
      return { ...current, names: trimmed === "" ? names : { ...names, [tabId]: trimmed } };
    });
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
    // A fresh agent/link navigation supersedes an older close of the reserved
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
      return { ...previous, tabs, selected };
    });
    return true;
  };

  return {
    ...state,
    add,
    close,
    rename,
    select,
    viewId: state.selected === null ? null : browserViewId(conversationId, state.selected),
    agentBrowser: state.selected === AGENT_BROWSER_TAB_ID,
  };
}
