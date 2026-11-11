import type { BrowserWindowConstructorOptions, Rectangle } from "electron";
import { pathToFileURL } from "node:url";
import type { BrowserPermissionChoice } from "./ipc/bridge.ts";

export type { BrowserPermissionChoice };

export const PROMPT_WIDTH = 380;
export const PROMPT_HEIGHT = 250;

const CHOICES: ReadonlySet<string> = new Set(["allow-once", "always-allow", "deny", "dismiss"]);

function isBrowserPermissionChoice(value: unknown): value is BrowserPermissionChoice {
  return typeof value === "string" && CHOICES.has(value);
}

type ParentWindowEvent = "closed" | "hide" | "minimize" | "move" | "resize";
type NavigationListener = (
  event: unknown,
  url: string,
  isInPlace: boolean,
  isMainFrame: boolean,
) => void;
type PreventableListener = (event: { preventDefault: () => void }) => void;

/** The shell window the prompt is anchored to. */
export interface PermissionPromptParent {
  isDestroyed: () => boolean;
  getContentBounds: () => Rectangle;
  on: (event: ParentWindowEvent, listener: () => void) => unknown;
  removeListener: (event: ParentWindowEvent, listener: () => void) => unknown;
  webContents: {
    isDestroyed: () => boolean;
    on: (event: "did-start-navigation", listener: NavigationListener) => unknown;
    removeListener: (event: "did-start-navigation", listener: NavigationListener) => unknown;
  };
}

/** The parts of the prompt's own BrowserWindow this module drives. */
export interface PermissionPromptWindow {
  excludedFromShownWindowsMenu?: boolean;
  isDestroyed: () => boolean;
  destroy: () => void;
  show: () => void;
  setBounds: (bounds: Rectangle) => void;
  loadFile: (filePath: string) => Promise<unknown>;
  on: (event: "closed" | "blur", listener: () => void) => unknown;
  once: (event: "ready-to-show", listener: () => void) => unknown;
  webContents: {
    mainFrame: unknown;
    on: ((event: "render-process-gone", listener: () => void) => unknown) &
      ((event: "will-navigate" | "will-frame-navigate", listener: PreventableListener) => unknown);
    setWindowOpenHandler: (handler: () => { action: "deny" }) => void;
    session: {
      setPermissionRequestHandler: (
        handler: (
          contents: unknown,
          permission: string,
          callback: (granted: boolean) => void,
        ) => void,
      ) => void;
      setPermissionCheckHandler: (handler: () => boolean) => void;
    };
  };
}

export type PermissionPromptWindowOptions<Parent> = Omit<
  BrowserWindowConstructorOptions,
  "parent"
> & { parent: Parent };

/** The parts of an IPC event used to recognize the bundled prompt page. */
export interface PermissionPromptIpcEvent {
  sender: unknown;
  senderFrame?: { url?: string } | null;
}

export interface BrowserPermissionPromptDeps<
  Parent extends PermissionPromptParent,
  Popup extends PermissionPromptWindow,
> {
  BrowserWindow: new (options: PermissionPromptWindowOptions<Parent>) => Popup;
  ipcMain: {
    handle: (
      channel: string,
      listener: (event: PermissionPromptIpcEvent, ...args: unknown[]) => unknown,
    ) => void;
  };
  /** Absolute path to the prompt page's HTML. */
  promptPage: string;
  preloadPath: string;
}

export interface PermissionPromptOptions<Parent> {
  parent: Parent | null | undefined;
  origin: string;
  reload: boolean;
  getAnchorBounds: () => { x: number; y: number };
  signal: AbortSignal;
}

export interface BrowserPermissionPrompt<Parent> {
  show: (options: PermissionPromptOptions<Parent>) => Promise<BrowserPermissionChoice>;
  dismiss: (parent: Parent) => void;
  registerIpc: () => void;
}

const CLOSING_EVENTS = ["closed", "hide", "minimize"] as const;
const MOVING_EVENTS = ["move", "resize"] as const;

interface OpenPrompt<Popup> {
  window: Popup;
  origin: string;
  reload: boolean;
  finish: (choice: BrowserPermissionChoice) => void;
}

export function promptBounds(content: Rectangle, anchor: { x: number; y: number }): Rectangle {
  const width = Math.min(PROMPT_WIDTH, content.width);
  const height = Math.min(PROMPT_HEIGHT, content.height);
  return {
    x: Math.round(content.x + Math.max(0, Math.min(anchor.x + 88, content.width - width))),
    y: Math.round(content.y + Math.max(0, Math.min(anchor.y - 4, content.height - height))),
    width,
    height,
  };
}

