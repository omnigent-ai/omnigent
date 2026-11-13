// Bundles the Electron main process and preloads from src/ into app/.
//
//   node scripts/build.mjs           one build
//   node scripts/build.mjs --watch   rebuild on change (restart Electron for main changes)
//
// Pages are not bundled; they load from their package-root directories.

import { readdir, rm } from "node:fs/promises";
import { builtinModules } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { build } from "vite";
import paths from "../src/paths.js";

const { BUNDLE_DIR, PACKAGE_ROOT } = paths;
const SRC_DIR = path.join(PACKAGE_ROOT, "src");

// Preload bundles keep their src/ file names (paths.preloadPath). The .js
// files load as CommonJS only while package.json has no "type": "module".
const PRELOAD_FILE = /(^|_)preload\.js$/;

// Runtime dependencies stay in node_modules, so the packaged dependency set is unchanged.
const RUNTIME_DEPENDENCIES = ["electron", "electron-updater", "js-yaml"];

// electron-builder writes extraMetadata (omnigentBuild) into the packaged
// package.json, so main must read it at runtime instead of inlining it.
const PACKAGE_JSON = path.join(PACKAGE_ROOT, "package.json");

// Electron 45 ships Node 22 and Chromium 142.
const MAIN_TARGET = "node22";
const PRELOAD_TARGET = "chrome142";

/** @param {string} id */
function isExternal(id) {
  if (id.startsWith("node:") || builtinModules.includes(id)) return true;
  if (path.isAbsolute(id) && path.normalize(id) === PACKAGE_JSON) return true;
  return RUNTIME_DEPENDENCIES.some((name) => id === name || id.startsWith(`${name}/`));
}

/**
 * One self-contained CommonJS file. No minification: injected-page script
 * builders stringify code that must reach the page unchanged.
 *
 * @param {{ entry: string, outDir: string, fileName: string, target: string, watch?: boolean }} options
 */
function bundleCommonJs({ entry, outDir, fileName, target, watch = false }) {
  return build({
    configFile: false,
    root: PACKAGE_ROOT,
    publicDir: false,
    logLevel: watch ? "info" : "warn",
    ssr: { target: "node", noExternal: true },
    build: {
      ssr: entry,
      outDir,
      emptyOutDir: false,
      target,
      minify: false,
      sourcemap: true,
      reportCompressedSize: false,
      watch: watch ? {} : null,
      rolldownOptions: {
        external: isExternal,
        output: {
          format: "cjs",
          entryFileNames: fileName,
          codeSplitting: false,
        },
      },
    },
  });
}

/**
 * Bundle a main-process entry with the same settings as app/main.cjs.
 *
 * @param {{ entry: string, outDir: string, fileName: string, watch?: boolean }} options
 */
export function bundleMainProcess(options) {
  return bundleCommonJs({ ...options, target: MAIN_TARGET });
}

/** Preload entry file names in src/. */
export async function listPreloadFiles() {
  return (await readdir(SRC_DIR)).filter((file) => PRELOAD_FILE.test(file)).sort();
}

/**
 * Bundle one preload. A sandboxed preload can only require("electron"), never
 * a shared chunk, so each preload is its own build.
 *
 * @param {{ fileName: string, outDir: string, watch?: boolean }} options
 */
export function bundlePreload({ fileName, outDir, watch = false }) {
  return bundleCommonJs({
    entry: path.join(SRC_DIR, fileName),
    outDir,
    fileName,
    target: PRELOAD_TARGET,
    watch,
  });
}

/** @param {{ watch?: boolean }} [options] */
export async function buildApp({ watch = false } = {}) {
  await rm(BUNDLE_DIR, { recursive: true, force: true });
  await bundleMainProcess({
    entry: path.join(SRC_DIR, "main.js"),
    outDir: BUNDLE_DIR,
    fileName: "main.cjs",
    watch,
  });
  for (const fileName of await listPreloadFiles()) {
    // oxlint-disable-next-line no-await-in-loop -- Builds share one output directory.
    await bundlePreload({ fileName, outDir: BUNDLE_DIR, watch });
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await buildApp({ watch: process.argv.includes("--watch") });
}
