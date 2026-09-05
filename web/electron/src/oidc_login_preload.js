"use strict";

const { contextBridge, ipcRenderer } = require("electron");

// Sandboxed preloads may require Electron built-ins, but not sibling files.
// Keep these private modal-only channel names aligned with oidc_login_dialog.js.
const OIDC_LOGIN_ACTION_CHANNEL = "omnigent:oidc-login-action";
const OIDC_LOGIN_STATE_CHANNEL = "omnigent:oidc-login-state";

contextBridge.exposeInMainWorld("omnigentOidcLogin", {
  cancel: () => ipcRenderer.send(OIDC_LOGIN_ACTION_CHANNEL, "cancel"),
  retry: () => ipcRenderer.send(OIDC_LOGIN_ACTION_CHANNEL, "retry"),
  onState: (callback) => {
    const listener = (_event, state) => callback(state);
    ipcRenderer.on(OIDC_LOGIN_STATE_CHANNEL, listener);
    return () => ipcRenderer.removeListener(OIDC_LOGIN_STATE_CHANNEL, listener);
  },
});
