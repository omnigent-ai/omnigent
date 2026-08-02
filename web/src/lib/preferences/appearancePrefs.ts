/**
 * Eager Appearance preference barrel.
 *
 * Registration is an import side-effect of each `createLocalPreference({
 * appearance: true })` module. Without this barrel, a preference that only
 * lives in a lazily-loaded chunk would be missing from the registry until
 * that chunk loads — and Appearance → Reset would silently skip it.
 *
 * Import this module once at app init (`main.tsx`, `embed.tsx`). When
 * migrating a preference:
 *
 * 1. Add a side-effect import below.
 * 2. Add its storage key to {@link EXPECTED_APPEARANCE_STORAGE_KEYS}.
 * 3. Remove that key from {@link LEGACY_APPEARANCE_STORAGE_KEYS}.
 *
 * The anti-rot test in `appearancePrefs.test.ts` fails CI if those drift.
 */

// Side-effect imports — each module registers its preference on load.
import "@/lib/uiFontPreferences";
import "@/lib/workspacePanelPreferences";
import "@/lib/terminalThemePreferences";

/**
 * Storage keys that MUST be registered after this barrel loads.
 * Add a key here when migrating a preference onto the declarative layer.
 */
export const EXPECTED_APPEARANCE_STORAGE_KEYS = [
  "omnigent:ui-font-size",
  "omnigent:default-workspace-panel",
  "omnigent:terminal-theme",
] as const;

/**
 * Appearance localStorage keys not yet on `createLocalPreference(...,
 * { appearance: true })`. Settings reset clears these explicitly. Remove a
 * key when its module migrates — the registry then owns reset for it.
 */
export const LEGACY_APPEARANCE_STORAGE_KEYS = [
  "omnigent:ui-font-family",
  "omnigent:code-font-size",
  "omnigent:code-font-family",
  "omnigent:ui-theme-palette",
  "omnigent:custom-theme",
  "omnigent:hide-unconfigured-harnesses",
] as const;
