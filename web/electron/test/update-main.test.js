const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const { createRequire } = require("node:module");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");

function loadMainHarness({
  settings = {},
  forceDevUpdateConfig = false,
  dialogResponses = [{ response: 1, checkboxChecked: false }],
} = {}) {
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
    showMessageBox: [],
    setApplicationMenu: [],
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
  autoUpdater.forceDevUpdateConfig = forceDevUpdateConfig;
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
      setApplicationMenu: (menu) => calls.setApplicationMenu.push(menu),
    },
    Notification: { isSupported: () => false },
    clipboard: {},
    dialog: {
      showMessageBox: (dialogWin, options) => {
        calls.showMessageBox.push({ win: dialogWin, options });
        return Promise.resolve(dialogResponses.shift() ?? { response: 1, checkboxChecked: false });
      },
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
  const mainRequire = createRequire(mainPath);
  // Expose only the composed pieces main.js still owns: the `updater` instance
  // it constructs from ./desktop_updater, plus the menu / IPC / window
  // registries. The updater's own behavior is unit-tested directly in
  // test/desktop_updater.test.js; this file proves main.js wires that instance
  // into the menu, the IPC surface, and the before-quit install handoff.
  const source =
    fs.readFileSync(mainPath, "utf8") +
    "\nmodule.exports.__test = { buildMenu, registerIpc, windows, updater };";

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
      return mainRequire(specifier);
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
    readSettings: () => JSON.parse(fs.readFileSync(path.join(userData, "settings.json"), "utf8")),
  };
}

