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

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

// Collapse the update states the in-page UpdateBanner renders on
// (available / downloading / downloaded / error-security) to `idle` so the server page can
// never show a banner — that UI is shell-owned (the corner overlay). Kept here
// (not main) so it applies uniformly to getStatus + every onStatus push, and
// so error-security still reaches Settings as idle+lastError (which it shows).
function bannerSafe(status) {
  if (
    status &&
    (status.state === "available" ||
      status.state === "downloading" ||
      status.state === "downloaded" ||
      status.state === "error-security")
  ) {
    return status.lastError ? { state: "idle", lastError: status.lastError } : { state: "idle" };
  }
  return status;
}

// Native integrations for the SPA: a dock/taskbar badge and OS notifications.
// Numbers/strings only so the values survive contextBridge's structured-clone
// boundary.
contextBridge.exposeInMainWorld("omnigentDesktop", {
  kind: "electron",
  /** Paint the dock/taskbar badge; 0 clears it. Fire-and-forget. */
  setBadgeCount: (count) => {
    ipcRenderer.send("omnigent:set-badge-count", count);
  },
  /**
   * Fire an OS notification. Resolves true when shown, false otherwise.
   * @param {{title: string, body?: string, navigatePath?: string}} params
   */
  notify: (params) =>
    ipcRenderer.invoke("omnigent:notify", {
      title: params?.title,
      body: params?.body,
      navigatePath: params?.navigatePath,
    }),
  /**
   * Subscribe to OS-notification clicks. The main process sends the in-app
   * path the clicked notification carried, which we forward to the SPA so it
   * can route there. Returns an unsubscribe function.
   * @param {(path: string) => void} callback
   * @returns {() => void}
   */
  onNotificationActivated: (callback) => {
    const listener = (_event, path) => {
      // Defense-in-depth: only forward in-app, same-origin paths. A leading
      // "/" rejects absolute/cross-origin URLs and `javascript:` shapes before
      // the renderer routes on the value, even if main ever sends junk.
      if (typeof path === "string" && path.startsWith("/")) callback(path);
    };
    ipcRenderer.on("omnigent:notification-activated", listener);
    return () => ipcRenderer.removeListener("omnigent:notification-activated", listener);
  },
  /**
   * Subscribe to in-app navigation from the main process. Native menu actions
   * and same-server deep links send a basename-less path so the SPA can route
   * in place without reloading. Returns an unsubscribe function.
   * @param {(path: string) => void} callback
   * @returns {() => void}
   */
  onOpenPath: (callback) => {
    const listener = (_event, path) => {
      // Defense-in-depth: only forward in-app, same-origin paths. A leading
      // "/" rejects absolute/cross-origin URLs and `javascript:` shapes before
      // the renderer routes on the value, even if main ever sends junk.
      if (typeof path === "string" && path.startsWith("/")) callback(path);
    };
    ipcRenderer.on("omnigent:open-path", listener);
    return () => ipcRenderer.removeListener("omnigent:open-path", listener);
  },
  /**
   * Server picker data: the current origin plus organization-provided and
   * recently-connected server URLs. Resolves null off a connected server.
   */
  getServerPicker: () => ipcRenderer.invoke("omnigent:get-server-picker"),
  /**
   * Re-point this window to a URL returned by getServerPicker (anything else
   * rejects in the main process).
   */
  switchServer: (url) => ipcRenderer.invoke("omnigent:switch-server", url),
  /** Return this window to the bundled "connect to server" setup page. */
  openServerSetup: () => {
    ipcRenderer.send("omnigent:open-server-setup");
  },
  /**
   * This machine's identity — `{ cliInstalled, hostId }` — read from local
   * config with no subprocess, so it's instant. Lets the SPA recognize "this
   * machine" in the server's host list.
   */
  getHostIdentity: () => ipcRenderer.invoke("omnigent:host-get-identity"),
  /**
   * Start / stop / restart this machine's host daemon for the window's server.
   * Resolves a `{ ok, error? }` result.
   * @param {"start" | "stop" | "restart"} action
   */
  controlHost: (action) => ipcRenderer.invoke("omnigent:host-control", action),
  /**
   * Desktop feature gates the server can't know about — currently
   * `{ databricksInternalFeatures }` from macOS Managed Preferences, scoped
   * to the window's server (true only on Databricks-managed servers).
   * Resolves null off a connected server.
   */
  getDesktopFeatures: () => ipcRenderer.invoke("omnigent:get-desktop-features"),
  /**
   * Connect the user's Arca instance (Databricks-internal) to the window's
   * server as a host, via `arca ssh`. Native consent is asked in the main
   * process. Resolves a `{ ok, error?, authError? }` result.
   */
  connectArcaHost: () => ipcRenderer.invoke("omnigent:arca-connect"),

  /**
   * Subscribe to host status-change pings. Fired only on real events (a host
   * child connecting/exiting, or a control action) — never on a timer — so the
   * renderer re-reads what it needs on demand. The callback takes no argument.
   * Returns an unsubscribe function.
   * @param {() => void} callback
   * @returns {() => void}
   */
  onHostStatusChanged: (callback) => {
    const listener = () => callback();
    ipcRenderer.on("omnigent:host-status-changed", listener);
    return () => ipcRenderer.removeListener("omnigent:host-status-changed", listener);
  },
  /**
   * The local `omni` CLI status — `{ installed, path, version, source,
   * installCommand }`. Read-only; lets the in-app Local CLI settings show which
   * binary is in use.
   */
  getCliStatus: () => ipcRenderer.invoke("omnigent:cli-get-status"),
  /**
   * Clear the saved CLI-path override (revert to auto-detection). The SPA can
   * reset but cannot SET a path: choosing a binary is restricted to the trusted
   * setup page, so a connected server can't repoint the CLI at an arbitrary one.
   */
  resetCliPath: () => ipcRenderer.invoke("omnigent:cli-reset-path"),
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
    getConfig: () => ipcRenderer.invoke("omnigent:get-update-config"),
    getStatus: () => ipcRenderer.invoke("omnigent:get-update-status").then(bannerSafe),
    check: () => ipcRenderer.invoke("omnigent:update-check"),
    download: () => ipcRenderer.invoke("omnigent:update-download"),
    installNow: () => ipcRenderer.invoke("omnigent:update-install"),
    setConfig: (patch) => ipcRenderer.invoke("omnigent:set-update-config", patch),
    onStatus: (callback) => {
      const listener = (_event, status) => callback(bannerSafe(status));
      ipcRenderer.on("omnigent:update-status", listener);
      return () => ipcRenderer.removeListener("omnigent:update-status", listener);
    },
    /** Current shell-owned update-overlay card height in CSS pixels. */
    getOverlayHeight: () => ipcRenderer.invoke("omnigent:get-update-overlay-height"),
    /** Subscribe to overlay height changes; returns an unsubscribe function. */
    onOverlayHeight: (callback) => {
      const listener = (_event, height) => {
        const normalized = Math.max(0, Math.round(Number(height) || 0));
        callback(normalized);
      };
      ipcRenderer.on("omnigent:update-overlay-height", listener);
      return () => ipcRenderer.removeListener("omnigent:update-overlay-height", listener);
    },
  },
  /**
   * Report the web app's resolved color scheme so the shell can mirror it via
   * `nativeTheme.themeSource` — keeping the update overlay, native dialogs, and
   * menus in sync with the in-app theme switcher (not just the OS setting).
   * @param {"light" | "dark" | "system"} scheme
   */
  setColorScheme: (scheme) => ipcRenderer.send("omnigent:set-color-scheme", scheme),

  // ── Embedded browser pane ──────────────────────────────────────────────
  // The relay hook (web/src/hooks/useBrowserAgentRelay.ts) drives a native
  // WebContentsView per conversation through these; all args/results are
  // structured-clone-safe. SECURITY: no generic agent `evaluate` is exposed —
  // `browserExecute` runs relay templates only (see README).

  /**
   * Open (create-if-absent) or navigate a conversation's view. `opts.force`
   * reloads on same URL; `opts.agent` marks model-issued nav, which the main
   * process gates behind a scheme/host allowlist (URL-bar nav omits it).
   * @param {string} conversationId
   * @param {string} url
   * @param {{x:number,y:number,width:number,height:number,devicePixelRatio?:number}} [bounds]
   * @param {{force?: boolean, agent?: boolean}} [opts]
   */
  browserOpenOrNavigate: (conversationId, url, bounds, opts) =>
    ipcRenderer.invoke("omnigent:browser-open-or-navigate", { conversationId, url, bounds, opts }),
  /**
   * Attach a conversation's view to the host window (detaching the previous
   * active one). Pass null to detach everything (no pane mounted).
   * @param {string | null} conversationId
   */
  browserSetActive: (conversationId) =>
    ipcRenderer.invoke("omnigent:browser-set-active", { conversationId }),
  /**
   * Hide (true) or show (false) the active browser view while a DOM overlay is
   * open, so the native layer doesn't cover dialogs/menus/tooltips/toasts.
   * @param {boolean} suppressed
   */
  browserSetSuppressed: (suppressed) =>
    ipcRenderer.invoke("omnigent:browser-set-suppressed", { suppressed }),
  /**
   * Reposition the conversation's view to freshly-measured placeholder bounds.
   * @param {string} conversationId
   * @param {{x:number,y:number,width:number,height:number,devicePixelRatio?:number}} bounds
   */
  browserResize: (conversationId, bounds) =>
    ipcRenderer.invoke("omnigent:browser-resize", { conversationId, bounds }),
  /**
   * Capture the conversation's view as a base64 PNG data URL.
   * @param {string} conversationId
   * @returns {Promise<{ ok: boolean, dataUrl?: string, error?: string }>}
   */
  browserScreenshot: (conversationId) =>
    ipcRenderer.invoke("omnigent:browser-screenshot", { conversationId }),
  /**
   * Run a relay-template JS string in the conversation's view. PRIVATE to the
   * relay's fixed templates (snapshot / click / type) — never an agent-facing
   * generic evaluate.
   * @param {string} conversationId
   * @param {string} js
   * @returns {Promise<{ ok: boolean, result?: string, error?: string }>}
   */
  browserExecute: (conversationId, js) =>
    ipcRenderer.invoke("omnigent:browser-execute", { conversationId, js }),
  /**
   * Destroy the conversation's view (explicit close — unmount only detaches).
   * @param {string} conversationId
   * @param {string} [reason]
   */
  browserClose: (conversationId, reason) =>
    ipcRenderer.invoke("omnigent:browser-close", { conversationId, reason }),
  /**
   * Subscribe to which conversation's browser view is currently attached to the
   * host window (`{conversationId}` or `{conversationId: null}` when detached).
   * The registry fires this on every attach/detach. Returns an unsubscribe.
   * @param {(payload: { conversationId: string | null }) => void} callback
   * @returns {() => void}
   */
  onBrowserHostActiveChanged: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("browser-host-active-changed", listener);
    return () => ipcRenderer.removeListener("browser-host-active-changed", listener);
  },
  /**
   * Subscribe to browser-view creation (`{conversationId}`), fired the first
   * time a view is created — including detached (fresh conversation), which is
   * how the SPA learns to mount+attach it. Returns an unsubscribe.
   * @param {(payload: { conversationId: string }) => void} callback
   * @returns {() => void}
   */
  onBrowserViewCreated: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("browser-view-created", listener);
    return () => ipcRenderer.removeListener("browser-view-created", listener);
  },
  /**
   * Whether a view already exists for a conversation — lets a re-mounting pane
   * re-attach it without waiting for a fresh create event.
   * @param {string} conversationId
   * @returns {Promise<{ exists: boolean }>}
   */
  browserHasView: (conversationId) =>
    ipcRenderer.invoke("omnigent:browser-has-view", { conversationId }),
  /**
   * Subscribe to browser-view close events (`{conversationId, reason}`) so the
   * SPA can drop the pane when the view is destroyed. Returns an unsubscribe.
   * @param {(payload: { conversationId: string, reason: string | null }) => void} callback
   * @returns {() => void}
   */
  onBrowserViewClosed: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("browser-view-closed", listener);
    return () => ipcRenderer.removeListener("browser-view-closed", listener);
  },

  // ── Toolbar: history navigation + reload ───────────────────────────────
  // Drive the conversation's view through its own history. Each resolves
  // `{ ok, canGoBack?, canGoForward?, error? }` so the toolbar can refresh its
  // disabled-button state without waiting for the browser-nav-state event.

  /**
   * Navigate the conversation's view back one history entry (no-op if it can't).
   * @param {string} conversationId
   * @returns {Promise<{ ok: boolean, canGoBack?: boolean, canGoForward?: boolean, error?: string }>}
   */
  browserGoBack: (conversationId) =>
    ipcRenderer.invoke("omnigent:browser-go-back", { conversationId }),
  /**
   * Navigate the conversation's view forward one history entry.
   * @param {string} conversationId
   * @returns {Promise<{ ok: boolean, canGoBack?: boolean, canGoForward?: boolean, error?: string }>}
   */
  browserGoForward: (conversationId) =>
    ipcRenderer.invoke("omnigent:browser-go-forward", { conversationId }),
  /**
   * Reload the conversation's view.
   * @param {string} conversationId
   * @returns {Promise<{ ok: boolean, error?: string }>}
   */
  browserReload: (conversationId) =>
    ipcRenderer.invoke("omnigent:browser-reload", { conversationId }),
  /**
   * Toggle Chrome DevTools (docked bottom) for the conversation's view.
   * @param {string} conversationId
   * @returns {Promise<{ ok: boolean, error?: string }>}
   */
  openBrowserDevTools: (conversationId) =>
    ipcRenderer.invoke("omnigent:open-browser-devtools", { conversationId }),
  /**
   * Subscribe to the real url of a view as it navigates (redirects, links,
   * back/forward) so the URL bar stays honest. Returns an unsubscribe.
   * @param {(payload: { conversationId: string, url: string }) => void} callback
   * @returns {() => void}
   */
  onBrowserUrlChanged: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("browser-url-changed", listener);
    return () => ipcRenderer.removeListener("browser-url-changed", listener);
  },
  /**
   * Subscribe to back/forward availability for a conversation's view, pushed
   * whenever it navigates. Lets the toolbar enable/disable the arrows.
   * @param {(payload: { conversationId: string, canGoBack: boolean, canGoForward: boolean }) => void} callback
   * @returns {() => void}
   */
  onBrowserNavState: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("browser-nav-state", listener);
    return () => ipcRenderer.removeListener("browser-nav-state", listener);
  },

  // ── Design mode (point-and-prompt) ─────────────────────────────────────
  // Toggle the in-page picker + popup. On Send, the SPA gets the submit via
  // onBrowserElementPromptSubmit, routes it as a normal chat message (no backend
  // route), then calls browserSignalDesignResult to paint the popup. Electron-only.

  /**
   * Inject the design-mode picker into the conversation's view.
   * @param {string} conversationId
   * @returns {Promise<{ ok: boolean, error?: string }>}
   */
  browserEnableDesignMode: (conversationId) =>
    ipcRenderer.invoke("omnigent:browser-enable-design-mode", { conversationId }),
  /**
   * Tear the design-mode picker back down.
   * @param {string} conversationId
   * @returns {Promise<{ ok: boolean, error?: string }>}
   */
  browserDisableDesignMode: (conversationId) =>
    ipcRenderer.invoke("omnigent:browser-disable-design-mode", { conversationId }),
  /**
   * Signal a submit's success/failure back into the in-page popup so it shows
   * green/red feedback. `id` must match the submitId the popup emitted.
   * @param {string} conversationId
   * @param {{ id: number, ok: boolean, message?: string }} result
   * @returns {Promise<{ ok: boolean, error?: string }>}
   */
  browserSignalDesignResult: (conversationId, result) =>
    ipcRenderer.invoke("omnigent:browser-signal-design-result", {
      conversationId,
      id: result?.id,
      ok: result?.ok,
      message: result?.message,
    }),
  /**
   * Subscribe to element-selection events. Payload: element info + a cropped
   * base64 screenshot (`{conversationId, ...info, screenshot}`). Returns an unsubscribe.
   * @param {(payload: Record<string, unknown>) => void} callback
   * @returns {() => void}
   */
  onBrowserElementSelected: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("browser-element-selected", listener);
    return () => ipcRenderer.removeListener("browser-element-selected", listener);
  },
  /**
   * Subscribe to element-prompt submits (Send/Enter). Payload:
   * `{conversationId, id, element, prompt}`. Returns an unsubscribe.
   * @param {(payload: Record<string, unknown>) => void} callback
   * @returns {() => void}
   */
  onBrowserElementPromptSubmit: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("browser-element-prompt-submit", listener);
    return () => ipcRenderer.removeListener("browser-element-prompt-submit", listener);
  },
  /**
   * Subscribe to element-prompt dismissals (user pressed × / Escape).
   * Payload: `{conversationId}`. Returns an unsubscribe.
   * @param {(payload: { conversationId: string }) => void} callback
   * @returns {() => void}
   */
  onBrowserElementPromptDismiss: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("browser-element-prompt-dismiss", listener);
    return () => ipcRenderer.removeListener("browser-element-prompt-dismiss", listener);
  },
});

