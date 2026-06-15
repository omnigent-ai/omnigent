// Omnigent desktop shell — Electron edition.
//
// A deliberately thin Electron wrapper around the existing web UI. It bundles
// ONLY a tiny "connect to server" setup page; the real application UI is the
// SPA served by the Omnigent server itself. At startup we read a persisted
// server URL and, if present, load it directly so the user lands in the same
// UI they'd see in a browser — now with OS-native notifications and a
// dock/taskbar badge (wired up on the web side via `src/lib/nativeBridge.ts`,
// which detects the Electron preload on `window.omnigentDesktop`).
//
// The "load the server's own SPA" model means there is ZERO UI duplication
// here: change the web app and the desktop app changes with it on next launch.

"use strict";

const {
  app,
  BrowserWindow,
  Menu,
  Notification,
  clipboard,
  dialog,
  ipcMain,
  nativeImage,
  screen,
  session,
  shell,
  systemPreferences,
} = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { registerLocalhostCors } = require("./localhost_cors");

/** Absolute path to the bundled setup page (the "connect to server" form). */
const SETUP_PAGE = path.join(__dirname, "..", "setup", "index.html");

/** The setup page's file:// URL, for verifying IPC sender frames. */
const SETUP_PAGE_URL = pathToFileURL(SETUP_PAGE);

/** Absolute path to the bundled find-in-page bar page. */
const FIND_PAGE = path.join(__dirname, "..", "find", "index.html");

/** The find bar's file:// URL, for verifying IPC sender frames. */
const FIND_PAGE_URL = pathToFileURL(FIND_PAGE);

/** Find bar dimensions and inset from the parent's top-right corner. */
const FIND_BAR_WIDTH = 320;
const FIND_BAR_HEIGHT = 44;
const FIND_BAR_INSET = 16;

/**
 * Chromium net error for a cancelled/superseded navigation (ERR_ABORTED) —
 * fired by e.g. SPA redirects or a second loadURL, not a real failure.
 * Electron doesn't export the net error codes as named constants.
 */
const ERR_ABORTED = -3;

/**
 * Schemes that open externally with no confirmation: they land in the
 * user's browser / mail client, which apply their own safety UX. Anything
 * else launches an OS protocol handler (vscode://, ssh://, …) with
 * page-controlled arguments — and `shell.openExternal`, unlike a browser,
 * shows no prompt of its own — so it goes through a consent dialog first.
 */
const WEB_SCHEMES = new Set(["http:", "https:", "mailto:"]);

/** Absolute path to the app icon (PNG works for the macOS dock at runtime). */
const ICON_PNG = path.join(__dirname, "..", "icons", "icon.png");

/**
 * Permissions the SPA legitimately needs and we auto-grant. The dictation
 * button drives the Web Speech API and a `getUserMedia` audio stream (for the
 * mic level meter); both go through Chromium's permission layer, which in
 * Electron asks the embedder (us) rather than showing Chrome's own prompt.
 * With NO handler wired, Chromium denies by default — which surfaces as a
 * `not-allowed` error the instant `recognition.start()` runs, i.e. "the
 * dictation button does nothing." We grant only the audio-related set and
 * deny everything else.
 *
 * NOTE: this clears the FIRST of two gates. Web Speech `SpeechRecognition`
 * also relies on Google's cloud speech backend keyed to official Chrome
 * builds, which Electron's Chromium lacks — so even with the mic permission
 * granted, recognition may still fail (typically a `network` error). The web
 * app already degrades gracefully there (the button reports "Dictation
 * unavailable" rather than crashing); a fully reliable in-app dictation would
 * need a MediaRecorder + server-side transcription fallback. See the README.
 *
 * ``clipboard-sanitized-write`` backs ``navigator.clipboard.writeText`` —
 * without it every "copy" button in the SPA silently fails (Chromium
 * denies when a permission-check handler is wired and returns false).
 * Sanitized write only lets the page PUT text on the clipboard from a
 * user gesture; ``clipboard-read`` stays denied.
 */
const GRANTED_PERMISSIONS = new Set([
  "media",
  "audioCapture",
  "mediaKeySystem",
  "clipboard-sanitized-write",
]);

/**
 * Chromium's Local Network Access permission names, handled separately from
 * GRANTED_PERMISSIONS because their trust scope is different
 * (localhost-trusted origins, not just pinned ones — see
 * isLocalhostTrustedOrigin). Two names because Chromium renamed the
 * permission: ``loopback-network`` is the granular Chromium 145+ name
 * (valid in Electron 42's Chromium 148, and the one Okta FastPass queries
 * FIRST), ``local-network-access`` the older aggregate it falls back to.
 *
 * The localhost fetches themselves are NOT gated in Electron 42 (Chromium's
 * LNA checks are disabled; verified empirically, including with
 * LocalNetworkAccessChecks force-enabled). But
 * ``navigator.permissions.query({name: ...})`` for these routes through the
 * permission handlers, and IdP device-trust scripts (e.g. Okta FastPass)
 * treat a "denied" answer as fatal — they surface
 * CHROME_LOCAL_NETWORK_ACCESS_DENIED_ERROR without ever attempting their
 * localhost probe. So both names must report "granted" for the pages the
 * localhost CORS layer trusts.
 */
const LNA_PERMISSIONS = new Set(["local-network-access", "loopback-network"]);

/**
 * Keychain access group for the WebAuthn Touch ID platform authenticator
 * (`app.configureWebAuthn`), in the form ``"<TEAM_ID>.ai.omnigent.desktop"``.
 *
 * null disables the platform authenticator: the value only works in a
 * code-signed build whose `keychain-access-groups` entitlement
 * (signing/entitlements.mac.plist) lists the SAME string, so there is no
 * meaningful default — set both places together when configuring signing.
 * External security keys (e.g. YubiKey) work regardless of this setting.
 *
 * Three pieces must agree: this constant, the `keychain-access-groups`
 * entitlement, AND the embedded Developer ID provisioning profile
 * authorizing the group — without the profile, AMFI SIGKILLs the signed
 * app at launch. Details in signing/entitlements.mac.plist.
 * @type {string | null}
 */
const WEBAUTHN_KEYCHAIN_ACCESS_GROUP = "8RMX4WU6F8.ai.omnigent.desktop";

/**
 * Enable the macOS WebAuthn platform authenticator so passkey
 * registration/sign-in shows the native Touch ID / keychain dialog instead
 * of completing invisibly. Two pieces:
 *
 *   1. `app.configureWebAuthn` (Electron ≥ 42, macOS-only) turns on the
 *      Secure-Enclave-backed authenticator. Until it's called,
 *      `PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable()`
 *      resolves false in the page and sites offer only external keys.
 *   2. The `select-webauthn-account` session event fires when a
 *      `navigator.credentials.get()` matches several discoverable
 *      credentials; we show a native chooser and answer with the picked
 *      `credentialId` (answering with nothing cancels the request).
 *
 * No-ops (with a console note) when the access group isn't configured,
 * off macOS, or on an Electron without the API — external security keys
 * keep working through Chromium's built-in CTAP path in all cases.
 * Credentials are device-bound (Secure Enclave), not synced to iCloud
 * Keychain, and invisible to Safari/Chrome — and vice versa.
 */
