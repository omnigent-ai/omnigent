// Locations of the shell's pages, preloads and icons.
//
// Both src/ and the bundled app/ directory sit one level below the package
// root, so the same resolution works when run from source and from the bundle.

import path from "node:path";

/** The Electron package root (holds package.json and the page directories). */
export const PACKAGE_ROOT = path.join(import.meta.dirname, "..");

/** Where scripts/build.mjs writes the bundled main process and preloads. */
export const BUNDLE_DIR = path.join(PACKAGE_ROOT, "app");

/** Absolute path to the app icon (PNG works for the macOS dock at runtime). */
export const ICON_PNG = path.join(PACKAGE_ROOT, "icons", "icon.png");

/**
 * Absolute path to a shell-owned page.
 *
 * @param directory Page directory under the package root.
 */
export function pagePath(directory: string, file = "index.html"): string {
  return path.join(PACKAGE_ROOT, directory, file);
}

/**
 * Absolute path to a bundled preload. The bundle keeps the source file name,
 * so src/find_preload.js becomes app/find_preload.js.
 *
 * @param name Preload file name without the extension.
 */
export function preloadPath(name: string): string {
  return path.join(BUNDLE_DIR, `${name}.js`);
}
