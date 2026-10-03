const { beforeEach, describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const {
  OIDC_LOGIN_ACTION_CHANNEL,
  OIDC_LOGIN_STATE_CHANNEL,
  runOidcLoginDialog,
} = require("../src/oidc_login_dialog");

class FakeWebContents extends EventEmitter {
  constructor() {
    super();
    this.sent = [];
    this.windowOpenHandler = null;
  }

  send(channel, payload) {
    this.sent.push({ channel, payload });
  }

  setWindowOpenHandler(handler) {
    this.windowOpenHandler = handler;
  }
}

class FakeBrowserWindow extends EventEmitter {
  static instances = [];

  constructor(options) {
    super();
    this.options = options;
    this.webContents = new FakeWebContents();
    this.destroyed = false;
    this.shown = false;
    FakeBrowserWindow.instances.push(this);
  }

  isDestroyed() {
    return this.destroyed;
  }

  show() {
    this.shown = true;
  }

  close() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.emit("closed");
  }

  async loadFile() {
    queueMicrotask(() => {
      this.webContents.emit("did-finish-load");
      this.emit("ready-to-show");
    });
  }
}

function latestWindow() {
  return FakeBrowserWindow.instances.at(-1);
}

describe("OIDC login modal", () => {
  beforeEach(() => {
    FakeBrowserWindow.instances.length = 0;
  });

  it("keeps the sandboxed preload self-contained", () => {
    const source = readFileSync(path.join(__dirname, "../src/oidc_login_preload.js"), "utf8");
    assert.doesNotMatch(source, /require\(["']\.\//);
    const defines = (name, value) => source.includes(`const ${name} = ${JSON.stringify(value)}`);
    assert.ok(defines("OIDC_LOGIN_ACTION_CHANNEL", OIDC_LOGIN_ACTION_CHANNEL));
    assert.ok(defines("OIDC_LOGIN_STATE_CHANNEL", OIDC_LOGIN_STATE_CHANNEL));
    assert.match(source, /ipcRenderer\.send\(OIDC_LOGIN_ACTION_CHANNEL, /);
    assert.match(source, /ipcRenderer\.on\(OIDC_LOGIN_STATE_CHANNEL, /);
  });

  it("uses a sandboxed isolated preload and cancels an in-flight login", async () => {
    const ipcMain = new EventEmitter();
    let aborted = false;
    const flow = runOidcLoginDialog({
      BrowserWindow: FakeBrowserWindow,
      ipcMain,
      parent: {},
      serverUrl: "https://server.example",
      pagePath: "/app/oidc_login.html",
      preloadPath: "/app/oidc_login_preload.js",
      runAttempt: ({ signal }) =>
        new Promise((resolve) => {
          signal.addEventListener("abort", () => {
            aborted = true;
            resolve({ ok: false, error: "cancelled" });
          });
        }),
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    const loginWindow = latestWindow();

    assert.equal(loginWindow.options.webPreferences.sandbox, true);
    assert.equal(loginWindow.options.webPreferences.contextIsolation, true);
    assert.equal(loginWindow.options.webPreferences.nodeIntegration, false);
    assert.equal(loginWindow.shown, true);
    assert.deepEqual(loginWindow.webContents.windowOpenHandler(), { action: "deny" });
    ipcMain.emit(OIDC_LOGIN_ACTION_CHANNEL, { sender: loginWindow.webContents }, "cancel");

    assert.equal(await flow, false);
    assert.equal(aborted, true);
  });

  it("shows a real error, retries, and closes only after success", async () => {
    const ipcMain = new EventEmitter();
    let attempts = 0;
    const flow = runOidcLoginDialog({
      BrowserWindow: FakeBrowserWindow,
      ipcMain,
      parent: {},
      serverUrl: "https://server.example",
      pagePath: "/app/oidc_login.html",
      preloadPath: "/app/oidc_login_preload.js",
      runAttempt: async () => {
        attempts += 1;
        return attempts === 1
          ? { ok: false, error: "The browser sign-in did not complete." }
          : { ok: true };
      },
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    const loginWindow = latestWindow();
    const errorState = loginWindow.webContents.sent.find(
      ({ channel, payload }) => channel === OIDC_LOGIN_STATE_CHANNEL && payload.phase === "error",
    );

    assert.equal(errorState.payload.message, "The browser sign-in did not complete.");
    ipcMain.emit(OIDC_LOGIN_ACTION_CHANNEL, { sender: loginWindow.webContents }, "retry");

    assert.equal(await flow, true);
    assert.equal(attempts, 2);
    assert.equal(loginWindow.destroyed, true);
  });

  it("surfaces recoverable poll progress while the attempt keeps running", async () => {
    const ipcMain = new EventEmitter();
    const flow = runOidcLoginDialog({
      BrowserWindow: FakeBrowserWindow,
      ipcMain,
      parent: {},
      serverUrl: "https://server.example",
      pagePath: "/app/oidc_login.html",
      preloadPath: "/app/oidc_login_preload.js",
      runAttempt: ({ signal, updateMessage }) =>
        new Promise((resolve) => {
          updateMessage(
            "Still waiting — the last attempt failed to reach server.example. Retrying…",
          );
          signal.addEventListener("abort", () => resolve({ ok: false, error: "cancelled" }));
        }),
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    const loginWindow = latestWindow();
    const progressState = loginWindow.webContents.sent.find(
      ({ channel, payload }) =>
        channel === OIDC_LOGIN_STATE_CHANNEL && payload.message.startsWith("Still waiting"),
    );

    assert.deepEqual(progressState.payload, {
      phase: "waiting",
      host: "server.example",
      message: "Still waiting — the last attempt failed to reach server.example. Retrying…",
    });
    ipcMain.emit(OIDC_LOGIN_ACTION_CHANNEL, { sender: loginWindow.webContents }, "cancel");
    assert.equal(await flow, false);
  });

  it("shows a recoverable error for a malformed server URL", async () => {
    const ipcMain = new EventEmitter();
    const flow = runOidcLoginDialog({
      BrowserWindow: FakeBrowserWindow,
      ipcMain,
      parent: {},
      serverUrl: "not a url",
      pagePath: "/app/oidc_login.html",
      preloadPath: "/app/oidc_login_preload.js",
      runAttempt: async () => ({
        ok: false,
        error: "The server address is invalid. Return to setup, correct it, and retry.",
      }),
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    const loginWindow = latestWindow();
    const errorState = loginWindow.webContents.sent.find(
      ({ channel, payload }) => channel === OIDC_LOGIN_STATE_CHANNEL && payload.phase === "error",
    );

    assert.equal(errorState.payload.host, "the configured server");
    assert.equal(
      errorState.payload.message,
      "The server address is invalid. Return to setup, correct it, and retry.",
    );
    ipcMain.emit(OIDC_LOGIN_ACTION_CHANNEL, { sender: loginWindow.webContents }, "cancel");
    assert.equal(await flow, false);
  });

  it("blocks navigation and redirects away from its page and denies all window opens", async () => {
    const ipcMain = new EventEmitter();
    const pagePath = "/app/oidc_login.html";
    const flow = runOidcLoginDialog({
      BrowserWindow: FakeBrowserWindow,
      ipcMain,
      parent: {},
      serverUrl: "https://server.example",
      pagePath,
      preloadPath: "/app/oidc_login_preload.js",
      runAttempt: ({ signal }) =>
        new Promise((resolve) => {
          signal.addEventListener("abort", () => resolve({ ok: false, error: "cancelled" }));
        }),
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    const loginWindow = latestWindow();
    const localEvent = {
      prevented: false,
      preventDefault() {
        this.prevented = true;
      },
    };
    const remoteEvent = {
      prevented: false,
      preventDefault() {
        this.prevented = true;
      },
    };

    loginWindow.webContents.emit("will-navigate", localEvent, pathToFileURL(pagePath).toString());
    loginWindow.webContents.emit("will-redirect", remoteEvent, "https://attacker.example/");

    assert.equal(localEvent.prevented, true);
    assert.equal(remoteEvent.prevented, true);
    assert.deepEqual(
      loginWindow.webContents.windowOpenHandler({ url: "https://attacker.example" }),
      {
        action: "deny",
      },
    );
    ipcMain.emit(OIDC_LOGIN_ACTION_CHANNEL, { sender: loginWindow.webContents }, "cancel");
    assert.equal(await flow, false);
  });

  it("keeps the error state when a progress update arrives after the attempt failed", async () => {
    const ipcMain = new EventEmitter();
    let lateUpdate;
    const flow = runOidcLoginDialog({
      BrowserWindow: FakeBrowserWindow,
      ipcMain,
      parent: {},
      serverUrl: "https://server.example",
      pagePath: "/app/oidc_login.html",
      preloadPath: "/app/oidc_login_preload.js",
      runAttempt: async ({ updateMessage }) => {
        lateUpdate = updateMessage;
        return { ok: false, error: "The browser sign-in did not complete." };
      },
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    const loginWindow = latestWindow();
    lateUpdate("Still waiting…");

    const last = loginWindow.webContents.sent.at(-1).payload;
    assert.equal(last.phase, "error");
    assert.equal(last.message, "The browser sign-in did not complete.");
    ipcMain.emit(OIDC_LOGIN_ACTION_CHANNEL, { sender: loginWindow.webContents }, "cancel");
    assert.equal(await flow, false);
  });

  it("resolves false when the window closes mid-attempt or its page fails to load", async () => {
    const ipcMain = new EventEmitter();
    let aborted = false;
    const closing = runOidcLoginDialog({
      BrowserWindow: FakeBrowserWindow,
      ipcMain,
      parent: {},
      serverUrl: "https://server.example",
      pagePath: "/app/oidc_login.html",
      preloadPath: "/app/oidc_login_preload.js",
      runAttempt: ({ signal }) =>
        new Promise(() => {
          signal.addEventListener("abort", () => {
            aborted = true;
          });
        }),
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    latestWindow().close();
    assert.equal(await closing, false);
    assert.equal(aborted, true);

    class BrokenWindow extends FakeBrowserWindow {
      async loadFile() {
        throw new Error("page missing");
      }
    }
    const broken = runOidcLoginDialog({
      BrowserWindow: BrokenWindow,
      ipcMain,
      parent: {},
      serverUrl: "https://server.example",
      pagePath: "/app/oidc_login.html",
      preloadPath: "/app/oidc_login_preload.js",
      runAttempt: async () => ({ ok: true }),
    });
    assert.equal(await broken, false);
  });

  it("closes when the connection attempt that opened it is superseded", async () => {
    const ipcMain = new EventEmitter();
    const controller = new AbortController();
    const flow = runOidcLoginDialog({
      BrowserWindow: FakeBrowserWindow,
      ipcMain,
      parent: {},
      serverUrl: "https://server.example",
      pagePath: "/app/oidc_login.html",
      preloadPath: "/app/oidc_login_preload.js",
      signal: controller.signal,
      runAttempt: ({ signal }) =>
        new Promise((resolve) => {
          signal.addEventListener("abort", () => resolve({ ok: false, error: "cancelled" }));
        }),
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    controller.abort();

    assert.equal(await flow, false);
    assert.equal(latestWindow().destroyed, true);
  });

  it("ignores actions from other senders and retry while an attempt is waiting", async () => {
    const ipcMain = new EventEmitter();
    let attempts = 0;
    let settle;
    const flow = runOidcLoginDialog({
      BrowserWindow: FakeBrowserWindow,
      ipcMain,
      parent: {},
      serverUrl: "https://server.example",
      pagePath: "/app/oidc_login.html",
      preloadPath: "/app/oidc_login_preload.js",
      runAttempt: () => {
        attempts += 1;
        return new Promise((resolve) => {
          settle = resolve;
        });
      },
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    const loginWindow = latestWindow();

    ipcMain.emit(OIDC_LOGIN_ACTION_CHANNEL, { sender: {} }, "cancel");
    ipcMain.emit(OIDC_LOGIN_ACTION_CHANNEL, { sender: loginWindow.webContents }, "retry");

    assert.equal(loginWindow.destroyed, false);
    assert.equal(attempts, 1);
    settle({ ok: true });
    assert.equal(await flow, true);
  });

  it("resolves false immediately when opened with an already superseded attempt", async () => {
    const controller = new AbortController();
    controller.abort();
    let attempted = false;
    const flow = runOidcLoginDialog({
      BrowserWindow: FakeBrowserWindow,
      ipcMain: new EventEmitter(),
      parent: {},
      serverUrl: "https://server.example",
      pagePath: "/app/oidc_login.html",
      preloadPath: "/app/oidc_login_preload.js",
      signal: controller.signal,
      runAttempt: async () => {
        attempted = true;
        return { ok: false, error: "unexpected" };
      },
    });

    assert.equal(await flow, false);
    assert.equal(attempted, false);
    assert.equal(latestWindow().destroyed, true);
  });
});