async function flushPromises() {
  await new Promise((resolve) => setImmediate(resolve));
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function findMenuItem(menu, id) {
  for (const item of menu.template) {
    const submenu = item.submenu ?? [];
    const found = submenu.find((entry) => entry.id === id);
    if (found) return found;
  }
  return null;
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
        harness.api.updater.setConfig({
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

    harness.api.updater.setConfig({ mode: "bogus" });
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

  it("prompts for every privileged update channel before running it", async (t) => {
    const cases = [
      {
        channel: "omnigent:update-download",
        args: [],
        message: "Download an Omnigent update?",
        prepare: () => {},
        assertRan: (harness) => {
          assert.equal(harness.calls.downloadUpdate, 1);
        },
      },
      {
        channel: "omnigent:update-install",
        args: [],
        message: "Restart Omnigent to install an update?",
        prepare: (harness) => {
          harness.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
        },
        assertRan: (harness) => {
          assert.equal(harness.api.updater.installPending, true);
          assert.equal(harness.calls.appQuit, 1);
        },
      },
      {
        channel: "omnigent:set-update-config",
        args: [{ mode: "manual" }],
        message: "Change Omnigent update settings?",
        prepare: () => {},
        assertRan: (harness) => {
          assert.equal(harness.readSettings().update_mode, "manual");
        },
      },
    ];

    for (const item of cases) {
      const harness = loadMainHarness({
        forceDevUpdateConfig: true,
        settings: { update_mode: "manual" },
      });
      t.after(harness.cleanup);
      harness.api.updater.init();
      harness.api.registerIpc();
      item.prepare(harness);

      await harness.ipcHandlers.get(item.channel)(harness.events.pinned, ...item.args);

      assert.equal(harness.calls.showMessageBox.length, 1, item.channel);
      assert.equal(harness.calls.showMessageBox[0].win, harness.api.windows.keys().next().value);
      assert.equal(harness.calls.showMessageBox[0].options.title, "Omnigent");
      assert.equal(harness.calls.showMessageBox[0].options.message, item.message);
      assert.deepEqual(plain(harness.calls.showMessageBox[0].options.buttons), [
        "Don't Allow",
        "Allow Once",
      ]);
      item.assertRan(harness);
    }
  });

  it("does not let a cached hosting grant bypass update-control consent", async (t) => {
    const cases = [
      {
        channel: "omnigent:update-download",
        args: [],
        prepare: () => {},
        assertBlocked: (harness) => {
          assert.equal(harness.calls.downloadUpdate, 0);
        },
      },
      {
        channel: "omnigent:update-install",
        args: [],
        prepare: (harness) => {
          harness.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
        },
        assertBlocked: (harness) => {
          assert.equal(harness.api.updater.installPending, false);
          assert.equal(harness.calls.appQuit, 0);
        },
      },
      {
        channel: "omnigent:set-update-config",
        args: [{ mode: "manual" }],
        prepare: () => {},
        assertBlocked: (harness) => {
          assert.equal(harness.readSettings().update_mode, "start");
        },
      },
    ];

    for (const item of cases) {
      const harness = loadMainHarness({
        forceDevUpdateConfig: true,
        settings: {
          allowed_hosting_origins: ["https://server.example"],
          update_mode: "start",
        },
        dialogResponses: [{ response: 0, checkboxChecked: false }],
      });
      t.after(harness.cleanup);
      harness.api.updater.init();
      harness.api.registerIpc();
      item.prepare(harness);

      await assert.rejects(
        harness.ipcHandlers.get(item.channel)(harness.events.pinned, ...item.args),
        /approved/,
      );

      assert.equal(harness.calls.showMessageBox.length, 1, item.channel);
      item.assertBlocked(harness);
    }
  });

  it("routes approved update-install through before-quit cleanup to quitAndInstall", async (t) => {
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { allowed_hosting_origins: ["https://server.example"], update_mode: "manual" },
    });
    t.after(harness.cleanup);
    harness.api.updater.init();
    harness.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
    harness.api.registerIpc();

    await harness.ipcHandlers.get("omnigent:update-install")(harness.events.pinned);

    assert.equal(harness.calls.showMessageBox.length, 1);
    assert.equal(harness.api.updater.installPending, true);
    assert.equal(harness.calls.appQuit, 1);

    let prevented = 0;
    harness.appEvents.get("before-quit")({ preventDefault: () => (prevented += 1) });
    await flushPromises();

    assert.equal(prevented, 1);
    assert.deepEqual(harness.calls.quitAndInstall, [[false, true]]);
    assert.equal(harness.calls.appQuit, 1);
  });

  it("does not start the install path when no update is downloaded", async (t) => {
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
    });
    t.after(harness.cleanup);
    harness.api.updater.init();
    harness.api.registerIpc();

    await assert.rejects(
      harness.ipcHandlers.get("omnigent:update-install")(harness.events.pinned),
      /No downloaded update/,
    );
    assert.equal(harness.calls.showMessageBox.length, 1);
    assert.equal(harness.api.updater.installPending, false);
    assert.equal(harness.calls.appQuit, 0);

    harness.api.buildMenu();
    const restartItem = findMenuItem(harness.calls.setApplicationMenu.at(-1), "restart_to_update");
    assert.ok(restartItem);
    restartItem.click();
    assert.equal(harness.api.updater.installPending, false);
    assert.equal(harness.calls.appQuit, 0);
  });

  it("surfaces manual check failures without changing the status union", async (t) => {
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
    });
    t.after(harness.cleanup);
    harness.api.updater.init();
    harness.autoUpdater.checkForUpdates = () => {
      harness.calls.checkForUpdates += 1;
      const err = new Error("Cannot find latest.yml: 404");
      harness.autoUpdater.emit("error", err);
      return Promise.reject(err);
    };
    harness.api.registerIpc();

    await assert.rejects(
      harness.ipcHandlers.get("omnigent:update-check")(harness.events.pinned),
      /latest\.yml/,
    );

    assert.equal(harness.calls.checkForUpdates, 1);
    assert.deepEqual(plain(harness.api.updater.getStatus()), {
      state: "idle",
      lastError: "Cannot find latest.yml: 404",
    });
  });

  it("blocks manual update paths when the updater feed is unavailable in development", async (t) => {
    const harness = loadMainHarness({ settings: { update_mode: "manual" } });
    t.after(harness.cleanup);
    harness.api.registerIpc();

    await assert.rejects(
      harness.ipcHandlers.get("omnigent:update-check")(harness.events.pinned),
      /unavailable in development/,
    );
    await assert.rejects(
      harness.ipcHandlers.get("omnigent:update-download")(harness.events.pinned),
      /unavailable in development/,
    );
    await assert.rejects(
      harness.ipcHandlers.get("omnigent:update-install")(harness.events.pinned),
      /unavailable in development/,
    );
    await assert.rejects(
      harness.ipcHandlers.get("omnigent:set-update-config")(harness.events.pinned, {
        mode: "manual",
      }),
      /unavailable in development/,
    );

    assert.equal(harness.calls.showMessageBox.length, 0);
    assert.equal(harness.calls.checkForUpdates, 0);
    assert.equal(harness.calls.downloadUpdate, 0);
    assert.equal(harness.api.updater.installPending, false);
  });

  it("supports forceDevUpdateConfig and broadcasts updater events", (t) => {
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
    });
    t.after(harness.cleanup);

    harness.api.updater.init();
    harness.autoUpdater.emit("update-available", { version: "0.4.0" });

    assert.equal(harness.autoUpdater.forceDevUpdateConfig, true);
    assert.deepEqual(plain(harness.api.updater.getStatus()), {
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
