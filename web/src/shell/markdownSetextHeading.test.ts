import { describe, expect, it, vi } from "vitest";
import { MarkdownManager } from "@tiptap/markdown";
import { StarterKit } from "@tiptap/starter-kit";
import { installIterativeSetextHeadings, scanSetextHeading } from "./markdownSetextHeading";
import { installMarkdownParserPatch } from "./tiptapMarkdownPatches";

const manager = new MarkdownManager({ extensions: [StarterKit] });

describe("iterative setext headings", () => {
  it.each([true, false])("preserves upstream heading fields with GFM=%s", (gfm) => {
    const lines = [
      "Title",
      "  Title  ",
      "Title \t",
      "",
      " ",
      "\t",
      "  continued",
      "    code",
      "# heading",
      "#word",
      "- item",
      " - item",
      "-\titem",
      "1. item",
      "  1. item",
      "   1) item",
      "> quote",
      "```",
      "~~~",
      "<div>",
      "<x>text",
      "| --- |",
      "---",
      "===",
      "  === ",
      "---- x",
      "**bold**",
      "x\u2028y",
      "x\r",
    ];
    for (const first of lines) {
      for (const second of lines) {
        for (const ending of ["", "\n===\n\nAfter", "\n---\nAfter"]) {
          const source = `${first}\n${second}${ending}`;
          const tokenizer = new manager.instance.Tokenizer({ gfm });
          tokenizer.lexer = new manager.instance.Lexer({ gfm, tokenizer });
          const token = tokenizer.lheading(source);
          expect(scanSetextHeading(source, gfm), source).toEqual(
            token
              ? {
                  type: token.type,
                  raw: token.raw,
                  depth: token.depth,
                  text: token.text,
                }
              : undefined,
          );
        }
      }
    }
  });

  it("registers once when managers share a marked instance", () => {
    const first = new MarkdownManager({ extensions: [StarterKit] });
    const second = new MarkdownManager({ extensions: [StarterKit] });
    expect(first.instance).toBe(second.instance);
    const use = vi.spyOn(first.instance, "use");
    installIterativeSetextHeadings(first.instance);
    installIterativeSetextHeadings(first.instance);
    installIterativeSetextHeadings(second.instance);
    expect(use).toHaveBeenCalledTimes(1);
    use.mockRestore();
  });

  it("keeps inline token content and delegates the pedantic grammar", () => {
    const sources = ["Title\n===\n\nAfter", "one\ntwo\n---\n", "#word\n===\n"];
    const expected = sources.map((source) =>
      new manager.instance.Lexer({ pedantic: true }).lex(source),
    );
    installMarkdownParserPatch();
    manager.parse("Title\n===\n");
    const lex = (source: string, pedantic = false) =>
      new manager.instance.Lexer({ ...manager.instance.defaults, pedantic }).lex(source);
    expect(sources.map((source) => lex(source, true))).toEqual(expected);
    const heading = lex("**Title**\n===\n")[0];
    expect(heading).toMatchObject({
      type: "heading",
      depth: 1,
      text: "**Title**",
      tokens: [{ type: "strong", text: "Title" }],
    });
  });

  it.each(["", "\n", "\n===\n", "\ncontinued\n---\n"])(
    "parses a 10 MiB line through the production manager patch with suffix %j",
    (suffix) => {
      installMarkdownParserPatch();
      const text = "y".repeat(10 * 1024 * 1024);
      const doc = manager.parse(text + suffix);
      const heading = suffix.includes("=") || suffix.includes("-");
      expect(doc.content).toHaveLength(1);
      expect(doc.content?.[0].type).toBe(heading ? "heading" : "paragraph");
      expect(doc.content?.[0].content?.map((node) => node.text).join("")).toBe(
        text + (suffix.includes("continued") ? "\ncontinued" : ""),
      );
    },
  );
});
