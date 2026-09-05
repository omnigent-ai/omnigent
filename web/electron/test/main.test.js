// Regression guard for how src/main.js WIRES workspace-chrome injection, run
// with `node --test` (no extra deps). The wiring itself lives in
// src/workspace-chrome.js (registerWorkspaceChromeHide registers a
// did-finish-load listener that injects the chrome-hide CSS) and its BEHAVIOR is
// unit-tested in workspace-chrome.test.js. This guards the complementary half
// that no behavior test can see: that main.js still actually INVOKES
// registerWorkspaceChromeHide(win.webContents) as live code — not removed, not
// commented out.
//
// A naive source-string match would pass even if the call were commented out
// (the text still appears in the comment), so we strip comments from the source
// before asserting. URL slashes (`https://`) are preserved by only treating a
// `//` NOT preceded by `:` as a line comment. (This cannot prove the call runs
// at runtime — only an Electron launch could — but it does catch the call being
// removed or commented out, which the behavior test in workspace-chrome.test.js
// cannot, because that test never touches main.js.)

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const fs = require("node:fs");
const os = require("node:os");
const { createRequire } = require("node:module");
const path = require("node:path");
const vm = require("node:vm");

const mainSource = readFileSync(path.join(__dirname, "../src/main.js"), "utf8");
const preloadSource = readFileSync(path.join(__dirname, "../src/preload.js"), "utf8");
const setupSource = readFileSync(path.join(__dirname, "../setup/index.html"), "utf8");
const urlHelpers = require("../src/url");

// Strip block comments, then line comments (leaving `://` in URLs intact).
const liveCode = mainSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

