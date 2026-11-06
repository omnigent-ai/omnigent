import { useCallback, useEffect, useRef, useState } from "react";
import { onBrowserActionRequest } from "@/lib/browserActionBus";
import { AGENT_BROWSER_TAB_ID, browserViewId } from "@/lib/browserTabs";
import { fetchSessionHostId } from "@/lib/fetchSessionHostId";
import { type BrowserHostOptions, navigationHostOptions } from "@/lib/nativeBridge";
import { onInAppLinkOpen } from "@/lib/openLinkInApp";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

export { AGENT_BROWSER_TAB_ID, browserViewId, browserViewOwnerId } from "@/lib/browserTabs";

/** How a tab close ended: the view closed, an agent navigation kept it, or it failed. */
export type BrowserTabCloseResult = "closed" | "kept" | "failed";

interface BrowserTabsState {
  tabs: string[];
  selected: string | null;
}

interface BrowserPage {
  url?: string;
  title?: string;
}

interface BrowserTabsBridge {
  browserOpenOrNavigate?: (
    viewId: string,
    url: string,
    bounds: undefined,
    opts: { force: boolean } & BrowserHostOptions,
  ) => Promise<{ ok: boolean; error?: string }>;
  browserHasView?: (
    viewId: string,
  ) => Promise<BrowserPage & { exists: boolean; pendingUrl?: string }>;
  onBrowserUrlChanged?: (
    callback: (page: BrowserPage & { conversationId: string }) => void,
  ) => () => void;
}

const agentNavigationEpochs = new Map<string, number>();
// Counts each session's tab selections, across mounts of its rail, so a slow reopen
// only selects its tab if nothing was selected after it was requested.
const browserSelections = new Map<string, number>();

// Mounted rails, told when a session's tabs change, so a reopen that a rail started
// before it remounted still reaches the one on screen.
const browserTabsListeners = new Set<(conversationId: string, next: BrowserTabsState) => void>();

// Each session's reopen requests in order, and the latest one whose tab was selected.
const reopenRequests = new Map<string, number>();
const shownReopens = new Map<string, number>();

