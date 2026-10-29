import { afterEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import type { Comment } from "@/hooks/useComments";
import { createMarkdownDocumentExtensions } from "./markdownDocumentExtensions";
import { installMarkdownParserPatch } from "./tiptapMarkdownPatches";
import {
  computeSelectionData,
  findPmRangeForComment,
  remapMarkdownComment,
} from "./TipTapEditorHelpers";
import { markdownSourceMap } from "./markdownSourceMap";
import callTable from "../../../tests/e2e_ui/comments/fixtures/call-table.md?raw";
import mixedStructures from "../../../tests/e2e_ui/comments/fixtures/mixed-structures.md?raw";
import escapedPipe from "../../../tests/e2e_ui/comments/fixtures/escaped-pipe.md?raw";

installMarkdownParserPatch();
let editor: Editor;
afterEach(() => editor?.destroy());
function createEditor(raw: string) {
  return new Editor({
    element: null,
    extensions: createMarkdownDocumentExtensions("conversation", "doc.md"),
    content: raw,
    contentType: "markdown",
  });
}
function parse(raw: string) {
  editor?.destroy();
  editor = createEditor(raw);
  return editor.state.doc;
}
function paragraphs(...texts: string[]) {
  return editor.schema.nodeFromJSON({
    type: "doc",
    content: texts.map((text) => ({ type: "paragraph", content: [{ type: "text", text }] })),
  });
}
function legacySelection(from: number, to: number, doc: ReturnType<typeof parse>, raw: string) {
  return computeSelectionData(from, to, doc, raw);
}
const link = `[Details](https://example.com/${"x".repeat(2000)})`;
// Brackets identify the expected raw range independently of the implementation.
const cases: [string, string, number, string?][] = [
  ["escaped pipe elsewhere in document", escapedPipe.replace("**word**", "**⟦word⟧**"), 1],
  ["escaped pipe cell", "| A | B |\n| --- | --- |\n| word \\| ⟦word⟧ | word |", 1],
  ["escaped pipe header", "| word \\| ⟦word⟧ | B |\n| --- | --- |\n| word | word |", 1],
  ["escaped pipe CRLF", "| A | B |\r\n| --- | --- |\r\n| word \\| ⟦word⟧ | word |", 1],
  ["escaped pipe without outer delimiters", "A | B\n--- | ---\nword \\| ⟦word⟧ | word", 1],
  ["escaped pipe in bold", "| A | B |\n| --- | --- |\n| **word \\| ⟦word⟧** | word |", 1],
  ["escaped pipe in code", "| A | B |\n| --- | --- |\n| `word \\| ⟦word⟧` | word |", 1],
  ["escaped pipe in quote", "> | A | B |\n> | --- | --- |\n> | word \\| ⟦word⟧ | word |", 1],
  ["escaped pipe span", "| A | B |\n| --- | --- |\n| ⟦left \\| right⟧ | word |", 0, "left | right"],
  ["escaped pipe character", "| A | B |\n| --- | --- |\n| left \\⟦|⟧ right | word |", 0, "|"],
  ["repeated pipe cells", "| A | B |\n| --- | --- |\n| \\| | \\⟦|⟧ |", 1, "|"],
  [
    "empty cells before duplicate",
    "| A | B | C |\n| --- | --- | --- |\n| | word \\| | ⟦word⟧ |",
    1,
  ],
  ["missing cells", "| A | B |\n| --- | --- |\n| word \\| ⟦word⟧ |", 1],
  ["extra cells", "| A | B |\n| --- | --- |\n| word \\| ⟦word⟧ | word | ignored |", 1],
  ...[1, 2, 3, 4, 5, 6].map((slashes): [string, string, number] => [
    `pipe after ${slashes} backslashes`,
    `| A | B |\n| --- | --- |\n| word ${"\\".repeat(slashes)}| ⟦word⟧ |`,
    1,
  ]),
  ["mixed structures", mixedStructures.replace("2. word **word**", "2. word **⟦word⟧**"), 17],
  ["nested ordered list parent", "1. word\n2. **⟦word⟧**\n   1. word\n3. word", 1],
  ["nested ordered list child", "1. word\n2. **word**\n   1. ⟦word⟧\n3. word", 2],
  ["deep ordered marker duplicate", "1. word\n   1. word\n      1. ⟦1⟧\n2. word", 0, "1"],
  ["alphabetic and roman list markers", "a. word\n   i. word\n   ii. **⟦word⟧**\nb. word", 2],
  ["ordered list blank lines", "1. word\n\n   1. word\n\n2. ⟦word⟧\n", 2],
  ["heading marker", "# ⟦#⟧ hello\n\n#", 0, "#"],
  ["ordered list marker", "1. ⟦1⟧\n2. 1", 0, "1"],
  ["single-tilde strikethrough", `~word~ **⟦word⟧** word\n\n${link}`, 1],
  ["triple emphasis", `word ***⟦word⟧*** word\n\n${link}`, 1],
  ["escaped quote", "\\> word ⟦word⟧", 1],
  ["escaped angle", "\\< word ⟦word⟧", 1],
  ["escaped ampersand", "\\& word ⟦word⟧", 1],
  ["original repro", `# Anchors\n\n**word** and **⟦word⟧**\n\n**word**\n\n${link}`, 1],
  ["hidden URL and split bold", "[Details](word) ⟦word⟧ wo**rd**", 0],
  ["selected split bold after hidden URL", "[Details](word) word ⟦wo**rd⟧**", 1],
  ["selected split italic", "word ⟦wo*rd⟧* word", 1],
  ["selected split strike", "word ⟦wo~~rd⟧~~ word", 1],
  ["selected split code", "word ⟦wo`rd⟧` word", 1],
  ["selected split HTML", "[Details](word) word ⟦wo<em>rd⟧</em>", 1],
  ["selected repeated split bold", "wo**rd** wo**rd** ⟦wo**rd⟧**", 2],
  ["selected nested marks", "word **⟦wo*rd⟧*** word", 1],
  ["selected split link", "word [⟦wo](word)rd⟧ word", 1],
  ["selected entity", "word ⟦&#119;ord⟧ word", 1],
  ["selected emoji entity", "😀 ⟦&#x1F600;⟧ 😀", 1, "😀"],
  ["selected formatted phrase", "say word now ⟦say **word** now⟧", 1, "say word now"],
  ["selected split table word", "| Call | B |\n| --- | --- |\n| Call | ⟦Ca**ll⟧** |", 2, "Call"],
  ["hidden URL and split HTML", "[Details](word) ⟦word⟧ wo<em>rd</em>", 0],
  ["hidden image label and split bold", "![word](logo.png)\n\n⟦word⟧ wo**rd**", 0],
  ["URL", `[Details](https://example.com/word/${"x".repeat(2000)})\n\nword ⟦word⟧ word`, 1],
  ["title", `[Details](https://example.com "word")\n\nword ⟦word⟧ word\n\n${link}`, 1],
  ["image", `![word](word.png)\n\nword ⟦word⟧ word\n\n${link}`, 1],
  [
    "image and link blocks before selection",
    `# Guide\n\n![Logo](https://example.com/${"x".repeat(200)}.png)\n\n${link}\n\nword ⟦word⟧ word`,
    1,
  ],
  ["HTML comment", `<!-- word -->\n\nword ⟦word⟧ word\n\n${link}`, 1],
  ["reference definition before text", "[ref]: word\n\n⟦word⟧ word [Details][ref]", 0],
  [
    "reference definition after text",
    "word ⟦word⟧ [Details][ref]\n\n[ref]: https://example.com/word",
    1,
  ],
  ["entities before selection", "&amp; &lt; &#119;ord word ⟦word⟧", 2],
  ["escaped punctuation", "word \\* ⟦word⟧", 1],
  ["table", "| word | B |\n| --- | --- |\n| word | ⟦word⟧ |", 2],
  ["nested lists", "- word\n  - ⟦word⟧\n- word", 1],
  ["multiline quote", "> word\n> ⟦word⟧\n> word", 1],
  ["nested task lists", "- [ ] word\n  - [x] ⟦word⟧\n- [ ] word", 1],
  ["mixed task lists", "- [ ] word\n  - ⟦word⟧\n- [x] word", 1],
  ["alert", "> [!NOTE]\n> word\n> ⟦word⟧\n\nword", 1],
  ["standalone alert marker", "> [!NOTE]\n>\n> word\n\nword ⟦word⟧", 2],
  ["fence language", "word\n\n```word\n⟦word⟧ word\n```", 1],
  ["indented code", "word\n\n    word\n    ⟦word⟧", 2],
  ["inline code", "word ` ⟦word⟧ ` word", 1],
  ["emoji/CRLF", "😀 word\r\n\r\n😀 ⟦word⟧ word", 1],
  ["Chinese", `词语 **⟦词语⟧** 词语\n\n${link}`, 1, "词语"],
  ["accented word", `café **⟦café⟧** café\n\n${link}`, 1, "café"],
  ["overlapping matches", `aaaa ⟦aa⟧\n\n${link}`, 3, "aa"],
  ["hard-wrapped paragraph", `word word\nsecond line\n\n**word** ⟦word⟧\n\n${link}`, 3],
  ["hard-wrapped quote", "> word\n> word\n\n> ⟦word⟧", 2],
  ["multiword phrase", "[Details](https://x/the-word) the word ⟦the word⟧", 1, "the word"],
  ["phrase after a soft break", `the word\nmore\n\n**⟦the word⟧**\n\n${link}`, 1, "the word"],
];

describe("validated Markdown source positions", () => {
  it.each<[string, number, number, number, number, string]>([
    ["alpha\r\nbeta", 1, 7, 0, 7, "alpha\n"],
    ["> alpha\r\n> beta", 2, 8, 2, 9, "alpha\n"],
    ["alpha\nbeta", 1, 7, 0, 6, "alpha\n"],
    ["alpha\rbeta", 1, 7, 0, 6, "alpha\n"],
    ["word  \nnext\n\nword\n\n[details](word)", 1, 6, 0, 4, "word"],
    [`[label](${"x".repeat(59)}) word **word**![alt](image.png)\n\n`, 12, 17, 76, 80, "word"],
    ["![alt](image.png)**word** word", 1, 6, 19, 23, "word"],
  ])(
    "maps text beside non-text boundaries: %s",
    (raw, from, to, start_index, end_index, anchor_content) => {
      const doc = parse(raw);
      const expected = { start_index, end_index, anchor_content };
      expect(computeSelectionData(from, to, doc, raw, editor.markdown)).toEqual(expected);
      const restored = findPmRangeForComment(doc, expected as Comment, raw, editor.markdown)!;
      expect(restored.from).toBeGreaterThanOrEqual(from);
      expect(restored.to).toBeLessThanOrEqual(to);
      expect(doc.textBetween(restored.from, restored.to, "\n")).toBe(anchor_content);
    },
  );

  it("reports unexpected mapping errors and caches the failed map", () => {
    const raw = "word word";
    const doc = parse(raw);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const traversal = vi.spyOn(doc, "forEach").mockImplementation(() => {
      throw new TypeError("mapper bug");
    });
    try {
      expect(markdownSourceMap(doc, raw, editor.markdown)).toBeNull();
      expect(markdownSourceMap(doc, raw, editor.markdown)).toBeNull();
      expect(warn).toHaveBeenCalledTimes(1);
      expect(traversal).toHaveBeenCalledTimes(1);
    } finally {
      traversal.mockRestore();
      warn.mockRestore();
    }
  });
  it.each([10, 11])("rejects a proportional offset on another mapped emoji (%i)", (length) => {
    const raw = `label &#x1F600; 😀 [x](${"x".repeat(length)})`;
    const doc = parse(raw);
    expect(markdownSourceMap(doc, raw, editor.markdown)).not.toBeNull();
    expect(doc.textBetween(7, 8)).toBe("\ud83d");
    expect(computeSelectionData(7, 8, doc, raw, editor.markdown)).toBeNull();
    const whole = computeSelectionData(7, 9, doc, raw, editor.markdown)!;
    expect(whole).toEqual({ start_index: 6, end_index: 15, anchor_content: "😀" });
    expect(findPmRangeForComment(doc, whole as Comment, raw, editor.markdown)).toEqual({
      from: 7,
      to: 9,
    });
  });
  it.each(
    [
      ["plain", callTable],
      ["CRLF", callTable.replaceAll("\n", "\r\n")],
      ["padded cells", callTable.replaceAll(" | ", "   |   ")],
      ["bold", callTable.replaceAll("Call.", "**Call.**")],
      ["inline code", callTable.replaceAll("Call.", "`Call.`")],
      ["Unicode heading and hidden URL", `# Notes 😀\n\n${link}\n\n${callTable}`],
    ]
      .flatMap(([name, raw]) =>
        ["Call", "Call.", "call", "(preview: as before). No call (preview: call)."].map(
          (phrase) => [name, phrase, raw],
        ),
      )
      .concat([
        ["mixed structures", "word", mixedStructures],
        ["mixed structures CRLF", "word", mixedStructures.replaceAll("\n", "\r\n")],
        ["mixed structures after hidden URL", "word", `${link}\n\n${mixedStructures}`],
        ["serialized mixed structures", "word", mixedStructures],
        ["escaped pipe repeated calls", "call", escapedPipe],
        ["escaped pipe repeated previews", "preview", escapedPipe],
        ["escaped pipe repeated No", "No", escapedPipe],
      ]),
  )("preserves every fixture occurrence: %s / %s", (name, phrase, source) => {
    const doc = parse(source);
    const raw = name === "serialized mixed structures" ? editor.getMarkdown() : source;
    expect(markdownSourceMap(doc, raw, editor.markdown)).not.toBeNull();
    let sourceCursor = 0;
    let matched = 0;
    doc.descendants((node, position) => {
      if (!node.isText || !node.text) return;
      for (
        let index = node.text.indexOf(phrase);
        index !== -1;
        index = node.text.indexOf(phrase, index + 1)
      ) {
        const from = position + index;
        const to = from + phrase.length;
        const start_index = raw.indexOf(phrase, sourceCursor);
        expect(start_index).toBeGreaterThanOrEqual(0);
        sourceCursor = start_index + phrase.length;
        const anchor = { start_index, end_index: sourceCursor, anchor_content: phrase };
        expect(computeSelectionData(from, to, doc, raw, editor.markdown)).toEqual(anchor);
        expect(findPmRangeForComment(doc, anchor as Comment, raw, editor.markdown)).toEqual({
          from,
          to,
        });
        matched++;
      }
    });
    expect(matched).toBe(raw.split(phrase).length - 1);
    expect(matched).toBeGreaterThan(0);
  });

  it.each(cases)(
    "maps capture and restore independently: %s",
    (_name, fixture, ordinal, word = "word") => {
      const start_index = fixture.indexOf("⟦");
      const end_index = fixture.indexOf("⟧") - 1;
      const raw = fixture.replace("⟦", "").replace("⟧", "");
      const doc = parse(raw);
      let text = "";
      const positions: number[] = [];
      doc.descendants((node, pos) => {
        if (!node.isText || !node.text) return;
        text += node.text;
        for (let i = 0; i < node.text.length; i++) positions.push(pos + i);
      });
      let at = -1;
      for (let i = 0; i <= ordinal; i++) at = text.indexOf(word, at + 1);
      expect(at).toBeGreaterThanOrEqual(0);
      const from = positions[at],
        to = positions[at + word.length - 1] + 1;
      expect(doc.textBetween(from, to)).toBe(word);
      expect(markdownSourceMap(doc, raw, editor.markdown)).not.toBeNull();
      expect(computeSelectionData(from, to, doc, raw, editor.markdown)).toEqual({
        start_index,
        end_index,
        anchor_content: word,
      });
      expect(
        findPmRangeForComment(
          doc,
          { start_index, end_index, anchor_content: word } as Comment,
          raw,
          editor.markdown,
        ),
      ).toEqual({ from, to });
      expect(
        remapMarkdownComment(
          doc,
          { start_index, end_index: end_index + 1, anchor_content: word } as Comment,
          raw,
          editor.markdown!,
        ),
      ).toEqual({ start_index, end_index, anchor_content: word });
    },
  );

  it("restores a legacy-captured offset to its stored copy rather than the old heuristic's copy", () => {
    const raw = `word word word\n\n${link}`;
    const doc = parse(raw);
    const selectedOccurrence = { from: 6, to: 10 };
    const legacyComment = {
      start_index: 10,
      end_index: 14,
      anchor_content: "word",
    } as Comment;

    expect(doc.textBetween(selectedOccurrence.from, selectedOccurrence.to)).toBe("word");
    expect(legacySelection(selectedOccurrence.from, selectedOccurrence.to, doc, raw)).toEqual(
      legacyComment,
    );
    expect(findPmRangeForComment(doc, legacyComment, raw)).toEqual({ from: 0, to: 5 });

    expect(markdownSourceMap(doc, raw, editor.markdown)).not.toBeNull();
    expect(findPmRangeForComment(doc, legacyComment, raw, editor.markdown)).toEqual({
      from: 11,
      to: 15,
    });
    expect(legacyComment).toEqual({ start_index: 10, end_index: 14, anchor_content: "word" });
  });

  it("reuses one mapping across comments and invalidates it when source or document changes", () => {
    const raw = "word **word** word";
    const doc = parse(raw);
    const lexer = vi.spyOn(editor.markdown!.instance.Lexer.prototype, "lex");
    try {
      const first = markdownSourceMap(doc, raw, editor.markdown);
      for (let i = 0; i < 100; i++)
        expect(markdownSourceMap(doc, raw, editor.markdown)).toBe(first);
      expect(lexer).toHaveBeenCalledTimes(1);
      const shifted = markdownSourceMap(doc, "\n" + raw, editor.markdown);
      expect(shifted?.starts[0]).toBe(first!.starts[0] + 1);
      expect(lexer).toHaveBeenCalledTimes(2);
      expect(markdownSourceMap(paragraphs("word word"), raw, editor.markdown)).toBeNull();
    } finally {
      lexer.mockRestore();
    }
  });

  it("resolves comments throughout a large document without re-lexing per comment", () => {
    const block = `# Section\n\nword **word**\n\n[Details](https://example.com/word/${"x".repeat(900)})\n\n`;
    const count = 250;
    const raw = block.repeat(count);
    const doc = parse(raw);
    const expected: number[] = [];
    doc.descendants((node, pos) => {
      if (node.isText && node.text === "word") expected.push(pos);
    });
    expect(expected).toHaveLength(count);
    const lexer = vi.spyOn(editor.markdown!.instance.Lexer.prototype, "lex");
    try {
      for (const i of [0, ...Array.from({ length: 98 }, (_, j) => j * 2 + 1), count - 1]) {
        const start_index = i * block.length + block.indexOf("**word") + 2;
        const end_index = start_index + 4;
        expect(
          findPmRangeForComment(
            doc,
            { start_index, end_index, anchor_content: "word" } as Comment,
            raw,
            editor.markdown,
          ),
        ).toEqual({ from: expected[i], to: expected[i] + 4 });
        expect(
          computeSelectionData(expected[i], expected[i] + 4, doc, raw, editor.markdown),
        ).toEqual({ start_index, end_index, anchor_content: "word" });
      }
      expect(lexer).toHaveBeenCalledTimes(1);
    } finally {
      lexer.mockRestore();
    }
  });

  it("keeps the whole document on legacy anchors when a block cannot be verified", () => {
    const raw = `| A | B |\n| --- | --- |\n| a\\|b | word |\n\nword\n\n${link}`;
    const doc = parse(raw);
    const manager = editor.markdown!;
    const tokens = new manager.instance.Lexer(manager.instance.defaults).lex(raw);
    tokens[0] = { type: "unsupported", raw: tokens[0].raw };
    const lexer = vi.spyOn(manager.instance.Lexer.prototype, "lex").mockReturnValueOnce(tokens);
    try {
      expect(markdownSourceMap(doc, raw, editor.markdown)).toBeNull();
    } finally {
      lexer.mockRestore();
    }
    const positions: number[] = [];
    doc.descendants((node, pos) => {
      if (node.isText && node.text?.includes("word"))
        positions.push(pos + node.text.indexOf("word"));
    });
    const [inTable, afterTable] = positions;
    expect(computeSelectionData(afterTable, afterTable + 4, doc, raw, editor.markdown)).toEqual(
      legacySelection(afterTable, afterTable + 4, doc, raw),
    );
    const captured = computeSelectionData(inTable, inTable + 4, doc, raw, editor.markdown);
    expect(captured).toEqual(legacySelection(inTable, inTable + 4, doc, raw));
    expect(findPmRangeForComment(doc, captured as Comment, raw, editor.markdown)).toEqual({
      from: inTable,
      to: inTable + 4,
    });
  });

  it("keeps edited documents on legacy anchors", () => {
    const raw = "word\n\nword";
    parse(raw);
    const inserted = paragraphs("new", "word", "word");
    expect(markdownSourceMap(inserted, raw, editor.markdown)).toBeNull();
    const second = inserted.content.size - 5;
    const comment = { start_index: 6, end_index: 10, anchor_content: "word" } as Comment;
    const range = findPmRangeForComment(inserted, comment, raw, editor.markdown);
    expect(range?.from).toBe(second);
    expect(inserted.textBetween(range!.from, range!.to)).toBe("word");
    const moved = paragraphs("word", "abc", "d", "word word");
    expect(markdownSourceMap(moved, "word\n\nab\n\ncd\n\nword word", editor.markdown)).toBeNull();
    const list = "- ab\n- cd";
    parse(list);
    const movedInList = editor.schema.nodeFromJSON({
      type: "doc",
      content: [
        {
          type: "bulletList",
          content: ["abc", "d"].map((text) => ({
            type: "listItem",
            content: [{ type: "paragraph", content: [{ type: "text", text }] }],
          })),
        },
      ],
    });
    expect(markdownSourceMap(movedInList, list, editor.markdown)).toBeNull();
  });

  it("stores the exact source span rather than another literal copy", () => {
    const raw = "word wo**rd** word";
    const doc = parse(raw);
    expect(markdownSourceMap(doc, raw, editor.markdown)).not.toBeNull();
    const from = 1 + "word ".length;
    const to = from + 4;
    expect(doc.textBetween(from, to)).toBe("word");
    const captured = computeSelectionData(from, to, doc, raw, editor.markdown);
    expect(captured).toEqual({ start_index: 5, end_index: 11, anchor_content: "word" });
    expect(findPmRangeForComment(doc, captured as Comment, raw, editor.markdown)).toEqual({
      from,
      to,
    });
  });

  it("does not expand a partial surrogate selection to an entire decoded entity", () => {
    const raw = "&#x1F600;";
    const doc = parse(raw);
    expect(markdownSourceMap(doc, raw, editor.markdown)).not.toBeNull();
    for (const from of [1, 2]) {
      expect(computeSelectionData(from, from + 1, doc, raw, editor.markdown)).toEqual(
        legacySelection(from, from + 1, doc, raw),
      );
    }
  });

  it("lexes once per editor and source without sharing tokens between editors", () => {
    const raw = "word **word**";
    const doc = parse(raw);
    const other = createEditor(raw);
    const lexer = vi.spyOn(editor.markdown!.instance.Lexer.prototype, "lex");
    try {
      expect(markdownSourceMap(doc, raw, editor.markdown)).not.toBeNull();
      expect(markdownSourceMap(other.state.doc, raw, other.markdown)).not.toBeNull();
      expect(lexer).toHaveBeenCalledTimes(2);
      const rebuilt = editor.schema.nodeFromJSON(doc.toJSON());
      expect(markdownSourceMap(rebuilt, raw, editor.markdown)).not.toBeNull();
      expect(lexer).toHaveBeenCalledTimes(2);
    } finally {
      lexer.mockRestore();
      other.destroy();
    }
  });

  it("caches failed tokenization and falls back without crashing comment restoration", () => {
    const raw = "word word";
    const doc = parse(raw);
    const changedSource = "\n" + raw;
    const comment = { start_index: 6, end_index: 10, anchor_content: "word" } as Comment;
    const lexer = vi
      .spyOn(editor.markdown!.instance.Lexer.prototype, "lex")
      .mockImplementation(() => {
        throw new Error("Custom tokenizer failure");
      });
    try {
      const rebuilt = editor.schema.nodeFromJSON(doc.toJSON());
      for (const current of [doc, doc, rebuilt]) {
        expect(findPmRangeForComment(current, comment, changedSource, editor.markdown)).toEqual(
          findPmRangeForComment(current, comment, changedSource),
        );
      }
      expect(lexer).toHaveBeenCalledTimes(1);
    } finally {
      lexer.mockRestore();
    }
  });
});
