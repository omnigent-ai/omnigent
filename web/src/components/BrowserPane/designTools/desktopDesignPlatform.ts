// The desktop app's design-tool platform (core/designPlatform.ts), one for each Browser view
// while its toolbar is mounted. Each page operation is a request to main, which builds and
// runs the page's script itself; screenshots are main's captures of the page alone.
import type { DesignPage, DesignPlatform } from "./core/designPlatform";
import type { CssRect, Shape } from "./core/designToolsStore";
import { drawingScreenshot, elementScreenshot } from "./desktopCapture";
import type { DesignOverlay } from "./desktopOverlay";

/** The platform of the Browser view `overlay` shows its tools on. */
export function desktopDesignPlatform(overlay: DesignOverlay): DesignPlatform {
  const result = async (op: string, args: Record<string, unknown>) =>
    (await overlay.request(op, args)).result ?? "";
  const page: DesignPage = {
    hitTest: (x, y) => result("hitTest", { x, y }),
    scroll: (x, y, dx, dy, modifiers = []) =>
      result("scroll", { x, y, dx, dy, ...(modifiers.length > 0 ? { modifiers } : {}) }),
  };
  return {
    page: async () => page,
    capture: {
      screenshot: async (_tabId: string, marks: readonly Shape[]) =>
        drawingScreenshot(await overlay.capture(), marks),
      element: (_tabId: string, rect: CssRect) => elementScreenshot(overlay, rect),
    },
  };
}
