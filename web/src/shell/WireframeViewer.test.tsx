// Tests for wireframes: file detection, screen listing, the srcdoc and its
// frame script (screen switches and links), and the WireframeViewer.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchFileContent } from "@/hooks/useFileContent";
import { DESIGN_SYSTEM_POINTER, serializeDesignSystemPointer } from "@/lib/designSystem";
import { getSessionSlim } from "@/lib/sessionsApi";
import { readFixtureFile } from "@/test/designSystemFixture";
import { DESIGN_KIT_DIR, HTML_PREVIEW_SANDBOX, type KitFile } from "./codeViewerHelpers";
import { WireframeViewer, fitScale } from "./WireframeViewer";
import {
  WIREFRAME_DEVICES,
  WIREFRAME_MSG_SOURCE,
  isWireframeFile,
  listWireframeScreens,
  prepareWireframeDoc,
} from "./wireframeDoc";

vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({ getSessionSlim: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

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

describe("fitScale", () => {
  const desktop = WIREFRAME_DEVICES[0];
  const phone = WIREFRAME_DEVICES[2];
  it("scales the device down to fit the container, letterboxed", () => {
    expect(fitScale(720, 900, desktop)).toBe(0.5);
    expect(fitScale(1440, 450, desktop)).toBe(0.5);
  });
  it("never scales above the device size", () => {
    expect(fitScale(2000, 2000, phone)).toBe(1);
  });
  it("is 0 before the container is measured", () => {
    expect(fitScale(0, 0, phone)).toBe(0);
  });
});

describe("WireframeViewer", () => {
  const frame = () => screen.getByTitle("Wireframe") as HTMLIFrameElement;
  const srcdoc = () => frame().getAttribute("srcdoc") ?? "";
  const fromFrame = (
    data: Record<string, unknown>,
    source: Window | null = frame().contentWindow,
  ) =>
    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", { data: { source: WIREFRAME_MSG_SOURCE, ...data }, source }),
      );
    });
  const spyOnFrame = () => {
    const spy = vi.fn();
    frame().contentWindow!.postMessage = spy;
    return spy;
  };

  it("renders in the HTML preview sandbox at the desktop size by default", () => {
    render(<WireframeViewer content={body(SCREENS)} />);
    expect(frame().getAttribute("sandbox")).toBe(HTML_PREVIEW_SANDBOX);
    expect(frame().style.width).toBe("1440px");
    expect(frame().style.height).toBe("900px");
    expect(srcdoc()).toContain(WIREFRAME_MSG_SOURCE);
    expect(screen.getByRole("button", { name: "Desktop" })).toHaveAttribute("aria-pressed", "true");
  });

  it("switches devices and rescales to fit", () => {
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(720);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
    render(<WireframeViewer content={body(SCREENS)} />);
    expect(frame().style.transform).toBe("scale(0.5)");
    fireEvent.click(screen.getByRole("button", { name: "Phone" }));
    expect(screen.getByRole("button", { name: "Phone" })).toHaveAttribute("aria-pressed", "true");
    expect(frame().style.width).toBe("390px");
    expect(frame().style.height).toBe("844px");
    expect(frame().style.transform).toBe(`scale(${600 / 844})`);
    fireEvent.click(screen.getByRole("button", { name: "Tablet" }));
    expect(frame().style.width).toBe("834px");
    expect(frame().style.height).toBe("1194px");
  });

  it("picks screens and posts goto to its iframe", () => {
    render(<WireframeViewer content={body(SCREENS)} />);
    const picker = screen.getByRole("combobox", { name: "Screen" }) as HTMLSelectElement;
    expect(Array.from(picker.options, (o) => o.text)).toEqual(["Home", "Sign in"]);
    const post = spyOnFrame();
    fireEvent.change(picker, { target: { value: "sign-in" } });
    expect(post).toHaveBeenLastCalledWith(
      { source: WIREFRAME_MSG_SOURCE, type: "goto", id: "sign-in" },
      "*",
    );
  });

  it("follows link switches reported by its own iframe only", () => {
    render(<WireframeViewer content={body(SCREENS)} />);
    const picker = screen.getByRole("combobox", { name: "Screen" }) as HTMLSelectElement;
    fromFrame({ type: "screen", id: "sign-in" }, window);
    fromFrame({ type: "screen", id: "missing" });
    expect(picker.value).toBe("home");
    fromFrame({ type: "screen", id: "sign-in" });
    expect(picker.value).toBe("sign-in");
  });

  it("has no screen picker for a file without screens", () => {
    render(<WireframeViewer content={body("<main>One page</main>")} />);
    expect(screen.queryByRole("combobox", { name: "Screen" })).not.toBeInTheDocument();
    expect(srcdoc()).toContain("<main>One page</main>");
  });

  it("hands back to the source view", () => {
    const onSource = vi.fn();
    render(<WireframeViewer content={body(SCREENS)} onRequestSourceMode={onSource} />);
    fireEvent.click(screen.getByRole("button", { name: "View wireframe source" }));
    expect(onSource).toHaveBeenCalled();
  });

  it("shows the fullscreen toggle when the Fullscreen API is supported", () => {
    Object.defineProperty(document, "fullscreenEnabled", { value: true, configurable: true });
    render(<WireframeViewer content={body(SCREENS)} />);
    expect(screen.getByRole("button", { name: "Enter fullscreen" })).toBeInTheDocument();
    delete (document as { fullscreenEnabled?: boolean }).fullscreenEnabled;
  });

  describe("branding", () => {
    const text = (content: string): KitFile => ({
      encoding: "utf-8",
      content,
      bytes: content.length,
    });
    const KIT = {
      name: "Acme",
      colors: { background: "#fafafa", primary: "#ff0066" },
      fonts: { body: { family: "Georgia, serif" } },
      logo: { src: "logo.svg" },
      css: "layouts.css",
    };
    const serve = (files: Record<string, KitFile>) =>
      vi.mocked(fetchFileContent).mockImplementation(async (_id, path) => {
        const f = files[path];
        if (!f) throw new Error("404 Not Found");
        return { ...f, path } as never;
      });
    beforeEach(() => vi.clearAllMocks());

    it("applies kit fonts and tokens without deck section rules or the logo", async () => {
      serve({ [`${DESIGN_KIT_DIR}/kit.json`]: text(JSON.stringify(KIT)) });
      render(<WireframeViewer content={body(SCREENS)} conversationId="conv_1" />);
      expect(await screen.findByTitle("Design kit: Acme")).toBeInTheDocument();
      const doc = srcdoc();
      expect(doc).toContain("--kit-primary:#ff0066");
      expect(doc).toContain('--kit-font-body:"Georgia", serif');
      expect(doc).not.toContain("body>section{");
      expect(doc).not.toContain("::after");
      expect(doc).not.toContain("background:var(--kit-background)!important");
      expect(fetchFileContent).not.toHaveBeenCalledWith("conv_1", `${DESIGN_KIT_DIR}/logo.svg`);
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    });

    it("injects a full design system and inlines ds: assets", async () => {
      const folder = "/Users/me/brand/fixture";
      vi.mocked(getSessionSlim).mockResolvedValue({ permissionLevel: null } as never);
      vi.mocked(fetchFileContent).mockImplementation(async (_id, path) => {
        if (path === DESIGN_SYSTEM_POINTER) {
          const ref = { path: folder, kind: "full" as const, name: "Fixture Brand" };
          return { ...text(serializeDesignSystemPointer(ref)), path } as never;
        }
        const f = path.startsWith(`${folder}/`) && readFixtureFile(path.slice(folder.length + 1));
        if (f) return { ...f, path } as never;
        throw new Error("404 Not Found");
      });
      const content = body('<section data-screen="a"><img src="ds:assets/logo.svg"></section>');
      render(<WireframeViewer content={content} conversationId="conv_1" />);
      expect(await screen.findByTitle("Design system: Fixture Brand")).toBeInTheDocument();
      const doc = srcdoc();
      expect(doc.indexOf("data-omnigent-design-system")).toBeLessThan(doc.indexOf("h1{color:red}"));
      expect(doc).toMatch(/<img src="data:image\/svg\+xml;base64,[^"]+">/);
      expect(doc).not.toContain("data-omnigent-kit");
    });

    it("names the reason when the kit does not apply", async () => {
      serve({ [`${DESIGN_KIT_DIR}/kit.json`]: text("{") });
      render(<WireframeViewer content={body(SCREENS)} conversationId="conv_1" />);
      expect(await screen.findByRole("status")).toHaveTextContent(
        "Design kit not applied: kit.json is not valid JSON",
      );
      expect(srcdoc()).toContain('<section data-screen="home"');
    });
  });
});
