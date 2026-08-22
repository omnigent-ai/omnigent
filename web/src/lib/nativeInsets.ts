// Applies the native iOS shell's reported bar footprint to the inset CSS
// variables the layout consumes.
//
// The iOS shell pushes the pixel height of its floating chat/terminal bar
// (bottom) over the bridge — the one piece of the inset system that CSS/JS
// cannot compute on its own. This module writes it onto
// `--omnigent-native-bottom-bar`; index.css folds it (together with the
// web-owned visibility flag and `env(safe-area-*)`) into
// `--omnigent-inset-bottom`, which `<PageScroll>` and the scoped iOS rules
// read.
//
// No-op off the iOS shell: the size var keeps its `0px` default, so the same
// inset variable resolves to plain `env(safe-area-*)` (browser, Electron).

import { onNativeInsets } from "@/lib/nativeBridge";

/**
 * Start mirroring the native bar footprint into the inset CSS variables. Call
 * once at app startup. Returns an unsubscribe (a no-op outside the iOS shell).
 */
export function initNativeInsets(): () => void {
  return onNativeInsets(({ bottomBar }) => {
    document.documentElement.style.setProperty("--omnigent-native-bottom-bar", `${bottomBar}px`);
  });
}
