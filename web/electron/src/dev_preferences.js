"use strict";

const { execFileSync } = require("node:child_process");

const DEV_DOMAIN = "ai.omnigent.desktop-dev";

/** Electron's unpackaged macOS binary has its own bundle ID. Read our dev
 * domain explicitly so local defaults can be used without repackaging it. */
function getDevUserDefault(key, type, { exec = execFileSync } = {}) {
  try {
    const xml = exec("/usr/bin/defaults", ["export", DEV_DOMAIN, "-"], {
      encoding: "utf8",
      timeout: 3000,
      stdio: ["ignore", "pipe", "ignore"],
    });
    const json = exec("/usr/bin/plutil", ["-convert", "json", "-o", "-", "-"], {
      input: xml,
      encoding: "utf8",
      timeout: 3000,
      stdio: ["pipe", "pipe", "ignore"],
    });
    const value = JSON.parse(json)[key];
    if (type === "boolean") return typeof value === "boolean" ? value : false;
    if (type === "array") return Array.isArray(value) ? value : null;
    if (type === "dictionary")
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    return null;
  } catch {
    return null;
  }
}

module.exports = { DEV_DOMAIN, getDevUserDefault };
