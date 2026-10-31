"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { createReturnBanner, BANNER_HEIGHT, BANNER_INSET } = require("../src/return_banner");
const {
  createSecurityKeyPrompt,
  PROMPT_WIDTH,
  PROMPT_HEIGHT,
} = require("../src/security_key_prompt");

const CONTENT = { x: 100, y: 80, width: 1000, height: 700 };

class Window extends EventEmitter {
  constructor(options = {}) {
    super();
    this.options = options;
    this.destroyed = false;
    this.visible = false;
    this.webContents = new EventEmitter();
    this.webContents.mainFrame = { name: "main" };
    this.webContents.setWindowOpenHandler = (handler) => {
      this.webContents.windowOpenHandler = handler;
    };
  }
  isDestroyed() {
    return this.destroyed;
  }
  isVisible() {
    return this.visible;
  }
  getContentBounds() {
    return CONTENT;
  }
  setBounds(bounds) {
    this.bounds = bounds;
  }
  loadFile(file) {
    this.file = file;
    return Promise.resolve();
  }
  loadURL(url) {
    this.loadedUrl = url;
    return Promise.resolve();
  }
  showInactive() {
    this.visible = true;
  }
  hide() {
    this.visible = false;
  }
  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.emit("closed");
  }
}

function harness() {
  const created = [];
  const owners = new Map();
  const handlers = new Map();
  class BrowserWindow extends Window {
    constructor(options) {
      super(options);
      created.push(this);
    }
    static fromWebContents(contents) {
      return owners.get(contents) ?? null;
    }
  }
  const shellWindow = () => {
    const win = new Window();
    owners.set(win.webContents, win);
    return win;
  };
  const ipcMain = { on: (channel, handler) => handlers.set(channel, handler) };
  const send = (channel, event, ...args) => handlers.get(channel)(event, ...args);
  return { BrowserWindow, ipcMain, created, owners, shellWindow, send };
}

function securityKeyHarness() {
  const h = harness();
  const shellWindows = new Set();
  const prompt = createSecurityKeyPrompt({
    BrowserWindow: h.BrowserWindow,
    ipcMain: h.ipcMain,
    promptPage: "/app/security-key-prompt/index.html",
    preloadPath: "/app/src/security_key_prompt_preload.js",
    isShellWindow: (win) => shellWindows.has(win),
    platform: "linux",
  });
  prompt.registerIpc();
  const win = h.shellWindow();
  shellWindows.add(win);
  prompt.attach(win);
  const fromPage = (target = win) => ({
    sender: target.webContents,
    senderFrame: target.webContents.mainFrame,
  });
  const report = (pending, event = fromPage()) =>
    h.send("omnigent:webauthn-pending", event, pending);
  return { ...h, prompt, win, shellWindows, fromPage, report };
}

describe("security-key prompt", () => {
  it("shows a bundled prompt below the return banner while the page waits", () => {
    const h = securityKeyHarness();
    h.report(true);

    assert.equal(h.created.length, 1);
    const [prompt] = h.created;
    assert.equal(prompt.visible, true);
    assert.equal(prompt.file, "/app/security-key-prompt/index.html");
    assert.equal(prompt.options.parent, h.win);
    assert.equal(prompt.options.show, false);
    assert.equal(prompt.options.webPreferences.preload, "/app/src/security_key_prompt_preload.js");
    assert.equal(prompt.options.webPreferences.contextIsolation, true);
    assert.equal(prompt.options.webPreferences.nodeIntegration, false);
    assert.equal(prompt.options.webPreferences.sandbox, true);
    assert.deepEqual(prompt.webContents.windowOpenHandler(), { action: "deny" });
    let navigationBlocked = false;
    prompt.webContents.emit("will-navigate", {
      preventDefault: () => (navigationBlocked = true),
    });
    assert.equal(navigationBlocked, true);
    assert.deepEqual(prompt.bounds, {
      x: CONTENT.x + (CONTENT.width - PROMPT_WIDTH) / 2,
      y: CONTENT.y + BANNER_INSET + BANNER_HEIGHT,
      width: PROMPT_WIDTH,
      height: PROMPT_HEIGHT,
    });

    h.report(false);
    assert.equal(prompt.visible, false);
    h.report(true);
    assert.equal(prompt.visible, true);
    assert.equal(h.created.length, 1, "the prompt window is reused");
  });

  it("ignores reports that are not from a shell window's top-level page", () => {
    const h = securityKeyHarness();
    const other = h.shellWindow();
    h.report(true, h.fromPage(other));
    h.report(true, { sender: h.win.webContents, senderFrame: { name: "iframe" } });
    h.report(true, { sender: new EventEmitter(), senderFrame: null });
    const embeddedView = new EventEmitter();
    embeddedView.mainFrame = { name: "embedded" };
    h.owners.set(embeddedView, h.win);
    h.report(true, { sender: embeddedView, senderFrame: embeddedView.mainFrame });
    h.report("true");
    assert.equal(h.created.length, 0);
  });

  it("keeps each shell window's prompt separate", () => {
    const h = securityKeyHarness();
    const other = h.shellWindow();
    h.shellWindows.add(other);
    h.report(true);
    h.report(true, h.fromPage(other));
    const [prompt, otherPrompt] = h.created;
    assert.equal(otherPrompt.options.parent, other);

    h.report(false, h.fromPage(other));
    assert.equal(otherPrompt.visible, false);
    assert.equal(prompt.visible, true);
  });

  it("hides when the user dismisses it, and only for the prompt's own sender", () => {
    const h = securityKeyHarness();
    h.report(true);
    const [prompt] = h.created;

    h.send("omnigent:security-key-prompt-dismiss", { sender: h.win.webContents });
    assert.equal(prompt.visible, true);
    h.send("omnigent:security-key-prompt-dismiss", { sender: prompt.webContents });
    assert.equal(prompt.visible, false);
    h.report(true);
    assert.equal(prompt.visible, true, "a later request re-shows a dismissed prompt");
  });

  it("hides when the page navigates or its renderer dies", () => {
    const h = securityKeyHarness();
    h.report(true);
    const [prompt] = h.created;

    h.win.webContents.emit("did-navigate");
    assert.equal(prompt.visible, false);
    h.report(true);
    h.win.webContents.emit("render-process-gone");
    assert.equal(prompt.visible, false);
  });

  it("follows the parent and closes with it", () => {
    const h = securityKeyHarness();
    h.report(true);
    const [prompt] = h.created;
    for (const event of ["resize", "move"]) {
      prompt.bounds = null;
      h.win.emit(event);
      assert.ok(prompt.bounds, `follows the parent's ${event}`);
    }

    h.win.destroyed = true;
    h.win.emit("closed");
    assert.equal(prompt.destroyed, true);
  });
});

