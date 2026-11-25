// One browser tab's navigation owner: it records the page's URL changes in the
// workspace timeline and lands Back/Forward on them by loading the recorded URL, as
// the address bar does. The latest landing owns the tab; a newer landing or the user
// taking over ends it, and the page it last showed decides where the tab is.

import type { TabId, Visit, WorkspacePlace } from "./workspacePlace";

type WebPlace = Extract<WorkspacePlace, { kind: "web" }>;
import {
  moveCursorTo,
  recordStep,
  replaceCurrent,
  withoutEntries,
  type WorkspaceHistoryState,
} from "./workspaceHistory";

/** What the shell can tell us: `replay` has URL change events, `untracked` has none. */
export type BrowserNavigationMode = "replay" | "untracked";

export interface BrowserNavigationBridge {
  mode: BrowserNavigationMode;
  /**
   * Load `url` in the view, creating it if needed. Resolves once the load is issued.
   * `isCurrent` says whether this load is still wanted; the bridge checks it right
   * before issuing the native load, after any lookup it awaits, and skips it if not.
   */
  openOrNavigate: (viewKey: string, url: string, isCurrent: () => boolean) => Promise<void>;
}

/** One owner transition: the tab's page and, when it changes, the timeline. */
export interface BrowserTabChange {
  /** The page the tab shows or is landing on; no URL is a blank page. */
  place: { url: string | undefined };
  history?: WorkspaceHistoryState;
}

/**
 * The tab store's side: selection and the timeline. The owner sets the tab's
 * place: the visit a landing asked for as soon as it starts, then whatever the
 * page commits. The store moves the cursor and calls `land`, nothing more.
 */
export interface BrowserTabHost {
  isSelected: () => boolean;
  history: () => WorkspaceHistoryState;
  /** Apply one transition at once, so subscribers and saves never see the place and timeline apart. */
  apply: (change: BrowserTabChange) => void;
  /** A replay started or ended waiting, which disables or enables stepping into the tab. */
  waitingChanged?: () => void;
  /** The workspace is moving the page (Back, Forward, a link), so a typed load not yet issued is dropped. */
  landingStarted?: () => void;
}

/** What the shell says about a tab's view when the app (re)loads: missing, or live with its page. */
export type BrowserViewProbe =
  | { exists: false }
  | {
      exists: true;
      /** Empty while a new view's first load hasn't committed. */
      url: string;
    };

export type LandResult = "landed" | "superseded" | "failed" | "stale";

/** How long a replay waits for its URL event before giving up. */
export const REPLAY_WAIT_MS = 15_000;

const BLANK_PAGE_URL = "about:blank";

/**
 * The place URL for a URL the page reports: a blank page, or a new view's empty
 * URL before its first load commits, is the canonical `{ kind: "web" }`.
 */
function placeUrl(nativeUrl: string): string | undefined {
  return nativeUrl === BLANK_PAGE_URL || nativeUrl === "" ? undefined : nativeUrl;
}

interface LivePage {
  url: string;
}

interface PendingReplay {
  requestId: string;
  resolve: (result: LandResult) => void;
  timer: ReturnType<typeof setTimeout>;
  target: string;
  /** URL events to pass over while waiting: the page being left, and older replays' targets. */
  ignored: ReadonlySet<string>;
  /** The latest of those the page reported, which it may show if the replay never lands. */
  lastIgnored?: string;
}

let requestCounter = 0;
function nextRequestId(): string {
  requestCounter += 1;
  return `nav-${Date.now().toString(36)}-${requestCounter}`;
}

export class BrowserTabNavigation {
  readonly tabId: TabId;
  readonly viewKey: string;
  private readonly bridge: BrowserNavigationBridge;
  private readonly host: BrowserTabHost;
  /** What the page last showed, which a landing doesn't change until its URL event. */
  private live: LivePage | null;
  /** The latest replay; only it may land. */
  private pending: PendingReplay | null = null;
  /** Untracked mode's load awaiting its acknowledgement; nothing else says the page will move. */
  private untrackedLoad: string | null = null;
  private disposed = false;
  /** Moves on every event and action, so a view probe taken before one of them is stale. */
  private generation = 0;
  /** Moves on every landing the workspace starts, so an older typed load is stale. */
  private landings = 0;
  /** The typed address the user last submitted, and the landings there were by then. */
  private submission: { id: number; landings: number } | null = null;

  constructor(
    tabId: TabId,
    viewKey: string,
    bridge: BrowserNavigationBridge,
    host: BrowserTabHost,
    live?: LivePage,
  ) {
    this.tabId = tabId;
    this.viewKey = viewKey;
    this.bridge = bridge;
    this.host = host;
    this.live = live ?? null;
  }

  /**
   * The page the view last committed, which a landing in flight hasn't changed yet;
   * null before anything committed. Reopening a closed tab loads this one.
   */
  committedPlace(): WebPlace | null {
    if (!this.live) return null;
    const url = placeUrl(this.live.url);
    return url === undefined ? { kind: "web" } : { kind: "web", url };
  }

