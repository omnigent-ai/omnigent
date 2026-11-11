// Shell-owned About window.
//
// The connected app UI is supplied by the server, so product/version details
// live in this bundled Electron page instead. Its narrow preload can only read
// local version information and trigger fixed-purpose update checks.

import type { BrowserWindowConstructorOptions } from "electron";
import { pathToFileURL } from "node:url";

import type { DesktopUpdater } from "./desktop_updater.ts";

export const ABOUT_WIDTH = 760;
export const ABOUT_HEIGHT = 480;

/** The parts of a NativeImage the About page's icon comes from. */
export interface AppIconImage {
  isEmpty: () => boolean;
  toDataURL: (options: { scaleFactor: number }) => string;
}

export interface AppIconSources {
  app: {
    isPackaged: boolean;
    getFileIcon: (path: string, options: { size: "large" }) => Promise<AppIconImage>;
  };
  nativeImage: {
    createFromNamedImage: (imageName: string) => AppIconImage;
    createFromPath: (path: string) => AppIconImage;
  };
  platform?: NodeJS.Platform;
  executablePath?: string;
  fallbackIconPath: string;
}

/** The parts of a shell window the About modal attaches to. */
export interface AboutParentWindow {
  isDestroyed: () => boolean;
  on: (event: "closed", listener: () => void) => unknown;
  removeListener: (event: "closed", listener: () => void) => unknown;
}

/** The parts of the About BrowserWindow this module drives. */
export interface AboutWindowHandle {
  excludedFromShownWindowsMenu?: boolean;
  webContents: {
    setWindowOpenHandler: (handler: () => { action: "deny" }) => void;
    on: (
      event: "will-navigate",
      listener: (event: { preventDefault: () => void }, url: string) => void,
    ) => unknown;
  };
  isDestroyed: () => boolean;
  isMinimized: () => boolean;
  restore: () => void;
  show: () => void;
  focus: () => void;
  close: () => void;
  destroy: () => void;
  loadFile: (filePath: string) => Promise<void>;
  once: (event: "ready-to-show", listener: () => void) => unknown;
  on: (event: "closed", listener: () => void) => unknown;
}

export type AboutWindowOptions<Parent> = Omit<BrowserWindowConstructorOptions, "parent"> & {
  parent?: Parent;
};

/** The parts of an IPC event used to recognize the About page. */
export interface AboutIpcEvent {
  sender: { getURL?: () => string } | null | undefined;
  senderFrame?: { url?: string } | null;
}

export interface AboutWindowDeps<
  Parent extends AboutParentWindow,
  About extends AboutWindowHandle,
> {
  BrowserWindow: new (options: AboutWindowOptions<Parent>) => About;
  ipcMain: {
    handle: (channel: string, listener: (event: AboutIpcEvent) => unknown) => void;
    on: (channel: string, listener: (event: AboutIpcEvent) => void) => unknown;
  };
  nativeTheme: { shouldUseDarkColors: boolean };
  updater: Pick<
    DesktopUpdater,
    "getStatus" | "checkForUpdates" | "downloadUpdate" | "installUpdateNow"
  >;
  getDesktopVersion: () => string;
  getAppIconDataUrl: () => Promise<string | null>;
  getCliStatus: () => Promise<unknown>;
  onDesktopDownloadStarted?: (parent: Parent) => void;
  onClosed?: (parent: Parent) => void;
  /** Absolute path to the bundled About HTML. */
  aboutPage: string;
  /** Absolute path to about_preload.js. */
  preloadPath: string;
  platform?: NodeJS.Platform;
}

export interface AboutWindow<Parent, About> {
  open: (nextParent?: Parent | null) => About;
  close: () => void;
  registerIpc: () => void;
  senderIsAbout: (event: AboutIpcEvent) => boolean;
}

export function platformDisplayName(platform: NodeJS.Platform): string {
  if (platform === "darwin") return "macOS";
  if (platform === "win32") return "Windows";
  if (platform === "linux") return "Linux";
  return "Desktop";
}

/** Resolve the current platform app icon, with the bundled PNG as fallback. */
export async function resolveAppIconDataUrl({
  app,
  nativeImage,
  platform = process.platform,
  executablePath = process.execPath,
  fallbackIconPath,
}: AppIconSources): Promise<string | null> {
  const candidates: (() => AppIconImage | Promise<AppIconImage>)[] = [];
  if (app.isPackaged && platform === "darwin") {
    candidates.push(() => nativeImage.createFromNamedImage("NSApplicationIcon"));
    candidates.push(() => app.getFileIcon(executablePath, { size: "large" }));
  } else if (app.isPackaged && platform === "win32") {
    candidates.push(() => app.getFileIcon(executablePath, { size: "large" }));
  }
  candidates.push(() => nativeImage.createFromPath(fallbackIconPath));

  async function resolveCandidate(index: number): Promise<string | null> {
    if (index >= candidates.length) return null;
    try {
      const image = await candidates[index]!();
      if (image && !image.isEmpty()) return image.toDataURL({ scaleFactor: 2 });
    } catch {
      // Try the next source. Native icon lookup can fail on older platforms.
    }
    return resolveCandidate(index + 1);
  }

  return resolveCandidate(0);
}

