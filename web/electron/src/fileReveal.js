"use strict";

const { execFile } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

/** Spotlight's type tree for a path, or null when it has no metadata (unindexed volume). */
function macContentTypes(filePath) {
  return new Promise((resolve) => {
    execFile(
      "mdls",
      ["-raw", "-name", "kMDItemContentTypeTree", filePath],
      { timeout: 2000 },
      (error, stdout) => resolve(error || stdout.trim() === "(null)" ? null : stdout),
    );
  });
}

/** Whether a macOS directory is a package (Foo.app, Deck.key) that openPath would launch. */
async function isMacPackage(dir, contentTypes) {
  if (fs.existsSync(path.join(dir, "Contents", "Info.plist"))) return true;
  const types = await contentTypes(dir);
  // Without metadata, a dotted name may be a package; selecting it is the safe miss.
  return types === null ? path.extname(dir) !== "" : types.includes("com.apple.package");
}

/**
 * Reveal this machine's files in the OS file manager, never executing them: a
 * file, link, or macOS package is selected in its folder, and a folder is opened.
 */
function registerFileReveal({
  ipcMain,
  shell,
  isPinnedOriginSender,
  localHostId,
  platform = process.platform,
  contentTypes = macContentTypes,
}) {
  ipcMain.handle("omnigent:reveal-file", async (event, hostId, rawPath) => {
    if (!isPinnedOriginSender(event)) return false;
    if (typeof hostId !== "string" || !hostId || hostId !== localHostId()) return false;
    if (typeof rawPath !== "string" || rawPath.includes("\0") || !path.isAbsolute(rawPath)) {
      return false;
    }
    // The SPA joins paths with "/"; normalize to this platform's separators.
    const filePath = path.normalize(rawPath);
    try {
      // lstat: a link is selected, never followed, so it can't open what it points at.
      const stat = fs.lstatSync(filePath);
      if (
        !stat.isDirectory() ||
        (platform === "darwin" && (await isMacPackage(filePath, contentTypes)))
      ) {
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
