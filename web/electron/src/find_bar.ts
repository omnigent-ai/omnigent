/**
 * Find in page. Cmd/Ctrl+F opens a small frameless child window (the bundled
 * find/index.html) anchored to the parent's top-right corner; the actual
 * search runs in the main process against the PARENT's webContents via
 * findInPage. A child window (rather than DOM injected into the remote SPA)
 * keeps the shell's hands off server-controlled pages entirely.
 *
 * All Electron surfaces are injected so the bar is unit-testable.
 */

import { pathToFileURL } from "node:url";
import type { BrowserWindowConstructorOptions, FoundInPageResult, Rectangle } from "electron";

import { on, push, type IpcMainOnPort, type PushTarget } from "./ipc/main.ts";

/** Find bar dimensions and inset from the parent's top-right corner. */
export const FIND_BAR_WIDTH = 320;
export const FIND_BAR_HEIGHT = 44;
export const FIND_BAR_INSET = 16;

type ParentWindowEvent = "resize" | "move";

/** The parts of Chromium's find result the bar shows. */
export type FindBarMatches = Pick<FoundInPageResult, "activeMatchOrdinal" | "matches">;

type FoundListener = (event: unknown, result: FindBarMatches) => void;
type NavigateListener = (event: unknown, url: string, isMainFrame: boolean) => void;

/** The parts of a shell window's webContents the bar searches and follows. */
export interface FindBarParentContents extends PushTarget {
  getURL: () => string;
  isDestroyed: () => boolean;
  findInPage: (text: string, options: { forward: boolean; findNext: boolean }) => unknown;
  stopFindInPage: (action: "clearSelection") => void;
  on: ((event: "found-in-page", listener: FoundListener) => unknown) &
    ((event: "did-navigate-in-page", listener: NavigateListener) => unknown);
  removeListener: ((event: "found-in-page", listener: FoundListener) => unknown) &
    ((event: "did-navigate-in-page", listener: NavigateListener) => unknown);
}

/** The parts of a shell window the bar is anchored to. */
export interface FindBarParent {
  webContents: FindBarParentContents;
  isDestroyed: () => boolean;
  focus: () => void;
  getContentBounds: () => Rectangle;
  on: (event: ParentWindowEvent, listener: () => void) => unknown;
  removeListener: (event: ParentWindowEvent, listener: () => void) => unknown;
}

/** The parts of the bar's own BrowserWindow this module drives. */
export interface FindBarWindow {
  webContents: PushTarget;
  isDestroyed: () => boolean;
  focus: () => void;
  show: () => void;
  close: () => void;
  setBounds: (bounds: Rectangle) => void;
  loadFile: (filePath: string) => Promise<void>;
  on: (event: "closed", listener: () => void) => unknown;
  once: (event: "ready-to-show", listener: () => void) => unknown;
}

/** A window that has focus: a shell window, a bar, or another shell-owned window. */
export interface FindBarFocusedWindow {
  getParentWindow: () => unknown;
}

/** A window an IPC sender belongs to, as far as find-close goes. */
export interface FindBarSenderWindow {
  isDestroyed: () => boolean;
  close: () => void;
}

/** The parts of an IPC event the bar's handlers read. */
export interface FindBarIpcEvent {
  sender: PushTarget;
  senderFrame?: { url?: string | undefined } | null | undefined;
}

export type FindBarWindowOptions<Parent> = Omit<BrowserWindowConstructorOptions, "parent"> & {
  parent: Parent;
};

export interface FindBarDeps<Parent extends FindBarParent, Bar extends FindBarWindow> {
  BrowserWindow: new (options: FindBarWindowOptions<Parent>) => Bar;
  ipcMain: IpcMainOnPort<FindBarIpcEvent>;
  /** Absolute path to the bundled find/index.html. */
  pagePath: string;
  /** Absolute path to find_preload.js. */
  preloadPath: string;
  getFocusedWindow: () => FindBarFocusedWindow | null;
  /** The window that owns an IPC sender's webContents, or null. */
  windowForSender: (sender: PushTarget) => FindBarSenderWindow | null;
  /** True for the shell's own app windows (never bars or other utility windows). */
  isShellWindow: (win: unknown) => win is Parent;
  /** The focused shell window, falling back to any open one. */
  activeWindow: () => Parent | null;
  warn?: (message: string) => void;
}

export interface FindBar<Parent> {
  /** Open the bar for a shell window, or re-focus the one already open. */
  open: (target: Parent) => void;
  /** The shell window Cmd/Ctrl+F should act on. */
  targetForShortcut: () => Parent | null;
  /** Register the bar's IPC channels. */
  registerIpc: () => void;
}

/** A property of an untrusted value, read the way `value?.[key]` would. */
function propertyOf(value: unknown, key: string): unknown {
  return value == null ? undefined : (value as Record<string, unknown>)[key];
}

/** The conversation a shell URL shows, or null. Routes end in `/c/<id>` after any mount. */
export function findConversationId(url: string): string | null {
  return /\/c\/([^/]+)\/?$/.exec(URL.parse(url)?.pathname ?? "")?.[1] ?? null;
}

