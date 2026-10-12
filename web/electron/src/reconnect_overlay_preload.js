// Preload for the reconnecting overlay (electron/reconnect-overlay/index.html):
// a narrow contextBridge API, never raw ipcRenderer. The main process verifies
// the sender frame, so this bridge is inert if attached to anything else.

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("omnigentReconnectOverlay", {
  /** Stop reconnecting and return the window to the setup page. */
  cancel: () => {
    ipcRenderer.send("omnigent:reconnect-overlay-cancel");
  },
});