export function createAboutWindow<
  Parent extends AboutParentWindow,
  About extends AboutWindowHandle,
>({
  BrowserWindow,
  ipcMain,
  nativeTheme,
  updater,
  getDesktopVersion,
  getAppIconDataUrl,
  getCliStatus,
  onDesktopDownloadStarted = () => {},
  onClosed = () => {},
  aboutPage,
  preloadPath,
  platform = process.platform,
}: AboutWindowDeps<Parent, About>): AboutWindow<Parent, About> {
  const aboutPageUrl = pathToFileURL(aboutPage);
  let about: About | null = null;
  let parent: Parent | null = null;

  function senderIsAbout(event: AboutIpcEvent): boolean {
    if (!about || about.isDestroyed() || event.sender !== about.webContents) return false;
    const frameUrl = event.senderFrame?.url ?? event.sender?.getURL?.() ?? "";
    try {
      const url = new URL(frameUrl);
      return url.protocol === "file:" && url.pathname === aboutPageUrl.pathname;
    } catch {
      return false;
    }
  }

  function guard(event: AboutIpcEvent): void {
    if (!senderIsAbout(event)) {
      throw new Error("About IPC is only available to the bundled About window");
    }
  }

  function closeAbout() {
    if (about && !about.isDestroyed()) about.close();
  }

  function open(nextParent: Parent | null = null): About {
    const validParent = nextParent && !nextParent.isDestroyed() ? nextParent : null;
    if (about && !about.isDestroyed()) {
      if (parent === validParent) {
        if (about.isMinimized()) about.restore();
        about.show();
        about.focus();
        return about;
      }
      about.destroy();
    }

    parent = validParent;
    about = new BrowserWindow({
      ...(parent ? { parent } : {}),
      modal: Boolean(parent),
      title: "About Omnigent",
      width: ABOUT_WIDTH,
      height: ABOUT_HEIGHT,
      minWidth: 640,
      minHeight: 420,
      resizable: true,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      show: false,
      backgroundColor: nativeTheme.shouldUseDarkColors ? "#151515" : "#ffffff",
      webPreferences: {
        preload: preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    if (platform === "darwin") about.excludedFromShownWindowsMenu = true;

    about.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    about.webContents.on("will-navigate", (event, targetUrl) => {
      if (targetUrl !== aboutPageUrl.href) event.preventDefault();
    });

    const opened = about;
    const openedParent = parent;
    const onParentClosed = () => {
      if (!opened.isDestroyed()) opened.destroy();
    };
    openedParent?.on("closed", onParentClosed);
    opened.once("ready-to-show", () => {
      if (!opened.isDestroyed()) opened.show();
    });
    opened.on("closed", () => {
      if (openedParent && !openedParent.isDestroyed()) {
        openedParent.removeListener("closed", onParentClosed);
      }
      if (openedParent) onClosed(openedParent);
      if (about === opened) {
        about = null;
        parent = null;
      }
    });
    void opened.loadFile(aboutPage);
    return opened;
  }

  function registerIpc() {
    ipcMain.handle("omnigent:about-get-info", async (event) => {
      guard(event);
      const [appIconDataUrl, cli] = await Promise.all([getAppIconDataUrl(), getCliStatus()]);
      return {
        desktopVersion: getDesktopVersion(),
        platformName: platformDisplayName(platform),
        appIconDataUrl,
        cli,
      };
    });

    ipcMain.handle("omnigent:about-get-desktop-update-status", (event) => {
      guard(event);
      return updater.getStatus();
    });

    ipcMain.handle("omnigent:about-check-desktop", async (event) => {
      guard(event);
      await updater.checkForUpdates({ manual: true });
      return updater.getStatus();
    });

    ipcMain.handle("omnigent:about-download-desktop-update", async (event) => {
      guard(event);
      if (parent && !parent.isDestroyed()) onDesktopDownloadStarted(parent);
      await updater.downloadUpdate();
    });

    ipcMain.handle("omnigent:about-install-desktop-update", (event) => {
      guard(event);
      if (!updater.installUpdateNow()) {
        throw new Error("No downloaded update is ready to install.");
      }
    });

    ipcMain.on("omnigent:about-close", (event) => {
      if (senderIsAbout(event)) closeAbout();
    });
  }

  return { open, close: closeAbout, registerIpc, senderIsAbout };
}
