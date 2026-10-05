// Tests for SlidesViewer: file detection, slide counting, srcdoc injection and
// its script, counter and navigation (buttons, keys, iframe messages), empty
// state, Source toggle, print, and fullscreen visibility.

import { act, cleanup, createEvent, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchFileContent, type FileContentResponse } from "@/hooks/useFileContent";
import {
  HTML_PREVIEW_HEAD,
  DESIGN_KIT_DIR,
  HTML_PREVIEW_SANDBOX,
  SLIDES_MSG_SOURCE,
  buildDesignKitStyle,
  countSlideSections,
  isSlidesFile,
  loadDesignKit,
  parseDesignKit,
  prepareSlidesDoc,
  type KitFile,
} from "./codeViewerHelpers";
import {
  DESIGN_KIT_TIMEOUT_MS,
  MAX_SLIDE_COUNT,
  SlidesViewer,
  isIgnoredNavKey,
} from "./SlidesViewer";

vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));

const DECK = `<!DOCTYPE html>
<html><head><title>Deck</title></head><body>
<section><h1>One</h1></section>
<section><h1>Two</h1></section>
<section><h1>Three</h1></section>
</body></html>`;

const deckFrame = () => screen.getByTitle("Slide deck") as HTMLIFrameElement;

function spyOnFrame() {
  const spy = vi.fn();
  deckFrame().contentWindow!.postMessage = spy;
  return spy;
}

function fromFrame(
  data: Record<string, unknown>,
  source: Window | null = deckFrame().contentWindow,
) {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", { data: { source: SLIDES_MSG_SOURCE, ...data }, source }),
    );
  });
}

const keyFromFrame = (key: string, source?: Window | null) =>
  fromFrame({ type: "key", key }, source);

const body = (inner: string) => `<html><body>${inner}</body></html>`;

