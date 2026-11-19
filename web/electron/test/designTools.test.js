// The design tools' main side (src/designTools.js): openings and their overlay views over
// the page, the page operations it builds, and captures, with the page's isolated world a
// real JSDOM window.

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const { afterEach, beforeEach, describe, it } = require("node:test");
const { JSDOM } = require("jsdom");

const { runInPage } = require("./browserInputFakes");

const {
  DESIGN_TOOLS_WORLD_ID,
  FRAME_PREFIX,
  createDesignTools,
  registerDesignToolsIpc,
  sharedDesignTools,
} = require("../src/designTools");
const { registerBrowserIpc } = require("../src/browserIpc");
const { createBrowserViewRegistry } = require("../src/browserViewRegistry");
const { createBrowserViewBoundsController } = require("../src/browserViewBounds");

const tick = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });
const wait = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
/** Wait until `done()` holds (each look a few ms apart), failing after `ms`. */
async function eventually(done, ms = 1000) {
  const until = Date.now() + ms;
  while (!done()) {
    if (Date.now() > until) throw new Error("timed out waiting");
    // oxlint-disable-next-line no-await-in-loop -- look again, a few ms on
    await wait(5);
  }
}

// Main's own timers don't keep the process alive: this does, so a test awaiting one
// isn't ended before it fires (with the real timers, which a test may mock).
const { setInterval: keepAlive, clearInterval: letGo } = globalThis;
let alive;
beforeEach(() => {
  alive = keepAlive(() => {}, 1000);
});
afterEach(() => letGo(alive));

/** A page's view: its isolated world is a JSDOM window. */
function fakePage({ zoom = 1, bounds = { x: 100, y: 50, width: 800, height: 600 } } = {}) {
  const { window } = new JSDOM("<body><h1 id=heading>Welcome back</h1></body>", {
    runScripts: "outside-only",
  });
  window.document.elementFromPoint = () => window.document.getElementById("heading");
  const held = [];
  const webContents = Object.assign(new EventEmitter(), {
    codes: [],
    worlds: [],
    inputs: [],
    focused: 0,
    zoom,
    getZoomFactor: () => webContents.zoom,
    isDestroyed: () => false,
    focus: () => (webContents.focused += 1),
    sendInputEvent: (input) => webContents.inputs.push(input),
    /** Hold the next script until the returned function is called. */
    holdNext: () => {
      let release;
      held.push(
        new Promise((resolve) => {
          release = resolve;
        }),
      );
      return () => release();
    },
    executeJavaScriptInIsolatedWorld: async (world, [{ code }]) => {
      webContents.worlds.push(world);
      webContents.codes.push(code);
      const hold = held.shift();
      if (hold) await hold;
      return runInPage(window, code);
    },
    capturePage: async (rect) => {
      webContents.captured = rect;
      return (
        webContents.image ?? {
          getSize: () => ({ width: 200, height: 100 }),
          isEmpty: () => false,
          toDataURL: () => "data:image/png;base64,AAAA",
        }
      );
    },
  });
  const view = Object.assign(new EventEmitter(), {
    webContents,
    bounds,
    visible: true,
    getBounds: () => view.bounds,
    setBounds: (next) => {
      view.bounds = next;
      view.emit("bounds-changed");
    },
    getVisible: () => view.visible,
    setVisible: (next) => (view.visible = next),
  });
  return { entry: { view }, view, webContents, window };
}

/** The window a page is in: its content view's children, bottom to top. */
function fakeHost(...below) {
  const children = [...below];
  const contentView = {
    children,
    addChildView: (view, index = children.length) => {
      const at = children.indexOf(view);
      if (at >= 0) children.splice(at, 1);
      children.splice(Math.min(index, children.length), 0, view);
    },
    removeChildView: (view) => {
      const at = children.indexOf(view);
      if (at >= 0) children.splice(at, 1);
    },
  };
  // The window's app renderer.
  const webContents = Object.assign(new EventEmitter(), {
    focused: 0,
    isDestroyed: () => false,
    focus: () => (webContents.focused += 1),
  });
  return { contentView, webContents, isDestroyed: () => false };
}

/** Electron's WebContentsView, for the overlays main makes. */
function fakeViews() {
  const made = [];
  class WebContentsView {
    constructor(options) {
      this.options = options;
      this.webContents = options.webContents;
      this.visible = true;
      this.bounds = null;
      made.push(this);
    }
    setBackgroundColor(color) {
      this.background = color;
    }
    setBounds(bounds) {
      this.bounds = bounds;
    }
    setVisible(visible) {
      this.visible = visible;
    }
  }
  /** What Electron passes createWindow: the child's webContents, not yet in a view. */
  const contents = () => {
    const wc = Object.assign(new EventEmitter(), {
      zoom: 1,
      focusedNow: false,
      destroyed: false,
      openHandler: null,
      getZoomFactor: () => wc.zoom,
      setZoomFactor: (zoom) => (wc.zoom = zoom),
      isDestroyed: () => wc.destroyed,
      isFocused: () => wc.focusedNow,
      focus: () => (wc.focusedNow = true),
      setWindowOpenHandler: (handler) => (wc.openHandler = handler),
      close: () => {
        if (wc.destroyed) return;
        wc.destroyed = true;
        wc.emit("destroyed");
      },
    });
    return wc;
  };
  return { WebContentsView, made, contents };
}