  /** True while a replay waits for its URL event: Back/Forward into this tab are disabled. */
  isWaitingForReplay(): boolean {
    return this.pending !== null;
  }

  /**
   * Make the page show `visit`; the cursor is already on it. The tab's place
   * becomes the visit at once, so the timeline and the tab agree while it loads.
   */
  land(visit: Visit): Promise<LandResult> {
    if (this.disposed) return Promise.resolve("stale");
    this.generation += 1;
    this.landings += 1;
    this.submission = null;
    this.host.landingStarted?.();
    const url = visit.place.kind === "web" ? visit.place.url : undefined;
    this.host.apply({ place: { url } });
    // With a load still in flight the page is about to move, so even its current page needs a request.
    if (!this.loadInFlight() && this.showsLive(url)) return Promise.resolve("landed");
    const ignored = this.ignoredWhileReplaying();
    this.supersedePending();
    const target = url ?? BLANK_PAGE_URL;
    if (this.bridge.mode === "untracked") return this.landUntracked(target);
    return this.replay(target, ignored);
  }

  /** Replay mode: today's URL change event for this view. */
  onUrlChanged(event: { viewKey: string; url: string }): void {
    if (this.disposed || event.viewKey !== this.viewKey || this.bridge.mode !== "replay") return;
    this.generation += 1;
    const pending = this.pending;
    if (pending) {
      // A title or load event of the page being left, or an older replay's page, isn't the landing.
      if (event.url !== pending.target && pending.ignored.has(event.url)) {
        pending.lastIgnored = event.url;
        return;
      }
      // The replay's evidence: the target, or where it redirected.
      this.live = { url: event.url };
      this.adopt(event.url);
      this.finish(pending, "landed");
      return;
    }
    if (this.live?.url === event.url) return;
    this.live = { url: event.url };
    this.ordinaryNavigation(event.url);
  }

  /**
   * The user typed in the URL bar, used the page's own ‹ › or reloaded: a waiting
   * landing ends, and a load not yet issued (one waiting for the session's host) is
   * revoked, so it can't overwrite where the user went. The tab and cursor go back to
   * the page the view last committed, which is the one the user acted on.
   */
  userActed(submission?: number): void {
    this.generation += 1;
    const abandoned = this.loadInFlight();
    if (this.pending) this.finish(this.pending, "superseded");
    this.untrackedLoad = null;
    if (abandoned) this.reconcileWithLivePage();
    // A control after a typed load leaves it: the shell may have issued it already.
    if (submission !== undefined) this.submission = { id: submission, landings: this.landings };
  }

  /**
   * The shell accepted the load of a typed address. Shells that report navigations
   * will say where it went; one that reports nothing has only this address, so the
   * tab goes by it, as for its own landings. It isn't a URL event.
   * Only the latest submission counts, and only if the workspace hasn't moved the page
   * since: a later landing makes it stale, and then it changes nothing. A reload or an
   * address edit doesn't, as the shell had issued the load and the page shows it.
   */
  submissionAccepted(submission: number, url: string): void {
    const current = this.submission;
    if (this.disposed || current?.id !== submission || current.landings !== this.landings) {
      return;
    }
    this.submission = null;
    if (this.bridge.mode !== "untracked") return;
    this.generation += 1;
    this.live = { url };
    this.ordinaryNavigation(url);
  }

  /** Taken before probing the view; `restore` ignores the probe if anything happened since. */
  probeToken(): number {
    return this.generation;
  }

  /**
   * After a reload: a live view's page wins over what was saved, and the tab's
   * saved entries no longer name anything. A missing view loads the saved URL.
   */
  restore(view: BrowserViewProbe, token: number, savedUrl: string | undefined): void {
    if (this.disposed || token !== this.generation) return;
    this.generation += 1;
    // A new view still loading its first page has committed nothing: its load stays, and
    // its first commit is what the tab shows next.
    if (view.exists && view.url === "") return;
    if (view.exists) {
      this.live = { url: view.url };
      this.reconcileWithLivePage(withoutEntries(this.host.history(), this.tabId));
      return;
    }
    this.host.apply({
      place: { url: savedUrl },
      history: withoutEntries(this.host.history(), this.tabId),
    });
    if (savedUrl !== undefined) {
      void this.land({ tabId: this.tabId, place: { kind: "web", url: savedUrl } });
    }
  }

  /**
   * Another tab is being selected: a waiting replay ends, and the tab shows the page the
   * view last committed until another event moves it. The timeline is left to the new
   * selection. An untracked load not yet issued, and a typed address whose load is still
   * to be accepted, are kept: only focus moved, and no other navigation competes with them.
   */
  deselected(): void {
    if (!this.pending) return;
    this.finish(this.pending, "superseded");
    if (this.live) this.host.apply({ place: { url: placeUrl(this.live.url) } });
  }

  /** The tab closed, the session or design switched: late events for it are ignored from now on. */
  dispose(): void {
    this.generation += 1;
    if (this.pending) this.finish(this.pending, "stale");
    this.untrackedLoad = null;
    this.disposed = true;
  }

