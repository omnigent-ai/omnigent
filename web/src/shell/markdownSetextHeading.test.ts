import { describe, expect, it } from "vitest";
import { MarkdownManager } from "@tiptap/markdown";
import { StarterKit } from "@tiptap/starter-kit";
import { scanSetextHeading } from "./markdownSetextHeading";
import { installMarkdownParserPatch } from "./tiptapMarkdownPatches";

const manager = new MarkdownManager({ extensions: [StarterKit] });
const rules = manager.instance.Lexer.rules.block;

describe("iterative setext headings", () => {
  it.each([true, false])("preserves upstream heading boundaries with GFM=%s", (gfm) => {
    const lines = [
      "Title",
      "",
      " ",
      "\t",
      "  continued",
      "    code",
      "# heading",
      "#word",
      "- item",
      "-\titem",
      "1. item",
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
          const cap = (gfm ? rules.gfm : rules.normal).lheading.exec(source);
          expect(scanSetextHeading(source, gfm), source).toEqual(
            cap
              ? {
                  type: "heading",
                  raw: cap[0],
                  depth: cap[2][0] === "=" ? 1 : 2,
                  text: cap[1].trim(),
                }
              : undefined,
          );
        }
      }
    }
  });

  it("keeps token content and the pedantic grammar when installed repeatedly", () => {
    const sources = ["Title\n===\n\nAfter", "one\ntwo\n---\n", "#word\n===\n"];
    const expected = sources.map((source) =>
      new manager.instance.Lexer({ pedantic: true }).lex(source),
    );
    installMarkdownParserPatch();
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
