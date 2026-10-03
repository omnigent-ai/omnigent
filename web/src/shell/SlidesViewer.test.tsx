// Tests for SlidesViewer: file detection, srcdoc injection, counter and
// navigation (buttons, keys, iframe-forwarded keys), empty state, Source toggle,
// print, and fullscreen visibility.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  HTML_PREVIEW_SANDBOX,
  SLIDES_MSG_SOURCE,
  isSlidesFile,
  prepareSlidesDoc,
} from "./codeViewerHelpers";
import { SlidesViewer } from "./SlidesViewer";

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

function keyFromFrame(key: string, source: Window | null = deckFrame().contentWindow) {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { source: SLIDES_MSG_SOURCE, type: "key", key },
        source,
      }),
    );
  });
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

describe("prepareSlidesDoc", () => {
  it("injects the slide CSS/script before </body> and keeps the deck intact", () => {
    const doc = prepareSlidesDoc(DECK);
    expect(doc).toContain('<base target="_blank">');
    expect(doc).toContain("@media print");
    expect(doc.indexOf("<script>")).toBeLessThan(doc.indexOf("</body>"));
    expect(doc).toContain("<section><h1>Three</h1></section>");
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
    expect(screen.queryByTitle("Slide deck")).not.toBeInTheDocument();
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
