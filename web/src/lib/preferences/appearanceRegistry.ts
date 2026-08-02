/**
 * Appearance preference registry.
 *
 * Preferences created with `appearance: true` register here. The Settings
 * Appearance reset dialog resets every registered preference and clears their
 * storage keys — so adding a setting updates reset automatically.
 *
 * Registration is an import side-effect. App init must import
 * `./appearancePrefs` (the eager barrel) so every Appearance preference is
 * registered regardless of which routes have loaded. The anti-rot test in
 * `appearancePrefs.test.ts` asserts the registered key set.
 *
 * ## Migrating a remaining preference
 *
 * 1. Replace its read/write helpers with `createLocalPreference({ ..., appearance: true })`.
 * 2. Keep the same `key` and `parse` the existing stored format (or migrate explicitly).
 * 3. Side-effect-import the module from `appearancePrefs.ts` and add its key to
 *    `EXPECTED_APPEARANCE_STORAGE_KEYS`; remove it from `LEGACY_APPEARANCE_STORAGE_KEYS`.
 * 4. Drop the matching hand-rolled write/apply calls from `resetAppearance` in Settings.
 * 5. Optionally switch the Settings control to `usePreference(pref)`.
 */

import type { LocalPreference } from "./createLocalPreference";

/** Registry entry — only `key` / `reset` are used by Appearance reset. */
export type AppearancePreference = Pick<LocalPreference<unknown>, "key" | "reset">;

const appearancePreferences: AppearancePreference[] = [];

/** Register a preference for Appearance → Reset. Called by the factory. */
export function registerAppearancePreference(pref: AppearancePreference): void {
  if (appearancePreferences.some((existing) => existing.key === pref.key)) {
    return;
  }
  appearancePreferences.push(pref);
}

/** Snapshot of registered Appearance preferences (order = registration order). */
export function getAppearancePreferences(): readonly AppearancePreference[] {
  return appearancePreferences;
}

/** Storage keys owned by registered Appearance preferences. */
export function getAppearanceStorageKeys(): readonly string[] {
  return appearancePreferences.map((pref) => pref.key);
}

/** Reset every registered Appearance preference to its default. */
export function resetAppearancePreferences(): void {
  for (const pref of appearancePreferences) {
    pref.reset();
  }
}

/** Test-only: drop prefs registered under `test:` keys; leave production prefs. */
export function clearAppearancePreferenceRegistryForTests(): void {
  for (let i = appearancePreferences.length - 1; i >= 0; i--) {
    const entry = appearancePreferences[i];
    if (entry && entry.key.startsWith("test:")) {
      appearancePreferences.splice(i, 1);
    }
  }
}