/** A tool open on a page in a window, from the window's own app renderer. */
function openTool({
  zoom,
  opTimeoutMs,
  zoomCheckMs,
  above = [],
  views = fakeViews(),
  tools = createDesignTools({ WebContentsView: views.WebContentsView, opTimeoutMs, zoomCheckMs }),
} = {}) {
  const page = fakePage({ zoom });
  const host = fakeHost(page.view, ...above);
  const sent = [];
  const app = {
    send: (channel, payload) => sent.push({ channel, ...payload }),
    owner: host.webContents,
  };
  const t = {
    ...page,
    host,
    tools,
    views,
    sent,
    app,
    session: null,
    frame: null,
    /** Open a tool (or a notice); later requests name its opening. */
    start: (args = {}) => {
      const answer = tools.start("view-1", page.entry, args, app);
      if (answer.ok) [t.session, t.frame] = [answer.session, answer.frame];
      return answer;
    },
    /** The app renderer's window.open of the overlay, as Electron hands it to main. */
    windowOpen: (details = {}) =>
      tools.windowOpen(host, { url: "about:blank", frameName: t.frame, ...details }),
    /** Start, and open the overlay: the overlay view. */
    open: (args = {}) => {
      t.start(args);
      const decision = t.windowOpen();
      assert.equal(decision.action, "allow");
      decision.createWindow({ webContents: views.contents(), webPreferences: {} });
      return views.made.at(-1);
    },
    /** A request about the open tool, in its opening's name. */
    ask: (op, args = {}) => tools.handle(page.entry, { op, session: t.session, ...args }),
  };
  return t;
}

