// Locations of the shell's pages, preloads and icons.
//
// Both src/ and the bundled app/ directory sit one level below the package
// root, so the same resolution works when run from source and from the bundle.

"use strict";

const path = require("node:path");

/** The Electron package root (holds package.json and the page directories). */
const PACKAGE_ROOT = path.join(__dirname, "..");

/** Where scripts/build.mjs writes the bundled main process and preloads. */
const BUNDLE_DIR = path.join(PACKAGE_ROOT, "app");

/** Absolute path to the app icon (PNG works for the macOS dock at runtime). */
const ICON_PNG = path.join(PACKAGE_ROOT, "icons", "icon.png");

/**
 * Absolute path to a shell-owned page.
 *
 * @param {string} directory Page directory under the package root.
 * @param {string} [file]
 * @returns {string}
 */
function pagePath(directory, file = "index.html") {
  return path.join(PACKAGE_ROOT, directory, file);
}

/**
 * Absolute path to a bundled preload. The bundle keeps the source file name,
 * so src/find_preload.js becomes app/find_preload.js.
 *
 * @param {string} name Preload file name without the extension.
 * @returns {string}
 */
function preloadPath(name) {
  return path.join(BUNDLE_DIR, `${name}.js`);
}

module.exports = { BUNDLE_DIR, ICON_PNG, PACKAGE_ROOT, pagePath, preloadPath };
