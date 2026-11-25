// A minimal stand-in for the workspace tab store: tabs, selection, a leave
// guard and browser tabs, applying the history rules of the design so the
// timeline traces can be written as tables. The real store replaces it.

import { samePlace } from "../planOpen";
import {
  BrowserTabNavigation,
  type BrowserNavigationBridge,
  type BrowserNavigationMode,
  type LandResult,
} from "../browserTabNavigation";
import {
  backTarget,
  currentVisit,
  forwardTarget,
  goBack,
  goForward,
  historyStartingAt,
  recordStep,
  removeTab,
  replaceCurrent,
  replaceNewTab,
  type WorkspaceHistoryState,
} from "../workspaceHistory";
import type { BrowserEntry, TabId, Visit, WorkspacePlace } from "../workspacePlace";

export interface FakeTab {
  id: TabId;
  place: WorkspacePlace;
  entry?: BrowserEntry;
  dirty?: boolean;
  /** Shown after a background open into an unselected, clean tab. */
  dot?: boolean;
  /** The one Files tab, which navigates in place and which agents never move. */
  role?: "files";
  /** A background open waiting to be shown in a selected, dirty or Files tab. */
  attention?: WorkspacePlace;
}

export class FakeBrowserBridge implements BrowserNavigationBridge {
  readonly openOrNavigateCalls: { viewKey: string; url: string }[] = [];

  readonly mode: BrowserNavigationMode;

  constructor(mode: BrowserNavigationMode) {
    this.mode = mode;
  }

  openOrNavigate(viewKey: string, url: string, _isCurrent: () => boolean) {
    this.openOrNavigateCalls.push({ viewKey, url });
    return Promise.resolve();
  }
}

export class HistoryWorkspace {
  tabs: FakeTab[] = [];
  selectedTabId: TabId;
  history: WorkspaceHistoryState;
  readonly browsers = new Map<TabId, BrowserTabNavigation>();
  /** The leave guard's answer for the next guarded navigation. */
  guardAnswer: "leave" | "cancel" = "leave";
  readonly guardCalls: TabId[][] = [];
  /** How many owner transitions were published. */
  publishes = 0;

  readonly bridge: FakeBrowserBridge;

  constructor(first: FakeTab, bridge = new FakeBrowserBridge("replay")) {
    this.bridge = bridge;
    this.tabs.push(first);
    this.selectedTabId = first.id;
    this.history = historyStartingAt(this.visitOf(first));
  }

  tab(id: TabId): FakeTab {
    const tab = this.tabs.find((candidate) => candidate.id === id);
    if (!tab) throw new Error(`no tab ${id}`);
    return tab;
  }

  /** Make `id` a browser tab whose page is `url`. */
  attachBrowser(id: TabId, live?: { url: string }): BrowserTabNavigation {
    const owner = new BrowserTabNavigation(
      id,
      `view-${id}`,
      this.bridge,
      {
        isSelected: () => this.selectedTabId === id,
        history: () => this.history,
        apply: ({ place, history }) => {
          const tab = this.tab(id);
          tab.place = place.url === undefined ? { kind: "web" } : { kind: "web", url: place.url };
          delete tab.entry;
          if (history) this.history = history;
          this.publishes += 1;
          // What a subscriber or a save would see: the invariant holds at every publish.
          if (this.selectedTabId === id && !this.invariantHolds()) {
            throw new Error(`incoherent publish: ${describeHistory(this.history)}`);
          }
        },
      },
      live,
    );
    this.browsers.set(id, owner);
    return owner;
  }

  /** An open that selects its tab: a step. New tabs go after the selected one. */
  openSelected(tab: FakeTab): void {
    this.insertAfterSelected(tab);
    this.selectedTabId = tab.id;
    this.history = recordStep(this.history, this.visitOf(tab));
  }

  /** An agent's open: never selects and never a step. */
  openInBackground(tab: FakeTab): void {
    const existing = this.tabs.find((candidate) => candidate.id === tab.id);
    if (!existing) {
      this.insertAfterSelected({ ...tab, dot: true });
      return;
    }
    if (existing.id === this.selectedTabId || existing.dirty || existing.role === "files") {
      existing.attention = tab.place;
    } else Object.assign(existing, { place: tab.place, dot: true });
  }

  /** The user selects a tab: a step for whatever it shows. */
  select(id: TabId): void {
    if (id === this.selectedTabId) return;
    const previous = this.selectedTabId;
    this.selectedTabId = id;
    this.browsers.get(previous)?.deselected();
    const tab = this.tab(id);
    tab.dot = false;
    this.history = recordStep(this.history, this.visitOf(tab));
  }

  /** In-tab navigation (a PR sub-view, a file from the tree): a step through the guard. */
  async navigate(id: TabId, place: WorkspacePlace): Promise<"leave" | "cancel"> {
    const tab = this.tab(id);
    if (tab.dirty && (await this.askToLeave([id])) === "cancel") return "cancel";
    tab.place = place;
    tab.dirty = false;
    if (id === this.selectedTabId) this.history = recordStep(this.history, this.visitOf(tab));
    else this.select(id);
    return "leave";
  }

  /** A line or comment move within the same tab and path: replaces the cursor visit when that tab is selected. */
  moveWithin(id: TabId, place: WorkspacePlace): void {
    const tab = this.tab(id);
    tab.place = place;
    if (id === this.selectedTabId) this.history = replaceCurrent(this.history, this.visitOf(tab));
    else this.select(id);
  }

