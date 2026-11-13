"use strict";

const { describe, it, mock } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { pathToFileURL } = require("node:url");
const path = require("node:path");
const fs = require("node:fs");
const vm = require("node:vm");
const { createBrowserPermissionPrompt, promptBounds } = require("../src/browserPermissionPrompt");
const {
  attachSecurityKeyNotice,
  PENDING_CHANNEL,
  SHOW_DELAY_MS,
} = require("../src/browserSecurityKeyNotice");

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
    this.webContents.isLoadingMainFrame = () => Boolean(this.loadingMainFrame);
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
    assert.deepEqual(h.info(), { kind: "permission", origin: ORIGIN, reload: true });
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
    assert.throws(() => h.choose("open-in-browser"), /Invalid permission choice/);
    h.choose("deny");
    assert.equal(await result, "deny");
  });

  it("shows a security-key notice that can only open the page in a browser or dismiss", async () => {
    const h = harness();
    const result = h.controller.show({
      parent: h.parent,
      kind: "security-key",
      origin: ORIGIN,
      cookieImport: true,
      getAnchorBounds: () => ({ x: 450, y: 64, width: 540, height: 600 }),
      signal: h.abort.signal,
    });
    h.windows[0].emit("ready-to-show");
    assert.equal(h.windows[0].visible, true);
    assert.deepEqual(h.info(), { kind: "security-key", origin: ORIGIN, cookieImport: true });
    assert.throws(() => h.choose("allow-once"), /Invalid permission choice/);
    h.choose("open-in-browser");
    assert.equal(await result, "open-in-browser");
  });

  it("defers a security-key notice until a pending permission decision is made", async () => {
    const h = harness();
    const permission = h.show();
    const deferred = h.show({ kind: "security-key" });
    assert.equal(await Promise.race([deferred, tick().then(() => "waiting")]), "waiting");
    assert.equal(h.windows.length, 1);
    const settled = new AbortController();
    const canceled = h.show({ kind: "security-key", signal: settled.signal });
    settled.abort();
    assert.equal(await Promise.race([canceled, tick().then(() => "waiting")]), "dismiss");
    h.choose("allow-once");
    assert.equal(await permission, "allow-once");
    assert.equal(await deferred, "suppressed");

    const notice = h.show({ kind: "security-key" });
    const replacement = h.show();
    assert.equal(await notice, "suppressed");
    h.choose("deny");
    assert.equal(await replacement, "deny");
  });

  it("keeps only the newest security-key notice open and sends it only its own details", async () => {
    const h = harness();
    const replaced = h.show({ kind: "security-key" });
    h.show({ kind: "security-key" });
    assert.equal(await replaced, "suppressed");
    assert.deepEqual(h.info(), { kind: "security-key", origin: ORIGIN, cookieImport: false });
    assert.deepEqual(
      h.windows.map((popup) => popup.destroyed),
      [true, false],
    );
  });

  it("brings back a security-key notice the app hid, but not one the user dismissed", async () => {
    mock.timers.enable({ apis: ["setTimeout"] });
    try {
      const h = harness();
      const contents = Object.assign(new EventEmitter(), {
        ipc: new EventEmitter(),
        mainFrame: {},
        getURL: () => `${ORIGIN}/verify`,
        isDestroyed: () => false,
      });
      let visible = true;
      attachSecurityKeyNotice(contents, {
        canPrompt: () => visible,
        showPrompt: (options) => h.show(options),
        openExternal: () => {},
        cookieImport: false,
      });
      const wait = async () => {
        await tick();
        mock.timers.tick(SHOW_DELAY_MS);
        await tick();
      };
      contents.ipc.emit(PENDING_CHANNEL, { senderFrame: contents.mainFrame }, true);
      await wait();
      assert.equal(h.windows.length, 1);
      const hideThenReturn = async (hide) => {
        visible = false;
        hide();
        await wait();
        assert.equal(h.windows.at(-1).destroyed, true);
        const shown = h.windows.length;
        visible = true;
        await wait();
        assert.equal(h.windows.length, shown + 1, "the pane is back while the request waits");
      };
      // Clicking another conversation blurs the notice before the pane switch closes it.
      await hideThenReturn(() => {
        h.windows.at(-1).emit("blur");
        h.controller.dismiss(h.parent);
      });
      await hideThenReturn(() => h.parent.emit("hide"));
      await hideThenReturn(() => h.parent.emit("minimize"));
      const beforeNavigation = h.windows.length;
      h.parent.loadingMainFrame = true;
      h.parent.webContents.emit("did-start-navigation", {}, "https://app.example", false, true);
      await wait();
      assert.equal(visible, true, "the shell stays visible while navigating");
      assert.equal(h.windows.at(-1).destroyed, true);
      assert.equal(h.windows.length, beforeNavigation, "navigation prevents immediate reopening");
      await wait();
      assert.equal(h.windows.length, beforeNavigation, "the notice waits for navigation to finish");
      h.parent.loadingMainFrame = false;
      h.parent.webContents.emit("did-stop-loading");
      await wait();
      assert.equal(h.windows.length, beforeNavigation + 1, "the notice returns after navigation");
      const beforeFailure = h.windows.length;
      const failedLoad = mock.method(Window.prototype, "loadFile", () =>
        Promise.reject(new Error("popup failed to load")),
      );
      h.windows.at(-1).webContents.emit("render-process-gone");
      await tick();
      assert.equal(h.windows.length, beforeFailure, "renderer failure waits before retrying");
      await wait();
      assert.equal(h.windows.length, beforeFailure + 1);
      assert.equal(h.windows.at(-1).destroyed, true, "failed page load closes the popup");
      await wait();
      assert.equal(h.windows.length, beforeFailure + 1, "repeated failures increase the delay");
      await wait();
      assert.equal(h.windows.length, beforeFailure + 2);
      mock.timers.tick(SHOW_DELAY_MS * 100);
      await tick();
      assert.equal(h.windows.length, beforeFailure + 2, "three failures exhaust this request");
      failedLoad.mock.restore();
      contents.ipc.emit(PENDING_CHANNEL, { senderFrame: contents.mainFrame }, false);
      contents.ipc.emit(PENDING_CHANNEL, { senderFrame: contents.mainFrame }, true);
      await wait();
      assert.equal(
        h.windows.length,
        beforeFailure + 3,
        "a new request can recover after load succeeds",
      );
      h.windows.at(-1).emit("blur");
      await wait();
      assert.equal(h.windows.at(-1).destroyed, false, "focus leaving keeps the notice");
      const beforeDismiss = h.windows.length;
      h.choose("dismiss");
      mock.timers.tick(SHOW_DELAY_MS * 2);
      await tick();
      assert.equal(h.windows.at(-1).destroyed, true);
      assert.equal(h.windows.length, beforeDismiss, "explicit dismissal does not retry");
    } finally {
      mock.restoreAll();
      mock.timers.reset();
    }
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
      kind: "permission",
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

const PAGE_BUTTONS = ["once", "always", "deny", "open-in-browser"];

function pageHarness(info = { kind: "permission", origin: ORIGIN, reload: true }) {
  const elements = new Map();
  for (const id of [
    "prompt",
    "heading",
    "description",
    "origin",
    "reload",
    "cookie-hint",
    "permission-choices",
    "security-key-choices",
    "close",
    ...PAGE_BUTTONS,
  ]) {
    elements.set(id, {
      disabled: PAGE_BUTTONS.includes(id),
      hidden: ["prompt", "reload", "cookie-hint", "security-key-choices"].includes(id),
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
      (selector === "footer button" ? PAGE_BUTTONS : [...PAGE_BUTTONS, "close"]).map((id) =>
        elements.get(id),
      ),
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
    assert.match(html, /<main[^>]+\bhidden>/);
    assert.match(html, /aria-label="Dismiss permission request"/);
    assert.doesNotMatch(html, /Okta Verify|Only allow sites you trust|Always allow and reload/);
  });

  for (const [id, choice, info] of [
    ["once", "allow-once"],
    ["always", "always-allow"],
    ["deny", "deny"],
    ["open-in-browser", "open-in-browser", { kind: "security-key", origin: ORIGIN }],
    ["close", "dismiss"],
  ]) {
    it(`maps ${id} to ${choice} and ignores repeated submissions`, async () => {
      const h = pageHarness(info);
      await tick();
      const group = info ? "security-key-choices" : "permission-choices";
      assert.equal(h.elements.get(group).hidden, false);
      h.elements.get(id).listeners.click();
      h.elements.get("close").listeners.click();
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
    const withoutReload = pageHarness({ kind: "permission", origin: ORIGIN, reload: false });
    await tick();
    assert.equal(withoutReload.elements.get("reload").hidden, true);
    assert.equal(withoutReload.elements.get("security-key-choices").hidden, true);
  });

  it("asks for the security key and offers only the browser handoff", async () => {
    const html = fs.readFileSync(path.join(__dirname, "../browser-permission/index.html"), "utf8");
    assert.ok(html.includes("Open in browser"));
    for (const cookieImport of [true, false]) {
      let resolveInfo;
      const h = pageHarness(
        new Promise((resolve) => {
          resolveInfo = resolve;
        }),
      );
      // oxlint-disable-next-line no-await-in-loop -- inspect the page before IPC settles.
      await tick();
      assert.equal(h.elements.get("prompt").hidden, true);
      resolveInfo({ kind: "security-key", origin: ORIGIN, cookieImport });
      // oxlint-disable-next-line no-await-in-loop -- each page loads its info separately.
      await tick();
      assert.equal(h.elements.get("prompt").hidden, false);
      assert.equal(h.elements.get("heading").textContent, "Use your security key");
      assert.equal(h.elements.get("close").ariaLabel, "Dismiss security key notice");
      assert.match(h.elements.get("description").textContent, /touch it to continue/);
      assert.equal(h.elements.get("origin").textContent, ORIGIN);
      assert.equal(h.elements.get("permission-choices").hidden, true);
      assert.equal(h.elements.get("security-key-choices").hidden, false);
      assert.equal(h.elements.get("cookie-hint").hidden, !cookieImport);
      assert.equal(h.elements.get("reload").hidden, true);
      assert.equal(h.elements.get("close").focused, true);
    }
  });
});
