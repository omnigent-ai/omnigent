// Shell-owned desktop update overlay window.
//
// Renders the SAME web `UpdateBanner` component (built into electron/overlay/
// by web's `build:overlay`) inside a transparent, frameless child window
// anchored to the bottom-right of a shell window — so the update UI ships with
// the desktop app and appears regardless of the connected server's web-bundle
// version (an old server without UpdateBanner must still be able to tell the
// user the desktop app is out of date).
//
// The overlay is a TRUSTED surface: its `omnigent:overlay-*` IPC drives the
// updater directly (no pinned-origin gate, no per-action consent dialog — the
// server-page IPC keeps those). The window sizes itself to the card via the
// height the overlay reports, and hides when the card renders nothing.

import type { BrowserWindowConstructorOptions, Rectangle } from "electron";
import { pathToFileURL } from "node:url";

import type { DesktopUpdater } from "./desktop_updater.ts";

export const OVERLAY_WIDTH = 344; // 320px card + 12px shadow gutter each side
export const OVERLAY_INSET = 12;

type ParentWindowEvent = "resize" | "move" | "closed";

/** The parts of a shell window the overlay is anchored to. */
export interface UpdateOverlayParent {
  isDestroyed: () => boolean;
  getContentBounds: () => Rectangle;
  webContents: {
    isDestroyed: () => boolean;
    send: (channel: string, height: number) => void;
  } | null;
  on: (event: ParentWindowEvent, listener: () => void) => unknown;
  removeListener: (event: ParentWindowEvent, listener: () => void) => unknown;
}

/** The parts of the overlay's own BrowserWindow this module drives. */
export interface UpdateOverlayWindow {
  excludedFromShownWindowsMenu?: boolean;
  isDestroyed: () => boolean;
  isVisible: () => boolean;
  showInactive: () => void;
  destroy: () => void;
  setBounds: (bounds: Rectangle) => void;
  setIgnoreMouseEvents: (ignore: boolean, options?: { forward?: boolean }) => void;
  loadFile: (filePath: string, options: { search: string }) => Promise<void>;
  on: (event: "closed", listener: () => void) => unknown;
  webContents: {
    setWindowOpenHandler: (handler: () => { action: "deny" }) => void;
    on: ((
      event: "will-navigate",
      listener: (event: { preventDefault: () => void }, url: string) => void,
    ) => unknown) &
      ((event: "did-finish-load", listener: () => void) => unknown);
    send: (channel: string, theme: "dark" | "light") => void;
  };
}

export type UpdateOverlayWindowOptions<Parent> = Omit<BrowserWindowConstructorOptions, "parent"> & {
  parent: Parent;
};

/** The parts of an IPC event used to recognize the overlay page. */
export interface UpdateOverlayIpcEvent {
  sender: { getURL?: () => string } | null | undefined;
  senderFrame?: { url?: string } | null;
}

export type UpdateOverlayUpdater = Pick<
  DesktopUpdater,
  | "getConfig"
  | "getStatus"
  | "setConfig"
  | "checkForUpdates"
  | "downloadUpdate"
  | "installUpdateNow"
>;

export interface UpdateOverlayDeps<
  Parent extends UpdateOverlayParent,
  Overlay extends UpdateOverlayWindow,
> {
  BrowserWindow: new (options: UpdateOverlayWindowOptions<Parent>) => Overlay;
  ipcMain: {
    on: (
      channel: string,
      listener: (event: UpdateOverlayIpcEvent, ...args: unknown[]) => void,
    ) => unknown;
    handle: (
      channel: string,
      listener: (event: UpdateOverlayIpcEvent, ...args: unknown[]) => unknown,
    ) => void;
  };
  nativeTheme: {
    shouldUseDarkColors: boolean;
    on: (event: "updated", listener: () => void) => unknown;
  };
  updater: UpdateOverlayUpdater;
  openAbout: (parent: Parent) => void;
  /** Absolute path to the built overlay HTML. */
  overlayPage: string;
  /** Absolute path to update_overlay_preload.js. */
  preloadPath: string;
  /** Runtime platform (injectable for tests). */
  platform?: NodeJS.Platform;
}

export interface UpdateOverlay<Parent, Overlay> {
  ensureOverlay: (parent: Parent) => Overlay;
  suppress: (parent: Parent) => void;
  unsuppress: (parent: Parent) => void;
  registerIpc: () => void;
}

export function createUpdateOverlay<
  Parent extends UpdateOverlayParent,
  Overlay extends UpdateOverlayWindow,
