"use strict";

// Test seam for desktop e2e runs: a `-r` script registers overrides before the
// main process loads, and main applies them where it requires a module.
// Packaged apps ignore them.

const TEST_OVERRIDES_GLOBAL = "omnigentTestOverrides";

/**
 * Receives the module's exports (after earlier overrides) and returns the
 * exports to replace.
 *
 * @typedef {(moduleExports: Record<string, any>) => Record<string, unknown>} TestOverride
 */

/**
 * Queue an override for a `src/` module. Overrides apply in registration order.
 *
 * @param {string} moduleName e.g. "browserViewRegistry"
 * @param {TestOverride} override
 */
function registerTestOverride(moduleName, override) {
  const registry = (globalThis[TEST_OVERRIDES_GLOBAL] ??= {});
  (registry[moduleName] ??= []).push(override);
}

/**
 * Return `moduleExports` with the registered overrides applied, or unchanged
 * when none are registered or the app is packaged.
 *
 * @template {Record<string, any>} T
 * @param {string} moduleName
 * @param {T} moduleExports
 * @returns {T}
 */
function withTestOverrides(moduleName, moduleExports) {
  /** @type {TestOverride[] | undefined} */
  const overrides = globalThis[TEST_OVERRIDES_GLOBAL]?.[moduleName];
  if (!overrides?.length) return moduleExports;
  // Required here so modules load without Electron when no override is registered.
  if (require("electron").app?.isPackaged !== false) return moduleExports;
  return overrides.reduce(
    (current, override) => ({ ...current, ...override(current) }),
    moduleExports,
  );
}

module.exports = { registerTestOverride, withTestOverrides };
