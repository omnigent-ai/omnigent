"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { pathToFileURL } = require("node:url");
const path = require("node:path");
const fs = require("node:fs");
const vm = require("node:vm");
const { createBrowserPermissionPrompt, promptBounds } = require("../src/browserPermissionPrompt");

const PAGE = "/app/browser-permission/index.html";
const ORIGIN = "https://login.example.com";
const tick = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });

class Window extends EventEmitter {
  constructor(options = {}) {
    super();
    this.options = options;
    this.destroyed = false;
    this.visible = false;
    this.webContents = new EventEmitter();
    this.webContents.mainFrame = { url: "" };
    this.webContents.isDestroyed = () => this.destroyed;
    this.webContents.setWindowOpenHandler = (handler) => {
      this.openHandler = handler;
    };
    this.webContents.session = {
      setPermissionRequestHandler: (handler) => {
        this.permissionRequest = handler;
      },
      setPermissionCheckHandler: (handler) => {
        this.permissionCheck = handler;
      },
    };
  }
  isDestroyed() {
    return this.destroyed;
  }
  getContentBounds() {
    return { x: 100, y: 80, width: 1000, height: 700 };
  }
  setBounds(bounds) {
    this.bounds = bounds;
  }
  loadFile(file) {
    this.file = file;
    this.webContents.mainFrame.url = pathToFileURL(file).href;
    return Promise.resolve();
  }
  show() {
    this.visible = true;
  }
  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.emit("closed");
  }
}

function harness() {
  const windows = [];
  const handlers = new Map();
  class BrowserWindow extends Window {
    constructor(options) {
      super(options);
      windows.push(this);
    }
  }
  const controller = createBrowserPermissionPrompt({
    BrowserWindow,
    ipcMain: { handle: (name, handler) => handlers.set(name, handler) },
    promptPage: PAGE,
    preloadPath: "/app/src/browser_permission_preload.js",
  });
  controller.registerIpc();
  const parent = new Window();
  const abort = new AbortController();
  const show = (overrides = {}) =>
    controller.show({
      parent,
      origin: ORIGIN,
      reload: true,
      getAnchorBounds: () => ({ x: 450, y: 64, width: 540, height: 600 }),
      signal: abort.signal,
      ...overrides,
    });
  const event = (win = windows.at(-1)) => ({
    sender: win.webContents,
    senderFrame: win.webContents.mainFrame,
  });
  return {
    controller,
    windows,
    parent,
    abort,
    show,
    event,
    info: (ev = event()) => handlers.get("omnigent:browser-permission-info")(ev),
    choose: (choice, ev = event()) =>
      handlers.get("omnigent:browser-permission-choose")(ev, choice),
  };
}

describe("browser permission popover", () => {
  it("uses a small shell-owned, isolated child window under the browser toolbar", async () => {
    const h = harness();
    const result = h.show();
    const popup = h.windows[0];
    assert.equal(popup.options.parent, h.parent);
    assert.equal(popup.options.frame, false);
    assert.equal(popup.options.transparent, true);
    assert.equal(popup.options.modal, undefined, "not a blocking system dialog");
    assert.deepEqual(popup.options.webPreferences, {
      preload: "/app/src/browser_permission_preload.js",
      partition: "omnigent-browser-permission-ui",
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      devTools: false,
    });
    assert.equal(popup.file, PAGE);
    assert.equal(popup.visible, false);
    popup.emit("ready-to-show");
    assert.equal(popup.visible, true);
    assert.deepEqual(popup.bounds, { x: 638, y: 140, width: 380, height: 250 });
    assert.deepEqual(h.info(), { origin: ORIGIN, reload: true });
    assert.deepEqual(popup.openHandler(), { action: "deny" });
    assert.equal(popup.permissionCheck(), false);
    popup.permissionRequest(null, "media", (granted) => assert.equal(granted, false));
    let prevented = 0;
    popup.webContents.emit("will-navigate", { preventDefault: () => prevented++ });
    popup.webContents.emit("will-frame-navigate", { preventDefault: () => prevented++ });
    assert.equal(prevented, 2);
    h.choose("dismiss");
    assert.equal(await result, "dismiss");
  });

  for (const choice of ["allow-once", "always-allow", "deny", "dismiss"]) {
    it(`accepts ${choice} only from its own main frame and settles once`, async () => {
      const h = harness();
      const result = h.show();
      const event = h.event();
      h.choose(choice);
      assert.equal(await result, choice);
      assert.equal(h.windows[0].destroyed, true);
      assert.throws(() => h.choose("always-allow", event), /bundled permission UI/);
      assert.equal(h.parent.listenerCount("closed"), 0);
      assert.equal(h.parent.listenerCount("resize"), 0);
      assert.equal(h.parent.webContents.listenerCount("did-start-navigation"), 0);
    });
  }

  it("rejects a site/server renderer, a matching-URL impostor, subframes, and invalid choices", async () => {
    const h = harness();
    const result = h.show();
    const event = h.event();
    const foreign = new Window();
    foreign.webContents.mainFrame.url = pathToFileURL(PAGE).href;
    assert.throws(() => h.choose("always-allow", h.event(foreign)), /bundled permission UI/);
    assert.throws(
      () => h.info({ ...event, senderFrame: { ...event.senderFrame } }),
      /bundled permission UI/,
    );
    event.senderFrame.url = "https://login.example.com";
    assert.throws(() => h.choose("always-allow", event), /bundled permission UI/);
    event.senderFrame.url = pathToFileURL(PAGE).href;
    assert.throws(() => h.choose("allow-everything"), /Invalid permission choice/);
    h.choose("deny");
    assert.equal(await result, "deny");
  });

  for (const [name, close] of [
    ["clicking outside", (h) => h.windows[0].emit("blur")],
    ["pane navigation", (h) => h.abort.abort()],
    ["parent closing", (h) => h.parent.destroy()],
    ["parent hiding", (h) => h.parent.emit("hide")],
    ["parent minimizing", (h) => h.parent.emit("minimize")],
    ["renderer crash", (h) => h.windows[0].webContents.emit("render-process-gone")],
    [
      "shell navigation",
      (h) =>
        h.parent.webContents.emit(
          "did-start-navigation",
          {},
          "https://elsewhere.example",
          false,
          true,
        ),
    ],
  ]) {
    it(`dismisses without approval on ${name}`, async () => {
      const h = harness();
      const result = h.show();
      close(h);
      assert.equal(await result, "dismiss");
      assert.equal(h.windows[0].destroyed, true);
      h.windows[0].emit("ready-to-show");
      assert.equal(h.windows[0].visible, false);
    });
  }

  it("does not create a window for an already-canceled request", async () => {
    const h = harness();
    h.abort.abort();
    assert.equal(await h.show(), "dismiss");
    assert.equal(h.windows.length, 0);
  });

  it("replaces a prompt in the same window without affecting another window's prompt", async () => {
    const h = harness();
    const first = h.show();
    const another = h.show({ parent: new Window(), origin: "https://another.example" });
    const second = h.show();
    assert.equal(await first, "dismiss");
    assert.deepEqual(h.info(h.event(h.windows[1])), {
      origin: "https://another.example",
      reload: true,
    });
    h.choose("always-allow", h.event(h.windows[1]));
    assert.equal(await another, "always-allow");
    h.controller.dismiss(h.parent);
    assert.equal(await second, "dismiss");
  });

  it("clamps the popover to its parent and repositions it when the parent moves", async () => {
    assert.deepEqual(promptBounds({ x: 0, y: 0, width: 300, height: 200 }, { x: 900, y: 600 }), {
      x: 0,
      y: 0,
      width: 300,
      height: 200,
    });
    const h = harness();
    const result = h.show();
    h.parent.emit("resize");
    assert.deepEqual(h.windows[0].bounds, { x: 638, y: 140, width: 380, height: 250 });
    h.choose("deny");
    await result;
  });
});

