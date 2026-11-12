// Preload for the workspace picker modal (workspace-picker/index.html).
//
// Bridges the account's workspace list from main into the picker page and sends
// the user's choice back. contextIsolation is on, so the page reaches these only
// through the exposed `window.workspacePicker` — never Node/ipcRenderer directly.

import { contextBridge, ipcRenderer } from "electron";
import type { ElectronWorkspacePickerBridge } from "./ipc/bridge.ts";
import { createRendererIpc } from "./ipc/renderer.ts";

const ipc = createRendererIpc(ipcRenderer);

contextBridge.exposeInMainWorld("workspacePicker", {
  /** Fetch the workspaces to show: [{ workspaceId, name, fqdn }]. */
  list: () => ipc.invoke("workspacePicker:list"),
  /** Resolve the picker with the chosen workspace id. */
  choose: (workspaceId) => ipc.send("workspacePicker:choose", workspaceId),
  /** Dismiss the picker without choosing. */
  cancel: () => ipc.send("workspacePicker:cancel"),
} satisfies ElectronWorkspacePickerBridge);
