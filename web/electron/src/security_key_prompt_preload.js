// Preload for security-key-prompt/index.html; main matches its IPC to the prompt by sender.

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("omnigentSecurityKeyPrompt", {
  /** Hide the prompt; the next WebAuthn request shows it again. */
  dismiss: () => {
    ipcRenderer.send("omnigent:security-key-prompt-dismiss");
  },
});