function pageHarness(info = { origin: ORIGIN, reload: true }) {
  const elements = new Map();
  for (const id of ["origin", "reload", "once", "always", "deny", "close"]) {
    elements.set(id, {
      disabled: id !== "close",
      listeners: {},
      addEventListener(name, fn) {
        this.listeners[name] = fn;
      },
      focus() {
        this.focused = true;
      },
    });
  }
  const events = {};
  const choices = [];
  const document = {
    getElementById: (id) => elements.get(id),
    querySelectorAll: (selector) =>
      (selector === "footer button"
        ? ["once", "always", "deny"]
        : ["once", "always", "deny", "close"]
      ).map((id) => elements.get(id)),
    addEventListener: (name, fn) => {
      events[name] = fn;
    },
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "../browser-permission/index.js"), "utf8"),
    {
      document,
      window: {
        omnigentBrowserPermission: {
          getInfo: async () => info,
          choose: async (choice) => {
            choices.push(choice);
          },
        },
      },
    },
  );
  return { elements, events, choices };
}

describe("permission popover page", () => {
  it("packages the compact UI with exactly the requested three choices", () => {
    const html = fs.readFileSync(path.join(__dirname, "../browser-permission/index.html"), "utf8");
    assert.ok(require("../package.json").build.files.includes("browser-permission/**/*"));
    for (const label of ["Allow once", "Always allow", "Deny"]) assert.ok(html.includes(label));
    assert.match(html, /default-src 'none'/);
    assert.doesNotMatch(html, /Okta Verify|Only allow sites you trust|Always allow and reload/);
  });

  for (const [id, choice] of [
    ["once", "allow-once"],
    ["always", "always-allow"],
    ["deny", "deny"],
    ["close", "dismiss"],
  ]) {
    it(`maps ${id} to ${choice} and ignores repeated submissions`, async () => {
      const h = pageHarness();
      await tick();
      h.elements.get(id).listeners.click();
      h.elements.get("always").listeners.click();
      assert.deepEqual(h.choices, [choice]);
    });
  }

  it("renders the origin as text, shows the reload hint, and focuses dismissal instead of approval", async () => {
    const origin = "https://long.example/<not-markup>";
    const h = pageHarness({ origin, reload: true });
    await tick();
    assert.equal(h.elements.get("origin").textContent, origin);
    assert.equal(h.elements.get("reload").hidden, false);
    assert.equal(h.elements.get("close").focused, true);
    h.events.keydown({ key: "Escape" });
    assert.deepEqual(h.choices, ["dismiss"]);
    const withoutReload = pageHarness({ origin: ORIGIN, reload: false });
    await tick();
    assert.equal(withoutReload.elements.get("reload").hidden, true);
  });
});
