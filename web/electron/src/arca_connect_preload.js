"use strict";

/**
 * Preload for the SHELL-OWNED Arca connect console (arca-connect/index.html).
 * Deliberately narrow: the page can confirm/cancel its own flow and receive
 * init/output/done events — nothing else. It must NOT reuse the main shell
 * preload: this page needs none of those bridges, and consent surfaces should
 * carry the minimum possible capability.
 */

const { contextBridge, ipcRenderer } = require("electron");

/** Subscribe to a main→console channel, passing the payload through. */
function on(channel, callback) {
  ipcRenderer.on(channel, (_event, payload) => callback(payload));
}

contextBridge.exposeInMainWorld("arcaConnect", {
  /** The user confirmed — run the command. Accepted only from this window. */
  confirm: () => ipcRenderer.send("arca-connect:confirm"),
  /** Cancel / close the console (kills a mid-run command). */
  cancel: () => ipcRenderer.send("arca-connect:cancel"),
  /** `{ serverUrl, command }` once the flow is ready. */
  onInit: (callback) => on("arca-connect:init", callback),
  /** The command was spawned. */
  onStarted: (callback) => on("arca-connect:started", callback),
  /** A chunk of live stdout/stderr. */
  onOutput: (callback) => on("arca-connect:output", callback),
  /** `{ ok, error }` when the command settles. */
  onDone: (callback) => on("arca-connect:done", callback),
});
