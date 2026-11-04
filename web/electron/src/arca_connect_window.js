"use strict";

/**
 * Shell-owned modal consent showing the exact `arca ssh` command starts a
 * background run; a non-modal output window offers replay, Hide, and Stop.
 *
 * Trust model: these windows are created by the main process and load a
 * bundled file:// page with its own narrow preload. The server-served SPA can
 * neither draw over them, forge a click in them, nor reach their IPC channels
 * — so a Connect click here carries the same authority as the old native
 * dialog (see confirmHostEnrollment for why consent must live outside the
 * server's page). Button messages are accepted only from these windows' own
 * webContents. The SPA can only ask to SHOW a run's output, never to start
 * one past consent.
 *
 * All Electron surfaces are injected so the flow is unit-testable.
 */

/** Channels between this window's preload and main. */
const CONFIRM_CHANNEL = "arca-connect:confirm";
const CANCEL_CHANNEL = "arca-connect:cancel";
const STOP_CHANNEL = "arca-connect:stop";
const RESIZE_CHANNEL = "arca-connect:resize";
const CONTENT_WIDTH = 720;
const MIN_CONTENT_HEIGHT = 300;
const RUNNING_CONTENT_HEIGHT = 500;
const MAX_CONTENT_HEIGHT = 640;
/** UTF-16 characters kept for an output window opened mid-run; a connect prints far less. */
const OUTPUT_BACKLOG_CHARS = 256 * 1024;
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
 * @returns {{
 *   run: (parentWin: unknown, serverUrl: string, options?: RunOptions) => Promise<object>,
 *   showOutput: (parentWin: unknown, serverUrl: string) => boolean,
 * }}
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
   * Button routing, keyed by a console window's webContents id and kept for
   * the window's WHOLE life (not the run's): the post-run "Close" click must
   * still route after the result promise settled. Only messages from a
   * registered console's own webContents are honored — a server page
   * (different webContents) can't spoof consent.
   * @type {Map<number, { win: Electron.BrowserWindow, phase: () => string, onConfirm: () => void, onCancel: () => void, onStop: () => void }>}
   */
  const consoles = new Map();
  /**
   * The in-flight run, or null. One run at a time — a second run() while it
   * exists re-attaches (surfaces its window, shares the outcome) instead of
   * failing, so a refreshed SPA can always get back to an in-flight connect.
   */
  let activeRun = null;

  ipcMain.on(CONFIRM_CHANNEL, (event) => {
    consoles.get(event.sender.id)?.onConfirm();
  });
  ipcMain.on(CANCEL_CHANNEL, (event) => {
    consoles.get(event.sender.id)?.onCancel();
  });
  ipcMain.on(STOP_CHANNEL, (event) => {
    consoles.get(event.sender.id)?.onStop();
  });
  ipcMain.on(RESIZE_CHANNEL, (event, payload) => {
    const consoleState = consoles.get(event.sender.id);
    const consoleWindow = consoleState?.win;
    const measured = Number(payload?.height);
    if (!consoleWindow || !Number.isFinite(measured) || measured <= 0) return;
    const minimum = consoleState.phase() === "asking" ? MIN_CONTENT_HEIGHT : RUNNING_CONTENT_HEIGHT;
    const height = Math.min(MAX_CONTENT_HEIGHT, Math.max(minimum, Math.ceil(measured)));
    try {
      if (!consoleWindow.isDestroyed()) {
        consoleWindow.setContentSize(CONTENT_WIDTH, height, true);
      }
    } catch {
      // Window mid-teardown; content sizing is best-effort.
    }
  });

  const titleFor = (reason) => (reason === "preview-reconnect" ? "Reconnect Arca" : "Connect Arca");
  const alive = (win) => {
    try {
      return Boolean(win) && !win.isDestroyed();
    } catch {
      return false;
    }
  };
  const surface = (win) => {
    try {
      if (!alive(win)) return;
      win.show();
      win.focus();
    } catch {
      // Window mid-teardown; the run's promise still settles.
    }
  };

  /**
   * Open one console window and register its buttons.
   * @param {{ parentWin: unknown, modal: boolean, height: number, title: string,
   *   onLoad: (win: Electron.BrowserWindow) => void, onClosed: () => void,
   *   handlers: { phase: () => string, onConfirm: () => void, onStop: () => void } }} spec
   */
  function openConsole({ parentWin, modal, height, title, onLoad, onClosed, handlers }) {
    const win = new BrowserWindow({
      parent: parentWin ?? undefined,
      modal,
      width: CONTENT_WIDTH,
      height,
      useContentSize: true,
      minimizable: !modal,
      maximizable: false,
      fullscreenable: false,
      resizable: false,
      show: false,
      title,
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
    consoles.set(consoleId, {
      win,
      ...handlers,
      // Cancel, Hide, and the post-run Close all just close this window; its
      // closed handler decides what that means for the run.
      onCancel: () => {
        try {
          win.close();
        } catch {
          // Already closing.
        }
      },
    });
    win.on("closed", () => {
      consoles.delete(consoleId);
      onClosed();
    });
    // did-finish-load fires after the page's top-level script ran, so its
    // IPC listeners exist before init arrives (ready-to-show can race them).
    win.webContents.once("did-finish-load", () => onLoad(win));
    win.once("ready-to-show", () => {
      win.show();
    });
    void win.loadFile(pagePath);
    return win;
  }

  /**
   * Open the consent sheet over `parentWin` and drive one connect to
   * `serverUrl`. Resolves with the connect result: `{ ok: false, canceled:
   * true }` when the user declines, or stops the run; the command's own result
   * after a confirmed run.
   *
   * By default the output window opens as soon as the user confirms. Callers
   * that show the run's progress themselves pass `inlineProgress`; the output
   * then opens only on request (see showOutput), for sign-in, or on failure.
   *
   * @param {unknown} parentWin
   * @param {string} serverUrl
   * @param {RunOptions} [options]
   */
  function run(parentWin, serverUrl, options = {}) {
    // Re-attach: an in-flight run survives an SPA refresh (it's shell-owned),
    // so a repeat click surfaces it and awaits the same outcome rather than
    // erroring with "already in progress".
    if (activeRun) {
      if (activeRun.target !== arcaTarget(serverUrl)) {
        return Promise.resolve({
          ok: false,
          error: "An Arca connection to another server is in progress. Try again when it finishes.",
        });
      }
      activeRun.updateReason(options.reason);
      activeRun.surface(parentWin);
      return activeRun.promise;
    }

    let resolveRun;
    const promise = new Promise((resolve) => {
      resolveRun = resolve;
    });
    let reason = options.reason ?? null;
    let phase = "asking";
    let loginAttempted = false;
    let connect = null;
    let settled = false;
    /** The last `arca-connect:started` payload, replayed into a late output window. */
    let started = null;
    let output = "";
    let done = null;
    let consentWin = null;
    let outputWin = null;
    /** Live sends wait for the output window's backlog replay, so nothing arrives twice. */
    let outputReady = false;

    // A background run stops once every window that asked for it has closed:
    // nothing is left to show its progress or offer Stop.
    const watchers = new Map();
    const watch = (win) => {
      if (!win || watchers.has(win) || typeof win.once !== "function") return;
      const onClosed = () => {
        watchers.delete(win);
        if (watchers.size === 0) stop();
      };
      try {
        if (win.isDestroyed?.()) return;
        win.once("closed", onClosed);
        watchers.set(win, onClosed);
      } catch {
        // Mid-teardown; nothing to watch.
      }
    };
    const unwatchAll = () => {
      for (const [win, onClosed] of watchers) {
        try {
          win.removeListener?.("closed", onClosed);
        } catch {
          // Window already gone.
        }
      }
      watchers.clear();
    };
    watch(parentWin);

    const sendTo = (win, channel, payload) => {
      try {
        if (alive(win)) win.webContents.send(channel, payload);
      } catch {
        // Window torn down mid-send; its closed handler cleans up.
      }
    };
    const initPayload = (view) => ({
      serverUrl,
      command: commandLine(serverUrl),
      reason,
      view,
    });
    const settle = (result) => {
      if (settled) return;
      settled = true;
      if (activeRun?.promise === promise) activeRun = null;
      unwatchAll();
      resolveRun(result);
    };
    const stop = () => {
      if (phase !== "running" || !connect) return;
      // An unwatched enrollment must not keep executing once the user stopped
      // it or closed the window that asked for it.
      log("arca connect: stopped mid-run; canceling");
      connect.cancel();
      // The canceled command's exit settles via its promise too; settle() is
      // idempotent.
      settle({ ok: false, canceled: true, error: "Connecting to Arca was canceled." });
      // Nothing is running anymore, so a window still offering Stop and Hide
      // would be stale.
      if (alive(outputWin)) outputWin.close();
    };

    const sendLive = (channel, payload) => {
      if (outputReady) sendTo(outputWin, channel, payload);
    };

    const openOutput = (requestedBy) => {
      if (alive(outputWin)) {
        surface(outputWin);
        return;
      }
      // The window that started the run may have closed while another watches it.
      outputReady = false;
      outputWin = openConsole({
        parentWin: [requestedBy, parentWin, ...watchers.keys()].find(alive),
        modal: false,
        height: RUNNING_CONTENT_HEIGHT,
        title: titleFor(reason),
        handlers: { phase: () => phase, onConfirm: () => {}, onStop: stop },
        onLoad: (win) => {
          sendTo(win, "arca-connect:init", initPayload("output"));
          if (started) sendTo(win, "arca-connect:started", started);
          if (output) sendTo(win, "arca-connect:output", output);
          if (done) sendTo(win, "arca-connect:done", done);
          outputReady = true;
        },
        // Hiding the output never stops the run; Stop connecting does.
        onClosed: () => {
          outputWin = null;
          outputReady = false;
        },
      });
    };

    const appendOutput = (text) => {
      output = (output + text).slice(-OUTPUT_BACKLOG_CHARS);
      sendLive("arca-connect:output", text);
    };

    const runCommand = (login = false) => {
      phase = "running";
      connect = login ? startLogin(serverUrl) : startConnect(serverUrl, appendOutput);
      started = {
        login,
        command: login ? loginCommandLine(serverUrl) : commandLine(serverUrl),
      };
      sendLive("arca-connect:started", started);
      // Sign-in waits on the user's browser, so its instructions are shown.
      if (login) openOutput();
      const finish = (result) => {
        // A stopped run must not start the next command after a late exit.
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
        done = {
          ok: result.ok === true,
          error: result.ok === true ? null : (result.error ?? "Connecting to Arca failed."),
        };
        // Sent before a failure opens the output, whose load replays it once.
        sendLive("arca-connect:done", done);
        // A failure is reported where its output can be read.
        if (!done.ok) openOutput();
        settle({ ...result, shownInConsole: alive(outputWin) });
      };
      void connect.promise.then(finish).catch((error) => {
        // A window that fails to open must not leave the run unsettled.
        log(`arca connect: finishing the run failed: ${error?.message ?? error}`);
        try {
          connect.cancel();
        } catch {
          // Already exited.
        }
        settle({ ok: false, error: "Connecting to Arca failed." });
      });
    };

    consentWin = openConsole({
      parentWin,
      modal: true,
      height: MIN_CONTENT_HEIGHT,
      title: titleFor(reason),
      handlers: {
        phase: () => phase,
        onConfirm: () => {
          if (phase !== "asking") return;
          log(`arca connect: user confirmed; running against ${serverUrl}`);
          runCommand();
          // Consent is given: release the window and keep the run going.
          const confirmed = consentWin;
          consentWin = null;
          try {
            confirmed.close();
          } catch {
            // Already closing.
          }
          if (!options.inlineProgress) openOutput();
        },
        onStop: () => {},
      },
      onLoad: (win) => sendTo(win, "arca-connect:init", initPayload("consent")),
      onClosed: () => {
        if (phase !== "asking") return;
        consentWin = null;
        // Declining the console is a deliberate user choice, not a failure —
        // `canceled` tells the picker to stay silent instead of rendering a
        // red error with a retry.
        settle({ ok: false, canceled: true, error: "Connecting Arca wasn't approved." });
      },
    });

    activeRun = {
      promise,
      target: arcaTarget(serverUrl),
      updateReason: (nextReason) => {
        reason = nextReason ?? null;
        const asking = phase === "asking";
        const shown = asking ? consentWin : outputWin;
        if (!alive(shown)) return;
        try {
          shown.setTitle?.(titleFor(reason));
        } catch {
          // Window mid-teardown.
        }
        // A loading output window reads the new reason when it replays.
        if (asking || outputReady) {
          sendTo(shown, "arca-connect:init", initPayload(asking ? "consent" : "output"));
        }
      },
      surface: (requestedBy) => {
        watch(requestedBy);
        if (phase === "asking") surface(consentWin);
        else openOutput(requestedBy);
      },
    };
    return promise;
  }

  /**
   * Surface the in-flight run for `serverUrl`: its consent sheet before the
   * user confirmed, its output window after. Returns false when no run for
   * that server is in flight. It never starts or confirms a run.
   *
   * @param {unknown} parentWin
   * @param {string} serverUrl
   */
  function showOutput(parentWin, serverUrl) {
    if (!activeRun || activeRun.target !== arcaTarget(serverUrl)) return false;
    activeRun.surface(parentWin);
    return true;
  }

  return { run, showOutput };
}

/**
 * @typedef {{
 *   reason?: "preview-reconnect",
 *   inlineProgress?: boolean,
 * }} RunOptions
 */

module.exports = {
  CANCEL_CHANNEL,
  CONFIRM_CHANNEL,
  STOP_CHANNEL,
  RESIZE_CHANNEL,
  CONTENT_WIDTH,
  MIN_CONTENT_HEIGHT,
  RUNNING_CONTENT_HEIGHT,
  MAX_CONTENT_HEIGHT,
  OUTPUT_BACKLOG_CHARS,
  createArcaConnectFlow,
};