describe("design tools: the overlay", () => {
  it("opens once, for the opening its frame names, from the app that started it", () => {
    const t = openTool();
    t.start();
    assert.ok(t.frame.startsWith(FRAME_PREFIX));
    assert.equal(
      t.tools.windowOpen(t.host, { url: "https://example.com/", frameName: "_blank" }),
      null,
    );
    for (const details of [
      { frameName: `${FRAME_PREFIX}not-an-opening` },
      { url: "https://example.test/" },
    ]) {
      assert.deepEqual(t.windowOpen(details), { action: "deny" }, JSON.stringify(details));
    }
    // Another window's renderer.
    const other = fakeHost(t.view);
    assert.deepEqual(t.tools.windowOpen(other, { url: "about:blank", frameName: t.frame }), {
      action: "deny",
    });
    const decision = t.windowOpen();
    assert.equal(decision.action, "allow");
    assert.deepEqual(decision.overrideBrowserWindowOptions.webPreferences, {
      preload: path.join(__dirname, "..", "src", "popup_preload.js"),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
    });
    assert.deepEqual(t.windowOpen(), { action: "deny" }, "only once");
  });

  it("is a transparent view just above the page, with its bounds and zoom, taking the keys", () => {
    const reconnect = { name: "reconnect overlay" };
    const t = openTool({ zoom: 1.5, above: [reconnect] });
    const overlay = t.open();
    assert.deepEqual(t.host.contentView.children, [t.view, overlay, reconnect]);
    assert.equal(overlay.background, "#00000000");
    assert.deepEqual(overlay.bounds, t.view.bounds);
    assert.equal(overlay.visible, true);
    assert.equal(overlay.webContents.getZoomFactor(), 1.5);
    assert.equal(overlay.webContents.isFocused(), true);
    // It has the app's origin, so it may neither open windows nor navigate away.
    assert.deepEqual(overlay.webContents.openHandler(), { action: "deny" });
    let prevented = false;
    overlay.webContents.emit("will-navigate", { preventDefault: () => (prevented = true) });
    assert.equal(prevented, true);
  });

  it("follows the page: its bounds, zoom, window and visibility", async () => {
    const t = openTool();
    const overlay = t.open();
    t.view.setBounds({ x: 0, y: 0, width: 400, height: 300 });
    assert.deepEqual(overlay.bounds, { x: 0, y: 0, width: 400, height: 300 });
    // The overlay's document resized (the menu zoomed it): back to the page's zoom.
    overlay.webContents.setZoomFactor(2);
    await t.ask("fit");
    assert.equal(overlay.webContents.getZoomFactor(), 1);
    // The pane switched away and back.
    t.host.contentView.removeChildView(t.view);
    t.tools.viewDetached(t.host, t.view);
    assert.deepEqual(t.host.contentView.children, []);
    t.host.contentView.addChildView(t.view);
    t.tools.viewAttached(t.host, t.view);
    assert.deepEqual(t.host.contentView.children, [t.view, overlay]);
    // An app dialog hides the page in place.
    t.view.setVisible(false);
    t.tools.visibilityChanged(t.host);
    assert.equal(overlay.visible, false);
    t.view.setVisible(true);
    t.tools.visibilityChanged(t.host);
    assert.equal(overlay.visible, true);
  });

  it("gives its keys to the app while its page is out of the window, and takes them back", () => {
    const t = openTool();
    const overlay = t.open();
    const switchAway = () => {
      t.host.contentView.removeChildView(t.view);
      t.tools.viewDetached(t.host, t.view);
    };
    const switchBack = () => {
      t.host.contentView.addChildView(t.view);
      t.tools.viewAttached(t.host, t.view);
    };
    switchAway();
    assert.equal(t.host.webContents.focused, 1);
    overlay.webContents.focusedNow = false; // the app has them now
    switchBack();
    assert.equal(overlay.webContents.isFocused(), true);
    // Without the keys when its page left, it doesn't take them coming back.
    overlay.webContents.focusedNow = false;
    switchAway();
    switchBack();
    assert.equal(overlay.webContents.isFocused(), false);
    assert.equal(t.host.webContents.focused, 1);
  });

  it("takes the keys only once it's over its page, if the page left while it opened", () => {
    const t = openTool();
    t.start();
    const decision = t.windowOpen();
    t.host.contentView.removeChildView(t.view);
    decision.createWindow({ webContents: t.views.contents(), webPreferences: {} });
    const overlay = t.views.made.at(-1);
    assert.equal(overlay.webContents.isFocused(), false);
    t.host.contentView.addChildView(t.view);
    t.tools.viewAttached(t.host, t.view);
    assert.equal(overlay.webContents.isFocused(), true);
  });

  it("covers only the page's part of its view while DevTools are docked there", async () => {
    const t = openTool({ zoom: 2 });
    const overlay = t.open();
    assert.deepEqual(overlay.bounds, { x: 100, y: 50, width: 800, height: 600 });
    // DevTools open at the view's bottom: the page is 200 CSS px (400 DIPs) tall now.
    Object.defineProperty(t.window, "innerWidth", { value: 400, configurable: true });
    Object.defineProperty(t.window, "innerHeight", { value: 200, configurable: true });
    t.webContents.isDevToolsOpened = () => true;
    t.webContents.emit("devtools-opened");
    await eventually(() => overlay.bounds.height === 400);
    assert.deepEqual(overlay.bounds, { x: 100, y: 50, width: 800, height: 400 });
    const captured = await t.ask("capture", {});
    assert.deepEqual(captured.viewport, { width: 400, height: 200 });
    // Closed again: the whole view.
    t.webContents.isDevToolsOpened = () => false;
    t.webContents.emit("devtools-closed");
    await eventually(() => overlay.bounds.height === 600);
  });

  it("measures the page one at a time, and drops a measurement that outlived its opening", async () => {
    const t = openTool();
    const overlay = t.open();
    await tick();
    const measured = () => t.webContents.codes.filter((code) => code.includes("innerHeight"));
    Object.defineProperty(t.window, "innerWidth", { value: 400, configurable: true });
    Object.defineProperty(t.window, "innerHeight", { value: 200, configurable: true });
    t.webContents.isDevToolsOpened = () => true;
    const release = t.webContents.holdNext();
    t.webContents.emit("devtools-opened");
    t.webContents.emit("devtools-opened");
    t.webContents.emit("devtools-opened");
    await tick();
    assert.equal(measured().length, 1, "one measurement in flight");
    t.tools.stop(t.entry, t.session);
    // Were its overlay still there, an ended opening's late answer wouldn't move it.
    overlay.webContents.destroyed = false;
    release();
    await tick();
    await tick();
    assert.deepEqual(overlay.bounds, { x: 100, y: 50, width: 800, height: 600 });
  });

  it("forwards the recent-session shortcut from the overlay, as from the page", () => {
    const t = openTool();
    const forwarded = [];
    t.entry.forwardRecentSessionInput = (contents) => forwarded.push(contents);
    const overlay = t.open();
    assert.deepEqual(forwarded, [overlay.webContents]);
  });

  it("tells main when its overlay holds the keys, so ⌘R, ⌘W and ⌘[ act on its page", () => {
    const t = openTool();
    assert.equal(t.tools.overlayFocused(t.entry), false, "no overlay yet");
    const overlay = t.open();
    assert.equal(t.tools.overlayFocused(t.entry), true);
    overlay.webContents.focusedNow = false;
    assert.equal(t.tools.overlayFocused(t.entry), false);
    overlay.webContents.focusedNow = true;
    t.tools.stop(t.entry, t.session);
    assert.equal(t.tools.overlayFocused(t.entry), false, "closed");
    assert.equal(t.tools.overlayFocused(undefined), false);
  });

  it("names its page as the focused view while its overlay holds the keys", (t) => {
    // Main's own design tools, made with this test's views.
    const views = fakeViews();
    const Module = require("node:module");
    const load = Module["_load"];
    Module["_load"] = function (request, ...rest) {
      if (request === "electron") return { WebContentsView: views.WebContentsView };
      return load.call(this, request, ...rest);
    };
    t.after(() => {
      Module["_load"] = load;
    });
    const tool = openTool({ views, tools: sharedDesignTools() });
    const overlay = tool.open();
    t.after(() => tool.tools.stop(tool.entry, tool.session));
    tool.webContents.isFocused = () => false;
    const handlers = new Map();
    registerBrowserIpc({
      ipcMain: { handle: (channel, fn) => handlers.set(channel, fn), on: () => {} },
      isPinnedOriginSender: () => true,
      getRegistryForEvent: () => ({
        activeConversationId: () => "view-1",
        get: (id) => (id === "view-1" ? tool.entry : undefined),
      }),
    });
    const focusedView = () => handlers.get("omnigent:browser-focused-view")({ sender: {} });
    assert.equal(focusedView(), "view-1");
    overlay.webContents.focusedNow = false;
    assert.equal(focusedView(), null);
  });

  it("closes with its opening, and hands its keys to the page", () => {
    const t = openTool();
    const overlay = t.open();
    assert.deepEqual(t.tools.stop(t.entry, t.session), { ok: true, stopped: true });
    assert.deepEqual(t.host.contentView.children, [t.view]);
    assert.equal(overlay.webContents.isDestroyed(), true);
    assert.equal(t.webContents.focused, 1);
    assert.equal(t.view.listenerCount("bounds-changed"), 0);
  });

  it("gives the overlay's keys to the app, not the page, when the page ends the tool", () => {
    for (const leave of [
      (t) =>
        t.webContents.emit("did-start-navigation", { isMainFrame: true, isSameDocument: false }),
      (t) => t.webContents.emit("did-navigate-in-page", { isMainFrame: true }),
    ]) {
      const t = openTool();
      const overlay = t.open();
      overlay.webContents.focusedNow = true;
      // The old document stays live until the new one commits: it mustn't get the keys.
      leave(t);
      assert.equal(overlay.webContents.isDestroyed(), true);
      assert.equal(t.webContents.focused, 0);
      assert.equal(t.host.webContents.focused, 1);
    }
  });

  it("keeps a replaced overlay's keys in the app until the next overlay takes them", () => {
    const t = openTool();
    const overlay = t.open();
    overlay.webContents.focusedNow = true;
    t.start();
    assert.equal(overlay.webContents.isDestroyed(), true);
    assert.equal(t.webContents.focused, 0);
    assert.equal(t.host.webContents.focused, 1);
  });

  it("ends the opening, telling the app, when the overlay goes", () => {
    const t = openTool();
    const overlay = t.open();
    overlay.webContents.close();
    assert.equal(t.tools.isOpen(t.entry), false);
    // Naming its opening, so an app that has opened another since can tell it's old.
    assert.deepEqual(t.sent.at(-1), {
      channel: "browser-design-tools-event",
      viewId: "view-1",
      type: "ended",
      session: t.session,
      reason: "gone",
    });
  });

  it("shows nothing for an opening that ended while its overlay opened", async () => {
    const t = openTool();
    t.start();
    const decision = t.windowOpen();
    t.tools.stop(t.entry, t.session);
    const contents = t.views.contents();
    assert.equal(decision.createWindow({ webContents: contents, webPreferences: {} }), contents);
    assert.deepEqual(t.host.contentView.children, [t.view]);
    await tick();
    assert.equal(contents.isDestroyed(), true);
  });

  it("gives a notice an overlay of its own size, over the page's bottom, without the keys", async (c) => {
    c.mock.timers.enable({ apis: ["setInterval"] });
    const t = openTool({ zoom: 2 });
    const overlay = t.open({ input: false });
    // Not shown until the app has measured the notice in it.
    assert.equal(overlay.visible, false);
    assert.equal(overlay.webContents.isFocused(), false);
    await t.ask("fit", { notice: { width: 100, height: 30 } });
    // Page at (100, 50) 800x600; 200x60 window pixels, centred, 8 above the bottom.
    assert.deepEqual(overlay.bounds, { x: 400, y: 582, width: 200, height: 60 });
    assert.equal(overlay.visible, true);
    // A notice isn't the page's: it stays across a navigation.
    t.webContents.emit("did-start-navigation", { isMainFrame: true, isSameDocument: false });
    assert.equal(t.tools.isOpen(t.entry), true);
    // So it can't capture, read or scroll the page, only lay itself out.
    const refused = { ok: false, error: "the design tool isn't open on this page" };
    assert.deepEqual(
      await Promise.all([
        t.ask("capture", {}),
        t.ask("hitTest", { x: 1, y: 1 }),
        t.ask("scroll", { x: 1, y: 1, dx: 0, dy: 10 }),
      ]),
      [refused, refused, refused],
    );
    assert.equal(t.webContents.captured, undefined);
    assert.deepEqual(t.webContents.inputs, []);
    assert.deepEqual(await t.ask("fit", { notice: { width: 100, height: 30 } }), { ok: true });
    // Nor measure it, DevTools open or not: the page's view bounds place it.
    t.webContents.isDevToolsOpened = () => true;
    t.webContents.emit("devtools-opened");
    c.mock.timers.tick(250);
    await tick();
    assert.deepEqual(t.webContents.codes, []);
    assert.equal(t.tools.isOpen(t.entry), true);
    // Its page destroyed: it ends then, and its zoom check never asks the gone page.
    t.webContents.isDestroyed = () => true;
    t.webContents.getZoomFactor = () => {
      throw new Error("Object has been destroyed");
    };
    t.webContents.emit("destroyed");
    assert.equal(t.tools.isOpen(t.entry), false);
    assert.equal(overlay.webContents.isDestroyed(), true);
    assert.deepEqual(t.sent.at(-1), {
      channel: "browser-design-tools-event",
      viewId: "view-1",
      type: "ended",
      session: t.session,
      reason: "gone",
    });
    assert.doesNotThrow(() => c.mock.timers.tick(1000));
  });

  it("ends a notice whose page its zoom check finds destroyed, without asking it", async (c) => {
    c.mock.timers.enable({ apis: ["setInterval"] });
    const t = openTool();
    const overlay = t.open({ input: false });
    await t.ask("fit", { notice: { width: 100, height: 30 } });
    // Gone without its "destroyed" reaching main.
    t.webContents.isDestroyed = () => true;
    t.webContents.getZoomFactor = () => {
      throw new Error("Object has been destroyed");
    };
    assert.doesNotThrow(() => c.mock.timers.tick(250));
    assert.equal(t.tools.isOpen(t.entry), false);
    assert.equal(overlay.webContents.isDestroyed(), true);
    assert.equal(t.sent.at(-1).reason, "gone");
    assert.doesNotThrow(() => c.mock.timers.tick(1000));
  });

  it("ends a notice whose page crashes", () => {
    const t = openTool();
    const overlay = t.open({ input: false });
    t.webContents.emit("render-process-gone", {}, { reason: "crashed" });
    assert.equal(t.tools.isOpen(t.entry), false);
    assert.equal(overlay.webContents.isDestroyed(), true);
    assert.equal(t.sent.at(-1).reason, "gone");
    assert.equal(t.webContents.listenerCount("render-process-gone"), 0);
  });
});

