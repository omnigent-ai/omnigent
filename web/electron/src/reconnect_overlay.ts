// Shell-owned "Reconnecting to Databricks…" overlay.
//
// While a Databricks workspace is briefly unreachable (VPN reconnecting after
// wake), the window keeps its page and this overlay dims it behind a spinner
// card with a Cancel button. It is a transparent WebContentsView on top of the
// window's content: it moves with the window, covers browser panes and blocks
// input to the page underneath. The page is bundled and its preload is narrow;
// IPC is verified by sender, since the page underneath may be untrusted.

import type { Rectangle, WebContentsViewConstructorOptions } from "electron";
import { pathToFileURL } from "node:url";

/** The parts of the overlay's WebContentsView this module drives. */
export interface ReconnectOverlayView {
  webContents: {
    isDestroyed: () => boolean;
    close: () => void;
    focus: () => void;
    setWindowOpenHandler: (handler: () => { action: "deny" }) => void;
    on: (
      event: "will-navigate",
      listener: (event: { preventDefault: () => void }) => void,
    ) => unknown;
    loadFile: (filePath: string, options: { query: Record<string, string> }) => Promise<void>;
  };
  setBackgroundColor: (color: string) => void;
  setBounds: (bounds: Rectangle) => void;
}

/** The parts of a shell window the overlay covers. */
export interface ReconnectOverlayParent {
  isDestroyed: () => boolean;
  getContentSize: () => number[];
  on: (event: "resize", listener: () => void) => unknown;
  once: (event: "closed", listener: () => void) => unknown;
  contentView: {
    addChildView: (view: ReconnectOverlayView) => void;
    removeChildView: (view: ReconnectOverlayView) => void;
  };
  isFocused?: () => boolean;
  webContents: { focus: () => void };
}

/** The parts of an IPC event used to recognize the overlay page. */
export interface ReconnectOverlayIpcEvent {
  sender: unknown;
  senderFrame?: { url?: string } | null;
}

export interface ReconnectOverlayDeps<Parent extends ReconnectOverlayParent> {
  WebContentsView: new (options: WebContentsViewConstructorOptions) => ReconnectOverlayView;
  ipcMain: {
    on: (channel: string, listener: (event: ReconnectOverlayIpcEvent) => void) => unknown;
  };
  /** Absolute path to the bundled overlay HTML. */
  overlayPage: string;
  /** Absolute path to reconnect_overlay_preload.js. */
  preloadPath: string;
  onCancel: (parent: Parent) => void;
}

export interface ReconnectOverlay<Parent> {
  show: (parent: Parent | null | undefined, hint: string) => void;
  hide: (parent: Parent) => void;
  isShown: (parent: Parent) => boolean;
  raise: (parent: Parent) => void;
  registerIpc: () => void;
}

interface OverlayEntry {
  view: ReconnectOverlayView;
  hint: string | null;
  shown: boolean;
  fit: () => void;
}

export function createReconnectOverlay<Parent extends ReconnectOverlayParent>({
  WebContentsView,
  ipcMain,
  overlayPage,
  preloadPath,
  onCancel,
}: ReconnectOverlayDeps<Parent>): ReconnectOverlay<Parent> {
  const pageUrl = pathToFileURL(overlayPage).href;
  const overlays = new Map<Parent, OverlayEntry>();

  function parentForSender(event: ReconnectOverlayIpcEvent): Parent | null {
    for (const [parent, overlay] of overlays) {
      const wc = overlay.view.webContents;
      if (event.sender === wc && event.senderFrame?.url?.split("?")[0] === pageUrl) return parent;
    }
    return null;
  }

  function ensureOverlay(parent: Parent): OverlayEntry {
    const existing = overlays.get(parent);
    if (existing) return existing;
    const view = new WebContentsView({
      webPreferences: {
        preload: preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        devTools: false,
      },
    });
    view.setBackgroundColor("#00000000");
    view.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    view.webContents.on("will-navigate", (event) => event.preventDefault());
    const overlay: OverlayEntry = { view, hint: null, shown: false, fit: () => {} };
    overlay.fit = () => {
      if (parent.isDestroyed()) return;
      const [width = 0, height = 0] = parent.getContentSize();
      view.setBounds({ x: 0, y: 0, width, height });
    };
    parent.on("resize", overlay.fit);
    parent.once("closed", () => {
      overlays.delete(parent);
      if (!view.webContents.isDestroyed()) view.webContents.close();
    });
    overlays.set(parent, overlay);
    return overlay;
  }

  /** Show (or update) the overlay over a window's current page. */
  function show(parent: Parent | null | undefined, hint: string): void {
    if (!parent || parent.isDestroyed()) return;
    const overlay = ensureOverlay(parent);
    if (overlay.hint !== hint) {
      overlay.hint = hint;
      void overlay.view.webContents.loadFile(overlayPage, { query: { hint } }).catch(() => {});
    }
    overlay.fit();
    // Re-adding moves the view to the top, above any browser pane.
    parent.contentView.addChildView(overlay.view);
    overlay.shown = true;
    overlay.view.webContents.focus();
  }

  /** Hide the overlay (idempotent); the view is kept for the next show. */
  function hide(parent: Parent): void {
    const overlay = overlays.get(parent);
    if (!overlay?.shown || parent.isDestroyed()) return;
    overlay.shown = false;
    parent.contentView.removeChildView(overlay.view);
    // Hand keyboard focus back to the page the overlay covered.
    if (parent.isFocused?.()) parent.webContents.focus();
  }

  function isShown(parent: Parent): boolean {
    return Boolean(overlays.get(parent)?.shown);
  }

  /** Keep the overlay above a view attached while it is shown. */
  function raise(parent: Parent): void {
    const overlay = overlays.get(parent);
    if (overlay?.shown && !parent.isDestroyed()) parent.contentView.addChildView(overlay.view);
  }

  function registerIpc() {
    ipcMain.on("omnigent:reconnect-overlay-cancel", (event) => {
      const parent = parentForSender(event);
      if (!parent || parent.isDestroyed() || !isShown(parent)) return;
      onCancel(parent);
    });
  }

  return { show, hide, isShown, raise, registerIpc };
}
