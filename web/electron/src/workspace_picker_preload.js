// Preload for the workspace picker modal (workspace-picker/index.html).
//
// Bridges the account's workspace list from main into the picker page and sends
// the user's choice back. contextIsolation is on, so the page reaches these only
// through the exposed `window.workspacePicker` — never Node/ipcRenderer directly.

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("workspacePicker", {
  /** Fetch the workspaces to show: [{ workspaceId, name, fqdn }]. */
  list: () => ipcRenderer.invoke("workspacePicker:list"),
  /** Resolve the picker with the chosen workspace id. */
  choose: (workspaceId) => ipcRenderer.send("workspacePicker:choose", workspaceId),
  /** Dismiss the picker without choosing. */
  cancel: () => ipcRenderer.send("workspacePicker:cancel"),
});
