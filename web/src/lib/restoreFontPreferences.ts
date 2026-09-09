// Boot-time font restoration, shared by the standalone (main.tsx) and embed
// (embed.tsx) entry points.
//
// The saved font preferences must be applied before first paint so there's no
// flash, and the catalog webfont loads kicked off so a chosen font is fetched on
// boot rather than only on the next Settings change. Both entry points need the
// exact same calls, so they live here once.
//
// SSR/no-DOM safe: every apply/load helper guards for a missing document.

import {
  applyDesktopUiFontSize,
  applyUiFontFamily,
  readUiFontFamily,
  readUiFontSizePx,
} from "./uiFontPreferences";
import { loadCodeFontFamily, readCodeFontFamily } from "./codeFontPreferences";

/**
 * Restore the saved UI + code font preferences on boot.
 *
 * - UI font: applies the size (`--desktop-ui-font-size`) and family
 *   (`--ui-font-family`) to the document root; `applyUiFontFamily` also kicks
 *   the catalog webfont load for the saved family.
 * - Code font: rides a pub/sub (not a CSS var), so nothing loads it unless we
 *   ask — `loadCodeFontFamily` fetches it and mounted editors/terminals
 *   re-measure when it lands.
 */
export function restoreFontPreferences(): void {
  applyDesktopUiFontSize(readUiFontSizePx());
  applyUiFontFamily(readUiFontFamily());
  loadCodeFontFamily(readCodeFontFamily());
}
