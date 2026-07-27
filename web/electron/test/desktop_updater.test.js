// Direct unit tests for the desktop auto-updater module.
//
// Unlike update-main.test.js (which boots the whole main.js in a vm to prove
// the wiring is hooked up), this file requires the module normally and drives
// its API with in-memory fakes for every injected dependency. That keeps the
// updater's own behavior — config normalization, feed gating, consent, event
// broadcasts, manual-error surfacing, and the install handoff — tested at the
// unit boundary the refactor introduced.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const {
  createDesktopUpdater,
  normalizeUpdateConfig,
  isUpdateSecurityError,
  UPDATE_MODES,
  UPDATES_UNAVAILABLE_IN_DEV,
} = require("../src/desktop_updater");

const PINNED_ORIGIN = "https://server.example";

/**
 * Build a fully-faked updater plus handles for asserting on the injected deps.
 *
 * @param {object} [opts]
 * @param {Record<string, unknown>} [opts.settings] Initial persisted settings.
 * @param {boolean} [opts.isPackaged] Simulate a packaged build.
 * @param {boolean} [opts.forceDevUpdateConfig] Force the dev feed on.
 * @param {boolean} [opts.pinnedSender] Whether IPC calls count as trusted.
 * @param {Array<{response: number}>} [opts.dialogResponses] Queued dialog answers.
 */