function loadNavigationHarness({
  serverUrl = "https://host.example/ml/omnigents",
  savedServerUrl,
  registerFallbacks = true,
} = {}) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-navigation-test-"));
  if (savedServerUrl) {
    fs.writeFileSync(
      path.join(userData, "settings.json"),
      JSON.stringify({ server_url: savedServerUrl }),
    );
  }
  const listeners = new Map();
  const calls = { loadFile: [], loadURL: [] };
  const bannerCalls = { show: [], hide: 0 };
  let currentUrl = serverUrl;
  const appEvents = new Map();
  const webContents = {
    on(eventName, listener) {
      // Multiple modules listen on the same events (navigation fallbacks,
      // away watch, workspace bounce): keep them all, like a real emitter.
      if (!listeners.has(eventName)) listeners.set(eventName, []);
      listeners.get(eventName).push(listener);
    },
    emit(eventName, ...args) {
      for (const listener of listeners.get(eventName) ?? []) listener({}, ...args);
    },
    getURL: () => currentUrl,
    setWindowOpenHandler: () => {},
  };
  const win = {
    webContents,
    contentView: { addChildView: () => {}, removeChildView: () => {} },
    isDestroyed: () => false,
    isMaximized: () => false,
    getNormalBounds: () => ({ x: 0, y: 0, width: 1280, height: 860 }),
    getPosition: () => [0, 0],
    setPosition: () => {},
    maximize: () => {},
    on: () => {},
    loadFile: (...args) => {
      calls.loadFile.push(args);
      return Promise.resolve();
    },
    loadURL: (...args) => {
      calls.loadURL.push(args);
      return Promise.resolve();
    },
  };

  function createDesktopUpdater() {
    return {
      init() {},
      registerIpc() {},
      getConfig: () => ({ mode: "none", autoInstall: false, skippedVersion: null }),
      getStatus: () => ({ state: "idle" }),
      checkForUpdates: async () => {},
      installUpdateNow: () => false,
      quitAndInstallIfPending: () => false,
    };
  }

  const electron = {
    app: {
      isPackaged: false,
      getPath: () => userData,
      setName: () => {},
      setBadgeCount: () => true,
      requestSingleInstanceLock: () => true,
      on: (eventName, listener) => appEvents.set(eventName, listener),
      whenReady: () => ({ then: () => {} }),
      quit: () => {},
      exit: () => {},
      isReady: () => false,
      setAsDefaultProtocolClient: () => {},
      setAppUserModelId: () => {},
      getVersion: () => "test",
    },
    BrowserWindow: Object.assign(
      function BrowserWindow() {
        return win;
      },
      {
        fromWebContents: () => null,
        getFocusedWindow: () => null,
        getAllWindows: () => [],
      },
    ),
    WebContentsView: function WebContentsView() {},
    Menu: { buildFromTemplate: () => ({}), setApplicationMenu: () => {} },
    Notification: { isSupported: () => false },
    clipboard: { writeText: () => {} },
    dialog: {},
    ipcMain: { handle: () => {}, on: () => {} },
    nativeImage: { createFromPath: () => ({ isEmpty: () => true }) },
    nativeTheme: { shouldUseDarkColors: false, on: () => {} },
    screen: {},
    session: { defaultSession: {} },
    shell: {},
    systemPreferences: {},
  };

  const localRequires = {
    "./desktop_updater": { createDesktopUpdater },
    "./update_overlay": {
      createUpdateOverlay: () => ({ ensureOverlay: () => {}, registerIpc: () => {} }),
    },
    "./localhost_cors": { registerLocalhostCors: () => {} },
    "./url": {
      ...urlHelpers,
      normalizeUrl: (url) => url,
      expandDatabricksWorkspaceUrl: async (url) => url,
      fetchServerManifest: async () => ({}),
      PRE_MANIFEST_BASELINE: {},
    },
    "./deepLink": {
      parseOmnigentDeepLink: () => null,
      chooseDeepLinkStrategy: () => null,
    },
    "./workspace-chrome": { registerWorkspaceChromeHide: () => {} },
    // The bounce's behavior is unit-tested in workspace-root-bounce.test.js;
    // stubbed here because it would call into the stubbed ./url module. The
    // away banner is intentionally NOT stubbed: its wiring through
    // createWindow is what the behavior test below exercises.
    "./workspace-root-bounce": { registerWorkspaceRootBounce: () => {} },
    "./return_banner": {
      createReturnBanner: () => ({
        ensureBanner: () => {},
        show: (bannerWin, returnUrl) => bannerCalls.show.push({ win: bannerWin, returnUrl }),
        hide: () => bannerCalls.hide++,
        registerIpc: () => {},
      }),
    },
    "./browserViewRegistry": {
      createBrowserViewRegistry: () => ({ closeAll: () => {} }),
    },
    "./browserViewBounds": {
      createBrowserViewBoundsController: () => ({ attach: () => {}, detach: () => {} }),
    },
    "./browserIpc": { registerBrowserIpc: () => {} },
    "./session-expiry": { registerSessionExpiryReload: () => {} },
    "./popupPolicy": {
      decideWindowOpen: () => ({ kind: "ignore" }),
      stripCrossOriginOpenerHeaders: () => {},
      WEB_SCHEMES: new Set(),
    },
    "./omnigent_cli": {
      isExecutableFile: () => false,
      resolveCliPath: () => null,
      localHostId: () => "host_test",
      getCliStatus: () => ({ installed: false }),
    },
    "./server_manager": {
      shutdown: async () => {},
      onChange: () => {},
      ensureServerAuth: async () => ({ ok: true }),
      ensureHostConnected: async () => ({ ok: true }),
      restartHost: async () => ({ ok: true }),
      disconnectHost: async () => ({ ok: true }),
      startLocalServer: async () => ({ ok: false }),
    },
  };

  const mainPath = path.join(__dirname, "../src/main.js");
  const mainRequire = createRequire(mainPath);
  const source =
    fs.readFileSync(mainPath, "utf8") +
    "\nmodule.exports.testApi = { createWindow, registerNavigationFallbacks, windows, SETUP_PAGE, setAwayBannerDelayMs: (ms) => { awayBannerDelayMs = ms; } };";
  const module = { exports: {} };
  const sandbox = {
    __dirname: path.dirname(mainPath),
    __filename: mainPath,
    AbortController,
    AbortSignal,
    Buffer,
    URL,
    URLSearchParams,
    clearInterval,
    clearTimeout,
    console,
    module,
    process: { ...process, env: { ...process.env } },
    require: (specifier) => {
      if (specifier === "electron") return electron;
      if (specifier === "electron-updater") return { autoUpdater: {} };
      if (specifier in localRequires) return localRequires[specifier];
      return mainRequire(specifier);
    },
    setInterval,
    setTimeout,
  };

  vm.runInNewContext(source, sandbox, { filename: mainPath });
  const api = module.exports.testApi;
  api.windows.set(win, {
    origin: new URL(serverUrl).origin,
    serverUrl,
    ephemeral: false,
    badgeCount: 0,
    browserRegistry: { closeAll: () => {} },
  });
  if (registerFallbacks) api.registerNavigationFallbacks(win);

  return {
    api,
    calls,
    bannerCalls,
    emit: (eventName, ...args) => webContents.emit(eventName, ...args),
    hasListener: (eventName) => listeners.has(eventName),
    setUrl: (url) => {
      currentUrl = url;
    },
    win,
    cleanup: () => {
      api.windows.clear();
      fs.rmSync(userData, { recursive: true, force: true });
    },
  };
}

