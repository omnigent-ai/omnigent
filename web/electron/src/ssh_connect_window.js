"use strict";

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

const CONFIRM_CHANNEL = "ssh-connect:confirm";
const CANCEL_CHANNEL = "ssh-connect:cancel";
const ANSWER_CHANNEL = "ssh-connect:answer";
const RESIZE_CHANNEL = "ssh-connect:resize";
const CONTENT_WIDTH = 720;
const MIN_CONTENT_HEIGHT = 300;
const RUNNING_CONTENT_HEIGHT = 500;
const MAX_CONTENT_HEIGHT = 680;

/**
 * What the server's page learns about a finished connect: app-authored fields
 * only. Remote output, including error text, stays in the shell-owned window.
 *
 * @param {{ ok?: boolean, canceled?: boolean, alreadyRunning?: boolean, identity?: { serverUrl: string, hostId: string } | null }} result
 * @returns {object}
 */
function pageResult(result) {
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

/**
 * @param {{
 *   BrowserWindow: typeof import("electron").BrowserWindow,
 *   ipcMain: import("electron").IpcMain,
 *   pagePath: string,
 *   preloadPath: string,
 *   startConnect: (serverUrl: string, destination: string, hooks: object) =>
 *     { promise: Promise<object>, cancel: () => void },
 *   commandLines: (serverUrl: string, destination: string) => string[],
 *   log?: (message: string) => void,
 * }} deps
 * @returns {{ run: (parentWin: unknown, serverUrl: string, destination: string) => Promise<object> }}
 */
function createSshConnectFlow({
  BrowserWindow,
  ipcMain,
  pagePath,
  preloadPath,
  startConnect,
  commandLines,
  log = () => {},
}) {
  /** Keyed by the console's webContents id, for the window's whole life. */
  const consoles = new Map();
  /** One connect at a time; a repeat for the same target re-focuses it. */
  let activeRun = null;

  ipcMain.on(CONFIRM_CHANNEL, (event) => consoles.get(event.sender.id)?.onConfirm());
  ipcMain.on(CANCEL_CHANNEL, (event) => consoles.get(event.sender.id)?.onCancel());
  ipcMain.on(ANSWER_CHANNEL, (event, payload) => consoles.get(event.sender.id)?.onAnswer(payload));
  ipcMain.on(RESIZE_CHANNEL, (event, payload) => {
    const state = consoles.get(event.sender.id);
    const measured = Number(payload?.height);
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
  function run(parentWin, serverUrl, destination) {
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

    let resolveRun;
    const promise = new Promise((resolve) => {
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
    let phase = "asking";
    let connect = null;
    let settled = false;
    let nextPromptId = 1;
    /** @type {Map<number, (answer: string | null) => void>} */
    const pending = new Map();

    const send = (channel, payload) => {
      try {
        if (!win.isDestroyed()) win.webContents.send(channel, payload);
      } catch {
        // Window torn down mid-send; the close handler settles the flow.
      }
    };
    const settle = (result) => {
      if (settled) return;
      settled = true;
      activeRun = null;
      resolveRun(result);
    };
    const ask = (kind, text, signal) =>
      new Promise((resolve) => {
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

    const hooks = {
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
        const resolve = pending.get(Number(payload?.id));
        if (!resolve) return;
        pending.delete(Number(payload.id));
        resolve(
          payload?.cancel === true || typeof payload?.answer !== "string" ? null : payload.answer,
        );
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

module.exports = {
  ANSWER_CHANNEL,
  CANCEL_CHANNEL,
  CONFIRM_CHANNEL,
  CONTENT_WIDTH,
  MAX_CONTENT_HEIGHT,
  MIN_CONTENT_HEIGHT,
  RESIZE_CHANNEL,
  RUNNING_CONTENT_HEIGHT,
  createSshConnectFlow,
};