/** A trusted child window keeps consent outside both the site and server SPA. */
export function createBrowserPermissionPrompt<
  Parent extends PermissionPromptParent,
  Popup extends PermissionPromptWindow,
>({
  BrowserWindow,
  ipcMain,
  promptPage,
  preloadPath,
}: BrowserPermissionPromptDeps<Parent, Popup>): BrowserPermissionPrompt<Parent> {
  const pageUrl = pathToFileURL(promptPage).href;
  const prompts = new Map<Parent, OpenPrompt<Popup>>();

  function forSender(event: PermissionPromptIpcEvent): OpenPrompt<Popup> {
    for (const request of prompts.values()) {
      const wc = request.window.webContents;
      if (
        !request.window.isDestroyed() &&
        event.sender === wc &&
        event.senderFrame === wc.mainFrame &&
        event.senderFrame?.url === pageUrl
      )
        return request;
    }
    throw new Error("Permission choices require the bundled permission UI");
  }

  function dismiss(parent: Parent): void {
    prompts.get(parent)?.finish("dismiss");
  }

  function show({
    parent,
    origin,
    reload,
    getAnchorBounds,
    signal,
  }: PermissionPromptOptions<Parent>): Promise<BrowserPermissionChoice> {
    if (!parent || parent.isDestroyed() || signal.aborted) return Promise.resolve("dismiss");
    dismiss(parent);
    return new Promise<BrowserPermissionChoice>((resolve) => {
      const popup = new BrowserWindow({
        parent,
        frame: false,
        transparent: true,
        hasShadow: false,
        resizable: false,
        movable: false,
        minimizable: false,
        maximizable: false,
        fullscreenable: false,
        skipTaskbar: true,
        hiddenInMissionControl: true,
        show: false,
        width: PROMPT_WIDTH,
        height: PROMPT_HEIGHT,
        webPreferences: {
          preload: preloadPath,
          partition: "omnigent-browser-permission-ui",
          nodeIntegration: false,
          contextIsolation: true,
          sandbox: true,
          devTools: false,
        },
      });
      if (process.platform === "darwin") popup.excludedFromShownWindowsMenu = true;
      let finished = false;
      const close = () => finish("dismiss");
      const position = () => {
        if (!parent.isDestroyed() && !popup.isDestroyed()) {
          popup.setBounds(promptBounds(parent.getContentBounds(), getAnchorBounds()));
        }
      };
      const onNavigation: NavigationListener = (_event, _url, isInPlace, isMainFrame) => {
        if (isMainFrame && !isInPlace) close();
      };
      const finish = (choice: BrowserPermissionChoice) => {
        if (finished) return;
        finished = true;
        if (prompts.get(parent)?.window === popup) prompts.delete(parent);
        signal.removeEventListener("abort", close);
        for (const event of CLOSING_EVENTS) parent.removeListener(event, close);
        for (const event of MOVING_EVENTS) parent.removeListener(event, position);
        if (!parent.webContents.isDestroyed()) {
          parent.webContents.removeListener("did-start-navigation", onNavigation);
        }
        if (!popup.isDestroyed()) popup.destroy();
        resolve(choice);
      };
      prompts.set(parent, { window: popup, origin, reload, finish });
      signal.addEventListener("abort", close, { once: true });
      for (const event of CLOSING_EVENTS) parent.on(event, close);
      for (const event of MOVING_EVENTS) parent.on(event, position);
      parent.webContents.on("did-start-navigation", onNavigation);
      popup.on("closed", close);
      popup.on("blur", close);
      popup.webContents.on("render-process-gone", close);
      popup.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      popup.webContents.on("will-navigate", (event) => event.preventDefault());
      popup.webContents.on("will-frame-navigate", (event) => event.preventDefault());
      popup.webContents.session.setPermissionRequestHandler((_wc, _permission, cb) => cb(false));
      popup.webContents.session.setPermissionCheckHandler(() => false);
      popup.once("ready-to-show", () => {
        if (finished) return;
        position();
        popup.show();
      });
      void popup.loadFile(promptPage).catch(close);
    });
  }

  function registerIpc() {
    ipcMain.handle("omnigent:browser-permission-info", (event) => {
      const { origin, reload } = forSender(event);
      return { origin, reload };
    });
    ipcMain.handle("omnigent:browser-permission-choose", (event, choice) => {
      const request = forSender(event);
      if (!isBrowserPermissionChoice(choice)) throw new Error("Invalid permission choice");
      request.finish(choice);
    });
  }

  return { show, dismiss, registerIpc };
}
