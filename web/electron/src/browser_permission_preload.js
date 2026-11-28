"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("omnigentBrowserPermission", {
  getInfo: () => ipcRenderer.invoke("omnigent:browser-permission-info"),
  choose: (choice) => ipcRenderer.invoke("omnigent:browser-permission-choose", choice),
});
