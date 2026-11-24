import { describe, expect, it, vi } from "vitest";
import type { DesignPage } from "./core/designPlatform";
import type { Answer } from "./desktopBridge";
import { desktopDesignPlatform } from "./desktopDesignPlatform";
import type { DesignOverlay } from "./desktopOverlay";

function fakeOverlay() {
  const request = vi.fn(async (op: string, _args?: Record<string, unknown>): Promise<Answer> => ({
    ok: true,
    result: `{"op":"${op}"}`,
  }));
  const capture = vi.fn(async () => ({
    png: "data:image/png;base64,AAAA",
    width: 2,
    height: 2,
    viewport: { width: 2, height: 2 },
  }));
  return { overlay: { request, capture } as unknown as DesignOverlay, request, capture };
}

describe("desktopDesignPlatform", () => {
  it("asks main for each page operation by name, with its checked values", async () => {
    const { overlay, request } = fakeOverlay();
    const page = (await desktopDesignPlatform(overlay).page("tab_1")) as DesignPage;
    expect(await page.hitTest(3, 4)).toBe('{"op":"hitTest"}');
    expect(request).toHaveBeenLastCalledWith("hitTest", { x: 3, y: 4 });
    await page.scroll(1, 2, 0, 40);
    expect(request).toHaveBeenLastCalledWith("scroll", { x: 1, y: 2, dx: 0, dy: 40 });
    await page.scroll(1, 2, 0, 40, ["control"]);
    expect(request).toHaveBeenLastCalledWith("scroll", {
      x: 1,
      y: 2,
      dx: 0,
      dy: 40,
      modifiers: ["control"],
    });
  });

  it("shoots an element a little larger than it, from main's capture of the page alone", async () => {
    const { overlay, capture } = fakeOverlay();
    const file = await desktopDesignPlatform(overlay).capture.element("tab_1", {
      x: 4,
      y: 20,
      width: 30,
      height: 10,
    });
    expect(capture).toHaveBeenCalledWith({ x: 0, y: 12, width: 42, height: 26 });
    expect(file).toMatchObject({ name: "design-element.png", type: "image/png" });
  });
});
