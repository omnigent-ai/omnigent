import { contextBridge, ipcRenderer } from "electron";
import type { ElectronBrowserPermissionBridge } from "./ipc/bridge.ts";
import { createRendererIpc } from "./ipc/renderer.ts";

const ipc = createRendererIpc(ipcRenderer);

contextBridge.exposeInMainWorld("omnigentBrowserPermission", {
  getInfo: () => ipc.invoke("omnigent:browser-permission-info"),
  choose: (choice) => ipc.invoke("omnigent:browser-permission-choose", choice),
} satisfies ElectronBrowserPermissionBridge);