export function createFindBar<Parent extends FindBarParent, Bar extends FindBarWindow>({
  BrowserWindow,
  ipcMain,
  pagePath,
  preloadPath,
  getFocusedWindow,
  windowForSender,
  isShellWindow,
  activeWindow,
  warn = () => {},
}: FindBarDeps<Parent, Bar>): FindBar<Parent> {
  /** The find bar's file:// URL, for verifying IPC sender frames. */
  const pageUrl = pathToFileURL(pagePath);

  /**
   * The open find bar per shell window. At most one bar per window; absent
   * when the window has no bar open.
   */
  const findBars = new Map<Parent, Bar>();

  /**
   * Anchor a find bar to its parent's top-right content corner. Called at
   * creation and again whenever the parent moves or resizes.
   */
  function positionFindBar(target: Parent, bar: Bar): void {
    if (target.isDestroyed() || bar.isDestroyed()) return;
    const content = target.getContentBounds();
    bar.setBounds({
      x: content.x + content.width - FIND_BAR_WIDTH - FIND_BAR_INSET,
      y: content.y + FIND_BAR_INSET,
      width: FIND_BAR_WIDTH,
      height: FIND_BAR_HEIGHT,
    });
  }

  /**
   * The bar is a frameless always-on-top-of-parent child window with its own
   * narrow preload (`find_preload.ts`); search results stream back to it via
   * the parent webContents' `found-in-page` event.
   */
  function open(target: Parent): void {
    // The menu item opens the bar without a page keydown; the page must still
    // render every transcript row for find to reach off-screen messages.
    push(target.webContents, "omnigent:find-opened");
    const existing = findBars.get(target);
    if (existing && !existing.isDestroyed()) {
      existing.focus();
      push(existing.webContents, "omnigent:find-activate");
      return;
    }
    const bar = new BrowserWindow({
      parent: target,
      frame: false,
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      // Transparent so the page's rounded-corner card is the visible shape.
      transparent: true,
      show: false,
      webPreferences: {
        preload: preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    findBars.set(target, bar);
    void bar.loadFile(pagePath);

    const reposition = () => positionFindBar(target, bar);
    const onFound = (_event: unknown, result: FindBarMatches) => {
      if (bar.isDestroyed()) return;
      push(bar.webContents, "omnigent:find-result", {
        active: result.activeMatchOrdinal,
        matches: result.matches,
      });
    };
    // Results belong to the searched conversation, so switching closes the bar.
    const openedIn = findConversationId(target.webContents.getURL());
    const onNavigate = (_event: unknown, url: string, isMainFrame: boolean) => {
      if (isMainFrame && findConversationId(url) !== openedIn) bar.close();
    };
    target.on("resize", reposition);
    target.on("move", reposition);
    target.webContents.on("found-in-page", onFound);
    target.webContents.on("did-navigate-in-page", onNavigate);

    bar.once("ready-to-show", () => {
      reposition();
      bar.show();
    });
    bar.on("closed", () => {
      findBars.delete(target);
      if (target.isDestroyed()) return;
      target.removeListener("resize", reposition);
      target.removeListener("move", reposition);
      target.webContents.removeListener("found-in-page", onFound);
      target.webContents.removeListener("did-navigate-in-page", onNavigate);
      // Closing the parent (Cmd+W) destroys its webContents before this child's
      // "closed" fires, while the parent window itself is not yet destroyed.
      if (target.webContents.isDestroyed()) return;
      target.webContents.stopFindInPage("clearSelection");
      target.focus();
    });
  }

  /**
   * Like activeWindow(), but when the focused window is a find BAR (focus
   * sits in its input), the shortcut targets the bar's parent shell window
   * rather than falling back to an arbitrary one.
   */
  function targetForShortcut(): Parent | null {
    const focused = getFocusedWindow();
    if (focused && !isShellWindow(focused)) {
      const parent = focused.getParentWindow();
      if (parent && isShellWindow(parent)) return parent;
    }
    return activeWindow();
  }

  /**
   * The shell window whose bar owns the sending webContents. Null for senders
   * that aren't a live find bar; callers must drop those messages.
   */
  function findBarTarget(event: FindBarIpcEvent): Parent | null {
    for (const [target, bar] of findBars) {
      if (!bar.isDestroyed() && bar.webContents === event.sender) return target;
    }
    return null;
  }

  /** True when an IPC event was sent by the bundled find bar page. */
  function isFindBarSender(event: FindBarIpcEvent): boolean {
    const frameUrl = event.senderFrame?.url ?? "";
    let url: URL;
    try {
      url = new URL(frameUrl);
    } catch {
      return false;
    }
    return url.protocol === "file:" && url.pathname === pageUrl.pathname;
  }

  function registerIpc(): void {
    // Find bar → run/continue a search in its parent window. Empty text
    // clears the highlight and zeroes the counter (findInPage rejects empty
    // queries, so it never reaches it).
    on(ipcMain, "omnigent:find-query", (event, params) => {
      if (!isFindBarSender(event)) {
        warn("[omnigent] find-query from untrusted sender dropped");
        return;
      }
      const target = findBarTarget(event);
      if (!target || target.isDestroyed()) return;
      const text = String(propertyOf(params, "text") ?? "");
      if (text === "") {
        target.webContents.stopFindInPage("clearSelection");
        push(event.sender, "omnigent:find-result", { active: 0, matches: 0 });
        return;
      }
      target.webContents.findInPage(text, {
        forward: propertyOf(params, "forward") !== false,
        findNext: propertyOf(params, "findNext") === true,
      });
    });

    // Find bar → dismiss itself (Esc / ✕). Cleanup (stop search, refocus the
    // parent) lives in the bar's "closed" handler in open().
    on(ipcMain, "omnigent:find-close", (event) => {
      if (!isFindBarSender(event)) {
        warn("[omnigent] find-close from untrusted sender dropped");
        return;
      }
      const bar = windowForSender(event.sender);
      if (bar && !bar.isDestroyed()) bar.close();
    });
  }

  return { open, targetForShortcut, registerIpc };
}