describe("return banner", () => {
  function returnBannerHarness() {
    const h = harness();
    const goBack = [];
    const banner = createReturnBanner({
      BrowserWindow: h.BrowserWindow,
      ipcMain: h.ipcMain,
      bannerPage: "/app/return-banner/index.html",
      preloadPath: "/app/src/return_banner_preload.js",
      onGoBack: (parent) => goBack.push(parent),
      platform: "darwin",
    });
    banner.registerIpc();
    return { ...h, banner, goBack, win: h.shellWindow() };
  }

  it("shows at the top inset and returns to the recorded URL", () => {
    const h = returnBannerHarness();
    h.banner.show(h.win, "https://host.example/app");
    const [bannerWindow] = h.created;
    assert.equal(bannerWindow.visible, true);
    assert.equal(bannerWindow.file, "/app/return-banner/index.html");
    assert.equal(bannerWindow.excludedFromShownWindowsMenu, true);
    assert.equal(bannerWindow.options.webPreferences.sandbox, true);
    assert.equal(bannerWindow.bounds.y, CONTENT.y + BANNER_INSET);

    h.send("omnigent:return-banner-go-back", { sender: h.win.webContents });
    assert.equal(h.win.loadedUrl, undefined, "only the banner page can go back");
    h.send("omnigent:return-banner-go-back", { sender: bannerWindow.webContents });
    assert.equal(bannerWindow.visible, false);
    assert.deepEqual(h.goBack, [h.win]);
    assert.equal(h.win.loadedUrl, "https://host.example/app");
  });

  it("dismisses, needs a return URL, and forgets it when the banner closes", () => {
    const h = returnBannerHarness();
    h.banner.show(h.win, "");
    assert.equal(h.created.length, 0);

    h.banner.show(h.win, "https://host.example/app");
    const [bannerWindow] = h.created;
    h.send("omnigent:return-banner-dismiss", { sender: bannerWindow.webContents });
    assert.equal(bannerWindow.visible, false);

    bannerWindow.destroy();
    assert.equal(h.win.listenerCount("closed"), 0);
    const recreated = h.banner.ensureBanner(h.win);
    assert.notEqual(recreated, bannerWindow);
    h.send("omnigent:return-banner-go-back", { sender: recreated.webContents });
    assert.equal(h.win.loadedUrl, undefined);
  });

  it("keeps a replacement banner when the old one reports closed late", () => {
    const h = returnBannerHarness();
    h.banner.show(h.win, "https://host.example/app");
    const [stale] = h.created;
    stale.destroyed = true;
    h.banner.show(h.win, "https://host.example/app");
    const replacement = h.created[1];
    stale.emit("closed");

    assert.equal(h.win.listenerCount("closed"), 1, "only the replacement follows the parent");
    h.send("omnigent:return-banner-go-back", { sender: replacement.webContents });
    assert.equal(h.win.loadedUrl, "https://host.example/app");
  });
});
