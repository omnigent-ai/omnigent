import type { BrowserWindowConstructorOptions, Rectangle } from "electron";

import { pagePath, preloadPath } from "./paths.ts";

// Loads that finish sooner never show the indicator, so fast connects don't flash it.
const SHOW_DELAY_MS = 300;

type ParentWindowEvent = "move" | "resize" | "closed";

/** The parts of a shell window the indicator follows. */
export interface LoadingParentWindow {
  isDestroyed: () => boolean;
  getContentBounds: () => Rectangle;
  on: (event: ParentWindowEvent, listener: () => void) => unknown;
  removeListener: (event: ParentWindowEvent, listener: () => void) => unknown;
}

/** The parts of the indicator's own BrowserWindow this module drives. */
export interface LoadingIndicatorWindow {
  excludedFromShownWindowsMenu?: boolean;
  isDestroyed: () => boolean;
  destroy: () => void;
  setIgnoreMouseEvents: (ignore: boolean, options?: { forward?: boolean }) => void;
  setBounds: (bounds: Rectangle) => void;
  showInactive: () => void;
  loadFile: (filePath: string) => Promise<void>;
  on: (event: "closed", listener: () => void) => unknown;
  webContents: {
    setWindowOpenHandler: (handler: () => { action: "deny" }) => void;
    on: ((
      event: "will-navigate",
      listener: (event: { preventDefault: () => void }) => void,
    ) => unknown) &
      ((event: "did-finish-load", listener: () => void) => unknown);
    send: (channel: string, label: string) => void;
  };
}

export type LoadingWindowOptions<Parent> = Omit<BrowserWindowConstructorOptions, "parent"> & {
  parent: Parent;
};

export interface ConnectionLoadingDeps<Parent extends LoadingParentWindow> {
  BrowserWindow: new (options: LoadingWindowOptions<Parent>) => LoadingIndicatorWindow;
  platform?: NodeJS.Platform;
}

export interface ConnectionLoading<Parent> {
  show: (parent: Parent, attempt: object, label: string) => void;
  hide: (parent: Parent, attempt: object) => void;
}

interface LoadingEntry {
  window: LoadingIndicatorWindow;
  attempt: object;
  label: string;
  ready: boolean;
  due: boolean;
}

/** A shell-owned loading indicator that survives replacement of the server document. */
export function createConnectionLoading<Parent extends LoadingParentWindow>({
  BrowserWindow,
  platform = process.platform,
}: ConnectionLoadingDeps<Parent>): ConnectionLoading<Parent> {
  const entries = new Map<Parent, LoadingEntry>();

  function show(parent: Parent, attempt: object, label: string): void {
    if (parent.isDestroyed()) return;
    let entry = entries.get(parent);
    if (!entry) {
      const window = new BrowserWindow({
        parent,
        frame: false,
        transparent: true,
        hasShadow: false,
        show: false,
        resizable: false,
        movable: false,
        minimizable: false,
        maximizable: false,
        fullscreenable: false,
        skipTaskbar: true,
        ...(platform === "darwin" ? { focusable: false, hiddenInMissionControl: true } : {}),
        width: 340,
        height: 64,
        webPreferences: {
          preload: preloadPath("connection_loading_preload"),
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
        },
      });
      if (platform === "darwin") window.excludedFromShownWindowsMenu = true;
      window.setIgnoreMouseEvents(true, { forward: true });
      entry = { window, attempt, label, ready: false, due: false };
      entries.set(parent, entry);
      const current = entry;
      const reveal = () => {
        if (
          current.ready &&
          current.due &&
          !window.isDestroyed() &&
          entries.get(parent) === current
        )
          window.showInactive();
      };
      const timer = setTimeout(() => {
        current.due = true;
        reveal();
      }, SHOW_DELAY_MS);
      const position = () => {
        if (parent.isDestroyed() || window.isDestroyed()) return;
        const bounds = parent.getContentBounds();
        window.setBounds({
          x: bounds.x + Math.round((bounds.width - 340) / 2),
          y: bounds.y + 40,
          width: 340,
          height: 64,
        });
      };
      const close = () => {
        if (!window.isDestroyed()) window.destroy();
      };
      parent.on("move", position);
      parent.on("resize", position);
      parent.on("closed", close);
      window.on("closed", () => {
        clearTimeout(timer);
        entries.delete(parent);
        parent.removeListener("move", position);
        parent.removeListener("resize", position);
        parent.removeListener("closed", close);
      });
      window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      window.webContents.on("will-navigate", (event) => event.preventDefault());
      window.webContents.on("did-finish-load", () => {
        if (window.isDestroyed() || entries.get(parent) !== current) return;
        current.ready = true;
        window.webContents.send("omnigent:connection-loading", current.label);
        reveal();
      });
      position();
      void window.loadFile(pagePath("connection-loading")).catch(close);
    }
    entry.attempt = attempt;
    entry.label = label;
    if (entry.ready) entry.window.webContents.send("omnigent:connection-loading", label);
  }

  function hide(parent: Parent, attempt: object): void {
    const entry = entries.get(parent);
    if (entry?.attempt === attempt && !entry.window.isDestroyed()) entry.window.destroy();
  }

  return { show, hide };
}
