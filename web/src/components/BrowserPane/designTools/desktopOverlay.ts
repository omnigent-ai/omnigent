// The desktop app's side of a Browser view's design tools overlay (main's side is
// web/electron/src/designTools.js): a document this app opens, which main shows in a
// transparent view just above the native page, so the tools render into it with portals.
import { getEmbedRoot } from "@/lib/host";
import type { Answer, DesignToolsBridge } from "./desktopBridge";

/** An open overlay's document: its window, and the element the tools portal into. */
export interface OverlayDocument {
  win: Window;
  root: HTMLElement;
}

/** A capture of the page alone. */
export interface PageCapture {
  png: string;
  width: number;
  height: number;
  /** The page's viewport when it was taken (page CSS pixels), which marks are drawn in. */
  viewport: { width: number; height: number };
}

const NOT_OPEN = "The design tool isn't open on this page.";
const NO_OVERLAY = "the tool's overlay didn't open";
// Main's answer for a point or capture measured at the overlay's old zoom.
const ZOOM_CHANGED = "the page's zoom changed";
// Main's answer for a capture rect wholly outside the page's viewport.
const OFF_SCREEN = "nothing to capture there";

type Opener = (frame: string) => Window | null;

// The overlay's document opens into the frame main named for the opening.
const openBlank: Opener = (frame) => window.open("about:blank", frame);

/**
 * One view's overlay. Starts and stops run one at a time, in order, so a stop always
 * names the opening its start got.
 */
export class DesignOverlay {
  private chain: Promise<unknown> = Promise.resolve();
  private generation = 0;
  private open = false;
  /** The current start's opening, once main has named it; null once it stops. */
  private session: string | null = null;
  /** Each start's opening by generation, until its stop sends it. */
  private readonly openings = new Map<number, string>();
  /** Requests waiting for the current (or next) start's opening. */
  private readonly waiting = new Set<(session: string | null) => void>();
  private readonly failures = new Set<(error: string) => void>();
  private readonly endings = new Set<() => void>();
  private readonly listeners = new Set<() => void>();
  private shown: OverlayDocument | null = null;
  private release: (() => void) | null = null;

  private readonly bridge: DesignToolsBridge;
  private readonly openWindow: Opener;
  readonly viewId: string;

  constructor(bridge: DesignToolsBridge, viewId: string, openWindow: Opener = openBlank) {
    this.bridge = bridge;
    this.viewId = viewId;
    this.openWindow = openWindow;
  }

  private call(op: string, args: Record<string, unknown> = {}): Promise<Answer> {
    return this.bridge.browserDesignTools({ conversationId: this.viewId, op, ...args });
  }

  private enqueue<T>(task: () => Promise<T>): Promise<T> {
    const run = this.chain.then(task, task);
    this.chain = run.catch(() => undefined);
    return run;
  }

  // ── What the tools render into ───────────────────────────

  /** The open overlay's document, or null (for useSyncExternalStore). */
  readonly current = (): OverlayDocument | null => this.shown;

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private show(next: OverlayDocument | null): void {
    if (this.shown === next) return;
    this.shown = next;
    for (const listener of [...this.listeners]) listener();
  }

  /** Close the overlay's document, if one is open. */
  private closeDocument(): void {
    const shown = this.shown;
    this.release?.();
    this.release = null;
    this.show(null);
    if (shown && !shown.win.closed) shown.win.close();
  }

  // ── Openings ─────────────────────────────────────────────

