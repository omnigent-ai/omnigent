const { it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { createConnectionLoading } = require("../src/connection_loading");

function harness() {
  const windows = [];
  class Window extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.destroyed = false;
      this.sent = [];
      this.webContents = Object.assign(new EventEmitter(), {
        setWindowOpenHandler: () => {},
        send: (_channel, label) => this.sent.push(label),
      });
      windows.push(this);
    }
    isDestroyed() {
      return this.destroyed;
    }
    destroy() {
      this.destroyed = true;
      this.emit("closed");
    }
    setIgnoreMouseEvents() {}
    setBounds(bounds) {
      this.bounds = bounds;
    }
    showInactive() {
      this.visible = true;
    }
    loadFile() {
      return Promise.resolve();
    }
  }
  const parent = Object.assign(new EventEmitter(), {
    isDestroyed: () => false,
    getContentBounds: () => ({ x: 10, y: 20, width: 1000, height: 700 }),
  });
  return { parent, windows, loading: createConnectionLoading({ BrowserWindow: Window }) };
}

it("shows the latest phase after the native loading page is ready without replacing the parent", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { parent, windows, loading } = harness();
  const attempt = {};
  loading.show(parent, attempt, "Signing in…");
  loading.show(parent, attempt, "Opening Omnigent…");
  assert.equal(windows.length, 1);
  assert.equal(windows[0].options.parent, parent);
  assert.equal(windows[0].visible, undefined);
  windows[0].webContents.emit("did-finish-load");
  assert.equal(windows[0].visible, undefined);
  t.mock.timers.tick(300);
  assert.equal(windows[0].visible, true);
  assert.deepEqual(windows[0].sent, ["Opening Omnigent…"]);
  loading.hide(parent, attempt);
  assert.equal(windows[0].destroyed, true);
  assert.equal(parent.listenerCount("resize"), 0);
});

it("a load that finishes within the show delay never shows the indicator", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { parent, windows, loading } = harness();
  const attempt = {};
  loading.show(parent, attempt, "Opening Omnigent…");
  windows[0].webContents.emit("did-finish-load");
  t.mock.timers.tick(299);
  loading.hide(parent, attempt);
  t.mock.timers.tick(1);
  assert.equal(windows[0].visible, undefined);
  assert.equal(windows[0].destroyed, true);
});

it("a superseded attempt cannot dismiss a newer window's loading state", () => {
  const { parent, windows, loading } = harness();
  const old = {},
    next = {};
  loading.show(parent, old, "Signing in…");
  loading.show(parent, next, "Opening Omnigent…");
  loading.hide(parent, old);
  assert.equal(windows[0].destroyed, false);
  loading.hide(parent, next);
  assert.equal(windows[0].destroyed, true);
});

it("closing a parent before the overlay loads leaves no orphan or late popup", () => {
  const { parent, windows, loading } = harness();
  loading.show(parent, {}, "Opening Omnigent…");
  parent.emit("closed");
  windows[0].webContents.emit("did-finish-load");
  assert.equal(windows[0].destroyed, true);
  assert.equal(windows[0].visible, undefined);
  assert.deepEqual(parent.eventNames(), []);
});

it("keeps concurrent parent windows independent", () => {
  const { parent, windows, loading } = harness();
  const other = Object.assign(new EventEmitter(), {
    isDestroyed: () => false,
    getContentBounds: parent.getContentBounds,
  });
  const first = {},
    second = {};
  loading.show(parent, first, "Opening Omnigent…");
  loading.show(other, second, "Signing in…");
  loading.hide(parent, first);
  assert.equal(windows[0].destroyed, true);
  assert.equal(windows[1].destroyed, false);
  other.emit("closed");
});
