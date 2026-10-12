import { describe, expect, it } from "vitest";
import { tokenizeComposerMarkdown } from "./composerMarkdown";
import {
  compactLayout,
  compactToSource,
  sourceToCompact,
  type CompactLayout,
} from "./composerCompact";

const layoutOf = (draft: string) => compactLayout(tokenizeComposerMarkdown(draft));
const shown = (layout: CompactLayout) => layout.pieces.map((piece) => piece.text).join("");

/** The draft with `|` where a click on the preview at `|` in `preview` puts the caret. */
function clicked(draft: string, preview: string): string {
  const layout = layoutOf(draft);
  const at = preview.indexOf("|");
  expect(shown(layout)).toBe(preview.replace("|", ""));
  const source = compactToSource(layout, at);
  return draft.slice(0, source) + "|" + draft.slice(source);
}

describe("compactLayout", () => {
  it("drops every hidden marker", () => {
    expect(shown(layoutOf("x **bold** and *it* and __b__ and _i_ y"))).toBe(
      "x bold and it and b and i y",
    );
    expect(shown(layoutOf("run `code` and ``a`b`` now"))).toBe("run code and a`b now");
    expect(shown(layoutOf("~~gone~~ and ~one~"))).toBe("gone and one");
    // A link keeps its destination, dimmed, as the live layer does.
    expect(shown(layoutOf("see [docs](http://x) now"))).toBe("see docshttp://x now");
  });

  it("drops a quote's `>` and the space after it, and nothing more", () => {
    const layout = layoutOf("> a\n>  b\n> > c");
    expect(shown(layout)).toBe("a\n b\nc");
    // Spaces after emphasis markers inside a quote stay.
    expect(shown(layoutOf("> *slanted **bold** words* inside"))).toBe("slanted bold words inside");
    expect(layout.blocks[0].lines.map((line) => line.quoted)).toEqual([true, true, true]);
  });

  it("keeps what isn't a marker: unclosed markers, escapes, open fences", () => {
    for (const draft of ["**open", "_open", "`open", "\\*not\\*", "snake_case_name", "2 * 3 * 4"]) {
      const layout = layoutOf(draft);
      expect(shown(layout)).toBe(draft);
      expect(layout.collapsed).toBe(false);
    }
    const open = layoutOf("```ts\nno end");
    expect(shown(open)).toBe("```ts\nno end");
    expect(open.blocks.map((block) => block.code)).toEqual([true]);
  });

  it("collapses a closed fence's lines, keeping its info string as a header", () => {
    const layout = layoutOf("a\n```ts\ncode\n```\nb");
    expect(shown(layout)).toBe("a\nts\ncode\nb");
    expect(layout.blocks.map((block) => [block.code, block.lines.length])).toEqual([
      [false, 1],
      [true, 2],
      [false, 1],
    ]);
    const bare = layoutOf("```\ncode\n\nmore\n```");
    expect(shown(bare)).toBe("code\n\nmore\n");
    // Blank code lines stay; only the fence lines go.
    expect(bare.blocks[0].lines.map((line) => line.pieces.map((p) => p.text).join(""))).toEqual([
      "code\n",
      "\n",
      "more\n",
    ]);
  });

  it("collapses fence lines inside quotes and list items too", () => {
    expect(shown(layoutOf("> ```\n> q\n> ```\nafter"))).toBe("q\nafter");
    // Code keeps its own indent.
    expect(shown(layoutOf("   ```\n   indented\n   ```"))).toBe("   indented\n");
    // A list item's marker isn't hidden, so its fence line stays for it.
    expect(shown(layoutOf("- ```\n  code\n  ```"))).toBe("- \n  code\n");
  });

  it("keeps a quote's empty line, so its paragraphs stay apart", () => {
    expect(shown(layoutOf("> a\n>\n> b"))).toBe("a\n\nb");
  });

  it("says when nothing is hidden", () => {
    expect(layoutOf("plain text\nsecond line").collapsed).toBe(false);
    expect(layoutOf("x **b**").collapsed).toBe(true);
  });
});

describe("compact offsets", () => {
  it("map both ways inside visible text", () => {
    const draft = "x **bold** `code` [a](u) > ~~s~~";
    const layout = layoutOf(draft);
    for (const piece of layout.pieces) {
      for (let i = 0; i < piece.text.length; i++) {
        expect(compactToSource(layout, piece.compact + i)).toBe(
          i === 0 ? compactToSource(layout, piece.compact) : piece.source + i,
        );
        expect(sourceToCompact(layout, piece.source + i)).toBe(piece.compact + i);
      }
    }
  });

  it("round-trip every preview offset", () => {
    for (const draft of [
      "x **bold** y",
      "> a\n> b",
      "a\n```ts\ncode\n```\nb",
      "see [docs](http://x) now",
      "**a**_b_ `c` ~~d~~",
      "\\*not\\* **open",
    ]) {
      const layout = layoutOf(draft);
      for (let at = 0; at <= layout.length; at++)
        expect(sourceToCompact(layout, compactToSource(layout, at))).toBe(at);
    }
  });

  it("put a caret beside hidden markers on the side with less formatting", () => {
    // After a bold word, typing continues plain text; before it, too.
    expect(clicked("x **bold** y", "x bold| y")).toBe("x **bold**| y");
    expect(clicked("x **bold** y", "x |bold y")).toBe("x |**bold** y");
    expect(clicked("Now **bold**", "Now bold|")).toBe("Now **bold**|");
    expect(clicked("**bold** x", "|bold x")).toBe("|**bold** x");
    expect(clicked("x `code` y", "x |code y")).toBe("x |`code` y");
    expect(clicked("x `code` y", "x code| y")).toBe("x `code`| y");
    expect(clicked("x ~~s~~", "x s|")).toBe("x ~~s~~|");
    // A link's destination counts most: just after the text stays in the text.
    expect(clicked("[docs](u) x", "docs|u x")).toBe("[docs|](u) x");
    expect(clicked("[docs](u) x", "docsu| x")).toBe("[docs](u)| x");
  });

  it("put a caret after a quote's `>` and past a collapsed fence line", () => {
    expect(clicked("> a\n> b", "a\n|b")).toBe("> a\n> |b");
    expect(clicked("> a", "|a")).toBe("> |a");
    expect(clicked("a\n```\ncode\n```\nb", "a\n|code\nb")).toBe("a\n```\n|code\n```\nb");
    expect(clicked("a\n```\ncode\n```\nb", "a\ncode\n|b")).toBe("a\n```\ncode\n```\n|b");
    expect(clicked("a\n```ts\ncode\n```", "a\nts|\ncode\n")).toBe("a\n```ts|\ncode\n```");
  });

  it("map draft offsets inside hidden markers to where they were", () => {
    const layout = layoutOf("x **bold** y");
    expect([2, 3, 4].map((at) => sourceToCompact(layout, at))).toEqual([2, 2, 2]);
    expect([8, 9, 10].map((at) => sourceToCompact(layout, at))).toEqual([6, 6, 6]);
    expect(sourceToCompact(layout, 12)).toBe(layout.length);
  });

  it("handle an empty draft and one that's all markers", () => {
    const empty = layoutOf("");
    expect([compactToSource(empty, 0), sourceToCompact(empty, 0)]).toEqual([0, 0]);
    const fence = layoutOf("```\n```");
    expect(shown(fence)).toBe("");
    expect(compactToSource(fence, 0)).toBe(fence.sourceLength);
  });
});
