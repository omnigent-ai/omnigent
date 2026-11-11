// Shell-owned "return to your server?" banner window.
//
// Shown when a shell window has been sitting on a foreign page (typically an
// SSO/IdP login) instead of the server it is pinned to — see away_banner.ts.
// Like the update overlay, it is a transparent, frameless child window with a
// bundled page and a narrow preload: the page the user is on is foreign and
// untrusted, so the banner can NOT ride the page's bridge and must be a
// shell-owned surface whose IPC is verified by sender.
//
// Unlike the update overlay the banner is created LAZILY (first show), so
// windows that never leave their server never pay for the child window.

import type { BrowserWindowConstructorOptions, Rectangle } from "electron";

// The window is larger than the card by the gutter on every side: a
// transparent window clips painting at its square bounds, so the card needs
// interior room for its border radius and shadow (the update overlay's
// "shadow gutter" pattern). Keep the gutter small — it is invisible dead
// space between the card and the window edge.
export const BANNER_WIDTH = 440;
export const BANNER_HEIGHT = 64;
export const BANNER_INSET = 12;

type ParentWindowEvent = "resize" | "move" | "closed";

/** The parts of a shell window the banner sits on and reloads. */
export interface ReturnBannerParent {
  isDestroyed: () => boolean;
  getContentBounds: () => Rectangle;
  loadURL: (url: string) => Promise<unknown>;
  on: (event: ParentWindowEvent, listener: () => void) => unknown;
  removeListener: (event: ParentWindowEvent, listener: () => void) => unknown;
}

/** The parts of the banner's own BrowserWindow this module drives. */
export interface ReturnBannerWindow {
  excludedFromShownWindowsMenu?: boolean;
  webContents: unknown;
  isDestroyed: () => boolean;
  isVisible: () => boolean;
  destroy: () => void;
  hide: () => void;
  showInactive: () => void;
  setBounds: (bounds: Rectangle) => void;
  loadFile: (filePath: string) => Promise<void>;
  on: (event: "closed", listener: () => void) => unknown;
}

export type ReturnBannerWindowOptions<Parent> = Omit<BrowserWindowConstructorOptions, "parent"> & {
  parent: Parent;
};

export interface ReturnBannerDeps<Parent extends ReturnBannerParent> {
  BrowserWindow: new (options: ReturnBannerWindowOptions<Parent>) => ReturnBannerWindow;
  ipcMain: {
    on: (channel: string, listener: (event: { sender: unknown }) => void) => unknown;
  };
  /** Absolute path to the bundled banner HTML. */
  bannerPage: string;
  /** Absolute path to return_banner_preload.js. */
  preloadPath: string;
  /**
   * Called when the user clicks "Go back", before the parent reloads — lets
   * the away-watch re-arm for the case where the return lands on SSO again.
   */
  onGoBack?: (parent: Parent) => void;
  /** Runtime platform (injectable for tests). */
  platform?: NodeJS.Platform;
}

export interface ReturnBanner<Parent> {
  ensureBanner: (parent: Parent) => ReturnBannerWindow;
  show: (parent: Parent | null | undefined, returnUrl: string | null | undefined) => void;
  hide: (parent: Parent) => void;
  registerIpc: () => void;
}

