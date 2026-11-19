// What the design tools need from the platform that shows a Browser tab's page. Everything
// in core/ goes through this; the desktop app's native view supplies one for each of its
// tabs (desktopDesignPlatform.ts).
import type { CssRect, Shape } from "./designToolsStore";

/**
 * A Browser tab's page, as the tools use it. Each operation is one fixed thing done in the
 * page, never a script from the tools; every answer is the page's own data (untrusted).
 */
export interface DesignPage {
  /** The element at a viewport point: JSON `{ element, rect, key }`, or JSON null. */
  hitTest: (x: number, y: number) => Promise<string>;
  /** Scroll what's under a viewport point by `(dx, dy)` CSS pixels, with the wheel's keys held. */
  scroll: (
    x: number,
    y: number,
    dx: number,
    dy: number,
    modifiers?: readonly string[],
  ) => Promise<string>;
}

/** Something done in the page, when its turn comes. */
export type PageOp = (page: DesignPage) => Promise<string>;

/** The tools' screenshots of the page alone (never the tools drawn over it). */
export interface DesignCapture {
  /** The tab's page as shown now with `marks` (in its CSS pixels) on top, as a PNG file. */
  screenshot: (tabId: string, marks: readonly Shape[]) => Promise<File>;
  /** The page round an element (`rect`, in its CSS pixels), as a PNG file. */
  element: (tabId: string, rect: CssRect) => Promise<File>;
}

export interface DesignPlatform {
  /** The tab's page once the tools can work in it, or why not, for the user. */
  page: (tabId: string) => Promise<DesignPage | string>;
  capture: DesignCapture;
}

const tabs = new Map<string, DesignPlatform>();

/** `platform` shows tab `tabId` (until the returned unregister). */
export function setTabDesignPlatform(tabId: string, platform: DesignPlatform): () => void {
  tabs.set(tabId, platform);
  return () => {
    if (tabs.get(tabId) === platform) tabs.delete(tabId);
  };
}

/** The platform showing `tabId`'s page. */
export function designPlatform(tabId: string): DesignPlatform {
  const platform = tabs.get(tabId);
  if (!platform) throw new Error("No platform shows the Browser tab's page for the design tools.");
  return platform;
}