describe("design tools: a new page ends the tool", () => {
  for (const [name, args, ends] of [
    [
      "a main-frame navigation",
      ["did-start-navigation", {}, "https://example.com/", false, true],
      true,
    ],
    [
      "a main-frame navigation, in Electron's details",
      [
        "did-start-navigation",
        { isMainFrame: true, isSameDocument: false, url: "https://example.com/" },
      ],
      true,
    ],
    [
      "a same-document navigation",
      ["did-navigate-in-page", {}, "https://example.com/#a", true],
      true,
    ],
    [
      "a same-document navigation, in Electron's details",
      ["did-navigate-in-page", { isMainFrame: true, url: "https://example.com/#a" }],
      true,
    ],
    // One that started before the tool opened, committing after.
    ["a new document committing", ["did-navigate", {}, "https://example.com/b", 200, "OK"], true],
    [
      "a frame's navigation",
      ["did-start-navigation", {}, "https://example.com/", false, false],
      false,
    ],
    [
      "a frame's navigation, in Electron's details",
      ["did-start-navigation", { isMainFrame: false, isSameDocument: false }],
      false,
    ],
    [
      "a frame's in-page navigation",
      ["did-navigate-in-page", {}, "https://example.com/#a", false],
      false,
    ],
  ]) {
    it(`${ends ? "ends" : "doesn't end"} on ${name}`, () => {
      const t = openTool();
      const overlay = t.open();
      t.webContents.emit(...args);
      assert.equal(t.tools.isOpen(t.entry), !ends);
      assert.equal(
        t.sent.some((e) => e.type === "ended" && e.reason === "navigated"),
        ends,
      );
      assert.equal(overlay.webContents.isDestroyed(), ends);
    });
  }

  for (const event of ["render-process-gone", "destroyed"]) {
    it(`says the page went, not that it changed, on ${event}`, () => {
      const t = openTool();
      t.open();
      t.webContents.emit(event, {}, { reason: "crashed" });
      assert.equal(t.tools.isOpen(t.entry), false);
      assert.equal(t.sent.at(-1).reason, "gone");
    });
  }
});

