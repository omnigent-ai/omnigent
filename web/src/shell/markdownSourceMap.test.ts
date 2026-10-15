import { afterEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { Markdown } from "@tiptap/markdown";
import { SourceMappedMarkdown } from "./SourceMappedMarkdown";
import { StarterKit } from "@tiptap/starter-kit";
import { Image } from "@tiptap/extension-image";
import { TableKit } from "@tiptap/extension-table";
import { TaskList, TaskItem } from "@tiptap/extension-list";
import type { Comment } from "@/hooks/useComments";
import { GitHubAlertBlockquote } from "./TipTapGitHubAlert";
import { HtmlPassthrough } from "./TipTapHtmlPassthrough";
import { installMarkdownParserPatch } from "./tiptapMarkdownPatches";
import { computeSelectionData, findPmRangeForComment } from "./TipTapEditorHelpers";
import { markdownSourceMap } from "./markdownSourceMap";

installMarkdownParserPatch();
let editor: Editor;
afterEach(() => editor?.destroy());
function parse(raw: string) {
  editor?.destroy();
  editor = new Editor({
    element: null,
    extensions: [
      StarterKit.configure({ blockquote: false }),
      GitHubAlertBlockquote,
      Image.configure({ inline: true }),
      TableKit,
      TaskList,
      TaskItem,
      HtmlPassthrough,
      SourceMappedMarkdown,
    ],
    content: raw,
    contentType: "markdown",
  });
  return editor.state.doc;
}
const link = `[Details](https://example.com/${"x".repeat(2000)})`;
// Brackets identify the expected raw range independently of the implementation.
const cases: [string, string, number, string?][] = [
  ["heading marker", "# ⟦#⟧ hello\n\n#", 0, "#"],
  ["ordered list marker", "1. ⟦1⟧\n2. 1", 0, "1"],
  ["single-tilde strikethrough", `~word~ **⟦word⟧** word\n\n${link}`, 1],
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
  ["fence language", "word\n\n```word\n⟦word⟧ word\n```", 1],
  ["indented code", "word\n\n    word\n    ⟦word⟧", 2],
  ["inline code", "word ` ⟦word⟧ ` word", 1],
  ["emoji/CRLF", "😀 word\r\n\r\n😀 ⟦word⟧ word", 1],
  ["Chinese", `词语 **⟦词语⟧** 词语\n\n${link}`, 1, "词语"],
  ["accented word", `café **⟦café⟧** café\n\n${link}`, 1, "café"],
  ["overlapping matches", `aaaa ⟦aa⟧\n\n${link}`, 3, "aa"],
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
      expect(lexer).toHaveBeenCalledTimes(0);
      const shifted = markdownSourceMap(doc, "\n" + raw, editor.markdown);
      expect(shifted?.starts[0]).toBe(first!.starts[0] + 1);
      expect(lexer).toHaveBeenCalledTimes(1);
      const edited = editor.schema.nodeFromJSON({
        type: "doc",
        content: [{ type: "paragraph", content: [{ type: "text", text: "word word" }] }],
      });
      expect(markdownSourceMap(edited, raw, editor.markdown)).toBeNull();
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
      expect(lexer).toHaveBeenCalledTimes(0);
    } finally {
      lexer.mockRestore();
    }
  });

  it("retains the fallback for token/source transforms it cannot verify", () => {
    const raw = "| A | B |\n| --- | --- |\n| a\\|b | word |\n\nword";
    const doc = parse(raw);
    expect(markdownSourceMap(doc, raw, editor.markdown)).toBeNull();
    const comment = {
      start_index: raw.lastIndexOf("word"),
      end_index: raw.length,
      anchor_content: "word",
    } as Comment;
    expect(findPmRangeForComment(doc, comment, raw, editor.markdown)).toEqual(
      findPmRangeForComment(doc, comment, raw),
    );
  });

  it("preserves parsing and serialization while keeping editor token caches separate", () => {
    const raw = "# Title\n\n[Details](word) word wo**rd**\n\n- item\n";
    const doc = parse(raw);
    const plain = new Editor({
      element: null,
      extensions: [StarterKit, Markdown],
      content: raw,
      contentType: "markdown",
    });
    const other = new Editor({
      element: null,
      extensions: [StarterKit, SourceMappedMarkdown],
      content: "Other word",
      contentType: "markdown",
    });
    const lexer = vi.spyOn(editor.markdown!.instance.Lexer.prototype, "lex");
    try {
      expect(editor.getJSON()).toEqual(plain.getJSON());
      expect(editor.getMarkdown()).toBe(plain.getMarkdown());
      expect(editor.markdown!.instance).not.toBe(other.markdown!.instance);
      expect(markdownSourceMap(other.state.doc, "Other word", other.markdown)).not.toBeNull();
      expect(markdownSourceMap(doc, raw, editor.markdown)).not.toBeNull();
      expect(lexer).toHaveBeenCalledTimes(0);
      const replacement = "word **word**";
      editor.commands.setContent(replacement, { contentType: "markdown" });
      expect(lexer).toHaveBeenCalledTimes(1);
      expect(markdownSourceMap(editor.state.doc, replacement, editor.markdown)).not.toBeNull();
      expect(lexer).toHaveBeenCalledTimes(1);
    } finally {
      lexer.mockRestore();
      plain.destroy();
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

  it("leaves multiword selections on the existing path", () => {
    const raw = `word phrase **word phrase**\n\n${link}`;
    const doc = parse(raw);
    expect(computeSelectionData(13, 24, doc, raw, editor.markdown)).toEqual(
      computeSelectionData(13, 24, doc, raw),
    );
  });
});
