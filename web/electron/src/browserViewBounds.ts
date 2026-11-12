/**
 * Browser preview bounds conversion: renderer CSS px (getBoundingClientRect) →
 * window device-independent px (WebContentsView#setBounds). Identical at zoom 1,
 * diverge after Cmd+/Cmd-. Pure math (no Electron imports), so unit-testable.
 */

import type { Rectangle } from "electron";

/** The placeholder's rect as the renderer measured it, in CSS px. */
export interface RendererBounds extends Rectangle {
  devicePixelRatio?: number;
}

export function normalizeBrowserViewBounds(
  bounds: RendererBounds,
  zoomFactor: number,
  displayScaleFactor: number | null,
): Rectangle;
export function normalizeBrowserViewBounds(
  bounds: RendererBounds | null | undefined,
  zoomFactor: number,
  displayScaleFactor: number | null,
): Rectangle | null | undefined;
export function normalizeBrowserViewBounds(
  bounds: RendererBounds | null | undefined,
  zoomFactor: number,
  displayScaleFactor: number | null,
): Rectangle | null | undefined {
  if (!bounds) return bounds;
  const rendererDpr = bounds.devicePixelRatio;
  const displayScale =
    Number.isFinite(displayScaleFactor) && displayScaleFactor !== null && displayScaleFactor > 0
      ? displayScaleFactor
      : null;
  const scale =
    rendererDpr !== undefined && Number.isFinite(rendererDpr) && rendererDpr > 0 && displayScale
      ? rendererDpr / displayScale
      : Number.isFinite(zoomFactor) && zoomFactor > 0
        ? zoomFactor
        : 1;
  return {
    x: Math.round(bounds.x * scale),
    y: Math.round(bounds.y * scale),
    width: Math.round(bounds.width * scale),
    height: Math.round(bounds.height * scale),
  };
}

export interface BrowserViewBoundsControllerOptions {
  getZoomFactor: () => number;
  getDisplayScaleFactor?: () => number | null;
  setBounds: (bounds: Rectangle) => void;
}

export interface BrowserViewBoundsController {
  setRendererBounds: (bounds: RendererBounds | null) => Rectangle | null;
  resync: () => Rectangle | null;
  clear: () => void;
}

export function createBrowserViewBoundsController({
  getZoomFactor,
  getDisplayScaleFactor = () => null,
  setBounds,
}: BrowserViewBoundsControllerOptions): BrowserViewBoundsController {
  let rendererBounds: RendererBounds | null = null;
  const apply = () => {
    if (!rendererBounds) return null;
    const normalized = normalizeBrowserViewBounds(
      rendererBounds,
      getZoomFactor(),
      getDisplayScaleFactor(),
    );
    setBounds(normalized);
    return normalized;
  };
  return {
    setRendererBounds(bounds) {
      rendererBounds = bounds;
      return apply();
    },
    resync() {
      return apply();
    },
    clear() {
      rendererBounds = null;
    },
  };
}