export function createReturnBanner<Parent extends ReturnBannerParent>({
  BrowserWindow,
  ipcMain,
  bannerPage,
  preloadPath,
  onGoBack,
  platform,
}: ReturnBannerDeps<Parent>): ReturnBanner<Parent> {
  /** parent -> banner */
  const banners = new Map<Parent, ReturnBannerWindow>();
  /** parent -> URL "Go back" loads */
  const returnUrls = new Map<Parent, string>();

  function bannerForSender(event: { sender: unknown }): ReturnBannerWindow | null {
    for (const banner of banners.values()) {
      if (!banner.isDestroyed() && banner.webContents === event.sender) return banner;
    }
    return null;
  }

  function parentOf(banner: ReturnBannerWindow): Parent | null {
    for (const [parent, b] of banners) {
      if (b === banner) return parent;
    }
    return null;
  }

  function position(parent: Parent, banner: ReturnBannerWindow): void {
    if (!parent || parent.isDestroyed() || banner.isDestroyed()) return;
    const content = parent.getContentBounds();
    banner.setBounds({
      x: content.x + Math.round((content.width - BANNER_WIDTH) / 2),
      y: content.y + BANNER_INSET,
      width: BANNER_WIDTH,
      height: BANNER_HEIGHT,
    });
  }

  /** Create (once) the banner window for a shell window and load the page. */
  function ensureBanner(parent: Parent): ReturnBannerWindow {
    const existing = banners.get(parent);
    if (existing && !existing.isDestroyed()) return existing;

    const banner = new BrowserWindow({
      parent,
      frame: false,
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      transparent: true,
      hasShadow: false, // the banner page draws its own border/shadow
      show: false,
      width: BANNER_WIDTH,
      height: BANNER_HEIGHT,
      webPreferences: {
        preload: preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    if (platform === "darwin" || (platform === undefined && process.platform === "darwin")) {
      banner.excludedFromShownWindowsMenu = true;
    }
    banners.set(parent, banner);
    void banner.loadFile(bannerPage);

    const reposition = () => position(parent, banner);
    parent.on("resize", reposition);
    parent.on("move", reposition);
    // Electron does NOT auto-close child windows when their parent closes.
    const onParentClosed = () => {
      if (!banner.isDestroyed()) banner.destroy();
    };
    parent.on("closed", onParentClosed);
    banner.on("closed", () => {
      banners.delete(parent);
      returnUrls.delete(parent);
      if (!parent.isDestroyed()) {
        parent.removeListener("resize", reposition);
        parent.removeListener("move", reposition);
        parent.removeListener("closed", onParentClosed);
      }
    });
    return banner;
  }

  /** Make the banner visible. The page is static — no payload to push. */
  function present(parent: Parent, banner: ReturnBannerWindow): void {
    if (parent.isDestroyed() || banner.isDestroyed()) return;
    position(parent, banner);
    // showInactive: never steal focus from the page (e.g. an SSO form).
    if (!banner.isVisible()) banner.showInactive();
    console.warn(`[omnigent] return-banner: shown (return to ${returnUrls.get(parent)})`);
  }

  /**
   * Show the banner on a shell window, offering to return to ``returnUrl``.
   *
   * @param returnUrl Full URL to load on "Go back" (subpage, mount path, and
   *   query args preserved). No-op when null.
   */
  function show(parent: Parent | null | undefined, returnUrl: string | null | undefined): void {
    if (!parent || parent.isDestroyed()) return;
    if (!returnUrl) {
      console.warn("[omnigent] return-banner: NOT shown — no return URL recorded");
      return;
    }
    returnUrls.set(parent, returnUrl);
    present(parent, ensureBanner(parent));
  }

  /** Hide the banner (idempotent); it re-appears on the next ``show``. */
  function hide(parent: Parent): void {
    const banner = banners.get(parent);
    if (banner && !banner.isDestroyed() && banner.isVisible()) banner.hide();
  }

  function registerIpc() {
    ipcMain.on("omnigent:return-banner-go-back", (event) => {
      const banner = bannerForSender(event);
      if (!banner) return;
      const parent = parentOf(banner);
      const target = parent && returnUrls.get(parent);
      if (!parent || parent.isDestroyed() || !target) return;
      hide(parent);
      // Re-arm before navigating: the return load can land on SSO again
      // (expired session) and that away episode must notify afresh.
      onGoBack?.(parent);
      void parent.loadURL(target).catch(() => {});
    });
    ipcMain.on("omnigent:return-banner-dismiss", (event) => {
      const banner = bannerForSender(event);
      if (!banner) return;
      const parent = parentOf(banner);
      if (parent) hide(parent);
    });
  }

  return { ensureBanner, show, hide, registerIpc };
}
