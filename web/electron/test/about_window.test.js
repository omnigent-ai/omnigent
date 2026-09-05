const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const {
  createAboutWindow,
  resolveAppIconDataUrl,
  platformDisplayName,
  ABOUT_HEIGHT,
  ABOUT_WIDTH,
} = require("../src/about_window");

const ABOUT_PAGE = "/app/about/index.html";

class FakeWebContents extends EventEmitter {
  constructor() {
    super();
    this.url = "";
    this.windowOpenHandler = null;
  }

  getURL() {
    return this.url;
  }

  setWindowOpenHandler(handler) {
    this.windowOpenHandler = handler;
  }
}

class FakeWindow extends EventEmitter {
  constructor(options = {}) {
    super();
    this.options = options;
    this.webContents = new FakeWebContents();
    this.destroyed = false;
    this.visible = false;
    this.focused = false;
    this.minimized = false;
    this.loadedFile = null;
  }

  isDestroyed() {
    return this.destroyed;
  }

  isMinimized() {
    return this.minimized;
  }

  isVisible() {
    return this.visible;
  }

  restore() {
    this.minimized = false;
  }

  show() {
    this.visible = true;
  }

  focus() {
    this.focused = true;
  }

  loadFile(file) {
    this.loadedFile = file;
    this.webContents.url = pathToFileURL(file).href;
    return Promise.resolve();
  }

  close() {
    this.destroy();
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.emit("closed");
  }
}

function makeAbout({
  cliStatus,
  desktopStatus,
  installReady = true,
  appIconDataUrl = "data:image/png;base64,native-icon",
  platform = "darwin",
} = {}) {
  const windows = [];
  class BrowserWindow extends FakeWindow {
    constructor(options) {
      super(options);
      windows.push(this);
    }
  }
  const handleHandlers = new Map();
  const onHandlers = new Map();
  const calls = {
    desktopChecks: [],
    desktopDownloads: 0,
    desktopDownloadParents: [],
    desktopInstalls: 0,
    closedParents: [],
  };
  const updater = {
    getStatus: () => desktopStatus ?? { state: "none" },
    checkForUpdates: async (options) => {
      calls.desktopChecks.push(options);
    },
    downloadUpdate: async () => {
      calls.desktopDownloads += 1;
    },
    installUpdateNow: () => {
      calls.desktopInstalls += 1;
      return installReady;
    },
  };
  const controller = createAboutWindow({
    BrowserWindow,
    ipcMain: {
      handle: (channel, handler) => handleHandlers.set(channel, handler),
      on: (channel, handler) => onHandlers.set(channel, handler),
    },
    nativeTheme: { shouldUseDarkColors: false },
    updater,
    getDesktopVersion: () => "0.13.0-dev.0",
    getAppIconDataUrl: async () => appIconDataUrl,
    getCliStatus: async () =>
      cliStatus ?? {
        installed: true,
        path: "/Users/alice/.local/bin/omnigent",
        version: "omnigent 0.13.0.dev0",
        source: "candidate",
        installCommand: "install omnigent",
      },
    onDesktopDownloadStarted: (parent) => calls.desktopDownloadParents.push(parent),
    onClosed: (parent) => calls.closedParents.push(parent),
    aboutPage: ABOUT_PAGE,
    preloadPath: "/app/src/about_preload.js",
    platform,
  });
  controller.registerIpc();
  return { calls, controller, handleHandlers, onHandlers, windows };
}

function eventFor(win, url = pathToFileURL(ABOUT_PAGE).href) {
  return { sender: win.webContents, senderFrame: { url } };
}

function fakeImage(dataUrl, empty = false) {
  return {
    isEmpty: () => empty,
    toDataURL: (options) => `${dataUrl}:${options.scaleFactor}x`,
  };
}

describe("platformDisplayName", () => {
  it("uses the native platform name", () => {
    assert.equal(platformDisplayName("darwin"), "macOS");
    assert.equal(platformDisplayName("win32"), "Windows");
    assert.equal(platformDisplayName("linux"), "Linux");
    assert.equal(platformDisplayName("freebsd"), "Desktop");
  });
});

