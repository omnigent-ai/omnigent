// Applies the native iOS shell's reported bar footprints to the inset CSS
// variables the layout consumes.
//
// The iOS shell pushes the pixel height of its floating server switcher (top)
// and chat/terminal bar (bottom) over the bridge — the one piece of the inset
// system that CSS/JS cannot compute on its own. This module writes those onto
// `--omnigent-native-top-bar` / `--omnigent-native-bottom-bar`; index.css folds
// them (together with the web-owned visibility flags and `env(safe-area-*)`)
// into `--omnigent-inset-top` / `--omnigent-inset-bottom`, which `<PageScroll>`
// and the scoped iOS rules read.
//
// No-op off the iOS shell: the size vars keep their `0px` defaults, so the same
// inset variables resolve to plain `env(safe-area-*)` (browser, Electron).

import { onNativeInsets } from "@/lib/nativeBridge";

/**
 * Start mirroring native bar footprints into the inset CSS variables. Call once
 * at app startup. Returns an unsubscribe (a no-op outside the iOS shell).
 */
export function initNativeInsets(): () => void {
  return onNativeInsets(({ topBar, bottomBar }) => {
    const root = document.documentElement.style;
    root.setProperty("--omnigent-native-top-bar", `${topBar}px`);
    root.setProperty("--omnigent-native-bottom-bar", `${bottomBar}px`);
  });
}
