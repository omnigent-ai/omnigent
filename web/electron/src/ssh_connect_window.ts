/**
 * The SSH connect console: a SHELL-OWNED modal window that asks consent with
 * the exact commands, then shows progress, ssh's prompts (password, passphrase,
 * new host key) and the install question for the remote machine.
 *
 * Same trust model as the Arca connect console (arca_connect_window.js): the
 * server page can open it but can neither draw over it nor reach its IPC
 * channels, and messages are honored only from this window's own webContents.
 *
 * All Electron surfaces are injected so the flow is unit-testable.
 */

import type { BrowserWindowConstructorOptions } from "electron";

import type { SshConnectResult, SshConnectStatus } from "./ipc/bridge.ts";
import type { PushChannels } from "./ipc/contract.ts";
import { on, push, type IpcMainOnPort, type PushTarget } from "./ipc/main.ts";

export const CONFIRM_CHANNEL = "ssh-connect:confirm";
export const CANCEL_CHANNEL = "ssh-connect:cancel";
export const ANSWER_CHANNEL = "ssh-connect:answer";
export const RESIZE_CHANNEL = "ssh-connect:resize";
export const CONTENT_WIDTH = 720;
export const MIN_CONTENT_HEIGHT = 300;
export const RUNNING_CONTENT_HEIGHT = 500;
export const MAX_CONTENT_HEIGHT = 680;

/** Pushes this module sends to its console. */
type SshConnectPushChannel = Extract<keyof PushChannels, `ssh-connect:${string}`>;

/** A property of an untrusted value, read the way `value?.[key]` would. */
function propertyOf(value: unknown, key: string): unknown {
  return value == null ? undefined : (value as Record<string, unknown>)[key];
}

/** How a connect ended, as far as the console reads it. */
export interface SshConnectRunResult {
  ok: boolean;
  error?: string | null | undefined;
  canceled?: boolean | undefined;
  alreadyRunning?: boolean | undefined;
  identity?: { serverUrl: string; hostId: string } | null | undefined;
}

/** A question for the user; `signal` aborts when ssh stops waiting for the answer. */
export interface SshConnectConsolePrompt {
  kind: string;
  text: string;
  signal?: AbortSignal | undefined;
}

/** What the console gives a connect to show progress and ask questions. */
export interface SshConnectConsoleHooks {
  onOutput: (text: string) => void;
  onStatus: (status: SshConnectStatus) => void;
  /** Resolves with the answer, or null when the user cancels or the window closes. */
  prompt: (request: SshConnectConsolePrompt) => Promise<string | null>;
  askInstall: () => Promise<boolean>;
}

/** A connect in flight. */
export interface SshConnectConsoleRun {
  promise: Promise<SshConnectRunResult>;
  cancel: () => void;
}

/** The parts of an IPC event the console's handlers read. */
export interface SshConnectIpcEvent {
  sender: { id: number };
}

/** The parts of the console's BrowserWindow this module drives. */
export interface SshConnectConsoleWindow {
  webContents: PushTarget & {
    id: number;
    once: (event: "did-finish-load", listener: () => void) => unknown;
  };
  isDestroyed: () => boolean;
  show: () => void;
  focus: () => void;
  close: () => void;
  setContentSize: (width: number, height: number, animate: boolean) => void;
  loadFile: (filePath: string) => Promise<void>;
  on: (event: "closed", listener: () => void) => unknown;
  once: (event: "ready-to-show", listener: () => void) => unknown;
}

export type SshConnectWindowOptions<Parent> = Omit<BrowserWindowConstructorOptions, "parent"> & {
  parent: Parent | undefined;
};

export interface SshConnectFlowDeps<Parent> {
  BrowserWindow: new (options: SshConnectWindowOptions<Parent>) => SshConnectConsoleWindow;
  ipcMain: IpcMainOnPort<SshConnectIpcEvent>;
  pagePath: string;
  preloadPath: string;
  startConnect: (
    serverUrl: string,
    destination: string,
    hooks: SshConnectConsoleHooks,
  ) => SshConnectConsoleRun;
  commandLines: (serverUrl: string, destination: string) => string[];
  log?: ((message: string) => void) | undefined;
}