function noteBrowserSelection(conversationId: string): number {
  const count = (browserSelections.get(conversationId) ?? 0) + 1;
  browserSelections.set(conversationId, count);
  return count;
}

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
  // The session on screen, so a late async update for another one only persists.
  const shownConversationRef = useRef(conversationId);
  shownConversationRef.current = conversationId;
  const [pages, setPages] = useState<Record<string, BrowserPage>>({});
  const viewIdsKey = JSON.stringify(
    state.tabs.map((tabId) => browserViewId(conversationId, tabId)),
  );
  useEffect(() => {
    setState(readBrowserTabsState(conversationId));
    // Showing the session again (or a remounted rail) counts as a selection, so a reopen
    // started before it doesn't move the tab shown now.
    noteBrowserSelection(conversationId);
  }, [conversationId]);
  useEffect(() => {
    const listener = (changedConversationId: string, next: BrowserTabsState) => {
      if (changedConversationId === shownConversationRef.current) setState(next);
    };
    browserTabsListeners.add(listener);
    return () => {
      browserTabsListeners.delete(listener);
    };
  }, []);

  useEffect(() => {
    const viewIds = new Set<string>(JSON.parse(viewIdsKey));
    setPages((current) => {
      const entries = Object.entries(current);
      const retained = entries.filter(([viewId]) => viewIds.has(viewId));
      return retained.length === entries.length ? current : Object.fromEntries(retained);
    });
    const bridge = (window as unknown as { omnigentDesktop?: BrowserTabsBridge }).omnigentDesktop;
    if (!bridge || viewIds.size === 0) return;
    let active = true;
    const updated = new Set<string>();
    const setPage = (viewId: string, page: BrowserPage) => {
      setPages((current) => ({ ...current, [viewId]: page }));
    };
    const unsubscribe = bridge.onBrowserUrlChanged?.((page) => {
      if (!active || !viewIds.has(page.conversationId)) return;
      updated.add(page.conversationId);
      setPage(page.conversationId, { url: page.url, title: page.title });
    });
    const restore = async (viewId: string) => {
      try {
        const page = await bridge.browserHasView?.(viewId);
        // Live updates take precedence over an earlier restore snapshot.
        if (!active || updated.has(viewId) || !page) return;
        setPage(
          viewId,
          page.exists
            ? {
                url: page.url && page.url !== "about:blank" ? page.url : page.pendingUrl,
                title: page.title,
              }
            : {},
        );
      } catch {
        // A closed view or an older shell can leave the default tab label.
      }
    };
    for (const viewId of viewIds) void restore(viewId);
    return () => {
      active = false;
      unsubscribe?.();
    };
  }, [viewIdsKey]);

  const labelFor = (tabId: string, index: number): string => {
    const page = pages[browserViewId(conversationId, tabId)];
    const title = page?.title?.trim();
    if (page?.url) {
      try {
        const hostname = new URL(page.url).hostname;
        // Chromium supplies the URL as the title for untitled documents.
        const urlTitle = page.url.replace(/^https?:\/\//, "");
        if (
          title &&
          title !== page.url &&
          title !== urlTitle &&
          title !== urlTitle.replace(/\/$/, "")
        ) {
          return title;
        }
        if (hostname) return hostname;
      } catch {
        // Blank or invalid URLs keep the numbered placeholder.
      }
    }
    return title && title !== "about:blank" ? title : `Browser ${index + 1}`;
  };

  const update = useCallback(
    (mutate: (current: BrowserTabsState) => BrowserTabsState) => {
      const next = mutate(readBrowserTabsState(conversationId));
      // Saving is best effort; mounted rails take the new state either way.
      writeSessionWorkspaceState(conversationId, {
        openBrowsers: next.tabs,
        selectedBrowserId: next.selected,
      });
      for (const listener of browserTabsListeners) listener(conversationId, next);
    },
    [conversationId],
  );

  const select = (selected: string | null) => {
    noteBrowserSelection(conversationId);
    update((current) => ({ ...current, selected }));
  };

  useEffect(() => {
    const selectBrowser = (sourceConversationId: string, tabId: string) => {
      if (sourceConversationId === conversationId) {
        noteBrowserSelection(conversationId);
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

  /** Open a tab, optionally loading `url` into its view before the pane mounts. */
  const add = (url?: string) => {
    noteBrowserSelection(conversationId);
    const selected = crypto.randomUUID();
    if (url) {
      const bridge = (window as unknown as { omnigentDesktop?: BrowserTabsBridge }).omnigentDesktop;
      void bridge
        ?.browserOpenOrNavigate?.(browserViewId(conversationId, selected), url, undefined, {
          force: true,
        })
        .catch(() => undefined);
    }
    update((current) => ({ tabs: [...current.tabs, selected], selected }));
  };

  /**
   * Reopen a closed tab at `url`. The tab is added only once its view opened, so a
   * refusal (too many views, a blocked URL) leaves no blank tab behind.
   */
  /**
   * Reopen a closed tab at `url`. `mayShow` lets the caller veto selecting it when the
   * view opens, such as after the user moved elsewhere in the workspace meanwhile.
   */
  const reopen = async (
    url: string,
    { mayShow = () => true }: { mayShow?: () => boolean } = {},
  ): Promise<{ ok: boolean; error?: string }> => {
    const tabId = crypto.randomUUID();
    const selection = browserSelections.get(conversationId) ?? 0;
    const request = (reopenRequests.get(conversationId) ?? 0) + 1;
    reopenRequests.set(conversationId, request);
    const bridge = (window as unknown as { omnigentDesktop?: BrowserTabsBridge }).omnigentDesktop;
    // The session's host lets the shell reopen its localhost pages, as the address bar does.
    const hostId = await fetchSessionHostId(conversationId);
    const result = await bridge
      ?.browserOpenOrNavigate?.(browserViewId(conversationId, tabId), url, undefined, {
        force: true,
        ...navigationHostOptions(hostId),
      })
      .catch((error: unknown) => ({ ok: false, error: String(error) }));
    if (result && !result.ok) return result;
    // The tab comes back either way. It's selected only if nothing was picked since and
    // no later reopen's tab is already shown; a later one that failed doesn't count.
    const latest =
      (browserSelections.get(conversationId) ?? 0) === selection &&
      request > (shownReopens.get(conversationId) ?? 0) &&
      mayShow();
    if (latest) shownReopens.set(conversationId, request);
    update((current) => ({
      tabs: [...current.tabs, tabId],
      selected: latest ? tabId : current.selected,
    }));
    return { ok: true };
  };

  /**
   * The page a tab shows. Before the strip has loaded it (just after returning to a
   * session), asks the tab's view, so closing the tab can still remember its page.
   */
  const pageUrl = async (tabId: string): Promise<string | undefined> => {
    const viewId = browserViewId(conversationId, tabId);
    // A snapshot without a URL (the view didn't exist yet when the strip looked) asks again.
    const known = pages[viewId]?.url;
    if (known) return known;
    const bridge = (window as unknown as { omnigentDesktop?: BrowserTabsBridge }).omnigentDesktop;
    const page = await bridge?.browserHasView?.(viewId).catch(() => undefined);
    if (!page?.exists) return undefined;
    return page.url && page.url !== "about:blank" ? page.url : page.pendingUrl;
  };

  /**
   * Close a tab's view and drop the tab. `beforeClose` runs first, after the close's intent
   * is captured: an agent navigation that arrives while it waits keeps the tab and its view
   * ("kept"). "failed" means the view didn't close.
   */
  const close = async (
    tabId: string,
    { beforeClose }: { beforeClose?: () => Promise<void> } = {},
  ): Promise<BrowserTabCloseResult> => {
    const navigationEpoch = agentNavigationEpoch(conversationId);
    // A fresh agent navigation supersedes an older close of the reserved agent view.
    const superseded = () =>
      tabId === AGENT_BROWSER_TAB_ID && agentNavigationEpoch(conversationId) !== navigationEpoch;
    if (beforeClose) await beforeClose();
    if (superseded()) return "kept";
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
    if (result && !result.ok) return "failed";
    // The view closed, but a navigation since recreated the tab: keep it selected.
    if (superseded()) return "closed";
    update((previous) => {
      const index = previous.tabs.indexOf(tabId);
      if (index === -1) return previous;
      const tabs = previous.tabs.filter((id) => id !== tabId);
      const selected =
        previous.selected === tabId ? (tabs[Math.max(0, index - 1)] ?? null) : previous.selected;
      return { tabs, selected };
    });
    return "closed";
  };

  return {
    ...state,
    labelFor,
    urlFor: (tabId: string) => pages[browserViewId(conversationId, tabId)]?.url,
    pageUrl,
    add,
    reopen,
    close,
    select,
    viewId: state.selected === null ? null : browserViewId(conversationId, state.selected),
    agentBrowser: state.selected === AGENT_BROWSER_TAB_ID,
  };
}
