import { afterEach, describe, expect, it, vi } from "vitest";
import { newSlantState, slantItalics } from "./ComposerItalicSlant";

/**
 * jsdom has no layout, so these tests stub the geometry the overlay reads:
 * element offsets, the layer's box and per-character range rects (7px glyphs,
 * 20px lines, wrapping after `wrapAt` characters of a text node).
 */
const GLYPH = 7;
const LINE = 20;
const LIFT = 3;

function stubLayout(wrapAt = Infinity) {
  vi.spyOn(document, "createRange").mockImplementation(() => {
    let node: Node | null = null;
    let offset = 0;
    return {
      setStart: (n: Node, o: number) => {
        node = n;
        offset = o;
      },
      setEnd: () => {},
      selectNodeContents: (n: Node) => {
        node = n;
        offset = 0;
      },
      getClientRects: () => {
        const probe = (node?.parentNode as HTMLElement | null)?.classList.contains("md-slant");
        if (probe) return [{ top: LIFT, left: 0 }];
        const line = Number.isFinite(wrapAt) ? Math.floor(offset / wrapAt) : 0;
        const column = offset - line * (Number.isFinite(wrapAt) ? wrapAt : 0);
        return [{ top: line * LINE, left: 100 + column * GLYPH }];
      },
    } as unknown as Range;
  });
}

function layerWith(html: string) {
  const layer = document.createElement("div");
  layer.innerHTML = `<div class="chunk">${html}</div><div data-italic-overlay></div>`;
  document.body.append(layer);
  const overlay = layer.querySelector<HTMLElement>("[data-italic-overlay]")!;
  Object.defineProperty(layer, "clientHeight", { value: 100, configurable: true });
  Object.defineProperty(layer, "clientWidth", { value: 500, configurable: true });
  layer.getBoundingClientRect = () => ({ left: 100, top: 0, width: 500, height: 100 }) as DOMRect;
  for (const element of layer.querySelectorAll<HTMLElement>(".chunk, span")) {
    Object.defineProperty(element, "offsetTop", { value: 0 });
    Object.defineProperty(element, "offsetHeight", { value: LINE });
  }
  const probeRect = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
    this: HTMLElement,
  ) {
    if (this.classList.contains("md-slant")) return { top: 0, left: 0 } as DOMRect;
    return probeRect.call(this);
  });
  return { layer, overlay, state: newSlantState() };
}

