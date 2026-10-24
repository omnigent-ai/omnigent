import { afterEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { Markdown } from "@tiptap/markdown";
import { StarterKit } from "@tiptap/starter-kit";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { findPmRangeForComment, computeSelectionData } from "./TipTapEditorHelpers";
import type { Comment } from "@/hooks/useComments";

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

// ---------------------------------------------------------------------------
// Repeated word behind markup the editor does not render
// ---------------------------------------------------------------------------

const REPEATED_PARAGRAPH =
  "The quick brown fox jumps over the lazy dog while the sleepy fox naps " +
  "and a third fox watches from the hill.";
const TRAILER = "Another paragraph follows so the editor has more text.";

function secondFox(text: string): number {
  return text.indexOf("fox", text.indexOf("sleepy"));
}

/** Start of the n-th (0-based) copy of `word` in `text`. */
function nthIndex(text: string, word: string, n: number): number {
  let idx = -1;
  for (let i = 0; i <= n; i++) idx = text.indexOf(word, idx + 1);
  return idx;
}

describe("repeated word after invisible markup", () => {
  // The image URL and link target exist only in the raw file, so raw offsets
  // run far ahead of text offsets by the time the repeated word appears.
  const rawContent = [
    "# Project Notes",
    "",
    `![Architecture diagram](https://example.com/${"a".repeat(100)}.png)`,
    "",
    `See the [design document](https://example.com/${"b".repeat(80)}) for details.`,
    "",
    REPEATED_PARAGRAPH,
    "",
    TRAILER,
    "",
  ].join("\n");
  const docText = [
    "Project Notes",
    "",
    "See the design document for details.",
    REPEATED_PARAGRAPH,
    TRAILER,
  ].join("\n");
  const secondFoxInText = secondFox(docText);
  const secondFoxInRaw = secondFox(rawContent);

  it("computeSelectionData anchors to the occurrence that was selected", () => {
    const result = computeSelectionData(
      secondFoxInText,
      secondFoxInText + 3,
      makeDoc(docText),
      rawContent,
    );
    expect(result).toEqual({
      start_index: secondFoxInRaw,
      end_index: secondFoxInRaw + 3,
      anchor_content: "fox",
    });
  });

  it("findPmRangeForComment highlights the occurrence the comment is stored at", () => {
    const result = findPmRangeForComment(
      makeDoc(docText),
      makeComment("fox", secondFoxInRaw),
      rawContent,
    );
    expect(result).toEqual({ from: secondFoxInText, to: secondFoxInText + 3 });
  });

  it("resolves the selected occurrence when inline markup sits right next to it", () => {
    // Emphasis markers break the verbatim match of the words before "fox";
    // the words after it still identify the copy.
    const emphasised = rawContent.replace("the sleepy fox", "the **sleepy** fox");
    const foxInRaw = secondFox(emphasised);

    const data = computeSelectionData(
      secondFoxInText,
      secondFoxInText + 3,
      makeDoc(docText),
      emphasised,
    );
    expect(data).toEqual({ start_index: foxInRaw, end_index: foxInRaw + 3, anchor_content: "fox" });

    const range = findPmRangeForComment(makeDoc(docText), makeComment("fox", foxInRaw), emphasised);
    expect(range).toEqual({ from: secondFoxInText, to: secondFoxInText + 3 });
  });

  it("breaks ties between identical lines with the scaled offset", () => {
    const raw = "- a fox\n- a fox\n- a fox";
    const text = "a fox\na fox\na fox";
    const secondLineFoxInText = nthIndex(text, "fox", 1);
    const secondLineFoxInRaw = nthIndex(raw, "fox", 1);

    const data = computeSelectionData(
      secondLineFoxInText,
      secondLineFoxInText + 3,
      makeDoc(text),
      raw,
    );
    expect(data?.start_index).toBe(secondLineFoxInRaw);

    const range = findPmRangeForComment(makeDoc(text), makeComment("fox", secondLineFoxInRaw), raw);
    expect(range).toEqual({ from: secondLineFoxInText, to: secondLineFoxInText + 3 });
  });

  it("uses the copy nearest the scaled offset when the stored offset no longer matches", () => {
    // The file changed since the comment was made, so the raw text at
    // start_index is not the anchor and only the offset can pick a copy.
    const text = "fox one fox two fox";
    const range = findPmRangeForComment(makeDoc(text), makeComment("fox", 7), text);
    expect(range).toEqual({ from: 8, to: 11 });
  });
});

// ---------------------------------------------------------------------------
// Formatting on the selected word itself
// ---------------------------------------------------------------------------

describe("formatting on the selected word itself", () => {
  // Emphasis or code markers around the selected copy mean no verbatim text
  // around it matches on the raw side; the surrounding words still identify it.
  it.each([
    ["bold first copy", "the **fox** and the fox", "the fox and the fox", "fox", 0],
    ["bold second copy", "the fox and the **fox**", "the fox and the fox", "fox", 1],
    [
      "code-formatted first copy",
      "run `build` then build again",
      "run build then build again",
      "build",
      0,
    ],
  ] as const)("%s", (_name, raw, text, word, occurrence) => {
    const textFrom = nthIndex(text, word, occurrence);
    const rawFrom = nthIndex(raw, word, occurrence);

    const data = computeSelectionData(textFrom, textFrom + word.length, makeDoc(text), raw);
    expect(data).toEqual({
      start_index: rawFrom,
      end_index: rawFrom + word.length,
      anchor_content: word,
    });

    const range = findPmRangeForComment(makeDoc(text), makeComment(word, rawFrom), raw);
    expect(range).toEqual({ from: textFrom, to: textFrom + word.length });
  });
});

// ---------------------------------------------------------------------------
// Real markdown document: PM positions come from the actual TipTap schema
// ---------------------------------------------------------------------------

describe("with a real markdown document", () => {
  const rawContent = [
    "# Project Notes",
    "",
    `See the [design document](https://example.com/${"b".repeat(80)}) and the ` +
      `[API reference](https://example.com/${"c".repeat(80)}) for details.`,
    "",
    REPEATED_PARAGRAPH,
    "",
    TRAILER,
    "",
  ].join("\n");

  let editor: Editor | null = null;
  afterEach(() => {
    editor?.destroy();
    editor = null;
  });

  function openDocument(): ProseMirrorNode {
    editor = new Editor({
      element: document.createElement("div"),
      extensions: [StarterKit, Markdown],
      content: rawContent,
      contentType: "markdown",
    });
    return editor.state.doc;
  }

  /** Smallest PM position whose preceding text content has `offset` characters. */
  function pmPos(doc: ProseMirrorNode, offset: number): number {
    let lo = 0;
    let hi = doc.content.size;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (doc.textBetween(0, mid, "\n").length < offset) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  it("round-trips a selection on a repeated word to its own raw offset", () => {
    const doc = openDocument();
    const text = doc.textBetween(0, doc.content.size, "\n");
    const from = pmPos(doc, secondFox(text));
    const to = pmPos(doc, secondFox(text) + 3);
    expect(doc.textBetween(from, to, "\n")).toBe("fox");
    const secondFoxInRaw = secondFox(rawContent);

    const data = computeSelectionData(from, to, doc, rawContent);
    expect(data).toEqual({
      start_index: secondFoxInRaw,
      end_index: secondFoxInRaw + 3,
      anchor_content: "fox",
    });

    const range = findPmRangeForComment(doc, makeComment("fox", secondFoxInRaw), rawContent);
    expect(range).toEqual({ from, to });
  });
});
