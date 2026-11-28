import { describe, expect, it } from "vitest";
import { OrderedList } from "@tiptap/extension-list";
import type { MarkdownToken } from "@tiptap/core";
import { MarkdownManager } from "@tiptap/markdown";
import { StarterKit } from "@tiptap/starter-kit";
import fixture from "../../../tests/browser_ui/files/markdown_editor_journey.md?raw";
import { AlignedOrderedList } from "./tiptapOrderedList";

// Use one manager: the default marked instance shares tokenizer registrations.
const manager = new MarkdownManager({
  extensions: [StarterKit.configure({ orderedList: false }), AlignedOrderedList],
});

function lexerHelpers() {
  const lexer = new manager.instance.Lexer(manager.instance.defaults);
  return {
    inlineTokens: (source: string) => lexer.inlineTokens(source),
    blockTokens: (source: string) => lexer.blockTokens(source),
  };
}

const upstream = OrderedList.config.markdownTokenizer!;
const aligned = AlignedOrderedList.config.markdownTokenizer!;

function itemTokens(source: string): MarkdownToken[][] {
  const token = aligned.tokenize(source, [], lexerHelpers()) as MarkdownToken;
  return (token.items ?? []).map((item) => item.tokens ?? []);
}

// Per-item source line counts, including nested lists, locate every item boundary.
function itemLineCounts(token: MarkdownToken | undefined): unknown {
  return token?.items?.map((item) => [
    item.raw?.split("\n").length,
    (item.tokens ?? []).filter((child) => child.type === "list").map(itemLineCounts),
  ]);
}

function paragraphTexts(tokens: MarkdownToken[]): string[] {
  return tokens.flatMap((token) =>
    token.type === "paragraph"
      ? [token.text ?? ""]
      : (token.items ?? []).flatMap((item) => paragraphTexts(item.tokens ?? [])),
  );
}

const reported = [
  "1. **Setup:** first",
  "",
  "2. **Task:**",
  "",
  "   | Field | Value |",
  "   |---|---|",
  "   | Task name | `stitch_lineage` |",
  "",
  "   Paste this:",
  "",
  "   ```json",
  "   {",
  '     "consumer_schema": "<hub catalog>.lineage_demo"',
  "   }",
  "   ```",
  "",
  "   For every table in the shared lineage, the job creates a view that",
  "   links the lineage to",
  "   it.",
  "",
  "3. **Trigger:** run",
].join("\n");

describe("AlignedOrderedList tokenizer", () => {
  const listOfLines = (count: number) =>
    Array.from({ length: count }, (_, index) => (index % 2 ? "   continuation" : `${index}. item`));
  const shapes = [
    "",
    "After",
    "paragraph\n1. not a list start",
    "(216) 555-1234",
    "1. one\n2. two\n\nAfter",
    "1. one\n   continuation\n2. two",
    "1. one\nlazy continuation\n\nAfter",
    "1. one\n\nAfter",
    "1. one\n\n   para\n   wrapped\n\n2. two\nAfter",
    "10. ten\n    continuation\n\n    para\n11. eleven",
    "1) paren\n   continuation\n2) two",
    "a. alpha\n   continuation\nb. beta",
    "i. roman\n   continuation\nii. two",
    "1.  wide gap\n    continuation\n\n    ```js\n    x\n    ```\n2.  two",
    "1. under\n continuation\n\n  shallow\n2. next",
    "1. parent\n   1. child\n      child continuation\n   2. child two\n2. sibling",
    "  1. indented list\n     continuation\nAfter",
    "1. item\n   - bullet\n     bullet continuation\n2. two",
    "1. item\n\tTab continuation\n2. two",
    "1. item\n   > quote\n\n       indented code\n2. two",
    "1. item\r\n   continuation\r\n\r\n2. two",
    "1.\n   empty first line\n2. two",
    "1.   \n   para\n\n       indented\n2. two",
    "1.      wide\n\n   para\n\n       indented\n2. two",
    "1.  wide\n\n    ```\n    1. step\n    next\n    ```",
    reported,
    listOfLines(63),
    listOfLines(64),
    `${listOfLines(64).join("\n")}\nAfter`,
    `${listOfLines(130).join("\n")}\n\nAfter`,
    fixture,
  ].map((shape) => (Array.isArray(shape) ? shape.join("\n") : shape));
  const sources = shapes.flatMap((source) => {
    const lines = source.split("\n");
    return lines.map((_, index) => lines.slice(index).join("\n"));
  });

  it("consumes the same source and item lines as upstream", () => {
    for (const source of sources) {
      const expected = upstream.tokenize(source, [], lexerHelpers()) as MarkdownToken | undefined;
      const actual = aligned.tokenize(source, [], lexerHelpers()) as MarkdownToken | undefined;
      expect(actual?.raw, source).toBe(expected?.raw);
      expect(actual?.start, source).toBe(expected?.start);
      expect(itemLineCounts(actual), source).toEqual(itemLineCounts(expected));
    }
  });

  it("keeps blocks after a blank line on the item's content column", () => {
    const [, task] = itemTokens(reported);
    const blocks = task.filter((token) => token.type !== "space");
    expect(blocks.map((token) => token.type)).toEqual([
      "paragraph",
      "table",
      "paragraph",
      "code",
      "paragraph",
    ]);
    expect(blocks[3].raw).toMatch(/^```json\n\{\n {2}"consumer_schema"/);
    expect(blocks[4].text).toBe(
      "For every table in the shared lineage, the job creates a view that\nlinks the lineage to\nit.",
    );
  });

  it.each([
    ["1. one\n\n   para\n   wrapped", "para\nwrapped"],
    ["10. ten\n\n    para\n    wrapped", "para\nwrapped"],
    ["1.  wide gap\n\n    para\n    wrapped", "para\nwrapped"],
    ["1. one\n   1. child\n\n      para\n      wrapped", "para\nwrapped"],
  ])("aligns continuation paragraphs in %j", (source, text) => {
    expect(paragraphTexts(itemTokens(source).flat())).toContain(text);
  });

  it.each(["1.   \n   para\n\n       indented", "1.      wide\n\n   para\n\n       indented"])(
    "keeps indented code past a one-column marker gap in %j",
    (source) => {
      const code = itemTokens(source)[0].filter((token) => token.type === "code");
      expect(code.map((token) => token.text)).toEqual(["indented"]);
    },
  );
});