function makeUpdater({
  settings = {},
  isPackaged = false,
  forceDevUpdateConfig = false,
  pinnedSender = true,
  dialogResponses = [{ response: 1 }],
} = {}) {
  let store = { ...settings };
  const calls = {
    sent: [],
    showMessageBox: [],
    appQuit: 0,
    downloadUpdate: 0,
    quitAndInstall: [],
    intervals: [],
    clearedIntervals: 0,
  };

  const win = {
    isDestroyed: () => false,
    webContents: {
      send: (channel, payload) => calls.sent.push({ channel, payload }),
    },
  };

  const autoUpdater = new EventEmitter();
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.forceDevUpdateConfig = forceDevUpdateConfig;
  autoUpdater.checkForUpdates = () => {
    calls.checkForUpdates = (calls.checkForUpdates ?? 0) + 1;
    return Promise.resolve();
  };
  autoUpdater.downloadUpdate = () => {
    calls.downloadUpdate += 1;
    return Promise.resolve();
  };
  autoUpdater.quitAndInstall = (...args) => calls.quitAndInstall.push(args);

  const ipcHandlers = new Map();

  const deps = {
    app: {
      isPackaged,
      quit: () => {
        calls.appQuit += 1;
      },
    },
    BrowserWindow: {
      getAllWindows: () => [win],
      fromWebContents: () => win,
    },
    ipcMain: {
      handle: (channel, handler) => ipcHandlers.set(channel, handler),
    },
    dialog: {
      showMessageBox: (dialogWin, options) => {
        calls.showMessageBox.push({ win: dialogWin, options });
        return Promise.resolve(dialogResponses.shift() ?? { response: 1 });
      },
    },
    nativeImage: {
      createFromPath: () => ({ isEmpty: () => true }),
    },
    autoUpdater,
    loadSettings: () => ({ ...store }),
    saveSettings: (next) => {
      store = { ...next };
    },
    isPinnedOriginSender: () => pinnedSender,
    pinnedOrigin: () => PINNED_ORIGIN,
    iconPath: "/icons/icon.png",
    forceDevUpdateConfig,
  };

  const updater = createDesktopUpdater(deps);
  return {
    updater,
    autoUpdater,
    calls,
    ipcHandlers,
    win,
    event: { sender: {} },
    readSettings: () => ({ ...store }),
  };
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

describe("desktop_updater — pure helpers", () => {
  it("normalizes unknown/missing config to safe defaults", () => {
    assert.deepEqual(normalizeUpdateConfig({}), {
      mode: "default",
      autoInstall: true,
      skippedVersion: null,
    });
    assert.deepEqual(normalizeUpdateConfig({ update_mode: "bogus" }).mode, "default");
    assert.deepEqual(
      normalizeUpdateConfig({
        update_mode: "manual",
        update_auto_install: false,
        update_skipped_version: "0.4.0",
      }),
      { mode: "manual", autoInstall: false, skippedVersion: "0.4.0" },
    );
  });

  it("exposes the update-mode set and dev-unavailable message", () => {
    assert.deepEqual([...UPDATE_MODES].sort(), ["default", "manual", "none", "start"]);
    assert.match(UPDATES_UNAVAILABLE_IN_DEV, /unavailable in development/);
  });

  it("classifies signing/integrity errors as security errors", () => {
    assert.equal(isUpdateSecurityError("sha512 mismatch"), true);
    assert.equal(isUpdateSecurityError("app is not signed"), true);
    assert.equal(isUpdateSecurityError("Cannot find latest.yml: 404"), false);
  });
});

describe("desktop_updater — config persistence", () => {
  it("preserves unrelated settings keys when writing update config", () => {
    const h = makeUpdater({
      settings: {
        server_url: "https://server.example/app",
        window_bounds: { width: 1200, height: 800 },
        update_mode: "start",
      },
    });

    assert.deepEqual(
      plain(
        h.updater.setConfig({
          mode: "manual",
          autoInstall: false,
          skippedVersion: "0.4.0",
          ignored: "value",
        }),
      ),
      { mode: "manual", autoInstall: false, skippedVersion: "0.4.0" },
    );

    const saved = h.readSettings();
    assert.equal(saved.server_url, "https://server.example/app");
    assert.deepEqual(saved.window_bounds, { width: 1200, height: 800 });
    assert.equal(saved.update_mode, "manual");
    assert.equal(saved.update_auto_install, false);
    assert.equal(saved.update_skipped_version, "0.4.0");
    assert.equal(saved.mode, undefined);

    h.updater.setConfig({ mode: "bogus" });
    assert.equal(h.readSettings().update_mode, "manual");
  });
});

describe("desktop_updater — event wiring + broadcast", () => {
  it("broadcasts updater events and replays the latest status", () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();

    h.autoUpdater.emit("update-available", { version: "0.4.0" });

    assert.equal(h.autoUpdater.forceDevUpdateConfig, true);
    assert.deepEqual(plain(h.updater.getStatus()), {
      state: "available",
      info: { version: "0.4.0" },
    });
    assert.deepEqual(plain(h.calls.sent), [
      {
        channel: "omnigent:update-status",
        payload: { state: "available", info: { version: "0.4.0" } },
      },
    ]);
  });

  it("start mode kicks off a check with no lingering periodic timer", () => {
    // "start" runs one check at launch (unlike "default", which also schedules
    // a 6-hour re-check); asserting on it avoids leaving a live interval that
    // would keep the test process alive.
    const h = makeUpdater({ isPackaged: true, settings: { update_mode: "start" } });
    h.updater.init();
    assert.equal(h.calls.checkForUpdates, 1);
  });
});

describe("desktop_updater — manual check errors", () => {
  it("surfaces manual check failures without a false available/none flip", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.autoUpdater.checkForUpdates = () => {
      const err = new Error("Cannot find latest.yml: 404");
      h.autoUpdater.emit("error", err);
      return Promise.reject(err);
    };

    await assert.rejects(h.updater.checkForUpdates({ manual: true }), /latest\.yml/);
    assert.deepEqual(plain(h.updater.getStatus()), {
      state: "idle",
      lastError: "Cannot find latest.yml: 404",
    });
  });

  it("marks signing errors as error-security", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.autoUpdater.checkForUpdates = () => {
      const err = new Error("sha512 checksum mismatch");
      h.autoUpdater.emit("error", err);
      return Promise.reject(err);
    };

    await assert.rejects(h.updater.checkForUpdates({ manual: true }), /sha512/);
    assert.equal(h.updater.getStatus().state, "error-security");
  });
});

