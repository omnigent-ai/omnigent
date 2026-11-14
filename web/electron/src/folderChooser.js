"use strict";

const path = require("node:path");

/**
 * Folder picking for workspaces on this machine, answered only to the pinned
 * server page: the OS folder chooser, and the local path of a file dropped
 * from the file manager (which Chromium otherwise hides from web pages).
 */
function registerFolderChooser({ ipcMain, dialog, windowFor, isPinnedOriginSender }) {
  // Windows with a chooser already up; a second click must not queue another.
  const choosing = new Set();

  ipcMain.handle("omnigent:choose-directory", async (event, defaultPath) => {
    if (!isPinnedOriginSender(event) || choosing.has(event.sender.id)) return null;
    choosing.add(event.sender.id);
    try {
      const result = await dialog.showOpenDialog(windowFor(event), {
        properties: ["openDirectory", "createDirectory"],
        defaultPath:
          typeof defaultPath === "string" && path.isAbsolute(defaultPath) ? defaultPath : undefined,
      });
      return result.canceled ? null : (result.filePaths[0] ?? null);
    } finally {
      choosing.delete(event.sender.id);
    }
  });

  // The preload resolves the path; main releases it only to the pinned page.
  ipcMain.handle("omnigent:dropped-file-path", (event, filePath) =>
    isPinnedOriginSender(event) && typeof filePath === "string" ? filePath : "",
  );
}

module.exports = { registerFolderChooser };
