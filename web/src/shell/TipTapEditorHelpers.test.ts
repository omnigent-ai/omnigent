import { afterEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { createMarkdownDocumentExtensions } from "./markdownDocumentExtensions";
import { installMarkdownParserPatch } from "./tiptapMarkdownPatches";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { findPmRangeForComment, computeSelectionData } from "./TipTapEditorHelpers";
import type { Comment } from "@/hooks/useComments";

installMarkdownParserPatch();

describe("repeated words in Markdown", () => {
  let editor: Editor | undefined;
  afterEach(() => {
    editor?.destroy();
    editor = undefined;
  });

  const link = `[Details](https://example.com/${"x".repeat(2000)})`;
  it.each([
    ["long URL after selection", `**word** and **word** and **word**\n\n${link}`],
    ["long URL before selection", `${link}\n\n**word** and **word** and **word**`],
    ["headings and paragraphs", `# word\n\nword\n\nword\n\n${link}`],
    ["lists", `- word\n- word\n- word\n\n${link}`],
    ["blockquotes", `> word\n>\n> word\n\nword\n\n${link}`],
    ["mixed formatting", `\`word\` **word** word\n\n${link}`],
    ["emoji and CRLF", `😀 word\r\n\r\nword word\r\n\r\n${link}`],
    ["task lists", `- [ ] word\n- [x] word\n- [ ] word\n\n${link}`],
    ["tables", `| A | B |\n| --- | --- |\n| word | word |\n| word | other |\n\n${link}`],
    ["GitHub alerts", `> [!NOTE]\n> word\n>\n> word\n\nword\n\n${link}`],
    ["code fences", `word\n\n\`\`\`text\nword word\n\`\`\`\n\n${link}`],
    ["link labels", `word [word](https://example.com) word\n\n${link}`],
    ["strikethrough", `word ~~word~~ word\n\n${link}`],
    ["substring matches", `sword word password word\n\n${link}`],
    ["hard-wrapped prose", `word word\nword\n\n> word\n> word\n\n${link}`],
  ])("preserves every occurrence's raw offset and highlight: %s", (_name, raw) => {
    editor = new Editor({
      extensions: createMarkdownDocumentExtensions("conversation", "doc.md"),
      content: raw,
      contentType: "markdown",
    });
    const doc = editor.state.doc;
    let rawFrom = 0;
    doc.descendants((node, pos) => {
      if (!node.isText || !node.text) return;
      for (let at = node.text.indexOf("word"); at !== -1; at = node.text.indexOf("word", at + 1)) {
        const from = pos + at;
        const expected = raw.indexOf("word", rawFrom);
        rawFrom = expected + 4;
        const data = computeSelectionData(from, from + 4, doc, raw, editor!.markdown);
        expect(data).toEqual({
          start_index: expected,
          end_index: expected + 4,
          anchor_content: "word",
        });
        // Check restore independently of capture so symmetric mistakes cannot pass.
        const range = findPmRangeForComment(
          doc,
          makeComment("word", expected),
          raw,
          editor!.markdown,
        );
        expect(range).not.toBeNull();
        if (!range) return;
        expect(doc.textBetween(0, range.from, "\n")).toBe(doc.textBetween(0, from, "\n"));
        expect(doc.textBetween(range.from, range.to, "\n")).toBe("word");
      }
    });
    expect(rawFrom).toBe(raw.lastIndexOf("word") + 4);
  });

  it("keeps the legacy fallback without a Markdown manager when hidden text changes occurrence counts", () => {
    const raw = "word [Details](word) word";
    const doc = makeDoc("word Details word");
    expect(computeSelectionData(13, 17, doc, raw)).toEqual({
      start_index: raw.lastIndexOf("word"),
      end_index: raw.length,
      anchor_content: "word",
    });
    expect(findPmRangeForComment(doc, makeComment("word", raw.lastIndexOf("word")), raw)).toEqual({
      from: 13,
      to: 17,
    });
  });
});

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

/**
 * Build a minimal ProseMirror Node mock where textBetween(from, to, sep)
 * is just text.slice(from, to).
 *
 * This collapses PM positions to text offsets (1:1 mapping), which makes
 * expected values trivial to compute while fully exercising the string
 * matching and binary search logic in the helpers.
 */
function makeDoc(text: string): ProseMirrorNode {
  return {
    content: { size: text.length },
    textBetween: (from: number, to: number, _sep: string) => text.slice(from, to),
  } as unknown as ProseMirrorNode;
}

/** Build a minimal Comment with only the fields the helpers use. */
function makeComment(
  anchor_content: string,
  start_index: number,
  end_index: number = start_index + anchor_content.length,
): Comment {
  return {
    id: "test-comment",
    start_index,
    end_index,
    anchor_content,
    body: "",
    created_at: "",
    author: null,
  } as unknown as Comment;
}

// ---------------------------------------------------------------------------
// findPmRangeForComment
// ---------------------------------------------------------------------------

describe("findPmRangeForComment", () => {
  const raw = "Hello, world! Hello, universe!";

  it("returns null when anchor_content is absent (undefined)", () => {
    const doc = makeDoc("Hello, world!");
    const comment = makeComment("x", 0);
    (comment as unknown as Record<string, unknown>).anchor_content = undefined;
    expect(findPmRangeForComment(doc, comment, raw)).toBeNull();
  });

  it("returns null when anchor_content is empty string", () => {
    const doc = makeDoc("Hello, world!");
    expect(findPmRangeForComment(doc, makeComment("", 0), raw)).toBeNull();
  });

  it("returns null when anchor_content is whitespace only", () => {
    const doc = makeDoc("Hello, world!");
    expect(findPmRangeForComment(doc, makeComment("   ", 0), raw)).toBeNull();
  });

  it("returns null when anchor_content is not present in the document", () => {
    const doc = makeDoc("Hello, world!");
    expect(findPmRangeForComment(doc, makeComment("missing text", 0), raw)).toBeNull();
  });

  it("finds anchor at the very beginning of the document", () => {
    const doc = makeDoc("Hello, world!");
    const result = findPmRangeForComment(doc, makeComment("Hello", 0), "Hello, world!");
    expect(result).toEqual({ from: 0, to: 5 });
  });

  it("finds anchor in the middle of the document", () => {
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = findPmRangeForComment(doc, makeComment("world", 7), text);
    expect(result).toEqual({ from: 7, to: 12 });
  });

  it("finds anchor at the end of the document", () => {
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = findPmRangeForComment(doc, makeComment("world!", 7), text);
    expect(result).toEqual({ from: 7, to: 13 });
  });

  it("uses start_index hint to skip the first occurrence when it is >500 chars away", () => {
    // "foo" appears at 0 and again at 600. With hint=600 the search window
    // starts at 100 (= 600-500), so indexOf("foo", 100) finds the second
    // occurrence at 600, not the first at 0.
    const prefix = "x".repeat(597);
    const text = "foo" + prefix + "foo"; // "foo" at 0 and 600
    const doc = makeDoc(text);
    const result = findPmRangeForComment(doc, makeComment("foo", 600), text);
    expect(result).toEqual({ from: 600, to: 603 });
  });

  it("uses the nearest occurrence when two identical strings are within the search window", () => {
    // A short selection can repeat nearby. The stored offset must win over the
    // first match so the comment re-attaches to the selected occurrence.
    const text = "Hello, world! Hello, universe!";
    const doc = makeDoc(text);
    const result = findPmRangeForComment(doc, makeComment("Hello", 14), text);
    expect(result).toEqual({ from: 14, to: 19 });
  });

  it("falls back to the first occurrence when hint window misses", () => {
    // start_index=999 is way past the end; global fallback finds index 0.
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = findPmRangeForComment(doc, makeComment("Hello", 999), text);
    expect(result).toEqual({ from: 0, to: 5 });
  });

  it("handles an empty document", () => {
    const doc = makeDoc("");
    expect(findPmRangeForComment(doc, makeComment("Hello", 0), "Hello")).toBeNull();
  });

  it("scales raw-file offset to text-content offset when rawContent is longer than text", () => {
    // Raw file has extra markdown syntax not in the doc text.
    // rawContent: "# Title\n\nHello, world!"  (len 22)
    // doc text:   "Title\nHello, world!"      (len 19)
    // anchor "Hello" at raw offset 10 → scaled hint ≈ 8 → finds at text offset 6.
    const rawContent = "# Title\n\nHello, world!";
    const docText = "Title\nHello, world!";
    const doc = makeDoc(docText);
    const result = findPmRangeForComment(doc, makeComment("Hello", 10), rawContent);
    expect(result).toEqual({ from: 6, to: 11 });
  });
});

// ---------------------------------------------------------------------------
// computeSelectionData
// ---------------------------------------------------------------------------

describe("computeSelectionData", () => {
  it("returns null when the selected PM range contains no text", () => {
    const text = "Hello, world!";
    const doc = makeDoc(text);
    expect(computeSelectionData(5, 5, doc, text)).toBeNull();
  });

  it("returns null when the selected text is whitespace only", () => {
    const text = "Hello   world";
    const doc = makeDoc(text);
    // Select the spaces at positions 5..8
    expect(computeSelectionData(5, 8, doc, text)).toBeNull();
  });

  it("returns correct indices when anchor is found verbatim in rawContent", () => {
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = computeSelectionData(7, 12, doc, text);
    expect(result).toEqual({ start_index: 7, end_index: 12, anchor_content: "world" });
  });

  it("uses anchor_content as-is (no trimming)", () => {
    // Selection includes a trailing space — should be stored verbatim.
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = computeSelectionData(0, 7, doc, text); // "Hello, "
    expect(result).toEqual({ start_index: 0, end_index: 7, anchor_content: "Hello, " });
  });

  it("skips the first occurrence when hint places it >500 chars away", () => {
    // "foo" at raw offset 0 and again at 600. Selection covers the second
    // "foo" (text offset 600); hint = 600, searchFrom = 100, so indexOf
    // returns the match at 600.
    const padding = "x".repeat(597);
    const text = "foo" + padding + "foo"; // "foo" at 0 and 600
    const doc = makeDoc(text);
    const result = computeSelectionData(600, 603, doc, text);
    expect(result).toEqual({ start_index: 600, end_index: 603, anchor_content: "foo" });
  });

  it("uses the nearest occurrence when duplicate strings are within the search window", () => {
    // Selecting the later short duplicate must not re-attach the comment to
    // the earlier identical text.
    const text = "foo bar foo baz";
    const doc = makeDoc(text);
    const result = computeSelectionData(8, 11, doc, text);
    expect(result).toEqual({ start_index: 8, end_index: 11, anchor_content: "foo" });
  });

  it("falls back to proportional indices when anchor_content is not in rawContent verbatim", () => {
    // Simulate a multi-line selection where the doc joins with "\n" but
    // rawContent uses a different representation.
    const docText = "first\nsecond"; // doc has "\n" as separator
    const rawContent = "first\r\nsecond"; // raw file has "\r\n" (different)
    const doc = makeDoc(docText);
    // Select "first\nsecond" — the "\n" form won't be found in rawContent.
    const result = computeSelectionData(0, 12, doc, rawContent);
    expect(result).not.toBeNull();
    // Should use proportional fallback (hint-based) rather than returning null.
    expect(result!.anchor_content).toBe("first\nsecond");
    // start_index is proportional: hint = round(0 * 13 / 12) = 0
    expect(result!.start_index).toBe(0);
    // end_index is start_index + anchor_content.length = 0 + 12 = 12
    expect(result!.end_index).toBe(12);
  });

  it("falls back to proportional indices when rawContent is empty", () => {
    const text = "Hello";
    const doc = makeDoc(text);
    const result = computeSelectionData(0, 5, doc, "");
    expect(result).not.toBeNull();
    expect(result!.anchor_content).toBe("Hello");
    expect(result!.start_index).toBe(0);
    expect(result!.end_index).toBe(5);
  });

  it("handles selection of the full document", () => {
    const text = "Hello, world!";
    const doc = makeDoc(text);
    const result = computeSelectionData(0, text.length, doc, text);
    expect(result).toEqual({
      start_index: 0,
      end_index: text.length,
      anchor_content: text,
    });
  });
});
