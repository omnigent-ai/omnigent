const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const { createUpdateOverlay, OVERLAY_INSET, OVERLAY_WIDTH } = require("../src/update_overlay");

class FakeWebContents extends EventEmitter {
  constructor() {
    super();
    this.sent = [];
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

  loadFile() {
    return Promise.resolve();
  }

  destroy() {
    this.destroyed = true;
    this.emit("closed");
  }
}

function makeOverlay() {
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
  const updater = {
    getConfig: () => ({}),
    getStatus: () => ({}),
    setConfig: () => ({}),
    checkForUpdates: async () => {},
    downloadUpdate: async () => {},
    installUpdateNow: () => true,
  };
  const controller = createUpdateOverlay({
    BrowserWindow,
    ipcMain,
    nativeTheme,
    updater,
    overlayPage: "/overlay.html",
    preloadPath: "/preload.js",
  });
  controller.registerIpc();
  return { controller, handleHandlers, onHandlers, windows };
}

describe("update overlay height reservation", () => {
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
});
