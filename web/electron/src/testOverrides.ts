// Test seam for desktop e2e runs: a `-r` script registers overrides before the
// main process loads, and main applies them where it requires a module.
// Packaged apps ignore them.

import { createRequire } from "node:module";

/**
 * Receives the module's exports (after earlier overrides) and returns the
 * exports to replace.
 */
export type TestOverride = (moduleExports: Record<string, unknown>) => Record<string, unknown>;

declare global {
  // Shared by every copy of this module, including the one bundled into app/main.cjs.
  var omnigentTestOverrides: Record<string, TestOverride[]> | undefined;
}

/** The part of Electron's exports this seam reads. */
interface ElectronPackagingState {
  app?: { isPackaged?: unknown } | undefined;
}

/**
 * Queue an override for a `src/` module. Overrides apply in registration order.
 *
 * @param moduleName e.g. "browserViewRegistry"
 */
export function registerTestOverride(moduleName: string, override: TestOverride): void {
  const registry = (globalThis.omnigentTestOverrides ??= {});
  (registry[moduleName] ??= []).push(override);
}

/**
 * Return `moduleExports` with the registered overrides applied, or unchanged
 * when none are registered or the app is packaged.
 */
export function withTestOverrides<T extends Record<string, unknown>>(
  moduleName: string,
  moduleExports: T,
): T {
  const overrides = globalThis.omnigentTestOverrides?.[moduleName];
  if (!overrides?.length) return moduleExports;
  // The one lazy Electron load outside an entry point: modules load without Electron when nothing is registered.
  const electron: ElectronPackagingState = createRequire(import.meta.url)("electron");
  if (electron.app?.isPackaged !== false) return moduleExports;
  return overrides.reduce<T>(
    (current, override) => ({ ...current, ...override(current) }),
    moduleExports,
  );
}
