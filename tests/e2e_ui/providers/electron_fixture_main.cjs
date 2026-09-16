"use strict";

// Test-only Electron entrypoint. It runs the production shell and renderer but
// removes the desktop-owned process, updater, login-shell, and protocol hooks.
const Module = require("node:module");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const { app } = require("electron");

const productionMain = path.resolve(__dirname, "../../../web/electron/src/main.js");

app.setAsDefaultProtocolClient = () => false;

class FixtureVersion {
  constructor(version) {
    this.version = version;
  }
}

const autoUpdater = Object.assign(new EventEmitter(), {
  currentVersion: new FixtureVersion("0.0.0"),
  checkForUpdates: async () => null,
  quitAndInstall: () => {},
});

const serverManager = {
  onChange: () => {},
  shutdown: async () => {},
  startLocalServer: async () => ({ ok: false, error: "disabled in provider fixture" }),
  ensureServerAuth: async () => ({ ok: false, error: "disabled in provider fixture" }),
  ensureHostConnected: async () => ({ ok: false, error: "disabled in provider fixture" }),
  restartHost: async () => ({ ok: false, error: "disabled in provider fixture" }),
  disconnectHost: async () => ({ ok: false, error: "disabled in provider fixture" }),
};

const omnigentCli = {
  resolveCliPath: () => null,
  isExecutableFile: () => false,
  getCliStatus: async () => ({ installed: false }),
  normalizeServerUrl: (url) => String(url || "").replace(/\/+$/, ""),
};

const originalLoad = Module._load;
Module._load = function fixtureLoad(request, parent, isMain) {
  if (parent?.filename === productionMain) {
    if (request === "./server_manager") return serverManager;
    if (request === "./loginShellPath") {
      return { resolveLoginShellPath: () => null, mergePath: (current) => current };
    }
    if (request === "./omnigent_cli") return omnigentCli;
    if (request === "electron-updater") return { autoUpdater };
  }
  return originalLoad.call(this, request, parent, isMain);
};

require(productionMain);