describe("design tools: IPC", () => {
  it("registers without loading Electron, which web-only installs don't have", () => {
    const Module = require("node:module");
    const load = Module["_load"];
    Module["_load"] = function (request, ...rest) {
      if (request === "electron") throw new Error("Cannot find module 'electron'");
      return load.call(this, request, ...rest);
    };
    try {
      const handlers = new Map();
      const ipcMain = { handle: (channel, fn) => handlers.set(channel, fn) };
      registerDesignToolsIpc({ ipcMain, gateRegistry: () => ({ error: "denied" }) });
      assert.equal(handlers.has("omnigent:browser-design-tools"), true);
    } finally {
      Module["_load"] = load;
    }
  });

  function ipc(gate) {
    const handlers = new Map();
    const ipcMain = { handle: (channel, fn) => handlers.set(channel, fn) };
    registerDesignToolsIpc({ ipcMain, gateRegistry: gate, tools: createDesignTools() });
    return handlers.get("omnigent:browser-design-tools");
  }

  it("refuses a sender outside the connected server's page", async () => {
    const call = ipc(() => ({
      error: "browser IPC is only available to the connected server's page",
    }));
    assert.deepEqual(await call({ sender: {} }, { op: "start", conversationId: "v" }), {
      ok: false,
      error: "browser IPC is only available to the connected server's page",
    });
  });

  it("starts on the sender's own view and sends the page's end to that sender", async () => {
    const page = fakePage();
    const call = ipc(() => ({ registry: { get: (id) => (id === "v" ? page.entry : null) } }));
    const received = [];
    const sender = Object.assign(new EventEmitter(), {
      send: (channel, payload) => received.push([channel, payload]),
    });
    assert.deepEqual(await call({ sender }, { op: "start", conversationId: "w" }), {
      ok: false,
      error: "No browser view",
    });
    const started = await call({ sender }, { op: "start", conversationId: "v" });
    assert.equal(started.ok, true);
    assert.equal(started.frame, `${FRAME_PREFIX}${started.session}`);
    // A stop names the opening it ends.
    assert.deepEqual(await call({ sender }, { op: "stop", conversationId: "v" }), {
      ok: false,
      error: "session is required",
    });
    assert.deepEqual(
      await call({ sender }, { op: "stop", conversationId: "v", session: "an-older-one" }),
      { ok: true, stopped: false },
    );
    page.webContents.emit("did-navigate-in-page", {}, "https://example.com/#b", true);
    assert.deepEqual(received.at(-1), [
      "browser-design-tools-event",
      { viewId: "v", type: "ended", session: started.session, reason: "navigated" },
    ]);
  });
});