describe("setup clipboard IPC wiring", () => {
  it("exposes a narrow copy action through the setup bridge", () => {
    assert.match(
      preloadSource,
      /copyText:\s*\(text\)\s*=>\s*ipcRenderer\.invoke\("omnigent:copy-setup-text",\s*text\)/,
    );
  });

  it("checks the setup-page sender before writing to the clipboard", () => {
    assert.match(
      liveCode,
      /ipcMain\.handle\("omnigent:copy-setup-text",[\s\S]{0,200}!isSetupPageSender\(event\)[\s\S]{0,300}clipboard\.writeText\(text\)/,
    );
  });
});

describe("macOS activation wiring", () => {
  it("counts tracked shell windows instead of utility windows", () => {
    assert.match(liveCode, /app\.on\("activate"[\s\S]{0,500}windows\.size === 0/);
    assert.doesNotMatch(
      liveCode,
      /app\.on\("activate"[\s\S]{0,500}BrowserWindow\.getAllWindows\(\)\.length === 0/,
    );
  });
});

describe("managed server preference wiring", () => {
  it("exposes managed servers only through the setup-page bridge", () => {
    assert.match(
      preloadSource,
      /getManagedServers:\s*\(\)\s*=>\s*ipcRenderer\.invoke\("omnigent:get-managed-servers"\)/,
    );
    assert.match(
      liveCode,
      /ipcMain\.handle\("omnigent:get-managed-servers"[\s\S]{0,180}!isSetupPageSender\(event\)[\s\S]{0,180}return managedServerUrls\(\)/,
    );
  });

  it("preserves a managed path while still expanding bare workspace roots", () => {
    assert.match(
      liveCode,
      /managedTarget\s*\?\?\s*normalizeUrl\(url\)[\s\S]{0,120}await expandDatabricksWorkspaceUrl\(normalized\)/,
    );
  });

  it("returns managed choices in the connected-server picker", () => {
    assert.match(
      liveCode,
      /ipcMain\.handle\("omnigent:get-server-picker"[\s\S]{0,500}managedServers[\s\S]{0,100}recentServers:\s*recents/,
    );
  });

  it("allows switching only to a recent or currently managed target", () => {
    assert.match(
      liveCode,
      /ipcMain\.handle\("omnigent:switch-server"[\s\S]{0,500}knownRecent[\s\S]{0,200}managedServerUrls\(\)\.includes\(url\)[\s\S]{0,150}!knownRecent\s*&&\s*!knownManaged/,
    );
  });

  it("renders organization-provided servers separately on setup", () => {
    assert.match(setupSource, /Provided by your organization/);
    assert.match(setupSource, /setup\s*\.getManagedServers\(\)/);
  });
});

describe("Databricks-internal local-host CLI wiring", () => {
  it("selects isaac omni only behind the internal flag and Databricks server gate", () => {
    assert.match(
      liveCode,
      /function hostCliCommand\(serverUrl\)[\s\S]{0,300}databricksInternalFeaturesEnabled\(\)[\s\S]{0,120}isDatabricksManagedServerUrl\(serverUrl\)[\s\S]{0,300}prefixArgs:\s*\["omni"\]/,
    );
  });

  it("uses the selected command for identity availability and every host action", () => {
    assert.match(
      liveCode,
      /host-get-identity[\s\S]{0,350}Boolean\(hostCliCommand\(senderServerUrl\(event\)\)\)/,
    );
    assert.match(
      liveCode,
      /host-control[\s\S]{0,450}const cliCommand = hostCliCommand\(serverUrl\)[\s\S]{0,1400}ensureServerAuth\(cliCommand, serverUrl\)[\s\S]{0,400}ensureHostConnected\(cliCommand, serverUrl\)[\s\S]{0,300}disconnectHost\(cliCommand, serverUrl\)/,
    );
  });

  it("disables CLI customization in main and explains managed policy in the setup dialog", () => {
    assert.match(
      liveCode,
      /omnigent:set-cli-path[\s\S]{0,300}databricksInternalFeaturesEnabled\(\)[\s\S]{0,250}customizationDisabled:\s*true[\s\S]{0,80}accepted:\s*false/,
    );
    assert.match(
      liveCode,
      /omnigent:browse-cli-path[\s\S]{0,250}databricksInternalFeaturesEnabled\(\)\) return null/,
    );
    assert.match(
      liveCode,
      /omnigent:cli-reset-path[\s\S]{0,250}databricksInternalFeaturesEnabled\(\)[\s\S]{0,250}customizationDisabled:\s*true/,
    );
    assert.match(setupSource, /cliGear\.hidden = false/);
    assert.match(setupSource, /cliManaged\.hidden = !cliCustomizationDisabled/);
    assert.match(setupSource, /cliPathInput\.disabled = cliCustomizationDisabled/);
    assert.match(setupSource, /cliBrowse\.disabled = cliCustomizationDisabled/);
    assert.match(setupSource, /cliRedetect\.disabled = cliCustomizationDisabled/);
    assert.match(setupSource, /Managed by your organization/);
  });
});

describe("production developer-mode wiring (src/main.js)", () => {
  it("uses the same opt-in to enable the shell window's DevTools capability", () => {
    assert.match(liveCode, /webPreferences:\s*\{[\s\S]{0,400}devTools:\s*developerModeEnabled\(\)/);
  });
});

describe("workspace root bounce wiring (src/main.js)", () => {
  it("registers the bounce against the window's current pinned origin", () => {
    assert.match(
      liveCode,
      /registerWorkspaceRootBounce\(\s*win\.webContents,\s*\(\)\s*=>\s*pinnedOrigin\(win\)\s*\)/,
    );
  });
});

describe("return-to-server banner wiring (src/main.js)", () => {
  it("registers the away watch against the window's current pinned origin", () => {
    assert.match(
      liveCode,
      /registerServerAwayWatch\(\s*win\.webContents,\s*\{[\s\S]{0,400}getPinnedOrigin:\s*\(\)\s*=>\s*pinnedOrigin\(win\)/,
      [
        "src/main.js no longer registers registerServerAwayWatch in createWindow (it was",
        "removed or commented out). That watch is what shows the 'return to your server?'",
        "banner when an SSO flow navigates the window away from its server and doesn't",
        "bring it back. Re-add the call (the behavior lives in src/away_banner.js and",
        "src/return_banner.js); do not delete this test.",
      ].join(" "),
    );
  });

  it("registers the banner's IPC handlers", () => {
    assert.match(liveCode, /returnBanner\.registerIpc\(\)/);
  });

  it("shows the banner after a foreign commit outlasts the delay, hides on return", async () => {
    // End-to-end through the REAL createWindow + away_banner (return_banner
    // stubbed): the regression this guards is the banner never appearing
    // because a listener wasn't wired, the pin wasn't read, or the delay
    // option never reached the watch.
    const harness = loadNavigationHarness({ registerFallbacks: false });
    harness.api.setAwayBannerDelayMs(5);
    harness.api.createWindow("https://host.example/ml/omnigents");

    // SSO navigates the window to the IdP and leaves it there.
    harness.setUrl("https://company.okta.com/login");
    harness.emit("did-navigate", "https://company.okta.com/login", 200, "OK");
    await new Promise((resolve) => {
      setTimeout(resolve, 30);
    });

    // The window never committed an on-server page in this scenario, so the
    // offer falls back to the stored server URL.
    assert.equal(harness.bannerCalls.show.length, 1);
    assert.equal(harness.bannerCalls.show[0].win, harness.win);
    assert.equal(harness.bannerCalls.show[0].returnUrl, "https://host.example/ml/omnigents");

    // Coming back to the server hides the banner.
    harness.setUrl("https://host.example/ml/omnigents");
    harness.emit("did-navigate", "https://host.example/ml/omnigents", 200, "OK");
    assert.equal(harness.bannerCalls.hide, 1);
    harness.cleanup();
  });

  it("never offers a same-origin SSO gate page as the return target", async () => {
    // Regression: the Databricks workspace login page (login.html) is served
    // on the SAME origin as the pinned server. An origin-only watch recorded
    // it as the return target, and the banner offered to "go back" to the
    // login page the user was stuck behind.
    const harness = loadNavigationHarness({ registerFallbacks: false });
    harness.api.setAwayBannerDelayMs(5);
    harness.api.createWindow("https://host.example/ml/omnigents");

    harness.setUrl("https://host.example/ml/omnigents");
    harness.emit("did-navigate", "https://host.example/ml/omnigents", 200, "OK");
    harness.setUrl("https://host.example/login.html?next_url=%2Fml%2Fomnigents");
    harness.emit(
      "did-navigate",
      "https://host.example/login.html?next_url=%2Fml%2Fomnigents",
      200,
      "OK",
    );
    harness.setUrl("https://company.okta.com/login");
    harness.emit("did-navigate", "https://company.okta.com/login", 200, "OK");
    await new Promise((resolve) => {
      setTimeout(resolve, 30);
    });

    assert.equal(harness.bannerCalls.show.length, 1);
    assert.equal(harness.bannerCalls.show[0].returnUrl, "https://host.example/ml/omnigents");
    // Clean up the episode (hides the banner, cancels any re-arm).
    harness.setUrl("https://host.example/ml/omnigents");
    harness.emit("did-navigate", "https://host.example/ml/omnigents", 200, "OK");
    harness.cleanup();
  });

  it("does not show the banner for a quick SSO round-trip", async () => {
    const harness = loadNavigationHarness({ registerFallbacks: false });
    harness.api.setAwayBannerDelayMs(50);
    harness.api.createWindow("https://host.example/ml/omnigents");

    harness.setUrl("https://company.okta.com/login");
    harness.emit("did-navigate", "https://company.okta.com/login", 200, "OK");
    // The flow hands back to the server before the delay elapses.
    harness.setUrl("https://host.example/ml/omnigents");
    harness.emit("did-navigate", "https://host.example/ml/omnigents", 200, "OK");
    await new Promise((resolve) => {
      setTimeout(resolve, 100);
    });

    assert.equal(harness.bannerCalls.show.length, 0);
    harness.cleanup();
  });
});

describe("workspace chrome injection wiring (src/main.js)", () => {
  it("invokes registerWorkspaceChromeHide(win.webContents) as live code", () => {
    assert.match(
      liveCode,
      /registerWorkspaceChromeHide\(win\.webContents\)/,
      [
        "src/main.js no longer has a live registerWorkspaceChromeHide(win.webContents)",
        "call (it was removed or commented out). That call wires the did-finish-load",
        "listener that injects WORKSPACE_CHROME_HIDE_CSS to hide the Databricks workspace",
        "top-nav/switcher in the desktop window. Without it the switcher reappears and users",
        "can navigate out of Omnigent into other workspace apps. Re-add the call (the wiring",
        "is defined in src/workspace-chrome.js); do not delete this test.",
      ].join(" "),
    );
  });

  it("does not gate the wiring behind a URL/path check", () => {
    assert.doesNotMatch(
      liveCode,
      /registerWorkspaceChromeHide[\s\S]{0,200}(WORKSPACE_UI_PATH|pathname|startsWith)/,
      [
        "A URL/path gate was reintroduced around the chrome-hide wiring. It must stay",
        "UNCONDITIONAL: the original bug gated on pathname.startsWith(WORKSPACE_UI_PATH),",
        "which skipped injection on auth redirects and path variants and left the workspace",
        "switcher visible. The CSS targets .omnigent-app (workspace-embedded build only), so",
        "injecting on every load is a safe no-op elsewhere. See src/workspace-chrome.js.",
      ].join(" "),
    );
  });
});

describe("navigation fallback wiring (src/main.js)", () => {
  it("boots a saved Databricks API URL on the UI mount without losing URL state", () => {
    const saved = "https://workspace.cloud.databricks.com/api/2.0/omnigent/?o=123#conversation";
    const harness = loadNavigationHarness({
      savedServerUrl: saved,
      registerFallbacks: false,
    });

    harness.api.createWindow();

    assert.equal(
      harness.calls.loadURL[0][0],
      "https://workspace.cloud.databricks.com/omnigent?o=123#conversation",
    );
    harness.cleanup();
  });

  it("registers navigation fallbacks when createWindow builds a window", async () => {
    const harness = loadNavigationHarness({ registerFallbacks: false });

    const win = harness.api.createWindow("https://host.example/ml/omnigents");
    harness.emit("did-navigate", "https://host.example/ml/omnigents/", 503, "Unavailable");
    // loadSetupPage defers its navigation a tick (see main.js loadSetupPage).
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });

    assert.equal(win, harness.win);
    assert.equal(harness.hasListener("did-fail-load"), true);
    assert.equal(harness.hasListener("did-navigate"), true);
    assert.equal(harness.calls.loadFile.length, 1);
    assert.equal(harness.calls.loadFile[0][0], harness.api.SETUP_PAGE);
    harness.cleanup();
  });
});

// Wiring guards for the window-open policy (src/popupPolicy.js decides,
// main.js enforces; policy behavior is unit-tested in popupPolicy.test.js).
// Losing any of these silently reopens the chromeless-credential-window
// hole the policy exists to close.
describe("window-open policy wiring (src/main.js)", () => {
  it("routes setWindowOpenHandler decisions through decideWindowOpen as live code", () => {
    assert.match(
      liveCode,
      /setWindowOpenHandler\(\s*\(\{\s*url,\s*disposition,\s*features\s*\}\)\s*=>\s*\{[\s\S]{0,200}decideWindowOpen\(/,
      [
        "src/main.js no longer passes window.open through decideWindowOpen. Either every",
        "popup is denied (OAuth sign-in breaks) or popups open without the",
        "pinned-opener/https/allowlist conditions. Restore the dispatch.",
      ].join(" "),
    );
  });

  it("attaches the no-op popup preload and sandbox to allowed popups", () => {
    assert.match(
      liveCode,
      /preload:\s*POPUP_PRELOAD[\s\S]{0,120}sandbox:\s*true/,
      [
        "Allowed popups no longer force preload: POPUP_PRELOAD + sandbox: true, so a child",
        "window can inherit the SHELL preload's IPC bridges while showing third-party",
        "sign-in pages. Restore both overrides (see popup_preload.js).",
      ].join(" "),
    );
  });

  it("hardens created popups via did-create-window → hardenOauthPopup as live code", () => {
    assert.match(
      liveCode,
      /did-create-window[\s\S]{0,120}hardenOauthPopup\(/,
      [
        "Allowed popups no longer run through hardenOauthPopup (host-stamped title, no",
        "popups-from-popups, localhost-trust registration). Re-add the wiring.",
      ].join(" "),
    );
  });
});

// Guards for the popup ↔ localhost-trust bridge. E2E-verified failure when
// lost: Okta FastPass queries the LNA permission from inside the popup,
// gets "denied" (a popup is not a shell window), and fails closed —
// blocking sign-in for every Okta-fronted provider.
describe("OAuth popup localhost trust wiring (src/main.js)", () => {
  it("registers popups in oauthPopups inside hardenOauthPopup as live code", () => {
    assert.match(
      liveCode,
      /function hardenOauthPopup\(child\)\s*\{[\s\S]{0,120}oauthPopups\.add\(child\)/,
      [
        "hardenOauthPopup no longer registers the popup in oauthPopups, so",
        "isCurrentPopupOrigin never matches and Okta FastPass fails closed inside every",
        "sign-in popup. Restore oauthPopups.add(child) + the closed → delete cleanup.",
      ].join(" "),
    );
  });

  it("extends isLocalhostTrustedOrigin to live popup pages as live code", () => {
    assert.match(
      liveCode,
      /function isLocalhostTrustedOrigin\(origin\)\s*\{[\s\S]{0,300}isCurrentPopupOrigin\(origin\)/,
      [
        "isLocalhostTrustedOrigin no longer consults isCurrentPopupOrigin, so popup IdP",
        "pages get a denied LNA answer and Okta FastPass fails closed. Restore the check.",
      ].join(" "),
    );
  });
});

// Guard for the COOP-strip wiring. E2E-verified failure when lost: a
// COOP: same-origin sign-in hop (slack.com) severs the popup's
// window.opener, so every FIRST sign-in through such a provider fails and
// only retries succeed.
describe("OAuth popup COOP-strip wiring (src/main.js)", () => {
  it("composes popupResponseHeadersHook into the localhost-CORS registration as live code", () => {
    assert.match(
      liveCode,
      /registerLocalhostCors\(\s*session\.defaultSession,\s*isLocalhostTrustedOrigin,\s*popupResponseHeadersHook,?\s*\)/,
      [
        "registerLocalhostAccess no longer passes popupResponseHeadersHook to",
        "registerLocalhostCors (which owns the session's single onHeadersReceived),",
        "so COOP-serving sign-in pages sever window.opener and first-time OAuth",
        "sign-ins fail. Restore the third argument.",
      ].join(" "),
    );
  });

  it("scopes the strip to main-frame responses of tracked popups", () => {
    assert.match(
      liveCode,
      /function popupResponseHeadersHook\(details\)\s*\{[\s\S]{0,200}resourceType[\s\S]{0,240}isOauthPopupWebContentsId\(/,
      [
        "popupResponseHeadersHook lost its mainFrame/tracked-popup scoping — stripping",
        "COOP anywhere else disables a real isolation protection on ordinary browsing.",
        "Restore the resourceType + isOauthPopupWebContentsId guards.",
      ].join(" "),
    );
  });
});

describe("recent-server startup wiring (src/main.js)", () => {
  it("backfills a saved server only after its cold load succeeds", () => {
    assert.match(
      liveCode,
      /loadURL\(destination\)\s*\.then\(\(\)\s*=>\s*\{\s*if\s*\(!ephemeral\s*&&\s*!explicit\s*&&\s*serverUrl\)[\s\S]{0,200}rememberRecentServer\(settings,\s*serverUrl\)/,
      [
        "createWindow no longer backfills a successfully loaded saved server into",
        "recent_servers. Existing installs can have server_url without recent_servers,",
        "so the setup page would show no recents after leaving that server. Keep the",
        "backfill in loadURL(destination).then, gated away from ephemeral windows and",
        "explicit target URLs (which may include a conversation path).",
      ].join(" "),
    );
  });

  it("normalizes persisted targets and excludes managed origins from setup recents", () => {
    assert.match(
      liveCode,
      /ipcMain\.handle\("omnigent:get-recent-servers"[\s\S]{0,400}excludingManagedServers\(\s*normalizeRecentServers\(loadSettings\(\)\.recent_servers\),\s*managed/,
    );
  });
});

// Guard for the deep-link path join in createWindow. A basename-less SPA path
// (/c/<id>) lives UNDER the server's workspace mount (/omnigent), so it
// must be string-concatenated (resolveServerPath) — NOT resolved with
// `new URL(path, serverUrl)`, which would anchor against the ORIGIN and drop
// the mount, opening the wrong URL for every workspace deep link. This catches
// a "simplification" the behavior tests can't (createWindow isn't unit-tested).
describe("deep-link path join wiring (src/main.js)", () => {
  it("joins opts.path onto opts.serverUrl via resolveServerPath as live code", () => {
    assert.match(
      liveCode,
      /resolveServerPath\(serverUrl, opts\.path\)/,
      [
        "createWindow no longer joins opts.path onto opts.serverUrl via",
        "resolveServerPath. A deep link to a workspace server (origin + /omnigent",
        "mount) would lose the mount and 404. Restore the mount-aware join (see",
        "resolveServerPath); do not replace it with `new URL(path, serverUrl)`.",
      ].join(" "),
    );
  });

  it("stores the clean serverUrl (no conversation path) separately from loadUrl", () => {
    // The window's server IDENTITY (for `omnigent host --server` etc.) must not
    // carry the /c/<id> path. Guard that createWindow sets `serverUrl: serverUrl`
    // (the clean value), not `serverUrl: destination`/`loadUrl`.
    assert.match(
      liveCode,
      /serverUrl:\s*destination\s*\?\s*serverUrl\s*:\s*null/,
      [
        "createWindow no longer stores the clean serverUrl as the window's server",
        "identity — it must keep the /c/<id> path out of `omnigent host --server`.",
        "Restore `serverUrl: destination ? serverUrl : null` in the windows.set call.",
      ].join(" "),
    );
  });
});

// Guards for the deep-link INGESTION + ORCHESTRATION wiring. The pure
// decision logic is unit-tested in deepLink.test.js; these guard that main.js
// still wires the OS entry points (open-url / second-instance / argv), the
// serialized queue, the protocol registration, and the orchestrator — the
// half no behavior test can see. Losing any silently reopens the readiness
// race (macOS open-url before whenReady) or the single-instance funnel.
describe("deep-link ingestion wiring (src/main.js)", () => {
  it("registers open-url with preventDefault + enqueueDeepLink as live code", () => {
    assert.match(
      liveCode,
      /app\.on\("open-url"[\s\S]{0,120}event\.preventDefault\(\)[\s\S]{0,80}enqueueDeepLink\(/,
      [
        "main.js no longer handles the macOS `open-url` event. Without preventDefault",
        "the OS also hands the URL to the default browser, and without enqueueDeepLink",
        "the pre-ready race (open-url can fire before whenReady) touches windows that",
        "don't exist yet. Restore app.on('open-url') → preventDefault + enqueueDeepLink.",
      ].join(" "),
    );
  });

  it("scans second-instance argv for omnigent:// and enqueues as live code", () => {
    assert.match(
      liveCode,
      /app\.on\("second-instance"[\s\S]{0,220}startsWith\("omnigent:\/\/"\)[\s\S]{0,60}enqueueDeepLink\(/,
      [
        "main.js no longer scans second-instance argv for omnigent://. Windows/Linux",
        "warm-start deep links (a second launch funneled by the single-instance lock)",
        "would be ignored. Restore the argv scan → enqueueDeepLink inside second-instance.",
      ].join(" "),
    );
  });

  it("registers the omnigent:// scheme as live code", () => {
    assert.match(
      liveCode,
      /setAsDefaultProtocolClient\("omnigent"\)/,
      [
        "main.js no longer calls app.setAsDefaultProtocolClient('omnigent'), so dev",
        "(`electron .`) clicks on an omnigent:// link won't route to the running dev",
        "instance. The packaged build's manifest registration is separate (package.json",
        "build.protocols). Restore the runtime call.",
      ].join(" "),
    );
  });

  it("gates the launch window on pending deep links as live code", () => {
    assert.match(
      liveCode,
      /pendingDeepLinks\.length > 0[\s\S]{0,80}drainPendingDeepLinks\(\)/,
      [
        "main.js no longer drains pending deep links instead of opening the default",
        "launch window, so a startup deep link would open a redundant default window",
        "next to the deep-link window. Restore the pendingDeepLinks gate in whenReady.",
      ].join(" "),
    );
  });

  it("drains the queue serialized via handleDeepLink as live code", () => {
    assert.match(
      liveCode,
      /void handleDeepLink\(/,
      [
        "main.js no longer calls handleDeepLink from the drain, so queued deep links",
        "would never be opened. Restore `void handleDeepLink(next)` in drainPendingDeepLinks.",
      ].join(" "),
    );
  });

  it("routes in-place navigation through the omnigent:open-path channel", () => {
    assert.match(
      liveCode,
      /send\("omnigent:open-path"/,
      [
        "main.js no longer sends omnigent:open-path to the SPA, so reuse-inplace deep",
        "links would focus a window without navigating it. Restore sendOpenPath's",
        "webContents.send('omnigent:open-path', path).",
      ].join(" "),
    );
  });

  it("decides via chooseDeepLinkStrategy as live code", () => {
    assert.match(
      liveCode,
      /chooseDeepLinkStrategy\(\{[\s\S]{0,80}targetOrigin[\s\S]{0,260}knownOrigins:/,
      [
        "main.js no longer drives deep-link window selection through the PURE",
        "chooseDeepLinkStrategy (unit-tested in deepLink.test.js). Inlining the",
        "decision would lose the reuse/reload/consent table. Restore the call.",
      ].join(" "),
    );
  });

  it("reloads/repoints via loadServerUrl(..., parsed.path) as live code", () => {
    assert.match(
      liveCode,
      /loadServerUrl\(\w+, \w+, parsed\.path\)/,
      [
        "main.js no longer reloads/repoints through loadServerUrl, so the mount-aware",
        "join and the clean-serverUrl identity (no /c/<id>) could be bypassed by a",
        "raw win.loadURL. Restore a loadServerUrl(<win>, <serverUrl>, parsed.path) call.",
      ].join(" "),
    );
  });

  it("runs the workspace mount probe only AFTER consent (no pre-consent SSRF)", () => {
    // The probe (expandDatabricksWorkspaceUrl) makes an HTTP request to the
    // link's host. For an UNKNOWN server that host is attacker-chosen, so the
    // probe must not run until the user has consented — otherwise clicking a
    // link probes an arbitrary host (SSRF / info disclosure) with no approval.
    // Guard that the probe call follows confirmOpenDeepLink inside the
    // consent-unknown branch, and does NOT appear before chooseDeepLinkStrategy.
    assert.match(
      liveCode,
      /confirmOpenDeepLink\(parent, targetOrigin\)[\s\S]{0,300}expandDatabricksWorkspaceUrl\(targetOrigin\)/,
      [
        "handleDeepLink no longer defers expandDatabricksWorkspaceUrl until AFTER",
        "confirmOpenDeepLink. A deep link to an unknown (attacker-chosen) server would",
        "make a pre-consent HTTP request to that host (SSRF / info disclosure). Move the",
        "probe into the consent-unknown branch, after confirmOpenDeepLink — the consent",
        "decision can run on parsed.origin (no fetch) since the probe only appends a path",
        "under the same origin.",
      ].join(" "),
    );
    assert.doesNotMatch(
      liveCode,
      /function handleDeepLink\(raw\)\s*\{[\s\S]{0,500}expandDatabricksWorkspaceUrl\(/,
      [
        "expandDatabricksWorkspaceUrl reappeared in the pre-decision section of",
        "handleDeepLink, reopening the pre-consent SSRF. The probe must run only after",
        "confirmOpenDeepLink (in the consent-unknown branch), not before chooseDeepLinkStrategy.",
      ].join(" "),
    );
  });
});

// HTTP 4xx/5xx commits as a successful Chromium navigation (empty body → black
// window against backgroundColor), so did-fail-load never fires. did-navigate
// carries httpResponseCode for main-frame navigations — fall back to setup
// with ?error=&url= the same way the net-error path does.
describe("HTTP error status fallback (src/main.js)", () => {
  // loadSetupPage defers its navigation to the next tick (see main.js), so the
  // fallback's loadFile lands a macrotask after the event is emitted.
  const flush = () =>
    new Promise((resolve) => {
      setTimeout(resolve, 0);
    });

  it("routes a 404 to the setup error surface with the mounted server URL", async (t) => {
    const harness = loadNavigationHarness();
    t.after(harness.cleanup);

    harness.emit("did-navigate", "https://host.example/ml/omnigents/health", 404, "Not Found");
    await flush();

    assert.equal(harness.calls.loadFile.length, 1);
    assert.equal(harness.calls.loadFile[0][0], harness.api.SETUP_PAGE);
    const params = new URLSearchParams(harness.calls.loadFile[0][1].search);
    assert.equal(params.get("error"), "404 Not Found");
    assert.equal(params.get("url"), "https://host.example/ml/omnigents");
  });

  it("routes a 503 to the same setup error surface", async (t) => {
    const harness = loadNavigationHarness();
    t.after(harness.cleanup);

    harness.emit("did-navigate", "https://host.example/ml/omnigents/", 503, "Service Unavailable");
    await flush();

    assert.equal(harness.calls.loadFile.length, 1);
    const params = new URLSearchParams(harness.calls.loadFile[0][1].search);
    assert.equal(params.get("error"), "503 Service Unavailable");
    assert.equal(params.get("url"), "https://host.example/ml/omnigents");
  });

  it("does not fall back for successful or redirect navigations", async (t) => {
    const harness = loadNavigationHarness();
    t.after(harness.cleanup);

    harness.emit("did-navigate", "https://host.example/ml/omnigents/", 200, "OK");
    harness.emit("did-navigate", "https://host.example/ml/omnigents/login", 302, "Found");
    await flush();

    assert.deepEqual(harness.calls.loadFile, []);
  });

  it("ignores a duplicate failure after the first fallback unpins the window", async (t) => {
    const harness = loadNavigationHarness();
    t.after(harness.cleanup);

    const failedUrl = "https://host.example/ml/omnigents/health";
    harness.emit("did-navigate", failedUrl, 503, "Service Unavailable");

    assert.equal(harness.api.windows.get(harness.win).origin, null);
    // Unpinning makes the origin guard reject re-entry.
    harness.emit("did-navigate", failedUrl, 503, "Service Unavailable");
    await flush();

    assert.equal(harness.calls.loadFile.length, 1);
  });

  it("keeps the network-error fallback and ignores ERR_ABORTED", async (t) => {
    const harness = loadNavigationHarness();
    t.after(harness.cleanup);

    harness.emit(
      "did-fail-load",
      -105,
      "NAME_NOT_RESOLVED",
      "https://host.example/ml/omnigents/",
      true,
    );
    await flush();
    assert.equal(harness.calls.loadFile.length, 1);

    const aborted = loadNavigationHarness();
    t.after(aborted.cleanup);
    aborted.emit("did-fail-load", -3, "ABORTED", "https://host.example/ml/omnigents/", true);
    await flush();
    assert.deepEqual(aborted.calls.loadFile, []);
  });
});

// pinWindow is the one chokepoint every "leave this server" path routes through
// (Connect to new server, Change Server…, switch-server, did-fail-load fallback).
// Those navigations tear down the renderer WITHOUT running BrowserPane's unmount
// detach, so pinWindow must close the window's browser registry when the origin
// changes — else the native WebContentsView dangles over the setup/welcome page.
describe("browser-view teardown on server change (src/main.js)", () => {
  it("closes the window's browserRegistry when pinWindow changes origin", () => {
    assert.match(
      liveCode,
      /function pinWindow\(win,\s*origin\)\s*\{[\s\S]{0,600}browserRegistry\?\.closeAll\(/,
      [
        "pinWindow no longer closes the window's embedded-browser views when the",
        "origin changes. Leaving a server (Connect to new server / Change Server / switch)",
        "navigates the window away and tears down the renderer WITHOUT running",
        "BrowserPane's unmount detach, so the native WebContentsView keeps painting over",
        "the setup/welcome page. Restore the closeAll call in pinWindow.",
      ].join(" "),
    );
  });

  it("guards the teardown so the initial cold-connect pin doesn't fire it", () => {
    assert.match(
      liveCode,
      /function pinWindow\(win,\s*origin\)\s*\{[\s\S]{0,600}state\.origin\s*!=\s*null[\s\S]{0,120}browserRegistry\?\.closeAll\(/,
      [
        "The closeAll in pinWindow is no longer guarded on a prior origin. Without the",
        "state.origin != null guard the initial pin (setup→first connect) would try to",
        "close a registry with nothing open. Keep the guard.",
      ].join(" "),
    );
  });
});
