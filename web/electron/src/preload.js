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
   * Title-bar server picker data: the window's current server origin and the
   * recently-connected server URLs (most recent first). Resolves null on
   * pages that aren't a connected server.
   */
  getServerPicker: () => ipcRenderer.invoke("omnigent:get-server-picker"),
  /**
   * Re-point this window to a previously-connected server URL (must come
   * from getServerPicker's recentServers list; anything else rejects).
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
});

// Setup-page bridge: persist + navigate to a server URL, and read the saved
// one to pre-fill the form. Separate object so the SPA never sees it.
contextBridge.exposeInMainWorld("omnigentSetup", {
  getServerUrl: () => ipcRenderer.invoke("omnigent:get-server-url"),
  /**
   * Persist + navigate to a server URL. Connecting this machine as a runner is
   * a separate, explicit action from the host menu — not a connect-time choice.
   * @param {string} url
   */
  setServerUrl: (url) => ipcRenderer.invoke("omnigent:set-server-url", url),
  /** Recently-connected server URLs, most recent first. */
  getRecentServers: () => ipcRenderer.invoke("omnigent:get-recent-servers"),
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