function registerWebAuthn() {
  if (process.platform !== "darwin") return;
  if (typeof app.configureWebAuthn !== "function") {
    console.log("[omnigent] webauthn: Electron too old for configureWebAuthn; skipping");
    return;
  }
  if (WEBAUTHN_KEYCHAIN_ACCESS_GROUP === null) {
    console.log(
      "[omnigent] webauthn: WEBAUTHN_KEYCHAIN_ACCESS_GROUP not set; " +
        "platform passkeys (Touch ID dialog) disabled — security keys still work",
    );
    return;
  }
  // Dev runs (`electron .`) use the unsigned prebuilt Electron binary, which
  // has no keychain-access-groups entitlement: configuring the authenticator
  // there doesn't fail at this call, but breaks every later ceremony with an
  // opaque NotAllowedError ("operation timed out or was not allowed"). Skip
  // cleanly so dev keeps the silent security-key path.
  if (!app.isPackaged) {
    console.log(
      "[omnigent] webauthn: dev run (unsigned, no keychain entitlement); " +
        "platform passkeys disabled — security keys still work",
    );
    return;
  }
  app.configureWebAuthn({
    touchID: {
      keychainAccessGroup: WEBAUTHN_KEYCHAIN_ACCESS_GROUP,
      // Rendered by macOS as "<app name> is trying to <promptReason>".
      promptReason: "sign in with your passkey",
    },
  });

  session.defaultSession.on("select-webauthn-account", (_event, details, callback) => {
    const accounts = details.accounts ?? [];
    const win = activeWindow();
    if (!win || accounts.length === 0) {
      callback(); // no UI to ask with / nothing to pick → cancel the request
      return;
    }
    // Label each account by whatever name fields the credential carries;
    // the index-based fallback is display-only (the answer is always the
    // credentialId, never the label).
    const labels = accounts.map(
      (a, i) => a.userName || a.userDisplayName || `Account ${i + 1}`,
    );
    void dialog
      .showMessageBox(win, {
        type: "question",
        message: `Choose a passkey for ${details.relyingPartyId}`,
        buttons: [...labels, "Cancel"],
        cancelId: labels.length,
      })
      .then(({ response }) => {
        if (response >= 0 && response < accounts.length) {
          callback(accounts[response].credentialId);
        } else {
          callback(); // Cancel
        }
      })
      .catch(() => callback()); // dialog failure must still answer → cancel
  });
}

/**
 * Origin of a webContents' top-level (main-frame) page, or null when the
 * webContents is absent or already destroyed. Electron passes a null
 * webContents to the permission-check handler for some permission types —
 * null here means "deny", never "skip the check".
 *
 * @param {Electron.WebContents | null} webContents
 * @returns {string | null}
 */
function topLevelOrigin(webContents) {
  if (!webContents || webContents.isDestroyed()) return null;
  return originOf(webContents.getURL());
}

/**
 * Audio permissions whose grant must also open the macOS system mic gate.
 * Without the OS grant, macOS hands Chromium silence and speech still fails
 * even after the in-app permission is allowed.
 */
const MIC_PERMISSIONS = new Set(["media", "audioCapture"]);

/**
 * On macOS, ask the OS for microphone consent (the system TCC prompt) before
 * answering an in-app mic permission grant. Deliberately lazy — called only
 * when the page actually requests the mic (user clicked dictate), never at
 * startup. Resolves regardless of the user's choice: a denial is the user's
 * decision, and the in-app error path handles the resulting silence.
 *
 * @returns {Promise<void>}
 */
async function ensureSystemMicAccess() {
  if (process.platform !== "darwin" || !systemPreferences.askForMediaAccess) return;
  try {
    await systemPreferences.askForMediaAccess("microphone");
  } catch {
    // Best-effort; the in-app error path handles a closed system gate.
  }
}

/**
 * Wire Chromium's permission request/check to our allow-list. Audio grants
 * additionally chain through the macOS system mic prompt (lazily, on first
 * actual mic request) so the OS-level gate is open too.
 */
/**
 * Answer for the Local Network Access permission: granted when the
 * requesting page's origin is localhost-trusted (see
 * isLocalhostTrustedOrigin) and — when Chromium attributes the
 * request/check to a webContents — the requesting page is its top-level
 * page. Permission *checks* (the permissions.query path) can arrive with a
 * null webContents; those are allowed on origin trust alone, because the
 * answer is advisory in Electron 42 — it gates nothing beyond what fetch
 * already allows, and a "denied" would falsely turn away IdP scripts that
 * probe before fetching.
 *
 * @param {string | undefined} requestingUrl Full URL or origin of the
 *   requesting page.
 * @param {Electron.WebContents | null} webContents
 * @returns {boolean}
 */
function lnaPermissionGranted(requestingUrl, webContents) {
  const origin = originOf(requestingUrl ?? "");
  if (!isLocalhostTrustedOrigin(origin)) return false;
  const top = topLevelOrigin(webContents);
  return top === null || top === origin;
}

function registerPermissions() {
  const ses = session.defaultSession;
  // Fires when the page actively requests a permission (getUserMedia, speech).
  // Grants require an allow-listed permission AND a requesting page on an
  // origin some window is pinned to AND that the requesting page IS the
  // top-level page (same origin as the webContents' main frame) — so a
  // pinned-origin iframe embedded in a hostile page, and any page reached
  // via auth redirects or links on foreign origins, gets nothing.
  // local-network-access is the one exception with its own, wider scope.
  ses.setPermissionRequestHandler((webContents, permission, callback, details) => {
    if (LNA_PERMISSIONS.has(permission)) {
      callback(lnaPermissionGranted(details.requestingUrl, webContents));
      return;
    }
    const granted =
      GRANTED_PERMISSIONS.has(permission) &&
      isPinnedServerUrl(details.requestingUrl) &&
      originOf(details.requestingUrl ?? "") === topLevelOrigin(webContents);
    if (granted && MIC_PERMISSIONS.has(permission)) {
      // Surface the OS prompt now (first dictate click), then answer.
      void ensureSystemMicAccess().then(() => callback(true));
      return;
    }
    callback(granted);
  });
  // Fires for synchronous capability checks (some Chromium paths use this
  // instead of the async request); keep the two in lockstep.
  ses.setPermissionCheckHandler((webContents, permission, requestingOrigin) => {
    if (LNA_PERMISSIONS.has(permission)) {
      return lnaPermissionGranted(requestingOrigin, webContents);
    }
    return (
      GRANTED_PERMISSIONS.has(permission) &&
      isPinnedServerUrl(requestingOrigin) &&
      originOf(requestingOrigin ?? "") === topLevelOrigin(webContents)
    );
  });
}

/**
 * True when an origin is the CURRENT top-level page of some open, pinned
 * shell window — i.e. a page the user navigated to in-window from a server
 * they explicitly connected to. Auth flows redirect the window's main
 * frame through SSO/IdP origins that can't be known in advance (e.g.
 * ``abc.aws.databricksapps.com`` → an SSO domain that probes a localhost
 * helper), and this is what lets those pages reach localhost while the
 * user is actually on them. The reachable set stays narrow because
 * in-window navigation only starts from the pinned server (links and
 * window.open go to the external browser — see setWindowOpenHandler);
 * unpinned windows (the setup page) confer nothing, and an iframe never
 * matches because this checks the main frame's origin only.
 *
 * @param {string} origin e.g. ``"https://login.example.com"``.
 * @returns {boolean}
 */
function isCurrentWindowOrigin(origin) {
  for (const [win, state] of windows) {
    if (state.origin === null || win.isDestroyed()) continue;
    if (originOf(win.webContents.getURL()) === origin) return true;
  }
  return false;
}

/**
 * The trust predicate for localhost access, shared by the CORS injection
 * (registerLocalhostAccess) and the Local Network Access permission answer
 * (lnaPermissionGranted). An origin is trusted when it is: an origin some
 * window is pinned to (a server the user explicitly connected to), the
 * current top-level page of a pinned window (SSO/IdP pages reached via
 * auth redirects — see isCurrentWindowOrigin), or hand-listed in
 * settings.json under ``localhost_allowed_origins`` (escape hatch for
 * pages that need localhost while NOT being the visible top-level page).
 *
 * @param {string | null} origin e.g. ``"https://login.example.com"``.
 * @returns {boolean}
 */
function isLocalhostTrustedOrigin(origin) {
  if (!origin) return false;
  if (isPinnedServerUrl(origin)) return true;
  if (isCurrentWindowOrigin(origin)) return true;
  const extra = loadSettings().localhost_allowed_origins;
  return Array.isArray(extra) && extra.includes(origin);
}

/**
 * Allow pages on trusted origins to call localhost services (auth helpers,
 * local runners) by injecting CORS/preflight headers on localhost responses
 * — see localhost_cors.js for the mechanism and isLocalhostTrustedOrigin
 * for the trust scope.
 */
function registerLocalhostAccess() {
  registerLocalhostCors(session.defaultSession, isLocalhostTrustedOrigin);
}

/**
 * Override the macOS dock icon at runtime. In `electron .` (dev) the dock tile
 * name AND icon are read from the generic prebuilt Electron.app bundle, so they
 * show "Electron" + the atom logo; the correct name/icon only land in a
 * packaged build (electron-builder reads productName + icon.icns). We can't
 * change the dock NAME in dev, but `app.dock.setIcon` lets us at least show the
 * real icon. No-op off macOS / if the image fails to load.
 */
