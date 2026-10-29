const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const { createRequire } = require("node:module");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");

function loadMainHarness({
  settings = {},
  serverUrl = "https://server.example/app",
  sessionCookies = [],
  forceDevUpdateConfig = false,
  dialogResponses = [{ response: 1, checkboxChecked: false }],
  saveDialogResult = { canceled: false, filePath: "/tmp/desktop-diagnostics.jsonl" },
  exportError = null,
  serverShutdown = () => Promise.resolve(),
  isPackaged = false,
  platform = process.platform,
  developerMode = false,
  desktopVersionOverride,
} = {}) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-update-test-"));
  fs.writeFileSync(path.join(userData, "settings.json"), JSON.stringify(settings), "utf8");

  const ipcHandlers = new Map();
  const ipcListeners = new Map();
  const appEvents = new Map();
  const mainImmediates = [];
  const calls = {
    appQuit: 0,
    appExit: 0,
    checkForUpdates: 0,
    downloadUpdate: 0,
    quitAndInstall: [],
    sent: [],
    showMessageBox: [],
    showSaveDialog: [],
    exportedDiagnostics: [],
    setApplicationMenu: [],
    aboutOpens: [],
    expiredAccess: [],
    removedRefresh: [],
    authRequests: [],
    cookieReads: [],
    cookieRemovals: [],
    reloads: 0,
    windowCloses: 0,
    webContentsFocuses: 0,
  };

  const sender = {
    getURL: () => serverUrl,
  };
  const win = {
    isDestroyed: () => false,
    webContents: {
      getURL: () => serverUrl,
      send: (channel, payload) => calls.sent.push({ channel, payload }),
      reload: () => {
        calls.reloads++;
      },
      focus: () => {
        calls.webContentsFocuses++;
      },
    },
    isMinimized: () => false,
    restore: () => {},
    focus: () => {},
    close: () => {
      calls.windowCloses += 1;
    },
  };

  class FakeSemVer {
    constructor(version) {
      if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(version)) {
        throw new Error(`Invalid version: ${version}`);
      }
      this.version = version;
    }

    format() {
      return this.version;
    }
  }

  const autoUpdater = new EventEmitter();
  autoUpdater.currentVersion = new FakeSemVer("0.3.0");
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.forceDevUpdateConfig = forceDevUpdateConfig;
  autoUpdater.checkForUpdates = () => {
    calls.checkForUpdates += 1;
    return Promise.resolve();
  };
  autoUpdater.downloadUpdate = () => {
    calls.downloadUpdate += 1;
    return Promise.resolve();
  };
  autoUpdater.quitAndInstall = (...args) => {
    calls.quitAndInstall.push(args);
  };

  const electron = {
    app: {
      name: "Omnigent",
      isPackaged,
      getPath: (name) => (name === "userData" ? userData : userData),
      getVersion: () => "0.3.0",
      setName: () => {},
      setPath: () => {},
      requestSingleInstanceLock: () => true,
      on: (name, listener) => appEvents.set(name, listener),
      whenReady: () => ({ then: () => {} }),
      quit: () => {
        calls.appQuit += 1;
      },
      exit: () => {
        calls.appExit += 1;
      },
      setAppUserModelId: () => {},
    },
    BrowserWindow: Object.assign(function BrowserWindow() {}, {
      fromWebContents: (webContents) => (webContents === sender ? win : null),
      getFocusedWindow: () => win,
      getAllWindows: () => [win],
    }),
    Menu: {
      buildFromTemplate: (template) => ({ template }),
      setApplicationMenu: (menu) => calls.setApplicationMenu.push(menu),
    },
    Notification: { isSupported: () => false },
    clipboard: {},
    dialog: {
      showSaveDialog: (...args) => {
        calls.showSaveDialog.push(args);
        return Promise.resolve(saveDialogResult);
      },
      showMessageBox: (dialogWin, options) => {
        calls.showMessageBox.push({ win: dialogWin, options });
        return Promise.resolve(dialogResponses.shift() ?? { response: 1, checkboxChecked: false });
      },
    },
    ipcMain: {
      handle: (channel, handler) => ipcHandlers.set(channel, handler),
      on: (channel, listener) => ipcListeners.set(channel, listener),
    },
    nativeImage: {
      createFromPath: () => ({ isEmpty: () => true }),
    },
    nativeTheme: { shouldUseDarkColors: false, on: () => {} },
    screen: {},
    session: {
      defaultSession: {
        cookies: {
          get: async (filter) => {
            calls.cookieReads.push(filter);
            return sessionCookies;
          },
          remove: async (url, name) => {
            calls.cookieRemovals.push([url, name]);
          },
        },
      },
    },
    shell: {},
    systemPreferences: {
      getUserDefault: (key, type) =>
        key === "DeveloperMode" && type === "boolean" ? developerMode : false,
    },
  };

  const localRequires = {
    "./diagnostics": {
      diagnostics: {
        record: () => {},
        exportTo: (destination) => {
          if (exportError) throw exportError;
          calls.exportedDiagnostics.push(destination);
        },
      },
    },
    "./about_window": {
      createAboutWindow: () => ({
        open: (parent) => calls.aboutOpens.push(parent),
        registerIpc: () => {},
      }),
    },
    "./localhost_cors": { registerLocalhostCors: () => {} },
    "./url": {
      normalizeUrl: (url) => url,
      expandDatabricksWorkspaceUrl: async (url) => url,
    },
    "./workspace-chrome": { registerWorkspaceChromeHide: () => {} },
    "./workspace-root-bounce": { registerWorkspaceRootBounce: () => {} },
    // databricks-session (transitively) requires electron's `net`, unresolvable
    // under the sandbox's real require; behavior is covered in databricks-*.test.js.
    "./databricks-session": {
      ensureDatabricksSession: async (_ses, origin) => {
        calls.authRequests.push(origin);
        return origin;
      },
    },
    "./databricks-oauth": {
      expireStoredAccessToken: (origin) => {
        calls.expiredAccess.push(origin);
        return true;
      },
      removeStoredRefreshToken: (origin) => {
        calls.removedRefresh.push(origin);
        return true;
      },
    },
    "./omnigent_cli": {
      isExecutableFile: () => false,
      resolveCliPath: () => null,
      localHostId: () => "host_test",
      getCliStatus: () => ({ installed: false }),
    },
    "./server_manager": {
      shutdown: () => serverShutdown(),
      onChange: () => {},
      ensureServerAuth: async () => ({ ok: true }),
      ensureHostConnected: async () => ({ ok: true }),
      restartHost: async () => ({ ok: true }),
      disconnectHost: async () => ({ ok: true }),
    },
  };

  const mainPath = path.join(__dirname, "../src/main.js");
  const mainRequire = createRequire(mainPath);
  // Expose only the composed pieces main.js still owns: the `updater` instance
  // it constructs from ./desktop_updater, plus the menu / IPC / window
  // registries. The updater's own behavior is unit-tested directly in
  // test/desktop_updater.test.js; this file proves main.js wires that instance
  // into the menu, the IPC surface, and the before-quit install handoff.
  const source =
    fs.readFileSync(mainPath, "utf8") +
    '\nmodule.exports.testApi = { buildMenu, registerIpc, windows, updater, setQuitTimeouts: (o) => { if (typeof (o && o.cleanup) === "number") quitCleanupTimeoutMs = o.cleanup; if (typeof (o && o.installFallback) === "number") quitInstallFallbackMs = o.installFallback; } }';

  const module = { exports: {} };
  const sandbox = {
    __dirname: path.dirname(mainPath),
    __filename: mainPath,
    AbortController,
    AbortSignal,
    Buffer,
    URL,
    clearInterval,
    clearTimeout,
    console,
    module,
    process: {
      ...process,
      platform,
      env: {
        ...process.env,
        OMNIGENT_DESKTOP_VERSION_OVERRIDE: desktopVersionOverride,
        // No OMNIGENT_FORCE_DEV_UPDATE_CONFIG injection: main.js now derives
        // forceDevUpdateConfig from !app.isPackaged (always true in this
        // harness), not an env var. The harness still controls the
        // autoUpdater.forceDevUpdateConfig property directly (above) for tests
        // that exercise the feed-unavailable path without calling init().
      },
    },
    require: (specifier) => {
      if (specifier === "electron") return electron;
      if (specifier === "electron-updater") return { autoUpdater };
      if (specifier in localRequires) return localRequires[specifier];
      return mainRequire(specifier);
    },
    setImmediate: (callback, ...args) => {
      mainImmediates.push(() => callback(...args));
    },
    setInterval,
    setTimeout,
  };

  vm.runInNewContext(source, sandbox, { filename: mainPath });
  module.exports.testApi.windows.set(win, {
    origin: new URL(serverUrl).origin,
    serverUrl,
    badgeCount: 0,
  });

  return {
    api: module.exports.testApi,
    appEvents,
    autoUpdater,
    calls,
    cleanup: () => fs.rmSync(userData, { recursive: true, force: true }),
    events: {
      pinned: { sender, senderFrame: { url: serverUrl } },
      unpinned: { sender, senderFrame: { url: "https://evil.example/app" } },
    },
    ipcHandlers,
    ipcListeners,
    win,
    pendingMainImmediates: () => mainImmediates.length,
    readSettings: () => JSON.parse(fs.readFileSync(path.join(userData, "settings.json"), "utf8")),
    runMainImmediates: () => {
      const pending = mainImmediates.splice(0);
      for (const run of pending) run();
    },
  };
}