>({
  BrowserWindow,
  ipcMain,
  nativeTheme,
  updater,
  openAbout,
  overlayPage,
  preloadPath,
  platform = process.platform,
}: UpdateOverlayDeps<Parent, Overlay>): UpdateOverlay<Parent, Overlay> {
  const overlayPageUrl = pathToFileURL(overlayPage);
  /** parent -> overlay */
  const overlays = new Map<Parent, Overlay>();
  /** overlay -> last reported height */
  const heights = new WeakMap<Overlay, number>();
  /** Parents whose update flow has moved into the About modal. */
  const suppressedParents = new Set<Parent | null>();

  function overlayForSender(event: UpdateOverlayIpcEvent): Overlay | null {
    for (const ov of overlays.values()) {
      if (ov.isDestroyed() || ov.webContents !== event.sender) continue;
      const frameUrl = event.senderFrame?.url ?? event.sender?.getURL?.() ?? "";
      try {
        const url = new URL(frameUrl);
        if (url.protocol === "file:" && url.pathname === overlayPageUrl.pathname) return ov;
      } catch {
        return null;
      }
    }
    return null;
  }

  function parentOf(overlay: Overlay): Parent | null {
    for (const [parent, ov] of overlays) {
      if (ov === overlay) return parent;
    }
    return null;
  }

  function overlayHeightForSender(event: UpdateOverlayIpcEvent): number {
    for (const [parent, overlay] of overlays) {
      if (!parent.isDestroyed() && parent.webContents === event.sender) {
        return heights.get(overlay) ?? 0;
      }
    }
    return 0;
  }

  function notifyParentHeight(parent: Parent | null, height: number): void {
    if (!parent || parent.isDestroyed()) return;
    const contents = parent.webContents;
    // During BrowserWindow's "closed" event Electron may already have
    // destroyed the renderer while the BrowserWindow wrapper has not yet
    // started reporting isDestroyed(). The child overlay closes from that
    // event and must not send its final height into the dead renderer.
    if (!contents || contents.isDestroyed()) return;
    contents.send("omnigent:update-overlay-height", height);
  }

  function collapse(parent: Parent | null, overlay: Overlay): void {
    notifyParentHeight(parent, 0);
    position(parent, overlay, 1);
    overlay.setIgnoreMouseEvents(true, { forward: true });
    if (!overlay.isVisible()) overlay.showInactive();
  }

  function suppress(parent: Parent): void {
    const overlay = overlays.get(parent);
    if (!overlay || overlay.isDestroyed()) return;
    suppressedParents.add(parent);
    collapse(parent, overlay);
  }

  function unsuppress(parent: Parent): void {
    const overlay = overlays.get(parent);
    suppressedParents.delete(parent);
    if (!overlay || overlay.isDestroyed() || parent.isDestroyed()) return;
    const height = heights.get(overlay) ?? 0;
    notifyParentHeight(parent, height);
    position(parent, overlay, Math.max(1, height));
    overlay.setIgnoreMouseEvents(height === 0, height === 0 ? { forward: true } : undefined);
    if (!overlay.isVisible()) overlay.showInactive();
  }

  function position(parent: Parent | null, overlay: Overlay, height: number): void {
    if (!parent || parent.isDestroyed() || overlay.isDestroyed()) return;
    const content = parent.getContentBounds();
    overlay.setBounds({
      x: content.x + content.width - OVERLAY_WIDTH - OVERLAY_INSET,
      y: content.y + content.height - height - OVERLAY_INSET,
      width: OVERLAY_WIDTH,
      height,
    });
  }

  /** Create (once) the overlay window for a shell window and load the card. */
  function ensureOverlay(parent: Parent): Overlay {
    const existing = overlays.get(parent);
    if (existing && !existing.isDestroyed()) return existing;

    // macOS otherwise presents this helper as a peer app window in Mission
    // Control and screen-share pickers. Keep the update card mouse-actionable
    // without letting it take focus from its parent. Do not apply focusable:
    // false on Linux: Electron documents that as making the window unmanaged,
    // always-on-top, and visible across every workspace.
    const macOverlayOptions: Pick<
      BrowserWindowConstructorOptions,
      "focusable" | "hiddenInMissionControl" | "acceptFirstMouse"
    > =
      platform === "darwin"
        ? {
            focusable: false,
            hiddenInMissionControl: true,
            acceptFirstMouse: true,
          }
        : {};

    const overlay = new BrowserWindow({
      parent,
      frame: false,
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      transparent: true,
      hasShadow: false, // the card draws its own shadow
      show: false,
      ...macOverlayOptions,
      width: OVERLAY_WIDTH,
      height: 1,
      webPreferences: {
        preload: preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    if (platform === "darwin") {
      overlay.excludedFromShownWindowsMenu = true;
    }
    overlays.set(parent, overlay);
    overlay.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    overlay.webContents.on("will-navigate", (event, targetUrl) => {
      try {
        const url = new URL(targetUrl);
        if (url.protocol === "file:" && url.pathname === overlayPageUrl.pathname) return;
      } catch {
        // Block malformed targets below.
      }
      event.preventDefault();
    });

    // The ?theme= URL param is only a pre-paint hint to avoid a flash before
    // the renderer subscribes; it's stale after a reload (the URL is fixed at
    // creation time from the OS theme, not the in-app theme that may have been
    // pushed via setColorScheme since). did-finish-load fires on every load —
    // including Cmd+R reloads — so push the LIVE theme here to correct it.
    const theme = nativeTheme.shouldUseDarkColors ? "dark" : "light";
    void overlay.loadFile(overlayPage, { search: `theme=${theme}` });
    overlay.webContents.on("did-finish-load", () => {
      if (overlay.isDestroyed()) return;
      overlay.webContents.send(
        "omnigent:overlay-theme",
        nativeTheme.shouldUseDarkColors ? "dark" : "light",
      );
    });

    const reposition = () => position(parent, overlay, heights.get(overlay) ?? 1);
    parent.on("resize", reposition);
    parent.on("move", reposition);
    // Electron does NOT auto-close child windows when their parent closes, so
    // tear the overlay down explicitly — otherwise a transparent, skipTaskbar
    // child is orphaned with live IPC handlers and a dangling Map entry.
    const onParentClosed = () => {
      if (!overlay.isDestroyed()) overlay.destroy();
    };
    parent.on("closed", onParentClosed);
    overlay.on("closed", () => {
      notifyParentHeight(parent, 0);
      overlays.delete(parent);
      suppressedParents.delete(parent);
      if (!parent.isDestroyed()) {
        parent.removeListener("resize", reposition);
        parent.removeListener("move", reposition);
        parent.removeListener("closed", onParentClosed);
      }
    });
    return overlay;
  }

  function registerIpc() {
    // The overlay reports its rendered card height; 0 => nothing to show.
    //
    // We must NOT hide() the window when empty: a hidden BrowserWindow suspends
    // its renderer, so its ResizeObserver stops firing and the card could never
    // report a height again to re-appear (e.g. after a transient "checking"
    // state collapses it). Instead keep the window shown but collapse it to an
    // invisible, click-through 1px sliver, which keeps layout — and the
    // ResizeObserver — alive so it expands again the moment there's content.
    ipcMain.on("omnigent:overlay-height", (event, height) => {
      const overlay = overlayForSender(event);
      if (!overlay) return;
      const h = Math.max(0, Math.round(Number(height) || 0));
      const parent = parentOf(overlay);
      if (suppressedParents.has(parent)) {
        heights.set(overlay, h);
        const state = updater.getStatus()?.state;
        if (state === "available" || state === "downloading" || state === "downloaded") {
          collapse(parent, overlay);
          return;
        }
        suppressedParents.delete(parent);
      }
      heights.set(overlay, h);
      notifyParentHeight(parent, h);
      if (h > 0) {
        position(parent, overlay, h);
        overlay.setIgnoreMouseEvents(false);
      } else {
        position(parent, overlay, 1);
        overlay.setIgnoreMouseEvents(true, { forward: true });
      }
      if (!overlay.isVisible()) overlay.showInactive();
    });

    // The parent SPA reads the current value on mount so an overlay that became
    // visible before its listener attached still reserves the correct space.
    ipcMain.handle("omnigent:get-update-overlay-height", (event) => overlayHeightForSender(event));

    // Trusted updater controls for the overlay page only.
    const guard = (event: UpdateOverlayIpcEvent): Overlay => {
      const overlay = overlayForSender(event);
      if (!overlay) {
        throw new Error("update overlay IPC is only available to the shell overlay page");
      }
      return overlay;
    };
    ipcMain.handle("omnigent:overlay-get-update-config", (event) => {
      guard(event);
      return updater.getConfig();
    });
    ipcMain.handle("omnigent:overlay-get-update-status", (event) => {
      guard(event);
      return updater.getStatus();
    });
    ipcMain.handle("omnigent:overlay-update-check", async (event) => {
      guard(event);
      await updater.checkForUpdates({ manual: true });
    });
    ipcMain.handle("omnigent:overlay-update-download", async (event) => {
      const overlay = guard(event);
      const parent = parentOf(overlay);
      if (!parent || parent.isDestroyed()) {
        throw new Error("The update prompt no longer has an active parent window");
      }
      openAbout(parent);
      suppress(parent);
      try {
        await updater.downloadUpdate();
      } catch (err) {
        suppressedParents.delete(parent);
        throw err;
      }
    });
    ipcMain.handle("omnigent:overlay-update-install", (event) => {
      guard(event);
      if (!updater.installUpdateNow()) {
        throw new Error("No downloaded update is ready to install.");
      }
    });
    ipcMain.handle("omnigent:overlay-set-update-config", (event, patch) => {
      guard(event);
      return updater.setConfig(patch);
    });

    // Keep the card's theme in sync with the OS/app appearance.
    nativeTheme.on("updated", () => {
      const theme = nativeTheme.shouldUseDarkColors ? "dark" : "light";
      for (const overlay of overlays.values()) {
        if (!overlay.isDestroyed()) {
          overlay.webContents.send("omnigent:overlay-theme", theme);
        }
      }
    });
  }

  return { ensureOverlay, suppress, unsuppress, registerIpc };
}
