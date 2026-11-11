import { ipcRenderer } from "electron";
import { createRendererIpc } from "./ipc/renderer.ts";

createRendererIpc(ipcRenderer).on("omnigent:connection-loading", (label) => {
  const status = document.getElementById("status");
  if (status && typeof label === "string") status.textContent = label;
});