describe("desktop_updater — dev feed gating", () => {
  it("blocks manual paths when the feed is unavailable in development", async () => {
    const h = makeUpdater({ settings: { update_mode: "manual" } });
    h.updater.registerIpc();

    for (const channel of [
      "omnigent:update-check",
      "omnigent:update-download",
      "omnigent:update-install",
      "omnigent:set-update-config",
    ]) {
      await assert.rejects(
        h.ipcHandlers.get(channel)(h.event, { mode: "manual" }),
        /unavailable in development/,
      );
    }
    assert.equal(h.calls.showMessageBox.length, 0);
    assert.equal(h.calls.checkForUpdates ?? 0, 0);
    assert.equal(h.calls.downloadUpdate, 0);
    assert.equal(h.updater.installPending, false);
  });
});

describe("desktop_updater — IPC trust + consent", () => {
  it("rejects every privileged handler from a non-pinned sender", async () => {
    const h = makeUpdater({ pinnedSender: false });
    h.updater.registerIpc();

    const cases = [
      ["omnigent:get-update-config", []],
      ["omnigent:get-update-status", []],
      ["omnigent:update-check", []],
      ["omnigent:update-download", []],
      ["omnigent:update-install", []],
      ["omnigent:set-update-config", [{ mode: "manual" }]],
    ];
    for (const [channel, args] of cases) {
      await assert.rejects(
        Promise.resolve().then(() => h.ipcHandlers.get(channel)(h.event, ...args)),
        /connected server page/,
      );
    }
  });

  it("prompts and runs each privileged action when approved", async () => {
    // download
    let h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.updater.registerIpc();
    await h.ipcHandlers.get("omnigent:update-download")(h.event);
    assert.equal(h.calls.showMessageBox.length, 1);
    assert.equal(h.calls.showMessageBox[0].options.message, "Download an Omnigent update?");
    assert.equal(h.calls.downloadUpdate, 1);

    // set-config
    h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "start" } });
    h.updater.init();
    h.updater.registerIpc();
    await h.ipcHandlers.get("omnigent:set-update-config")(h.event, { mode: "manual" });
    assert.equal(h.calls.showMessageBox[0].options.message, "Change Omnigent update settings?");
    assert.equal(h.readSettings().update_mode, "manual");
  });

  it("does not let a denied consent run the action", async () => {
    const h = makeUpdater({
      forceDevUpdateConfig: true,
      settings: { update_mode: "start" },
      dialogResponses: [{ response: 0 }],
    });
    h.updater.init();
    h.updater.registerIpc();

    await assert.rejects(h.ipcHandlers.get("omnigent:update-download")(h.event), /approved/);
    assert.equal(h.calls.downloadUpdate, 0);
  });
});

describe("desktop_updater — install handoff", () => {
  it("routes an approved install through before-quit to quitAndInstall", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
    h.updater.registerIpc();

    await h.ipcHandlers.get("omnigent:update-install")(h.event);
    assert.equal(h.calls.showMessageBox.length, 1);
    assert.equal(h.updater.installPending, true);
    assert.equal(h.calls.appQuit, 1);

    assert.equal(h.updater.quitAndInstallIfPending(), true);
    assert.deepEqual(h.calls.quitAndInstall, [[false, true]]);
  });

  it("install fails cleanly when no update is downloaded", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.updater.registerIpc();

    await assert.rejects(
      h.ipcHandlers.get("omnigent:update-install")(h.event),
      /No downloaded update/,
    );
    assert.equal(h.updater.installPending, false);
    assert.equal(h.calls.appQuit, 0);
    // Nothing pending → the before-quit handoff is a no-op.
    assert.equal(h.updater.quitAndInstallIfPending(), false);
    assert.deepEqual(h.calls.quitAndInstall, []);
  });
});
