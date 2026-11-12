// Narrow bridge for the bundled About window.

import { contextBridge, ipcRenderer } from "electron";
import type { ElectronAboutBridge } from "./ipc/bridge.ts";
import { createRendererIpc } from "./ipc/renderer.ts";

const ipc = createRendererIpc(ipcRenderer);

contextBridge.exposeInMainWorld(
  "omnigentAbout",
  Object.freeze({
    getInfo: () => ipc.invoke("omnigent:about-get-info"),
    getDesktopUpdateStatus: () => ipc.invoke("omnigent:about-get-desktop-update-status"),
    checkDesktopUpdates: () => ipc.invoke("omnigent:about-check-desktop"),
    downloadDesktopUpdate: () => ipc.invoke("omnigent:about-download-desktop-update"),
    installDesktopUpdate: () => ipc.invoke("omnigent:about-install-desktop-update"),
    onDesktopUpdateStatus: (callback) =>
      ipc.on("omnigent:update-status", (status) => callback(status)),
    close: () => ipc.send("omnigent:about-close"),
  } satisfies ElectronAboutBridge),
);
