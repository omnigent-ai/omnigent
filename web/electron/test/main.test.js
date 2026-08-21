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

const { isSetupIdle, withServerLoad } = require("../src/server_load");
const { normalizeUrl } = require("../src/url");
const {
  oidcServerUrlError,
  runOidcBrowserLogin,
  installAndVerifySessionCookie,
} = require("../src/oidc_auth");

const mainSource = readFileSync(path.join(__dirname, "../src/main.js"), "utf8");
const omnigentCliSource = readFileSync(path.join(__dirname, "../src/omnigent_cli.js"), "utf8");
const preloadSource = readFileSync(path.join(__dirname, "../src/preload.js"), "utf8");
const setupSource = readFileSync(path.join(__dirname, "../setup/index.html"), "utf8");
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
  /async function showWebAuthnTimeout[\s\S]*?(?=function resolveServerPath)/,
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
  const state = { origin: null, pendingServerLoads: initialPendingLoads, serverUrl: null };
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
    expandedServerUrlError: (serverUrl, expectedOrigin) =>
      oidcServerUrlError(serverUrl) ??
      (new URL(serverUrl).origin === expectedOrigin ? null : "invalid_server_url"),
    findKnownServerUrl: () => null,
    focusAndRestore: () => {},
    isSetupIdle,
    knownOrigins: () => new Set(),
    loadServerUrl: async (win) => {
      parentLoads.push(win);
      return true;
    },
    originOf: (url) => {
      try {
        return new URL(url).origin;
      } catch {
        return null;
      }
    },
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
  registerFallbacks = true,
  rejectAuthSideEffects = false,
} = {}) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-navigation-test-"));
  const listeners = new Map();
  const calls = { loadFile: [] };
  const appEvents = new Map();
  const webContents = {
    on(eventName, listener) {
      listeners.set(eventName, listener);
    },
    emit(eventName, ...args) {
      listeners.get(eventName)?.({}, ...args);
    },
    getURL: () => serverUrl,
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
    loadURL: () =>
      rejectAuthSideEffects
        ? Promise.reject(new Error("invalid server URL reached navigation"))
        : Promise.resolve(),
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
    "\nmodule.exports.testApi = { createWindow, registerNavigationFallbacks, windows, SETUP_PAGE };";
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
    emit: (eventName, ...args) => webContents.emit(eventName, ...args),
    hasListener: (eventName) => listeners.has(eventName),
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
  it("registers navigation fallbacks when createWindow builds a window", async () => {
    const harness = loadNavigationHarness({ registerFallbacks: false });

    const win = harness.api.createWindow("https://host.example/ml/omnigents");
    await new Promise(setImmediate);
    harness.emit("did-navigate", "https://host.example/ml/omnigents/", 503, "Unavailable");

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

  it("preserves canonical loopback HTTP and HTTPS session detection", async () => {
    const probes = [];
    const ensureWindowOidcSession = runInNewContext(`${oidcSessionCode}; ensureWindowOidcSession`, {
      oidcServerUrlError,
      omnigentCli: require("../src/omnigent_cli"),
      probeServerAuth: async (_session, serverUrl) => {
        probes.push(serverUrl);
        return { kind: "other" };
      },
      session: { defaultSession: {} },
      setWindowAuthenticationNavigation() {},
    });

    const results = await Promise.all(
      [
        "http://localhost:6767",
        "http://127.0.0.1:6767",
        "http://[::1]:6767",
        "https://server.example",
      ].map((serverUrl) => ensureWindowOidcSession({}, serverUrl)),
    );

    assert.deepEqual(results, [true, true, true, true]);
    assert.deepEqual(probes, ["https://server.example"]);
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
      windows: new Map([[win, state]]),
      withServerLoad,
    });

    await showWebAuthnTimeout(win);

    assert.equal(handoffs, 0);
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
          expandedServerUrlError: (serverUrl, expectedOrigin) =>
            oidcServerUrlError(serverUrl) ??
            (new URL(serverUrl).origin === expectedOrigin ? null : "invalid_server_url"),
          isSetupPageSender: () => true,
          loadServerUrl: async () => assert.fail("loaded an unsafe expanded URL"),
          normalizeUrl: () => "https://workspace.example",
          originOf: (url) => new URL(url).origin,
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
      expandedServerUrlError: (serverUrl, expectedOrigin) =>
        oidcServerUrlError(serverUrl) ??
        (new URL(serverUrl).origin === expectedOrigin ? null : "invalid_server_url"),
      fetchServerManifest: async () => ({}),
      configuredServerUrlError: (_raw, normalized) => oidcServerUrlError(normalized),
      isSetupPageSender: () => true,
      loadServerUrl: async (_win, url) => {
        calls.push(["load", url]);
        return true;
      },
      loadSettings: () => ({}),
      normalizeUrl: (url) => url,
      oidcServerUrlError,
      originOf: (url) => new URL(url).origin,
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
      normalizeUrl: (url) => url,
      oidcServerUrlError,
      originOf: (url) => new URL(url).origin,
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
      normalizeUrl: (url) => url,
      oidcServerUrlError,
      originOf: (url) => new URL(url).origin,
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
      isPinnedOriginSender: () => true,
      loadServerUrl: async () => {
        loadCalls += 1;
        return true;
      },
      loadSettings: () => ({ recent_servers: [target] }),
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
    let loadFileCalls = 0;
    let pinCalls = 0;
    const win = { loadFile: () => (loadFileCalls += 1) };
    const handlerSource = openServerSetupCode.slice(
      openServerSetupCode.indexOf("(event) =>"),
      openServerSetupCode.lastIndexOf(");"),
    );
    const handler = runInNewContext(`(${handlerSource})`, {
      BrowserWindow: { fromWebContents: () => win },
      SETUP_PAGE: "/setup/index.html",
      isPinnedOriginSender: () => true,
      pinWindow: () => (pinCalls += 1),
      setWindowServerUrl: () => {},
      windows: new Map([[win, state]]),
    });

    handler({ sender: {} });
    assert.equal(pinCalls, 0);
    assert.equal(loadFileCalls, 0);
    state.pendingServerLoads = 0;
    handler({ sender: {} });
    assert.equal(pinCalls, 1);
    assert.equal(loadFileCalls, 1);
  });

  it("wiring-only: routes intercepted OIDC expiry through the single-load path", () => {
    assert.match(
      liveCode,
      /registerOidcSessionExpiryHandoff\([\s\S]{0,700}loadServerUrl\(win, expiredServerUrl, undefined, returnUrl\)/,
    );
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
  });

  it("normalizes persisted targets before returning setup-page recents", () => {
    assert.match(
      liveCode,
      /ipcMain\.handle\("omnigent:get-recent-servers"[\s\S]{0,300}return normalizeRecentServers\(loadSettings\(\)\.recent_servers\)/,
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
  it("routes a 404 to the setup error surface with the mounted server URL", (t) => {
    const harness = loadNavigationHarness();
    t.after(harness.cleanup);

    harness.emit("did-navigate", "https://host.example/ml/omnigents/health", 404, "Not Found");

    assert.equal(harness.calls.loadFile.length, 1);
    assert.equal(harness.calls.loadFile[0][0], harness.api.SETUP_PAGE);
    const params = new URLSearchParams(harness.calls.loadFile[0][1].search);
    assert.equal(params.get("error"), "404 Not Found");
    assert.equal(params.get("url"), "https://host.example/ml/omnigents");
  });

  it("routes a 503 to the same setup error surface", (t) => {
    const harness = loadNavigationHarness();
    t.after(harness.cleanup);

    harness.emit("did-navigate", "https://host.example/ml/omnigents/", 503, "Service Unavailable");

    assert.equal(harness.calls.loadFile.length, 1);
    const params = new URLSearchParams(harness.calls.loadFile[0][1].search);
    assert.equal(params.get("error"), "503 Service Unavailable");
    assert.equal(params.get("url"), "https://host.example/ml/omnigents");
  });

  it("does not fall back for successful or redirect navigations", (t) => {
    const harness = loadNavigationHarness();
    t.after(harness.cleanup);

    harness.emit("did-navigate", "https://host.example/ml/omnigents/", 200, "OK");
    harness.emit("did-navigate", "https://host.example/ml/omnigents/login", 302, "Found");

    assert.deepEqual(harness.calls.loadFile, []);
  });

  it("ignores a duplicate failure after the first fallback unpins the window", (t) => {
    const harness = loadNavigationHarness();
    t.after(harness.cleanup);

    const failedUrl = "https://host.example/ml/omnigents/health";
    harness.emit("did-navigate", failedUrl, 503, "Service Unavailable");

    assert.equal(harness.api.windows.get(harness.win).origin, null);
    // Unpinning makes the origin guard reject re-entry.
    harness.emit("did-navigate", failedUrl, 503, "Service Unavailable");

    assert.equal(harness.calls.loadFile.length, 1);
  });

  it("keeps the network-error fallback and ignores ERR_ABORTED", (t) => {
    const harness = loadNavigationHarness();
    t.after(harness.cleanup);

    harness.emit(
      "did-fail-load",
      -105,
      "NAME_NOT_RESOLVED",
      "https://host.example/ml/omnigents/",
      true,
    );
    assert.equal(harness.calls.loadFile.length, 1);

    const aborted = loadNavigationHarness();
    t.after(aborted.cleanup);
    aborted.emit("did-fail-load", -3, "ABORTED", "https://host.example/ml/omnigents/", true);
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
