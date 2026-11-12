// Browser tab operations for `browser_tabs` and the relay,
// as atomic changes to the session's record in main (tabStore.ts). They
// work for background sessions too, whose tab hooks aren't mounted.
import { agentBrowserTabId, browserViewId } from "@/hooks/useBrowserTabs";
import { randomUUID } from "@/lib/randomUUID";
import { BrowserTabsExpired, changeTabs, RESERVED_TAB_ID, type TabsRecord } from "./tabStore";

export { agentBrowserTabId };

/** Tabs a session can have open: the saved workspace keeps only the last 20
 *  (MAX_OPEN_FILES in lib/sessionWorkspaceState.ts). */
export const BROWSER_TAB_LIMIT = 20;

/** The tab agents act on in `record`. */
export function agentTabIn(record: Pick<TabsRecord, "tabs" | "agentTab">): string {
  return record.agentTab !== null && record.tabs.includes(record.agentTab)
    ? record.agentTab
    : RESERVED_TAB_ID;
}

const agentTabValue = (tabId: string): string | null => (tabId === RESERVED_TAB_ID ? null : tabId);

/** What a new tab replaced, so a refused first page can give it back. */
export interface NewTab {
  tabId: string;
  before: { selected: string | null };
}

/**
 * Open a new selected tab, unless the session has BROWSER_TAB_LIMIT already
 * (null). Agents stay on their tab until `adoptAgentBrowserTab`, so actions
 * running meanwhile don't move to a page that isn't there yet.
 */
export async function addBrowserTab(
  conversationId: string,
  deadlineAt?: number,
): Promise<NewTab | null> {
  const tabId = randomUUID();
  let before: NewTab["before"] | undefined;
  const { applied } = await changeTabs(
    conversationId,
    (current) => {
      // The saved tab list keeps the last BROWSER_TAB_LIMIT; past that,
      // older tabs would drop out of it while their pages keep running.
      if (current.tabs.length >= BROWSER_TAB_LIMIT) return null;
      before = { selected: current.selected };
      return { ...current, tabs: [...current.tabs, tabId], selected: tabId };
    },
    deadlineAt,
  );
  return applied && before ? { tabId, before } : null;
}

/**
 * Undo `addBrowserTab` for a tab whose first page was refused: drop it, and
 * give back the selection from before it opened, unless something since
 * (another tab opened or selected meanwhile) replaced it.
 * Runs whatever the time: it cleans up.
 */
export async function discardNewBrowserTab(conversationId: string, added: NewTab): Promise<void> {
  const { tabId, before } = added;
  await changeTabs(conversationId, (current) => {
    if (!current.tabs.includes(tabId)) return null;
    const tabs = current.tabs.filter((id) => id !== tabId);
    // Agents moved onto it meanwhile move off as off any closed tab (tabStore).
    return {
      ...current,
      tabs,
      selected:
        current.selected === tabId
          ? before.selected !== null && tabs.includes(before.selected)
            ? before.selected
            : (tabs.at(-1) ?? null)
          : current.selected,
    };
  });
}

/** Make an open tab the one agents act on; `show` also selects it.
 *  Returns false if it isn't open. */
export async function chooseAgentBrowserTab(
  conversationId: string,
  tabId: string,
  { show, deadlineAt }: { show: boolean; deadlineAt?: number },
): Promise<boolean> {
  const { applied } = await changeTabs(
    conversationId,
    (current) =>
      current.tabs.includes(tabId)
        ? {
            ...current,
            selected: show ? tabId : current.selected,
            agentTab: agentTabValue(tabId),
          }
        : null,
    deadlineAt,
  );
  return applied;
}

interface CloseResult {
  ok: boolean;
  removed?: boolean;
  expired?: boolean;
  superseded?: boolean;
}

interface CloseBridge {
  browserClose?: (
    viewId: string,
    reason?: string,
    opts?: { deadlineAt?: number; expectedNavs?: { tabId: string; count: number } },
  ) => Promise<CloseResult>;
}

/**
 * Close a tab's page (main closes it in whichever window has it) and drop
 * the tab: "closed", or "failed" when the page would not close. A tab
 * navigated after the close began stays, with its new page ("navigated"):
 * that navigation is the newer intent, so main keeps the page too.
 * Past `deadlineAt` nothing closes (BrowserTabsExpired); once its page has
 * closed, the tab is dropped whatever the time, so no tab is left without one.
 */
export async function closeBrowserTab(
  conversationId: string,
  record: TabsRecord,
  tabId: string,
  deadlineAt?: number,
): Promise<"closed" | "navigated" | "failed"> {
  const navigations = record.navs[tabId] ?? 0;
  if (deadlineAt !== undefined && Date.now() >= deadlineAt) throw new BrowserTabsExpired();
  const desktop = (window as { omnigentDesktop?: CloseBridge }).omnigentDesktop;
  const result = await desktop
    ?.browserClose?.(browserViewId(conversationId, tabId), undefined, {
      deadlineAt,
      expectedNavs: { tabId, count: navigations },
    })
    .catch((): CloseResult => ({ ok: false }));
  if (result?.expired) throw new BrowserTabsExpired();
  if (result?.superseded) return "navigated";
  if (result && !result.ok) return "failed";
  const pageClosed = result?.removed === true;
  await changeTabs(
    conversationId,
    (current) => {
      if ((current.navs[tabId] ?? 0) !== navigations) return null;
      const index = current.tabs.indexOf(tabId);
      if (index === -1) return null;
      const tabs = current.tabs.filter((id) => id !== tabId);
      const selected =
        current.selected === tabId ? (tabs[Math.max(0, index - 1)] ?? null) : current.selected;
      // An agent that closes the tab it acts on returns to its reserved tab,
      // rather than adopting whichever tab the rail shows next. Naming the
      // reserved tab keeps the store from adopting one when it was the one closed.
      const acting = current.agentTab !== null && current.tabs.includes(current.agentTab);
      const agentTab =
        current.agentTab === tabId
          ? null
          : !acting && tabId === RESERVED_TAB_ID
            ? RESERVED_TAB_ID
            : current.agentTab;
      return { ...current, tabs, selected, agentTab };
    },
    pageClosed ? undefined : deadlineAt,
  );
  return "closed";
}