// Runs the injected deck script against a stub window so its guards are testable.
function runDeckScript(inner: string) {
  const doc = document.implementation.createHTMLDocument("deck");
  doc.body.innerHTML = inner;
  const listeners: Record<string, (e: unknown) => void> = {};
  const parent = { postMessage: vi.fn() };
  const html = prepareSlidesDoc(body(inner));
  const script = html.slice(html.lastIndexOf("<script>") + 8, html.lastIndexOf("</script>"));
  new Function("document", "addEventListener", "parent", "print", script)(
    doc,
    (type: string, fn: (e: unknown) => void) => (listeners[type] = fn),
    parent,
    vi.fn(),
  );
  return { doc, listeners, parent };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("isSlidesFile", () => {
  it.each(["deck.slides.html", "a/b/Talk.SLIDES.HTML"])("matches %s", (p) => {
    expect(isSlidesFile(p)).toBe(true);
  });
  it.each(["index.html", "slides.html", "deck.slides.htm", "deck.slides.html.bak"])(
    "rejects %s",
    (p) => {
      expect(isSlidesFile(p)).toBe(false);
    },
  );
});

describe("countSlideSections", () => {
  it.each([
    ["direct children of body", "<section>a</section><section>b</section>", 2],
    ["a nested section as one slide", "<section>a<section>inner</section></section>", 1],
    [
      "a template section as none",
      "<template><section>t</section></template><section>a</section>",
      1,
    ],
    ["a commented-out section as none", "<!-- <section>x</section> --><section>a</section>", 1],
    [
      "sections wrapped in <main> as none",
      "<main><section>a</section><section>b</section></main>",
      0,
    ],
  ])("counts %s", (_label, inner, expected) => {
    expect(countSlideSections(body(inner))).toBe(expected);
  });
});

describe("prepareSlidesDoc", () => {
  it("injects the slide CSS/script before </body> and keeps the deck intact", () => {
    const doc = prepareSlidesDoc(DECK);
    expect(doc).toContain('<base target="_blank">');
    expect(doc).toContain("@media print");
    expect(doc.indexOf("<script>")).toBeLessThan(doc.indexOf("</body>"));
    expect(doc).toContain("<section><h1>Three</h1></section>");
  });

  it("appends the injection to a bare fragment with no </body>", () => {
    const doc = prepareSlidesDoc("<section>a</section>");
    expect(doc.startsWith(`${HTML_PREVIEW_HEAD}<section>a</section><style>`)).toBe(true);
    expect(doc.endsWith("</script>")).toBe(true);
  });

  it("ignores a </body> inside a comment and uses the last real one", () => {
    const doc = prepareSlidesDoc("<html><body><section>a</section></body><!-- </body> --></html>");
    expect(doc.indexOf("</script>")).toBeLessThan(doc.indexOf("</body><!--"));
  });

  it("falls back to appending when the only </body> is commented out", () => {
    const doc = prepareSlidesDoc("<section>a</section><!-- </body> -->");
    expect(doc.endsWith("</script>")).toBe(true);
  });
});

describe("injected deck script", () => {
  const nav = (key: string, extra: Record<string, unknown> = {}) => ({
    key,
    defaultPrevented: false,
    preventDefault: vi.fn(),
    target: null,
    ...extra,
  });

  it("shows the first section and reports the runtime count", () => {
    const { doc, parent } = runDeckScript("<section>a</section><section>b</section>");
    expect(doc.querySelectorAll("[data-omnigent-active]")).toHaveLength(1);
    expect(parent.postMessage).toHaveBeenCalledWith(
      { source: SLIDES_MSG_SOURCE, type: "count", total: 2 },
      "*",
    );
  });

  it("forwards plain nav keys and ignores modified, prevented, or editable ones", () => {
    const { doc, listeners, parent } = runDeckScript("<section><input /></section>");
    parent.postMessage.mockClear();
    const input = doc.querySelector("input");
    for (const e of [
      nav("ArrowRight", { altKey: true }),
      nav("ArrowRight", { metaKey: true }),
      nav("ArrowRight", { ctrlKey: true }),
      nav("ArrowRight", { defaultPrevented: true }),
      nav("ArrowRight", { target: input }),
      nav("Enter"),
    ]) {
      listeners.keydown(e);
    }
    expect(parent.postMessage).not.toHaveBeenCalled();
    listeners.keydown(nav("PageDown"));
    expect(parent.postMessage).toHaveBeenCalledWith(
      { source: SLIDES_MSG_SOURCE, type: "key", key: "PageDown" },
      "*",
    );
  });
});

describe("SlidesViewer", () => {
  it("renders the deck in the HTML preview sandbox", () => {
    render(<SlidesViewer content={DECK} />);
    expect(deckFrame()).toHaveAttribute("sandbox", HTML_PREVIEW_SANDBOX);
    expect(HTML_PREVIEW_SANDBOX).not.toContain("allow-same-origin");
  });

  it("steps through slides with the buttons and posts goto to the iframe", () => {
    render(<SlidesViewer content={DECK} />);
    const post = spyOnFrame();
    const prev = screen.getByRole("button", { name: "Previous slide" });
    const next = screen.getByRole("button", { name: "Next slide" });
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    expect(prev).toBeDisabled();

    fireEvent.click(next);
    expect(screen.getByText("2 / 3")).toBeInTheDocument();
    expect(post).toHaveBeenLastCalledWith(
      { source: SLIDES_MSG_SOURCE, type: "goto", index: 1 },
      "*",
    );

    fireEvent.click(next);
    expect(screen.getByText("3 / 3")).toBeInTheDocument();
    expect(next).toBeDisabled();
    fireEvent.click(prev);
    expect(screen.getByText("2 / 3")).toBeInTheDocument();
  });

  it("navigates with arrow and page keys when the viewer is focused", () => {
    render(<SlidesViewer content={DECK} />);
    const viewer = screen.getByRole("region", { name: "Slide deck" });
    fireEvent.keyDown(viewer, { key: "ArrowRight" });
    fireEvent.keyDown(viewer, { key: "PageDown" });
    expect(screen.getByText("3 / 3")).toBeInTheDocument();
    fireEvent.keyDown(viewer, { key: "ArrowRight" });
    expect(screen.getByText("3 / 3")).toBeInTheDocument();
    fireEvent.keyDown(viewer, { key: "PageUp" });
    fireEvent.keyDown(viewer, { key: "ArrowLeft" });
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
  });

  it("accepts forwarded keys only from its own iframe", () => {
    render(<SlidesViewer content={DECK} />);
    keyFromFrame("ArrowRight", window);
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    keyFromFrame("ArrowRight");
    expect(screen.getByText("2 / 3")).toBeInTheDocument();
  });

  it("asks the iframe to print", () => {
    render(<SlidesViewer content={DECK} />);
    const post = spyOnFrame();
    fireEvent.click(screen.getByRole("button", { name: "Print / Save as PDF" }));
    expect(post).toHaveBeenCalledWith({ source: SLIDES_MSG_SOURCE, type: "print" }, "*");
  });

  it("shows a friendly empty state for a deck with no sections", () => {
    render(<SlidesViewer content="<html><body><p>hi</p></body></html>" />);
    expect(screen.getByText("No slides yet")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Next slide" })).not.toBeInTheDocument();
  });

  it("shows the truncated banner in the empty state", () => {
    render(<SlidesViewer content="<p>hi</p>" truncated />);
    expect(screen.getByText("No slides yet")).toBeInTheDocument();
    expect(screen.getByText(/truncated/i)).toBeInTheDocument();
  });

  it("renders the deck when the runtime count arrives for a source count of 0", () => {
    render(<SlidesViewer content="<p>built by script</p>" />);
    expect(screen.getByText("No slides yet")).toBeInTheDocument();
    fromFrame({ type: "count", total: 2 });
    expect(screen.queryByText("No slides yet")).not.toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
  });

  it("prefers the runtime count and ignores counts from other windows", () => {
    render(<SlidesViewer content={DECK} />);
    fromFrame({ type: "count", total: 9 }, window);
    fromFrame({ type: "count", total: "5" });
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    fromFrame({ type: "count", total: 5 });
    expect(screen.getByText("1 / 5")).toBeInTheDocument();
  });

  it("caps a huge runtime count and ignores non-integer or negative totals", () => {
    render(<SlidesViewer content={DECK} />);
    fromFrame({ type: "count", total: 1_000_000_000 });
    expect(screen.getByText(`1 / ${MAX_SLIDE_COUNT}`)).toBeInTheDocument();
    fromFrame({ type: "count", total: 1.5 });
    fromFrame({ type: "count", total: -1 });
    fromFrame({ type: "count", total: Number.POSITIVE_INFINITY });
    expect(screen.getByText(`1 / ${MAX_SLIDE_COUNT}`)).toBeInTheDocument();
  });

  it("caps the source section count before the iframe reports", () => {
    const sections = Array.from(
      { length: MAX_SLIDE_COUNT + 500 },
      (_, i) => `<section>${i}</section>`,
    ).join("");
    render(<SlidesViewer content={body(sections)} />);
    expect(screen.getByText(`1 / ${MAX_SLIDE_COUNT}`)).toBeInTheDocument();
    expect(screen.queryByText(`1 / ${MAX_SLIDE_COUNT + 500}`)).toBeNull();
  });

  it("clamps when the deck shrinks so the next keypress still moves", () => {
    render(<SlidesViewer content={DECK} />);
    const viewer = screen.getByRole("region", { name: "Slide deck" });
    fireEvent.keyDown(viewer, { key: "ArrowRight" });
    fireEvent.keyDown(viewer, { key: "ArrowRight" });
    expect(screen.getByText("3 / 3")).toBeInTheDocument();
    fromFrame({ type: "count", total: 2 });
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
    fireEvent.keyDown(viewer, { key: "ArrowLeft" });
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
  });

  it("ignores modified, prevented, and editable-target keys", () => {
    render(<SlidesViewer content={DECK} />);
    const viewer = screen.getByRole("region", { name: "Slide deck" });
    for (const mod of ["altKey", "metaKey", "ctrlKey"]) {
      fireEvent.keyDown(viewer, { key: "ArrowRight", [mod]: true });
    }
    const prevented = createEvent.keyDown(viewer, { key: "ArrowRight", cancelable: true });
    prevented.preventDefault();
    fireEvent(viewer, prevented);
    const editor = document.createElement("div");
    editor.setAttribute("contenteditable", "true");
    viewer.appendChild(editor);
    fireEvent.keyDown(editor, { key: "ArrowRight" });
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    fireEvent.keyDown(viewer, { key: "ArrowRight" });
    expect(screen.getByText("2 / 3")).toBeInTheDocument();
  });

  it("isIgnoredNavKey treats form fields as editable", () => {
    const base = { defaultPrevented: false, altKey: false, metaKey: false, ctrlKey: false };
    for (const tag of ["input", "textarea", "select"]) {
      expect(isIgnoredNavKey({ ...base, target: document.createElement(tag) })).toBe(true);
    }
    expect(isIgnoredNavKey({ ...base, target: document.createElement("button") })).toBe(false);
  });

  it("Source toggle hands back to the file viewer's source view", () => {
    const onRequestSourceMode = vi.fn();
    render(<SlidesViewer content={DECK} onRequestSourceMode={onRequestSourceMode} />);
    fireEvent.click(screen.getByRole("button", { name: "View deck source" }));
    expect(onRequestSourceMode).toHaveBeenCalledTimes(1);
  });

  describe("fullscreen", () => {
    // jsdom has no Fullscreen API, so each test defines the flag itself.
    const setFullscreenEnabled = (value: boolean) =>
      Object.defineProperty(document, "fullscreenEnabled", { value, configurable: true });
    afterEach(() => {
      delete (document as { fullscreenEnabled?: boolean }).fullscreenEnabled;
    });

    it("hides the toggle when the Fullscreen API is unsupported", () => {
      setFullscreenEnabled(false);
      render(<SlidesViewer content={DECK} />);
      expect(screen.queryByRole("button", { name: /fullscreen/i })).not.toBeInTheDocument();
    });

    it("shows the toggle when the Fullscreen API is supported", () => {
      setFullscreenEnabled(true);
      render(<SlidesViewer content={DECK} />);
      expect(screen.getByRole("button", { name: "Enter fullscreen" })).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Design kit
// ---------------------------------------------------------------------------

const text = (content: string): KitFile => ({ encoding: "utf-8", content, bytes: content.length });
const bin = (content: string, bytes = 3): KitFile => ({ encoding: "base64", content, bytes });
const MB2 = 2 * 1024 * 1024;

const FULL_KIT = {
  name: "Acme",
  colors: {
    primary: "#ff0066",
    secondary: "rgb(0, 0, 0)",
    accent: "hsl(200 50% 50%)",
    background: "#fafafa",
    text: "navy",
  },
  fonts: {
    heading: { family: "Acme Sans, sans-serif", src: "fonts/acme.woff2", weight: 700 },
    body: { family: "'Georgia', serif" },
  },
  logo: { src: "logo.svg" },
  css: "layouts.css",
};

const KIT_FILES: Record<string, KitFile> = {
  "kit.json": text(JSON.stringify(FULL_KIT)),
  "fonts/acme.woff2": bin("AAAA"),
  "logo.svg": text("<svg/>"),
  "layouts.css": text(".layout-title{text-align:center}"),
};

const kitReader = (files: Record<string, KitFile>) => async (path: string) =>
  files[path.slice(DESIGN_KIT_DIR.length + 1)] ?? null;

const kitJson = (patch: Record<string, unknown>) => JSON.stringify({ ...FULL_KIT, ...patch });

describe("parseDesignKit", () => {
  it("parses a full kit, quoting families and defaulting the logo position", () => {
    const kit = parseDesignKit(JSON.stringify(FULL_KIT));
    expect(kit.name).toBe("Acme");
    expect(kit.colors).toEqual(FULL_KIT.colors);
    expect(kit.fonts.heading).toEqual({
      family: '"Acme Sans", sans-serif',
      src: "fonts/acme.woff2",
      weight: "700",
    });
    expect(kit.fonts.body).toEqual({ family: '"Georgia", serif' });
    expect(kit.logo).toEqual({ src: "logo.svg", position: "bottom-right" });
    expect(kit.css).toBe("layouts.css");
  });

  it("accepts a kit with only a name", () => {
    expect(parseDesignKit('{"name":"Min"}')).toEqual({ name: "Min", colors: {}, fonts: {} });
  });

  it.each([
    ["bad JSON", "{", /not valid JSON/],
    ["a non-object", "[]", /must be a JSON object/],
    ["a missing name", '{"colors":{}}', /needs a "name"/],
    [
      "a style breakout color",
      kitJson({ colors: { primary: "red}</style><b>" } }),
      /colors\.primary is not a valid CSS color/,
    ],
    [
      "a malformed hex color",
      kitJson({ colors: { text: "#12345" } }),
      /colors\.text is not a valid CSS color/,
    ],
    [
      "a breakout font family",
      kitJson({ fonts: { body: { family: 'X";}body{' } } }),
      /fonts\.body\.family is not a valid font family/,
    ],
    [
      "a bad font weight",
      kitJson({ fonts: { body: { family: "X", weight: "heavy" } } }),
      /weight must be/,
    ],
    [
      "a bad logo position",
      kitJson({ logo: { src: "logo.svg", position: "center" } }),
      /logo\.position/,
    ],
    [
      "a non-font font src",
      kitJson({ fonts: { body: { family: "X", src: "x.svg" } } }),
      /fonts\.body\.src must be one of/,
    ],
    ["a non-css stylesheet", kitJson({ css: "layouts.js" }), /css must be one of/],
  ])("rejects %s", (_label, json, reason) => {
    expect(() => parseDesignKit(json)).toThrow(reason);
  });

  it.each([
    "../logo.svg",
    "a/../../logo.svg",
    "./logo.svg",
    "/etc/logo.svg",
    "https://evil.example/logo.svg",
    "//evil.example/logo.svg",
    "C:\\logo.svg",
    "logo.svg?x=1",
    "",
  ])("rejects the asset path %j", (src) => {
    expect(() => parseDesignKit(kitJson({ logo: { src } }))).toThrow(/relative path inside/);
  });
});

describe("loadDesignKit", () => {
  it("is none when kit.json does not exist", async () => {
    expect(await loadDesignKit(kitReader({}))).toEqual({ status: "none" });
  });

  it("embeds fonts, logo, tokens, kit CSS, then the enforced base rules", async () => {
    const kit = await loadDesignKit(kitReader(KIT_FILES));
    expect(kit.status).toBe("ok");
    if (kit.status !== "ok") return;
    const { name, style } = kit;
    expect(name).toBe("Acme");
    expect(style).toContain(
      '@font-face{font-family:"Acme Sans";src:url("data:font/woff2;base64,AAAA");font-weight:700',
    );
    expect(style).toContain("--kit-primary:#ff0066;--kit-secondary:rgb(0, 0, 0)");
    expect(style).toContain(
      "--kit-accent:hsl(200 50% 50%);--kit-background:#fafafa;--kit-text:navy",
    );
    expect(style).toContain(
      '--kit-font-heading:"Acme Sans", sans-serif;--kit-font-body:"Georgia", serif',
    );
    expect(style).toContain(`url("data:image/svg+xml;base64,${btoa("<svg/>")}")`);
    expect(style).toContain("bottom:24px;right:24px");
    expect(style).toContain("background:var(--kit-background)!important");
    expect(style).toContain("font-family:var(--kit-font-heading)!important");
    const order = ["@font-face", ":root{", ".layout-title", "body>section{", "body>section::after"];
    const at = order.map((s) => style.indexOf(s));
    expect(at.every((n) => n >= 0)).toBe(true);
    expect([...at].sort((a, b) => a - b)).toEqual(at);
  });

  it("places the logo by position", () => {
    const style = buildDesignKitStyle(
      parseDesignKit(kitJson({ logo: { src: "logo.png", position: "top-left" } })),
      { "logo.png": "data:image/png;base64,AA==" },
    );
    expect(style).toContain("top:24px;left:24px");
  });

  it.each([
    [
      "an oversize asset",
      { "logo.svg": { ...text("<svg/>"), bytes: MB2 + 1 } },
      /logo\.svg is larger than 2 MB/,
    ],
    [
      "a truncated asset",
      { "fonts/acme.woff2": { ...bin("AAAA"), truncated: true } },
      /acme\.woff2 is larger than 2 MB/,
    ],
    [
      "an oversize kit.json",
      { "kit.json": { ...KIT_FILES["kit.json"], bytes: MB2 + 1 } },
      /kit\.json is larger/,
    ],
    ["a missing asset", { "logo.svg": undefined }, /logo\.svg not found/],
    [
      "a stylesheet that closes <style>",
      { "layouts.css": text("a{}</STYLE><script>") },
      /must not contain/,
    ],
    ["bad base64", { "fonts/acme.woff2": bin('AA")}') }, /not valid base64/],
    ["a binary kit.json", { "kit.json": bin("AAAA") }, /kit\.json is not a text file/],
  ])("errors on %s", async (_label, patch, reason) => {
    const files = { ...KIT_FILES, ...patch } as Record<string, KitFile>;
    const kit = await loadDesignKit(kitReader(files));
    expect(kit.status).toBe("error");
    expect(kit.status === "error" && kit.reason).toMatch(reason);
  });

  it("errors when the read itself fails", async () => {
    const kit = await loadDesignKit(() => Promise.reject(new Error("500 Server Error")));
    expect(kit).toEqual({ status: "error", reason: "500 Server Error" });
  });
});

describe("prepareSlidesDoc with a kit", () => {
  it("injects the kit style after the deck's styles and before the deck script", () => {
    const deck =
      "<html><head><style>body{color:red}</style></head><body><section>a</section></body></html>";
    const doc = prepareSlidesDoc(deck, "<style data-omnigent-kit>x</style>");
    const kitAt = doc.indexOf("data-omnigent-kit");
    expect(kitAt).toBeGreaterThan(doc.indexOf("body{color:red}"));
    expect(kitAt).toBeLessThan(doc.lastIndexOf("<script>"));
    expect(kitAt).toBeLessThan(doc.indexOf("</body>"));
  });

  it.each([
    ["a head", "<html><head><style>body{color:red}</style></head><body><section>a</section>"],
    ["no head", "<!DOCTYPE html><html><body><style>body{color:red}</style><section>a</section>"],
    ["a bare fragment", "<style>body{color:red}</style><section>a</section>"],
  ])("injects a design-system style before the deck's styles in %s", (_label, deck) => {
    const ds = "<style data-omnigent-design-system>x</style>";
    const doc = prepareSlidesDoc(deck, "<style data-omnigent-kit>k</style>", ds);
    const dsAt = doc.indexOf(ds);
    expect(dsAt).toBe(doc.indexOf('<base target="_blank">') + '<base target="_blank">'.length);
    expect(dsAt).toBeLessThan(doc.indexOf("body{color:red}"));
    expect(doc.indexOf("data-omnigent-kit")).toBeGreaterThan(doc.indexOf("body{color:red}"));
  });
});

describe("SlidesViewer design kit", () => {
  const response = (path: string, f: KitFile): FileContentResponse => ({
    object: "session.environment.filesystem.file_content",
    path,
    content_type: null,
    ...f,
  });
  const serve = (files: Record<string, KitFile>, gate?: Promise<void>) =>
    vi.mocked(fetchFileContent).mockImplementation(async (_id, path) => {
      await gate;
      const f = files[path.slice(DESIGN_KIT_DIR.length + 1)];
      if (!f) throw new Error("404 Not Found");
      return response(path, f);
    });
  const srcdoc = () => deckFrame().getAttribute("srcdoc") ?? "";

  it("applies the kit to the deck and shows its name", async () => {
    serve(KIT_FILES);
    render(<SlidesViewer content={DECK} conversationId="conv_1" />);
    expect(await screen.findByTitle("Design kit: Acme")).toBeInTheDocument();
    expect(fetchFileContent).toHaveBeenCalledWith("conv_1", `${DESIGN_KIT_DIR}/kit.json`);
    expect(srcdoc()).toContain("--kit-primary:#ff0066");
    expect(srcdoc()).toContain("<section><h1>Three</h1></section>");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("renders the deck without the kit and names the reason when the kit is invalid", async () => {
    serve({ "kit.json": text("{") });
    render(<SlidesViewer content={DECK} conversationId="conv_1" />);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Design kit not applied: kit.json is not valid JSON",
    );
    expect(srcdoc()).toBe(prepareSlidesDoc(DECK));
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    expect(screen.queryByTitle(/Design kit:/)).not.toBeInTheDocument();
  });

  it("is unchanged when the workspace has no kit", async () => {
    serve({});
    render(<SlidesViewer content={DECK} conversationId="conv_1" />);
    await vi.waitFor(() => expect(srcdoc()).toBe(prepareSlidesDoc(DECK)));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByTitle(/Design kit:/)).not.toBeInTheDocument();
  });

  it("renders the deck unbranded when the kit read hangs past the timeout", () => {
    vi.useFakeTimers();
    try {
      vi.mocked(fetchFileContent).mockReturnValue(new Promise(() => {}));
      render(<SlidesViewer content={DECK} conversationId="conv_1" />);
      expect(srcdoc()).toBe("");
      act(() => vi.advanceTimersByTime(DESIGN_KIT_TIMEOUT_MS + 1));
      expect(srcdoc()).toBe(prepareSlidesDoc(DECK));
      expect(screen.getByRole("status")).toHaveTextContent(
        "Design kit not applied: design kit timed out",
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("drops the previous session's kit until the new one loads", async () => {
    serve(KIT_FILES);
    const { rerender } = render(<SlidesViewer content={DECK} conversationId="conv_1" />);
    expect(await screen.findByTitle("Design kit: Acme")).toBeInTheDocument();

    let release!: () => void;
    serve(
      { ...KIT_FILES, "kit.json": text(kitJson({ name: "Beta" })) },
      new Promise((r) => {
        release = r;
      }),
    );
    rerender(<SlidesViewer content={DECK} conversationId="conv_2" />);
    expect(screen.queryByTitle("Design kit: Acme")).not.toBeInTheDocument();
    expect(srcdoc()).toBe("");
    release();
    expect(await screen.findByTitle("Design kit: Beta")).toBeInTheDocument();

    rerender(<SlidesViewer content={DECK} />);
    expect(screen.queryByTitle(/Design kit:/)).not.toBeInTheDocument();
    expect(srcdoc()).toBe(prepareSlidesDoc(DECK));
  });

  it("keeps the kit on a content refresh of the same session", async () => {
    serve(KIT_FILES);
    const { rerender } = render(<SlidesViewer content={DECK} conversationId="conv_1" />);
    expect(await screen.findByTitle("Design kit: Acme")).toBeInTheDocument();
    const updated = DECK.replace("Three", "Four");
    rerender(<SlidesViewer content={updated} conversationId="conv_1" />);
    expect(screen.getByTitle("Design kit: Acme")).toBeInTheDocument();
    expect(srcdoc()).toContain("--kit-primary:#ff0066");
    expect(srcdoc()).toContain("<h1>Four</h1>");
  });

  it("gives each document a fresh iframe so swaps add no browser history", async () => {
    serve({});
    const { rerender } = render(<SlidesViewer content={DECK} conversationId="conv_1" />);
    const loading = deckFrame();
    await vi.waitFor(() => expect(srcdoc()).toBe(prepareSlidesDoc(DECK)));
    const first = deckFrame();
    expect(first).not.toBe(loading);

    const updated = DECK.replace("Three", "Four");
    rerender(<SlidesViewer content={updated} conversationId="conv_1" />);
    await vi.waitFor(() => expect(srcdoc()).toContain("<h1>Four</h1>"));
    expect(deckFrame()).not.toBe(first);
  });
});
