import { afterEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import type { Comment } from "@/hooks/useComments";
import { createMarkdownDocumentExtensions } from "./markdownDocumentExtensions";
import { installMarkdownParserPatch } from "./tiptapMarkdownPatches";
import { computeSelectionData, findPmRangeForComment } from "./TipTapEditorHelpers";
import { markdownSourceMap } from "./markdownSourceMap";

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
  ["heading marker", "# ⟦#⟧ hello\n\n#", 0, "#"],
  ["ordered list marker", "1. ⟦1⟧\n2. 1", 0, "1"],
  ["single-tilde strikethrough", `~word~ **⟦word⟧** word\n\n${link}`, 1],
  ["triple emphasis", `word ***⟦word⟧*** word\n\n${link}`, 1],
  ["escaped quote", "\\> word ⟦word⟧", 1],
  ["escaped angle", "\\< word ⟦word⟧", 1],
  ["escaped ampersand", "\\& word ⟦word⟧", 1],
  ["original repro", `# Anchors\n\n**word** and **⟦word⟧**\n\n**word**\n\n${link}`, 1],
  ["hidden URL and split bold", "[Details](word) ⟦word⟧ wo**rd**", 0],
  ["hidden URL and split HTML", "[Details](word) ⟦word⟧ wo<em>rd</em>", 0],
  ["hidden image label and split bold", "![word](logo.png)\n\n⟦word⟧ wo**rd**", 0],
  ["URL", `[Details](https://example.com/word/${"x".repeat(2000)})\n\nword ⟦word⟧ word`, 1],
  ["title", `[Details](https://example.com "word")\n\nword ⟦word⟧ word\n\n${link}`, 1],
  ["image", `![word](word.png)\n\nword ⟦word⟧ word\n\n${link}`, 1],
  ["HTML comment", `<!-- word -->\n\nword ⟦word⟧ word\n\n${link}`, 1],
  ["reference definition before text", "[ref]: word\n\n⟦word⟧ word [Details][ref]", 0],
  [
    "reference definition after text",
    "word ⟦word⟧ [Details][ref]\n\n[ref]: https://example.com/word",
    1,
  ],
  ["entities before selection", "&amp; &lt; &#119;ord word ⟦word⟧", 1],
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
    },
  );

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

  it("falls back only for the block whose tokens it cannot verify", () => {
    const raw = "| A | B |\n| --- | --- |\n| a\\|b | word |\n\nword word";
    const doc = parse(raw);
    const positions: number[] = [];
    doc.descendants((node, pos) => {
      if (node.isText && node.text?.includes("word"))
        positions.push(pos + node.text.indexOf("word"));
    });
    const [inTable, afterTable] = positions;
    expect(computeSelectionData(inTable, inTable + 4, doc, raw, editor.markdown)).toEqual(
      legacySelection(inTable, inTable + 4, doc, raw),
    );
    expect(computeSelectionData(afterTable, afterTable + 4, doc, raw, editor.markdown)).toEqual({
      start_index: raw.lastIndexOf("word word"),
      end_index: raw.lastIndexOf("word word") + 4,
      anchor_content: "word",
    });
  });

  it("keeps unedited blocks mapped and rejects text moved between blocks", () => {
    const raw = "word\n\nab\n\ncd\n\nword word";
    parse(raw);
    const edited = paragraphs("word", "abc", "d", "word word");
    const target = edited.content.size - 5;
    expect(edited.textBetween(target, target + 4)).toBe("word");
    expect(computeSelectionData(target, target + 4, edited, raw, editor.markdown)).toEqual({
      start_index: raw.length - 4,
      end_index: raw.length,
      anchor_content: "word",
    });
    const moved = 1 + "word".length + 2 + 2;
    expect(edited.textBetween(moved, moved + 1)).toBe("c");
    expect(computeSelectionData(moved, moved + 1, edited, raw, editor.markdown)).toEqual(
      legacySelection(moved, moved + 1, edited, raw),
    );
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

  it("lexes once per editor and source without sharing tokens between editors", () => {
    const raw = "word **word**";
    const doc = parse(raw);
    const other = createEditor("Other word");
    const lexer = vi.spyOn(editor.markdown!.instance.Lexer.prototype, "lex");
    try {
      expect(markdownSourceMap(doc, raw, editor.markdown)).not.toBeNull();
      expect(markdownSourceMap(other.state.doc, "Other word", other.markdown)).not.toBeNull();
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
      for (let i = 0; i < 2; i++) {
        expect(findPmRangeForComment(doc, comment, changedSource, editor.markdown)).toEqual(
          findPmRangeForComment(doc, comment, changedSource),
        );
      }
      expect(lexer).toHaveBeenCalledTimes(1);
    } finally {
      lexer.mockRestore();
    }
  });
});
