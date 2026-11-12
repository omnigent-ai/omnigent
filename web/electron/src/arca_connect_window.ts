/**
 * The Arca connect console: a modal window owned by the shell, used in place of
 * a native consent dialog. It shows the exact `arca ssh` command that will run
 * and, after the user confirms, streams its live stdout/stderr into an embedded
 * terminal pane.
 *
 * Trust model: this window is created by the main process and loads a bundled
 * file:// page with its own narrow preload. The server-served page can neither
 * draw over it, forge a click in it, nor reach its IPC channels, so a Confirm
 * click here carries the same authority the native dialog had (see
 * confirmHostEnrollment for why consent must live outside the server's page).
 * Confirm/cancel messages are accepted only from this window's own webContents.
 *
 * All Electron surfaces are injected so the flow is unit-testable.
 */

import type { BrowserWindowConstructorOptions } from "electron";

import type { ArcaErrorKind } from "./arca.ts";
import { arcaTarget, type ArcaIdentity } from "./arcaIdentity.ts";
import type { PushChannels } from "./ipc/contract.ts";
import { on, push, type IpcMainOnPort, type PushTarget } from "./ipc/main.ts";

/** Channels between this window's preload and main. */
export const CONFIRM_CHANNEL = "arca-connect:confirm";
export const CANCEL_CHANNEL = "arca-connect:cancel";
export const RESIZE_CHANNEL = "arca-connect:resize";
export const CONTENT_WIDTH = 720;
export const MIN_CONTENT_HEIGHT = 300;
export const RUNNING_CONTENT_HEIGHT = 500;
export const MAX_CONTENT_HEIGHT = 640;

/** Pushes this module sends to its console. */
type ArcaConnectPushChannel = Extract<keyof PushChannels, `arca-connect:${string}`>;

/** A property of an untrusted value, read the way `value?.[key]` would. */
function propertyOf(value: unknown, key: string): unknown {
  return value == null ? undefined : (value as Record<string, unknown>)[key];
}

/** How a connect or sign-in ended, as far as the console reads it. */
export interface ArcaConnectRunResult {
  ok: boolean;
  error?: string | undefined;
  errorKind?: ArcaErrorKind | undefined;
  authError?: boolean | undefined;
  canceled?: boolean | undefined;
  alreadyRunning?: boolean | undefined;
  identity?: ArcaIdentity | undefined;
}

/** What a console run resolves with: the command's own result after a confirmed run. */
export interface ArcaConnectFlowResult extends ArcaConnectRunResult {
  /** The outcome was already shown in the console's terminal. */
  shownInConsole?: boolean;
}

/** A connect or sign-in in flight. */
export interface ArcaConnectConsoleRun {
  promise: Promise<ArcaConnectRunResult>;
  cancel: () => void;
}

/** Why the console opened; changes its title and copy. */
export type ArcaConnectReason = "preview-reconnect";

export interface ArcaConnectRunOptions {
  reason?: ArcaConnectReason | undefined;
}

/** The parts of an IPC event the console's handlers read. */
export interface ArcaConnectIpcEvent {
  sender: { id: number };
}

/** The parts of the console's BrowserWindow this module drives. */
export interface ArcaConnectConsoleWindow {
  webContents: PushTarget & {
    id: number;
    once: (event: "did-finish-load", listener: () => void) => unknown;
  };
  isDestroyed: () => boolean;
  show: () => void;
  focus: () => void;
  close: () => void;
  setTitle?: ((title: string) => void) | undefined;
  setContentSize: (width: number, height: number, animate: boolean) => void;
  loadFile: (filePath: string) => Promise<void>;
  on: (event: "closed", listener: () => void) => unknown;
  once: (event: "ready-to-show", listener: () => void) => unknown;
}

export type ArcaConnectWindowOptions<Parent> = Omit<BrowserWindowConstructorOptions, "parent"> & {
  parent: Parent | undefined;
};

export interface ArcaConnectFlowDeps<Parent> {
  BrowserWindow: new (options: ArcaConnectWindowOptions<Parent>) => ArcaConnectConsoleWindow;
  ipcMain: IpcMainOnPort<ArcaConnectIpcEvent>;
  pagePath: string;
  preloadPath: string;
  startConnect: (serverUrl: string, onOutput: (text: string) => void) => ArcaConnectConsoleRun;
  commandLine: (serverUrl: string) => string;
  startLogin: (serverUrl: string) => ArcaConnectConsoleRun;
  loginCommandLine: (serverUrl: string) => string;
  log?: ((message: string) => void) | undefined;
}