describe("design tools: the app never sends the page a script", () => {
  it("refuses a script, and builds every page operation from checked values", async () => {
    const t = openTool();
    t.window.document.elementFromPoint = () => null;
    t.start();
    for (const op of ["run", "render", "start"]) {
      // oxlint-disable-next-line no-await-in-loop -- one request at a time
      assert.deepEqual(await t.ask(op, { script: "window.taken = document.cookie" }), {
        ok: false,
        error: "unknown design tools operation",
      });
    }
    for (const args of [{ x: "1); window.taken = 1; (0", y: 2 }, { x: 1 }, { x: NaN, y: 1 }]) {
      // oxlint-disable-next-line no-await-in-loop -- one request at a time
      assert.deepEqual(await t.ask("hitTest", args), {
        ok: false,
        error: "invalid design tools request",
      });
    }
    assert.deepEqual(t.webContents.codes, [], "nothing ran in the page");
    assert.equal(t.window.taken, undefined);
    assert.equal((await t.ask("hitTest", { x: 3, y: 4 })).ok, true);
    assert.match(t.webContents.codes.at(-1), /const el = at\(3, 4\);/);
    assert.deepEqual(t.webContents.worlds, [DESIGN_TOOLS_WORLD_ID]);
  });

  it("hit-tests the element at a point: what it is, where, and a key that stays its own", async () => {
    const t = openTool();
    t.start();
    const first = JSON.parse((await t.ask("hitTest", { x: 1, y: 1 })).result);
    assert.equal(first.element.tag, "h1");
    assert.equal(first.element.id, "#heading");
    assert.equal(first.element.text, "Welcome back");
    assert.deepEqual(Object.keys(first.rect), ["x", "y", "width", "height"]);
    const again = JSON.parse((await t.ask("hitTest", { x: 2, y: 2 })).result);
    assert.equal(again.key, first.key);
    // Another element, alike or not, has a key of its own.
    const other = t.window.document.createElement("h1");
    t.window.document.body.append(other);
    t.window.document.elementFromPoint = () => other;
    const third = JSON.parse((await t.ask("hitTest", { x: 3, y: 3 })).result);
    assert.notEqual(third.key, first.key);
    // Nothing (or only the page itself) there.
    t.window.document.elementFromPoint = () => t.window.document.body;
    assert.equal((await t.ask("hitTest", { x: 4, y: 4 })).result, "null");
  });

  it("scrolls what's under the wheel, at the page's zoom, as the wheel does", async () => {
    const t = openTool({ zoom: 2 });
    t.start();
    assert.deepEqual(await t.ask("scroll", { x: 10, y: 20, dx: 0, dy: "1" }), {
      ok: false,
      error: "invalid design tools request",
    });
    assert.equal((await t.ask("scroll", { x: 10, y: 20, dx: 3, dy: 40 })).ok, true);
    assert.deepEqual(t.webContents.inputs, [
      { type: "mouseWheel", x: 20, y: 40, deltaX: -6, deltaY: -80, canScroll: true },
    ]);
    // Its modifier keys go with it (Ctrl+wheel zooms), and only ones a wheel can have.
    await t.ask("scroll", { x: 10, y: 20, dx: 0, dy: 40, modifiers: ["control", "x", "control"] });
    assert.deepEqual(t.webContents.inputs.at(-1).modifiers, ["control"]);
  });

  it("follows the page's zoom, and refuses a point measured at the old one", async () => {
    const t = openTool({ zoomCheckMs: 5 });
    const overlay = t.open();
    t.webContents.zoom = 2;
    for (const [op, args] of [
      ["hitTest", { x: 1, y: 1 }],
      ["scroll", { x: 1, y: 1, dx: 0, dy: 1 }],
      ["capture", {}],
    ]) {
      // oxlint-disable-next-line no-await-in-loop -- each after the overlay caught up
      assert.deepEqual(await t.ask(op, args), { ok: false, error: "the page's zoom changed" });
      assert.equal(overlay.webContents.zoom, t.webContents.zoom, "the overlay at the page's again");
      t.webContents.zoom = t.webContents.zoom === 2 ? 1 : 2;
    }
    // Without a request: it checks while the tool is open.
    await wait(40);
    assert.equal(overlay.webContents.zoom, t.webContents.zoom);
    assert.equal((await t.ask("hitTest", { x: 1, y: 1 })).ok, true);
  });

  it("fails an operation the page never answers", async () => {
    const t = openTool({ opTimeoutMs: 20 });
    t.start();
    t.webContents.holdNext();
    await assert.rejects(t.ask("hitTest", { x: 1, y: 1 }), /the page didn't answer/);
  });
});

describe("design tools: openings", () => {
  it("does nothing for an opening that was replaced", async () => {
    const t = openTool();
    t.start();
    const old = t.session;
    // Another pane on the same view opens its own.
    t.start();
    assert.notEqual(t.session, old);
    assert.deepEqual(t.tools.stop(t.entry, old), { ok: true, stopped: false });
    assert.equal(t.tools.isOpen(t.entry), true);
    for (const op of ["fit", "capture", "hitTest", "scroll"]) {
      // oxlint-disable-next-line no-await-in-loop -- one request at a time
      const answer = await t.tools.handle(t.entry, { op, session: old, x: 1, y: 1, dx: 0, dy: 1 });
      assert.deepEqual(answer, { ok: false, error: "the design tool isn't open on this page" });
    }
    assert.deepEqual(t.tools.stop(t.entry, t.session), { ok: true, stopped: true });
  });
});

describe("design tools: capture", () => {
  it("is the page alone, at its zoom, with its viewport, and keeps a crop inside it", async () => {
    const t = openTool({ zoom: 2 });
    t.open();
    const answer = await t.ask("capture", { rect: { x: 10, y: 20, width: 1000, height: 30 } });
    assert.equal(answer.ok, true);
    assert.deepEqual(answer.viewport, { width: 400, height: 300 });
    assert.deepEqual(t.webContents.captured, { x: 20, y: 40, width: 780, height: 60 });
    assert.deepEqual(t.webContents.codes, [], "nothing to hide in the page first");
  });

  it("crops a rect off the top or left to what's left of it", async () => {
    const t = openTool();
    t.open();
    await t.ask("capture", { rect: { x: -10, y: -5, width: 50, height: 30 } });
    assert.deepEqual(t.webContents.captured, { x: 0, y: 0, width: 40, height: 25 });
  });

  it("refuses a rect with nothing of it in the viewport", async () => {
    const t = openTool();
    t.open();
    const nothing = { ok: false, error: "nothing to capture there" };
    for (const rect of [
      { x: 800, y: 20, width: 50, height: 30 },
      { x: 10, y: 600, width: 50, height: 30 },
      { x: -60, y: 20, width: 50, height: 30 },
      { x: 10, y: -30, width: 50, height: 30 },
    ]) {
      // oxlint-disable-next-line no-await-in-loop -- one capture at a time
      assert.deepEqual(await t.ask("capture", { rect }), nothing, JSON.stringify(rect));
    }
    assert.equal(t.webContents.captured, undefined, "nothing was captured");
  });

  it("refuses a capture the page zoomed during, whether it was measuring or painting", async () => {
    const t = openTool();
    t.open();
    // DevTools docked: the capture measures the page's area first, and that's held.
    t.webContents.isDevToolsOpened = () => true;
    const release = t.webContents.holdNext();
    const measuring = t.ask("capture", { rect: { x: 10, y: 20, width: 50, height: 30 } });
    await tick();
    assert.equal(t.webContents.codes.filter((code) => code.includes("innerHeight")).length, 1);
    t.webContents.zoom = 2;
    release();
    assert.deepEqual(await measuring, { ok: false, error: "the page's zoom changed" });
    // The overlay at the new zoom; the page zooms back while it paints.
    await t.ask("fit");
    const paint = t.webContents.capturePage;
    let painted = 0;
    t.webContents.capturePage = async (rect) => {
      painted += 1;
      t.webContents.zoom = 1;
      return paint(rect);
    };
    assert.deepEqual(await t.ask("capture"), { ok: false, error: "the page's zoom changed" });
    assert.equal(painted, 1);
  });

  it("answers nothing for an opening that ended while the page painted or answered", async () => {
    const t = openTool();
    t.open();
    const paint = t.webContents.capturePage;
    t.webContents.capturePage = async (rect) => {
      // The page navigates mid-capture: the image may be the next document's.
      t.webContents.emit("did-start-navigation", { isMainFrame: true, isSameDocument: false });
      return paint(rect);
    };
    const refused = { ok: false, error: "the design tool isn't open on this page" };
    assert.deepEqual(await t.ask("capture"), refused);
    const u = openTool();
    u.open();
    const release = u.webContents.holdNext();
    const hit = u.ask("hitTest", { x: 1, y: 1 });
    await tick();
    u.webContents.emit("did-start-navigation", { isMainFrame: true, isSameDocument: false });
    release();
    assert.deepEqual(await hit, refused);
  });

  it("fails a capture the page never paints", async () => {
    const t = openTool({ opTimeoutMs: 20 });
    t.open();
    t.webContents.capturePage = () => new Promise(() => {});
    await assert.rejects(t.ask("capture"), /the page didn't answer/);
  });

  it("refuses when the view isn't shown, or paints nothing", async () => {
    const t = openTool();
    t.open();
    t.view.setVisible(false);
    assert.deepEqual(await t.ask("capture"), { ok: false, error: "empty capture" });
    t.view.setVisible(true);
    t.webContents.image = { getSize: () => ({ width: 0, height: 0 }), isEmpty: () => true };
    assert.deepEqual(await t.ask("capture"), { ok: false, error: "empty capture" });
  });
});

describe("design tools: the app that opened it", () => {
  for (const [name, event] of [
    ["reloads or navigates", ["did-navigate", {}, "https://app.example.com/"]],
    ["crashes", ["render-process-gone", {}, { reason: "crashed" }]],
    ["goes away", ["destroyed"]],
  ]) {
    it(`ends the opening and its overlay when it ${name}`, async () => {
      const t = openTool();
      const overlay = t.open();
      overlay.webContents.focusedNow = true;
      t.host.webContents.emit(...event);
      await tick();
      assert.equal(t.tools.isOpen(t.entry), false);
      assert.equal(overlay.webContents.isDestroyed(), true);
      // Keys meant for the tool don't go on to the page.
      assert.equal(t.webContents.focused, 0);
      assert.deepEqual(t.host.contentView.children, [t.view]);
      assert.equal(t.host.webContents.listenerCount("did-navigate"), 0);
    });
  }

  it("doesn't open for an app that's already gone", () => {
    const t = openTool();
    t.app.owner = Object.assign(new EventEmitter(), { isDestroyed: () => true });
    assert.equal(t.start().ok, false);
    assert.equal(t.tools.isOpen(t.entry), false);
  });

  it("ignores an app navigating within its page", () => {
    const t = openTool();
    t.start();
    t.host.webContents.emit("did-navigate-in-page", {}, "https://app.example.com/#x", true);
    assert.equal(t.tools.isOpen(t.entry), true);
  });
});

describe("design tools: the recent-session shortcut", () => {
  it("forwards the gesture from another view taking the page's keys (the overlay)", () => {
    const sent = [];
    const webContents = {
      loadURL() {},
      close() {},
      removeListener() {},
      on() {},
      setWindowOpenHandler() {},
    };
    const registry = createBrowserViewRegistry({
      WebContentsViewCtor: () => ({ setBounds() {}, webContents }),
      createBoundsController: createBrowserViewBoundsController,
      attachToHost() {},
      detachFromHost() {},
      sendToRenderer: (channel, payload) => sent.push({ channel, payload }),
      isHostFocused: () => true,
    });
    registry.openOrNavigate("conv_1", "https://example.com");
    registry.setActive("conv_1");
    registry.setRecentSessionSwitchSupported(true);
    const overlayListeners = new Map();
    const overlay = { on: (name, listener) => overlayListeners.set(name, listener) };
    registry.get("conv_1").forwardRecentSessionInput(overlay);
    const prevented = [];
    overlayListeners.get("before-input-event")(
      { preventDefault: () => prevented.push(true) },
      { type: "keyDown", key: "Tab", code: "Tab", control: true },
    );
    const forwarded = sent.filter((item) => item.channel === "browser-recent-session-input");
    assert.deepEqual(
      forwarded.map((item) => [item.payload.type, item.payload.key]),
      [["keydown", "Tab"]],
    );
    assert.equal(prevented.length, 1);
  });
});
