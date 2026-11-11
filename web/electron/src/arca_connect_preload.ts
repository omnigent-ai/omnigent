/**
 * Preload for the SHELL-OWNED Arca connect console (arca-connect/index.html).
 * Deliberately narrow: the page can confirm/cancel its own flow and receive
 * init/output/done events — nothing else. It must NOT reuse the main shell
 * preload: this page needs none of those bridges, and consent surfaces should
 * carry the minimum possible capability.
 */

import { contextBridge, ipcRenderer } from "electron";
import type { ElectronArcaConnectBridge } from "./ipc/bridge.ts";
import { createRendererIpc } from "./ipc/renderer.ts";

const ipc = createRendererIpc(ipcRenderer);

contextBridge.exposeInMainWorld("arcaConnect", {
  /** The user confirmed — run the command. Accepted only from this window. */
  confirm: () => ipc.send("arca-connect:confirm"),
  /** Cancel / close the console (kills a mid-run command). */
  cancel: () => ipc.send("arca-connect:cancel"),
  /** Resize the shell-owned window to the rendered page, within main-owned bounds. */
  resizeToContent: (size) => ipc.send("arca-connect:resize", size),
  /** `{ serverUrl, command }` once the flow is ready. */
  onInit: (callback) => {
    ipc.on("arca-connect:init", (payload) => callback(payload));
  },
  /** `{ login, command }` when a command was spawned. */
  onStarted: (callback) => {
    ipc.on("arca-connect:started", (payload) => callback(payload));
  },
  /** A chunk of live stdout/stderr. */
  onOutput: (callback) => {
    ipc.on("arca-connect:output", (payload) => callback(payload));
  },
  /** `{ ok, error }` once the connect, and any sign-in it needed, finishes. */
  onDone: (callback) => {
    ipc.on("arca-connect:done", (payload) => callback(payload));
  },
} satisfies ElectronArcaConnectBridge);
