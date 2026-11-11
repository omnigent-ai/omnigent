// Electron preload — the ONLY bridge between the remote SPA (untrusted) and
// the main process. Runs with contextIsolation, so we expose a tiny, frozen
// API via contextBridge rather than leaking `ipcRenderer` or Node into the
// page. Two consumers:
//
//   1. window.omnigentDesktop — read by the web app's nativeBridge.ts
//      (badge + notifications). Its `kind: "electron"` field is the
//      feature-detection discriminator.
//   2. window.omnigentSetup — used only by the bundled setup page to
//      persist/read the server URL.
//
// The same preload is attached to both the setup page and the remote SPA;
// each side only touches the bridge it needs.

import { contextBridge, ipcRenderer, webFrame, webUtils } from "electron";
import type {
  BannerSafeUpdateStatus,
  ElectronMainWindowBridges,
  GlobalShortcutSetting,
  UpdateStatus,
} from "./ipc/bridge.ts";
import { createRendererIpc } from "./ipc/renderer.ts";

const ipc = createRendererIpc(ipcRenderer);

// Native window controls stay in window pixels while the page zooms.
if (process.platform === "darwin") {
  const setFullScreenAttribute = (fullScreen: boolean) => {
    document.documentElement.toggleAttribute("data-electron-full-screen", fullScreen === true);
  };
  const syncTitlebarZoom = () => {
    document.documentElement.style.setProperty(
      "--omnigent-electron-zoom",
      String(webFrame.getZoomFactor()),
    );
  };
  window.addEventListener(
    "DOMContentLoaded",
    () => {
      syncTitlebarZoom();
      void ipc
        .invoke("omnigent:is-full-screen")
        .then(setFullScreenAttribute)
        .catch(() => {});
    },
    { once: true },
  );
  window.addEventListener("resize", syncTitlebarZoom);
  ipc.on("omnigent:full-screen-changed", (fullScreen) => {
    setFullScreenAttribute(fullScreen);
  });
}

// Collapse the update states the in-page UpdateBanner renders on
// (available / downloading / downloaded / error-security) to `idle` so the server page can
// never show a banner — that UI is shell-owned (the corner overlay). Kept here
// (not main) so it applies uniformly to getStatus + every onStatus push, and
// so error-security still reaches Settings as idle+lastError (which it shows).
function bannerSafe(status: UpdateStatus): BannerSafeUpdateStatus {
  switch (status?.state) {
    case "available":
    case "downloading":
    case "downloaded":
    case "error-security":
      return status.lastError ? { state: "idle", lastError: status.lastError } : { state: "idle" };
    default:
      return status;
  }
}

