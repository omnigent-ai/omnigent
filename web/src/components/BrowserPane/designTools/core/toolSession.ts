// One scope per opening of a design tool, owning its async work: it starts when the store
// opens a tool and ends when the store closes or replaces it. Ending it removes its
// listeners and refuses its late results; its page operations run in one ordered queue.
import { designPlatform, type DesignPage, type PageOp } from "./designPlatform";
import {
  begin as beginPhase,
  closeTool,
  editDrafts,
  settle as settlePhase,
  tabTools,
  useDesignTools,
  type Drafts,
  type Phase,
  type TabTools,
  type Ticket,
} from "./designToolsStore";

/** Work that was still running when its session ended. */
export class SessionEnded extends Error {
  constructor() {
    super("The tool closed.");
    this.name = "SessionEnded";
  }
}

export const isSessionEnded = (error: unknown) => error instanceof SessionEnded;

export class ToolSession {
  readonly tabId: string;
  readonly id: number;
  private ended = false;
  private tail: Promise<unknown> = Promise.resolve();
  private pageReady: Promise<DesignPage | string> | null = null;
  private readonly cleanup = new Set<() => void>();

  /** `after`: the tab's previous session's page work, which this one's follows. */
  constructor(tabId: string, id: number, after: Promise<unknown> = Promise.resolve()) {
    this.tabId = tabId;
    this.id = id;
    this.tail = after.catch(() => undefined);
  }

  get isEnded(): boolean {
    return this.ended;
  }

  /** Once everything this session asked of the page is done. */
  get drained(): Promise<unknown> {
    return this.tail;
  }

  private get ticket(): Ticket {
    return { tabId: this.tabId, session: this.id };
  }

  /** This session's view of the tab's tool state. */
  state(): TabTools {
    return tabTools(this.tabId);
  }

  // ── The page ─────────────────────────────────────────────

  /** The page, once the tools can work in it; rejects with why not (from the platform). */
  async page(): Promise<DesignPage> {
    this.pageReady ??= Promise.resolve().then(() => designPlatform(this.tabId).page(this.tabId));
    const page = await this.pageReady;
    if (this.ended) throw new SessionEnded();
    if (typeof page === "string") throw new Error(page);
    return page;
  }

  /**
   * Do `op` in the page after everything asked for before it, called when its turn comes.
   * It never starts once the session has ended, and its result is dropped if it ends meanwhile.
   */
  run(op: PageOp): Promise<string> {
    if (this.ended) return Promise.reject(new SessionEnded());
    const next = this.tail.then(async () => {
      if (this.ended) throw new SessionEnded();
      const page = await this.page();
      const answer = await op(page);
      if (this.ended) throw new SessionEnded();
      return answer;
    });
    this.tail = next.catch(() => undefined);
    return next;
  }

  // ── The drafts ───────────────────────────────────────────

  /** Change the drafts, if this session is current and editing. */
  edit(change: Partial<Drafts> | ((current: TabTools) => Partial<Drafts>)): boolean {
    return !this.ended && editDrafts(this.tabId, change, this.ticket);
  }

  /** Freeze the drafts for `phase`; false unless this session is current and editing. */
  begin(phase: Exclude<Phase, "editing">): boolean {
    if (this.ended || tabTools(this.tabId).session !== this.id) return false;
    return beginPhase(this.tabId, phase) !== null;
  }

  /** Back to editing after `begin`; false once the session has ended. */
  settle(change?: Partial<Drafts>): boolean {
    return !this.ended && settlePhase(this.ticket, change);
  }

  /** Close the tool, if this session is still the open one. */
  close(): void {
    if (!this.ended && tabTools(this.tabId).session === this.id) closeTool(this.tabId);
  }

  // ── Listeners ────────────────────────────────────────────

  /** Listen until the session ends (or `stop` is called). */
  listen<K extends keyof WindowEventMap>(
    target: Window,
    type: K,
    handler: (event: WindowEventMap[K]) => void,
    options?: AddEventListenerOptions,
  ): () => void {
    if (this.ended) return () => {};
    const stop = () => {
      target.removeEventListener(type, handler, options);
      this.cleanup.delete(stop);
    };
    target.addEventListener(type, handler, options);
    this.cleanup.add(stop);
    return stop;
  }

  /** End the session: stop its listeners and drop its late results. */
  end(): void {
    if (this.ended) return;
    this.ended = true;
    for (const stop of [...this.cleanup]) stop();
    this.cleanup.clear();
  }
}

// Every tab's live session, kept in step with the store. A tab's sessions do their page
// work in turn: a new session's starts once the last one's is done, or after a while.
const sessions = new Map<string, ToolSession>();
const lastWork = new Map<string, Promise<unknown>>();
// The most a new session waits for the last one's: a page that never answers can't keep
// the tab's tools from working.
const LAST_WORK_WAIT_MS = 2000;

/** `work`, or a promise that resolves after LAST_WORK_WAIT_MS if it hasn't by then. */
function settledOrLate(work: Promise<unknown>): Promise<unknown> {
  const late = new Promise((resolve) => {
    setTimeout(resolve, LAST_WORK_WAIT_MS);
  });
  return Promise.race([work, late]);
}

function sync(tabs: Record<string, TabTools>): void {
  for (const [tabId, tools] of Object.entries(tabs)) {
    const live = sessions.get(tabId);
    if (live && (live.id !== tools.session || tools.tool === null)) {
      live.end();
      sessions.delete(tabId);
      const drained = settledOrLate(live.drained);
      lastWork.set(tabId, drained);
      void drained.then(() => {
        if (lastWork.get(tabId) === drained) lastWork.delete(tabId);
      });
    }
    if (tools.tool !== null && !sessions.has(tabId)) {
      sessions.set(tabId, new ToolSession(tabId, tools.session, lastWork.get(tabId)));
    }
  }
  for (const [tabId, live] of sessions) {
    if (!(tabId in tabs)) {
      live.end();
      sessions.delete(tabId);
    }
  }
  for (const tabId of lastWork.keys()) {
    if (!(tabId in tabs)) lastWork.delete(tabId);
  }
}

useDesignTools.subscribe((state) => sync(state.tabs));

/** The open tool's session for `tabId`, or null when no tool is open. */
export function toolSession(tabId: string): ToolSession | null {
  return sessions.get(tabId) ?? null;
}
