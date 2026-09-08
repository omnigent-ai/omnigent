"use strict";

const fs = require("node:fs");
const path = require("node:path");

/** Reveal local host items through the OS file manager, never execute them. */
function registerFileReveal({ ipcMain, shell, isPinnedOriginSender, localHostId }) {
  ipcMain.handle("omnigent:reveal-file", (event, hostId, filePath) => {
    if (!isPinnedOriginSender(event)) return false;
    if (typeof hostId !== "string" || !hostId || hostId !== localHostId()) return false;
    if (typeof filePath !== "string" || filePath.includes("\0") || !path.isAbsolute(filePath)) {
      return false;
    }
    try {
      if (!fs.existsSync(filePath)) return false;
      shell.showItemInFolder(filePath);
      return true;
    } catch {
      return false;
    }
  });
}

module.exports = { registerFileReveal };