function applyDockIcon() {
  if (process.platform !== "darwin" || !app.dock) return;
  // Packaged builds get the bundle icon (Assets.car / icon.icns), which has
  // the standard margins and dynamic-icon support; overriding it with the
  // full-bleed PNG would render oversized in the Dock.
  if (app.isPackaged) return;
  const img = nativeImage.createFromPath(ICON_PNG);
  if (!img.isEmpty()) app.dock.setIcon(img);
}

/**
 * Per-window shell state. The app is multi-window (Server → New Window):
 * every window is an independent BrowserWindow, so a user can view two
 * conversations side by side.
 *
 * Each window is *pinned* to the one server origin the user explicitly
 * connected it to. The pin is the shell's trust boundary: privileged IPC
 * (notifications, badge) and permission grants are honored only for pages
 * on the pinned origin. Navigation itself is NOT restricted — servers may
 * sit behind auth that redirects through external identity providers — so
 * a window can legitimately visit foreign origins; those pages simply get
 * an inert bridge.
 *
 * @typedef {Object} WindowState
 * @property {string | null} origin Origin (e.g. ``"http://localhost:8000"``)
 *   this window is pinned to, or null while it shows the bundled setup page.
 * @property {boolean} ephemeral True for multi-server windows whose
 *   connection must not be persisted to settings.
 * @property {number} badgeCount The unread count this window's SPA last
 *   reported. Each SPA instance reports its server's app-wide unread count,
 *   so the OS badge aggregates per distinct ORIGIN (not per window — two
 *   windows on the same server report the same number and must not be
 *   double-counted), then sums across origins.
 *
 * @type {Map<BrowserWindow, WindowState>}
 */
const windows = new Map();

/**
 * Recompute the app-wide dock/taskbar badge: take each distinct pinned
 * origin's count (max across that origin's windows, which report the same
 * server-wide number modulo timing) and sum across origins.
 * `app.setBadgeCount(0)` clears it (macOS dock, Linux Unity launcher;
 * unsupported on Windows at the app level — Electron returns false there
 * and we don't paper over it).
 *
 * The total AND `app.setBadgeCount`'s boolean return are logged so a "badge
 * never shows" report is diagnosable from the terminal running `npm start`:
 * `true` means the OS accepted the count (so any miss is a Dock /
 * Notification-Center display setting), `false` means the platform rejected
 * it (e.g. Windows app-level, or macOS without a Dock tile).
 */
function updateBadge() {
  /** @type {Map<string, number>} max reported count per pinned origin */
  const perOrigin = new Map();
  for (const state of windows.values()) {
    if (!state.origin) continue;
    perOrigin.set(state.origin, Math.max(perOrigin.get(state.origin) ?? 0, state.badgeCount));
  }
  let total = 0;
  for (const count of perOrigin.values()) total += count;
  const ok = app.setBadgeCount(total);
  console.log(`[omnigent] setBadgeCount(${total}) -> ${ok}`);
}

/**
 * Hostnames are prefixed onto notification titles only when windows are
 * pinned to more than one distinct server (multi-server) — with a
 * single server the prefix would be pure noise.
 *
 * @returns {boolean}
 */
function multipleServersActive() {
  const origins = new Set();
  for (const state of windows.values()) {
    if (state.origin) origins.add(state.origin);
  }
  return origins.size > 1;
}

/**
 * Parse a URL string into its origin, or null when it isn't a valid URL.
 * Used wherever a URL crosses a trust/persistence boundary (saved settings,
 * IPC sender frames) and a parse failure must not throw.
 *
 * @param {string} url e.g. ``"http://localhost:8000/conversations/3"``
 * @returns {string | null} e.g. ``"http://localhost:8000"``, or null.
 */
