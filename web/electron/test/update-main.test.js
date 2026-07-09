const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");

function loadMainHarness({ settings = {}, forceDevUpdateConfig = false } = {}) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-update-test-"));
  fs.writeFileSync(path.join(userData, "settings.json"), JSON.stringify(settings), "utf8");

  const ipcHandlers = new Map();
  const appEvents = new Map();
  const calls = {
    appQuit: 0,
    checkForUpdates: 0,
    downloadUpdate: 0,
    quitAndInstall: [],
    sent: [],
  };

  const sender = {
    getURL: () => "https://server.example/app",
  };
  const win = {
    isDestroyed: () => false,
    webContents: {
      getURL: () => "https://server.example/app",
      send: (channel, payload) => calls.sent.push({ channel, payload }),
    },
    isMinimized: () => false,
    restore: () => {},
    focus: () => {},
  };

  const autoUpdater = new EventEmitter();
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.forceDevUpdateConfig = false;
  autoUpdater.checkForUpdates = () => {
    calls.checkForUpdates += 1;
    return Promise.resolve();
  };
  autoUpdater.downloadUpdate = () => {
    calls.downloadUpdate += 1;
    return Promise.resolve();
  };
  autoUpdater.quitAndInstall = (...args) => {
    calls.quitAndInstall.push(args);
  };

  const electron = {
    app: {
      isPackaged: false,
      getPath: (name) => (name === "userData" ? userData : userData),
      setName: () => {},
      requestSingleInstanceLock: () => true,
      on: (name, listener) => appEvents.set(name, listener),
      whenReady: () => ({ then: () => {} }),
      quit: () => {
        calls.appQuit += 1;
      },
      setAppUserModelId: () => {},
    },
    BrowserWindow: Object.assign(function BrowserWindow() {}, {
      fromWebContents: (webContents) => (webContents === sender ? win : null),
      getFocusedWindow: () => win,
      getAllWindows: () => [win],
    }),
    Menu: {
      buildFromTemplate: (template) => ({ template }),
      setApplicationMenu: () => {},
    },
    Notification: { isSupported: () => false },
    clipboard: {},
    dialog: {
      showMessageBox: () => Promise.resolve({ response: 1, checkboxChecked: false }),
    },
    ipcMain: {
      handle: (channel, handler) => ipcHandlers.set(channel, handler),
      on: () => {},
    },
    nativeImage: {
      createFromPath: () => ({ isEmpty: () => true }),
    },
    screen: {},
    session: { defaultSession: {} },
    shell: {},
    systemPreferences: {},
  };

  const localRequires = {
    "./localhost_cors": { registerLocalhostCors: () => {} },
    "./url": {
      normalizeUrl: (url) => url,
      expandDatabricksWorkspaceUrl: async (url) => url,
    },
    "./workspace-chrome": { registerWorkspaceChromeHide: () => {} },
    "./omnigent_cli": {
      isExecutableFile: () => false,
      resolveCliPath: () => null,
      localHostId: () => "host_test",
      getCliStatus: () => ({ installed: false }),
    },
    "./server_manager": {
      shutdown: () => Promise.resolve(),
      onChange: () => {},
      ensureServerAuth: async () => ({ ok: true }),
      ensureHostConnected: async () => ({ ok: true }),
      restartHost: async () => ({ ok: true }),
      disconnectHost: async () => ({ ok: true }),
    },
  };

  const mainPath = path.join(__dirname, "../src/main.js");
  const source =
    fs.readFileSync(mainPath, "utf8") +
    "\nmodule.exports.__test = { getUpdateConfig, setUpdateConfig, setupAutoUpdater, registerIpc, windows, get installPending() { return installPending; }, get currentUpdateStatus() { return currentUpdateStatus; } };";

  const module = { exports: {} };
  const sandbox = {
    __dirname: path.dirname(mainPath),
    __filename: mainPath,
    AbortController,
    AbortSignal,
    Buffer,
    URL,
    clearInterval,
    console,
    module,
    process: {
      ...process,
      env: {
        ...process.env,
        ...(forceDevUpdateConfig ? { OMNIGENT_FORCE_DEV_UPDATE_CONFIG: "1" } : {}),
      },
    },
    require: (specifier) => {
      if (specifier === "electron") return electron;
      if (specifier === "electron-updater") return { autoUpdater };
      if (specifier in localRequires) return localRequires[specifier];
      return require(specifier);
    },
    setInterval,
  };

  vm.runInNewContext(source, sandbox, { filename: mainPath });
  module.exports.__test.windows.set(win, {
    origin: "https://server.example",
    serverUrl: "https://server.example/app",
    badgeCount: 0,
  });

  return {
    api: module.exports.__test,
    appEvents,
    autoUpdater,
    calls,
    cleanup: () => fs.rmSync(userData, { recursive: true, force: true }),
    events: {
      pinned: { sender, senderFrame: { url: "https://server.example/app" } },
      unpinned: { sender, senderFrame: { url: "https://evil.example/app" } },
    },
    ipcHandlers,
    readSettings: () =>
      JSON.parse(fs.readFileSync(path.join(userData, "settings.json"), "utf8")),
  };
}