  private loadInFlight(): boolean {
    return this.pending !== null || this.untrackedLoad !== null;
  }

  private showsLive(url: string | undefined): boolean {
    // A view that has shown nothing yet is blank.
    if (!this.live) return url === undefined;
    return this.live.url === (url ?? BLANK_PAGE_URL);
  }

  private ignoredWhileReplaying(): Set<string> {
    const ignored = new Set<string>(this.live ? [this.live.url] : []);
    if (this.pending) {
      ignored.add(this.pending.target);
      for (const url of this.pending.ignored) ignored.add(url);
    }
    return ignored;
  }

  private async landUntracked(url: string): Promise<LandResult> {
    const requestId = nextRequestId();
    this.untrackedLoad = requestId;
    let issued = true;
    try {
      await this.bridge.openOrNavigate(
        this.viewKey,
        url,
        () => !this.disposed && this.untrackedLoad === requestId,
      );
    } catch {
      issued = false;
    }
    if (this.disposed) return "stale";
    if (this.untrackedLoad !== requestId) return "superseded";
    this.untrackedLoad = null;
    if (!issued) {
      this.reconcileWithLivePage();
      return "failed";
    }
    // Nothing better is known than the URL this client asked for.
    this.live = { url };
    return "landed";
  }

  private replay(url: string, ignored: ReadonlySet<string>): Promise<LandResult> {
    const requestId = nextRequestId();
    const landed = new Promise<LandResult>((resolve) => {
      const timer = setTimeout(() => {
        const pending = this.pending;
        if (pending?.requestId !== requestId) return;
        // No failure is claimed. A page that reported nothing may still be loading, so the tab
        // keeps the requested place; one that reported a page it shows, such as a redirect back
        // to the page being left, settles there.
        this.finish(pending, "superseded");
        if (pending.lastIgnored === undefined) return;
        this.live = { url: pending.lastIgnored };
        this.reconcileWithLivePage();
      }, REPLAY_WAIT_MS);
      this.pending = { requestId, resolve, timer, target: url, ignored };
    });
    this.host.waitingChanged?.();
    // The promise only acknowledges that the load was issued; the next URL event is the evidence.
    const isCurrent = () => !this.disposed && this.pending?.requestId === requestId;
    this.bridge.openOrNavigate(this.viewKey, url, isCurrent).catch(() => {
      if (this.pending?.requestId !== requestId) return;
      this.finish(this.pending, "failed");
      this.reconcileWithLivePage();
    });
    return landed;
  }

  // The latest replay's page: the cursor is already on its visit, so refresh it.
  private adopt(url: string): void {
    const history = this.host.history();
    const onThisTab = history.visits[history.cursor]?.tabId === this.tabId;
    this.host.apply({
      place: { url: placeUrl(url) },
      ...(onThisTab ? { history: replaceCurrent(history, this.webVisit(url)) } : {}),
    });
  }

  // The page moved on its own (a link, a script, a typed load): a new step when the tab is selected.
  private ordinaryNavigation(url: string): void {
    const place = { url: placeUrl(url) };
    if (!this.host.isSelected()) {
      this.host.apply({ place });
      return;
    }
    this.host.apply({ place, history: recordStep(this.host.history(), this.webVisit(url)) });
  }

  private visitShows(visit: Visit, url: string): boolean {
    return (
      visit.tabId === this.tabId && visit.place.kind === "web" && visit.place.url === placeUrl(url)
    );
  }

  // A landing ended without landing: show the page that is really there, and put the cursor on it.
  private reconcileWithLivePage(history = this.host.history()): void {
    const live = this.live;
    if (!live) return;
    const place = { url: placeUrl(live.url) };
    const changed = history !== this.host.history();
    const current = history.visits[history.cursor];
    if (!this.host.isSelected() || (current && this.visitShows(current, live.url))) {
      this.host.apply(changed ? { place, history } : { place });
      return;
    }
    const match = history.visits.findIndex((visit) => this.visitShows(visit, live.url));
    this.host.apply({
      place,
      history:
        match >= 0 ? moveCursorTo(history, match) : recordStep(history, this.webVisit(live.url)),
    });
  }

  // Visits use canonical places, so a blank page is `{ kind: "web" }`, never `about:blank`.
  private webVisit(nativeUrl: string): Visit {
    const url = placeUrl(nativeUrl);
    const place: WebPlace = url === undefined ? { kind: "web" } : { kind: "web", url };
    return { tabId: this.tabId, place };
  }

  private supersedePending(): void {
    const pending = this.pending;
    if (!pending) return;
    this.pending = null;
    clearTimeout(pending.timer);
    pending.resolve("superseded");
    this.host.waitingChanged?.();
  }

  // The latest landing ended: landed, failed, gave up or closed.
  private finish(pending: PendingReplay, result: LandResult): void {
    if (this.pending === pending) this.pending = null;
    clearTimeout(pending.timer);
    pending.resolve(result);
    this.host.waitingChanged?.();
  }
}
