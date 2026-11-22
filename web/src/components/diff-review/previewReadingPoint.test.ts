import { afterEach, describe, expect, it } from "vitest";
import { characterTop, readPreviewAnchor, readingCharacter } from "./previewReadingPoint";

interface Hooks {
  elementFromPoint?: (x: number, y: number) => Element | null;
  caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
  caretRangeFromPoint?: (x: number, y: number) => Range | null;
}
const hooks = document as unknown as Hooks;
const rangePrototype = Range.prototype as { getBoundingClientRect?: () => DOMRect };
const originalRangeRect = rangePrototype.getBoundingClientRect;

/** Each character sits on its own 20 px line, so a range's top names its start character. */
function layOutCharactersByLine(height = 20) {
  rangePrototype.getBoundingClientRect = function (this: Range) {
    const section = this.startContainer.parentElement?.closest("[data-markdown-diff-section]");
    let before = 0;
    const walker = document.createTreeWalker(section ?? document.body, NodeFilter.SHOW_TEXT);
    for (
      let node = walker.nextNode();
      node && node !== this.startContainer;
      node = walker.nextNode()
    ) {
      before += (node as Text).length;
    }
    return new DOMRect(0, (before + this.startOffset) * 20, 8, height);
  };
}

/** A rich diff preview of `docs/a.md` with one section, its text split across inline elements. */
function preview() {
  const container = document.createElement("div");
  container.innerHTML =
    '<section data-markdown-diff-path="docs/a.md"><div data-markdown-diff-version="3">' +
    '<div data-markdown-diff-section="7"><p>Alpha <em>beta</em> gamma</p></div>' +
    "</div></section>";
  document.body.append(container);
  const section = container.querySelector("[data-markdown-diff-section]")!;
  const emphasis = container.querySelector("em")!.firstChild!;
  hooks.elementFromPoint = () => container.querySelector("p");
  return { container, section, emphasis };
}

afterEach(() => {
  delete hooks.elementFromPoint;
  delete hooks.caretPositionFromPoint;
  delete hooks.caretRangeFromPoint;
  rangePrototype.getBoundingClientRect = originalRangeRect;
  document.body.replaceChildren();
});

describe("reading the reader's place in a rich diff", () => {
  it("names the section and the character under the view's top, across inline elements", () => {
    layOutCharactersByLine();
    const { container, emphasis } = preview();
    // "Alpha " is 6 characters, so "be|ta" is character 8.
    hooks.caretPositionFromPoint = () => ({ offsetNode: emphasis, offset: 2 });
    expect(readPreviewAnchor(container)).toEqual({
      path: "docs/a.md",
      version: "3",
      section: 7,
      offset: 0,
      width: 0,
      text: { offset: 8, top: 160 },
    });
  });

  it("reads the start of the reader's line, not a point past a short line's end", () => {
    layOutCharactersByLine();
    const { container, section, emphasis } = preview();
    section.firstElementChild!.getBoundingClientRect = () => new DOMRect(50, 0, 300, 20);
    const probes: number[] = [];
    hooks.caretPositionFromPoint = (x) => {
      probes.push(x);
      return { offsetNode: emphasis, offset: 0 };
    };
    readPreviewAnchor(container);
    expect(probes).toEqual([51]);
  });

  it("uses caretRangeFromPoint where caretPositionFromPoint isn't available", () => {
    layOutCharactersByLine();
    const { container, emphasis } = preview();
    hooks.caretRangeFromPoint = () => {
      const range = document.createRange();
      range.setStart(emphasis, 1);
      return range;
    };
    expect(readPreviewAnchor(container)?.text).toEqual({ offset: 7, top: 140 });
  });

  it("keeps only the section when the caret can't name a character in it", () => {
    layOutCharactersByLine();
    const { container, section } = preview();
    const outside = document.body.appendChild(document.createTextNode("elsewhere"));
    const cases: Hooks["caretPositionFromPoint"][] = [
      () => null,
      () => ({ offsetNode: outside, offset: 2 }),
      // An element position, between children rather than inside text.
      () => ({ offsetNode: section.querySelector("p")!, offset: 1 }),
    ];
    for (const caret of cases) {
      hooks.caretPositionFromPoint = caret;
      const anchor = readPreviewAnchor(container);
      expect(anchor?.section).toBe(7);
      expect(anchor?.text).toBeUndefined();
    }
    // No caret API at all.
    delete hooks.caretPositionFromPoint;
    expect(readPreviewAnchor(container)).toMatchObject({ section: 7 });
    expect(readPreviewAnchor(container)?.text).toBeUndefined();
  });

  it("keeps only the section when the character has no size on screen", () => {
    layOutCharactersByLine(0);
    const { container, emphasis } = preview();
    hooks.caretPositionFromPoint = () => ({ offsetNode: emphasis, offset: 2 });
    expect(readPreviewAnchor(container)?.text).toBeUndefined();
  });

  /** The preview, scrolled well into, with section 6 in the view's first 24 px above section 7. */
  function sectionInTheTopBand() {
    const { container } = preview();
    const top = document.createElement("div");
    top.setAttribute("data-markdown-diff-section", "6");
    container.querySelector("[data-markdown-diff-version]")!.prepend(top);
    container.querySelector("[data-markdown-diff-path]")!.getBoundingClientRect = () =>
      new DOMRect(0, -500, 800, 5_000);
    const probes: number[] = [];
    hooks.elementFromPoint = (_x, y) => {
      probes.push(y);
      return y < 24 ? top : container.querySelector("p");
    };
    return { container, probes };
  }

  it("reads below the file's sticky header, even before the header is drawn", () => {
    layOutCharactersByLine();
    // Right after a scroll the header isn't drawn yet: its band shows the section it will cover.
    const { container, probes } = sectionInTheTopBand();
    expect(readPreviewAnchor(container, 44)?.section).toBe(7);
    expect(probes).toEqual([1, 48]);
  });

  it("reads the view's first line where no header will cover it", () => {
    layOutCharactersByLine();
    const { container, probes } = sectionInTheTopBand();
    expect(readPreviewAnchor(container)?.section).toBe(6);
    expect(probes).toEqual([1]);
  });

  it("reads nothing where it can't hit-test, or the view's top isn't a versioned section", () => {
    const { container, section } = preview();
    delete hooks.elementFromPoint;
    expect(readPreviewAnchor(container)).toBeNull();
    hooks.elementFromPoint = () => document.body;
    expect(readPreviewAnchor(container)).toBeNull();
    section.parentElement!.removeAttribute("data-markdown-diff-version");
    hooks.elementFromPoint = () => section;
    expect(readPreviewAnchor(container)).toBeNull();
  });
});

describe("finding a character again", () => {
  it("measures the character at an offset, or at the end of the text the one before it", () => {
    layOutCharactersByLine();
    const { section } = preview();
    const length = section.textContent!.length;
    expect(characterTop(section, 8)).toBe(160);
    expect(characterTop(section, length)).toBe((length - 1) * 20);
    expect(characterTop(section, length + 1)).toBeNull();
  });

  it("ignores a caret outside the section it reads", () => {
    layOutCharactersByLine();
    const { section } = preview();
    const other = document.body.appendChild(document.createElement("p"));
    other.textContent = "other";
    hooks.caretPositionFromPoint = () => ({ offsetNode: other.firstChild!, offset: 1 });
    expect(readingCharacter(section, 0, 0)).toBeNull();
  });
});
