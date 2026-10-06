// Tests for SlidesViewer: file detection, slide counting, srcdoc injection and
// its script, counter and navigation (buttons, keys, iframe messages), empty
// state, Source toggle, print, and fullscreen visibility.

import { act, cleanup, createEvent, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  HTML_PREVIEW_HEAD,
  HTML_PREVIEW_SANDBOX,
  SLIDES_MSG_SOURCE,
  countSlideSections,
  isSlidesFile,
  prepareSlidesDoc,
} from "./codeViewerHelpers";
import { MAX_SLIDE_COUNT, SlidesViewer, isIgnoredNavKey } from "./SlidesViewer";

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
