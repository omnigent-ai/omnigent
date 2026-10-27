"use strict";

const { execFile } = require("node:child_process");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { promisify } = require("node:util");

/** Electron's file-icon API returns a generic .app icon; use the bundle artwork. */
async function readMacAppIcon(appPath, { run = promisify(execFile) } = {}) {
  let temporaryDirectory;
  const options = { timeout: 3000, maxBuffer: 128 * 1024 };
  try {
    const { stdout } = await run(
      "/usr/bin/plutil",
      ["-extract", "CFBundleIconFile", "raw", "-o", "-", path.join(appPath, "Contents/Info.plist")],
      options,
    );
    const filename = stdout.trim();
    if (!filename || filename === "." || filename === ".." || path.basename(filename) !== filename)
      return null;
    const iconPath = path.join(
      appPath,
      "Contents/Resources",
      path.extname(filename) ? filename : `${filename}.icns`,
    );
    temporaryDirectory = await fs.mkdtemp(path.join(os.tmpdir(), "omnigent-app-icon-"));
    const pngPath = path.join(temporaryDirectory, "icon.png");
    await run(
      "/usr/bin/sips",
      ["-s", "format", "png", "-Z", "32", iconPath, "--out", pngPath],
      options,
    );
    return `data:image/png;base64,${(await fs.readFile(pngPath)).toString("base64")}`;
  } catch {
    return null;
  } finally {
    if (temporaryDirectory) await fs.rm(temporaryDirectory, { recursive: true, force: true });
  }
}

module.exports = { readMacAppIcon };
