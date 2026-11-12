// Preload for the reconnecting overlay (electron/reconnect-overlay/index.html):
// a narrow contextBridge API, never raw ipcRenderer. The main process verifies
// the sender frame, so this bridge is inert if attached to anything else.

import { contextBridge, ipcRenderer } from "electron";
import type { ElectronReconnectOverlayBridge } from "./ipc/bridge.ts";
import { createRendererIpc } from "./ipc/renderer.ts";

const ipc = createRendererIpc(ipcRenderer);

contextBridge.exposeInMainWorld("omnigentReconnectOverlay", {
  /** Stop reconnecting and return the window to the setup page. */
  cancel: () => {
    ipc.send("omnigent:reconnect-overlay-cancel");
  },
} satisfies ElectronReconnectOverlayBridge);
