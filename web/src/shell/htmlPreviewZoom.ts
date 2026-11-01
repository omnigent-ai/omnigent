// Zoom levels for the file viewer's HTML preview. The preview handles the
// browser zoom shortcuts itself so they scale only the rendered file.

import type { CSSProperties } from "react";

/** Chrome's page-zoom presets, so the preview steps like browser zoom. */
export const HTML_PREVIEW_ZOOM_LEVELS = [
  0.25, 0.33, 0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4, 5,
] as const;

export const HTML_PREVIEW_ZOOM_ACTIONS = ["in", "out", "reset"] as const;
export type HtmlPreviewZoomAction = (typeof HTML_PREVIEW_ZOOM_ACTIONS)[number];

// Tolerates float drift so a level never steps to itself.
const LEVEL_EPSILON = 0.001;

export function isHtmlPreviewZoomAction(value: unknown): value is HtmlPreviewZoomAction {
  return (HTML_PREVIEW_ZOOM_ACTIONS as readonly unknown[]).includes(value);
}

/**
 * The zoom level after `action`: the next preset above or below `current`
 * (clamped to the ends of the list), or 100% for `reset`.
 */
export function nextHtmlPreviewZoom(current: number, action: HtmlPreviewZoomAction): number {
  const levels = HTML_PREVIEW_ZOOM_LEVELS;
  if (action === "reset") return 1;
  if (action === "in") {
    return levels.find((level) => level > current + LEVEL_EPSILON) ?? levels[levels.length - 1];
  }
  return levels.findLast((level) => level < current - LEVEL_EPSILON) ?? levels[0];
}

/**
 * Style for the box wrapping the preview frame. Like browser zoom, the frame
 * lays out at 1/zoom of the pane and is scaled back to fill it, so the
 * document's own CSS and media queries see a zoomed viewport.
 */
export function htmlPreviewZoomStyle(zoom: number): CSSProperties | undefined {
  if (zoom === 1) return undefined;
  return {
    width: `${100 / zoom}%`,
    height: `${100 / zoom}%`,
    transform: `scale(${zoom})`,
    transformOrigin: "0 0",
  };
}

export function formatHtmlPreviewZoom(zoom: number): string {
  return `${Math.round(zoom * 100)}%`;
}
