import { describe, expect, it } from "vitest";
import {
  formatHtmlPreviewZoom,
  HTML_PREVIEW_ZOOM_LEVELS,
  htmlPreviewZoomStyle,
  isHtmlPreviewZoomAction,
  nextHtmlPreviewZoom,
} from "./htmlPreviewZoom";

describe("nextHtmlPreviewZoom", () => {
  it("steps through the browser presets in both directions", () => {
    expect(nextHtmlPreviewZoom(1, "in")).toBe(1.1);
    expect(nextHtmlPreviewZoom(1.1, "in")).toBe(1.25);
    expect(nextHtmlPreviewZoom(1, "out")).toBe(0.9);
    expect(nextHtmlPreviewZoom(0.9, "out")).toBe(0.8);
  });

  it("clamps at the smallest and largest preset", () => {
    const smallest = HTML_PREVIEW_ZOOM_LEVELS[0];
    const largest = HTML_PREVIEW_ZOOM_LEVELS[HTML_PREVIEW_ZOOM_LEVELS.length - 1];
    expect([smallest, largest]).toEqual([0.25, 5]);
    expect(nextHtmlPreviewZoom(smallest, "out")).toBe(smallest);
    expect(nextHtmlPreviewZoom(largest, "in")).toBe(largest);
  });

  it("moves from an off-preset level to the neighboring preset", () => {
    expect(nextHtmlPreviewZoom(1.2, "in")).toBe(1.25);
    expect(nextHtmlPreviewZoom(1.2, "out")).toBe(1.1);
    expect(nextHtmlPreviewZoom(1.1 + 1e-9, "in")).toBe(1.25);
    expect(nextHtmlPreviewZoom(0.9 - 1e-9, "out")).toBe(0.8);
  });

  it("resets to 100% from any level", () => {
    expect(nextHtmlPreviewZoom(4, "reset")).toBe(1);
    expect(nextHtmlPreviewZoom(0.25, "reset")).toBe(1);
  });
});

describe("htmlPreviewZoomStyle", () => {
  it("adds no style at 100%", () => {
    expect(htmlPreviewZoomStyle(1)).toBeUndefined();
  });

  it("lays the frame out at 1/zoom of the pane and scales it to fill the pane", () => {
    expect(htmlPreviewZoomStyle(1.25)).toEqual({
      width: "80%",
      height: "80%",
      transform: "scale(1.25)",
      transformOrigin: "0 0",
    });
    expect(htmlPreviewZoomStyle(0.5)).toMatchObject({ width: "200%", transform: "scale(0.5)" });
  });
});

describe("HTML preview zoom helpers", () => {
  it("formats the level as a whole percentage", () => {
    expect(formatHtmlPreviewZoom(1.1)).toBe("110%");
    expect(formatHtmlPreviewZoom(0.33)).toBe("33%");
  });

  it("recognizes only the known actions", () => {
    expect(isHtmlPreviewZoomAction("in")).toBe(true);
    expect(isHtmlPreviewZoomAction("reset")).toBe(true);
    expect(isHtmlPreviewZoomAction("zoom")).toBe(false);
    expect(isHtmlPreviewZoomAction(undefined)).toBe(false);
  });
});