// Native integrations for the SPA: a dock/taskbar badge and OS notifications.
// Numbers/strings only so the values survive contextBridge's structured-clone
// boundary.
contextBridge.exposeInMainWorld("omnigentDesktop", {
  kind: "electron",
  /** Paint the dock/taskbar badge; 0 clears it. Fire-and-forget. */
  setBadgeCount: (count) => {
    ipc.send("omnigent:set-badge-count", count);
  },
  /**
   * Fire an OS notification. Resolves true when shown, false otherwise.
   */
  notify: (params) =>
    ipc.invoke("omnigent:notify", {
      title: params?.title,
      body: params?.body,
      navigatePath: params?.navigatePath,
    }),
  /**
   * Subscribe to OS-notification clicks. The main process sends the in-app
   * path the clicked notification carried, which we forward to the SPA so it
   * can route there. Returns an unsubscribe function.
   */
  onNotificationActivated: (callback) =>
    ipc.on("omnigent:notification-activated", (path) => {
      // Defense-in-depth: only forward in-app, same-origin paths. A leading
      // "/" rejects absolute/cross-origin URLs and `javascript:` shapes before
      // the renderer routes on the value, even if main ever sends junk.
      if (typeof path === "string" && path.startsWith("/")) callback(path);
    }),
  /**
   * Subscribe to in-app navigation from the main process. Native menu actions
   * and same-server deep links send a basename-less path so the SPA can route
   * in place without reloading. Returns an unsubscribe function.
   */
  onOpenPath: (callback) =>
    ipc.on("omnigent:open-path", (path) => {
      // Defense-in-depth: only forward in-app, same-origin paths. A leading
      // "/" rejects absolute/cross-origin URLs and `javascript:` shapes before
      // the renderer routes on the value, even if main ever sends junk.
      if (typeof path === "string" && path.startsWith("/")) callback(path);
    }),
  /**
   * Subscribe to the find bar opening (Cmd/Ctrl+F or Edit > Find…), so the
   * page can render content it would otherwise virtualize away. Returns an
   * unsubscribe function.
   */
  onFindOpened: (callback) => ipc.on("omnigent:find-opened", () => callback()),
  /**
   * Subscribe to native menu commands for this window (Close, New Terminal,
   * New Browser Tab, Pin/Archive Session). `browserViewId` names the embedded
   * browser view that held focus when Close fired, else null; `quitDetail` says
   * what Quit stops, for its prompt. Returns an unsubscribe function.
   */
  onDesktopCommand: (callback) =>
    ipc.on("omnigent:desktop-command", (payload) => {
      if (!payload || typeof payload.command !== "string") return;
      callback({
        command: payload.command,
        browserViewId: typeof payload.browserViewId === "string" ? payload.browserViewId : null,
        closingQuits: payload.closingQuits === true,
        quitDetail: typeof payload.quitDetail === "string" ? payload.quitDetail : null,
      });
    }),
  /**
   * Report which menu commands this page can act on right now, plus the open
   * session's pin/archive state, so the native menu can enable and label them.
   * Fire-and-forget.
   */
  setDesktopCommandState: (state) => {
    ipc.send("omnigent:set-desktop-command-state", {
      commands: Array.isArray(state?.commands) ? state.commands.map(String) : [],
      sessionPinned: state?.sessionPinned === true,
      sessionArchived: state?.sessionArchived === true,
    });
  },
  /** Close this window once the page has confirmed. Resolves whether it closed. */
  closeWindow: () => ipc.invoke("omnigent:close-window"),
  /** Quit once the page's prompt has confirmed; `dontAskAgain` turns that prompt off. */
  confirmQuit: (options) =>
    ipc.invoke("omnigent:confirm-quit", { dontAskAgain: options?.dontAskAgain === true }),
  // macOS ships system Dictation; elsewhere the composer mic needs server dictation.
  ...(process.platform === "darwin"
    ? {
        /** Start macOS Dictation in the focused field. Resolves whether it started. */
        startSystemDictation: () => ipc.invoke("omnigent:start-system-dictation"),
      }
    : {}),
  /** The "Show Omnigent from anywhere" shortcut (off by default). */
  getGlobalShortcut: () => ipc.invoke("omnigent:get-global-shortcut"),
  /**
   * Saves `{ enabled, accelerator }`, or turns the shortcut off for `{ enabled: false }`,
   * keeping the saved chord. Resolves `{ ok, setting?, error? }`; `ok: false` means the
   * saved setting is unchanged.
   */
  setGlobalShortcut: ({ enabled, accelerator }: Partial<GlobalShortcutSetting> = {}) =>
    ipc.invoke(
      "omnigent:set-global-shortcut",
      enabled !== true && accelerator === undefined
        ? { enabled: false }
        : { enabled: enabled === true, accelerator: String(accelerator ?? "") },
    ),
  /**
   * Takes the next chord in the shell, before the page's shortcuts and the menu see it.
   * Resolves its accelerator, or null when Escape or the cancel call ends recording.
   */
  recordGlobalShortcut: () => ipc.invoke("omnigent:record-global-shortcut"),
  cancelGlobalShortcutRecording: () => ipc.invoke("omnigent:cancel-global-shortcut-recording"),
  /** The runner picked during onboarding for this server ("local" |
   *  "remote"), returned once, else null. */
  takeOnboardingRunner: () => ipc.invoke("omnigent:take-onboarding-runner"),
  /**
   * Server picker data: the current origin plus organization-provided and
   * recently-connected server URLs. Resolves null off a connected server.
   */
  getServerPicker: () => ipc.invoke("omnigent:get-server-picker"),
  /**
   * Re-point this window to a URL returned by getServerPicker (anything else
   * rejects in the main process).
   */
  switchServer: (url) => ipc.invoke("omnigent:switch-server", url),
  /** Sign this window's server out; every window on it returns to the setup page. */
  signOutOfServer: () => ipc.invoke("omnigent:sign-out-of-server"),
  /** Return this window to the bundled "connect to server" setup page. */
  openServerSetup: () => {
    ipc.send("omnigent:open-server-setup");
  },
  /** Reveal one of this machine's files in the OS file manager. */
  revealFile: (hostId, path) => ipc.invoke("omnigent:reveal-file", hostId, path),
  /** OS folder chooser on this machine; resolves the chosen path, or null on cancel. */
  chooseDirectory: (defaultPath) => ipc.invoke("omnigent:choose-directory", defaultPath),
  /**
   * This machine's path for a File dropped from the OS file manager. Resolves
   * "" when it has none or the page isn't the pinned server.
   */
  getPathForFile: (file) => ipc.invoke("omnigent:dropped-file-path", webUtils.getPathForFile(file)),
  /**
   * This machine's identity — `{ cliInstalled, hostId }` — read from local
   * config with no subprocess, so it's instant. Lets the SPA recognize "this
   * machine" in the server's host list.
   */
  getHostIdentity: () => ipc.invoke("omnigent:host-get-identity"),
  /**
   * Start / stop / restart this machine's host daemon for the window's server.
   * Resolves a `{ ok, error? }` result.
   */
  controlHost: (action) => ipc.invoke("omnigent:host-control", action),
  /**
   * Desktop feature gates the server can't know about: currently
   * `{ databricksInternalFeatures }` from macOS Managed Preferences, scoped
   * to the window's server (true only on managed servers).
   * Resolves null off a connected server.
   */
  getDesktopFeatures: () => ipc.invoke("omnigent:get-desktop-features"),
  /**
   * Connect the user's Arca instance to the window's
   * server as a host, via `arca ssh`. Native consent is asked in the main
   * process. Resolves a `{ ok, error?, authError? }` result.
   */
  connectArcaHost: () => ipc.invoke("omnigent:arca-connect"),
  // Windows' built-in ssh can't share one connection, so it gets this later.
  ...(process.platform === "win32"
    ? {}
    : {
        /**
         * Connect a machine the user reaches over SSH to the window's server as
         * a host. Consent and ssh prompts happen in a shell-owned window.
         * Resolves `{ ok, error?, canceled?, identity? }`.
         */
        connectSshHost: (destination) => ipc.invoke("omnigent:ssh-connect", destination),
      }),
  /** Extend the local Arca instance with one of the main process's approved modes. */
  extendArca: (mode) => ipc.invoke("omnigent:arca-extend", mode),
  /**
   * Renew this machine's dbcert. Resolves a `{ ok, error? }` result.
   */
  refreshDbcert: (options) => ipc.invoke("omnigent:dbcert-refresh", options),
  /** Check this machine's dbcert without starting sign-in. */
  checkDbcert: () => ipc.invoke("omnigent:dbcert-check"),

  /**
   * Subscribe to host status-change pings. Fired only on real events (a host
   * child connecting/exiting, or a control action) — never on a timer — so the
   * renderer re-reads what it needs on demand. The callback takes no argument.
   * Returns an unsubscribe function.
   */
  onHostStatusChanged: (callback) => ipc.on("omnigent:host-status-changed", () => callback()),
  /**
   * The local `omni` CLI status — `{ installed, path, version, source,
   * installCommand }`. Read-only; lets the in-app Local CLI settings show which
   * binary is in use.
   */
  getCliStatus: () => ipc.invoke("omnigent:cli-get-status"),
  /**
   * Clear the saved CLI-path override (revert to auto-detection). The SPA can
   * reset but cannot SET a path: choosing a binary is restricted to the trusted
   * setup page, so a connected server can't repoint the CLI at an arbitrary one.
   */
  resetCliPath: () => ipc.invoke("omnigent:cli-reset-path"),
  // Update bridge for the server page — CONFIG ONLY, by design. Desktop update
  // NOTIFICATIONS are owned by the shell now (a native corner overlay with its
  // own preload + the Server menu). This bridge stays so Settings can still
  // read/write update preferences (mode, auto-install) and trigger a check, but
  // it is BANNER-SAFE: `bannerSafe()` collapses the states the in-page
  // UpdateBanner renders on (available / downloading / downloaded /
  // error-security) down to
  // `idle` before the page sees them. That means NO web bundle — including
  // older shipped ones that still mount the in-page banner — can show a
  // (duplicate) banner, while Settings still gets check progress and errors
  // (error-security is forwarded as idle+lastError, which Settings surfaces).
  updates: {
    getConfig: () => ipc.invoke("omnigent:get-update-config"),
    getStatus: () => ipc.invoke("omnigent:get-update-status").then(bannerSafe),
    check: () => ipc.invoke("omnigent:update-check"),
    download: () => ipc.invoke("omnigent:update-download"),
    installNow: () => ipc.invoke("omnigent:update-install"),
    setConfig: (patch) => ipc.invoke("omnigent:set-update-config", patch),
    onStatus: (callback) =>
      ipc.on("omnigent:update-status", (status) => callback(bannerSafe(status))),
    /** Current shell-owned update-overlay card height in CSS pixels. */
    getOverlayHeight: () => ipc.invoke("omnigent:get-update-overlay-height"),
    /** Subscribe to overlay height changes; returns an unsubscribe function. */
    onOverlayHeight: (callback) =>
      ipc.on("omnigent:update-overlay-height", (height) => {
        const normalized = Math.max(0, Math.round(Number(height) || 0));
        callback(normalized);
      }),
  },
  /**
   * Report the web app's resolved color scheme so the shell can mirror it via
   * `nativeTheme.themeSource` — keeping the update overlay, native dialogs, and
   * menus in sync with the in-app theme switcher (not just the OS setting).
   */
  setColorScheme: (scheme) => ipc.send("omnigent:set-color-scheme", scheme),

  // ── Embedded browser pane ──────────────────────────────────────────────
  // The relay hook (web/src/hooks/useBrowserAgentRelay.ts) drives a native
  // WebContentsView per conversation through these; all args/results are
  // structured-clone-safe. SECURITY: no generic agent `evaluate` is exposed —
  // `browserPerformAction` accepts fixed actions only (see README).

  /**
   * Open (create-if-absent) or navigate a conversation's view. `opts.force`
   * reloads on same URL; `opts.agent` marks model-issued nav, which the main
   * process gates behind a scheme/host allowlist (URL-bar nav omits it).
   */
  browserOpenOrNavigate: (conversationId, url, bounds, opts) =>
    ipc.invoke("omnigent:browser-open-or-navigate", { conversationId, url, bounds, opts }),
  /**
   * Attach a conversation's view to the host window (detaching the previous
   * active one). Pass null to detach everything (no pane mounted).
   */
  browserSetActive: (conversationId, options) =>
    ipc.invoke("omnigent:browser-set-active", {
      conversationId,
      focus: options?.focus === true,
    }),
  /** The attached browser page's view id if it has keyboard focus, else null. */
  browserFocusedView: () => ipc.invoke("omnigent:browser-focused-view"),
  /**
   * Hide (true) or show (false) the active browser view while a DOM overlay is
   * open, so the native layer doesn't cover dialogs/menus/tooltips/toasts.
   */
  browserSetSuppressed: (suppressed) =>
    ipc.invoke("omnigent:browser-set-suppressed", { suppressed }),
  /**
   * Reposition the conversation's view to freshly-measured placeholder bounds.
   */
  browserResize: (conversationId, bounds) =>
    ipc.invoke("omnigent:browser-resize", { conversationId, bounds }),
  /**
   * Capture the conversation's view as a base64 PNG data URL.
   */
  browserScreenshot: (conversationId, opts) =>
    ipc.invoke("omnigent:browser-screenshot", { conversationId, opts }),
  /** Perform a fixed snapshot, click or type action in the conversation's view. */
  browserPerformAction: (conversationId, action, params, opts) =>
    ipc.invoke("omnigent:browser-perform-action", { conversationId, action, params, opts }),
  /** @deprecated Until v0.21: main translates only recognized v0.18 action templates. */
  browserExecute: (conversationId, js, opts) =>
    ipc.invoke("omnigent:browser-execute", { conversationId, js, opts }),
  /**
   * Destroy the conversation's view (explicit close — unmount only detaches).
   */
  browserClose: (conversationId, reason) =>
    ipc.invoke("omnigent:browser-close", { conversationId, reason }),
  /**
   * Subscribe to which conversation's browser view is currently attached to the
   * host window (`{conversationId}` or `{conversationId: null}` when detached).
   * The registry fires this on every attach/detach. Returns an unsubscribe.
   */
  onBrowserHostActiveChanged: (callback) =>
    ipc.on("browser-host-active-changed", (payload) => callback(payload)),
  /**
   * Forward Ctrl+Tab, Control release, and Escape from the focused embedded
   * Browser WebContents so the shell's recent-session switcher can own them.
   */
  onBrowserRecentSessionInput: (callback) =>
    ipc.on("browser-recent-session-input", (payload) => callback(payload)),
  /** Enable native Ctrl+Tab forwarding only while this renderer supports it. */
  browserSetRecentSessionSwitchSupported: (supported) =>
    ipc.invoke("omnigent:browser-set-recent-session-switch-supported", { supported }),
  /**
   * Subscribe to page links Cmd/Ctrl- or middle-clicked in an embedded browser
   * view (`{conversationId, url}`, where conversationId is the clicked view's id).
   */
  onBrowserOpenTab: (callback) => ipc.on("browser-open-tab", (payload) => callback(payload)),
  /** Route modifier-clicked page links to `onBrowserOpenTab` while supported. */
  browserSetOpenTabSupported: (supported) =>
    ipc.invoke("omnigent:browser-set-open-tab-supported", { supported }),
  /** Clear the native Ctrl+Tab latch when the renderer has no sessions to show. */
  browserCancelRecentSessionSwitch: () =>
    ipc.invoke("omnigent:browser-cancel-recent-session-switch"),
  /**
   * Subscribe to browser-view creation (`{conversationId}`), fired the first
   * time a view is created — including detached (fresh conversation), which is
   * how the SPA learns to mount+attach it. Returns an unsubscribe.
   */
  onBrowserViewCreated: (callback) =>
    ipc.on("browser-view-created", (payload) => callback(payload)),
  /**
   * Whether a view already exists for a conversation — lets a re-mounting pane
   * re-attach it without waiting for a fresh create event.
   */
  browserHasView: (conversationId) => ipc.invoke("omnigent:browser-has-view", { conversationId }),
  /**
   * Subscribe to browser-view close events (`{conversationId, reason}`) so the
   * SPA can drop the pane when the view is destroyed. Returns an unsubscribe.
   */
  onBrowserViewClosed: (callback) => ipc.on("browser-view-closed", (payload) => callback(payload)),

  // ── Toolbar: history navigation + reload ───────────────────────────────
  // Drive the conversation's view through its own history. Each resolves
  // `{ ok, canGoBack?, canGoForward?, error? }` so the toolbar can refresh its
  // disabled-button state without waiting for the browser-nav-state event.

  /**
   * Navigate the conversation's view back one history entry (no-op if it can't).
   */
  browserGoBack: (conversationId) => ipc.invoke("omnigent:browser-go-back", { conversationId }),
  /**
   * Navigate the conversation's view forward one history entry.
   */
  browserGoForward: (conversationId) =>
    ipc.invoke("omnigent:browser-go-forward", { conversationId }),
  /**
   * Reload the conversation's view.
   */
  browserReload: (conversationId) => ipc.invoke("omnigent:browser-reload", { conversationId }),
  /**
   * Toggle Chrome DevTools (docked bottom) for the conversation's view.
   */
  openBrowserDevTools: (conversationId) =>
    ipc.invoke("omnigent:open-browser-devtools", { conversationId }),
  /**
   * Inspect the user's local Google Chrome profiles for cookies in the site
   * tree the conversation's view is currently on. Metadata only (profile name +
   * per-scope counts): no Keychain access and no cookie values. macOS only.
   */
  browserChromeCookiePreview: (conversationId) =>
    ipc.invoke("omnigent:browser-chrome-cookie-preview", { conversationId }),
  /**
   * Copy ONLY that site's cookies out of one Chrome profile into the
   * conversation's own (in-memory) session. Decrypts via the Keychain, so the
   * first import shows a system approval prompt. `expectedHost` must still match
   * the view's site.
   * `scope` is the site tree to copy (the page's host or a parent domain); main
   * rejects anything that isn't an ancestor of the page.
   */
  browserChromeCookieImport: (conversationId, args) =>
    ipc.invoke("omnigent:browser-chrome-cookie-import", {
      conversationId,
      profileId: args?.profileId,
      expectedHost: args?.expectedHost,
      scope: args?.scope,
    }),
  /**
   * Subscribe to URL and title changes, including background views, so the
   * address bar and tab labels stay current. Returns an unsubscribe.
   */
  onBrowserUrlChanged: (callback) => ipc.on("browser-url-changed", (payload) => callback(payload)),
  /**
   * Subscribe to back/forward availability for a conversation's view, pushed
   * whenever it navigates. Lets the toolbar enable/disable the arrows.
   */
  onBrowserNavState: (callback) => ipc.on("browser-nav-state", (payload) => callback(payload)),

  // ── Design mode (point-and-prompt) ─────────────────────────────────────
  // Toggle the in-page picker + popup. On Send, the SPA gets the submit via
  // onBrowserElementPromptSubmit, routes it as a normal chat message (no backend
  // route), then calls browserSignalDesignResult to paint the popup. Electron-only.

  /**
   * Inject the design-mode picker into the conversation's view.
   */
  browserEnableDesignMode: (conversationId) =>
    ipc.invoke("omnigent:browser-enable-design-mode", { conversationId }),
  /**
   * Tear the design-mode picker back down.
   */
  browserDisableDesignMode: (conversationId) =>
    ipc.invoke("omnigent:browser-disable-design-mode", { conversationId }),
  /**
   * Signal a submit's success/failure back into the in-page popup so it shows
   * green/red feedback. `id` must match the submitId the popup emitted.
   */
  browserSignalDesignResult: (conversationId, result) =>
    ipc.invoke("omnigent:browser-signal-design-result", {
      conversationId,
      id: result?.id,
      ok: result?.ok,
      message: result?.message,
    }),
  /**
   * Subscribe to element-selection events. Payload: element info + a cropped
   * base64 screenshot (`{conversationId, ...info, screenshot}`). Returns an unsubscribe.
   */
  onBrowserElementSelected: (callback) =>
    ipc.on("browser-element-selected", (payload) => callback(payload)),
  /**
   * Subscribe to element-prompt submits (Send/Enter). Payload:
   * `{conversationId, id, element, prompt}`. Returns an unsubscribe.
   */
  onBrowserElementPromptSubmit: (callback) =>
    ipc.on("browser-element-prompt-submit", (payload) => callback(payload)),
  /**
   * Subscribe to element-prompt dismissals (user pressed × / Escape).
   * Payload: `{conversationId}`. Returns an unsubscribe.
   */
  onBrowserElementPromptDismiss: (callback) =>
    ipc.on("browser-element-prompt-dismiss", (payload) => callback(payload)),
} satisfies ElectronMainWindowBridges["omnigentDesktop"]);

