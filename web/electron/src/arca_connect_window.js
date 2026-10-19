"use strict";

/**
 * The Arca connect console — a SHELL-OWNED modal window that replaces the
 * bare native consent dialog. It shows the exact `arca ssh` command that will
 * run and, after the user confirms, streams the command's live stdout/stderr
 * into an embedded terminal pane, so enrollment is fully transparent.
 *
 * Trust model: this window is created by the main process and loads a bundled
 * file:// page with its own narrow preload. The server-served SPA can neither
 * draw over it, forge a click in it, nor reach its IPC channels — so a
 * Confirm click here carries the same authority as the old native dialog
 * (see confirmHostEnrollment for why consent must live outside the server's
 * page). Confirm/cancel messages are accepted only from this window's own
 * webContents.
 *
 * All Electron surfaces are injected so the flow is unit-testable.
 */

/** Channels between this window's preload and main. */
const CONFIRM_CHANNEL = "arca-connect:confirm";
const CANCEL_CHANNEL = "arca-connect:cancel";
const RESIZE_CHANNEL = "arca-connect:resize";
const CONTENT_WIDTH = 720;
const MIN_CONTENT_HEIGHT = 300;
const RUNNING_CONTENT_HEIGHT = 500;
const MAX_CONTENT_HEIGHT = 640;
const { arcaTarget } = require("./arcaIdentity");

/**
 * @param {{
 *   BrowserWindow: typeof import("electron").BrowserWindow,
 *   ipcMain: import("electron").IpcMain,
 *   pagePath: string,
 *   preloadPath: string,
 *   startConnect: (serverUrl: string, onOutput: (text: string) => void) =>
 *     ReturnType<typeof import("./arca").startArcaConnect>,
 *   commandLine: (serverUrl: string) => string,
 *   startLogin: (serverUrl: string) => ReturnType<typeof import("./arca").startArcaLogin>,
 *   loginCommandLine: (serverUrl: string) => string,
 *   log?: (message: string) => void,
 * }} deps
 * @returns {{ run: (parentWin: unknown, serverUrl: string, options?: { reason?: "preview-reconnect" }) => Promise<object> }}
 */
function createArcaConnectFlow({
  BrowserWindow,
  ipcMain,
  pagePath,
  preloadPath,
  startConnect,
  commandLine,
  startLogin,
  loginCommandLine,
  log = () => {},
}) {
  /**
   * Button routing, keyed by the console window's webContents id and kept
   * for the window's WHOLE life (not the run's): the post-run "Close" click
   * must still route after the result promise settled. Only messages from a
   * registered console's own webContents are honored — a server page
   * (different webContents) can't spoof consent.
   * @type {Map<number, { win: Electron.BrowserWindow, phase: () => string, onConfirm: () => void, onCancel: () => void }>}
   */
  const consoles = new Map();
  /**
   * The in-flight run's window + shared outcome promise, or null. One run at
   * a time — a second run() while it exists re-attaches (focuses the window,
   * shares the outcome) instead of failing, so a refreshed SPA can always get
   * back to an in-flight connect.
   */
  let activeRun = null;

  ipcMain.on(CONFIRM_CHANNEL, (event) => {
    consoles.get(event.sender.id)?.onConfirm();
  });
  ipcMain.on(CANCEL_CHANNEL, (event) => {
    consoles.get(event.sender.id)?.onCancel();
  });
  ipcMain.on(RESIZE_CHANNEL, (event, payload) => {
    const consoleState = consoles.get(event.sender.id);
    const consoleWindow = consoleState?.win;
    const measured = Number(payload?.height);
    if (!consoleWindow || !Number.isFinite(measured) || measured <= 0) return;
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
   *
   * @param {unknown} parentWin
   * @param {string} serverUrl
   * @param {{ reason?: "preview-reconnect" }} [options]
   */
  function run(parentWin, serverUrl, options = {}) {
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
      return activeRun.promise;
    }
    const promise = new Promise((resolve) => {
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
      let phase = "asking";
      let loginAttempted = false;
      let connect = null;
      let settled = false;
      const sendInit = (reason) => {
        try {
          if (win.isDestroyed()) return;
          win.setTitle?.(reason === "preview-reconnect" ? "Reconnect Arca" : "Connect Arca");
          win.webContents.send("arca-connect:init", {
            serverUrl,
            command: commandLine(serverUrl),
            reason: reason ?? null,
          });
        } catch {
          // Window torn down mid-send; the close handler settles the flow.
        }
      };
      const settle = (result) => {
        if (settled) return;
        settled = true;
        activeRun = null;
        resolve(result);
      };

      const send = (channel, payload) => {
        try {
          if (!win.isDestroyed()) win.webContents.send(channel, payload);
        } catch {
          // Window torn down mid-send; the close handler settles the flow.
        }
      };

      const runCommand = (login = false) => {
        phase = "running";
        connect = login
          ? startLogin(serverUrl)
          : startConnect(serverUrl, (text) => send("arca-connect:output", text));
        send("arca-connect:started", {
          login,
          command: login ? loginCommandLine(serverUrl) : commandLine(serverUrl),
        });
        void connect.promise.then((result) => {
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
    if (activeRun) activeRun.promise = promise;
    return promise;
  }

  return { run };
}

module.exports = {
  CANCEL_CHANNEL,
  CONFIRM_CHANNEL,
  RESIZE_CHANNEL,
  CONTENT_WIDTH,
  MIN_CONTENT_HEIGHT,
  RUNNING_CONTENT_HEIGHT,
  MAX_CONTENT_HEIGHT,
  createArcaConnectFlow,
};
