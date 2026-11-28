// Where Live Edit's editor goes: always on the page, next to the text it edits.
import { describe, expect, it } from "vitest";

import { editorStyle } from "./layerParts";

const layer = { left: 100, top: 50, width: 800, height: 600 };

/** The editor's box in the window, its height at most `maxHeight`. */
function placed(edit: { x: number; y: number; width: number; height: number }) {
  const style = editorStyle(layer, { rect: edit });
  const maxHeight = Number(style.maxHeight);
  const top = Number(style.top);
  const bottom = style.transform === "translateY(-100%)" ? top : top + maxHeight;
  return { top: style.transform ? top - maxHeight : top, bottom, maxHeight, style };
}

describe("editorStyle", () => {
  it("puts the editor under the text when there's room", () => {
    const box = placed({ x: 20, y: 40, width: 100, height: 20 });
    expect(box.style.transform).toBeUndefined();
    expect(box.top).toBe(50 + 40 + 20 + 10);
    expect(box.bottom).toBeLessThanOrEqual(layer.top + layer.height);
  });

  it("puts it over text near the page's bottom, on the page", () => {
    const box = placed({ x: 20, y: 560, width: 100, height: 20 });
    expect(box.style.transform).toBe("translateY(-100%)");
    expect(Number(box.style.top)).toBe(50 + 560 - 10);
    expect(box.top).toBeGreaterThanOrEqual(layer.top);
  });

  it("puts it over text that runs off the page's bottom", () => {
    const box = placed({ x: 20, y: 500, width: 100, height: 400 });
    expect(box.top).toBeGreaterThanOrEqual(layer.top);
    expect(box.bottom).toBeLessThanOrEqual(50 + 500 - 10);
  });

  it("keeps its width at the page's right edge, moving it left onto the page", () => {
    const style = editorStyle(layer, { rect: { x: 780, y: 40, width: 15, height: 20 } });
    expect(style.width).toBe(240);
    expect(Number(style.left) + Number(style.width)).toBeLessThanOrEqual(100 + 800 - 8);
  });

  it("starts on the page for text scrolled off its left edge, no wider than the page", () => {
    const style = editorStyle(
      { ...layer, width: 200 },
      { rect: { x: -300, y: 40, width: 900, height: 20 } },
    );
    expect(style.left).toBe(100 + 8);
    expect(style.width).toBe(200 - 16);
  });

  it("keeps it on the page for text that fills it, inside the text", () => {
    const box = placed({ x: 20, y: -100, width: 100, height: 900 });
    expect(box.top).toBeGreaterThanOrEqual(layer.top);
    expect(box.bottom).toBeLessThanOrEqual(layer.top + layer.height);
  });
});
