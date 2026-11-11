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

import { contextBridge, ipcRenderer } from "electron";
import type { ElectronUpdateOverlayBridges } from "./ipc/bridge.ts";
import { createRendererIpc } from "./ipc/renderer.ts";

const ipc = createRendererIpc(ipcRenderer);

contextBridge.exposeInMainWorld("omnigentDesktop", {
  kind: "electron",
  updates: {
    getConfig: () => ipc.invoke("omnigent:overlay-get-update-config"),
    getStatus: () => ipc.invoke("omnigent:overlay-get-update-status"),
    check: () => ipc.invoke("omnigent:overlay-update-check"),
    download: () => ipc.invoke("omnigent:overlay-update-download"),
    installNow: () => ipc.invoke("omnigent:overlay-update-install"),
    setConfig: (patch) => ipc.invoke("omnigent:overlay-set-update-config", patch),
    // Reuse the shell's existing broadcast (sent to every window).
    onStatus: (callback) => ipc.on("omnigent:update-status", (status) => callback(status)),
  },
} satisfies ElectronUpdateOverlayBridges["omnigentDesktop"]);

contextBridge.exposeInMainWorld("omnigentUpdateOverlay", {
  /** Report the rendered card height (px) so the shell can size/show/hide the
   *  transparent overlay window. 0 means nothing to show — the shell hides it. */
  reportHeight: (height) => ipc.send("omnigent:overlay-height", Math.max(0, Math.ceil(height))),
  /** Subscribe to OS/app appearance changes so the card can restyle live. */
  onTheme: (callback) => ipc.on("omnigent:overlay-theme", (theme) => callback(theme)),
} satisfies ElectronUpdateOverlayBridges["omnigentUpdateOverlay"]);
