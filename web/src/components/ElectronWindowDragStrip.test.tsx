// Unit tests for ElectronWindowDragStrip — the window-drag surface pages
// mounted OUTSIDE the AppShell render so the frameless macOS desktop window
// stays movable (the shell hides the native title bar, so a screen without a
// `-webkit-app-region: drag` element leaves the window impossible to move).
//
// Detection goes through isMacElectronShell(): the `window.omnigentDesktop`
// preload bridge (kind "electron") plus a Macintosh user agent. Each case
// installs/clears those two signals and asserts the strip renders exactly on
// the mac desktop shell — a plain browser, a non-mac Electron shell, and a
// mac Safari page must all get nothing.

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ElectronWindowDragStrip } from "./ElectronWindowDragStrip";

const MAC_UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) omnigent-desktop/1.0.0 Chrome/126.0.0.0 " +
  "Electron/31.0.0 Safari/537.36";
const LINUX_ELECTRON_UA =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) omnigent-desktop/1.0.0 Chrome/126.0.0.0 " +
  "Electron/31.0.0 Safari/537.36";
const MAC_SAFARI_UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 " +
  "(KHTML, like Gecko) Version/17.4 Safari/605.1.15";

function setShell({ bridge, userAgent }: { bridge: boolean; userAgent: string }) {
  const host = window as unknown as { omnigentDesktop?: unknown };
  if (bridge) host.omnigentDesktop = { kind: "electron" };
  else delete host.omnigentDesktop;
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(userAgent);
}

afterEach(() => {
  cleanup();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
  vi.restoreAllMocks();
});

describe("ElectronWindowDragStrip", () => {
  it("renders the drag strip on the macOS Electron shell", () => {
    setShell({ bridge: true, userAgent: MAC_UA });
    const { container } = render(<ElectronWindowDragStrip />);
    const strip = container.querySelector(".electron-standalone-drag-strip");
    expect(strip).not.toBeNull();
    // Decorative window chrome — must stay out of the accessibility tree.
    expect(strip).toHaveAttribute("aria-hidden", "true");
  });

  it("renders nothing in a plain browser (no preload bridge)", () => {
    setShell({ bridge: false, userAgent: MAC_SAFARI_UA });
    const { container } = render(<ElectronWindowDragStrip />);
    expect(container.querySelector(".electron-standalone-drag-strip")).toBeNull();
  });

  it("renders nothing on a non-mac Electron shell (native frame is draggable)", () => {
    setShell({ bridge: true, userAgent: LINUX_ELECTRON_UA });
    const { container } = render(<ElectronWindowDragStrip />);
    expect(container.querySelector(".electron-standalone-drag-strip")).toBeNull();
  });
});