describe("resolveAppIconDataUrl", () => {
  it("uses the native running-application icon for packaged macOS", async () => {
    const calls = [];
    const result = await resolveAppIconDataUrl({
      app: {
        isPackaged: true,
        getFileIcon: async () => {
          calls.push("file");
          return fakeImage("file");
        },
      },
      nativeImage: {
        createFromNamedImage: (name) => {
          calls.push(name);
          return fakeImage("native");
        },
        createFromPath: () => fakeImage("fallback"),
      },
      platform: "darwin",
      executablePath: "/Applications/Omnigent.app/Contents/MacOS/Omnigent",
      fallbackIconPath: "/app/icons/icon.png",
    });

    assert.equal(result, "native:2x");
    assert.deepEqual(calls, ["NSApplicationIcon"]);
  });

  it("falls back from the named macOS icon to the app bundle icon", async () => {
    const result = await resolveAppIconDataUrl({
      app: { isPackaged: true, getFileIcon: async () => fakeImage("bundle") },
      nativeImage: {
        createFromNamedImage: () => fakeImage("empty", true),
        createFromPath: () => fakeImage("fallback"),
      },
      platform: "darwin",
      executablePath: "/Applications/Omnigent.app/Contents/MacOS/Omnigent",
      fallbackIconPath: "/app/icons/icon.png",
    });

    assert.equal(result, "bundle:2x");
  });

  it("uses the bundled PNG in development", async () => {
    const calls = [];
    const result = await resolveAppIconDataUrl({
      app: { isPackaged: false, getFileIcon: async () => fakeImage("file") },
      nativeImage: {
        createFromNamedImage: () => fakeImage("native"),
        createFromPath: (iconPath) => {
          calls.push(iconPath);
          return fakeImage("fallback");
        },
      },
      platform: "darwin",
      executablePath: "/Electron",
      fallbackIconPath: "/app/icons/icon.png",
    });

    assert.equal(result, "fallback:2x");
    assert.deepEqual(calls, ["/app/icons/icon.png"]);
  });
});