async function flushPromises() {
  await new Promise((resolve) => {
    setImmediate(resolve);
  });
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function findMenuItem(menu, id) {
  for (const item of menu.template) {
    if (item.id === id) return item;
    const found = findMenuItem({ template: item.submenu ?? [] }, id);
    if (found) return found;
  }
  return null;
}

function hasDebugMenu(menu) {
  return menu.template.some((item) => item.label === "Debug");
}

describe("in-app navigation menu actions", () => {
  it("routes Settings and New Session through the focused connected window", (t) => {
    const harness = loadMainHarness();
    t.after(harness.cleanup);

    harness.api.buildMenu();
    const menu = harness.calls.setApplicationMenu.at(-1);
    const settingsItem = findMenuItem(menu, "open_settings");
    const newSessionItem = findMenuItem(menu, "new_session");
    const newWindowItem = findMenuItem(menu, "new_window");

    settingsItem.click();
    assert.deepEqual(harness.calls.sent, [{ channel: "omnigent:open-path", payload: "/settings" }]);

    assert.equal(newSessionItem.label, "New Session");
    assert.equal(newSessionItem.accelerator, "CmdOrCtrl+N");
    assert.equal(newWindowItem.accelerator, "CmdOrCtrl+Shift+N");

    newSessionItem.click();

    assert.deepEqual(harness.calls.sent.at(-1), {
      channel: "omnigent:open-path",
      payload: "/",
    });
  });
});

describe("diagnostics export menu wiring", () => {
  it("exports from the production Server menu without renderer IPC", async (t) => {
    const harness = loadMainHarness({ isPackaged: true, platform: "darwin" });
    t.after(harness.cleanup);
    harness.api.registerIpc();
    harness.api.buildMenu();
    const menu = harness.calls.setApplicationMenu.at(-1);
    assert.equal(hasDebugMenu(menu), false);
    assert.ok(harness.ipcHandlers.size > 0);
    const item = findMenuItem(menu, "export_diagnostics");
    assert.equal(item.label, "Export Diagnostics…");
    item.click();
    await flushPromises();
    assert.equal(harness.calls.showSaveDialog.length, 1);
    assert.deepEqual(harness.calls.exportedDiagnostics, ["/tmp/desktop-diagnostics.jsonl"]);
    assert.equal(harness.ipcHandlers.has("omnigent:export-diagnostics"), false);
  });

  it("does not export when the save dialog is cancelled", async (t) => {
    const harness = loadMainHarness({ saveDialogResult: { canceled: true } });
    t.after(harness.cleanup);
    harness.api.buildMenu();
    findMenuItem(harness.calls.setApplicationMenu.at(-1), "export_diagnostics").click();
    await flushPromises();
    assert.deepEqual(harness.calls.exportedDiagnostics, []);
  });

  it("shows an error if the chosen destination cannot be written", async (t) => {
    const harness = loadMainHarness({ exportError: new Error("Destination is not writable") });
    t.after(harness.cleanup);
    harness.api.buildMenu();
    findMenuItem(harness.calls.setApplicationMenu.at(-1), "export_diagnostics").click();
    await flushPromises();
    assert.deepEqual(harness.calls.exportedDiagnostics, []);
    assert.equal(harness.calls.showMessageBox.length, 1);
    assert.equal(harness.calls.showMessageBox[0].options.title, "Could not export diagnostics");
  });
});

describe("About menu wiring", () => {
  it("opens the shell-owned About window from About and Check for Updates", async (t) => {
    const harness = loadMainHarness({
      platform: "darwin",
      settings: { update_mode: "manual" },
    });
    t.after(harness.cleanup);
    harness.api.updater.init();

    harness.api.buildMenu();
    const menu = harness.calls.setApplicationMenu.at(-1);
    const aboutItem = findMenuItem(menu, "open_about");
    const checkItem = findMenuItem(menu, "check_for_updates");
    aboutItem.click();

    assert.equal(aboutItem.label, "About Omnigent");
    assert.equal(harness.calls.aboutOpens.length, 1);

    checkItem.click();
    await flushPromises();
    assert.equal(harness.calls.aboutOpens.length, 2);
    assert.equal(harness.calls.checkForUpdates, 1);
  });

  it("exposes About under Help on Windows and Linux", (t) => {
    const harness = loadMainHarness({ platform: "linux" });
    t.after(harness.cleanup);

    harness.api.buildMenu();
    const menu = harness.calls.setApplicationMenu.at(-1);
    const aboutItem = findMenuItem(menu, "open_about");
    aboutItem.click();

    assert.equal(aboutItem.label, "About Omnigent");
    assert.equal(harness.calls.aboutOpens.length, 1);
  });
});

function submenuIds(menu, label) {
  const entry = menu.template.find((item) => item.label === label);
  return plain(entry.submenu.map((item) => item.id ?? item.role ?? item.type));
}

function topLevelMenus(menu) {
  return plain(
    menu.template.map((item) => item.label ?? item.role).filter((label) => label !== "Debug"),
  );
}

describe("application menu layout", () => {
  it("keeps updates, Settings, and Quit in the macOS app menu", (t) => {
    const harness = loadMainHarness({ platform: "darwin" });
    t.after(harness.cleanup);

    harness.api.buildMenu();
    const menu = harness.calls.setApplicationMenu.at(-1);

    assert.deepEqual(topLevelMenus(menu), [
      "Omnigent",
      "File",
      "Edit",
      "View",
      "Go",
      "Session",
      "Server",
      "windowMenu",
    ]);
    assert.deepEqual(submenuIds(menu, "Omnigent"), [
      "open_about",
      "check_for_updates",
      "restart_to_update",
      "separator",
      "open_settings",
      "separator",
      "services",
      "separator",
      "hide",
      "hideOthers",
      "unhide",
      "separator",
      "quit",
    ]);
    assert.deepEqual(submenuIds(menu, "File"), [
      "new_session",
      "new_window",
      "new_server_window",
      "separator",
      "new_terminal",
      "new_browser_tab",
      "focus_address_bar",
      "separator",
      "close",
      "close_window",
    ]);
    assert.deepEqual(submenuIds(menu, "Session"), ["toggle_pin_session", "archive_session"]);
    assert.deepEqual(submenuIds(menu, "Server"), [
      "change_server",
      "sign_out_server",
      "separator",
      "export_diagnostics",
    ]);
  });

  it("keeps Settings and Quit in File and updates in Help on Windows and Linux", (t) => {
    const harness = loadMainHarness({ platform: "linux" });
    t.after(harness.cleanup);

    harness.api.buildMenu();
    const menu = harness.calls.setApplicationMenu.at(-1);

    assert.deepEqual(topLevelMenus(menu), [
      "File",
      "Edit",
      "View",
      "Go",
      "Session",
      "Server",
      "Window",
      "Help",
    ]);
    assert.deepEqual(submenuIds(menu, "File"), [
      "new_session",
      "new_window",
      "new_server_window",
      "separator",
      "new_terminal",
      "new_browser_tab",
      "focus_address_bar",
      "separator",
      "open_settings",
      "separator",
      "close",
      "close_window",
      "separator",
      "quit",
    ]);
    // Electron's Window role adds its own CmdOrCtrl+W Close off macOS.
    assert.deepEqual(submenuIds(menu, "Window"), ["minimize", "zoom"]);
    assert.deepEqual(submenuIds(menu, "Help"), [
      "check_for_updates",
      "restart_to_update",
      "separator",
      "open_about",
    ]);
    const quit = findMenuItem(menu, "quit");
    assert.equal(quit.accelerator, "CmdOrCtrl+Q");
    quit.click();
    assert.equal(harness.calls.appQuit, 1);
  });
});

describe("desktop command menu items", () => {
  function reportState(harness, state, event = harness.events.pinned) {
    harness.api.registerIpc();
    harness.ipcListeners.get("omnigent:set-desktop-command-state")(event, state);
    return harness.calls.setApplicationMenu.at(-1);
  }

  it("binds the platform shortcuts and disables commands until the SPA reports them", (t) => {
    const harness = loadMainHarness({ platform: "darwin" });
    t.after(harness.cleanup);
    harness.api.buildMenu();
    const menu = harness.calls.setApplicationMenu.at(-1);

    const expected = {
      new_terminal: "Ctrl+`",
      new_browser_tab: "CmdOrCtrl+T",
      close: "CmdOrCtrl+W",
      close_window: "CmdOrCtrl+Shift+W",
      toggle_pin_session: "CmdOrCtrl+Alt+P",
      archive_session: "CmdOrCtrl+Shift+A",
    };
    for (const [id, accelerator] of Object.entries(expected)) {
      assert.equal(findMenuItem(menu, id).accelerator, accelerator, id);
    }
    for (const id of ["new_terminal", "new_browser_tab", "toggle_pin_session", "archive_session"]) {
      assert.equal(findMenuItem(menu, id).enabled, false, id);
    }
    assert.equal(findMenuItem(menu, "close_window").role, "close");
  });

  it("closes the window directly when the page never reported command support", (t) => {
    const harness = loadMainHarness({ platform: "darwin" });
    t.after(harness.cleanup);
    harness.api.buildMenu();
    findMenuItem(harness.calls.setApplicationMenu.at(-1), "close").click();

    assert.equal(harness.calls.windowCloses, 1);
    assert.deepEqual(harness.calls.sent, []);
  });

  it("enables and labels commands from the reported state, then routes clicks to the SPA", (t) => {
    const harness = loadMainHarness({ platform: "darwin" });
    t.after(harness.cleanup);
    const menu = reportState(harness, {
      commands: ["close", "new-terminal", "toggle-pin-session", "archive-session", "bogus"],
      sessionPinned: true,
      sessionArchived: true,
    });

    assert.equal(findMenuItem(menu, "new_terminal").enabled, true);
    assert.equal(findMenuItem(menu, "new_browser_tab").enabled, false);
    assert.equal(findMenuItem(menu, "toggle_pin_session").label, "Unpin Session");
    assert.equal(findMenuItem(menu, "archive_session").label, "Unarchive Session");
    assert.deepEqual(plain([...harness.api.windows.get(harness.win).desktopCommands.commands]), [
      "close",
      "new-terminal",
      "toggle-pin-session",
      "archive-session",
    ]);

    findMenuItem(menu, "new_terminal").click();
    findMenuItem(menu, "toggle_pin_session").click();
    // Not reported, so the click is dropped rather than sent to a page that can't act.
    findMenuItem(menu, "new_browser_tab").click();
    assert.deepEqual(plain(harness.calls.sent), [
      { channel: "omnigent:desktop-command", payload: { command: "new-terminal" } },
      { channel: "omnigent:desktop-command", payload: { command: "toggle-pin-session" } },
    ]);
  });

  it("hands Close to the SPA with the focused embedded browser view", (t) => {
    const harness = loadMainHarness({ platform: "linux" });
    t.after(harness.cleanup);
    let viewFocused = false;
    harness.api.windows.get(harness.win).browserRegistry = {
      activeConversationId: () => "browser-tab:session-1:tab-1",
      get: () => ({ view: { webContents: { isFocused: () => viewFocused } } }),
    };
    const menu = reportState(harness, { commands: ["close"] });

    findMenuItem(menu, "close").click();
    viewFocused = true;
    findMenuItem(menu, "close").click();

    assert.equal(harness.calls.windowCloses, 0);
    // The only window off macOS, so closing it would quit the app.
    assert.deepEqual(plain(harness.calls.sent), [
      {
        channel: "omnigent:desktop-command",
        payload: { command: "close", browserViewId: null, closingQuits: true },
      },
      {
        channel: "omnigent:desktop-command",
        payload: {
          command: "close",
          browserViewId: "browser-tab:session-1:tab-1",
          closingQuits: true,
        },
      },
    ]);
  });

  it("steps a focused web page's own history, else hands Back/Forward to the app", (t) => {
    const harness = loadMainHarness({ platform: "darwin" });
    t.after(harness.cleanup);
    const pageHistory = [];
    let viewFocused = true;
    harness.api.windows.get(harness.win).browserRegistry = {
      activeConversationId: () => "browser-tab:session-1:tab-1",
      get: () => ({
        view: {
          webContents: {
            isFocused: () => viewFocused,
            navigationHistory: {
              canGoBack: () => true,
              canGoForward: () => true,
              goBack: () => pageHistory.push("back"),
              goForward: () => pageHistory.push("forward"),
            },
          },
        },
      }),
    };
    const menu = reportState(harness, { commands: ["go-back", "go-forward"] });
    assert.equal(findMenuItem(menu, "go_back").accelerator, "CmdOrCtrl+[");
    assert.equal(findMenuItem(menu, "go_forward").accelerator, "CmdOrCtrl+]");

    findMenuItem(menu, "go_back").click();
    findMenuItem(menu, "go_forward").click();
    viewFocused = false;
    findMenuItem(menu, "go_back").click();

    assert.deepEqual(pageHistory, ["back", "forward"]);
    assert.deepEqual(plain(harness.calls.sent), [
      { channel: "omnigent:desktop-command", payload: { command: "go-back" } },
    ]);
  });

  it("reloads a focused web page, else asks the app, else reloads the window", (t) => {
    const harness = loadMainHarness({ platform: "darwin" });
    t.after(harness.cleanup);
    let pageReloads = 0;
    let viewFocused = true;
    harness.api.windows.get(harness.win).browserRegistry = {
      activeConversationId: () => "browser-tab:session-1:tab-1",
      get: () => ({
        view: {
          webContents: {
            isFocused: () => viewFocused,
            reload: () => {
              pageReloads += 1;
            },
          },
        },
      }),
    };
    harness.api.buildMenu();
    findMenuItem(harness.calls.setApplicationMenu.at(-1), "reload").click();
    assert.equal(harness.calls.reloads, 1, "an older page reloads the window as before");

    const menu = reportState(harness, { commands: ["reload", "focus-address-bar"] });
    findMenuItem(menu, "reload").click();
    viewFocused = false;
    findMenuItem(menu, "reload").click();
    findMenuItem(menu, "focus_address_bar").click();

    assert.equal(findMenuItem(menu, "reload").accelerator, "CmdOrCtrl+R");
    assert.equal(findMenuItem(menu, "focus_address_bar").accelerator, "CmdOrCtrl+L");
    assert.equal(pageReloads, 1);
    assert.equal(harness.calls.reloads, 1);
    assert.equal(harness.calls.webContentsFocuses, 1);
    assert.deepEqual(plain(harness.calls.sent), [
      { channel: "omnigent:desktop-command", payload: { command: "reload" } },
      { channel: "omnigent:desktop-command", payload: { command: "focus-address-bar" } },
    ]);
  });

  it("ignores command state from a page off the pinned server", (t) => {
    const harness = loadMainHarness({ platform: "darwin" });
    t.after(harness.cleanup);
    harness.api.registerIpc();
    const builds = harness.calls.setApplicationMenu.length;
    harness.ipcListeners.get("omnigent:set-desktop-command-state")(harness.events.unpinned, {
      commands: ["close", "new-terminal"],
    });

    assert.equal(harness.api.windows.get(harness.win).desktopCommands, undefined);
    assert.equal(harness.calls.setApplicationMenu.length, builds);
  });
});

describe("close-window request", () => {
  it("closes the window once the page has confirmed, without a native prompt", async (t) => {
    const harness = loadMainHarness({ platform: "darwin" });
    t.after(harness.cleanup);
    harness.api.registerIpc();

    assert.equal(
      await harness.ipcHandlers.get("omnigent:close-window")(harness.events.pinned),
      true,
    );
    assert.equal(harness.calls.windowCloses, 1);
    assert.equal(harness.calls.showMessageBox.length, 0);
  });

  it("refuses a page off the pinned server", async (t) => {
    const harness = loadMainHarness({ platform: "darwin" });
    t.after(harness.cleanup);
    harness.api.registerIpc();

    assert.equal(
      await harness.ipcHandlers.get("omnigent:close-window")(harness.events.unpinned),
      false,
    );
    assert.equal(harness.calls.windowCloses, 0);
  });
});

describe("developer-mode menu wiring", () => {
  it("places expiry simulations under Debug, not Server, in development builds", (t) => {
    const harness = loadMainHarness({ isPackaged: false, platform: "linux" });
    t.after(harness.cleanup);

    harness.api.buildMenu();

    const menu = harness.calls.setApplicationMenu.at(-1);
    const debug = menu.template.find((item) => item.label === "Debug");
    const server = menu.template.find((item) => item.label === "Server");
    const authentication = debug.submenu.find((item) => item.id === "debug_authentication");
    assert.equal(authentication.label, "Authentication");
    for (const id of [
      "simulate_session_expiry",
      "simulate_oauth_token_expiry",
      "invalidate_oauth_refresh_token",
    ]) {
      assert.equal(typeof authentication.submenu.find((item) => item.id === id)?.click, "function");
      assert.equal(findMenuItem({ template: [server] }, id), null);
    }
  });

  it("clears the focused workspace cookie without forcing a reload or renewal", async (t) => {
    const origin = "https://workspace.cloud.databricks.com";
    const harness = loadMainHarness({
      isPackaged: false,
      platform: "linux",
      serverUrl: `${origin}/omnigent`,
      sessionCookies: [
        { name: "DBAUTH", domain: ".workspace.cloud.databricks.com", path: "/", secure: true },
      ],
    });
    t.after(harness.cleanup);
    harness.api.buildMenu();
    findMenuItem(harness.calls.setApplicationMenu.at(-1), "simulate_session_expiry").click();
    await flushPromises();
    assert.deepEqual(plain(harness.calls.cookieReads), [{ url: origin, name: "DBAUTH" }]);
    assert.deepEqual(harness.calls.cookieRemovals, [[`${origin}/`, "DBAUTH"]]);
    assert.equal(harness.calls.reloads, 0);
    assert.deepEqual(harness.calls.authRequests, []);
  });

  it("changes cached tokens without starting a refresh or reload", async (t) => {
    const origin = "https://workspace.cloud.databricks.com";
    const harness = loadMainHarness({
      isPackaged: false,
      platform: "linux",
      serverUrl: `${origin}/omnigent`,
    });
    t.after(harness.cleanup);
    harness.api.buildMenu();
    const menu = harness.calls.setApplicationMenu.at(-1);
    findMenuItem(menu, "simulate_oauth_token_expiry").click();
    findMenuItem(menu, "invalidate_oauth_refresh_token").click();
    await flushPromises();
    assert.deepEqual(harness.calls.expiredAccess, [origin]);
    assert.deepEqual(harness.calls.removedRefresh, [origin]);
    assert.deepEqual(harness.calls.authRequests, []);
    assert.equal(harness.calls.reloads, 0);
    assert.deepEqual(harness.calls.showMessageBox, []);
  });

  it("hides the Debug menu in packaged builds by default", (t) => {
    const harness = loadMainHarness({
      isPackaged: true,
      platform: "darwin",
      developerMode: false,
    });
    t.after(harness.cleanup);

    harness.api.buildMenu();

    assert.equal(hasDebugMenu(harness.calls.setApplicationMenu.at(-1)), false);
  });

  it("shows authentication debugging when a packaged macOS build opts in", (t) => {
    const harness = loadMainHarness({
      isPackaged: true,
      platform: "darwin",
      developerMode: true,
    });
    t.after(harness.cleanup);

    harness.api.buildMenu();

    const menu = harness.calls.setApplicationMenu.at(-1);
    assert.equal(hasDebugMenu(menu), true);
    const authentication = findMenuItem(menu, "debug_authentication");
    assert.equal(authentication.label, "Authentication");
    for (const id of [
      "simulate_session_expiry",
      "simulate_oauth_token_expiry",
      "invalidate_oauth_refresh_token",
    ]) {
      assert.equal(typeof findMenuItem(menu, id)?.click, "function");
    }
  });
});

describe("auto-update main-process wiring", () => {
  it("preserves unrelated settings keys when writing update config", (t) => {
    const harness = loadMainHarness({
      settings: {
        server_url: "https://server.example/app",
        recent_servers: ["https://server.example/app"],
        window_bounds: { width: 1200, height: 800 },
        update_mode: "start",
      },
    });
    t.after(harness.cleanup);

    assert.deepEqual(
      plain(
        harness.api.updater.setConfig({
          mode: "manual",
          autoInstall: false,
          skippedVersion: "0.4.0",
          ignored: "value",
        }),
      ),
      { mode: "manual", autoInstall: false, skippedVersion: "0.4.0" },
    );

    const saved = harness.readSettings();
    assert.equal(saved.server_url, "https://server.example/app");
    assert.deepEqual(saved.recent_servers, ["https://server.example/app"]);
    assert.deepEqual(saved.window_bounds, { width: 1200, height: 800 });
    assert.equal(saved.update_mode, "manual");
    assert.equal(saved.update_auto_install, false);
    assert.equal(saved.update_skipped_version, "0.4.0");
    assert.equal(saved.mode, undefined);

    harness.api.updater.setConfig({ mode: "bogus" });
    assert.equal(harness.readSettings().update_mode, "manual");
  });

  it("rejects the frozen IPC handlers from a non-pinned sender", async (t) => {
    const harness = loadMainHarness();
    t.after(harness.cleanup);
    harness.api.registerIpc();

    const cases = [
      ["omnigent:get-update-config", []],
      ["omnigent:get-update-status", []],
      ["omnigent:update-check", []],
      ["omnigent:update-download", []],
      ["omnigent:update-install", []],
      ["omnigent:set-update-config", [{ mode: "manual" }]],
    ];
    await Promise.all(
      cases.map(([channel, args]) => {
        const handler = harness.ipcHandlers.get(channel);
        return assert.rejects(
          Promise.resolve().then(() => handler(harness.events.unpinned, ...args)),
          /connected server page/,
        );
      }),
    );
  });

  it("prompts for every privileged update channel before running it", async (t) => {
    const cases = [
      {
        channel: "omnigent:update-download",
        args: [],
        message: "Download an Omnigent update?",
        prepare: () => {},
        assertRan: (harness) => {
          assert.equal(harness.calls.downloadUpdate, 1);
        },
      },
      {
        channel: "omnigent:update-install",
        args: [],
        message: "Restart Omnigent to install an update?",
        prepare: (harness) => {
          harness.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
        },
        assertRan: (harness) => {
          assert.equal(harness.api.updater.installPending, true);
          assert.equal(harness.calls.appQuit, 1);
        },
      },
      {
        channel: "omnigent:set-update-config",
        args: [{ mode: "manual" }],
        message: "Change Omnigent update settings?",
        prepare: () => {},
        assertRan: (harness) => {
          assert.equal(harness.readSettings().update_mode, "manual");
        },
      },
    ];

    // Harnesses install process-level module mocks and must run one at a time.
    /* oxlint-disable no-await-in-loop */
    for (const item of cases) {
      const harness = loadMainHarness({
        forceDevUpdateConfig: true,
        settings: { update_mode: "manual" },
      });
      t.after(harness.cleanup);
      harness.api.updater.init();
      harness.api.registerIpc();
      item.prepare(harness);

      await harness.ipcHandlers.get(item.channel)(harness.events.pinned, ...item.args);

      assert.equal(harness.calls.showMessageBox.length, 1, item.channel);
      assert.equal(harness.calls.showMessageBox[0].win, harness.api.windows.keys().next().value);
      assert.equal(harness.calls.showMessageBox[0].options.title, "Omnigent");
      assert.equal(harness.calls.showMessageBox[0].options.message, item.message);
      assert.deepEqual(plain(harness.calls.showMessageBox[0].options.buttons), [
        "Don't Allow",
        "Allow Once",
      ]);
      item.assertRan(harness);
    }
    /* oxlint-enable no-await-in-loop */
  });

  it("does not let a cached hosting grant bypass update-control consent", async (t) => {
    const cases = [
      {
        channel: "omnigent:update-download",
        args: [],
        prepare: () => {},
        assertBlocked: (harness) => {
          assert.equal(harness.calls.downloadUpdate, 0);
        },
      },
      {
        channel: "omnigent:update-install",
        args: [],
        prepare: (harness) => {
          harness.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
        },
        assertBlocked: (harness) => {
          assert.equal(harness.api.updater.installPending, false);
          assert.equal(harness.calls.appQuit, 0);
        },
      },
      {
        channel: "omnigent:set-update-config",
        args: [{ mode: "manual" }],
        prepare: () => {},
        assertBlocked: (harness) => {
          assert.equal(harness.readSettings().update_mode, "start");
        },
      },
    ];

    // Harnesses install process-level module mocks and must run one at a time.
    /* oxlint-disable no-await-in-loop */
    for (const item of cases) {
      const harness = loadMainHarness({
        forceDevUpdateConfig: true,
        settings: {
          allowed_hosting_origins: ["https://server.example"],
          update_mode: "start",
        },
        dialogResponses: [{ response: 0, checkboxChecked: false }],
      });
      t.after(harness.cleanup);
      harness.api.updater.init();
      harness.api.registerIpc();
      item.prepare(harness);

      await assert.rejects(
        harness.ipcHandlers.get(item.channel)(harness.events.pinned, ...item.args),
        /approved/,
      );

      assert.equal(harness.calls.showMessageBox.length, 1, item.channel);
      item.assertBlocked(harness);
    }
    /* oxlint-enable no-await-in-loop */
  });

  it("routes approved update-install through before-quit cleanup to quitAndInstall", async (t) => {
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { allowed_hosting_origins: ["https://server.example"], update_mode: "manual" },
    });
    t.after(harness.cleanup);
    harness.api.updater.init();
    harness.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
    harness.api.registerIpc();

    await harness.ipcHandlers.get("omnigent:update-install")(harness.events.pinned);

    assert.equal(harness.calls.showMessageBox.length, 1);
    assert.equal(harness.api.updater.installPending, true);
    assert.equal(harness.calls.appQuit, 1);

    let prevented = 0;
    harness.appEvents.get("before-quit")({ preventDefault: () => (prevented += 1) });
    await flushPromises();

    assert.equal(prevented, 1);
    assert.deepEqual(harness.calls.quitAndInstall, []);
    assert.equal(harness.pendingMainImmediates(), 1);

    harness.runMainImmediates();
    assert.deepEqual(harness.calls.quitAndInstall, [[false, true]]);
    assert.equal(harness.calls.appQuit, 1);
  });

  it("force-exits when a pending update install doesn't re-issue app.quit", async (t) => {
    // electron-updater's quitAndInstall() only re-issues app.quit() when it can
    // actually install; if it can't (staged update gone, install() returned
    // false), the before-quit handoff would otherwise leave the app up. The
    // fallback forces an exit instead, so the app never stays up waiting for
    // an update that won't install.
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
      platform: "linux",
    });
    t.after(harness.cleanup);
    harness.api.updater.init();
    harness.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
    harness.api.registerIpc();
    await harness.ipcHandlers.get("omnigent:update-install")(harness.events.pinned);
    assert.equal(harness.api.updater.installPending, true);
    assert.equal(harness.calls.appQuit, 1); // installUpdateNow → app.quit()

    // Shrink the fallback so the test doesn't wait 3s. quitAndInstall is a
    // no-op in the harness (it never re-issues app.quit), simulating a failed
    // install(), so only the fallback can quit.
    harness.api.setQuitTimeouts({ installFallback: 10 });
    harness.appEvents.get("before-quit")({ preventDefault: () => {} });
    await flushPromises();
    assert.deepEqual(harness.calls.quitAndInstall, []);
    assert.equal(harness.pendingMainImmediates(), 1);

    harness.runMainImmediates();
    assert.deepEqual(harness.calls.quitAndInstall, [[false, true]]);
    assert.equal(harness.calls.appQuit, 1); // still no re-issued quit
    assert.equal(harness.calls.appExit, 0); // fallback not fired yet

    await new Promise((resolve) => {
      setTimeout(resolve, 30);
    });
    assert.equal(harness.calls.appExit, 1); // fallback forced the exit
    assert.equal(harness.calls.appQuit, 1); // never re-issued
  });

  it("waits on macOS for an async install (admin prompt) instead of force-exiting", async (t) => {
    // Squirrel.Mac installs asynchronously and may show an admin password
    // prompt for a root-owned bundle; a timed exit would kill that prompt.
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
      platform: "darwin",
    });
    t.after(harness.cleanup);
    harness.api.updater.init();
    harness.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
    harness.api.registerIpc();
    await harness.ipcHandlers.get("omnigent:update-install")(harness.events.pinned);

    harness.api.setQuitTimeouts({ installFallback: 10, cleanup: 10 });
    harness.appEvents.get("before-quit")({ preventDefault: () => {} });
    await flushPromises();
    harness.runMainImmediates();
    assert.deepEqual(harness.calls.quitAndInstall, [[false, true]]);

    await new Promise((resolve) => {
      setTimeout(resolve, 30);
    });
    assert.equal(harness.calls.appExit, 0); // still waiting on the install

    // A failed/cancelled install still exits rather than leaving the app up.
    harness.autoUpdater.emit("error", new Error("authorization canceled"));
    assert.equal(harness.calls.appExit, 1);
  });

  it("force-exits if the re-issued normal quit does not terminate", async (t) => {
    const harness = loadMainHarness({ settings: { update_mode: "manual" } });
    t.after(harness.cleanup);
    harness.api.setQuitTimeouts({ cleanup: 10 });

    harness.appEvents.get("before-quit")({ preventDefault: () => {} });
    await flushPromises();
    assert.equal(harness.calls.appQuit, 0);
    assert.equal(harness.pendingMainImmediates(), 1);

    harness.runMainImmediates();
    assert.equal(harness.calls.appQuit, 1);
    assert.equal(harness.calls.appExit, 0);

    await new Promise((resolve) => {
      setTimeout(resolve, 30);
    });
    assert.equal(harness.calls.appExit, 1);
  });

  it("cancels the normal quit fallback once quit is reached", async (t) => {
    const harness = loadMainHarness({ settings: { update_mode: "manual" } });
    t.after(harness.cleanup);
    harness.api.setQuitTimeouts({ cleanup: 10 });

    harness.appEvents.get("before-quit")({ preventDefault: () => {} });
    await flushPromises();
    assert.equal(harness.calls.appQuit, 0);
    assert.equal(harness.pendingMainImmediates(), 1);

    harness.runMainImmediates();
    assert.equal(harness.calls.appQuit, 1);
    harness.appEvents.get("quit")();
    await new Promise((resolve) => {
      setTimeout(resolve, 30);
    });
    assert.equal(harness.calls.appExit, 0);
  });

  it("force-exits if before-quit cleanup hangs past the safety cap", async (t) => {
    // A stuck shutdown (e.g. a hung `omnigent server stop`, or the known
    // Electron hazard where re-issuing app.quit() after before-quit's
    // preventDefault doesn't terminate) must not strand the quit. The hard cap
    // force-exits. Here shutdown never settles, so the re-issued app.quit() in
    // .finally never runs — only the cap can quit.
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
      serverShutdown: () => new Promise(() => {}),
    });
    t.after(harness.cleanup);
    harness.api.updater.init();
    harness.api.registerIpc();
    harness.api.setQuitTimeouts({ cleanup: 10 });

    harness.appEvents.get("before-quit")({ preventDefault: () => {} });
    await flushPromises();
    assert.equal(harness.calls.appQuit, 0); // shutdown hung, no re-issued quit
    assert.equal(harness.pendingMainImmediates(), 0);
    assert.equal(harness.calls.appExit, 0); // cap not fired yet

    await new Promise((resolve) => {
      setTimeout(resolve, 30);
    });
    assert.equal(harness.calls.appExit, 1); // cap forced the exit
    assert.equal(harness.calls.appQuit, 0); // never re-issued
  });

  it("shows Restart to Update only while an update is ready to install", (t) => {
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
    });
    t.after(harness.cleanup);
    harness.api.updater.init();

    harness.api.buildMenu();
    let restartItem = findMenuItem(harness.calls.setApplicationMenu.at(-1), "restart_to_update");
    assert.equal(restartItem.visible, false);

    harness.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
    restartItem = findMenuItem(harness.calls.setApplicationMenu.at(-1), "restart_to_update");
    assert.equal(restartItem.visible, true);

    // A late/redundant check result must not discard an already-downloaded artifact.
    harness.autoUpdater.emit("update-not-available");
    restartItem = findMenuItem(harness.calls.setApplicationMenu.at(-1), "restart_to_update");
    assert.equal(restartItem.visible, true);
  });

  it("does not start the install path when no update is downloaded", async (t) => {
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
    });
    t.after(harness.cleanup);
    harness.api.updater.init();
    harness.api.registerIpc();

    await assert.rejects(
      harness.ipcHandlers.get("omnigent:update-install")(harness.events.pinned),
      /No downloaded update/,
    );
    assert.equal(harness.calls.showMessageBox.length, 1);
    assert.equal(harness.api.updater.installPending, false);
    assert.equal(harness.calls.appQuit, 0);

    harness.api.buildMenu();
    const restartItem = findMenuItem(harness.calls.setApplicationMenu.at(-1), "restart_to_update");
    assert.ok(restartItem);
    assert.equal(restartItem.visible, false);
    restartItem.click();
    assert.equal(harness.api.updater.installPending, false);
    assert.equal(harness.calls.appQuit, 0);
  });

  it("surfaces manual check failures without changing the status union", async (t) => {
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
    });
    t.after(harness.cleanup);
    harness.api.updater.init();
    harness.autoUpdater.checkForUpdates = () => {
      harness.calls.checkForUpdates += 1;
      const err = new Error("Cannot find latest.yml: 404");
      harness.autoUpdater.emit("error", err);
      return Promise.reject(err);
    };
    harness.api.registerIpc();

    await assert.rejects(
      harness.ipcHandlers.get("omnigent:update-check")(harness.events.pinned),
      /latest\.yml/,
    );

    assert.equal(harness.calls.checkForUpdates, 1);
    assert.deepEqual(plain(harness.api.updater.getStatus()), {
      state: "idle",
      lastError: "Cannot find latest.yml: 404",
    });
  });

  it("blocks manual update paths when the updater feed is unavailable in development", async (t) => {
    const harness = loadMainHarness({ settings: { update_mode: "manual" } });
    t.after(harness.cleanup);
    harness.api.registerIpc();

    await assert.rejects(
      harness.ipcHandlers.get("omnigent:update-check")(harness.events.pinned),
      /unavailable in development/,
    );
    await assert.rejects(
      harness.ipcHandlers.get("omnigent:update-download")(harness.events.pinned),
      /unavailable in development/,
    );
    await assert.rejects(
      harness.ipcHandlers.get("omnigent:update-install")(harness.events.pinned),
      /unavailable in development/,
    );
    await assert.rejects(
      harness.ipcHandlers.get("omnigent:set-update-config")(harness.events.pinned, {
        mode: "manual",
      }),
      /unavailable in development/,
    );

    assert.equal(harness.calls.showMessageBox.length, 0);
    assert.equal(harness.calls.checkForUpdates, 0);
    assert.equal(harness.calls.downloadUpdate, 0);
    assert.equal(harness.api.updater.installPending, false);
  });

  it("allows only development builds to override the effective desktop version", (t) => {
    const development = loadMainHarness({
      settings: { update_mode: "manual" },
      desktopVersionOverride: " 0.2.0 ",
    });
    t.after(development.cleanup);
    assert.equal(development.autoUpdater.currentVersion.version, "0.2.0");
    assert.equal(development.autoUpdater.currentVersion.format(), "0.2.0");

    development.api.updater.init();
    development.autoUpdater.emit("update-available", { version: "0.4.0" });
    assert.equal(development.api.updater.getStatus().currentVersion, "0.2.0");

    development.autoUpdater.emit("update-not-available");

    const packaged = loadMainHarness({
      isPackaged: true,
      settings: { update_mode: "manual" },
      desktopVersionOverride: "0.2.0",
    });
    t.after(packaged.cleanup);
    assert.equal(packaged.autoUpdater.currentVersion.version, "0.3.0");

    packaged.api.updater.init();
    packaged.autoUpdater.emit("update-available", { version: "0.4.0" });
    assert.equal(packaged.api.updater.getStatus().currentVersion, "0.3.0");
  });

  it("supports forceDevUpdateConfig and broadcasts updater events", (t) => {
    const harness = loadMainHarness({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
    });
    t.after(harness.cleanup);

    harness.api.updater.init();
    harness.autoUpdater.emit("update-available", { version: "0.4.0" });

    assert.equal(harness.autoUpdater.forceDevUpdateConfig, true);
    assert.deepEqual(plain(harness.api.updater.getStatus()), {
      state: "available",
      currentVersion: "0.3.0",
      info: { version: "0.4.0" },
    });
    assert.deepEqual(plain(harness.calls.sent), [
      {
        channel: "omnigent:update-status",
        payload: {
          state: "available",
          currentVersion: "0.3.0",
          info: { version: "0.4.0" },
        },
      },
    ]);
  });
});
