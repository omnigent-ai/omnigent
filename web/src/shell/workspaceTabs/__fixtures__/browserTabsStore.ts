// The tab store with real browser navigation owners and a scripted shell: what
// the shell probes and reports, and a check that every published state keeps the
// cursor on the selected tab.

import { onTestFinished } from "vitest";

import { noteBrowserPageControl } from "@/lib/browserPageControls";
import { browserViewId } from "@/lib/browserTabs";

import { FakeBrowserBridge } from "./historyWorkspace";
import type { BrowserNavigationMode, BrowserViewProbe } from "../browserTabNavigation";
import {
  attachBrowserTabNavigation,
  type BrowserNavigationShell,
} from "../browserTabNavigationRegistry";
import { currentVisit, visitsEqual, type WorkspaceHistoryState } from "../workspaceHistory";
import type { Visit } from "../workspacePlace";
import type { WorkspaceTab } from "../workspaceTab";
import type { WorkspaceTabsSessionState } from "../workspaceTabsPersistence";
import { createWorkspaceTabsStore, visitOf, type WorkspaceTabsStore } from "../workspaceTabsStore";

export const SESSION = "s1";
export const A = "https://x.test/a";
export const B = "https://x.test/b";
export const C = "https://x.test/c";
export const W_VIEW = browserViewId(SESSION, "w");

export class FakeShell extends FakeBrowserBridge implements BrowserNavigationShell {
  /** What each view's probe answers; null is a shell that can't say. Unlisted views are gone. */
  readonly views = new Map<string, BrowserViewProbe | null>();
  readonly probes: { viewKey: string; resolve: (view: BrowserViewProbe | null) => void }[] = [];
  /** Answer probes at once from `views`; false holds them for the test to answer. */
  autoProbe = true;
  private readonly urlChanged = new Set<(event: { viewKey: string; url: string }) => void>();

  probe(viewKey: string) {
    if (this.autoProbe) {
      return Promise.resolve(
        this.views.has(viewKey) ? this.views.get(viewKey)! : ({ exists: false } as const),
      );
    }
    return new Promise<BrowserViewProbe | null>((resolve) => {
      this.probes.push({ viewKey, resolve });
    });
  }

  onUrlChanged(listener: (event: { viewKey: string; url: string }) => void) {
    this.urlChanged.add(listener);
    return () => this.urlChanged.delete(listener);
  }

  changeUrl(viewKey: string, url: string) {
    for (const listener of [...this.urlChanged]) listener({ viewKey, url });
  }

  /** The user used the view's own controls, through the real page-control bus. */
  usePageControl(viewKey: string) {
    noteBrowserPageControl(viewKey);
  }

  listenerCount() {
    return this.urlChanged.size;
  }
}

export const files: WorkspaceTab = {
  id: "F",
  place: { kind: "files" },
  role: "files",
  openedBy: "user",
};

/** A browser tab showing `url`; `entryIndex` is a page entry a saved session may still carry. */
export function browserTab(url: string | undefined, entryIndex?: number): WorkspaceTab {
  return {
    id: "W",
    place: url === undefined ? { kind: "web" } : { kind: "web", url },
    binding: { kind: "browser-view", viewKey: "w" },
    ...(entryIndex === undefined ? {} : { entry: { index: entryIndex } }),
    openedBy: "user",
  };
}

/** The selected tab's place and entry match the cursor visit. */
export function coherent(
  state: { tabs: readonly WorkspaceTab[]; selectedTabId: string | null },
  history: WorkspaceHistoryState,
) {
  const selected = state.tabs.find((tab) => tab.id === state.selectedTabId);
  const cursor = currentVisit(history);
  return selected !== undefined && cursor !== null && visitsEqual(visitOf(selected), cursor);
}

export function setup({
  tabs = [browserTab(A), files],
  selected = "W",
  timeline,
  mode = "replay",
  views = {},
}: {
  tabs?: WorkspaceTab[];
  selected?: string;
  timeline?: Visit[];
  mode?: BrowserNavigationMode;
  views?: Record<string, BrowserViewProbe | null>;
} = {}) {
  const selectedTab = tabs.find((tab) => tab.id === selected)!;
  const visits = timeline ?? [visitOf(selectedTab)];
  const initial: WorkspaceTabsSessionState = {
    tabs,
    selectedTabId: selected,
    timeline: visits,
    cursor: visits.length - 1,
    closed: [],
  };
  const incoherent: string[] = [];
  let createdViews = 0;
  let store!: WorkspaceTabsStore;
  store = createWorkspaceTabsStore({
    scopeId: SESSION,
    initial,
    save: (state) => {
      if (!coherent(state as never, { visits: state.timeline, cursor: state.cursor })) {
        incoherent.push(`save: ${JSON.stringify(state.timeline)} @${state.cursor}`);
      }
      return "saved";
    },
    requestLeave: () => Promise.resolve("leave"),
    newTabId: (() => {
      let next = 0;
      return () => `n${++next}`;
    })(),
    newViewKey: () => `v${++createdViews}`,
  });
  store.subscribe(() => {
    if (!coherent(store.snapshot() as never, store.history.state)) {
      incoherent.push(`notify: ${JSON.stringify(store.history.state)}`);
    }
  });
  const shell = new FakeShell(mode);
  for (const [viewKey, view] of Object.entries(views)) shell.views.set(viewKey, view);
  const attached = attachBrowserTabNavigation(store, SESSION, shell);
  let detached = false;
  const detach = () => {
    if (detached) return;
    detached = true;
    attached();
  };
  // Page-control subscriptions are module-wide, so a test that doesn't detach would leak its owners.
  onTestFinished(detach);
  return { store, shell, detach, incoherent };
}

export const tick = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

export function placeOf(store: WorkspaceTabsStore, tabId = "W") {
  return store.findTab(tabId)!.place;
}
