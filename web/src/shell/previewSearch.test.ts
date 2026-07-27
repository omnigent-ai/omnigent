// Unit tests for the rendered-preview find matcher (findTextRanges).
//
// The paint/clear helpers use the CSS Custom Highlight API, which jsdom doesn't
// implement, so those are exercised in the app; here we cover the matcher — the
// risk-bearing logic that walks the preview DOM into DOM Ranges. jsdom provides
// TreeWalker + Range, so ranges round-trip to their matched text.

import { afterEach, describe, expect, it } from "vitest";
import { findTextRanges } from "./previewSearch";

let container: HTMLElement | null = null;

function mount(html: string): HTMLElement {
  container = document.createElement("div");
  container.innerHTML = html;
  document.body.appendChild(container);
  return container;
}

afterEach(() => {
  container?.remove();
  container = null;
});

describe("findTextRanges", () => {
  it("returns [] for an empty query", () => {
    expect(findTextRanges(mount("<p>hello world</p>"), "")).toEqual([]);
  });

  it("returns [] when nothing matches", () => {
    expect(findTextRanges(mount("<p>hello world</p>"), "zzz")).toEqual([]);
  });

  it("finds all case-insensitive occurrences and each range maps to the match text", () => {
    const el = mount("<p>The quick brown fox jumps over the lazy dog.</p>");
    const ranges = findTextRanges(el, "the");
    expect(ranges).toHaveLength(2);
    for (const r of ranges) expect(r.toString().toLowerCase()).toBe("the");
  });

  it("matches a term split across inline formatting within a block", () => {
    // "Hello" spans a text node, a <strong>, and another text node.
    const el = mount("<p>Hel<strong>lo</strong> world</p>");
    const ranges = findTextRanges(el, "hello");
    expect(ranges).toHaveLength(1);
    // The range spans the two text nodes but still reads as the matched text.
    expect(ranges[0].toString()).toBe("Hello");
  });

  it("does not match across a block boundary", () => {
    const el = mount("<p>foo</p><p>bar</p>");
    expect(findTextRanges(el, "foobar")).toHaveLength(0);
    // Each half still matches within its own block.
    expect(findTextRanges(el, "foo")).toHaveLength(1);
    expect(findTextRanges(el, "bar")).toHaveLength(1);
  });

  it("does not merge text across list items", () => {
    const el = mount("<ul><li>ab</li><li>cd</li></ul>");
    expect(findTextRanges(el, "abcd")).toHaveLength(0);
  });

  it("keeps positions aligned after a length-changing case-fold character", () => {
    // "İ" (U+0130) lowercases to two UTF-16 units; a naive toLowerCase() haystack
    // would shift every offset after it. The trailing word must still map exactly.
    const el = mount("<p>İstanbul word</p>");
    const ranges = findTextRanges(el, "word");
    expect(ranges).toHaveLength(1);
    expect(ranges[0].toString()).toBe("word");
  });

  it("finds non-overlapping repeats within a single text node", () => {
    const el = mount("<p>aaa</p>");
    // "aa" advances past each match, so "aaa" yields one non-overlapping hit.
    expect(findTextRanges(el, "aa")).toHaveLength(1);
    expect(findTextRanges(el, "a")).toHaveLength(3);
  });
});
