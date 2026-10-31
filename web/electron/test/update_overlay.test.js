const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { pathToFileURL } = require("node:url");

const { createUpdateOverlay, OVERLAY_INSET, OVERLAY_WIDTH } = require("../src/update_overlay");

class FakeWebContents extends EventEmitter {
  constructor() {
    super();
    this.sent = [];
    this.url = "";
    this.windowOpenHandler = null;
    this.destroyed = false;
  }

  getURL() {
    return this.url;
  }

  isDestroyed() {
    return this.destroyed;
  }

  setWindowOpenHandler(handler) {
    this.windowOpenHandler = handler;
  }

  send(channel, payload) {
    if (this.destroyed) throw new TypeError("Object has been destroyed");
    this.sent.push({ channel, payload });
  }
}

class FakeWindow extends EventEmitter {
  constructor(options = {}) {
    super();
    this.options = options;
    this.webContents = new FakeWebContents();
    this.destroyed = false;
    this.visible = false;
    this.bounds = null;
    this.contentBounds = { x: 10, y: 20, width: 1000, height: 700 };
  }

  isDestroyed() {
    return this.destroyed;
  }

  getContentBounds() {
    return this.contentBounds;
  }

  setBounds(bounds) {
    this.bounds = bounds;
  }

  isVisible() {
    return this.visible;
  }

  showInactive() {
    this.visible = true;
  }

  hide() {
    this.visible = false;
  }

  loadFile(file) {
    this.webContents.url = pathToFileURL(file).href;
    return Promise.resolve();
  }

  destroy() {
    this.webContents.destroyed = true;
    this.destroyed = true;
    this.emit("closed");
  }

  close() {
    // Electron destroys a window's renderer before all BrowserWindow "closed"
    // listeners finish. Model the interval where isDestroyed() is still false
    // but webContents.send() can no longer be called.
    this.webContents.destroyed = true;
    this.emit("closed");
    this.destroyed = true;
  }
}

function makeOverlay({ platform = process.platform, updateState } = {}) {
  const onHandlers = new Map();
  const handleHandlers = new Map();
  const windows = [];
  class BrowserWindow extends FakeWindow {
    constructor(options) {
      super(options);
      windows.push(this);
    }
  }
  const ipcMain = {
    on: (channel, handler) => onHandlers.set(channel, handler),
    handle: (channel, handler) => handleHandlers.set(channel, handler),
  };
  const nativeTheme = new EventEmitter();
  nativeTheme.shouldUseDarkColors = false;
  const calls = [];
  const updater = {
    getConfig: () => ({}),
    getStatus: () => ({ state: updateState }),
    setConfig: () => ({}),
    checkForUpdates: async () => {},
    downloadUpdate: async () => {
      calls.push("download");
    },
    installUpdateNow: () => true,
  };
  const controller = createUpdateOverlay({
    BrowserWindow,
    ipcMain,
    nativeTheme,
    updater,
    openAbout: (parent) => calls.push({ openAbout: parent }),
    overlayPage: "/overlay.html",
    preloadPath: "/preload.js",
    platform,
  });
  controller.registerIpc();
  return { calls, controller, handleHandlers, onHandlers, windows };
}