afterEach(() => {
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("slantItalics", () => {
  it("draws a skewed copy at the italic text's glyphs and hides the in-flow text", () => {
    stubLayout();
    const { layer, overlay, state } = layerWith(
      'plain <span class="md-deco md-em text-primary">slanted</span> tail',
    );
    slantItalics(layer, overlay, state);
    const copies = Array.from(overlay.children) as HTMLElement[];
    expect(copies).toHaveLength(1);
    expect(copies[0].dataset.text).toBe("slanted");
    // Its other styles carry over; the dotted underline doesn't.
    expect(copies[0].className).toBe("md-deco text-primary md-slant");
    expect(copies[0].style.left).toBe("0px");
    expect(copies[0].style.top).toBe(`${-LIFT}px`);
    expect(layer.querySelector(".md-em")).toHaveAttribute("data-slanted");
    // No text reaches the layer: the copy's text is a pseudo-element's content.
    expect(overlay.textContent).toBe("");
  });

  it("splits a wrapped span into one copy per line fragment", () => {
    stubLayout(4);
    const { layer, overlay, state } = layerWith('<span class="md-em">abcdefghij</span>');
    slantItalics(layer, overlay, state);
    const copies = Array.from(overlay.children) as HTMLElement[];
    expect(copies.map((copy) => copy.dataset.text)).toEqual(["abcd", "efgh", "ij"]);
    expect(copies.map((copy) => copy.style.top)).toEqual(["-3px", "17px", "37px"]);
  });

  it("copies nested bold and hidden markers with their own styles", () => {
    stubLayout();
    const { layer, overlay, state } = layerWith(
      '<span class="md-em">a </span><span class="text-transparent md-em">**</span>' +
        '<span class="md-em md-strong">b</span>',
    );
    slantItalics(layer, overlay, state);
    const classes = Array.from(overlay.children, (copy) => copy.className);
    expect(classes).toEqual(["md-slant", "text-transparent md-slant", "md-strong md-slant"]);
  });

  it("keeps the dotted underline on a span it can't copy exactly (a tab)", () => {
    stubLayout();
    const { layer, overlay, state } = layerWith('<span class="md-em">a\tb</span>');
    slantItalics(layer, overlay, state);
    expect(overlay.children).toHaveLength(0);
    expect(layer.querySelector(".md-em")).not.toHaveAttribute("data-slanted");
  });

  it.each([
    ["Hebrew in the span", '<div><span class="md-em">שלום עולם</span></div>'],
    ["Arabic in the span", '<div><span class="md-em">مرحبا بالعالم</span></div>'],
    ["RTL elsewhere on the line", '<div>שלום <span class="md-em">hello</span></div>'],
    ["a bidi control", '<div><span class="md-em">a\u202eb</span></div>'],
  ])("keeps the dotted underline with %s", (_, html) => {
    stubLayout();
    const { layer, overlay, state } = layerWith(html);
    slantItalics(layer, overlay, state);
    expect(overlay.children).toHaveLength(0);
    expect(layer.querySelector(".md-em")).not.toHaveAttribute("data-slanted");
  });

  it("still slants a left-to-right line next to a right-to-left one", () => {
    stubLayout();
    const { layer, overlay, state } = layerWith(
      '<div>שלום <span class="md-em">rtl line</span></div><div><span class="md-em">ltr</span></div>',
    );
    slantItalics(layer, overlay, state);
    expect(Array.from(overlay.children, (copy) => (copy as HTMLElement).dataset.text)).toEqual([
      "ltr",
    ]);
  });

  it("does nothing when the layer isn't laid out", () => {
    const { layer, overlay, state } = layerWith('<span class="md-em">x</span>');
    Object.defineProperty(layer, "clientHeight", { value: 0 });
    slantItalics(layer, overlay, state);
    expect(overlay.children).toHaveLength(0);
  });

  it("keeps its copies when nothing near the viewport changed", () => {
    stubLayout();
    const { layer, overlay, state } = layerWith('<span class="md-em">same</span>');
    slantItalics(layer, overlay, state);
    const first = overlay.firstElementChild;
    slantItalics(layer, overlay, state);
    expect(overlay.firstElementChild).toBe(first);
    // A changed span (new text or classes) rebuilds.
    layer.querySelector(".md-em")!.className = "md-em text-muted-foreground";
    slantItalics(layer, overlay, state);
    expect(overlay.firstElementChild).not.toBe(first);
  });

  it("rebuilds only the spans that changed, text edited in place included", () => {
    stubLayout();
    const { layer, overlay, state } = layerWith(
      '<div><span class="md-em">one</span></div><div><span class="md-em">two</span></div>',
    );
    slantItalics(layer, overlay, state);
    const [one, two] = Array.from(overlay.children);
    (layer.querySelectorAll(".md-em")[1].firstChild as Text).data = "three";
    slantItalics(layer, overlay, state);
    const copies = Array.from(overlay.children) as HTMLElement[];
    expect(copies).toHaveLength(2);
    expect(copies[0]).toBe(one);
    expect(copies[1]).not.toBe(two);
    expect(copies[1].dataset.text).toBe("three");
    // New copies keep the overlay in draft order.
    (layer.querySelector(".md-em")!.firstChild as Text).data = "first";
    slantItalics(layer, overlay, state);
    expect(Array.from(overlay.children, (copy) => (copy as HTMLElement).dataset.text)).toEqual([
      "first",
      "three",
    ]);
    const [, kept] = Array.from(overlay.children);
    expect(kept).toBe(copies[1]);
    // A span that leaves the layer takes its copy and nothing else.
    const [firstCopy] = Array.from(overlay.children);
    layer.querySelectorAll(".md-em")[1].remove();
    slantItalics(layer, overlay, state);
    expect(Array.from(overlay.children)).toEqual([firstCopy]);
  });

  it("rebuilds when the width changes by a fraction of a pixel", () => {
    stubLayout();
    const { layer, overlay, state } = layerWith('<span class="md-em">wraps</span>');
    slantItalics(layer, overlay, state);
    const first = overlay.firstElementChild;
    // clientWidth rounds to the same 500; the text's wrap can still move.
    layer.getBoundingClientRect = () =>
      ({ left: 100, top: 0, width: 499.8, height: 100 }) as DOMRect;
    slantItalics(layer, overlay, state);
    expect(overlay.firstElementChild).not.toBe(first);
  });

  it("rebuilds a span whose line fragments moved, however little", () => {
    stubLayout();
    const { layer, overlay, state } = layerWith('<span class="md-em">moving</span>');
    const span = layer.querySelector<HTMLElement>(".md-em")!;
    let rects = [{ left: 100, top: 0, width: 42 }];
    span.getClientRects = () => rects as unknown as DOMRectList;
    slantItalics(layer, overlay, state);
    const first = overlay.firstElementChild;
    slantItalics(layer, overlay, state);
    expect(overlay.firstElementChild).toBe(first);
    // Its last word wrapped to the next line, with integer offsets unchanged.
    rects = [
      { left: 100, top: 0, width: 28 },
      { left: 0, top: 20.8, width: 14 },
    ];
    slantItalics(layer, overlay, state);
    expect(overlay.firstElementChild).not.toBe(first);
  });

  it("keeps its copies while the layer scrolls a line at a time", () => {
    stubLayout();
    const { layer, overlay, state } = layerWith('<span class="md-em">steady</span>');
    let scrollTop = 0;
    Object.defineProperty(layer, "scrollTop", { get: () => scrollTop, configurable: true });
    slantItalics(layer, overlay, state);
    const first = overlay.firstElementChild;
    for (scrollTop = LINE; scrollTop <= 2 * LINE; scrollTop += LINE) {
      slantItalics(layer, overlay, state);
      expect(overlay.firstElementChild).toBe(first);
    }
  });

  it("goes back to the underline everywhere when it can't measure", () => {
    stubLayout();
    const { layer, overlay, state } = layerWith(
      '<div><span class="md-em">a</span></div><div><span class="md-em">b</span></div>',
    );
    slantItalics(layer, overlay, state);
    expect(layer.querySelectorAll("[data-slanted]")).toHaveLength(2);
    layer.querySelectorAll(".md-em")[1].className = "md-em text-primary";
    layer.getBoundingClientRect = () => ({ left: 0, top: 0, width: 0, height: 0 }) as DOMRect;
    slantItalics(layer, overlay, state);
    expect(overlay.children).toHaveLength(0);
    expect(layer.querySelectorAll("[data-slanted]")).toHaveLength(0);
  });
});