async function flushPromises() {
  await new Promise((resolve) => setImmediate(resolve));
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

describe("auto-update main-process wiring", () => {
  it("preserves unrelated settings keys when writing update config", (t) => {
    const harness = loadMainHarness({
      settings: {
        server_url: "https://server.example/app",
        recent_servers: ["https://server.example/app"],
        window_bounds: { width: 1200, height: 800 },
        update_mode: "start",
      },
    });
    t.after(harness.cleanup);

    assert.deepEqual(
      plain(
        harness.api.setUpdateConfig({
          mode: "manual",
          autoInstall: false,
          skippedVersion: "0.4.0",
          ignored: "value",
        }),
      ),
      { mode: "manual", autoInstall: false, skippedVersion: "0.4.0" },
    );

    const saved = harness.readSettings();
    assert.equal(saved.server_url, "https://server.example/app");
    assert.deepEqual(saved.recent_servers, ["https://server.example/app"]);
    assert.deepEqual(saved.window_bounds, { width: 1200, height: 800 });
    assert.equal(saved.update_mode, "manual");
    assert.equal(saved.update_auto_install, false);
    assert.equal(saved.update_skipped_version, "0.4.0");
    assert.equal(saved.mode, undefined);

    harness.api.setUpdateConfig({ mode: "bogus" });
    assert.equal(harness.readSettings().update_mode, "manual");
  });

  it("rejects the frozen IPC handlers from a non-pinned sender", async (t) => {
    const harness = loadMainHarness();
    t.after(harness.cleanup);
    harness.api.registerIpc();

    const cases = [
      ["omnigent:get-update-config", []],
      ["omnigent:get-update-status", []],
      ["omnigent:update-check", []],
      ["omnigent:update-download", []],
      ["omnigent:update-install", []],
      ["omnigent:set-update-config", [{ mode: "manual" }]],
    ];
    for (const [channel, args] of cases) {
      const handler = harness.ipcHandlers.get(channel);
      await assert.rejects(
        Promise.resolve().then(() => handler(harness.events.unpinned, ...args)),
        /connected server page/,
      );
    }
  });

  it("routes update-install through before-quit cleanup to quitAndInstall", async (t) => {
    const harness = loadMainHarness({
      settings: { allowed_hosting_origins: ["https://server.example"] },
    });
    t.after(harness.cleanup);
    harness.api.registerIpc();

    await harness.ipcHandlers.get("omnigent:update-install")(harness.events.pinned);

    assert.equal(harness.api.installPending, true);
    assert.equal(harness.calls.appQuit, 1);

    let prevented = 0;
    harness.appEvents.get("before-quit")({ preventDefault: () => (prevented += 1) });
    await flushPromises();

    assert.equal(prevented, 1);
    assert.deepEqual(harness.calls.quitAndInstall, [[false, true]]);
    assert.equal(harness.calls.appQuit, 1);
  });

  it("supports forceDevUpdateConfig and broadcasts updater events", (t) => {
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
    });
    t.after(harness.cleanup);

    harness.api.setupAutoUpdater();
    harness.autoUpdater.emit("update-available", { version: "0.4.0" });

    assert.equal(harness.autoUpdater.forceDevUpdateConfig, true);
    assert.deepEqual(plain(harness.api.currentUpdateStatus), {
      state: "available",
      info: { version: "0.4.0" },
    });
    assert.deepEqual(plain(harness.calls.sent), [
      {
        channel: "omnigent:update-status",
        payload: { state: "available", info: { version: "0.4.0" } },
      },
    ]);
  });
});
