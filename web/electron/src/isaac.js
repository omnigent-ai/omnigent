"use strict";

/**
 * Locate Databricks' internal `isaac` launcher for desktop host enrollment.
 * GUI-launched Electron inherits a minimal PATH, so mirror the Omnigent/Arca
 * resolver pattern: PATH first, then well-known macOS install locations.
 */

const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

/** @returns {string[]} */
function candidatePaths() {
  const home = os.homedir();
  return [
    "/usr/local/bin/isaac",
    "/opt/homebrew/bin/isaac",
    path.join(home, ".local", "bin", "isaac"),
  ];
}

/** @param {string} value @returns {boolean} */
function isExecutableFile(value) {
  try {
    if (!fs.statSync(value).isFile()) return false;
    fs.accessSync(value, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/** @returns {string | null} */
function whichIsaac() {
  try {
    const out = execFileSync("/bin/sh", ["-c", "command -v isaac"], { encoding: "utf8" });
    return out.trim() || null;
  } catch {
    return null;
  }
}

/**
 * @param {{
 *   isExecutableFile?: (value: string) => boolean,
 *   whichIsaac?: () => string | null,
 *   candidatePaths?: () => string[],
 * }} [deps]
 * @returns {string | null}
 */
function resolveIsaacPath(deps = {}) {
  const isExec = deps.isExecutableFile || isExecutableFile;
  const onPath = (deps.whichIsaac || whichIsaac)();
  if (onPath && isExec(onPath)) return onPath;
  for (const candidate of (deps.candidatePaths || candidatePaths)()) {
    if (isExec(candidate)) return candidate;
  }
  return null;
}

module.exports = { candidatePaths, resolveIsaacPath };
