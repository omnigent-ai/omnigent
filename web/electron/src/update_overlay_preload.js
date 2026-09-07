// Preload for the desktop update overlay (electron/overlay/update-overlay.html).
//
// The overlay is a bundled, TRUSTED shell surface — unlike the remote server
// page — so it drives the updater over its own `omnigent:overlay-*` channels
// (main verifies the sender frame), bypassing the server-page IPC that requires
// a pinned origin + per-action consent dialog. Clicking the shell's own toast
// IS the user's consent.
//
// It exposes `window.omnigentDesktop` in the exact shape the web
// `updateBridge()` reads (kind + `updates`), so the reused `UpdateBanner`
// component works unchanged, plus a tiny `omnigentUpdateOverlay` used only to
// report the card's height back to the shell so it can size the window.

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("omnigentDesktop", {
  kind: "electron",
  updates: {
    getConfig: () => ipcRenderer.invoke("omnigent:overlay-get-update-config"),
    getStatus: () => ipcRenderer.invoke("omnigent:overlay-get-update-status"),
    check: () => ipcRenderer.invoke("omnigent:overlay-update-check"),
    download: () => ipcRenderer.invoke("omnigent:overlay-update-download"),
    installNow: () => ipcRenderer.invoke("omnigent:overlay-update-install"),
    setConfig: (patch) => ipcRenderer.invoke("omnigent:overlay-set-update-config", patch),
    onStatus: (callback) => {
      // Reuse the shell's existing broadcast (sent to every window).
      const listener = (_event, status) => callback(status);
      ipcRenderer.on("omnigent:update-status", listener);
      return () => ipcRenderer.removeListener("omnigent:update-status", listener);
    },
  },
});

contextBridge.exposeInMainWorld("omnigentUpdateOverlay", {
  /** Report the rendered card height (px) so the shell can size/show/hide the
   *  transparent overlay window. 0 means nothing to show — the shell hides it. */
  reportHeight: (height) =>
    ipcRenderer.send("omnigent:overlay-height", Math.max(0, Math.ceil(height))),
  /** Subscribe to OS/app appearance changes so the card can restyle live. */
  onTheme: (callback) => {
    const listener = (_event, theme) => callback(theme);
    ipcRenderer.on("omnigent:overlay-theme", listener);
    return () => ipcRenderer.removeListener("omnigent:overlay-theme", listener);
  },
});
