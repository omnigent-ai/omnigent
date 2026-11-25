// Worker bundles resolve packages with the page's "browser" export condition, so a
// package whose browser entry needs the DOM (DOMParser, document) stops a worker at
// startup. Where a package also publishes a "worker" entry, worker bundles use it.

import { readFileSync } from "node:fs";
import path from "node:path";
import type { Plugin } from "vite";

/** The file a package's root export names under `condition`, if it names one directly. */
function rootExportFor(exportsField: unknown, condition: string): string | null {
  if (typeof exportsField !== "object" || exportsField === null) return null;
  const root = "." in exportsField ? (exportsField as Record<string, unknown>)["."] : exportsField;
  if (typeof root !== "object" || root === null) return null;
  const target = (root as Record<string, unknown>)[condition];
  return typeof target === "string" ? target : null;
}

/**
 * The package's "worker" entry when `resolvedFile` is its "browser" entry and the two
 * differ; null when the package has no such pair or something else was resolved.
 */
export function workerEntryFor(
  manifest: { exports?: unknown },
  packageRoot: string,
  resolvedFile: string,
): string | null {
  const browser = rootExportFor(manifest.exports, "browser");
  const worker = rootExportFor(manifest.exports, "worker");
  if (!browser || !worker || browser === worker) return null;
  if (path.resolve(packageRoot, browser) !== path.resolve(resolvedFile)) return null;
  return path.resolve(packageRoot, worker);
}

/** The package a bare import names, such as `pkg` or `@scope/pkg`; null for its subpaths and files. */
export function rootPackageName(source: string): string | null {
  if (source.startsWith(".") || source.startsWith("/") || source.startsWith("\0")) return null;
  if (source.includes(":")) return null;
  const parts = source.split("/");
  const name = source.startsWith("@") ? parts.slice(0, 2).join("/") : parts[0]!;
  return name === source && name.length > 0 ? name : null;
}

/** The installed package's folder that `file` sits in, for the package called `name`. */
function packageRootOf(file: string, name: string): string | null {
  const marker = `/node_modules/${name}/`;
  const normalized = file.replaceAll("\\", "/");
  const index = normalized.lastIndexOf(marker);
  return index === -1 ? null : file.slice(0, index + marker.length - 1);
}

/** Resolves a package's "worker" entry, in place of its "browser" one, inside worker bundles. */
export function preferWorkerExports(): Plugin {
  return {
    name: "omnigent:prefer-worker-exports",
    enforce: "pre",
    async resolveId(source, importer, options) {
      const name = rootPackageName(source);
      if (!name) return null;
      const resolved = await this.resolve(source, importer, { ...options, skipSelf: true });
      if (!resolved || resolved.external) return resolved;
      const file = resolved.id.split("?")[0]!;
      const root = packageRootOf(file, name);
      if (!root) return resolved;
      let manifest: { exports?: unknown };
      try {
        manifest = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
      } catch {
        return resolved;
      }
      const entry = workerEntryFor(manifest, root, file);
      return entry ? { ...resolved, id: entry } : resolved;
    },
  };
}

/** The plugins every worker bundle is built with, by the app and by the worker tests. */
export const workerBuildPlugins = (): Plugin[] => [preferWorkerExports()];

/** The `worker` options of every web build, the app's and the embed's. */
export const workerBuildOptions = { plugins: workerBuildPlugins };

/**
 * What `vite serve` needs for the same: the page and its workers share each module and the
 * dependency pre-bundle there, so both take the worker entries, which need no DOM either. A
 * build, the tests and any other run keep the browser entries in both halves.
 */
export function devServerWorkerExports(
  command: string,
  mode: string,
): { plugins: Plugin[]; optimizeDeps: { rolldownOptions?: { plugins: Plugin[] } } } {
  if (command !== "serve" || mode === "test") return { plugins: [], optimizeDeps: {} };
  return {
    plugins: [preferWorkerExports()],
    optimizeDeps: { rolldownOptions: { plugins: [preferWorkerExports()] } },
  };
}
