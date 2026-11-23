import { afterEach, describe, expect, it, vi } from "vitest";
import { rowInView, scrollRowIntoView } from "./revealScroll";

/** A tree root whose viewport spans `top`..`bottom`, holding rows at the given spans. */
function treeHost(
  view: { top: number; bottom: number },
  rows: { path: string; top: number }[],
): HTMLElement {
  const host = document.createElement("div");
  host.setAttribute("data-file-tree", "");
  const place = (element: Element, top: number, bottom: number) => {
    element.getBoundingClientRect = () =>
      ({ top, bottom, height: bottom - top, left: 0, right: 100, width: 100 }) as DOMRect;
  };
  place(host, view.top, view.bottom);
  for (const row of rows) {
    const element = document.createElement("button");
    element.setAttribute("role", "treeitem");
    element.setAttribute("data-item-path", row.path);
    place(element, row.top, row.top + 30);
    host.append(element);
  }
  document.body.append(host);
  return host;
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("rowInView", () => {
  it("counts a row wholly inside the viewport", () => {
    const host = treeHost({ top: 100, bottom: 400 }, [{ path: "a/OWNERS", top: 200 }]);
    expect(rowInView(host, "a/OWNERS")).toBe(true);
  });

  it("doesn't count a row cut off at either edge, or one not drawn", () => {
    const host = treeHost({ top: 100, bottom: 400 }, [
      { path: "above", top: 80 },
      { path: "below", top: 390 },
    ]);
    expect(rowInView(host, "above")).toBe(false);
    expect(rowInView(host, "below")).toBe(false);
    expect(rowInView(host, "missing")).toBe(false);
  });
});

describe("scrollRowIntoView", () => {
  it("leaves a row already in view where it is", () => {
    const host = treeHost({ top: 100, bottom: 400 }, [{ path: "a/OWNERS", top: 200 }]);
    const model = { scrollToPath: vi.fn() };
    scrollRowIntoView(model, host, "a/OWNERS");
    expect(model.scrollToPath).not.toHaveBeenCalled();
  });

  it("centres a row out of view, or with no tree drawn yet", () => {
    const host = treeHost({ top: 100, bottom: 400 }, []);
    const model = { scrollToPath: vi.fn() };
    scrollRowIntoView(model, host, "a/OWNERS");
    scrollRowIntoView(model, null, "a/OWNERS");
    expect(model.scrollToPath).toHaveBeenCalledTimes(2);
    expect(model.scrollToPath).toHaveBeenCalledWith("a/OWNERS", { offset: "center", focus: false });
  });
});
