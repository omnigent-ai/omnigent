// Standalone entry for the desktop update overlay.
//
// The Electron shell owns the auto-update UI so it works regardless of the
// connected server's web-bundle version (an old server that predates
// UpdateBanner must not leave the desktop app unable to say it's out of date).
// This entry mounts the SAME `UpdateBanner` component — reused, not duplicated —
// into a bundle the shell ships and loads in a transparent corner window.
//
// It talks to the shell's updater over the native bridge exposed by the
// overlay's preload (window.omnigentNative.updates), identical to the in-page
// build; only the outer chrome differs (`variant="bare"`, since the shell
// window supplies position/size).

import { createRoot } from "react-dom/client";
import { UpdateBanner } from "./components/UpdateBanner";
import "./index.css";

// Theme: the shell passes `?theme=dark|light` (from nativeTheme). Fall back to
// the OS preference so the card matches the app in either case.
const params = new URLSearchParams(window.location.search);
const theme = params.get("theme");
const dark =
  theme === "dark" ||
  (theme !== "light" && window.matchMedia("(prefers-color-scheme: dark)").matches);
document.documentElement.classList.toggle("dark", dark);

const container = document.getElementById("update-overlay-root");
if (container) {
  createRoot(container).render(<UpdateBanner variant="bare" />);

  // Report the rendered height so the shell can size the transparent window to
  // the card (0 when UpdateBanner renders nothing → shell hides the window).
  const overlay = (
    window as unknown as {
      omnigentUpdateOverlay?: {
        reportHeight?: (h: number) => void;
        onTheme?: (cb: (theme: string) => void) => void;
      };
    }
  ).omnigentUpdateOverlay;
  const report = () => overlay?.reportHeight?.(container.getBoundingClientRect().height);
  new ResizeObserver(report).observe(container);
  report();

  overlay?.onTheme?.((next) => document.documentElement.classList.toggle("dark", next === "dark"));
}