  /** The new-tab page became an app, in the same tab or a bound tab in its slot. */
  pickApp(newTabId: TabId, app: FakeTab): void {
    const index = this.tabs.findIndex((tab) => tab.id === newTabId);
    this.tabs.splice(index, 1, app);
    if (this.selectedTabId === newTabId) this.selectedTabId = app.id;
    this.history = replaceNewTab(this.history, newTabId, this.visitOf(app));
  }

  /** Back or Forward: guard, then move the cursor, select and land. Resolves once landing has started. */
  async back(): Promise<{ moved: boolean; landing?: Promise<LandResult> }> {
    return this.step(-1);
  }

  async forward(): Promise<{ moved: boolean; landing?: Promise<LandResult> }> {
    return this.step(1);
  }

  canGoBack(): boolean {
    return backTarget(this.history, { isBlocked: this.isBlocked }) !== null;
  }

  canGoForward(): boolean {
    return forwardTarget(this.history, { isBlocked: this.isBlocked }) !== null;
  }

  async close(id: TabId): Promise<"leave" | "cancel"> {
    if (this.tab(id).dirty && (await this.askToLeave([id])) === "cancel") return "cancel";
    const wasSelected = id === this.selectedTabId;
    this.browsers.get(id)?.dispose();
    this.browsers.delete(id);
    this.tabs = this.tabs.filter((tab) => tab.id !== id);
    const { state, restore } = removeTab(this.history, id);
    this.history = state;
    if (!wasSelected) return "leave";
    if (restore) {
      this.selectedTabId = restore.tabId;
      this.show(restore);
      return "leave";
    }
    const fresh: FakeTab = { id: `new-${this.tabs.length}`, place: { kind: "new" } };
    this.tabs.push(fresh);
    this.selectedTabId = fresh.id;
    this.history = historyStartingAt(this.visitOf(fresh));
    return "leave";
  }

  /** The design's invariant: the cursor visit belongs to the selected tab and equals its place. */
  invariantHolds(): boolean {
    const visit = currentVisit(this.history);
    const tab = this.tab(this.selectedTabId);
    return (
      visit !== null &&
      visit.tabId === tab.id &&
      samePlace(visit.place, tab.place) &&
      visit.entry?.index === tab.entry?.index
    );
  }

  private readonly isBlocked = (visit: Visit) =>
    this.browsers.get(visit.tabId)?.isWaitingForReplay() ?? false;

  private async step(
    direction: -1 | 1,
  ): Promise<{ moved: boolean; landing?: Promise<LandResult> }> {
    const options = { isBlocked: this.isBlocked };
    const target =
      direction < 0 ? backTarget(this.history, options) : forwardTarget(this.history, options);
    if (!target) return { moved: false };
    const tab = this.tab(target.tabId);
    const changesPlace = !samePlace(tab.place, target.place);
    if (changesPlace && tab.dirty && (await this.askToLeave([tab.id])) === "cancel")
      return { moved: false };
    this.history = direction < 0 ? goBack(this.history, options) : goForward(this.history, options);
    const previous = this.selectedTabId;
    this.selectedTabId = tab.id;
    if (previous !== tab.id) this.browsers.get(previous)?.deselected();
    return { moved: true, landing: this.show(target) };
  }

  private show(visit: Visit): Promise<LandResult> | undefined {
    const owner = this.browsers.get(visit.tabId);
    if (owner) return owner.land(visit);
    const tab = this.tab(visit.tabId);
    tab.place = visit.place;
    tab.dirty = false;
    return undefined;
  }

  private async askToLeave(tabIds: TabId[]): Promise<"leave" | "cancel"> {
    this.guardCalls.push(tabIds);
    return this.guardAnswer;
  }

  private insertAfterSelected(tab: FakeTab): void {
    const index = this.tabs.findIndex((candidate) => candidate.id === this.selectedTabId);
    this.tabs.splice(index + 1, 0, tab);
  }

  private visitOf(tab: FakeTab): Visit {
    return tab.entry
      ? { tabId: tab.id, place: tab.place, entry: tab.entry }
      : { tabId: tab.id, place: tab.place };
  }
}

/** The design's trace notation: `[F:a.ts, P:#12*]`. */
export function describeHistory(history: WorkspaceHistoryState): string {
  const parts = history.visits.map(
    (visit, index) => describeVisit(visit) + (index === history.cursor ? "*" : ""),
  );
  return `[${parts.join(", ")}]`;
}

function describeVisit(visit: Visit): string {
  const detail = describePlace(visit.place, visit.entry);
  return detail ? `${visit.tabId}:${detail}` : visit.tabId;
}

function describePlace(place: WorkspacePlace, entry: BrowserEntry | undefined): string {
  switch (place.kind) {
    case "files":
      return place.path ? place.path + (place.line ? `:${place.line.start}` : "") : "";
    case "changes":
      return place.path ?? "";
    case "pull": {
      const number = `#${place.url.split("/").at(-1)}`;
      const view = place.view === "summary" ? "" : `/${place.view}`;
      return number + view + (place.path ? `:${place.path}` : "");
    }
    case "web":
      return (place.url ? new URL(place.url).pathname : "") + (entry ? `#${entry.index}` : "");
    default:
      return "";
  }
}