export interface ArcaConnectFlow<Parent> {
  run: (
    parentWin: Parent | null | undefined,
    serverUrl: string,
    options?: ArcaConnectRunOptions,
  ) => Promise<ArcaConnectFlowResult>;
}

type ConsolePhase = "asking" | "running" | "done";

/** One open console, keyed by its webContents id. */
interface ConsoleState {
  win: ArcaConnectConsoleWindow;
  phase: () => ConsolePhase;
  onConfirm: () => void;
  onCancel: () => void;
}

/** The in-flight run's window and shared outcome. */
interface ActiveRun {
  win: ArcaConnectConsoleWindow;
  promise: Promise<ArcaConnectFlowResult> | null;
  target: string | null;
  updateReason: (reason: ArcaConnectReason | undefined) => void;
}

export function createArcaConnectFlow<Parent>({
  BrowserWindow,
  ipcMain,
  pagePath,
  preloadPath,
  startConnect,
  commandLine,
  startLogin,
  loginCommandLine,
  log = () => {},
}: ArcaConnectFlowDeps<Parent>): ArcaConnectFlow<Parent> {
  /**
   * Button routing, keyed by the console window's webContents id and kept
   * for the window's WHOLE life (not the run's): the post-run "Close" click
   * must still route after the result promise settled. Only messages from a
   * registered console's own webContents are honored — a server page
   * (different webContents) can't spoof consent.
   */
  const consoles = new Map<number, ConsoleState>();
  /**
   * The in-flight run's window + shared outcome promise, or null. One run at
   * a time — a second run() while it exists re-attaches (focuses the window,
   * shares the outcome) instead of failing, so a refreshed SPA can always get
   * back to an in-flight connect.
   */
  let activeRun: ActiveRun | null = null;

  on(ipcMain, CONFIRM_CHANNEL, (event) => {
    consoles.get(event.sender.id)?.onConfirm();
  });
  on(ipcMain, CANCEL_CHANNEL, (event) => {
    consoles.get(event.sender.id)?.onCancel();
  });
  on(ipcMain, RESIZE_CHANNEL, (event, payload) => {
    const consoleState = consoles.get(event.sender.id);
    const measured = Number(propertyOf(payload, "height"));
    if (!consoleState || !Number.isFinite(measured) || measured <= 0) return;
    const consoleWindow = consoleState.win;
    const minimum =
      consoleState.phase() === "running" ? RUNNING_CONTENT_HEIGHT : MIN_CONTENT_HEIGHT;
    const height = Math.min(MAX_CONTENT_HEIGHT, Math.max(minimum, Math.ceil(measured)));
    try {
      if (!consoleWindow.isDestroyed()) {
        consoleWindow.setContentSize(CONTENT_WIDTH, height, true);
      }
    } catch {
      // Window mid-teardown; content sizing is best-effort.
    }
  });

  /**
   * Open the console over `parentWin` and drive one connect to `serverUrl`.
   * Resolves with the connect result: `{ ok: false }` (not approved) when the
   * user cancels or closes before confirming; the command's own result after
   * a confirmed run. The window stays open after a run so the user can read
   * the output; the promise resolves as soon as the command settles.
   */
  function run(
    parentWin: Parent | null | undefined,
    serverUrl: string,
    options: ArcaConnectRunOptions = {},
  ): Promise<ArcaConnectFlowResult> {
    // Re-attach: an in-flight console survives an SPA refresh (it's a shell
    // window), so a repeat click surfaces it and awaits the same outcome
    // rather than erroring with "already in progress".
    if (activeRun) {
      if (activeRun.target !== arcaTarget(serverUrl)) {
        return Promise.resolve({
          ok: false,
          error: "An Arca connection to another server is in progress. Try again when it finishes.",
        });
      }
      try {
        if (!activeRun.win.isDestroyed()) {
          activeRun.updateReason(options.reason);
          activeRun.win.show();
          activeRun.win.focus();
        }
      } catch {
        // Window mid-teardown; the shared promise still settles.
      }
      // Set right after the run's promise executor returned.
      return activeRun.promise!;
    }
    const promise = new Promise<ArcaConnectFlowResult>((resolve) => {
      // Opens as a compact consent card; grows on Confirm when the page
      // reveals its terminal pane (see index.html's data-phase styling).
      const win = new BrowserWindow({
        parent: parentWin ?? undefined,
        modal: true,
        width: CONTENT_WIDTH,
        height: MIN_CONTENT_HEIGHT,
        useContentSize: true,
        minimizable: false,
        maximizable: false,
        fullscreenable: false,
        resizable: false,
        show: false,
        title: options.reason === "preview-reconnect" ? "Reconnect Arca" : "Connect Arca",
        webPreferences: {
          preload: preloadPath,
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
        },
      });

      // Captured now: webContents is unreadable after the window is destroyed,
      // and the closed handler needs the id to unregister the console.
      const consoleId = win.webContents.id;
      let phase: ConsolePhase = "asking";
      let loginAttempted = false;
      let connect: ArcaConnectConsoleRun | null = null;
      let settled = false;
      const sendInit = (reason: ArcaConnectReason | undefined) => {
        try {
          if (win.isDestroyed()) return;
          win.setTitle?.(reason === "preview-reconnect" ? "Reconnect Arca" : "Connect Arca");
          push(win.webContents, "arca-connect:init", {
            serverUrl,
            command: commandLine(serverUrl),
            reason: reason ?? null,
          });
        } catch {
          // Window torn down mid-send; the close handler settles the flow.
        }
      };
      const settle = (result: ArcaConnectFlowResult) => {
        if (settled) return;
        settled = true;
        activeRun = null;
        resolve(result);
      };

      const send = <Channel extends ArcaConnectPushChannel>(
        channel: Channel,
        ...args: PushChannels[Channel]["args"]
      ) => {
        try {
          if (!win.isDestroyed()) push(win.webContents, channel, ...args);
        } catch {
          // Window torn down mid-send; the close handler settles the flow.
        }
      };

      const runCommand = (login = false) => {
        phase = "running";
        const started = login
          ? startLogin(serverUrl)
          : startConnect(serverUrl, (text) => send("arca-connect:output", text));
        connect = started;
        send("arca-connect:started", {
          login,
          command: login ? loginCommandLine(serverUrl) : commandLine(serverUrl),
        });
        void started.promise.then((result) => {
          // Closing the console must not start the next command after a late exit.
          if (settled) return;
          if (login && result.ok) {
            runCommand();
            return;
          }
          // The user's Connect click covers sign-in, so it starts without its own prompt.
          if (!login && result.authError && !loginAttempted) {
            loginAttempted = true;
            log(`arca connect: ${serverUrl} needs sign-in; starting it`);
            runCommand(true);
            return;
          }
          phase = "done";
          send("arca-connect:done", {
            ok: result.ok === true,
            error: result.ok === true ? null : (result.error ?? "Connecting to Arca failed."),
          });
          settle({ ...result, shownInConsole: true });
        });
      };

      consoles.set(consoleId, {
        win,
        phase: () => phase,
        onConfirm: () => {
          if (phase !== "asking") return;
          log(`arca connect: user confirmed; running against ${serverUrl}`);
          runCommand();
        },
        onCancel: () => {
          // Cancel button (or Close after a run) — just close; the close
          // handler owns settling so both paths behave identically.
          try {
            win.close();
          } catch {
            // Already closing.
          }
        },
      });

      win.on("closed", () => {
        consoles.delete(consoleId);
        if (phase === "running" && connect) {
          // Closing mid-run kills the command: an unwatched enrollment must
          // not keep executing after its console is gone.
          log("arca connect: console closed mid-run; canceling");
          connect.cancel();
          // The canceled command's exit settles via the promise above; as a
          // belt-and-brace, settle here too (settle() is idempotent).
          settle({ ok: false, canceled: true, error: "Connecting to Arca was canceled." });
          return;
        }
        // Declining the console is a deliberate user choice, not a failure —
        // `canceled` tells the picker to stay silent instead of rendering a
        // red error with a retry.
        settle({ ok: false, canceled: true, error: "Connecting Arca wasn't approved." });
      });

      // did-finish-load fires after the page's top-level script ran, so its
      // IPC listeners exist before init arrives (ready-to-show can race them).
      win.webContents.once("did-finish-load", () => {
        sendInit(options.reason);
      });
      win.once("ready-to-show", () => {
        win.show();
      });
      void win.loadFile(pagePath);
      activeRun = {
        win,
        promise: null,
        target: arcaTarget(serverUrl),
        updateReason: (reason) => {
          if (phase === "asking") sendInit(reason);
        },
      };
    });
    // The executor set it unless it threw; the cast undoes TS's narrowing to null above.
    const started = activeRun as ActiveRun | null;
    if (started) started.promise = promise;
    return promise;
  }

  return { run };
}
