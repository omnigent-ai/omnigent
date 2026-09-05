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

const { runInNewContext } = vm;

const {
  isSetupIdle,
  loadInitialDestination,
  loadServerAfterAuth,
  withServerLoad,
} = require("../src/server_load");
const { joinServerUrl, normalizeUrl, workspaceIdentityKey } = require("../src/url");
const {
  oidcServerUrlError,
  runOidcBrowserLogin,
  installAndVerifySessionCookie,
} = require("../src/oidc_auth");

const mainSource = readFileSync(path.join(__dirname, "../src/main.js"), "utf8");
const omnigentCliSource = readFileSync(path.join(__dirname, "../src/omnigent_cli.js"), "utf8");
const preloadSource = readFileSync(path.join(__dirname, "../src/preload.js"), "utf8");
const setupSource = readFileSync(path.join(__dirname, "../setup/index.html"), "utf8");
const urlHelpers = require("../src/url");

const startLocalHandlerSource = setupSource.match(
  /startLocalBtn\.addEventListener\("click",\s*(async \(\) => \{[\s\S]*?\n        \})\);/,
)?.[1];
const setupConnectSource = setupSource.match(
  /async function connect\(\) \{[\s\S]*?\n      \}/,
)?.[0];

// Strip block comments, then line comments (leaving `://` in URLs intact).
const liveCode = mainSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
const oidcSessionCode = liveCode.match(
  /async function runWindowOidcBrowserHandoff[\s\S]*?(?=async function loadAuthenticatedServerUrl)/,
)?.[0];
const webAuthnTimeoutCode = liveCode.match(
  /async function showWebAuthnTimeout[\s\S]*?(?=async function runWindowOidcBrowserHandoff)/,
)?.[0];
const switchServerCode = liveCode.match(
  /ipcMain\.handle\("omnigent:switch-server"[\s\S]*?(?=ipcMain\.on\("omnigent:open-server-setup")/,
)?.[0];
const openServerSetupCode = liveCode.match(
  /ipcMain\.on\("omnigent:open-server-setup"[\s\S]*?(?=ipcMain\.on\("omnigent:find-query")/,
)?.[0];
const setupServerCode = liveCode.match(
  /ipcMain\.handle\("omnigent:set-server-url"[\s\S]*?(?=ipcMain\.handle\("omnigent:get-server-url")/,
)?.[0];
const createWindowCode = liveCode.match(
  /function createWindow\(targetUrl, opts = \{\}\)[\s\S]*?(?=const MAX_SPELL_SUGGESTIONS)/,
)?.[0];
const serverLoadCode = liveCode.match(
  /async function loadAuthenticatedServerUrl[\s\S]*?(?=async function loadSetupPage)/,
)?.[0];
const expiryHandoffCallbackCode = createWindowCode?.match(
  /async \(\{ serverUrl: expiredServerUrl, returnUrl \}\) => \{[\s\S]*?\n    \}/,
)?.[0];
const expiryHandoffServerUrlGetterCode = createWindowCode?.match(
  /registerOidcSessionExpiryHandoff\(\s*win\.webContents,\s*(\(\) => \{[\s\S]*?\n    \}),/,
)?.[1];
const oauthPopupCode = liveCode.match(
  /function hardenOauthPopup\(child\)[\s\S]*?(?=async function showWebAuthnTimeout)/,
)?.[0];
const deepLinkHandlerCode = liveCode.match(
  /async function handleDeepLink\(raw\)[\s\S]*?(?=app\.setName)/,
)?.[0];

async function runConsentUnknownDeepLink(
  initialPendingLoads,
  { closeDuringExpansion = false, expandedServerUrl = "https://unknown.example" } = {},
) {
  const state = {
    origin: null,
    identity: null,
    pendingServerLoads: initialPendingLoads,
    serverUrl: null,
  };
  let destroyed = false;
  const parent = {
    isDestroyed: () => destroyed,
    webContents: { getURL: () => "file:///setup" },
  };
  const windowStates = new Map([[parent, state]]);
  const parentLoads = [];
  const created = [];
  const pendingDuringExpansion = [];
  const remembered = [];
  const handler = runInNewContext(`${deepLinkHandlerCode}; handleDeepLink`, {
    BrowserWindow: { getFocusedWindow: () => parent },
    activeWindow: () => parent,
    chooseDeepLinkStrategy: () => ({ strategy: "consent-unknown" }),
    confirmOpenDeepLink: async () => true,
    console: { log: () => {} },
    createWindow: (_target, options) => {
      created.push(options);
      return {};
    },
    expandDatabricksWorkspaceUrl: async () => {
      pendingDuringExpansion.push(state.pendingServerLoads);
      if (closeDuringExpansion) {
        destroyed = true;
        windowStates.delete(parent);
      }
      return expandedServerUrl;
    },
    expandedServerUrlError: (serverUrl, expectedIdentity) =>
      oidcServerUrlError(serverUrl) ??
      (workspaceIdentityKey(serverUrl) === expectedIdentity ? null : "invalid_server_url"),
    findKnownServerUrl: () => null,
    focusAndRestore: () => {},
    isSetupIdle,
    knownWorkspaceIdentities: () => [],
    loadServerUrl: async (win) => {
      parentLoads.push(win);
      return true;
    },
    workspaceIdentityKey,
    oidcServerUrlError,
    parseOmnigentDeepLink: () => ({ origin: "https://unknown.example", path: "/c/1" }),
    rememberServerUrl: (url) => remembered.push(url),
    windows: windowStates,
    withServerLoad,
  });

  await handler("omnigent://unknown.example/c/1");
  return { created, parent, parentLoads, pendingDuringExpansion, remembered, state };
}

function loadNavigationHarness({
  serverUrl = "https://host.example/ml/omnigents",
  savedServerUrl,
  registerFallbacks = true,
  rejectAuthSideEffects = false,
} = {}) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-navigation-test-"));
  if (savedServerUrl) {
    fs.writeFileSync(
      path.join(userData, "settings.json"),
      JSON.stringify({ server_url: savedServerUrl }),
    );
  }
  const listeners = new Map();
  const calls = { closeAll: [], loadFile: [], loadURL: [] };
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
      return rejectAuthSideEffects
        ? Promise.reject(new Error("invalid server URL reached navigation"))
        : Promise.resolve();
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
        fromWebContents: (contents) => (contents === webContents ? win : null),
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
      normalizeRecentServers: (urls) => urls,
      workspaceIdentityKey,
      joinServerUrl,
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
    "./session-expiry": {
      registerSessionExpiryReload: () => {},
      registerOidcSessionExpiryHandoff: () => {},
    },
    "./popupPolicy": {
      decideWindowOpen: () => ({ kind: "ignore" }),
      stripCrossOriginOpenerHeaders: () => {},
      WEB_SCHEMES: new Set(),
    },
    "./oidc_auth": {
      OIDC_LOGIN_TIMEOUT_MS: 300_000,
      oidcServerUrlError,
      probeServerAuth: async () => {
        if (rejectAuthSideEffects) assert.fail("invalid server URL reached authentication probe");
        return { kind: "other" };
      },
      runOidcBrowserLogin: async () => ({ ok: false, reason: "cancelled" }),
      installAndVerifySessionCookie: async () => {
        if (rejectAuthSideEffects) assert.fail("invalid server URL reached cookie installation");
      },
    },
    "./oidc_login_dialog": {
      runOidcLoginDialog: async () => {
        if (rejectAuthSideEffects) assert.fail("invalid server URL constructed the OIDC modal");
        return false;
      },
    },
    "./webauthn_timeout": {
      isWebAuthnEscapePage: () => false,
      registerWebAuthnTimeout: () => {},
    },
    "./omnigent_cli": {
      isExecutableFile: () => false,
      isLoopbackServer: () => false,
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
    "\nmodule.exports.testApi = { createWindow, isPinnedWorkspaceSender, pinWindow, registerNavigationFallbacks, windows, SETUP_PAGE, setAwayBannerDelayMs: (ms) => { awayBannerDelayMs = ms; } };";
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
    identity: workspaceIdentityKey(serverUrl),
    serverUrl,
    ephemeral: false,
    badgeCount: 0,
    browserRegistry: { closeAll: (reason) => calls.closeAll.push(reason) },
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
    waitForPin: async () => {
      await api.windows.get(win)?.pendingLoad;
      assert.ok(api.windows.get(win)?.origin, "window did not finish its authenticated pin");
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
    assert.match(preloadSource, /serverDisplayLabel[,\s]/);
  });

  it("returns an exact managed mount without normalizing it", async () => {
    const managedUrl = "https://mdm.example.com/ml/omnigents";
    const state = { ephemeral: true, serverUrl: null };
    const win = { isDestroyed: () => false };
    const calls = [];
    const handlerSource = setupServerCode.slice(
      setupServerCode.indexOf("async"),
      setupServerCode.lastIndexOf(");"),
    );
    const handler = runInNewContext(`(${handlerSource})`, {
      BrowserWindow: { fromWebContents: () => win },
      configuredServerUrlError: (_raw, normalized) => oidcServerUrlError(normalized),
      configuredServerUrlErrorMessage: () => "Invalid server.",
      expandDatabricksWorkspaceUrl: async (url) => {
        calls.push(["expand", url]);
        return url;
      },
      expandedServerUrlError: (url, expectedIdentity) =>
        oidcServerUrlError(url) ??
        (workspaceIdentityKey(url) === expectedIdentity ? null : "invalid_server_url"),
      isSetupPageSender: () => true,
      loadServerUrl: async (_win, url) => {
        calls.push(["load", url]);
        return true;
      },
      managedServerUrls: () => [managedUrl],
      normalizeUrl: () => assert.fail("normalized a managed mount"),
      serverSelectorV2Enabled: () => false,
      workspaceIdentityKey,
      windows: new Map([[win, state]]),
      withServerLoad,
    });

    const result = await handler({ sender: {} }, managedUrl);

    assert.equal(result.loaded, true);
    assert.deepEqual(calls, [
      ["expand", managedUrl],
      ["load", managedUrl],
    ]);
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
    await harness.waitForPin();

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
    await harness.waitForPin();

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
    await harness.waitForPin();

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

describe("setup Start locally", () => {
  it("restores the button when the connection is not accepted", async () => {
    assert.ok(startLocalHandlerSource);
    const startLocalBtn = { disabled: false, textContent: "Start locally" };
    const err = { textContent: "" };
    const input = { value: "" };
    const handler = runInNewContext(`(${startLocalHandlerSource})`, {
      cliInstalled: true,
      err,
      input,
      setup: {
        setServerUrl: async () => ({ loaded: false }),
        startLocalServer: async () => ({ ok: true, url: "http://localhost:8000" }),
      },
      startLocalBtn,
    });

    await handler();

    assert.equal(startLocalBtn.disabled, false);
    assert.equal(startLocalBtn.textContent, "Start locally");
    assert.equal(err.textContent, "");
  });
});

describe("setup Connect", () => {
  it("surfaces the canonical remote HTTP rejection on the first attempt", async () => {
    assert.ok(setupConnectSource);
    const button = { disabled: false };
    const err = { textContent: "" };
    const input = { value: "http://server.example" };
    let attempts = 0;
    const connect = runInNewContext(`${setupConnectSource}; connect`, {
      button,
      err,
      input,
      isPlainHttpRemote: () => true,
      setup: {
        setServerUrl: async () => {
          attempts += 1;
          return { loaded: false, error: "Remote servers require HTTPS." };
        },
      },
      warnedFor: null,
    });

    await connect();

    assert.equal(attempts, 1);
    assert.equal(err.textContent, "Remote servers require HTTPS.");
    assert.equal(button.disabled, false);
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
  it("boots a saved Databricks API URL on the UI mount without losing URL state", async () => {
    const saved = "https://workspace.cloud.databricks.com/api/2.0/omnigent/?o=123#conversation";
    const harness = loadNavigationHarness({
      savedServerUrl: saved,
      registerFallbacks: false,
    });

    harness.api.createWindow();
    await harness.waitForPin();

    assert.equal(
      harness.calls.loadURL[0][0],
      "https://workspace.cloud.databricks.com/omnigent?o=123#conversation",
    );
    harness.cleanup();
  });

  it("registers navigation fallbacks when createWindow builds a window", async () => {
    const harness = loadNavigationHarness({ registerFallbacks: false });

    const win = harness.api.createWindow("https://host.example/ml/omnigents");
    await new Promise(setImmediate);
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

  it("surfaces an unsafe saved server URL without authentication side effects", async () => {
    const serverUrl = "https://host.example/ml/omnigents?workspace=one";
    const harness = loadNavigationHarness({
      serverUrl,
      registerFallbacks: false,
      rejectAuthSideEffects: true,
    });

    harness.api.createWindow(serverUrl);
    await new Promise(setImmediate);
    // loadSetupPage defers its navigation a tick (see main.js loadSetupPage).
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });

    assert.equal(harness.calls.loadFile.length, 1);
    const [, options] = harness.calls.loadFile[0];
    const search = new URLSearchParams(options.search);
    assert.match(search.get("error"), /server address is invalid/i);
    assert.equal(search.get("url"), serverUrl);
    harness.cleanup();
  });
});

describe("remote OIDC browser handoff wiring (src/main.js)", () => {
  it("keeps js-yaml outside the main process startup import path", () => {
    assert.match(mainSource, /const omnigentCli = require\("\.\/omnigent_cli"\)/);
    const loaderIndex = omnigentCliSource.indexOf("function loadYamlParser");
    assert.notEqual(loaderIndex, -1);
    assert.doesNotMatch(omnigentCliSource.slice(0, loaderIndex), /require\("js-yaml"\)/);
    assert.match(omnigentCliSource.slice(loaderIndex), /require\("js-yaml"\)/);
  });

  it("wiring-only: uses the main-process ticket client without requiring the CLI", () => {
    assert.ok(oidcSessionCode);
    assert.match(oidcSessionCode, /runOidcBrowserLogin\(/);
    assert.match(oidcSessionCode, /onPollError:[\s\S]{0,180}updateMessage\(/);
    assert.doesNotMatch(oidcSessionCode, /resolvedCliPath|omnigentCli\.loginServer/);
    assert.doesNotMatch(oidcSessionCode, /storeServerAuthToken|auth_tokens\.json/);
  });

  it("wiring-only: deduplicates concurrent OIDC flows per shell window", () => {
    assert.match(liveCode, /const oidcLoginFlows = new WeakMap\(\)/);
    assert.match(
      liveCode,
      /oidcLoginFlows\.get\(win\)[\s\S]{0,180}existingFlow\?\.serverUrl === serverUrl[\s\S]{0,80}existingFlow\.promise/,
    );
  });

  it("rejects unsafe OIDC server URLs before the authenticated probe", async () => {
    const ensureWindowOidcSession = runInNewContext(`${oidcSessionCode}; ensureWindowOidcSession`, {
      AbortController,
      BrowserWindow: function BrowserWindow() {},
      installAndVerifySessionCookie: async () => assert.fail("installed a cookie"),
      ipcMain: {},
      OIDC_LOGIN_PAGE: "/oidc_login.html",
      OIDC_LOGIN_PRELOAD: "/oidc_login_preload.js",
      OIDC_LOGIN_TIMEOUT_MS: 100,
      oidcLoginFlows: new WeakMap(),
      oidcServerUrlError,
      omnigentCli: { isLoopbackServer: () => false },
      probeServerAuth: async () => assert.fail("sent an authenticated probe"),
      runOidcBrowserLogin,
      runOidcLoginDialog: async () => assert.fail("constructed the OIDC modal"),
      session: { defaultSession: { fetch: async () => assert.fail("made a request") } },
      setWindowAuthenticationNavigation() {},
      shell: { openExternal: async () => assert.fail("opened a URL") },
      URL,
    });

    await Promise.all(
      [
        [
          "http://server.example",
          "Browser sign-in requires HTTPS for remote servers. Update the server URL and retry.",
        ],
        [
          "https://user:secret@server.example",
          "The server address is invalid. Return to setup, correct it, and retry.",
        ],
        [
          "ftp://server.example",
          "The server address is invalid. Return to setup, correct it, and retry.",
        ],
        ["not a url", "The server address is invalid. Return to setup, correct it, and retry."],
      ].map(async ([serverUrl]) => {
        const result = await ensureWindowOidcSession({}, serverUrl);
        assert.equal(result, false);
      }),
    );
  });

  it("bypasses the cached CLI token when the login navigation is explicit", async () => {
    // The expiry handoff fires on the renderer's OWN /auth/login navigation —
    // sign-out or account switch as much as expiry. Silently reinstalling a
    // still-valid cached CLI token there signs the user straight back in with
    // no way out; forceInteractive must skip the cache and run the modal flow.
    const makeSession = () => {
      const calls = { cacheReads: 0, installs: 0, dialogs: 0 };
      const ensureWindowOidcSession = runInNewContext(
        `${oidcSessionCode}; ensureWindowOidcSession`,
        {
          BrowserWindow: function BrowserWindow() {},
          console: { log: () => {} },
          installAndVerifySessionCookie: async () => {
            calls.installs += 1;
          },
          ipcMain: {},
          OIDC_LOGIN_PAGE: "/oidc_login.html",
          OIDC_LOGIN_PRELOAD: "/oidc_login_preload.js",
          OIDC_LOGIN_TIMEOUT_MS: 100,
          oidcLoginFlows: new WeakMap(),
          oidcServerUrlError,
          omnigentCli: {
            isLoopbackServer: () => false,
            serverAuthEntry: () => {
              calls.cacheReads += 1;
              return { token: "cached-token" };
            },
          },
          probeServerAuth: async () => ({ kind: "oidc" }),
          runOidcBrowserLogin,
          runOidcLoginDialog: async () => {
            calls.dialogs += 1;
            return true;
          },
          session: { defaultSession: {} },
          setWindowAuthenticationNavigation() {},
          shell: {},
          URL,
        },
      );
      return { ensureWindowOidcSession, calls };
    };

    const cached = makeSession();
    assert.equal(await cached.ensureWindowOidcSession({}, "https://server.example"), true);
    assert.deepEqual(cached.calls, { cacheReads: 1, installs: 1, dialogs: 0 });

    const forced = makeSession();
    assert.equal(
      await forced.ensureWindowOidcSession({}, "https://server.example", {
        forceInteractive: true,
      }),
      true,
    );
    assert.deepEqual(forced.calls, { cacheReads: 0, installs: 0, dialogs: 1 });
  });

  it("routes loopback OIDC servers through the browser handoff", async () => {
    const makeSession = (kind, calls) =>
      runInNewContext(`${oidcSessionCode}; ensureWindowOidcSession`, {
        BrowserWindow: function BrowserWindow() {},
        installAndVerifySessionCookie: async () => {},
        ipcMain: {},
        OIDC_LOGIN_PAGE: "/oidc_login.html",
        OIDC_LOGIN_PRELOAD: "/oidc_login_preload.js",
        OIDC_LOGIN_TIMEOUT_MS: 100,
        oidcLoginFlows: new WeakMap(),
        oidcServerUrlError,
        omnigentCli: { serverAuthEntry: () => null },
        probeServerAuth: async (_session, serverUrl) => {
          calls.probes.push(serverUrl);
          return { kind };
        },
        runOidcBrowserLogin,
        runOidcLoginDialog: async ({ serverUrl }) => {
          calls.dialogs.push(serverUrl);
          return true;
        },
        session: { defaultSession: {} },
        setWindowAuthenticationNavigation() {},
        shell: {},
        URL,
      });

    // Every loopback spelling is probed; an OIDC-mode server (a tunneled or
    // port-forwarded deployment) signs in via the system browser rather than
    // rendering the IdP in-window.
    const oidc = { probes: [], dialogs: [] };
    const viaHandoff = makeSession("oidc", oidc);
    const loopbacks = ["http://localhost:6767", "http://127.0.0.1:6767", "http://[::1]:6767"];
    const results = await Promise.all(loopbacks.map((serverUrl) => viaHandoff({}, serverUrl)));
    assert.deepEqual(results, [true, true, true]);
    assert.deepEqual(oidc.probes, loopbacks);
    assert.deepEqual(oidc.dialogs, loopbacks);

    // A local no-auth dev server (probe: authenticated) still connects directly.
    const local = { probes: [], dialogs: [] };
    const direct = makeSession("authenticated", local);
    assert.equal(await direct({}, "http://localhost:6767"), true);
    assert.deepEqual(local.probes, ["http://localhost:6767"]);
    assert.deepEqual(local.dialogs, []);
  });

  it("wiring-only: includes loopback servers in expiry handoff", () => {
    assert.ok(expiryHandoffServerUrlGetterCode);
    assert.doesNotMatch(expiryHandoffServerUrlGetterCode, /isLoopbackServer/);
  });

  it("completes the WebAuthn escape through ticket, cookie verification, and one reload", async () => {
    assert.ok(webAuthnTimeoutCode);
    let stored = null;
    const fetches = [];
    const electronSession = {
      cookies: {
        set: async (details) => {
          stored = { ...details };
        },
        get: async () => [stored],
      },
      fetch: async (url) => {
        fetches.push(url);
        if (url.endsWith("/auth/cli-login")) {
          return {
            status: 200,
            json: async () => ({ ticket: "one-time", login_url: "/auth/login?ticket=one-time" }),
          };
        }
        if (url.includes("/auth/cli-poll")) {
          return { status: 200, json: async () => ({ token: "session-token" }) };
        }
        return { status: 200, json: async () => ({ id: "user" }) };
      },
    };
    const win = {
      isDestroyed: () => false,
      webContents: { getURL: () => "https://idp.example/passkey" },
    };
    const state = { serverUrl: "https://server.example", pendingServerLoads: 0 };
    const reloads = [];
    const opened = [];
    const showWebAuthnTimeout = runInNewContext(
      `${oidcSessionCode}; ${webAuthnTimeoutCode}; showWebAuthnTimeout`,
      {
        AbortController,
        BrowserWindow: function BrowserWindow() {},
        WEB_SCHEMES: new Set(["https:"]),
        URL,
        dialog: { showMessageBox: async () => ({ response: 0 }) },
        installAndVerifySessionCookie,
        ipcMain: {},
        loadAuthenticatedServerUrl: async (...args) => reloads.push(args),
        OIDC_LOGIN_PAGE: "/oidc_login.html",
        OIDC_LOGIN_PRELOAD: "/oidc_login_preload.js",
        OIDC_LOGIN_TIMEOUT_MS: 100,
        oidcServerUrlError,
        oidcLoginFlows: new WeakMap(),
        probeServerAuth: async () => ({ kind: "oidc", status: 401 }),
        runOidcBrowserLogin: (...args) => {
          const options = args[3];
          return runOidcBrowserLogin(...args.slice(0, 3), { ...options, pollIntervalMs: 1 });
        },
        runOidcLoginDialog: async ({ runAttempt }) => {
          const result = await runAttempt({
            signal: new AbortController().signal,
            updateMessage() {},
          });
          return result.ok;
        },
        session: { defaultSession: electronSession },
        shell: { openExternal: async (url) => opened.push(url) },
        windows: new Map([[win, state]]),
        withServerLoad,
      },
    );

    await showWebAuthnTimeout(win);

    assert.equal(opened.length, 1);
    assert.equal(stored.value, "session-token");
    assert.equal(fetches.at(-1), "https://server.example/v1/me");
    assert.deepEqual(reloads, [[win, "https://server.example"]]);
    assert.equal(state.pendingServerLoads, 0);
  });

  it("does not attempt ticket login for an accounts-mode passkey page", async () => {
    const win = {
      isDestroyed: () => false,
      webContents: { getURL: () => "https://server.example/login" },
    };
    const state = { serverUrl: "https://server.example", pendingServerLoads: 0 };
    let handoffs = 0;
    const opened = [];
    const showWebAuthnTimeout = runInNewContext(`${webAuthnTimeoutCode}; showWebAuthnTimeout`, {
      WEB_SCHEMES: new Set(["https:"]),
      URL,
      dialog: { showMessageBox: async () => ({ response: 0 }) },
      oidcServerUrlError,
      probeServerAuth: async () => ({ kind: "accounts", status: 401 }),
      runWindowOidcBrowserHandoff: async () => {
        handoffs += 1;
      },
      session: { defaultSession: {} },
      shell: { openExternal: async (url) => opened.push(url) },
      windows: new Map([[win, state]]),
      withServerLoad,
    });

    await showWebAuthnTimeout(win);

    assert.equal(handoffs, 0);
    // "Open in Browser" must still do what it says on an accounts-mode
    // /login page: no ticket handoff exists, so the stuck sign-in page
    // itself goes to the system browser (where a platform passkey works).
    assert.deepEqual(opened, ["https://server.example/login"]);
    assert.equal(state.pendingServerLoads, 0);
  });

  it("keeps 'other' auth postures inert (no handoff, no browser open)", async () => {
    const win = {
      isDestroyed: () => false,
      webContents: { getURL: () => "https://server.example/somewhere" },
    };
    const state = { serverUrl: "https://server.example", pendingServerLoads: 0 };
    const opened = [];
    const showWebAuthnTimeout = runInNewContext(`${webAuthnTimeoutCode}; showWebAuthnTimeout`, {
      WEB_SCHEMES: new Set(["https:"]),
      URL,
      dialog: { showMessageBox: async () => ({ response: 0 }) },
      oidcServerUrlError,
      probeServerAuth: async () => ({ kind: "other", status: 500 }),
      runWindowOidcBrowserHandoff: async () => assert.fail("constructed the OIDC modal"),
      session: { defaultSession: {} },
      shell: { openExternal: async (url) => opened.push(url) },
      windows: new Map([[win, state]]),
      withServerLoad,
    });

    await showWebAuthnTimeout(win);

    assert.deepEqual(opened, []);
    assert.equal(state.pendingServerLoads, 0);
  });

  it("rejects an unsafe WebAuthn handoff before the authenticated probe", async () => {
    const win = {
      isDestroyed: () => false,
      webContents: { getURL: () => "https://idp.example/passkey" },
    };
    const state = { serverUrl: "http://server.example", pendingServerLoads: 0 };
    const setupLoads = [];
    const showWebAuthnTimeout = runInNewContext(`${webAuthnTimeoutCode}; showWebAuthnTimeout`, {
      WEB_SCHEMES: new Set(["https:"]),
      URL,
      configuredServerUrlErrorMessage: () => "Remote servers require HTTPS.",
      dialog: { showMessageBox: async () => assert.fail("constructed a confirmation modal") },
      loadSetupPage: async (...args) => setupLoads.push(args),
      oidcServerUrlError,
      probeServerAuth: async () => assert.fail("sent an authenticated probe"),
      runWindowOidcBrowserHandoff: async () => assert.fail("constructed the OIDC modal"),
      session: { defaultSession: {} },
      windows: new Map([[win, state]]),
      withServerLoad,
    });

    await showWebAuthnTimeout(win);

    assert.equal(setupLoads.length, 1);
    assert.equal(setupLoads[0][0], win);
    assert.equal(setupLoads[0][1].error, "Remote servers require HTTPS.");
    assert.equal(setupLoads[0][1].url, "http://server.example");
    assert.equal(state.pendingServerLoads, 0);
  });

  it("rejects ambiguous WebAuthn server URLs before the authenticated probe", async () => {
    const invalidServerUrls = [
      "https://server.example/base?workspace=one",
      "https://server.example/base?",
      "https://server.example/base#workspace",
      "https://server.example/base#",
      "https://server.example/base/../other",
      "https://server.example/base/%2e%2e/other",
      "https://server.example/base/%252e%252e/other",
      "https://server.example/base/%2525252e%2525252e/other",
      "https://server.example/base\\other",
      "https://server.example/base//other",
      "https://server.example/base/%2fother",
      "https://server.example/base/%252fother",
      "https://server.example/base/%2525252fother",
      `https://server.example/base/%${"25".repeat(40)}2fother`,
      "https://server.example/base/%",
      "https://server.example/base/%C0%AF",
    ];

    await Promise.all(
      invalidServerUrls.map(async (serverUrl) => {
        const win = {
          isDestroyed: () => false,
          webContents: { getURL: () => "https://idp.example/passkey" },
        };
        const state = { serverUrl, pendingServerLoads: 0 };
        const setupLoads = [];
        const showWebAuthnTimeout = runInNewContext(`${webAuthnTimeoutCode}; showWebAuthnTimeout`, {
          WEB_SCHEMES: new Set(["https:"]),
          URL,
          configuredServerUrlErrorMessage: () => "Invalid server URL.",
          dialog: { showMessageBox: async () => assert.fail("constructed a confirmation modal") },
          loadSetupPage: async (...args) => setupLoads.push(args),
          oidcServerUrlError,
          probeServerAuth: async () => assert.fail("sent an authenticated probe"),
          runWindowOidcBrowserHandoff: async () => assert.fail("constructed the OIDC modal"),
          session: { defaultSession: {} },
          windows: new Map([[win, state]]),
          withServerLoad,
        });

        await showWebAuthnTimeout(win);

        assert.equal(setupLoads.length, 1);
        assert.equal(setupLoads[0][0], win);
        assert.equal(setupLoads[0][1].error, "Invalid server URL.");
        assert.equal(setupLoads[0][1].url, serverUrl);
        assert.equal(state.pendingServerLoads, 0);
      }),
    );
  });

  it("cancels the real ticket composition without installing or reloading", async () => {
    const controller = new AbortController();
    const electronSession = {
      cookies: {
        set: async () => assert.fail("cancelled login installed a cookie"),
        get: async () => [],
      },
      fetch: async () => ({
        status: 200,
        json: async () => ({ ticket: "one-time", login_url: "/auth/login?ticket=one-time" }),
      }),
    };
    const win = {
      isDestroyed: () => false,
      webContents: { getURL: () => "https://idp.example/passkey" },
    };
    const state = { serverUrl: "https://server.example", pendingServerLoads: 0 };
    let reloads = 0;
    const showWebAuthnTimeout = runInNewContext(
      `${oidcSessionCode}; ${webAuthnTimeoutCode}; showWebAuthnTimeout`,
      {
        AbortController,
        BrowserWindow: function BrowserWindow() {},
        WEB_SCHEMES: new Set(["https:"]),
        URL,
        dialog: { showMessageBox: async () => ({ response: 0 }) },
        installAndVerifySessionCookie,
        ipcMain: {},
        loadAuthenticatedServerUrl: async () => {
          reloads += 1;
        },
        OIDC_LOGIN_PAGE: "/oidc_login.html",
        OIDC_LOGIN_PRELOAD: "/oidc_login_preload.js",
        OIDC_LOGIN_TIMEOUT_MS: 100,
        oidcServerUrlError,
        oidcLoginFlows: new WeakMap(),
        probeServerAuth: async () => ({ kind: "oidc", status: 401 }),
        runOidcBrowserLogin,
        runOidcLoginDialog: async ({ runAttempt }) => {
          const result = await runAttempt({ signal: controller.signal, updateMessage() {} });
          return result.ok;
        },
        session: { defaultSession: electronSession },
        shell: { openExternal: async () => controller.abort() },
        windows: new Map([[win, state]]),
        withServerLoad,
      },
    );

    await showWebAuthnTimeout(win);

    assert.equal(reloads, 0);
    assert.equal(state.pendingServerLoads, 0);
  });

  it("rejects remote HTTP browser login without network, shell, or cookie side effects", async () => {
    const runWindowOidcBrowserHandoff = runInNewContext(
      `${oidcSessionCode}; runWindowOidcBrowserHandoff`,
      {
        AbortController,
        BrowserWindow: function BrowserWindow() {},
        installAndVerifySessionCookie: async () => assert.fail("installed a plaintext cookie"),
        ipcMain: {},
        OIDC_LOGIN_PAGE: "/oidc_login.html",
        OIDC_LOGIN_PRELOAD: "/oidc_login_preload.js",
        OIDC_LOGIN_TIMEOUT_MS: 100,
        oidcLoginFlows: new WeakMap(),
        runOidcBrowserLogin,
        runOidcLoginDialog: async ({ runAttempt }) =>
          runAttempt({ signal: new AbortController().signal, updateMessage() {} }),
        session: {
          defaultSession: { fetch: async () => assert.fail("made a plaintext request") },
        },
        shell: { openExternal: async () => assert.fail("opened a plaintext ticket URL") },
        URL,
      },
    );

    const result = await runWindowOidcBrowserHandoff({}, "http://server.example");

    assert.equal(result.ok, false);
    assert.equal(
      result.error,
      "Browser sign-in requires HTTPS for remote servers. Update the server URL and retry.",
    );
  });

  it("reports a malformed OIDC server URL without authentication side effects", async () => {
    const runWindowOidcBrowserHandoff = runInNewContext(
      `${oidcSessionCode}; runWindowOidcBrowserHandoff`,
      {
        AbortController,
        BrowserWindow: function BrowserWindow() {},
        installAndVerifySessionCookie: async () => assert.fail("installed a cookie"),
        ipcMain: {},
        OIDC_LOGIN_PAGE: "/oidc_login.html",
        OIDC_LOGIN_PRELOAD: "/oidc_login_preload.js",
        OIDC_LOGIN_TIMEOUT_MS: 100,
        oidcLoginFlows: new WeakMap(),
        runOidcBrowserLogin,
        runOidcLoginDialog: async ({ runAttempt }) =>
          runAttempt({ signal: new AbortController().signal, updateMessage() {} }),
        session: { defaultSession: { fetch: async () => assert.fail("made a request") } },
        shell: { openExternal: async () => assert.fail("opened a URL") },
        URL,
      },
    );

    const result = await runWindowOidcBrowserHandoff({}, "not a url");

    assert.equal(result.ok, false);
    assert.equal(
      result.error,
      "The server address is invalid. Return to setup, correct it, and retry.",
    );
  });

  it("rolls back when runAttempt is cancelled after cookie verification", async () => {
    const controller = new AbortController();
    let stored = null;
    const electronSession = {
      cookies: {
        get: async () => (stored ? [{ ...stored }] : []),
        set: async (details) => {
          stored = { ...details };
        },
        remove: async () => {
          stored = null;
        },
      },
      fetch: async () => {
        queueMicrotask(() => controller.abort());
        return { status: 200, json: async () => ({ id: "user" }) };
      },
    };
    let attemptResult;
    const runWindowOidcBrowserHandoff = runInNewContext(
      `${oidcSessionCode}; runWindowOidcBrowserHandoff`,
      {
        AbortController,
        BrowserWindow: function BrowserWindow() {},
        installAndVerifySessionCookie,
        ipcMain: {},
        OIDC_LOGIN_PAGE: "/oidc_login.html",
        OIDC_LOGIN_PRELOAD: "/oidc_login_preload.js",
        OIDC_LOGIN_TIMEOUT_MS: 100,
        oidcLoginFlows: new WeakMap(),
        runOidcBrowserLogin: async () => ({ ok: true, token: "session-token" }),
        runOidcLoginDialog: async ({ runAttempt }) => {
          attemptResult = await runAttempt({ signal: controller.signal, updateMessage() {} });
          return attemptResult.ok;
        },
        session: { defaultSession: electronSession },
        shell: { openExternal: async () => {} },
        URL,
      },
    );

    const authenticated = await runWindowOidcBrowserHandoff({}, "https://server.example");

    assert.equal(authenticated, false);
    assert.equal(attemptResult.ok, false);
    assert.equal(stored, null);
  });

  it("routes the saved cold-launch destination through loadServerUrl", () => {
    assert.ok(createWindowCode);
    assert.match(
      createWindowCode,
      /if \(destination\)[\s\S]{0,200}loadInitialDestination\([\s\S]{0,300}loadServerUrl\(win, serverUrl, undefined, destination/,
    );
    assert.doesNotMatch(
      createWindowCode,
      /if \(destination\)[\s\S]{0,300}win\.loadURL\(destination\)/,
    );
  });

  it("commits Setup Connect settings only inside beforeLoad", () => {
    assert.ok(setupServerCode);
    assert.match(
      setupServerCode,
      /loadServerUrl\(win, target,[\s\S]{0,300}beforeLoad:\s*\(\)\s*=>\s*\{[\s\S]{0,300}settings\.server_url = target/,
    );
    const beforeTransaction = setupServerCode.slice(0, setupServerCode.indexOf("loadServerUrl"));
    assert.doesNotMatch(beforeTransaction, /settings\.server_url = target/);
  });

  it("rejects invalid Setup URLs before workspace expansion or authentication", async () => {
    const state = { serverUrl: null };
    const win = {};
    const handlerSource = setupServerCode.slice(
      setupServerCode.indexOf("async"),
      setupServerCode.lastIndexOf(");"),
    );
    const handler = runInNewContext(`(${handlerSource})`, {
      BrowserWindow: { fromWebContents: () => win },
      configuredServerUrlErrorMessage: (reason) =>
        reason === "insecure_transport" ? "Remote servers require HTTPS." : "Invalid server.",
      configuredServerUrlError: (raw, normalized) => {
        const trimmed = String(raw).trim();
        return oidcServerUrlError(
          trimmed.includes("://") ? trimmed : `${new URL(normalized).protocol}//${trimmed}`,
        );
      },
      expandDatabricksWorkspaceUrl: async () => assert.fail("expanded an invalid URL"),
      isSetupPageSender: () => true,
      managedServerUrls: () => [],
      normalizeUrl,
      oidcServerUrlError,
      windows: new Map([[win, state]]),
      withServerLoad,
    });

    await Promise.all(
      [
        "http://server.example",
        "https://user:secret@server.example",
        "https://server.example/base?workspace=one",
        "https://server.example/base/../other",
        "server.example/base/../other",
        "ftp://server.example",
        "not a url",
      ].map(async (url) => {
        const result = await handler({ sender: {} }, url);
        assert.equal(result.loaded, false);
        assert.match(result.error, /server|HTTPS/i);
      }),
    );
    assert.equal(state.pendingServerLoads, undefined);
  });

  it("rejects unsafe Setup expansion output before load or persistence", async () => {
    await Promise.all(
      [
        "https://different.example/ml/omnigents",
        "https://workspace.example/ml/omnigents?workspace=one",
        "https://workspace.example/ml/omnigents#fragment",
        "https://user:secret@workspace.example/ml/omnigents",
        "http://workspace.example/ml/omnigents",
        "https://workspace.example/ml//omnigents",
        "https://workspace.example/ml/%252fomnigents",
      ].map(async (expandedTarget) => {
        const state = { serverUrl: null };
        const win = {};
        const handlerSource = setupServerCode.slice(
          setupServerCode.indexOf("async"),
          setupServerCode.lastIndexOf(");"),
        );
        const handler = runInNewContext(`(${handlerSource})`, {
          BrowserWindow: { fromWebContents: () => win },
          configuredServerUrlError: () => null,
          configuredServerUrlErrorMessage: () => "The server address is invalid.",
          expandDatabricksWorkspaceUrl: async () => expandedTarget,
          expandedServerUrlError: (serverUrl, expectedIdentity) =>
            oidcServerUrlError(serverUrl) ??
            (workspaceIdentityKey(serverUrl) === expectedIdentity ? null : "invalid_server_url"),
          isSetupPageSender: () => true,
          loadServerUrl: async () => assert.fail("loaded an unsafe expanded URL"),
          managedServerUrls: () => [],
          normalizeUrl: () => "https://workspace.example",
          workspaceIdentityKey,
          windows: new Map([[win, state]]),
          withServerLoad,
        });

        const result = await handler({ sender: {} }, "https://workspace.example");

        assert.equal(result.loaded, false);
        assert.equal(result.error, "The server address is invalid.");
        assert.equal(state.pendingServerLoads, 0);
      }),
    );
  });

  it("keeps valid Setup URL expansion before loading", async () => {
    const state = { serverUrl: null };
    const win = {};
    const calls = [];
    const handlerSource = setupServerCode.slice(
      setupServerCode.indexOf("async"),
      setupServerCode.lastIndexOf(");"),
    );
    const handler = runInNewContext(`(${handlerSource})`, {
      BrowserWindow: { fromWebContents: () => win },
      expandDatabricksWorkspaceUrl: async (url) => {
        calls.push(["expand", url]);
        return `${url}/ml/omnigents`;
      },
      expandedServerUrlError: (serverUrl, expectedIdentity) =>
        oidcServerUrlError(serverUrl) ??
        (workspaceIdentityKey(serverUrl) === expectedIdentity ? null : "invalid_server_url"),
      fetchServerManifest: async () => ({}),
      configuredServerUrlError: (_raw, normalized) => oidcServerUrlError(normalized),
      isSetupPageSender: () => true,
      loadServerUrl: async (_win, url) => {
        calls.push(["load", url]);
        return true;
      },
      loadSettings: () => ({}),
      managedServerUrls: () => [],
      normalizeUrl: (url) => url,
      oidcServerUrlError,
      serverSelectorV2Enabled: () => false,
      workspaceIdentityKey,
      rememberRecentServer() {},
      saveSettings() {},
      windows: new Map([[win, { ...state, ephemeral: true }]]),
      withServerLoad,
    });

    const result = await handler({ sender: {} }, "https://ws.cloud.databricks.com");
    assert.equal(result.loaded, true);
    assert.deepEqual(calls, [
      ["expand", "https://ws.cloud.databricks.com"],
      ["load", "https://ws.cloud.databricks.com/ml/omnigents"],
    ]);
  });

  it("blocks deep-link reuse during Setup workspace expansion", async () => {
    const state = { serverUrl: null };
    const win = {};
    const expansion = Promise.withResolvers();
    const handlerSource = setupServerCode.slice(
      setupServerCode.indexOf("async"),
      setupServerCode.lastIndexOf(");"),
    );
    const handler = runInNewContext(`(${handlerSource})`, {
      BrowserWindow: { fromWebContents: () => win },
      configuredServerUrlError: (_raw, normalized) => oidcServerUrlError(normalized),
      expandDatabricksWorkspaceUrl: () => expansion.promise,
      isSetupPageSender: () => true,
      managedServerUrls: () => [],
      normalizeUrl: (url) => url,
      oidcServerUrlError,
      workspaceIdentityKey,
      windows: new Map([[win, state]]),
      withServerLoad,
    });

    const connecting = handler({ sender: {} }, "https://workspace.example");
    assert.equal(isSetupIdle(state), false);
    expansion.reject(new Error("stop after expansion"));
    await assert.rejects(connecting, /stop after expansion/);
    assert.equal(isSetupIdle(state), true);
  });

  it("ignores Setup Connect while another load is pending", async () => {
    const state = { serverUrl: null, pendingServerLoads: 1 };
    const win = {};
    let expansionCalled = false;
    const handlerSource = setupServerCode.slice(
      setupServerCode.indexOf("async"),
      setupServerCode.lastIndexOf(");"),
    );
    const handler = runInNewContext(`(${handlerSource})`, {
      BrowserWindow: { fromWebContents: () => win },
      configuredServerUrlError: (_raw, normalized) => oidcServerUrlError(normalized),
      expandDatabricksWorkspaceUrl: async () => {
        expansionCalled = true;
        throw new Error("unexpected expansion");
      },
      isSetupPageSender: () => true,
      managedServerUrls: () => [],
      normalizeUrl: (url) => url,
      oidcServerUrlError,
      workspaceIdentityKey,
      windows: new Map([[win, state]]),
      withServerLoad,
    });

    const result = await handler({ sender: {} }, "https://workspace.example");
    assert.equal(result.loaded, false);
    assert.equal(expansionCalled, false);
    assert.equal(state.pendingServerLoads, 1);
  });

  it("commits switched-server settings and manifest only inside beforeLoad", () => {
    assert.ok(switchServerCode);
    assert.match(
      switchServerCode,
      /loadServerUrl\(win, url,[\s\S]{0,300}beforeLoad:\s*\(\)\s*=>\s*\{[\s\S]{0,300}settings\.server_url = url[\s\S]{0,300}setWindowServerManifest\(win, PRE_MANIFEST_BASELINE\)/,
    );
    const beforeTransaction = switchServerCode.slice(0, switchServerCode.indexOf("loadServerUrl"));
    assert.doesNotMatch(beforeTransaction, /settings\.server_url = url|setWindowServerManifest/);
  });

  it("leaves the existing server state untouched when authentication fails", async () => {
    assert.ok(serverLoadCode);
    const previousServerUrl = "https://previous.example";
    const previousManifest = { manifestVersion: 1 };
    const state = {
      origin: new URL(previousServerUrl).origin,
      identity: workspaceIdentityKey(previousServerUrl),
      serverUrl: previousServerUrl,
      serverManifest: previousManifest,
      pendingServerLoads: 0,
    };
    const settings = { server_url: previousServerUrl };
    const win = { isDestroyed: () => false };
    const loadServerUrl = runInNewContext(`${serverLoadCode}; loadServerUrl`, {
      ensureWindowOidcSession: async () => false,
      joinServerUrl,
      loadServerAfterAuth,
      pinWindow: () => assert.fail("changed the existing pin"),
      setWindowServerUrl: () => assert.fail("changed the existing server URL"),
      windows: new Map([[win, state]]),
      withServerLoad,
    });

    const loaded = await loadServerUrl(win, "https://next.example", undefined, undefined, {
      beforeLoad: () => {
        settings.server_url = "https://next.example";
        state.serverManifest = { manifestVersion: 2 };
      },
    });

    assert.equal(loaded, false);
    assert.equal(state.origin, new URL(previousServerUrl).origin);
    assert.equal(state.identity, workspaceIdentityKey(previousServerUrl));
    assert.equal(state.serverUrl, previousServerUrl);
    assert.equal(settings.server_url, previousServerUrl);
    assert.equal(state.serverManifest, previousManifest);
    assert.equal(state.pendingServerLoads, 0);
  });

  it("ignores switch-server while another window load is pending", async () => {
    const target = "https://other.example";
    const state = { ephemeral: false, pendingServerLoads: 1 };
    const win = {};
    let loadCalls = 0;
    const handlerSource = switchServerCode.slice(
      switchServerCode.indexOf("async"),
      switchServerCode.lastIndexOf(");"),
    );
    const handler = runInNewContext(`(${handlerSource})`, {
      BrowserWindow: { fromWebContents: () => win },
      isPinnedWorkspaceSender: () => true,
      loadServerUrl: async () => {
        loadCalls += 1;
        return true;
      },
      loadSettings: () => ({ recent_servers: [target] }),
      managedServerUrls: () => [],
      rememberRecentServer: () => {},
      saveSettings: () => {},
      windows: new Map([[win, state]]),
    });

    await handler({ sender: {} }, target);
    assert.equal(loadCalls, 0);
    state.pendingServerLoads = 0;
    await handler({ sender: {} }, target);
    assert.equal(loadCalls, 1);
  });

  it("ignores open-server-setup while another window load is pending", () => {
    const state = { ephemeral: false, pendingServerLoads: 1 };
    const setupLoads = [];
    const win = {};
    const handlerSource = openServerSetupCode.slice(
      openServerSetupCode.indexOf("(event) =>"),
      openServerSetupCode.lastIndexOf(");"),
    );
    const handler = runInNewContext(`(${handlerSource})`, {
      BrowserWindow: { fromWebContents: () => win },
      isPinnedWorkspaceSender: () => true,
      loadSetupPage: (target) => {
        setupLoads.push(target);
        return Promise.resolve();
      },
      windows: new Map([[win, state]]),
    });

    handler({ sender: {} });
    assert.equal(setupLoads.length, 0);
    state.pendingServerLoads = 0;
    handler({ sender: {} });
    assert.deepEqual(setupLoads, [win]);
  });

  it("defers expiry recovery until the pending load settles", async () => {
    assert.ok(expiryHandoffCallbackCode);
    const win = { isDestroyed: () => false };
    const pendingLoad = Promise.withResolvers();
    const events = [];
    const state = {
      pendingServerLoads: 1,
      serverUrl: "https://server.example",
    };
    // Mirror withServerLoad's finally: a settled load releases the claim.
    state.pendingLoad = pendingLoad.promise.then(() => {
      events.push("pending settled");
      state.pendingServerLoads = 0;
      state.pendingLoad = null;
    });
    const setupLoads = [];
    const callback = runInNewContext(`(${expiryHandoffCallbackCode})`, {
      loadServerUrl: async (...args) => {
        events.push("recovery");
        assert.deepEqual(args.slice(0, 4), [
          win,
          "https://server.example",
          undefined,
          "https://server.example/c/current",
        ]);
        // Recovery follows the renderer's own /auth/login navigation — an
        // explicit unauthenticated intent, so the cached-CLI-token fast path
        // must be bypassed. (Options come from the vm realm, so compare the
        // field, not the object.)
        assert.equal(args[4]?.forceInteractiveAuth, true);
        return false;
      },
      loadSetupPage: async (...args) => setupLoads.push(args),
      win,
      windows: new Map([[win, state]]),
    });

    const recovery = callback({
      serverUrl: "https://server.example",
      returnUrl: "https://server.example/c/current",
    });
    pendingLoad.resolve();
    await recovery;

    assert.deepEqual(events, ["pending settled", "recovery"]);
    assert.equal(setupLoads.length, 1);
    assert.equal(setupLoads[0][0], win);
    assert.equal(setupLoads[0][1].error, "Your session expired and sign-in did not complete.");
    assert.equal(setupLoads[0][1].url, "https://server.example");
  });

  it("re-checks the pinned server after a pending load settles", async () => {
    assert.ok(expiryHandoffCallbackCode);
    const expiredServerUrl = "https://server-a.example";
    const returnUrl = `${expiredServerUrl}/c/current`;

    const runInterleaving = async ({ pinAfterPending, rejectPending = false }) => {
      const win = { isDestroyed: () => false };
      const pendingLoad = Promise.withResolvers();
      const state = {
        pendingServerLoads: 1,
        serverUrl: expiredServerUrl,
      };
      // Mirror withServerLoad's finally: a settled load releases the claim.
      state.pendingLoad = pendingLoad.promise.finally(() => {
        state.pendingServerLoads = 0;
        state.pendingLoad = null;
      });
      const recoveryLoads = [];
      const setupLoads = [];
      const pinCalls = [];
      const serverUrlCalls = [];
      const callback = runInNewContext(`(${expiryHandoffCallbackCode})`, {
        loadServerUrl: async (...args) => {
          recoveryLoads.push(args);
          return true;
        },
        loadSetupPage: async (...args) => {
          setupLoads.push(args);
          pinCalls.push([args[0], null]);
          serverUrlCalls.push([args[0], null]);
        },
        win,
        windows: new Map([[win, state]]),
      });

      const recovery = callback({ serverUrl: expiredServerUrl, returnUrl });
      if (pinAfterPending) state.serverUrl = pinAfterPending;
      if (rejectPending) pendingLoad.reject(new Error("pending load failed"));
      else pendingLoad.resolve();
      await recovery;
      return { recoveryLoads, setupLoads, pinCalls, serverUrlCalls, win };
    };

    const switched = await runInterleaving({
      pinAfterPending: "https://server-b.example",
    });
    assert.deepEqual(switched.recoveryLoads, []);
    assert.deepEqual(switched.setupLoads, []);
    assert.deepEqual(switched.pinCalls, []);
    assert.deepEqual(switched.serverUrlCalls, []);

    const unchanged = await runInterleaving({ rejectPending: true });
    assert.equal(unchanged.recoveryLoads.length, 1);
    assert.deepEqual(unchanged.recoveryLoads[0].slice(0, 4), [
      unchanged.win,
      expiredServerUrl,
      undefined,
      returnUrl,
    ]);
    // Options come from the vm realm, so compare the field, not the object.
    assert.equal(unchanged.recoveryLoads[0][4]?.forceInteractiveAuth, true);
    assert.deepEqual(unchanged.setupLoads, []);
    assert.deepEqual(unchanged.pinCalls, []);
    assert.deepEqual(unchanged.serverUrlCalls, []);
  });

  it("leaves the window to a load that starts in the settle gap", async () => {
    // The awaited pending load settles and a user-initiated load (switch
    // server / deep link, possibly to the SAME server URL) claims the window
    // in the gap before recovery runs. Recovery must do nothing — running
    // loadServerUrl would return false (withServerLoad already claimed) and
    // the old behavior then flashed the expired-session setup page over a
    // healthy in-flight load.
    assert.ok(expiryHandoffCallbackCode);
    const win = { isDestroyed: () => false };
    const pendingLoad = Promise.withResolvers();
    const state = {
      pendingServerLoads: 1,
      serverUrl: "https://server.example",
    };
    state.pendingLoad = pendingLoad.promise.then(() => {
      // Settle releases the claim — and a new load immediately takes it.
      state.pendingServerLoads = 1;
      state.pendingLoad = null;
    });
    const recoveryLoads = [];
    const setupLoads = [];
    const callback = runInNewContext(`(${expiryHandoffCallbackCode})`, {
      loadServerUrl: async (...args) => {
        recoveryLoads.push(args);
        return false; // withServerLoad refuses: another load owns the window
      },
      loadSetupPage: async (...args) => setupLoads.push(args),
      win,
      windows: new Map([[win, state]]),
    });

    const recovery = callback({
      serverUrl: "https://server.example",
      returnUrl: "https://server.example/c/current",
    });
    pendingLoad.resolve();
    await recovery;

    assert.deepEqual(recoveryLoads, []);
    assert.deepEqual(setupLoads, []);
  });

  it("wiring-only: limits the fail-loud WebAuthn guard to main-window fallback authentication", () => {
    assert.ok(oauthPopupCode);
    assert.doesNotMatch(oauthPopupCode, /registerWebAuthnTimeout|showWebAuthnTimeout/);
    assert.match(
      oidcSessionCode,
      /setWindowAuthenticationNavigation\(win, true\)[\s\S]{0,220}setWindowAuthenticationNavigation\(win, probe\.kind === "other"\)/,
    );
    assert.match(
      liveCode,
      /registerWebAuthnTimeout\(win\.webContents,[\s\S]{0,500}isWebAuthnEscapePage\([\s\S]{0,160}authenticationNavigation === true/,
    );
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
  it("routes an organization-selected cold launch through the authenticated loader", async () => {
    const serverUrl = "https://dbc-a.cloud.databricks.com/?o=123456789";
    const destinations = [];
    let setupLoads = 0;

    assert.equal(oidcServerUrlError(serverUrl), null);
    const loaded = await loadInitialDestination({
      loadServer: async () => {
        destinations.push(serverUrl);
        return true;
      },
      loadSetup: async () => {
        setupLoads += 1;
      },
    });

    assert.equal(loaded, true);
    assert.deepEqual(destinations, [serverUrl]);
    assert.equal(setupLoads, 0);
    assert.match(
      createWindowCode,
      /loadInitialDestination\([\s\S]{0,300}loadServerUrl\(win, serverUrl, undefined, destination/,
    );
  });

  it("backfills a saved server only after its cold load succeeds", () => {
    assert.match(
      liveCode,
      /loadInitialDestination\(\{[\s\S]{0,800}\.then\(\(loaded\)\s*=>\s*\{[\s\S]{0,200}if\s*\(loaded\s*&&\s*!ephemeral\s*&&\s*!explicit\s*&&\s*serverUrl\)[\s\S]{0,200}rememberRecentServer\(settings,\s*serverUrl\)/,
      [
        "createWindow no longer backfills a successfully loaded saved server into",
        "recent_servers. Existing installs can have server_url without recent_servers,",
        "so the setup page would show no recents after leaving that server. Keep the",
        "backfill in loadInitialDestination(...).then((loaded) => ...), gated on a",
        "successful load and away from ephemeral windows and explicit target URLs",
        "(which may include a conversation path).",
      ].join(" "),
    );
    assert.match(
      liveCode,
      /if\s*\(loaded\s*&&\s*!ephemeral\s*&&\s*!explicit\s*&&\s*serverUrl\)\s*\{\s*try\s*\{[\s\S]{0,200}rememberRecentServer[\s\S]{0,120}catch/,
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
// lives under the server mount, while an organization selector stays in the
// query. The shared joinServerUrl helper updates URL fields without dropping either.
describe("deep-link path join wiring (src/main.js)", () => {
  it("joins opts.path onto opts.serverUrl via joinServerUrl as live code", () => {
    assert.match(
      liveCode,
      /joinServerUrl\(serverUrl, opts\.path\)/,
      [
        "createWindow no longer joins opts.path onto opts.serverUrl via",
        "joinServerUrl. A deep link to a workspace server would lose its",
        "mount or organization selector. Restore the URL-aware mount join.",
      ].join(" "),
    );
  });

  it("keeps the organization selector after a mounted deep-link path", () => {
    assert.equal(
      joinServerUrl("https://dbc-a.cloud.databricks.com/omnigent?o=123456789", "/c/conv_abc"),
      "https://dbc-a.cloud.databricks.com/omnigent/c/conv_abc?o=123456789",
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
      /confirmOpenDeepLink\(parent, parsed\.origin, workspaceCandidates\)[\s\S]{0,700}expandDatabricksWorkspaceUrl\(targetIdentity\)/,
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

  it("registers consent-unknown expansion as a pending load", async () => {
    const result = await runConsentUnknownDeepLink(0);
    assert.deepEqual(result.pendingDuringExpansion, [1]);
    assert.equal(result.state.pendingServerLoads, 0);
  });

  it("rejects unsafe expanded server URLs before load or persistence", async () => {
    await Promise.all(
      [
        "https://user:secret@unknown.example/ml/omnigents",
        "https://unknown.example/ml/omnigents?workspace=one",
        "https://unknown.example/ml/omnigents#fragment",
        "http://unknown.example/ml/omnigents",
        "https://different.example/ml/omnigents",
        "https://unknown.example/ml//omnigents",
        "https://unknown.example/ml/%252fomnigents",
      ].map(async (expandedServerUrl) => {
        const result = await runConsentUnknownDeepLink(0, { expandedServerUrl });
        assert.deepEqual(result.parentLoads, []);
        assert.deepEqual(result.created, []);
        assert.deepEqual(result.remembered, []);
        assert.equal(result.state.pendingServerLoads, 0);
      }),
    );
  });

  it("loads and remembers a canonical expanded server URL", async () => {
    const result = await runConsentUnknownDeepLink(0, {
      expandedServerUrl: "https://unknown.example/ml/omnigents",
    });

    assert.deepEqual(result.parentLoads, [result.parent]);
    assert.deepEqual(result.remembered, ["https://unknown.example/ml/omnigents"]);
  });

  it("reuses an idle setup parent but not a busy one", async () => {
    const idle = await runConsentUnknownDeepLink(0);
    assert.deepEqual(idle.parentLoads, [idle.parent]);
    assert.equal(idle.created.length, 0);

    const busy = await runConsentUnknownDeepLink(1);
    assert.equal(busy.parentLoads.length, 0);
    assert.equal(busy.created.length, 1);
    assert.equal(busy.state.pendingServerLoads, 1);
  });

  it("opens a replacement when the idle parent closes during expansion", async () => {
    const result = await runConsentUnknownDeepLink(0, { closeDuringExpansion: true });
    assert.equal(result.created.length, 1);
    assert.equal(result.parentLoads.length, 0);
  });

  it("remembers explicit consent even when loading the reused window fails", () => {
    assert.match(
      deepLinkHandlerCode,
      /await loadServerUrl\(parent,[\s\S]{0,100}\.catch\(\(\) => \{\}\);[\s\S]{0,300}rememberServerUrl\(serverUrl\)/,
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

describe("privileged IPC workspace sender gate (src/main.js)", () => {
  it("rejects a same-origin frame that changes or drops o", (t) => {
    const workspaceA = "https://dbc-a.cloud.databricks.com/omnigent?o=workspace-a";
    const harness = loadNavigationHarness({ serverUrl: workspaceA });
    t.after(harness.cleanup);
    const eventFor = (url) => ({
      sender: harness.win.webContents,
      senderFrame: { url },
    });

    assert.equal(
      harness.api.isPinnedWorkspaceSender(
        eventFor("https://dbc-a.cloud.databricks.com/omnigent/c/1?o=workspace-a"),
      ),
      true,
    );
    assert.equal(
      harness.api.isPinnedWorkspaceSender(
        eventFor("https://dbc-a.cloud.databricks.com/omnigent/c/1?o=workspace-b"),
      ),
      false,
    );
    assert.equal(
      harness.api.isPinnedWorkspaceSender(
        eventFor("https://dbc-a.cloud.databricks.com/omnigent/c/1"),
      ),
      false,
    );
  });
});

// pinWindow is the one chokepoint every "leave this server" path routes through
// (Connect to new server, Change Server…, switch-server, did-fail-load fallback).
// Those navigations tear down the renderer WITHOUT running BrowserPane's unmount
// detach, so pinWindow must close the window's browser registry when the
// workspace identity changes — else the native WebContentsView dangles over
// the next workspace or setup page.
describe("browser-view teardown on server change (src/main.js)", () => {
  it("closes embedded views when o changes on the same origin", (t) => {
    const workspaceA = "https://dbc-a.cloud.databricks.com/omnigent?o=workspace-a";
    const workspaceB = "https://dbc-a.cloud.databricks.com/omnigent?o=workspace-b";
    const harness = loadNavigationHarness({ serverUrl: workspaceA });
    t.after(harness.cleanup);

    harness.api.pinWindow(harness.win, workspaceB);

    assert.deepEqual(harness.calls.closeAll, ["server-changed"]);
    assert.equal(harness.api.windows.get(harness.win).identity, workspaceIdentityKey(workspaceB));
  });

  it("guards the teardown so the initial cold-connect pin doesn't fire it", (t) => {
    const serverUrl = "https://dbc-a.cloud.databricks.com/omnigent?o=workspace-a";
    const harness = loadNavigationHarness({ serverUrl });
    t.after(harness.cleanup);
    const state = harness.api.windows.get(harness.win);
    state.origin = null;
    state.identity = null;

    harness.api.pinWindow(harness.win, serverUrl);

    assert.deepEqual(harness.calls.closeAll, []);
  });
});

// Round-1 review fixes: expiry reload matching, deep-link workspace identity,
// cold-start failure fallback, and New Window's explicit target.
describe("expired-session reload identity matching (src/main.js)", () => {
  const { expiredRequestMatchesIdentity } = require("../src/session-expiry");

  it("wires expiredRequestMatchesIdentity into the pinned-workspace predicate", () => {
    assert.match(
      liveCode,
      /function isPinnedWorkspaceUrl\(url\)\s*\{[\s\S]{0,300}expiredRequestMatchesIdentity\(state\.identity,\s*identity\)/,
      [
        "isPinnedWorkspaceUrl no longer matches via expiredRequestMatchesIdentity.",
        "The API request that hits the expired SSO gate usually lacks the ?o=",
        "selector a pinned identity carries, so exact matching means the expiry",
        "reload never fires for ?o=-pinned Databricks windows.",
      ].join(" "),
    );
  });

  it("wires expiredRequestMatchesIdentity into the reload loop", () => {
    assert.match(
      liveCode,
      /registerSessionExpiryReload\([\s\S]{0,900}expiredRequestMatchesIdentity\(state\.identity,\s*identity\)/,
      [
        "The expiry-reload window loop no longer matches via",
        "expiredRequestMatchesIdentity, so ?o=-pinned windows are never reloaded",
        "when their bare-origin API request hits the expired gate.",
      ].join(" "),
    );
  });

  it("reloads a ?o=-pinned window for a bare-origin expired request end to end", () => {
    const pinned = "https://ws.databricks.com?o=123456789";
    assert.equal(
      expiredRequestMatchesIdentity(
        pinned,
        workspaceIdentityKey("https://ws.databricks.com/ajax-api/2.0/omnigents/v1/sessions"),
      ),
      true,
    );
  });
});

describe("deep-link workspace identity (src/main.js)", () => {
  const { parseOmnigentDeepLink } = require("../src/deepLink");

  function runDeepLink(
    raw,
    { knownIdentities = [], knownServerUrl = null, liveIdentities = [] } = {},
  ) {
    const decisions = [];
    const created = [];
    const lookups = [];
    const windows = new Map(
      liveIdentities.map((entry) => {
        const descriptor = typeof entry === "string" ? { identity: entry } : entry;
        return [
          {
            isDestroyed: () => descriptor.destroyed === true,
            webContents: { getURL: () => `${descriptor.identity.split("?")[0]}/` },
          },
          {
            closing: descriptor.closing === true,
            identity: descriptor.identity,
            serverUrl: descriptor.identity,
          },
        ];
      }),
    );
    const handler = runInNewContext(`${deepLinkHandlerCode}; handleDeepLink`, {
      BrowserWindow: { getFocusedWindow: () => null },
      chooseDeepLinkStrategy: (state) => {
        decisions.push(state);
        return { strategy: "open-known" };
      },
      console: { log: () => {} },
      createWindow: (_target, options) => {
        created.push(options);
        return {};
      },
      focusAndRestore: () => {},
      findKnownServerUrl: (identity) => {
        lookups.push(identity);
        return knownServerUrl;
      },
      knownWorkspaceIdentities: () => knownIdentities,
      parseOmnigentDeepLink,
      windows,
      workspaceIdentityKey,
    });
    return handler(raw).then(() => ({ decisions, created, lookups }));
  }

  it("keeps the link's ?o= selector in the target identity", async () => {
    const { decisions, lookups } = await runDeepLink(
      "omnigent://ws.cloud.databricks.com/c/conv_abc?o=123456789",
      {
        knownIdentities: ["https://ws.cloud.databricks.com?o=123456789"],
        knownServerUrl: "https://ws.cloud.databricks.com/omnigent?o=123456789",
      },
    );

    assert.equal(decisions[0].targetOrigin, "https://ws.cloud.databricks.com?o=123456789");
    assert.deepEqual(lookups, ["https://ws.cloud.databricks.com?o=123456789"]);
  });

  it("adopts a known ?o= identity for a selector-less link to the same origin", async () => {
    const { decisions, created } = await runDeepLink(
      "omnigent://ws.cloud.databricks.com/c/conv_abc",
      {
        knownIdentities: ["https://ws.cloud.databricks.com?o=123456789"],
        knownServerUrl: "https://ws.cloud.databricks.com/omnigent?o=123456789",
      },
    );

    assert.equal(decisions[0].targetOrigin, "https://ws.cloud.databricks.com?o=123456789");
    assert.equal(created[0].serverUrl, "https://ws.cloud.databricks.com/omnigent?o=123456789");
  });

  it("keeps a bare identity when it is itself known", async () => {
    const { decisions } = await runDeepLink("omnigent://ws.cloud.databricks.com/c/conv_abc", {
      knownIdentities: [
        "https://ws.cloud.databricks.com",
        "https://ws.cloud.databricks.com?o=123456789",
      ],
      knownServerUrl: "https://ws.cloud.databricks.com",
    });

    assert.equal(decisions[0].targetOrigin, "https://ws.cloud.databricks.com");
  });

  it("ignores non-Databricks query parameters for identity", async () => {
    const { decisions } = await runDeepLink("omnigent://server.example/c/conv_abc?token=evil");

    assert.equal(decisions[0].targetOrigin, "https://server.example");
  });

  it("counts a live ephemeral workspace against silent adoption", async () => {
    // Only ?o=111 is PERSISTED, but an ephemeral window is live on ?o=222 —
    // adopting the persisted identity would silently route the link away
    // from a workspace the user is actively using (and skip its consent
    // ambiguity listing). The identity must stay bare (ambiguous).
    const { decisions } = await runDeepLink("omnigent://ws.cloud.databricks.com/c/conv_abc", {
      knownIdentities: ["https://ws.cloud.databricks.com?o=111"],
      liveIdentities: ["https://ws.cloud.databricks.com?o=222"],
      knownServerUrl: null,
    });

    assert.equal(decisions[0].targetOrigin, "https://ws.cloud.databricks.com");
  });

  it("adopts a live-only workspace identity when it is the only one on the origin", async () => {
    // Nothing persisted, one ephemeral window live on the origin: the link
    // can only mean that workspace.
    const { decisions } = await runDeepLink("omnigent://ws.cloud.databricks.com/c/conv_abc", {
      knownIdentities: [],
      liveIdentities: ["https://ws.cloud.databricks.com?o=222"],
      knownServerUrl: null,
    });

    assert.equal(decisions[0].targetOrigin, "https://ws.cloud.databricks.com?o=222");
  });

  it("excludes destroyed and closing windows from adoption and dispatch", async () => {
    await Promise.all(
      [{ destroyed: true }, { closing: true }].map(async (unavailable) => {
        const { decisions } = await runDeepLink("omnigent://ws.cloud.databricks.com/c/conv_abc", {
          liveIdentities: [
            {
              identity: "https://ws.cloud.databricks.com?o=222",
              ...unavailable,
            },
          ],
        });

        assert.equal(decisions[0].targetOrigin, "https://ws.cloud.databricks.com");
        assert.deepEqual(Array.from(decisions[0].windows), []);
      }),
    );
  });

  it("lists live workspaces among the consent candidates", async () => {
    const confirmCalls = [];
    const parentState = { serverUrl: null, pendingServerLoads: 0 };
    const parent = { isDestroyed: () => false, webContents: { getURL: () => "file:///setup" } };
    const live = {
      isDestroyed: () => false,
      webContents: { getURL: () => "https://ws.cloud.databricks.com/" },
    };
    const handler = runInNewContext(`${deepLinkHandlerCode}; handleDeepLink`, {
      BrowserWindow: { getFocusedWindow: () => null },
      activeWindow: () => parent,
      chooseDeepLinkStrategy: () => ({ strategy: "consent-unknown" }),
      confirmOpenDeepLink: async (...args) => {
        confirmCalls.push(args);
        return false; // cancel — the candidates listing is what's under test
      },
      console: { log: () => {} },
      findKnownServerUrl: () => null,
      knownWorkspaceIdentities: () => ["https://ws.cloud.databricks.com?o=111"],
      parseOmnigentDeepLink,
      windows: new Map([
        [parent, parentState],
        [live, { identity: "https://ws.cloud.databricks.com?o=222", serverUrl: null }],
      ]),
      workspaceIdentityKey,
    });

    await handler("omnigent://ws.cloud.databricks.com/c/conv_abc");

    assert.equal(confirmCalls.length, 1);
    // Array.from: the candidates array is built inside the vm realm, so the
    // strict deepEqual prototype check needs a host-realm copy.
    assert.deepEqual(Array.from(confirmCalls[0][2]), [
      "https://ws.cloud.databricks.com?o=111",
      "https://ws.cloud.databricks.com?o=222",
    ]);
  });

  it("does not guess between several known ?o= workspaces on one origin", async () => {
    // A selector-less link is ambiguous when the origin has several saved
    // workspaces — silently adopting one (e.g. the saved default) could
    // route the conversation into the wrong tenant. The identity stays bare
    // so the link goes through the consent path instead.
    const { decisions } = await runDeepLink("omnigent://ws.cloud.databricks.com/c/conv_abc", {
      knownIdentities: [
        "https://ws.cloud.databricks.com?o=111",
        "https://ws.cloud.databricks.com?o=222",
      ],
      knownServerUrl: null,
    });

    assert.equal(decisions[0].targetOrigin, "https://ws.cloud.databricks.com");
  });

  it("passes the ambiguous workspace candidates to the consent dialog", async () => {
    const confirmCalls = [];
    const state = { serverUrl: null, pendingServerLoads: 0 };
    const parent = { isDestroyed: () => false, webContents: { getURL: () => "file:///setup" } };
    const handler = runInNewContext(`${deepLinkHandlerCode}; handleDeepLink`, {
      BrowserWindow: { getFocusedWindow: () => null },
      activeWindow: () => parent,
      chooseDeepLinkStrategy: () => ({ strategy: "consent-unknown" }),
      confirmOpenDeepLink: async (...args) => {
        confirmCalls.push(args);
        return false; // cancel — the candidates listing is what's under test
      },
      console: { log: () => {} },
      findKnownServerUrl: () => null,
      knownWorkspaceIdentities: () => [
        "https://ws.cloud.databricks.com?o=111",
        "https://ws.cloud.databricks.com?o=222",
      ],
      parseOmnigentDeepLink,
      windows: new Map([[parent, state]]),
      workspaceIdentityKey,
    });

    await handler("omnigent://ws.cloud.databricks.com/c/conv_abc");

    assert.equal(confirmCalls.length, 1);
    // Array.from: the candidates array is built inside the vm realm, so the
    // strict deepEqual prototype check needs a host-realm copy.
    assert.deepEqual(Array.from(confirmCalls[0][2]), [
      "https://ws.cloud.databricks.com?o=111",
      "https://ws.cloud.databricks.com?o=222",
    ]);
  });

  it("lists the candidate workspaces in the consent dialog detail", async () => {
    const confirmCode = liveCode.match(
      /async function confirmOpenDeepLink\(parent, targetOrigin, workspaceCandidates = \[\]\)[\s\S]*?\n\}/,
    )?.[0];
    assert.ok(confirmCode);
    const boxes = [];
    const confirm = runInNewContext(`${confirmCode}; confirmOpenDeepLink`, {
      URL,
      ICON_PNG: "/icon.png",
      dialog: {
        showMessageBox: async (_parent, options) => {
          boxes.push(options);
          return { response: 1 };
        },
      },
      nativeImage: { createFromPath: () => ({ isEmpty: () => true }) },
    });

    const opened = await confirm({}, "https://ws.cloud.databricks.com", [
      "https://ws.cloud.databricks.com?o=111",
      "https://ws.cloud.databricks.com?o=222",
    ]);

    assert.equal(opened, true);
    assert.match(boxes[0].detail, /2 saved workspaces/);
    assert.match(boxes[0].detail, /o=111, o=222/);

    // A single (or no) candidate adds no note — adoption handles that case.
    await confirm({}, "https://ws.cloud.databricks.com", []);
    assert.doesNotMatch(boxes[1].detail, /saved workspaces/);
  });
});

describe("cold-start load failure fallback (src/main.js)", () => {
  it("defers load failures to did-fail-load instead of blanking the setup params", () => {
    assert.ok(createWindowCode);
    assert.doesNotMatch(
      createWindowCode,
      /loadInitialDestination\([\s\S]{0,900}\.catch\(\(\) => loadSetupPage\(win\)\)/,
      [
        "createWindow's cold-start catch loads a BLANK setup page again. did-fail-load",
        "already routes a failed saved-server load to setup WITH ?error=&url= — the",
        "catch must stay a no-op so it cannot supersede that parameterized page.",
      ].join(" "),
    );
    assert.match(
      createWindowCode,
      /loadInitialDestination\([\s\S]{0,1200}\.catch\(\(\) => \{\s*\}\)/,
      "createWindow's cold-start load chain lost its no-op rejection catch.",
    );
  });
});

describe("New Window explicit target (src/main.js)", () => {
  const newWindowCode = liveCode.match(/function newWindow\(\)[\s\S]*?\n\}/)?.[0];

  it("passes only http(s) pages as the clone target", () => {
    assert.ok(newWindowCode);
    const created = [];
    const win = {
      webContents: { getURL: () => "file:///electron/setup/index.html" },
    };
    const newWindow = runInNewContext(`${newWindowCode}; newWindow`, {
      activeWindow: () => win,
      createWindow: (target, opts) => created.push({ target, opts }),
      windows: new Map([[win, { ephemeral: false, serverUrl: null }]]),
      workspaceIdentityKey,
    });

    newWindow();

    // The setup page's file:// URL must not be treated as a server to clone
    // (it would prefill a "server address is invalid" banner downstream).
    assert.equal(created[0].target, undefined);
  });

  it("still clones a real server page URL", () => {
    const created = [];
    const win = {
      webContents: { getURL: () => "https://server.example/c/conv_abc" },
    };
    const newWindow = runInNewContext(`${newWindowCode}; newWindow`, {
      activeWindow: () => win,
      createWindow: (target, opts) => created.push({ target, opts }),
      windows: new Map([[win, { ephemeral: false, serverUrl: "https://server.example" }]]),
      workspaceIdentityKey,
    });

    newWindow();

    assert.equal(created[0].target, "https://server.example/c/conv_abc");
    assert.equal(created[0].opts.serverUrl, "https://server.example");
  });

  it("does not clone a mid-SSO foreign IdP page as the target", () => {
    // While the window is off on its IdP, the current URL is a one-time
    // authorization URL on a foreign origin — the clone must fall back to
    // the window's own server, never load that URL again.
    const created = [];
    const win = {
      webContents: {
        getURL: () => "https://idp.example/authorize?client_id=x&code_challenge=y",
      },
    };
    const newWindow = runInNewContext(`${newWindowCode}; newWindow`, {
      activeWindow: () => win,
      createWindow: (target, opts) => created.push({ target, opts }),
      windows: new Map([[win, { ephemeral: false, serverUrl: "https://server.example" }]]),
      workspaceIdentityKey,
    });

    newWindow();

    assert.equal(created[0].target, undefined);
    assert.equal(created[0].opts.serverUrl, "https://server.example");
  });

  it("does not clone across ?o= workspaces on one Databricks host", () => {
    const created = [];
    const win = {
      webContents: {
        getURL: () => "https://ws.cloud.databricks.com/omnigent/c/x?o=999",
      },
    };
    const newWindow = runInNewContext(`${newWindowCode}; newWindow`, {
      activeWindow: () => win,
      createWindow: (target, opts) => created.push({ target, opts }),
      windows: new Map([
        [win, { ephemeral: false, serverUrl: "https://ws.cloud.databricks.com/omnigent?o=123" }],
      ]),
      workspaceIdentityKey,
    });

    newWindow();

    assert.equal(created[0].target, undefined);
  });
});

describe("expired-session reload attribution (src/main.js)", () => {
  const accessCode = liveCode.match(/function registerSessionExpiryAccess\(\)[\s\S]*?\n\}/)?.[0];
  const pinnedCode = liveCode.match(/function isPinnedWorkspaceUrl\(url\)[\s\S]*?\n\}/)?.[0];
  const {
    expiredRequestMatchesIdentity,
    registerSessionExpiryReload,
  } = require("../src/session-expiry");

  const HOST_URL = "https://ws.databricks.com/ajax-api/2.0/omnigents/v1/sessions";
  const LOGIN_REDIRECT = {
    url: HOST_URL,
    statusCode: 303,
    redirectURL: "https://ws.databricks.com/login.html?next_url=%2Fajax-api",
  };

  function makeWindow(id, identity) {
    let reloads = 0;
    const win = {
      isDestroyed: () => false,
      webContents: {
        id,
        reload: () => {
          reloads += 1;
        },
      },
    };
    return { win, state: { identity }, reloadCount: () => reloads };
  }

  function wire(windowsList, { monotonicNow = () => 1_000_000, wallDate = Date } = {}) {
    assert.ok(accessCode);
    assert.ok(pinnedCode);
    let listener = null;
    const ses = {
      webRequest: {
        onBeforeRedirect: (cb) => {
          listener = cb;
        },
      },
    };
    const windows = new Map(windowsList.map(({ win, state }) => [win, state]));
    const register = runInNewContext(`${pinnedCode}; ${accessCode}; registerSessionExpiryAccess`, {
      Date: wallDate,
      EXPIRY_RELOAD_MIN_INTERVAL_MS: 15_000,
      expiredRequestMatchesIdentity,
      lastExpiryReloadAt: new WeakMap(),
      performance: { now: monotonicNow },
      registerSessionExpiryReload,
      session: { defaultSession: ses },
      windows,
      workspaceIdentityKey,
    });
    register();
    return { emit: (details) => listener(details) };
  }

  it("reloads only the window whose webContents issued the request", () => {
    // Two windows pinned to different ?o= workspaces on ONE host: a
    // bare-origin login redirect matches both identities, so only the
    // issuing webContents may decide which window reloads.
    const a = makeWindow(1, "https://ws.databricks.com?o=111");
    const b = makeWindow(2, "https://ws.databricks.com?o=222");
    const { emit } = wire([a, b]);

    emit({ ...LOGIN_REDIRECT, webContentsId: 1 });

    assert.equal(a.reloadCount(), 1);
    assert.equal(b.reloadCount(), 0);
  });

  it("reloads nothing for an untracked webContents", () => {
    // A login-shaped redirect from contents the shell doesn't own (an
    // embedded browser view, a worker, or a hostile page's synthesized
    // request) must not reload any window.
    const a = makeWindow(1, "https://ws.databricks.com?o=111");
    const b = makeWindow(2, "https://ws.databricks.com?o=222");
    const { emit } = wire([a, b]);

    emit({ ...LOGIN_REDIRECT, webContentsId: 99 });
    emit({ ...LOGIN_REDIRECT }); // no webContentsId at all

    assert.equal(a.reloadCount(), 0);
    assert.equal(b.reloadCount(), 0);
  });

  it("keeps the identity match as a secondary filter", () => {
    // The issuing window still only reloads when the request identity fits
    // its pinned identity (cross-origin requests from a pinned window's
    // page must not reload it).
    const a = makeWindow(1, "https://other.example");
    const { emit } = wire([a]);

    emit({ ...LOGIN_REDIRECT, webContentsId: 1 });

    assert.equal(a.reloadCount(), 0);
  });

  it("throttles on a monotonic clock — a backward wall-clock step cannot suppress reloads", () => {
    const a = makeWindow(1, "https://ws.databricks.com?o=111");
    const monotonic = [1_000, 2_000, 18_000];
    let tick = 0;
    // Wall clock steps BACKWARD across the whole scenario (NTP correction):
    // Date.now()-based throttling would see `now - last` negative forever
    // and suppress every reload until wall time recovers.
    let wall = 5_000_000_000;
    const { emit } = wire([a], {
      monotonicNow: () => monotonic[tick],
      wallDate: { now: () => (wall -= 60_000) },
    });

    emit({ ...LOGIN_REDIRECT, webContentsId: 1 }); // t=1s → reload
    tick = 1;
    emit({ ...LOGIN_REDIRECT, webContentsId: 1 }); // t=2s → throttled
    tick = 2;
    emit({ ...LOGIN_REDIRECT, webContentsId: 1 }); // t=18s → reload again

    assert.equal(a.reloadCount(), 2);
  });
});
