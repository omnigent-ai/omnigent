"use strict";

const fs = require("node:fs");
const path = require("node:path");

/**
 * Reveal local host items through the OS file manager, never execute them: a
 * file is selected in its containing folder, a folder is opened.
 */
function registerFileReveal({ ipcMain, shell, isPinnedOriginSender, localHostId }) {
  ipcMain.handle("omnigent:reveal-file", async (event, hostId, filePath) => {
    if (!isPinnedOriginSender(event)) return false;
    if (typeof hostId !== "string" || !hostId || hostId !== localHostId()) return false;
    if (typeof filePath !== "string" || filePath.includes("\0") || !path.isAbsolute(filePath)) {
      return false;
    }
    try {
      if (!fs.statSync(filePath).isDirectory()) {
        shell.showItemInFolder(filePath);
        return true;
      }
      // Resolves "" on success, otherwise the platform's error message.
      return (await shell.openPath(filePath)) === "";
    } catch {
      return false;
    }
  });
}

module.exports = { registerFileReveal };