describe("update overlay", () => {
  it("keeps the macOS overlay out of OS window lists without changing Linux window management", () => {
    const mac = makeOverlay({ platform: "darwin" });
    const overlay = mac.controller.ensureOverlay(new FakeWindow());
    // A focusable overlay is listed by window switchers / Mission Control /
    // screen-share pickers as a second app window and steals focus on click.
    assert.equal(overlay.options.focusable, false);
    assert.equal(overlay.options.hiddenInMissionControl, true);
    assert.equal(overlay.options.skipTaskbar, true);
    // Never-active windows only get clicks via first-mouse delivery (macOS).
    assert.equal(overlay.options.acceptFirstMouse, true);

    const linux = makeOverlay({ platform: "linux" });
    const linuxOverlay = linux.controller.ensureOverlay(new FakeWindow());
    // Electron makes non-focusable Linux windows unmanaged, always-on-top, and
    // visible on every workspace. Leave the platform defaults intact there.
    assert.equal(linuxOverlay.options.focusable, undefined);
    assert.equal(linuxOverlay.options.hiddenInMissionControl, undefined);
    assert.equal(linuxOverlay.options.acceptFirstMouse, undefined);
  });

  it("excludes the overlay from the macOS shown-windows menu", () => {
    const mac = makeOverlay({ platform: "darwin" });
    const macOverlay = mac.controller.ensureOverlay(new FakeWindow());
    assert.equal(macOverlay.excludedFromShownWindowsMenu, true);
    assert.equal(macOverlay.options.webPreferences.sandbox, true);
    assert.deepEqual(macOverlay.webContents.windowOpenHandler(), { action: "deny" });
    let prevented = false;
    macOverlay.webContents.emit(
      "will-navigate",
      {
        preventDefault: () => {
          prevented = true;
        },
      },
      "https://evil.example/",
    );
    assert.equal(prevented, true);

    const linux = makeOverlay({ platform: "linux" });
    const linuxOverlay = linux.controller.ensureOverlay(new FakeWindow());
    assert.equal(linuxOverlay.excludedFromShownWindowsMenu, undefined);
  });

  it("broadcasts visible height to the parent and supports an initial read", async () => {
    const { controller, handleHandlers, onHandlers, windows } = makeOverlay();
    const parent = new FakeWindow();
    const overlay = controller.ensureOverlay(parent);
    assert.equal(windows[0], overlay);

    onHandlers.get("omnigent:overlay-height")({ sender: overlay.webContents }, 180.4);

    assert.deepEqual(parent.webContents.sent.at(-1), {
      channel: "omnigent:update-overlay-height",
      payload: 180,
    });
    assert.deepEqual(overlay.bounds, {
      x: 10 + 1000 - OVERLAY_WIDTH - OVERLAY_INSET,
      y: 20 + 700 - 180 - OVERLAY_INSET,
      width: OVERLAY_WIDTH,
      height: 180,
    });
    assert.equal(overlay.isVisible(), true);
    assert.equal(
      await handleHandlers.get("omnigent:get-update-overlay-height")({
        sender: parent.webContents,
      }),
      180,
    );
    assert.equal(
      await handleHandlers.get("omnigent:get-update-overlay-height")({
        sender: new FakeWebContents(),
      }),
      0,
    );

    overlay.destroy();
    assert.deepEqual(parent.webContents.sent.at(-1), {
      channel: "omnigent:update-overlay-height",
      payload: 0,
    });
  });

  it("does not notify a parent whose web contents were destroyed during close", () => {
    const { controller } = makeOverlay();
    const parent = new FakeWindow();
    const overlay = controller.ensureOverlay(parent);

    assert.doesNotThrow(() => parent.close());
    assert.equal(overlay.isDestroyed(), true);
    assert.deepEqual(parent.webContents.sent, []);
  });

  it("keeps an empty overlay hidden with renderer throttling disabled", () => {
    const { controller, onHandlers } = makeOverlay();
    const parent = new FakeWindow();
    const overlay = controller.ensureOverlay(parent);

    onHandlers.get("omnigent:overlay-height")({ sender: overlay.webContents }, 0);

    assert.deepEqual(parent.webContents.sent.at(-1), {
      channel: "omnigent:update-overlay-height",
      payload: 0,
    });
    assert.equal(overlay.isVisible(), false);
    assert.equal(overlay.options.webPreferences.backgroundThrottling, false);
  });

  it("hides a cleared update and shows the next notice", () => {
    const { controller, onHandlers } = makeOverlay();
    const parent = new FakeWindow();
    const overlay = controller.ensureOverlay(parent);
    const reportHeight = onHandlers.get("omnigent:overlay-height");

    reportHeight({ sender: overlay.webContents }, 180);
    assert.equal(overlay.isVisible(), true);
    reportHeight({ sender: overlay.webContents }, 0);
    assert.equal(overlay.isVisible(), false);

    parent.contentBounds = { x: -1400, y: 100, width: 1000, height: 700 };
    parent.emit("move");
    parent.emit("resize");
    assert.equal(overlay.isVisible(), false);

    reportHeight({ sender: overlay.webContents }, 140);
    assert.equal(overlay.isVisible(), true);
    assert.deepEqual(overlay.bounds, {
      x: -1400 + 1000 - OVERLAY_WIDTH - OVERLAY_INSET,
      y: 100 + 700 - 140 - OVERLAY_INSET,
      width: OVERLAY_WIDTH,
      height: 140,
    });
  });

  it("opens About before starting a download from Update now", async () => {
    const { calls, controller, handleHandlers } = makeOverlay();
    const parent = new FakeWindow();
    const overlay = controller.ensureOverlay(parent);

    await handleHandlers.get("omnigent:overlay-update-download")({ sender: overlay.webContents });

    assert.deepEqual(calls, [{ openAbout: parent }, "download"]);
    assert.equal(overlay.isVisible(), false);
    assert.deepEqual(parent.webContents.sent.at(-1), {
      channel: "omnigent:update-overlay-height",
      payload: 0,
    });
  });

  it("does not open About or download for an invalid prompt", async () => {
    const { calls, controller, handleHandlers } = makeOverlay();
    const parent = new FakeWindow();
    const overlay = controller.ensureOverlay(parent);
    const download = handleHandlers.get("omnigent:overlay-update-download");

    await assert.rejects(download({ sender: new FakeWebContents() }), /shell overlay page/);
    overlay.webContents.url = "https://evil.example/";
    await assert.rejects(download({ sender: overlay.webContents }), /shell overlay page/);
    overlay.webContents.url = pathToFileURL("/overlay.html").href;
    parent.destroyed = true;
    await assert.rejects(download({ sender: overlay.webContents }), /active parent window/);
    assert.deepEqual(calls, []);
  });

  it("restores the notice when About closes during an update", () => {
    const { controller, onHandlers } = makeOverlay();
    const parent = new FakeWindow();
    const overlay = controller.ensureOverlay(parent);
    onHandlers.get("omnigent:overlay-height")({ sender: overlay.webContents }, 180);

    controller.suppress(parent);
    assert.equal(overlay.isVisible(), false);
    parent.emit("resize");
    assert.equal(overlay.isVisible(), false);

    controller.unsuppress(parent);
    assert.equal(overlay.bounds.height, 180);
    assert.equal(overlay.isVisible(), true);
    assert.deepEqual(parent.webContents.sent.at(-1), {
      channel: "omnigent:update-overlay-height",
      payload: 180,
    });
  });

  it("keeps the overlay hidden when About closes without a notice", () => {
    const { controller, onHandlers } = makeOverlay();
    const parent = new FakeWindow();
    const overlay = controller.ensureOverlay(parent);
    onHandlers.get("omnigent:overlay-height")({ sender: overlay.webContents }, 0);

    controller.suppress(parent);
    controller.unsuppress(parent);

    assert.equal(overlay.isVisible(), false);
    assert.deepEqual(parent.webContents.sent.at(-1), {
      channel: "omnigent:update-overlay-height",
      payload: 0,
    });
  });

  for (const state of ["available", "downloading", "downloaded"]) {
    it(`keeps the notice hidden while About handles an update in state ${state}`, () => {
      const { controller, onHandlers } = makeOverlay({ updateState: state });
      const parent = new FakeWindow();
      const overlay = controller.ensureOverlay(parent);
      const reportHeight = onHandlers.get("omnigent:overlay-height");
      reportHeight({ sender: overlay.webContents }, 180);

      controller.suppress(parent);
      reportHeight({ sender: overlay.webContents }, 200);
      parent.emit("move");
      parent.emit("resize");
      assert.equal(overlay.isVisible(), false);
      assert.deepEqual(parent.webContents.sent.at(-1), {
        channel: "omnigent:update-overlay-height",
        payload: 0,
      });

      controller.unsuppress(parent);
      assert.equal(overlay.isVisible(), true);
      assert.equal(overlay.bounds.height, 200);
    });
  }
});
