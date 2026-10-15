// Shared boot restoration for standalone and embedded entry points.
// Saved CSS applies before paint; catalog loads wait for the server capability.
//
// SSR/no-DOM safe: every apply/load helper guards for a missing document.

import {
  applyDesktopUiFontSize,
  applyUiFontFamily,
  readUiFontFamily,
  readUiFontSizePx,
} from "./uiFontPreferences";
import { applyFixedWidthFontFamily, readFixedWidthFontFamily } from "./fixedWidthFontPreferences";
import { loadCodeFontFamily, readCodeFontFamily } from "./codeFontPreferences";
import { setWebFontsEnabled } from "./webFontLoader";
import { isFeatureEnabled, type ServerInfo } from "./capabilities";

/**
 * Restore the saved UI + fixed-width + code font preferences on boot.
 *
 * - UI font: applies the size (`--desktop-ui-font-size`) and family
 *   (`--ui-font-family`) to the document root.
 * - Fixed-width font: applies `--ui-mono-font-family`.
 * - Code font: travels via pub/sub, not a CSS variable, so load only when asked.
 *
 * Font loading is a no-op until `applyWebFontCapability` enables it.
 */
export function restoreFontPreferences(): void {
  applyDesktopUiFontSize(readUiFontSizePx());
  applyUiFontFamily(readUiFontFamily());
  applyFixedWidthFontFamily(readFixedWidthFontFamily());
  loadCodeFontFamily(readCodeFontFamily());
}

/** Apply the resolved webfonts capability before restoring downloadable fonts. */
export function applyWebFontCapability(enabled: boolean): void {
  setWebFontsEnabled(enabled);
  if (enabled) restoreFontPreferences();
}

/** Apply the settled server capability before restoring downloadable fonts. */
export function applyWebFontCapabilityFromServerInfo(info: ServerInfo): void {
  applyWebFontCapability(isFeatureEnabled(info, "webfonts"));
}