// Setup-page bridge: persist + navigate to a server URL, and read the saved
// one to pre-fill the form. Separate object so the SPA never sees it.
contextBridge.exposeInMainWorld("omnigentSetup", {
  getServerUrl: () => ipc.invoke("omnigent:get-server-url"),
  /**
   * Persist + navigate to a server URL. Connecting this machine as a runner is
   * a separate, explicit action from the host menu — not a connect-time choice.
   */
  setServerUrl: (url, opts) => ipc.invoke("omnigent:set-server-url", url, opts),
  /** Cancel only this setup window's matching connection attempt. */
  cancelServerConnection: (requestId) => ipc.invoke("omnigent:cancel-server-connection", requestId),
  /** Subscribe to connecting/authenticating phases for a request ID. */
  onConnectionProgress: (callback) =>
    ipc.on("omnigent:connection-progress", (progress) => callback(progress)),
  /** Organization-provided server URLs from macOS Managed Preferences. */
  getManagedServers: () => ipc.invoke("omnigent:get-managed-servers"),
  /** Display names for those servers, server URL → name. */
  getManagedServerNames: () => ipc.invoke("omnigent:get-managed-server-names"),
  /** Names servers gave themselves in their manifest, origin → name (display only). */
  getServerNames: () => ipc.invoke("omnigent:get-server-names"),
  /** Wizard capabilities, e.g. `{v2Forced}` — v2Forced disables "Switch to
   *  legacy" because the env var pins the selector on. */
  getSetupCapabilities: () => ipc.invoke("omnigent:get-setup-capabilities"),
  /** Runners the onboarding runner step offers for `url`: `{remote, bundledCli}`. */
  getRunnerOptions: (url) => ipc.invoke("omnigent:get-runner-options", url),
  /** Connect the onboarding runner ("local" | "remote") to `url`. Resolves
   *  `{ok, error?}`; output streams via onRunnerConnectLog. */
  connectRunner: (url, runner) => ipc.invoke("omnigent:connect-runner", url, runner),
  /** Subscribe to connectRunner output. Returns an unsubscribe function. */
  onRunnerConnectLog: (callback) =>
    ipc.on("omnigent:runner-connect-log", (payload) => callback(payload?.line ?? "")),
  /** Live color-scheme override for the wizard (System/Light/Dark). Not
   *  persisted — resets to the OS default on relaunch. */
  setColorScheme: (scheme) => ipc.send("omnigent:setup-set-color-scheme", scheme),
  /** Current color scheme: `{source, effective}` — the persisted-for-the-session
   *  source (system/light/dark) and the resolved appearance. Seeds the wizard's
   *  radio + `.dark` class on load (themeSource may hold a value set earlier). */
  getColorScheme: () => ipc.invoke("omnigent:setup-get-color-scheme"),
  /** Subscribe to the wizard's effective theme ("dark"/"light") so the renderer
   *  can sync its `.dark` class; fires on set and on OS changes. */
  onColorScheme: (callback) => ipc.on("omnigent:setup-theme", (theme) => callback(theme)),
  /** Recently-connected server URLs, most recent first. */
  getRecentServers: () => ipc.invoke("omnigent:get-recent-servers"),
  /** Drop one recent server from the saved list; resolves the remaining ones. */
  forgetRecentServer: (url) => ipc.invoke("omnigent:forget-recent-server", url),
  /**
   * Advisory reachability probe for a server URL; resolves
   * `{status: "ok" | "reachable" | "unreachable"}`. Never gates connecting.
   */
  checkServer: (url) => ipc.invoke("omnigent:check-server", url),
  /** Copy text from the bundled setup page to the native clipboard. */
  copyText: (text) => ipc.invoke("omnigent:copy-setup-text", text),
  /**
   * Toggle the revamped server selector and reload this window to the chosen
   * setup page. `true` → new experience, `false` → classic. No-op if the env
   * var forces the choice.
   */
  setServerSelectorV2: (enabled) => ipc.invoke("omnigent:set-server-selector-v2", enabled),
  /**
   * Whether the `omnigent` CLI is installed/runnable, e.g.
   * `{installed, path, version, source, installCommand}`.
   */
  getCliStatus: () => ipc.invoke("omnigent:get-cli-status"),
  /**
   * Set an explicit path to the omnigent binary. Resolves the CLI status plus
   * `accepted` (whether that exact path validated and was saved).
   */
  setCliPath: (path) => ipc.invoke("omnigent:set-cli-path", path),
  /** Native file picker for the omnigent binary; resolves the path or null. */
  browseCliPath: () => ipc.invoke("omnigent:browse-cli-path"),
  /**
   * Start (or reuse) the local server. Resolves `{ok, url?, error?}`; the
   * caller then connects to `url` via setServerUrl.
   */
  startLocalServer: () => ipc.invoke("omnigent:start-local-server"),
  /**
   * Subscribe to the local server's startup log lines (streamed while it boots
   * during startLocalServer). Returns an unsubscribe function. Absent on older
   * shells → the setup page falls back to phase-only display.
   */
  onLocalServerSetupLog: (callback) =>
    ipc.on("omnigent:local-server-setup-log", (payload) => callback(payload?.line ?? "")),
  /**
   * Install the omnigent CLI (macOS). Resolves `{ok, error?, installed}` once
   * the installer finishes; output streams via onCliInstallLog.
   */
  installCli: () => ipc.invoke("omnigent:cli-install"),
  /**
   * Subscribe to the CLI installer's output lines. Returns an unsubscribe fn.
   */
  onCliInstallLog: (callback) =>
    ipc.on("omnigent:cli-install-log", (payload) => callback(payload?.line ?? "")),
} satisfies ElectronMainWindowBridges["omnigentSetup"]);
