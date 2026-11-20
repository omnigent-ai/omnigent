"use strict";

/**
 * Preload for the SHELL-OWNED SSH connect console (ssh-connect/index.html).
 * Deliberately narrow, like arca_connect_preload.js: the page can confirm or
 * cancel its own flow, answer a prompt it was shown, and receive events.
 */

const { contextBridge, ipcRenderer } = require("electron");

function on(channel, callback) {
  ipcRenderer.on(channel, (_event, payload) => callback(payload));
}

contextBridge.exposeInMainWorld("sshConnect", {
  confirm: () => ipcRenderer.send("ssh-connect:confirm"),
  cancel: () => ipcRenderer.send("ssh-connect:cancel"),
  /** `{ id, answer }`, or `{ id, cancel: true }`. */
  answer: (payload) => ipcRenderer.send("ssh-connect:answer", payload),
  resizeToContent: (size) => ipcRenderer.send("ssh-connect:resize", size),
  /** `{ serverUrl, destination, commands }`. */
  onInit: (callback) => on("ssh-connect:init", callback),
  onStarted: (callback) => on("ssh-connect:started", callback),
  /** `{ step, code? }`. */
  onStatus: (callback) => on("ssh-connect:status", callback),
  /** `{ id, kind, text }`; kind is confirm, secret, text or install. */
  onPrompt: (callback) => on("ssh-connect:prompt", callback),
  /** `{ id }`: ssh stopped waiting for that prompt's answer. */
  onPromptClosed: (callback) => on("ssh-connect:prompt-closed", callback),
  onOutput: (callback) => on("ssh-connect:output", callback),
  /** `{ ok, error }`. */
  onDone: (callback) => on("ssh-connect:done", callback),
});
