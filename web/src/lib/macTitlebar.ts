import { isMacElectronShell } from "./nativeBridge";

/** The macOS shell's title-bar strip in window pixels; mirrors `--mac-titlebar-height`. */
const MAC_TITLEBAR_WINDOW_PX = 48;

type Side = "top" | "right" | "bottom" | "left";
export type CollisionPadding = number | Partial<Record<Side, number>>;

/**
 * Popper collision padding that keeps floating menus below the macOS title bar.
 * The native traffic lights and the title-bar actions paint over anything there.
 */
export function withMacTitlebarClearance(padding: CollisionPadding = 0): CollisionPadding {
  if (!isMacElectronShell()) return padding;
  const zoom = Number.parseFloat(
    document.documentElement.style.getPropertyValue("--omnigent-electron-zoom"),
  );
  const titlebarHeight = MAC_TITLEBAR_WINDOW_PX / (zoom > 0 ? zoom : 1);
  const sides =
    typeof padding === "number"
      ? { top: padding, right: padding, bottom: padding, left: padding }
      : padding;
  return { ...sides, top: titlebarHeight + (sides.top ?? 0) };
}
