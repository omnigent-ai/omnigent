"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { JSDOM } = require("jsdom");
const { createReconnectOverlay } = require("../src/reconnect_overlay");

const PAGE = path.join(__dirname, "../reconnect-overlay/index.html");
const PAGE_URL = pathToFileURL(PAGE).href;

class View {
  constructor(options) {
    this.options = options;
    this.webContents = Object.assign(new EventEmitter(), {
      destroyed: false,
      loads: [],
      focused: 0,
      isDestroyed: () => this.webContents.destroyed,
      close: () => {
        this.webContents.destroyed = true;
      },
      focus: () => this.webContents.focused++,
      setWindowOpenHandler: (handler) => {
        this.openHandler = handler;
      },
      loadFile: (file, loadOptions) => {
        this.webContents.loads.push([file, loadOptions]);
        return Promise.resolve();
      },
    });
  }
  setBackgroundColor(color) {
    this.background = color;
  }
  setBounds(bounds) {
    this.bounds = bounds;
  }
}

class Window extends EventEmitter {
  constructor() {
    super();
    this.destroyed = false;
    this.size = [1000, 700];
    this.children = [];
    this.contentView = {
      addChildView: (view) => {
        this.children = [...this.children.filter((child) => child !== view), view];
      },
      removeChildView: (view) => {
        this.children = this.children.filter((child) => child !== view);
      },
    };
  }
  isDestroyed() {
    return this.destroyed;
  }
  getContentSize() {
    return this.size;
  }
}

function harness() {
  const views = [];
  const handlers = new Map();
  const cancelled = [];
  class WebContentsView extends View {
    constructor(options) {
      super(options);
      views.push(this);
    }
  }
  const overlay = createReconnectOverlay({
    WebContentsView,
    ipcMain: { on: (name, handler) => handlers.set(name, handler) },
    overlayPage: PAGE,
    preloadPath: "/app/src/reconnect_overlay_preload.js",
    onCancel: (parent) => cancelled.push(parent),
  });
  overlay.registerIpc();
  const parent = new Window();
  const sendCancel = (view, url = PAGE_URL) =>
    handlers.get("omnigent:reconnect-overlay-cancel")({
      sender: view.webContents,
      senderFrame: { url },
    });
  return { overlay, views, parent, cancelled, sendCancel };
}

describe("reconnect overlay (src/reconnect_overlay.js)", () => {
  it("covers the window content with a transparent, isolated view on top", () => {
    const { overlay, views, parent } = harness();
    const pane = {};
    parent.contentView.addChildView(pane);
    overlay.show(parent, "Check your network connection.");
    const [view] = views;
    assert.deepEqual(parent.children, [pane, view]);
    assert.deepEqual(view.bounds, { x: 0, y: 0, width: 1000, height: 700 });
    assert.equal(view.background, "#00000000");
    assert.equal(view.options.webPreferences.contextIsolation, true);
    assert.equal(view.options.webPreferences.nodeIntegration, false);
    assert.equal(view.options.webPreferences.sandbox, true);
    assert.deepEqual(view.webContents.loads, [
      [PAGE, { query: { hint: "Check your network connection." } }],
    ]);
    assert.equal(view.webContents.focused, 1);
    assert.deepEqual(view.openHandler(), { action: "deny" });
    assert.equal(overlay.isShown(parent), true);
  });

  it("follows resizes and stays above panes attached while shown", () => {
    const { overlay, views, parent } = harness();
    overlay.show(parent, "hint");
    parent.size = [800, 500];
    parent.emit("resize");
    assert.deepEqual(views[0].bounds, { x: 0, y: 0, width: 800, height: 500 });
    const pane = {};
    parent.contentView.addChildView(pane);
    overlay.raise(parent);
    assert.deepEqual(parent.children, [pane, views[0]]);
  });

  it("reuses one view, reloading only when the hint changes", () => {
    const { overlay, views, parent } = harness();
    overlay.show(parent, "a");
    overlay.show(parent, "a");
    overlay.hide(parent);
    assert.equal(overlay.isShown(parent), false);
    assert.deepEqual(parent.children, []);
    // Raising a hidden overlay does not bring it back.
    overlay.raise(parent);
    assert.deepEqual(parent.children, []);
    overlay.show(parent, "b");
    assert.equal(views.length, 1);
    assert.deepEqual(
      views[0].webContents.loads.map(([, options]) => options.query.hint),
      ["a", "b"],
    );
  });

  it("cancels only for its own shown page", () => {
    const { overlay, views, parent, cancelled, sendCancel } = harness();
    overlay.show(parent, "hint");
    const [view] = views;
    sendCancel(view, "https://workspace.example/");
    sendCancel(new View({}));
    assert.deepEqual(cancelled, []);
    sendCancel(view, `${PAGE_URL}?hint=x`);
    assert.deepEqual(cancelled, [parent]);
    overlay.hide(parent);
    sendCancel(view);
    assert.equal(cancelled.length, 1);
  });

  it("hands focus back to the page when hidden in a focused window", () => {
    const { overlay, parent } = harness();
    let focused = 0;
    parent.isFocused = () => true;
    parent.webContents = { focus: () => focused++ };
    overlay.show(parent, "hint");
    overlay.hide(parent);
    overlay.hide(parent);
    assert.equal(focused, 1);
  });

  it("closes its page with the window", () => {
    const { overlay, views, parent } = harness();
    overlay.show(parent, "hint");
    parent.destroyed = true;
    parent.emit("closed");
    assert.equal(views[0].webContents.isDestroyed(), true);
    assert.equal(overlay.isShown(parent), false);
  });
});

describe("reconnect overlay page (reconnect-overlay/index.html)", () => {
  function render(hint) {
    const dom = new JSDOM(fs.readFileSync(PAGE, "utf8"), {
      url: `file:///app/reconnect-overlay/index.html?${new URLSearchParams({ hint })}`,
      runScripts: "outside-only",
    });
    const calls = [];
    dom.window.omnigentReconnectOverlay = { cancel: () => calls.push("cancel") };
    dom.window.eval(dom.window.document.querySelector("script").textContent);
    return { document: dom.window.document, window: dom.window, calls };
  }

  it("shows the hint as text and cancels from the button or Escape", () => {
    const { document, window, calls } = render("<b>Check your network connection.</b>");
    assert.equal(document.getElementById("title").textContent, "Reconnecting to Databricks…");
    assert.equal(
      document.getElementById("hint").textContent,
      "<b>Check your network connection.</b>",
    );
    document.getElementById("cancel").click();
    document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape" }));
    assert.deepEqual(calls, ["cancel", "cancel"]);
  });
});
