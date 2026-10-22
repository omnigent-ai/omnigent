import { describe, expect, it } from "vitest";
import { isCanvasPathname } from "./canvasNavigation";

describe("Canvas route matching", () => {
  it.each(["", "/ml/omnigent-embed"])("matches only Canvas below %s", (mount) => {
    const canvasPath = `${mount}/canvas`;
    for (const route of ["/canvas", "/canvas/", "/canvas/c/session", "/canvas/c/temp%3Adraft/"]) {
      expect(isCanvasPathname(`${mount}${route}`, canvasPath)).toBe(true);
    }
    for (const route of [
      "/c/canvas",
      "/extensions/example/canvas",
      "/settings/canvas",
      "/canvas/c/",
      "/canvas/c/session/extra",
      "/canvases",
    ]) {
      expect(isCanvasPathname(`${mount}${route}`, canvasPath)).toBe(false);
    }
    expect(isCanvasPathname(`/another-mount${canvasPath}`, canvasPath)).toBe(false);
  });
});
