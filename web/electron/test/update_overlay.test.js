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
  }

  getURL() {
    return this.url;
  }

  setWindowOpenHandler(handler) {
    this.windowOpenHandler = handler;
  }

  send(channel, payload) {
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
    this.ignoreMouse = [];
  }

  isDestroyed() {
    return this.destroyed;
  }

  getContentBounds() {
    return { x: 10, y: 20, width: 1000, height: 700 };
  }

  setBounds(bounds) {
    this.bounds = bounds;
  }

  setIgnoreMouseEvents(ignore, options) {
    this.ignoreMouse.push({ ignore, options });
  }

  isVisible() {
    return this.visible;
  }

  showInactive() {
    this.visible = true;
  }

  loadFile(file) {
    this.webContents.url = pathToFileURL(file).href;
    return Promise.resolve();
  }

  destroy() {
    this.destroyed = true;
    this.emit("closed");
  }
}

function makeOverlay({ platform = process.platform } = {}) {
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
    getStatus: () => ({}),
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
    assert.deepEqual(overlay.ignoreMouse.at(-1), { ignore: false, options: undefined });
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

  it("reports zero and keeps an empty overlay as a click-through sliver", () => {
    const { controller, onHandlers } = makeOverlay();
    const parent = new FakeWindow();
    const overlay = controller.ensureOverlay(parent);

    onHandlers.get("omnigent:overlay-height")({ sender: overlay.webContents }, 0);

    assert.deepEqual(parent.webContents.sent.at(-1), {
      channel: "omnigent:update-overlay-height",
      payload: 0,
    });
    assert.equal(overlay.bounds.height, 1);
    assert.deepEqual(overlay.ignoreMouse.at(-1), {
      ignore: true,
      options: { forward: true },
    });
  });

  it("opens About before starting a download from Update now", async () => {
    const { calls, controller, handleHandlers } = makeOverlay();
    const parent = new FakeWindow();
    const overlay = controller.ensureOverlay(parent);

    await handleHandlers.get("omnigent:overlay-update-download")({ sender: overlay.webContents });

    assert.deepEqual(calls, [{ openAbout: parent }, "download"]);
    assert.equal(overlay.bounds.height, 1);
    assert.deepEqual(overlay.ignoreMouse.at(-1), {
      ignore: true,
      options: { forward: true },
    });
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
    assert.equal(overlay.bounds.height, 1);

    controller.unsuppress(parent);
    assert.equal(overlay.bounds.height, 180);
    assert.deepEqual(overlay.ignoreMouse.at(-1), { ignore: false, options: undefined });
    assert.deepEqual(parent.webContents.sent.at(-1), {
      channel: "omnigent:update-overlay-height",
      payload: 180,
    });
  });
});
