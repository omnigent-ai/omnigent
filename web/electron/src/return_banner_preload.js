// Preload for the return-to-server banner (electron/return-banner/index.html)
// — a tiny, bundled, trusted page, but it gets the same contextIsolation
// treatment as everything else: a narrow contextBridge API, never raw
// ipcRenderer. The main process verifies the sender frame on every message,
// so this bridge is inert if it ever ends up attached to anything else.

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("omnigentReturnBanner", {
  /** Navigate the parent window back to the remembered server URL. */
  goBack: () => {
    ipcRenderer.send("omnigent:return-banner-go-back");
  },
  /** Hide the banner without navigating. */
  dismiss: () => {
    ipcRenderer.send("omnigent:return-banner-dismiss");
  },
});