  /**
   * Open the overlay; with `input` it's a tool's (over the whole page, taking the user's
   * mouse and keys), without it a notice's (only as big as the notice). If it can't
   * open, `onFailed` listeners hear why.
   */
  start(input: boolean): void {
    const generation = ++this.generation;
    this.open = true;
    void this.enqueue(async () => {
      if (generation !== this.generation) return;
      const answer = await this.call("start", { input }).catch((error: unknown): Answer => ({
        ok: false,
        error: String(error),
      }));
      if (answer.ok && answer.session) {
        // Its stop (already asked for, maybe) sends this, wherever it is by then.
        this.openings.set(generation, answer.session);
      }
      if (generation !== this.generation) return;
      const win = answer.ok && answer.frame ? this.openWindow(answer.frame) : null;
      if (!win || !answer.session) {
        this.open = false;
        this.wake(null);
        // Main's opening, if it has one, has nothing to show it.
        const session = this.openings.get(generation);
        this.openings.delete(generation);
        if (session) void this.call("stop", { session }).catch(() => undefined);
        const error = answer.ok ? NO_OVERLAY : (answer.error ?? "the page didn't answer");
        for (const listener of this.failures) listener(error);
        return;
      }
      const mirror = mirrorAppDocument(win);
      // Closed from outside (main ended it, the window went): the opening's over.
      const onHide = () => {
        if (this.shown?.win === win) this.lost();
      };
      win.addEventListener("pagehide", onHide);
      this.release = () => {
        win.removeEventListener("pagehide", onHide);
        mirror.dispose();
      };
      this.session = answer.session;
      this.wake(answer.session);
      this.show({ win, root: mirror.root });
    });
  }

  /** Stop this overlay's opening, and only it (another pane may have opened since). */
  stop(): void {
    const generation = this.generation;
    this.generation += 1;
    this.open = false;
    // Nothing more asks in its name, from now on.
    this.session = null;
    this.closeDocument();
    void this.enqueue(async () => {
      const session = this.openings.get(generation);
      this.openings.delete(generation);
      // A request still waiting here, with no newer start to wait for, never gets one.
      if (!this.open) this.wake(null);
      if (session) await this.call("stop", { session }).catch(() => undefined);
    });
  }

  /**
   * Main ended opening `session` (its page went): false, changing nothing, when that's
   * not the one open now (an old opening's end, arriving after another started).
   */
  ended(session?: string): boolean {
    if (session !== undefined && session !== this.session) return false;
    this.open = false;
    this.session = null;
    this.closeDocument();
    return true;
  }

  /** The overlay's document went without a stop or main's end. */
  private lost(): void {
    this.ended();
    for (const listener of [...this.endings]) listener();
  }

  /** The overlay closed under the tools (not by their stop). */
  onLost(listener: () => void): () => void {
    this.endings.add(listener);
    return () => this.endings.delete(listener);
  }

  /** A start that couldn't open. */
  onFailed(listener: (error: string) => void): () => void {
    this.failures.add(listener);
    return () => this.failures.delete(listener);
  }

  private wake(session: string | null): void {
    const waiting = [...this.waiting];
    this.waiting.clear();
    for (const resolve of waiting) resolve(session);
  }

  /** The current opening, or the next start's (a child's request comes before its parent starts). */
  private opened(): Promise<string | null> {
    if (this.session !== null) return Promise.resolve(this.session);
    if (!this.open) return Promise.resolve(null);
    return new Promise((resolve) => {
      this.waiting.add(resolve);
    });
  }

  // ── Requests ─────────────────────────────────────────────

  /**
   * Lay the overlay out over the page again (its document resized: main keeps it at the
   * page's zoom), or, for a notice, at `notice`'s size (page CSS pixels).
   */
  fit(notice?: { width: number; height: number }): void {
    const session = this.session;
    if (session) void this.call("fit", notice ? { session, notice } : { session }).catch(() => {});
  }

  /** The page alone, `rect` (page CSS pixels) of it or all of it. */
  async capture(rect?: {
    x: number;
    y: number;
    width: number;
    height: number;
  }): Promise<PageCapture> {
    const answer = await this.request("capture", rect ? { rect } : {}).catch((error: unknown) => {
      if (error instanceof Error && error.message === "empty capture") {
        throw new Error(
          "The Browser tab was hidden while it was captured. Show it and send again.",
        );
      }
      if (error instanceof Error && error.message === ZOOM_CHANGED) {
        throw new Error("The page's zoom changed while it was captured. Send again.");
      }
      if (error instanceof Error && error.message === OFF_SCREEN) {
        throw new Error("The element isn't on screen. Scroll to it and send again.");
      }
      throw error;
    });
    if (!answer.png) throw new Error("Couldn't capture the page.");
    return {
      png: answer.png,
      width: answer.width ?? 0,
      height: answer.height ?? 0,
      viewport: answer.viewport ?? { width: 0, height: 0 },
    };
  }