describe("About window", () => {
  it("packages the bundled About page with button and Escape close controls", () => {
    const packageConfig = require("../package.json");
    const html = fs.readFileSync(path.join(__dirname, "../about/index.html"), "utf8");
    const script = fs.readFileSync(path.join(__dirname, "../about/index.js"), "utf8");
    const preload = fs.readFileSync(path.join(__dirname, "../src/about_preload.js"), "utf8");

    assert.ok(packageConfig.build.files.includes("about/**/*"));
    assert.match(html, /img-src 'self' data:/);
    assert.match(html, /id="app-icon"/);
    assert.match(html, /id="desktop-update-now"[^>]*>Update now<\/button>/);
    assert.match(html, /aria-label="Update download progress"/);
    assert.match(html, /id="desktop-restart"[^>]*>Restart to update<\/button>/);
    assert.match(html, /Run <code>omni upgrade<\/code> to update\./);
    assert.match(html, /id="close"[^>]*>Close<\/button>/);
    assert.match(script, /info\.appIconDataUrl\.startsWith\("data:image\/"\)/);
    assert.match(script, /desktopHeading\.textContent = `Omnigent \$\{platformName\} app`/);
    assert.match(script, /event\.key !== "Escape"/);
    assert.match(script, /api\?\.close\(\)/);
    assert.match(preload, /downloadDesktopUpdate:[\s\S]*about-download-desktop-update/);
    assert.match(preload, /installDesktopUpdate:[\s\S]*about-install-desktop-update/);
    assert.match(preload, /onDesktopUpdateStatus:[\s\S]*omnigent:update-status/);
    assert.match(preload, /close:\s*\(\)\s*=>\s*ipcRenderer\.send\("omnigent:about-close"\)/);
  });

  it("opens a shell-owned modal and reuses it", () => {
    const h = makeAbout();
    const parent = new FakeWindow();
    const first = h.controller.open(parent);

    assert.equal(h.windows.length, 1);
    assert.equal(first.options.parent, parent);
    assert.equal(first.options.modal, true);
    assert.equal(first.options.width, ABOUT_WIDTH);
    assert.equal(first.options.height, ABOUT_HEIGHT);
    assert.equal(first.options.webPreferences.contextIsolation, true);
    assert.equal(first.options.webPreferences.nodeIntegration, false);
    assert.equal(first.options.webPreferences.sandbox, true);
    assert.equal(first.loadedFile, ABOUT_PAGE);
    assert.equal(first.excludedFromShownWindowsMenu, true);
    assert.deepEqual(first.webContents.windowOpenHandler(), { action: "deny" });

    first.emit("ready-to-show");
    assert.equal(first.visible, true);

    first.minimized = true;
    assert.equal(h.controller.open(parent), first);
    assert.equal(h.windows.length, 1);
    assert.equal(first.minimized, false);
    assert.equal(first.focused, true);
  });

  it("reparents the singleton when another app window opens About", () => {
    const h = makeAbout();
    const firstParent = new FakeWindow();
    const secondParent = new FakeWindow();
    const first = h.controller.open(firstParent);

    const second = h.controller.open(secondParent);

    assert.equal(first.destroyed, true);
    assert.notEqual(second, first);
    assert.equal(second.options.parent, secondParent);
    assert.deepEqual(h.calls.closedParents, [firstParent]);
  });

  it("opens without a parent when macOS has no main windows", () => {
    const h = makeAbout();
    const about = h.controller.open(null);

    assert.equal(about.options.parent, undefined);
    assert.equal(about.options.modal, false);
    about.emit("ready-to-show");
    assert.equal(about.visible, true);
  });

  it("closes with its parent and can be reopened", () => {
    const h = makeAbout();
    const parent = new FakeWindow();
    const first = h.controller.open(parent);

    parent.emit("closed");
    assert.equal(first.destroyed, true);

    const second = h.controller.open(new FakeWindow());
    assert.notEqual(second, first);
    assert.equal(h.windows.length, 2);
  });

  it("accepts IPC only from the tracked bundled page", async () => {
    const h = makeAbout();
    const about = h.controller.open(new FakeWindow());
    const getInfo = h.handleHandlers.get("omnigent:about-get-info");

    assert.deepEqual(await getInfo(eventFor(about)), {
      desktopVersion: "0.13.0-dev.0",
      platformName: "macOS",
      appIconDataUrl: "data:image/png;base64,native-icon",
      cli: {
        installed: true,
        path: "/Users/alice/.local/bin/omnigent",
        version: "omnigent 0.13.0.dev0",
        source: "candidate",
        installCommand: "install omnigent",
      },
    });
    const wrongPage = eventFor(about, "https://evil.example/");
    await Promise.all(
      [
        "omnigent:about-get-info",
        "omnigent:about-get-desktop-update-status",
        "omnigent:about-check-desktop",
        "omnigent:about-download-desktop-update",
        "omnigent:about-install-desktop-update",
      ].map((channel) =>
        assert.rejects(
          Promise.resolve().then(() => h.handleHandlers.get(channel)(wrongPage)),
          /bundled About/,
        ),
      ),
    );
    await assert.rejects(
      getInfo({
        sender: new FakeWebContents(),
        senderFrame: { url: pathToFileURL(ABOUT_PAGE).href },
      }),
      /bundled About/,
    );
  });

  it("closes only when the bundled About page requests it", () => {
    const h = makeAbout();
    const parent = new FakeWindow();
    const about = h.controller.open(parent);
    const close = h.onHandlers.get("omnigent:about-close");

    close(eventFor(about, "https://evil.example/"));
    assert.equal(about.destroyed, false);

    close(eventFor(about));
    assert.equal(about.destroyed, true);
    assert.deepEqual(h.calls.closedParents, [parent]);
  });

  it("delegates the full desktop update flow and returns raw updater status", async () => {
    const status = { state: "available", info: { version: "0.14.0" } };
    const h = makeAbout({ desktopStatus: status });
    const parent = new FakeWindow();
    const about = h.controller.open(parent);
    const event = eventFor(about);

    assert.deepEqual(
      await h.handleHandlers.get("omnigent:about-get-desktop-update-status")(event),
      status,
    );
    assert.deepEqual(await h.handleHandlers.get("omnigent:about-check-desktop")(event), status);
    assert.deepEqual(h.calls.desktopChecks, [{ manual: true }]);

    await h.handleHandlers.get("omnigent:about-download-desktop-update")(event);
    assert.deepEqual(h.calls.desktopDownloadParents, [parent]);
    assert.equal(h.calls.desktopDownloads, 1);

    await h.handleHandlers.get("omnigent:about-install-desktop-update")(event);
    assert.equal(h.calls.desktopInstalls, 1);
  });

  it("rejects install when no downloaded update is ready", async () => {
    const h = makeAbout({ installReady: false });
    const about = h.controller.open(new FakeWindow());

    assert.throws(
      () => h.handleHandlers.get("omnigent:about-install-desktop-update")(eventFor(about)),
      /No downloaded update is ready/,
    );
  });
});