export interface SshConnectFlow<Parent> {
  run: (
    parentWin: Parent | null | undefined,
    serverUrl: string,
    destination: string,
  ) => Promise<SshConnectResult>;
}

type ConsolePhase = "asking" | "running" | "done";

/** One open console, keyed by its webContents id. */
interface ConsoleState {
  win: SshConnectConsoleWindow;
  phase: () => ConsolePhase;
  onConfirm: () => void;
  onCancel: () => void;
  onAnswer: (payload: unknown) => void;
}

/**
 * What the server's page learns about a finished connect: app-authored fields
 * only. Remote output, including error text, stays in the shell-owned window.
 */
function pageResult(result: SshConnectRunResult): SshConnectResult {
  if (result.ok === true) {
    const identity = result.identity
      ? { serverUrl: String(result.identity.serverUrl), hostId: String(result.identity.hostId) }
      : null;
    return {
      ok: true,
      alreadyRunning: result.alreadyRunning === true,
      identity,
      shownInConsole: true,
    };
  }
  return result.canceled === true
    ? { ok: false, canceled: true, error: "Connecting was canceled.", shownInConsole: true }
    : {
        ok: false,
        error: "Connecting over SSH failed. The connect window shows why.",
        shownInConsole: true,
      };
}

export function createSshConnectFlow<Parent>({
  BrowserWindow,
  ipcMain,
  pagePath,
  preloadPath,
  startConnect,
  commandLines,
  log = () => {},
}: SshConnectFlowDeps<Parent>): SshConnectFlow<Parent> {
  /** Keyed by the console's webContents id, for the window's whole life. */
  const consoles = new Map<number, ConsoleState>();
  /** One connect at a time; a repeat for the same target re-focuses it. */
  let activeRun: {
    key: string;
    win: SshConnectConsoleWindow;
    promise: Promise<SshConnectResult>;
  } | null = null;

  on(ipcMain, CONFIRM_CHANNEL, (event) => consoles.get(event.sender.id)?.onConfirm());
  on(ipcMain, CANCEL_CHANNEL, (event) => consoles.get(event.sender.id)?.onCancel());
  on(ipcMain, ANSWER_CHANNEL, (event, payload) => consoles.get(event.sender.id)?.onAnswer(payload));
  on(ipcMain, RESIZE_CHANNEL, (event, payload) => {
    const state = consoles.get(event.sender.id);
    const measured = Number(propertyOf(payload, "height"));
    if (!state || !Number.isFinite(measured) || measured <= 0) return;
    const minimum = state.phase() === "asking" ? MIN_CONTENT_HEIGHT : RUNNING_CONTENT_HEIGHT;
    const height = Math.min(MAX_CONTENT_HEIGHT, Math.max(minimum, Math.ceil(measured)));
    try {
      if (!state.win.isDestroyed()) state.win.setContentSize(CONTENT_WIDTH, height, true);
    } catch {
      // Window mid-teardown; sizing is best-effort.
    }
  });

  /**
   * Open the console over `parentWin` and drive one connect. Resolves with
   * `{ ok: false, canceled: true }` when the user declines or closes it, else
   * with the connect result. The window stays open afterwards so the user can
   * read the outcome.
   */
  function run(
    parentWin: Parent | null | undefined,
    serverUrl: string,
    destination: string,
  ): Promise<SshConnectResult> {
    const key = `${destination}\n${serverUrl}`;
    if (activeRun) {
      if (activeRun.key !== key) {
        return Promise.resolve({
          ok: false,
          error: "Another SSH connection is in progress. Try again when it finishes.",
        });
      }
      try {
        if (!activeRun.win.isDestroyed()) {
          activeRun.win.show();
          activeRun.win.focus();
        }
      } catch {
        // Window mid-teardown; the shared promise still settles.
      }
      return activeRun.promise;
    }

    // Assigned by the executor below, which runs synchronously.
    let resolveRun!: (result: SshConnectResult) => void;
    const promise = new Promise<SshConnectResult>((resolve) => {
      resolveRun = resolve;
    });
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
      title: "Connect over SSH",
      webPreferences: {
        preload: preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    const consoleId = win.webContents.id;
    let phase: ConsolePhase = "asking";
    let connect: SshConnectConsoleRun | null = null;
    let settled = false;
    let nextPromptId = 1;
    const pending = new Map<number, (answer: string | null) => void>();

    const send = <Channel extends SshConnectPushChannel>(
      channel: Channel,
      ...args: PushChannels[Channel]["args"]
    ) => {
      try {
        if (!win.isDestroyed()) push(win.webContents, channel, ...args);
      } catch {
        // Window torn down mid-send; the close handler settles the flow.
      }
    };
    const settle = (result: SshConnectResult) => {
      if (settled) return;
      settled = true;
      activeRun = null;
      resolveRun(result);
    };
    const ask = (kind: string, text: string, signal?: AbortSignal) =>
      new Promise<string | null>((resolve) => {
        const id = nextPromptId;
        nextPromptId += 1;
        pending.set(id, resolve);
        send("ssh-connect:prompt", { id, kind, text });
        // ssh stopped waiting, so take the question down instead of leaving it up.
        signal?.addEventListener("abort", () => {
          if (!pending.delete(id)) return;
          resolve(null);
          send("ssh-connect:prompt-closed", { id });
        });
      });
    const answerAll = () => {
      for (const resolve of pending.values()) resolve(null);
      pending.clear();
    };

    const hooks: SshConnectConsoleHooks = {
      onOutput: (text) => send("ssh-connect:output", text),
      onStatus: (status) => send("ssh-connect:status", status),
      prompt: ({ kind, text, signal }) => ask(kind, text, signal),
      askInstall: async () =>
        (await ask(
          "install",
          `Omnigent isn't installed on ${destination}. Install it now? This runs the Omnigent installer there, which also installs uv and the Databricks CLI.`,
        )) === "yes",
    };

    consoles.set(consoleId, {
      win,
      phase: () => phase,
      onConfirm: () => {
        if (phase !== "asking") return;
        phase = "running";
        log(`ssh connect: user confirmed ${destination} -> ${serverUrl}`);
        send("ssh-connect:started", {});
        connect = startConnect(serverUrl, destination, hooks);
        void connect.promise.then((result) => {
          if (settled) return;
          phase = "done";
          answerAll();
          send("ssh-connect:done", {
            ok: result.ok === true,
            error:
              result.ok === true ? null : (result.error ?? `Connecting to ${destination} failed.`),
          });
          settle(pageResult(result));
        });
      },
      onCancel: () => {
        try {
          win.close();
        } catch {
          // Already closing.
        }
      },
      onAnswer: (payload) => {
        const resolve = pending.get(Number(propertyOf(payload, "id")));
        if (!resolve) return;
        pending.delete(Number(propertyOf(payload, "id")));
        const answer =
          propertyOf(payload, "cancel") === true ? null : propertyOf(payload, "answer");
        resolve(typeof answer !== "string" ? null : answer);
      },
    });

    win.on("closed", () => {
      consoles.delete(consoleId);
      answerAll();
      if (phase === "running" && connect) {
        log("ssh connect: console closed mid-run; canceling");
        connect.cancel();
        settle({ ok: false, canceled: true, error: "Connecting was canceled." });
        return;
      }
      settle({ ok: false, canceled: true, error: "Connecting over SSH wasn't approved." });
    });
    win.webContents.once("did-finish-load", () => {
      send("ssh-connect:init", {
        serverUrl,
        destination,
        commands: commandLines(serverUrl, destination),
      });
    });
    win.once("ready-to-show", () => win.show());
    void win.loadFile(pagePath);
    activeRun = { key, win, promise };
    return promise;
  }

  return { run };
}