function originOf(url) {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

/**
 * Read the origin a window is pinned to.
 *
 * @param {BrowserWindow | null | undefined} win
 * @returns {string | null} The pinned origin, or null when the window is
 *   unknown or still on the setup page.
 */
function pinnedOrigin(win) {
  return (win && windows.get(win)?.origin) ?? null;
}

/**
 * True when a URL (or origin string) belongs to an origin some open window
 * is currently pinned to — i.e. a server the user explicitly connected to.
 * Used to scope permission grants, which are per-session rather than
 * per-window, to the set of user-chosen servers.
 *
 * @param {string | undefined} url A full URL or bare origin, e.g.
 *   ``"http://localhost:8000/chat"`` or ``"http://localhost:8000"``.
 * @returns {boolean}
 */
function isPinnedServerUrl(url) {
  const origin = originOf(url ?? "");
  if (!origin) return false;
  for (const state of windows.values()) {
    if (state.origin === origin) return true;
  }
  return false;
}

/**
 * Pin (or unpin) a window to a server origin. Called when a window is
 * created onto a server URL, when the setup page connects it, and (with
 * null) when it returns to the setup page.
 *
 * @param {BrowserWindow} win
 * @param {string | null} origin Origin string from ``new URL(url).origin``,
 *   or null to unpin.
 */
function pinWindow(win, origin) {
  const state = windows.get(win);
  if (!state) return;
  if (state.origin !== origin) {
    // Leaving a server: this window's unread contribution goes with it.
    state.badgeCount = 0;
    updateBadge();
  }
  state.origin = origin;
}

/**
 * The window an OS-menu / app-level action should target: the currently
 * focused shell window, falling back to any open one (or null when none).
 * Per-window IPC (e.g. the setup page persisting a URL) instead resolves the
 * sender's own window via `BrowserWindow.fromWebContents`, not this.
 * @returns {BrowserWindow | null}
 */
function activeWindow() {
  const focused = BrowserWindow.getFocusedWindow();
  if (focused && windows.has(focused)) return focused;
  for (const win of windows.keys()) return win;
  return null;
}

// ---------------------------------------------------------------------------
// Persisted settings (the saved server URL and the recently-connected server
// list), stored as JSON in the per-user app data dir (Electron's `userData`
// path).
// ---------------------------------------------------------------------------

function settingsPath() {
  return path.join(app.getPath("userData"), "settings.json");
}

function loadSettings() {
  try {
    return JSON.parse(fs.readFileSync(settingsPath(), "utf8"));
  } catch {
    // Missing/corrupt file → empty settings (first launch).
    return {};
  }
}

function saveSettings(settings) {
  fs.mkdirSync(app.getPath("userData"), { recursive: true });
  fs.writeFileSync(settingsPath(), JSON.stringify(settings, null, 2), "utf8");
}

/** Maximum number of entries kept in the persisted recent-servers list. */
const MAX_RECENT_SERVERS = 5;

/**
 * Record a successfully-connected server URL at the head of the persisted
 * recent-servers list: most recent first, deduplicated, capped at
 * MAX_RECENT_SERVERS. Mutates `settings` in place; the caller saves it.
 *
 * @param {Record<string, unknown>} settings Settings object from
 *   loadSettings().
 * @param {string} url Normalized server URL from normalizeUrl(),
 *   e.g. ``"http://localhost:8000/"``.
 */
function rememberRecentServer(settings, url) {
  // Tolerate a hand-edited/corrupt settings.json (non-array, junk entries)
  // by rebuilding the list from whatever string entries survive.
  const existing = Array.isArray(settings.recent_servers) ? settings.recent_servers : [];
  settings.recent_servers = [
    url,
    ...existing.filter((u) => typeof u === "string" && u !== url),
  ].slice(0, MAX_RECENT_SERVERS);
}

/**
 * Normalize a user-entered server URL into something navigable. Accepts bare
 * `host:port` (assumes http), trims whitespace, and rejects anything that
 * isn't an http(s) URL — fail loud rather than navigate to garbage.
 *
 * @param {string} raw
 * @returns {string} A normalized absolute http(s) URL.
 */
function normalizeUrl(raw) {
  const trimmed = (raw ?? "").trim();
  if (trimmed === "") throw new Error("server URL is empty");
  const withScheme = trimmed.includes("://") ? trimmed : `http://${trimmed}`;
  let url;
  try {
    url = new URL(withScheme);
  } catch (e) {
    throw new Error(`invalid URL: ${e.message}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`unsupported scheme '${url.protocol}' (use http/https)`);
  }
  return url.toString();
}

/**
 * Path under a Databricks workspace where the Omnigent web UI is mounted. A
 * bare workspace URL serves the workspace's own web app at the root, so a user
 * who pastes just the workspace host (e.g.
 * ``https://<ws>.azuredatabricks.net``) lands on a 404 unless this suffix is
 * appended.
 *
 * NOTE: the Python CLI records the same UI mount as ``/ml/omnigent``
 * (singular) in ``omnigent/conversation_browser.py`` (WORKSPACE_UI_PATH); the
 * plural here is the path that actually resolves on the live workspace. The
 * two should be reconciled — see also that file's WORKSPACE_API_PATH.
 */
const WORKSPACE_UI_PATH = "/ml/omnigents";

/**
 * CSS that hides the Databricks workspace navigation chrome around a
 * workspace-hosted Omnigent SPA.
 *
 * On a workspace the SPA is mounted as a workspace *page*, so Databricks wraps
 * it in its top-nav shell (the dark bar with the workspace switcher). In a
 * dedicated desktop window that chrome is just noise. We promote Omnigent's
 * own root — ``.omnigent-app``, the wrapper ap-web's embed entry sets
 * (``ap-web/src/embed.tsx``) — to a full-viewport overlay so it paints over
 * the workspace bar. Keying on Omnigent's wrapper (defined in THIS repo)
 * rather than the monolith-owned, unstable workspace nav markup keeps this
 * from silently breaking when Databricks reshuffles its chrome; on a
 * standalone (non-embed) build there is no ``.omnigent-app``, so the rule is
 * a harmless no-op.
 */
const WORKSPACE_CHROME_HIDE_CSS = `
  .omnigent-app {
    position: fixed !important;
    inset: 0 !important;
    z-index: 2147483647 !important;
  }
`;

/**
 * Probe timeout for Databricks workspace detection. Deliberately short: a slow
 * or unreachable host must not stall the connect flow — on timeout we fall
 * back to loading the URL exactly as entered.
 */
const WORKSPACE_PROBE_TIMEOUT_MS = 8000;

/**
 * Expand a bare Databricks workspace URL to its Omnigent web-UI mount.
 *
 * Mirrors the omni CLI's behavioral detection
 * (``omnigent/cli.py:_workspace_api_server_url``): rather than match
 * hostnames, probe the URL and adopt the mount only when the host answers
 * like a Databricks workspace — a response carrying the ``server: databricks``
 * header. URLs that already carry a path, or aren't https, are returned
 * untouched WITHOUT a probe, so a user who pastes the full ``…/ml/omnigents``
 * URL (or connects to any non-workspace server) is never second-guessed.
 *
 * The CLI appends the API mount because it's an API client; the desktop shell
 * loads the web UI, so it appends the SPA mount instead.
 *
 * @param {string} normalized A normalized http(s) URL from {@link normalizeUrl}.
 * @returns {Promise<string>} The workspace UI URL when expansion applies, else
 *   the input unchanged.
 */
async function expandDatabricksWorkspaceUrl(normalized) {
  let url;
  try {
    url = new URL(normalized);
  } catch {
    return normalized;
  }
  // Only bare https roots are candidates: a non-root path means the user
  // already pointed at a specific mount, and Databricks workspaces are
  // https-only.
  if (url.protocol !== "https:" || (url.pathname !== "/" && url.pathname !== "")) {
    return normalized;
  }
  let probe;
  try {
    probe = await fetch(`${url.origin}/`, {
      method: "HEAD",
      redirect: "manual",
      signal: AbortSignal.timeout(WORKSPACE_PROBE_TIMEOUT_MS),
    });
  } catch {
    // Unreachable / DNS / TLS / timeout: connect to the URL as given and let
    // the did-fail-load fallback surface any real failure.
    return normalized;
  }
  if ((probe.headers.get("server") ?? "").toLowerCase() !== "databricks") {
    return normalized;
  }
  return `${url.origin}${WORKSPACE_UI_PATH}`;
}

// ---------------------------------------------------------------------------
// Window + navigation
// ---------------------------------------------------------------------------

/** Debounce delay for persisting window bounds while dragging/resizing. */
const SAVE_BOUNDS_DEBOUNCE_MS = 500;

/** Offset applied when a new window would exactly cover an existing one. */
const CASCADE_OFFSET_PX = 24;

/**
 * Read the persisted window bounds from settings, or null when none are
 * saved, the entry is malformed (hand-edited settings.json), or the saved
 * position no longer intersects any connected display's work area (e.g.
 * the monitor it was on has been unplugged) — restoring those would put
 * the window somewhere invisible.
 *
 * @returns {{x: number, y: number, width: number, height: number,
 *   maximized: boolean} | null}
 */
function loadSavedWindowBounds() {
  const saved = loadSettings().window_bounds;
  if (
    !saved ||
    typeof saved.x !== "number" ||
    typeof saved.y !== "number" ||
    typeof saved.width !== "number" ||
    typeof saved.height !== "number"
  ) {
    return null;
  }
  // getDisplayMatching returns the display with the largest overlap; if
  // even that one doesn't intersect the saved rect, the rect is off-screen.
  const area = screen.getDisplayMatching(saved).workArea;
  const intersects =
    saved.x < area.x + area.width &&
    saved.x + saved.width > area.x &&
    saved.y < area.y + area.height &&
    saved.y + saved.height > area.y;
  if (!intersects) return null;
  return {
    x: saved.x,
    y: saved.y,
    width: saved.width,
    height: saved.height,
    maximized: saved.maximized === true,
  };
}

/**
 * Persist a window's bounds to settings so the next launch reopens where
 * the user left it. Saves debounced on move/resize (those events fire
 * continuously during a drag) and once more on close. Stores
 * `getNormalBounds()` — the pre-maximize rect — plus a `maximized` flag,
 * so un-maximizing after a restore returns to a sane size. Last writer
 * wins across windows: the most recently moved/closed window's bounds are
 * what the next launch restores.
 *
 * @param {BrowserWindow} win The shell window to track.
 */
function trackWindowBounds(win) {
  /** @type {NodeJS.Timeout | null} */
  let timer = null;
  const persist = () => {
    if (win.isDestroyed()) return;
    const settings = loadSettings();
    settings.window_bounds = { ...win.getNormalBounds(), maximized: win.isMaximized() };
    saveSettings(settings);
  };
  const debounced = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(persist, SAVE_BOUNDS_DEBOUNCE_MS);
  };
  win.on("resize", debounced);
  win.on("move", debounced);
  win.on("close", () => {
    if (timer) clearTimeout(timer);
    persist();
  });
}

/**
 * Nudge a freshly-created window down-right while it sits (nearly) exactly
 * on top of another open window, so restored bounds and New Window don't
 * stack windows invisibly on one spot.
 *
 * @param {BrowserWindow} win The window to (possibly) reposition.
 */
function cascadeIfCovering(win) {
  const isCovering = () => {
    const [x, y] = win.getPosition();
    for (const other of windows.keys()) {
      if (other === win || other.isDestroyed()) continue;
      const [ox, oy] = other.getPosition();
      if (Math.abs(ox - x) < CASCADE_OFFSET_PX && Math.abs(oy - y) < CASCADE_OFFSET_PX) {
        return true;
      }
    }
    return false;
  };
  // Bounded by the number of open windows, so this always terminates.
  let shifts = windows.size;
  while (shifts-- > 0 && isCovering()) {
    const [x, y] = win.getPosition();
    win.setPosition(x + CASCADE_OFFSET_PX, y + CASCADE_OFFSET_PX);
  }
}

/**
 * Create a shell window and load a destination, in priority order:
 *   1. `targetUrl`, when given (used by "New Window" to clone the current
 *      window's exact URL — e.g. a specific conversation).
 *   2. the saved server URL (the normal launch path).
 *   3. the bundled setup page (first run / no server configured).
 *
 * @param {string} [targetUrl] Explicit http(s) URL to load instead of the
 *   saved server. Anything not http(s) is ignored (we never load file:// or
 *   internal URLs from an untrusted caller).
 * @param {{ephemeral?: boolean}} [opts] ``ephemeral: true`` creates a debug
 *   multi-server window: it opens on the setup page (ignoring the saved
 *   server) and a URL connected from it is pinned to this window only,
 *   never persisted to settings.
 * @returns {BrowserWindow}
 */
function createWindow(targetUrl, opts = {}) {
  const ephemeral = opts.ephemeral === true;
  const savedBounds = loadSavedWindowBounds();
  const win = new BrowserWindow({
    width: savedBounds?.width ?? 1280,
    height: savedBounds?.height ?? 860,
    // Without saved coordinates Electron centers the window.
    ...(savedBounds ? { x: savedBounds.x, y: savedBounds.y } : {}),
    minWidth: 720,
    minHeight: 480,
    title: "Omnigent",
    backgroundColor: "#0b0b0c",
    // macOS: hide the native title bar but keep the traffic lights, inset
    // into the content. The web layer provides the drag surface + clearance
    // (see ap-web `[data-electron-mac]` rules and the setup page's
    // .drag-strip). Other platforms keep their native frame — `hiddenInset`
    // is macOS-only and a frameless window without `titleBarOverlay` would
    // lose its window controls there.
    ...(process.platform === "darwin" ? { titleBarStyle: "hiddenInset" } : {}),
    webPreferences: {
      // Security: the SPA is remote/untrusted relative to the shell, so we
      // keep Node out of the renderer and isolate the preload's context.
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      // Electron passes HTML5 drag-drop through to the page by default (no
      // native handler intercepts it), so images drop onto the composer
      // textbox with no extra work.
      spellcheck: true,
    },
  });
  const explicit =
    typeof targetUrl === "string" && /^https?:\/\//i.test(targetUrl) ? targetUrl : undefined;
  const saved = loadSettings().server_url;
  // An explicit target (New Window cloning a sibling) always wins. Otherwise
  // ephemeral windows start on the setup page so the user can enter the
  // alternate server, and normal windows fall back to the saved server.
  const candidate =
    explicit ??
    (ephemeral ? null : typeof saved === "string" && saved.length > 0 ? saved : null);
  // A candidate that doesn't parse (hand-edited/corrupt settings.json) is
  // treated as "no server configured" rather than crashing window creation.
  const destinationOrigin = candidate ? originOf(candidate) : null;
  const destination = destinationOrigin ? candidate : null;
  windows.set(win, {
    // Pin to the destination's origin up front; setup-page windows stay
    // unpinned (null) until the user connects them.
    origin: destinationOrigin,
    ephemeral,
    badgeCount: 0,
  });
  if (destination) {
    void win.loadURL(destination);
  } else {
    // ?ephemeral=1 only changes the setup page's copy (the window's
    // WindowState is the source of truth for persistence behavior).
    const search = new URLSearchParams();
    if (ephemeral) search.set("ephemeral", "1");
    if (candidate && !destinationOrigin) {
      // Fail loud on a corrupt hand-edited settings.json: show WHY the
      // window landed on setup instead of silently presenting a blank form.
      search.set("error", "saved server URL in settings.json is not a valid URL");
      search.set("url", candidate);
    }
    void win.loadFile(SETUP_PAGE, search.size > 0 ? { search: search.toString() } : undefined);
  }

  // Never spawn chromeless Electron windows: web links open in the user's
  // real browser, and any other scheme (a custom OS protocol handler like
  // vscode://) requires explicit user consent first — see WEB_SCHEMES.
  win.webContents.setWindowOpenHandler(({ url }) => {
    let scheme = null;
    try {
      scheme = new URL(url).protocol;
    } catch {
      // Unparseable URL from page content — nothing safe to open.
    }
    if (scheme && WEB_SCHEMES.has(scheme)) {
      void shell.openExternal(url);
    } else if (scheme) {
      void confirmExternalProtocol(win, url, scheme);
    }
    return { action: "deny" };
  });

  // Server unreachable / DNS failure / TLS error → fall back to the setup
  // page with the failure shown, instead of stranding the user on Chromium's
  // raw error surface with no way back. The saved server_url is left intact:
  // the server may simply be down, and Connect retries it.
  win.webContents.on(
    "did-fail-load",
    (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
      if (!isMainFrame) return;
      if (errorCode === ERR_ABORTED) return;
      // A failure report for a URL the window is no longer pinned to (the
      // window was re-pointed while the failing load was in flight) must
      // not yank the window off its new destination.
      const failedOrigin = originOf(validatedURL ?? "");
      if (failedOrigin !== windows.get(win)?.origin) return;
      const params = new URLSearchParams({
        error: `${errorDescription || "load failed"} (${errorCode})`,
        // The failure often happens on a deep SPA route (e.g. /chat/…);
        // prefill the setup form with just the server origin — that's what
        // the user connects to — not the full path that happened to fail.
        url: failedOrigin ? failedOrigin + "/" : (validatedURL ?? ""),
      });
      if (windows.get(win)?.ephemeral) params.set("ephemeral", "1");
      pinWindow(win, null); // back on the setup page → no trusted origin
      void win.loadFile(SETUP_PAGE, { search: params.toString() });
    },
  );

  // Databricks workspace-hosted Omnigent renders inside the workspace's
  // top-nav chrome (the SPA is a workspace page). On a dedicated desktop
  // window, hide it by overlaying Omnigent's own root — see
  // WORKSPACE_CHROME_HIDE_CSS. Re-applied on every full load (a server switch
  // is a fresh document); the SPA's own client-side routing keeps the same
  // document, so the injected stylesheet persists across in-app navigation.
  win.webContents.on("did-finish-load", () => {
    let pathname = "";
    try {
      pathname = new URL(win.webContents.getURL()).pathname;
    } catch {
      return;
    }
    if (pathname.startsWith(WORKSPACE_UI_PATH)) {
      void win.webContents.insertCSS(WORKSPACE_CHROME_HIDE_CSS);
    }
  });

  win.on("closed", () => {
    windows.delete(win);
    updateBadge(); // drop this window's contribution from the app-wide badge
  });
  attachContextMenu(win);
  cascadeIfCovering(win);
  if (savedBounds?.maximized) win.maximize();
  trackWindowBounds(win);
  return win;
}

/** Maximum number of spelling suggestions offered in the context menu. */
const MAX_SPELL_SUGGESTIONS = 5;

/**
 * Attach a right-click context menu to a window's webContents. Electron
 * ships NO context menu by default, so without this there is no
 * copy/paste/spell-suggestion UI anywhere in the app.
 *
 * The menu is built per-invocation from Chromium's hit-test `params`:
 *   - spelling suggestions + "Add to Dictionary" over a misspelled word
 *     (`spellcheck: true` is set on the window's webPreferences),
 *   - Copy Link Address over a link,
 *   - Cut / Copy / Paste / Select All in editable fields, Copy over a
 *     text selection — each enabled per Chromium's `editFlags`.
 * Right-clicking dead space shows nothing (no popup) rather than a menu
 * of disabled items.
 *
 * @param {BrowserWindow} win The shell window to attach to.
 */
function attachContextMenu(win) {
  win.webContents.on("context-menu", (_event, params) => {
    /** @type {Electron.MenuItemConstructorOptions[]} */
    const template = [];

    if (params.misspelledWord) {
      for (const suggestion of params.dictionarySuggestions.slice(0, MAX_SPELL_SUGGESTIONS)) {
        template.push({
          label: suggestion,
          click: () => win.webContents.replaceMisspelling(suggestion),
        });
      }
      template.push({
        label: "Add to Dictionary",
        click: () =>
          win.webContents.session.addWordToSpellCheckerDictionary(params.misspelledWord),
      });
      template.push({ type: "separator" });
    }

    if (params.linkURL) {
      template.push({
        label: "Copy Link Address",
        click: () => clipboard.writeText(params.linkURL),
      });
      template.push({ type: "separator" });
    }

    if (params.isEditable) {
      template.push(
        { role: "cut", enabled: params.editFlags.canCut },
        { role: "copy", enabled: params.editFlags.canCopy },
        { role: "paste", enabled: params.editFlags.canPaste },
        { role: "selectAll", enabled: params.editFlags.canSelectAll },
      );
    } else if (params.selectionText.trim() !== "") {
      template.push({ role: "copy" });
    }

    // Drop a trailing separator (e.g. link menu over a non-editable,
    // unselected area) and skip the popup entirely when nothing applies.
    while (template.length > 0 && template[template.length - 1].type === "separator") {
      template.pop();
    }
    if (template.length === 0) return;
    Menu.buildFromTemplate(template).popup({ window: win });
  });
}

// ---------------------------------------------------------------------------
// Find in page. Cmd/Ctrl+F opens a small frameless child window (the bundled
// find/index.html) anchored to the parent's top-right corner; the actual
// search runs in the main process against the PARENT's webContents via
// findInPage. A child window (rather than DOM injected into the remote SPA)
// keeps the shell's hands off server-controlled pages entirely.
// ---------------------------------------------------------------------------

/**
 * The open find bar per shell window. At most one bar per window; absent
 * when the window has no bar open.
 * @type {Map<BrowserWindow, BrowserWindow>} shell window → its find bar
 */
const findBars = new Map();

/**
 * Anchor a find bar to its parent's top-right content corner. Called at
 * creation and again whenever the parent moves or resizes.
 *
 * @param {BrowserWindow} target The shell window being searched.
 * @param {BrowserWindow} bar The find bar child window.
 */
function positionFindBar(target, bar) {
  if (target.isDestroyed() || bar.isDestroyed()) return;
  const content = target.getContentBounds();
  bar.setBounds({
    x: content.x + content.width - FIND_BAR_WIDTH - FIND_BAR_INSET,
    y: content.y + FIND_BAR_INSET,
    width: FIND_BAR_WIDTH,
    height: FIND_BAR_HEIGHT,
  });
}

/**
 * Open the find bar for a shell window (or re-focus the one already open).
 * The bar is a frameless always-on-top-of-parent child window with its own
 * narrow preload (`find_preload.js`); search results stream back to it via
 * the parent webContents' `found-in-page` event.
 *
 * @param {BrowserWindow} target The shell window to search.
 */
function openFindBar(target) {
  const existing = findBars.get(target);
  if (existing && !existing.isDestroyed()) {
    existing.focus();
    existing.webContents.send("omnigent:find-activate");
    return;
  }
  const bar = new BrowserWindow({
    parent: target,
    frame: false,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    // Transparent so the page's rounded-corner card is the visible shape.
    transparent: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "find_preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  findBars.set(target, bar);
  void bar.loadFile(FIND_PAGE);

  const reposition = () => positionFindBar(target, bar);
  const onFound = (_event, result) => {
    if (bar.isDestroyed()) return;
    bar.webContents.send("omnigent:find-result", {
      active: result.activeMatchOrdinal,
      matches: result.matches,
    });
  };
  target.on("resize", reposition);
  target.on("move", reposition);
  target.webContents.on("found-in-page", onFound);

  bar.once("ready-to-show", () => {
    reposition();
    bar.show();
  });
  bar.on("closed", () => {
    findBars.delete(target);
    if (!target.isDestroyed()) {
      target.removeListener("resize", reposition);
      target.removeListener("move", reposition);
      target.webContents.removeListener("found-in-page", onFound);
      target.webContents.stopFindInPage("clearSelection");
      target.focus();
    }
  });
}

/**
 * The shell window Cmd/Ctrl+F should act on. Like activeWindow(), but when
 * the focused window is a find BAR (focus sits in its input), the shortcut
 * targets the bar's parent shell window rather than falling back to an
 * arbitrary one.
 *
 * @returns {BrowserWindow | null}
 */
function findTargetForShortcut() {
  const focused = BrowserWindow.getFocusedWindow();
  if (focused && !windows.has(focused)) {
    const parent = focused.getParentWindow();
    if (parent && windows.has(parent)) return parent;
  }
  return activeWindow();
}

/**
 * Resolve which shell window a find-bar IPC message controls: the entry in
 * `findBars` whose bar owns the sending webContents. Null for senders that
 * aren't a live find bar — callers must drop those messages.
 *
 * @param {Electron.IpcMainEvent} event
 * @returns {BrowserWindow | null}
 */
function findBarTarget(event) {
  for (const [target, bar] of findBars) {
    if (!bar.isDestroyed() && bar.webContents === event.sender) return target;
  }
  return null;
}

/**
 * True when an IPC event was sent by the bundled find bar page. Same
 * path-compare approach as isSetupPageSender.
 *
 * @param {Electron.IpcMainEvent} event
 * @returns {boolean}
 */
function isFindBarSender(event) {
  const frameUrl = event.senderFrame?.url ?? "";
  let url;
  try {
    url = new URL(frameUrl);
  } catch {
    return false;
  }
  return url.protocol === "file:" && url.pathname === FIND_PAGE_URL.pathname;
}

/**
 * Open a new window cloning the focused window's current URL when it's a
 * loaded server page, so "New Window" lands on the same place (and the user
 * can then navigate it to a different conversation). Falls back to a plain
 * new window on the saved server when there's no usable current URL.
 */
function newWindow() {
  const win = activeWindow();
  const current = win?.webContents.getURL();
  // Cloning an ephemeral (multi-server) window keeps the clone
  // ephemeral, so Change Server… from it still won't touch saved settings.
  createWindow(current, { ephemeral: win ? windows.get(win)?.ephemeral === true : false });
}

/**
 * Ask the user before handing a non-web URL to an OS protocol handler
 * (vscode://, ssh://, …). Mirrors the external-protocol prompt every browser
 * shows: the dialog displays the requesting page's origin and the FULL,
 * unabbreviated URL (protocol handlers have a history of argument-injection
 * bugs, so the user must be able to see exactly what is passed), with
 * Cancel as the default button.
 *
 * "Always allow" is offered only while the window's top-level page is on
 * its pinned server origin, and the grant is persisted per
 * (scheme, server origin) in settings.json under `allowed_protocols` —
 * trusting vscode:// links from your own server must not trust them from
 * every page this window ever visits.
 *
 * @param {BrowserWindow} win The window whose page requested the URL.
 * @param {string} url The full URL to open, e.g. ``"vscode://file/x.py"``.
 * @param {string} scheme The URL's scheme including the colon,
 *   e.g. ``"vscode:"``.
 */
async function confirmExternalProtocol(win, url, scheme) {
  const pinned = pinnedOrigin(win);
  // The persisted grant applies only when the user is actually ON the
  // pinned server — a foreign page reached via redirect gets a fresh
  // prompt even for an always-allowed scheme.
  const onPinnedServer = pinned !== null && originOf(win.webContents.getURL()) === pinned;
  const allowedSchemes = loadSettings().allowed_protocols?.[pinned] ?? [];
  if (onPinnedServer && allowedSchemes.includes(scheme)) {
    void shell.openExternal(url);
    return;
  }
  const requester = originOf(win.webContents.getURL());
  const { response, checkboxChecked } = await dialog.showMessageBox(win, {
    type: "warning",
    buttons: ["Cancel", "Open"],
    defaultId: 0, // Cancel is the safe default
    cancelId: 0,
    message: `Open this ${scheme.slice(0, -1)} link?`,
    detail: `${requester ?? "This page"} wants to open:\n\n${url}`,
    checkboxLabel: onPinnedServer
      ? `Always allow ${scheme.slice(0, -1)} links from ${new URL(pinned).host}`
      : undefined,
    checkboxChecked: false,
  });
  if (response !== 1) return;
  if (checkboxChecked && onPinnedServer) {
    const settings = loadSettings();
    const grants = settings.allowed_protocols ?? {};
    const schemes = grants[pinned] ?? [];
    if (!schemes.includes(scheme)) schemes.push(scheme);
    grants[pinned] = schemes;
    settings.allowed_protocols = grants;
    saveSettings(settings);
  }
  void shell.openExternal(url);
}

/**
 * OS-level attention cue for a notification fired while the app is frontmost,
 * where the banner is suppressed by the OS. On macOS we bounce the dock icon
 * (`informational` = a single gentle bounce); on Windows/Linux we flash the
 * window frame. No-op when the window is the foreground, actively-focused
 * surface AND nothing is queued — but we always cue here because the web layer
 * only calls notify for sessions the user is NOT actively viewing, so a cue is
 * always warranted. Wrapped in try/catch: a cue must never break notifying.
 */
function signalForeground() {
  try {
    if (process.platform === "darwin" && app.dock) {
      // "informational" bounces once; "critical" bounces until focused. We use
      // the gentler one — this is an FYI, not an alert.
      app.dock.bounce("informational");
    } else {
      const win = activeWindow();
      if (win && !win.isFocused()) win.flashFrame(true);
    }
  } catch (err) {
    console.warn("[omnigent] signalForeground failed:", err);
  }
}

/**
 * Forget the saved server URL and return the focused window to the bundled
 * setup page so the user can enter a new one. For an ephemeral (debug
 * multi-server) window nothing was persisted, so only that window returns
 * to the setup page — the saved server stays untouched.
 */
function changeServer() {
  const win = activeWindow();
  const ephemeral = win ? windows.get(win)?.ephemeral === true : false;
  if (!ephemeral) {
    const settings = loadSettings();
    delete settings.server_url;
    saveSettings(settings);
  }
  if (win) {
    pinWindow(win, null); // back on the setup page → no trusted origin
    void win.loadFile(SETUP_PAGE, ephemeral ? { search: "ephemeral=1" } : undefined);
  }
}

// ---------------------------------------------------------------------------
// Application menu — start from Electron's standard menu (which wires up the
// platform text-editing shortcuts: Cmd/Ctrl-A/C/V/X/Z via the Edit role) and
// insert our custom "Server" submenu (New Window, Change Server…). This is the
// Electron way to avoid a common bug: a hand-rolled menu that drops the Edit
// roles kills those shortcuts inside webview text fields.
// ---------------------------------------------------------------------------

function buildMenu() {
  const isMac = process.platform === "darwin";

  /** @type {Electron.MenuItemConstructorOptions[]} */
  const template = [];

  // macOS app menu (About/Services/Hide/Quit), named "Omnigent" via the
  // app name set below. Non-mac platforms have no app menu.
  if (isMac) {
    template.push({ role: "appMenu" });
  }

  /** @type {Electron.MenuItemConstructorOptions[]} */
  const serverSubmenu = [
    {
      id: "new_window",
      label: "New Window",
      // Standard new-window accelerator; the role-based File menu below
      // doesn't include one, so we own it here.
      accelerator: "CmdOrCtrl+N",
      click: () => newWindow(),
    },
    {
      id: "new_server_window",
      // A second server in its own window. The connection is per-window —
      // it never replaces the saved default server.
      label: "New Window on Different Server…",
      click: () => createWindow(undefined, { ephemeral: true }),
    },
    { type: "separator" },
    {
      id: "change_server",
      label: "Change Server…",
      click: () => changeServer(),
    },
  ];

  // Our custom Server menu, inserted right after the leftmost menu — index 1
  // on macOS (after the app menu), first on Linux/Windows.
  template.push({
    label: "Server",
    submenu: serverSubmenu,
  });

  // Standard roles — these carry the predefined keyboard shortcuts.
  template.push({ role: "fileMenu" });
  // The Edit roles (Undo/Redo/Cut/Copy/Paste/Select All) carry the platform
  // text-editing shortcuts; hand-rolled here instead of `role: "editMenu"`
  // only so Find… can live where users expect it.
  template.push({
    label: "Edit",
    submenu: [
      { role: "undo" },
      { role: "redo" },
      { type: "separator" },
      { role: "cut" },
      { role: "copy" },
      { role: "paste" },
      ...(isMac ? [{ role: "pasteAndMatchStyle" }] : []),
      { role: "delete" },
      { role: "selectAll" },
      { type: "separator" },
      {
        id: "find",
        label: "Find…",
        accelerator: "CmdOrCtrl+F",
        click: () => {
          const target = findTargetForShortcut();
          if (target) openFindBar(target);
        },
      },
    ],
  });
  // Same items as `role: "viewMenu"`, hand-rolled so Toggle Developer
  // Tools (and its accelerator) can be dropped from release builds.
  template.push({
    label: "View",
    submenu: [
      { role: "reload" },
      { role: "forceReload" },
      ...(app.isPackaged ? [] : [{ role: "toggleDevTools" }]),
      { type: "separator" },
      { role: "resetZoom" },
      { role: "zoomIn" },
      { role: "zoomOut" },
      { type: "separator" },
      { role: "togglefullscreen" },
    ],
  });
  template.push({ role: "windowMenu" });

  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
}

// ---------------------------------------------------------------------------
// IPC: the preload bridge (window.omnigentDesktop) forwards these from the
// renderer. Kept to the two OS integrations the web app needs.
//
// Trust model: navigation is unrestricted (auth-fronted servers redirect
// through external identity providers), so the preload bridge is reachable
// from pages we never chose to trust. The gate therefore lives HERE: every
// handler verifies the sender frame before acting, making the bridge inert
// on any page that isn't the window's pinned server origin (or, for the
// setup bridge, the bundled setup page itself).
// ---------------------------------------------------------------------------

/**
 * True when an IPC event was sent by the bundled setup page. Compares the
 * sender frame's file:// path against the setup page's path, ignoring any
 * query string (the setup page is loaded with ``?error=…`` / ``?ephemeral=1``
 * variants).
 *
 * @param {Electron.IpcMainInvokeEvent | Electron.IpcMainEvent} event
 * @returns {boolean}
 */
function isSetupPageSender(event) {
  const frameUrl = event.senderFrame?.url ?? "";
  let url;
  try {
    url = new URL(frameUrl);
  } catch {
    return false;
  }
  return url.protocol === "file:" && url.pathname === SETUP_PAGE_URL.pathname;
}

/**
 * True when an IPC event was sent by a page on the sender window's pinned
 * server origin — the only pages allowed to use the privileged desktop
 * bridge (notifications, badge). False for unpinned windows (setup page),
 * unknown windows, and any foreign origin reached via redirect or link.
 *
 * Both the CALLING frame and the window's TOP-LEVEL frame must be on the
 * pinned origin. The calling-frame check alone is not enough: a hostile
 * top-level page can embed the pinned server in an iframe (unless the
 * server sends frame-ancestors), and that iframe is genuinely on the
 * pinned origin — but the page the user is looking at is the attacker's.
 * Privileges flow only when the whole visible page is the server's.
 *
 * @param {Electron.IpcMainInvokeEvent | Electron.IpcMainEvent} event
 * @returns {boolean}
 */
function isPinnedOriginSender(event) {
  const pinned = pinnedOrigin(BrowserWindow.fromWebContents(event.sender));
  if (!pinned) return false;
  if (originOf(event.senderFrame?.url ?? "") !== pinned) return false;
  // event.sender.getURL() is the webContents' main-frame URL.
  return originOf(event.sender.getURL()) === pinned;
}

function registerIpc() {
  // Setup page → persist URL and navigate the SENDING window to it. We target
  // the window that owns the setup page (via its webContents) rather than a
  // global, so connecting from one window doesn't hijack another.
  ipcMain.handle("omnigent:set-server-url", async (event, url) => {
    if (!isSetupPageSender(event)) {
      // A server page must never be able to re-point which server is saved.
      throw new Error("set-server-url is only available to the setup page");
    }
    const normalized = normalizeUrl(url); // throws → rejects → setup page shows error
    // Bare Databricks workspace URLs serve a 404 at the root; expand them to
    // the Omnigent UI mount so the user can paste just the workspace host.
    const target = await expandDatabricksWorkspaceUrl(normalized);
    const win = BrowserWindow.fromWebContents(event.sender) ?? activeWindow();
    // Multi-server windows connect without touching the saved server —
    // the connection lives and dies with the window.
    const ephemeral = Boolean(win && windows.get(win)?.ephemeral);
    if (!ephemeral) {
      const settings = loadSettings();
      // The saved default persists immediately even if this load fails:
      // the failure fallback keeps it pre-filled so Connect retries it.
      settings.server_url = target;
      saveSettings(settings);
    }
    if (win) {
      // The user explicitly chose this server — it becomes the window's
      // trusted origin for privileged IPC and permission grants.
      pinWindow(win, new URL(target).origin);
      win
        .loadURL(target)
        .then(() => {
          // Only a server that actually responded earns a recents slot —
          // a typo'd or unreachable URL must not show up in the
          // quick-pick list on the setup page.
          if (ephemeral) return;
          const settings = loadSettings();
          rememberRecentServer(settings, target);
          saveSettings(settings);
        })
        .catch(() => {
          // Load failure is handled by the did-fail-load fallback (setup
          // page with the error); the URL is deliberately not recorded.
        });
    }
  });

  // Setup page → pre-fill the input with any saved URL.
  ipcMain.handle("omnigent:get-server-url", (event) => {
    if (!isSetupPageSender(event)) {
      throw new Error("get-server-url is only available to the setup page");
    }
    return loadSettings().server_url ?? null;
  });

  // Setup page → recently-connected servers, most recent first, for the
  // quick-pick list under the URL form.
  ipcMain.handle("omnigent:get-recent-servers", (event) => {
    if (!isSetupPageSender(event)) {
      throw new Error("get-recent-servers is only available to the setup page");
    }
    const recents = loadSettings().recent_servers;
    // Same hand-edited-settings tolerance as rememberRecentServer.
    return Array.isArray(recents) ? recents.filter((u) => typeof u === "string") : [];
  });

  // SPA title-bar server picker → the sender window's pinned origin plus the
  // persisted recent-servers list, so the picker can render "current server"
  // and the switch targets. Foreign pages get null (nothing to fingerprint).
  ipcMain.handle("omnigent:get-server-picker", (event) => {
    if (!isPinnedOriginSender(event)) {
      console.warn("[omnigent] get-server-picker from untrusted sender dropped");
      return null;
    }
    const win = BrowserWindow.fromWebContents(event.sender);
    const recents = loadSettings().recent_servers;
    return {
      // isPinnedOriginSender guarantees the sender window is tracked.
      currentOrigin: windows.get(win).origin,
      recentServers: Array.isArray(recents) ? recents.filter((u) => typeof u === "string") : [],
    };
  });

  // SPA title-bar server picker → re-point the SENDING window to another
  // server. Only URLs already in the persisted recent-servers list are
  // accepted: pinning is a privilege grant (notifications, badge, protocol
  // grants), so a server page must never be able to pin a window to an
  // arbitrary origin of its choosing — only to servers the user previously
  // connected to by hand.
  ipcMain.handle("omnigent:switch-server", (event, url) => {
    if (!isPinnedOriginSender(event)) {
      throw new Error("switch-server is only available to a connected server page");
    }
    const recents = loadSettings().recent_servers;
    const known = Array.isArray(recents) && recents.includes(url);
    if (!known) {
      throw new Error("switch-server target must be a previously-connected server");
    }
    const win = BrowserWindow.fromWebContents(event.sender);
    const ephemeral = Boolean(win && windows.get(win)?.ephemeral);
    if (!ephemeral) {
      const settings = loadSettings();
      settings.server_url = url;
      saveSettings(settings);
    }
    if (win) {
      pinWindow(win, new URL(url).origin);
      win
        .loadURL(url)
        .then(() => {
          if (ephemeral) return;
          const settings = loadSettings();
          rememberRecentServer(settings, url); // bump to head of the recents
          saveSettings(settings);
        })
        .catch(() => {
          // Load failure falls back via did-fail-load → setup page w/ error.
        });
    }
  });

  // SPA title-bar server picker → "connect to new server": return the
  // SENDING window to the bundled setup page. Unlike Change Server… this
  // keeps the saved default server (connecting from setup overwrites it
  // only when the user actually submits a URL).
  ipcMain.on("omnigent:open-server-setup", (event) => {
    if (!isPinnedOriginSender(event)) {
      console.warn("[omnigent] open-server-setup from untrusted sender dropped");
      return;
    }
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win) return;
    const ephemeral = windows.get(win)?.ephemeral === true;
    pinWindow(win, null); // back on the setup page → no trusted origin
    void win.loadFile(SETUP_PAGE, ephemeral ? { search: "ephemeral=1" } : undefined);
  });

  // Find bar → run/continue a search in its parent window. Empty text
  // clears the highlight and zeroes the counter (findInPage rejects empty
  // queries, so it never reaches it).
  ipcMain.on("omnigent:find-query", (event, params) => {
    if (!isFindBarSender(event)) {
      console.warn("[omnigent] find-query from untrusted sender dropped");
      return;
    }
    const target = findBarTarget(event);
    if (!target || target.isDestroyed()) return;
    const text = String(params?.text ?? "");
    if (text === "") {
      target.webContents.stopFindInPage("clearSelection");
      event.sender.send("omnigent:find-result", { active: 0, matches: 0 });
      return;
    }
    target.webContents.findInPage(text, {
      forward: params?.forward !== false,
      findNext: params?.findNext === true,
    });
  });

  // Find bar → dismiss itself (Esc / ✕). Cleanup (stop search, refocus the
  // parent) lives in the bar's "closed" handler in openFindBar.
  ipcMain.on("omnigent:find-close", (event) => {
    if (!isFindBarSender(event)) {
      console.warn("[omnigent] find-close from untrusted sender dropped");
      return;
    }
    const bar = BrowserWindow.fromWebContents(event.sender);
    if (bar && !bar.isDestroyed()) bar.close();
  });

  // Dock/taskbar badge. Each window's SPA reports ITS unread count; the
  // app-wide badge shown is the sum across windows (see updateBadge), so two
  // windows on different servers don't clobber each other's counts.
  ipcMain.on("omnigent:set-badge-count", (event, count) => {
    if (!isPinnedOriginSender(event)) {
      console.warn("[omnigent] set-badge-count from untrusted sender dropped");
      return;
    }
    // isPinnedOriginSender guarantees the sender window is tracked.
    const state = windows.get(BrowserWindow.fromWebContents(event.sender));
    state.badgeCount = typeof count === "number" && count > 0 ? Math.floor(count) : 0;
    updateBadge();
  });

  // OS notification via the main-process Notification API. Clicking focuses
  // the app window (the useful default). Resolves true when shown.
  //
  // Foreground caveat (esp. macOS): the OS suppresses the BANNER for a
  // notification posted by the frontmost app — it still lands in Notification
  // Center, but no toast appears, which reads as "notifications only work when
  // backgrounded." The web layer already decides WHETHER to notify (it fires
  // for any session except the one you're actively viewing), so when the
  // window is focused we add an OS-level attention cue the frontmost app CAN
  // show: bounce the macOS dock icon / flash the taskbar frame. That makes a
  // non-open session's turn-end noticeable even with the app in front.
  ipcMain.handle("omnigent:notify", (event, params) => {
    if (!isPinnedOriginSender(event)) {
      // The contract is "resolves false when not shown" — a foreign page
      // gets a quiet false, not an exception it could fingerprint.
      console.warn("[omnigent] notify from untrusted sender dropped");
      return false;
    }
    if (!Notification.isSupported()) return false;
    // With windows pinned to more than one server (multi-server),
    // prefix the firing server's hostname so alerts are attributable.
    let title = String(params?.title ?? "");
    if (multipleServersActive()) {
      const origin = pinnedOrigin(BrowserWindow.fromWebContents(event.sender));
      // isPinnedOriginSender above guarantees a pinned, parseable origin.
      title = `[${new URL(origin).host}] ${title}`;
    }
    const notification = new Notification({
      title,
      body: String(params?.body ?? ""),
    });
    // Focus the window that fired the notification (so a click lands on the
    // right one in a multi-window setup), falling back to any open window.
    notification.on("click", () => {
      const win = BrowserWindow.fromWebContents(event.sender) ?? activeWindow();
      if (win) {
        if (win.isMinimized()) win.restore();
        win.focus();
      }
    });
    notification.show();
    signalForeground();
    return true;
  });
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

// Name drives the macOS app menu title and the notification source name.
app.setName("Omnigent");

// Single-instance: focus the existing window instead of opening a second.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const win = activeWindow();
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(() => {
    // App User Model ID so Windows attributes notifications/taskbar correctly.
    if (process.platform === "win32") app.setAppUserModelId("ai.omnigent.desktop");
    applyDockIcon();
    registerPermissions();
    registerLocalhostAccess();
    registerWebAuthn();
    registerIpc();
    buildMenu();
    createWindow();


    app.on("activate", () => {
      // macOS: re-create the window when the dock icon is clicked and none open.
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on("window-all-closed", () => {
    // macOS apps typically stay alive until Cmd-Q.
    if (process.platform !== "darwin") app.quit();
  });
}