  /** One of main's page operations for the open tool (`hitTest`, `scroll`, `capture`). */
  async request(op: string, args: Record<string, unknown> = {}): Promise<Answer> {
    const session = await this.opened();
    if (!session) throw new Error(NOT_OPEN);
    const answer = await this.call(op, { session, ...args });
    if (!answer.ok) throw new Error(answer.error ?? "The page didn't answer.");
    return answer;
  }
}

// ── The overlay's document ─────────────────────────────────

// The overlay shows the page through it, and never scrolls.
const OVERLAY_CSS =
  "html, body { background: transparent !important; margin: 0 !important; " +
  "overflow: hidden !important; }";

const STYLES = 'link[rel~="stylesheet"], style';

/** Copy `from`'s attributes onto `to` (replacing its own), with `extraStyle` after its style. */
function copyAttributes(from: Element, to: Element, extraStyle = ""): void {
  for (const { name } of Array.from(to.attributes)) {
    if (!from.hasAttribute(name)) to.removeAttribute(name);
  }
  for (const { name, value } of Array.from(from.attributes)) to.setAttribute(name, value);
  if (extraStyle) to.setAttribute("style", `${from.getAttribute("style") ?? ""};${extraStyle}`);
}

/**
 * Make `win`'s document the app's: its stylesheets, and empty copies of its elements from
 * `<html>` down to where its portals go, with their attributes (theme, font size), laid out
 * as `display: contents`. Kept in step with the app until `dispose`.
 */
export function mirrorAppDocument(
  win: Window,
  appDocument: Document = document,
  portalParent: HTMLElement = getEmbedRoot() ?? appDocument.body,
): { root: HTMLElement; dispose: () => void } {
  const doc = win.document;
  doc.open();
  doc.write('<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>');
  doc.close();

  // The app's stylesheets, as it has them.
  const styles = new Map<Node, Node>();
  const addStyle = (node: Element) => {
    if (styles.has(node)) return;
    const copy = doc.importNode(node, true);
    // Whole, as the app resolved it. (`copy` is the overlay window's element, of its own
    // HTMLLinkElement, so it's set by attribute.)
    if (node instanceof HTMLLinkElement && node.href) copy.setAttribute("href", node.href);
    styles.set(node, copy);
    doc.head.append(copy);
  };
  const syncStyles = () => {
    const present = new Set(appDocument.querySelectorAll(STYLES));
    for (const [node, copy] of styles) {
      if (present.has(node as Element)) continue;
      copy.parentNode?.removeChild(copy);
      styles.delete(node);
    }
    for (const node of present) addStyle(node);
    // A style element's text can change (the dev server's updates).
    for (const [node, copy] of styles) {
      if (node instanceof HTMLStyleElement && copy.textContent !== node.textContent) {
        copy.textContent = node.textContent;
      }
    }
  };
  syncStyles();
  const own = doc.createElement("style");
  own.textContent = OVERLAY_CSS;
  doc.head.append(own);

  // The app's elements down to the portals' parent.
  const chain: HTMLElement[] = [];
  for (let node: HTMLElement | null = portalParent; node; node = node.parentElement) {
    chain.unshift(node);
  }
  const pairs: [HTMLElement, HTMLElement, string][] = [];
  let parent: HTMLElement = doc.body;
  for (const element of chain) {
    if (element === appDocument.documentElement) {
      pairs.push([element, doc.documentElement, ""]);
    } else if (element === appDocument.body) {
      pairs.push([element, doc.body, ""]);
    } else {
      const copy = doc.createElement(element.tagName.toLowerCase());
      parent.append(copy);
      parent = copy;
      pairs.push([element, copy, "display:contents"]);
    }
  }
  const syncAttributes = () => {
    for (const [from, to, extra] of pairs) copyAttributes(from, to, extra);
  };
  syncAttributes();
  const root = doc.createElement("div");
  parent.append(root);

  const observer = new MutationObserver(() => {
    syncStyles();
    syncAttributes();
  });
  observer.observe(appDocument.head, { childList: true, subtree: true, characterData: true });
  if (appDocument.body !== portalParent) observer.observe(appDocument.body, { childList: true });
  for (const [from] of pairs) observer.observe(from, { attributes: true });
  return { root, dispose: () => observer.disconnect() };
}
