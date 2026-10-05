// Tests for wireframes: file detection, screen listing, the srcdoc and its
// frame script (screen switches and links), and the WireframeViewer.

import { describe, expect, it, vi } from "vitest";
import {
  WIREFRAME_DEVICES,
  WIREFRAME_MSG_SOURCE,
  isWireframeFile,
  listWireframeScreens,
  prepareWireframeDoc,
} from "./wireframeDoc";

const body = (inner: string) =>
  `<html><head><style>h1{color:red}</style></head><body>${inner}</body></html>`;

const SCREENS = `<section data-screen="home" data-title="Home"><a href="#sign-in">Sign in</a>
<p id="faq">FAQ</p><a href="#faq">Jump</a><a href="#">Nowhere</a></section>
<section data-screen="sign-in" data-title="Sign in"><button data-goto="home"><span>Back</span></button></section>`;

// Runs the injected frame script against a stub window so its handlers are testable.
function runFrameScript(inner: string) {
  const doc = document.implementation.createHTMLDocument("wireframe");
  doc.body.innerHTML = inner;
  const listeners: Record<string, (e: unknown) => void> = {};
  const parent = { postMessage: vi.fn() };
  const scrollTo = vi.fn();
  const html = prepareWireframeDoc(body(inner));
  const script = html.slice(html.lastIndexOf("<script>") + 8, html.lastIndexOf("</script>"));
  new Function("document", "addEventListener", "parent", "scrollTo", script)(
    doc,
    (type: string, fn: (e: unknown) => void) => (listeners[type] = fn),
    parent,
    scrollTo,
  );
  const active = () =>
    Array.from(doc.querySelectorAll("[data-omnigent-active]"), (s) =>
      s.getAttribute("data-screen"),
    );
  const click = (target: Element) => {
    const e = { target, preventDefault: vi.fn() };
    listeners.click(e);
    return e;
  };
  const message = (data: unknown, source: unknown = parent) => listeners.message({ source, data });
  return { doc, parent, scrollTo, active, click, message };
}

describe("isWireframeFile", () => {
  it.each(["wireframes/app.wireframe.html", "A.WIREFRAME.HTML"])("matches %s", (p) => {
    expect(isWireframeFile(p)).toBe(true);
  });
  it.each(["app.html", "deck.slides.html", "wireframe.html.bak"])("rejects %s", (p) => {
    expect(isWireframeFile(p)).toBe(false);
  });
});

describe("WIREFRAME_DEVICES", () => {
  it("offers desktop, tablet, and phone at their sizes", () => {
    expect(WIREFRAME_DEVICES.map((d) => [d.label, d.width, d.height])).toEqual([
      ["Desktop", 1440, 900],
      ["Tablet", 834, 1194],
      ["Phone", 390, 844],
    ]);
  });
});

describe("listWireframeScreens", () => {
  it("lists top-level data-screen sections with their titles", () => {
    expect(listWireframeScreens(body(SCREENS))).toEqual([
      { id: "home", title: "Home" },
      { id: "sign-in", title: "Sign in" },
    ]);
  });

  it("falls back to the id, skips nested, unnamed, and repeated screens", () => {
    const html = body(
      '<section data-screen="a"><section data-screen="nested"></section></section>' +
        '<section data-screen=""></section><section data-screen="a"></section><section>plain</section>',
    );
    expect(listWireframeScreens(html)).toEqual([{ id: "a", title: "a" }]);
  });

  it("is empty for a file without sections (one screen)", () => {
    expect(listWireframeScreens(body("<main>One page</main>"))).toEqual([]);
  });
});

describe("prepareWireframeDoc", () => {
  it("injects the system style first and the kit style after the wireframe's styles", () => {
    const doc = prepareWireframeDoc(
      body(SCREENS),
      "<style data-omnigent-kit>k</style>",
      "<style data-omnigent-design-system>s</style>",
    );
    const at = (s: string) => doc.indexOf(s);
    expect(at('<base target="_blank">')).toBeGreaterThan(0);
    expect(at("data-omnigent-design-system")).toBeLessThan(at("h1{color:red}"));
    expect(at("data-omnigent-kit")).toBeGreaterThan(at("h1{color:red}"));
    expect(at("data-omnigent-kit")).toBeLessThan(at("<script>"));
    expect(at("<script>")).toBeLessThan(at("</body>"));
    expect(doc).toContain(WIREFRAME_MSG_SOURCE);
  });

  it("hides inactive screens on screen only and adds no slide rules", () => {
    const doc = prepareWireframeDoc(body(SCREENS));
    expect(doc).toContain(
      "@media screen{body>section[data-screen]:not([data-omnigent-active]){display:none!important}}",
    );
    expect(doc).not.toContain("overflow:hidden");
    expect(doc).not.toContain("omnigent-slides");
  });
});

describe("wireframe frame script", () => {
  it("shows the first screen", () => {
    expect(runFrameScript(SCREENS).active()).toEqual(["home"]);
  });

  it("switches screens on #id links and data-goto, telling the parent", () => {
    const f = runFrameScript(SCREENS);
    const e = f.click(f.doc.querySelector('a[href="#sign-in"]')!);
    expect(e.preventDefault).toHaveBeenCalled();
    expect(f.active()).toEqual(["sign-in"]);
    expect(f.parent.postMessage).toHaveBeenCalledWith(
      { source: WIREFRAME_MSG_SOURCE, type: "screen", id: "sign-in" },
      "*",
    );
    f.click(f.doc.querySelector("[data-goto] span")!);
    expect(f.active()).toEqual(["home"]);
    expect(f.scrollTo).toHaveBeenCalledWith(0, 0);
  });

  it("scrolls to an in-page #id and keeps other links from opening a tab", () => {
    const f = runFrameScript(SCREENS);
    const faq = f.doc.getElementById("faq")!;
    faq.scrollIntoView = vi.fn();
    expect(f.click(f.doc.querySelector('a[href="#faq"]')!).preventDefault).toHaveBeenCalled();
    expect(faq.scrollIntoView).toHaveBeenCalled();
    expect(f.click(f.doc.querySelector('a[href="#"]')!).preventDefault).toHaveBeenCalled();
    expect(f.active()).toEqual(["home"]);
    expect(f.parent.postMessage).not.toHaveBeenCalled();
  });

  it("leaves other clicks alone", () => {
    const f = runFrameScript(`${SCREENS}<a href="https://example.com">out</a>`);
    expect(f.click(f.doc.querySelector('a[href^="https"]')!).preventDefault).not.toHaveBeenCalled();
  });

  it("follows goto from the parent only", () => {
    const f = runFrameScript(SCREENS);
    f.message({ source: WIREFRAME_MSG_SOURCE, type: "goto", id: "sign-in" }, {});
    expect(f.active()).toEqual(["home"]);
    f.message({ source: "other", type: "goto", id: "sign-in" });
    expect(f.active()).toEqual(["home"]);
    f.message({ source: WIREFRAME_MSG_SOURCE, type: "goto", id: "sign-in" });
    expect(f.active()).toEqual(["sign-in"]);
  });
});