// Setup-page bridge: persist + navigate to a server URL, and read the saved
// one to pre-fill the form. Separate object so the SPA never sees it.
contextBridge.exposeInMainWorld("omnigentSetup", {
  getServerUrl: () => ipcRenderer.invoke("omnigent:get-server-url"),
  /**
   * Persist + navigate to a server URL. Connecting this machine as a runner is
   * a separate, explicit action from the host menu — not a connect-time choice.
   * Resolves `{needsConfirm:true, url}` when a remote URL doesn't look like an
   * Omnigent server; re-call with `{force:true}` to proceed anyway.
   * @param {string} url
   * @param {{force?: boolean}} [opts]
   */
  setServerUrl: (url, opts) => ipcRenderer.invoke("omnigent:set-server-url", url, opts),
  /** Organization-provided server URLs from macOS Managed Preferences. */
  getManagedServers: () => ipcRenderer.invoke("omnigent:get-managed-servers"),
  /** Recently-connected server URLs, most recent first. */
  getRecentServers: () => ipcRenderer.invoke("omnigent:get-recent-servers"),
  /** Drop one recent server from the saved list; resolves the remaining ones. */
  forgetRecentServer: (url) => ipcRenderer.invoke("omnigent:forget-recent-server", url),
  /**
   * Advisory reachability probe for a server URL; resolves
   * `{status: "ok" | "reachable" | "unreachable"}`. Never gates connecting.
   * @param {string} url
   */
  checkServer: (url) => ipcRenderer.invoke("omnigent:check-server", url),
  /** Copy text from the bundled setup page to the native clipboard. */
  copyText: (text) => ipcRenderer.invoke("omnigent:copy-setup-text", text),
  /**
   * Toggle the revamped server selector and reload this window to the chosen
   * setup page. `true` → new experience, `false` → classic. No-op if the env
   * var forces the choice.
   * @param {boolean} enabled
   */
  setServerSelectorV2: (enabled) => ipcRenderer.invoke("omnigent:set-server-selector-v2", enabled),
  /**
   * Whether the `omnigent` CLI is installed/runnable, e.g.
   * `{installed, path, version, source, installCommand}`.
   */
  getCliStatus: () => ipcRenderer.invoke("omnigent:get-cli-status"),
  /**
   * Set an explicit path to the omnigent binary. Resolves the CLI status plus
   * `accepted` (whether that exact path validated and was saved).
   * @param {string} path
   */
  setCliPath: (path) => ipcRenderer.invoke("omnigent:set-cli-path", path),
  /** Native file picker for the omnigent binary; resolves the path or null. */
  browseCliPath: () => ipcRenderer.invoke("omnigent:browse-cli-path"),
  /**
   * Start (or reuse) the local server. Resolves `{ok, url?, error?}`; the
   * caller then connects to `url` via setServerUrl.
   */
  startLocalServer: () => ipcRenderer.invoke("omnigent:start-local-server"),
});
