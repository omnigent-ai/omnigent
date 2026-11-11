/**
 * Preload for the SHELL-OWNED SSH connect console (ssh-connect/index.html).
 * Deliberately narrow, like arca_connect_preload.ts: the page can confirm or
 * cancel its own flow, answer a prompt it was shown, and receive events.
 */

import { contextBridge, ipcRenderer } from "electron";
import type { ElectronSshConnectBridge } from "./ipc/bridge.ts";
import { createRendererIpc } from "./ipc/renderer.ts";

const ipc = createRendererIpc(ipcRenderer);

contextBridge.exposeInMainWorld("sshConnect", {
  confirm: () => ipc.send("ssh-connect:confirm"),
  cancel: () => ipc.send("ssh-connect:cancel"),
  /** `{ id, answer }`, or `{ id, cancel: true }`. */
  answer: (payload) => ipc.send("ssh-connect:answer", payload),
  resizeToContent: (size) => ipc.send("ssh-connect:resize", size),
  /** `{ serverUrl, destination, commands }`. */
  onInit: (callback) => {
    ipc.on("ssh-connect:init", (payload) => callback(payload));
  },
  onStarted: (callback) => {
    ipc.on("ssh-connect:started", (payload) => callback(payload));
  },
  /** `{ step, code? }`. */
  onStatus: (callback) => {
    ipc.on("ssh-connect:status", (payload) => callback(payload));
  },
  /** `{ id, kind, text }`; kind is confirm, secret, text or install. */
  onPrompt: (callback) => {
    ipc.on("ssh-connect:prompt", (payload) => callback(payload));
  },
  /** `{ id }`: ssh stopped waiting for that prompt's answer. */
  onPromptClosed: (callback) => {
    ipc.on("ssh-connect:prompt-closed", (payload) => callback(payload));
  },
  onOutput: (callback) => {
    ipc.on("ssh-connect:output", (payload) => callback(payload));
  },
  /** `{ ok, error }`. */
  onDone: (callback) => {
    ipc.on("ssh-connect:done", (payload) => callback(payload));
  },
} satisfies ElectronSshConnectBridge);
