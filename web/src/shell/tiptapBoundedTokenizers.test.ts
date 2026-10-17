// The bounded list/table tokenizers must parse exactly like the upstream ones
// while reading only a prefix of the remaining document, so a large file's
// parse grows with its size rather than with size × block count.

import { describe, expect, it } from "vitest";
import type { MarkdownLexerConfiguration, MarkdownTokenizer, Node } from "@tiptap/core";
import { MarkdownManager, Markdown } from "@tiptap/markdown";
import { StarterKit } from "@tiptap/starter-kit";
import { Table, TableRow, TableCell, TableHeader } from "@tiptap/extension-table";
import { OrderedList, TaskItem, TaskList } from "@tiptap/extension-list";
import {
  BoundedOrderedList,
  BoundedTable,
  BoundedTaskList,
  orderedListPrefix,
  tableStartPrefix,
  tableTokenizePrefix,
  taskListPrefix,
} from "./tiptapBoundedTokenizers";

describe("orderedListPrefix", () => {
  it("declines when the first line is not an ordered item", () => {
    expect(orderedListPrefix("Plain paragraph\n1. later")).toBeNull();
    expect(orderedListPrefix("- bullet\n1. later")).toBeNull();
    expect(orderedListPrefix("")).toBeNull();
  });

  it("ends before the first unindented line that follows a blank line", () => {
    const md = "1. one\n2. two\n\nAfter\n\n3. not this list";
    expect(orderedListPrefix(md)).toBe("1. one\n2. two\n");
  });

  it("keeps blank-separated items, indented content and lazy continuation", () => {
    const list = "1. one\n\n   indented\n\n2. two\nlazy line\n\n3. three";
    // The trailing newline is the blank line upstream absorbs into the last item.
    expect(orderedListPrefix(`${list}\n\nOutside`)).toBe(`${list}\n`);
  });

  it("returns the whole source when the list runs to the end", () => {
    expect(orderedListPrefix("1. one\n2. two")).toBe("1. one\n2. two");
  });
});

describe("taskListPrefix", () => {
  it("declines unless the first non-blank line is a task item", () => {
    expect(taskListPrefix("- bullet\n- [ ] later")).toBeNull();
    expect(taskListPrefix("\n\n")).toBeNull();
  });

  it("ends before the first non-item line indented no deeper than the item", () => {
    const list = "\n- [ ] one\n  nested text\n\n  - [x] child\n- [ ] two";
    expect(taskListPrefix(`${list}\nOutside\n- [ ] later`)).toBe(list);
  });

  it("treats a shallower item as the new reference indent", () => {
    const list = "  - [ ] deep\n- [ ] shallow\n  content of shallow";
    expect(taskListPrefix(`${list}\nstop`)).toBe(list);
  });
});

describe("table prefixes", () => {
  it("gives start only the first two lines", () => {
    expect(tableStartPrefix("| a |\n| - |\n| 1 |\n\nmore")).toBe("| a |\n| - |");
    expect(tableStartPrefix("single line")).toBe("single line");
  });

  it("gives tokenize the block before the first blank line plus one more line", () => {
    expect(tableTokenizePrefix("| a |\n| - |\n| 1 |\n\nnext\n\nlater")).toBe(
      "| a |\n| - |\n| 1 |\n\nnext",
    );
    expect(tableTokenizePrefix("| a |\n| - |")).toBe("| a |\n| - |");
  });

  it("gives tokenize only two lines when the second is not a delimiter row", () => {
    expect(tableTokenizePrefix("para one\npara two\n \nmore\n \n| a |\n| - |")).toBe(
      "para one\npara two",
    );
    expect(tableTokenizePrefix("single line")).toBe("single line");
  });

  it("ends a table candidate at a whitespace-only line as well", () => {
    expect(tableTokenizePrefix("| a |\n| - |\n| 1 |\n \nnext\n \nlater")).toBe(
      "| a |\n| - |\n| 1 |\n \nnext",
    );
  });
});

const SECTION = `## Section

Prose with **bold**, _emphasis_ and \`code\`.

- Input queue
- Output queue

1. Validate the request
2. Transform the payload
3. Publish the result

| Field | Type | Notes |
| ----- | ---- | ----- |
| id | string | Primary key |
| state | enum | queued, running, done |

\`\`\`python
def handle(payload: dict) -> dict:
    return {"state": "done"}
\`\`\`

> Stay idempotent because retries replay the request.

- [ ] follow up
- [x] shipped

`;

/** Markdown of at least `targetBytes` built from repeated sections. */
function largeMarkdown(targetBytes: number): string {
  const parts = ["# Large design document\n\n"];
  let size = parts[0].length;
  while (size < targetBytes) {
    parts.push(SECTION);
    size += SECTION.length;
  }
  return parts.join("");
}

// One manager only: MarkdownManager registers tokenizers on the module-wide
// marked instance, so a second manager would add its tokenizers to this one.
const boundedManager = new MarkdownManager({
  extensions: [
    StarterKit.configure({ orderedList: false }),
    BoundedOrderedList,
    BoundedTaskList,
    TaskItem.configure({ nested: true }),
    BoundedTable,
    TableRow,
    TableCell,
    TableHeader,
    Markdown,
  ],
});

/** The private lexer surface a tokenizer receives from marked. */
interface LexerFactory {
  createLexer: () => unknown;
  createTokenizerHelpers: (lexer: unknown) => MarkdownLexerConfiguration;
}

function tokenizerOf(extension: Node): MarkdownTokenizer {
  const tokenizer = extension.config.markdownTokenizer;
  if (!tokenizer) throw new Error(`${extension.name} has no markdown tokenizer`);
  return tokenizer;
}

/** The manager's lexer factory, or a clear error when the private surface moved. */
function lexerFactory(manager: MarkdownManager): LexerFactory {
  const factory = manager as unknown as Partial<LexerFactory>;
  if (
    typeof factory.createLexer !== "function" ||
    typeof factory.createTokenizerHelpers !== "function"
  ) {
    throw new Error("MarkdownManager no longer exposes createLexer/createTokenizerHelpers");
  }
  return factory as LexerFactory;
}

describe("bounded tokenizers tokenize like upstream", () => {
  const manager = lexerFactory(boundedManager);
  const lexer = () => manager.createTokenizerHelpers(manager.createLexer());
  const PAIRS: [string, MarkdownTokenizer, MarkdownTokenizer][] = [
    ["orderedList", tokenizerOf(OrderedList), tokenizerOf(BoundedOrderedList)],
    ["taskList", tokenizerOf(TaskList), tokenizerOf(BoundedTaskList)],
    ["table", tokenizerOf(Table), tokenizerOf(BoundedTable)],
  ];
  const SHAPES = [
    "1. one\n2. two\n3. three",
    "1. one\n\n2. two\n\n3. three\n\nAfter the list.",
    "1. one\ncontinues lazily\n2. two",
    "1. one\n## Heading right after\ntext",
    "1. one\n   indented continuation\n\n   still inside\n\nOutside paragraph.",
    "1. outer\n   1. nested\n   2. nested two\n2. outer two\n\nParagraph.",
    "i. roman\nii. roman two\n\nText",
    "a) alpha\nb) beta\n\nText",
    "3. starts at three\n4. four",
    "1. item\n- bullet right after\ntext",
    "1. item\n```\ncode fence\n```\nafter",
    "1. item\n\n> quote after blank",
    "1. item\n\n| a | b |\n| - | - |\n| 1 | 2 |",
    "Paragraph before\n1. one\n2. two\n\nParagraph after",
    "1. only one item",
    "1. item\n\n\n\nFar paragraph",
    "- [ ] todo\n- [x] done\n\nText after",
    "- [ ] todo\n\n- [x] separated",
    "- [ ] parent\n  - [ ] child\n  - [x] child two\n- [ ] sibling\n\nText",
    "- [ ] item\n  continuation text\n\n  more content\nOutside",
    "\n\n- [ ] leading blanks\n- [ ] second\nText",
    "- [ ] item\n- plain bullet\ntext",
    "  - [ ] indented first\n  - [ ] second\n para at lower indent",
    "| a | b |\n| - | - |\n| 1 | 2 |\n\nParagraph",
    "| a | b |\n| - | - |\n| 1 | 2 |\nParagraph directly after",
    "| a | b |\n| - | - |\n| 1 | 2 |\n\n| c | d |\n| - | - |\n| 3 | 4 |",
    "| a | b |\n| - | - |",
    "Text\n\n| a | b |\n|---|---|\n| x | y |\n\n- [ ] after table",
    "| not | a table\nsecond line",
    "a | b\n- | -\n1 | 2\n\nmore",
    // Shapes where a prefix could decline or cut while upstream consumes.
    "1.\n2. two\n\ntext",
    "1. one\n\n   indented\nlazy\n\nafter",
    "1. one\n\tindented with a tab\n\n2. two\n\nafter",
    "\t1. tab-indented item\n\t2. two\nafter",
    "- [ ]\n- [ ] second",
    "- [ ] one\nlazy line\n\nAfter",
    "- [ ] a\n\t- [ ] tab child\nText",
    "\t- [ ] tab item\n\tcontent\nText",
    "para one\npara two\n| a | b |\n| - | - |\n| 1 | 2 |",
    "para\n| a | b |\n| - | - |\n| 1 | 2 |\n\nafter",
    "para\n \n| a | b |\n| - | - |\n| 1 | 2 |\n \nafter",
    "| a | b |\n| - | - |\n| 1 | 2 |\n \nparagraph",
    "| a | b |\n| - | - |\n| 1 | 2 |\n\t\nafter a tab-only line",
    "| a | b |\n| - | - |\n \nno data rows",
    "| a | b |\n| - | - |\n| 1 | 2 |\n \nlater a | `b`\n\nend",
    "| a | b |\n| - | - |\n| `x|y` | 2 |\n \nafter",
    "a | b\n- | -\n1 | 2\n \n3 | `4`\n\nend",
    "  - [ ] item\n\tcontent\nText",
    "  1. item\n\tcontent\n\nText",
    "\t- [ ] tab item\n  - [ ] space child\nText",
    largeMarkdown(4 * 1024),
  ];

  // marked hands tokenizers the remaining document from each block start, so
  // every shape is also tried from each of its later lines.
  const SOURCES = SHAPES.flatMap((md) => {
    const lines = md.split("\n");
    return lines.map((_, index) => lines.slice(index).join("\n"));
  });

  it.each(PAIRS)("%s tokenize matches upstream on every source", (_name, upstream, bounded) => {
    for (const src of SOURCES) {
      expect(bounded.tokenize(src, [], lexer()), src).toEqual(upstream.tokenize(src, [], lexer()));
    }
  });

  it("table start matches upstream on every source", () => {
    const [, upstream, bounded] = PAIRS[2];
    for (const src of SOURCES) {
      expect(bounded.start, src).toBeTypeOf("function");
      expect((bounded.start as (s: string) => number)(src), src).toBe(
        (upstream.start as (s: string) => number)(src),
      );
    }
  });

  it("registers the bounded tokenizers with the manager", () => {
    // CommonMark lists need digits; only the extension tokenizer reads roman markers.
    const doc = boundedManager.parse("i. roman\nii. two\n\n- [ ] task\n\na | b\n- | -\n1 | 2");
    expect(doc.content?.map((node) => node.type)).toEqual(["orderedList", "taskList", "table"]);
  });
});

describe("prefixes stay proportional to the document", () => {
  // marked calls the tokenizers at block starts, a subset of these line starts;
  // an unbounded prefix would hand over the whole remainder each time.
  const DOCUMENTS: [string, string][] = [
    ["blank lines", largeMarkdown(256 * 1024)],
    // Blank lines with trailing whitespace never match a literal "\n\n".
    ["whitespace-only blank lines", largeMarkdown(256 * 1024).replaceAll("\n\n", "\n \n")],
  ];

  it.each(DOCUMENTS)("hands upstream a bounded multiple of a document with %s", (_name, md) => {
    let handed = 0;
    let start = 0;
    while (start < md.length) {
      const src = md.slice(start);
      handed +=
        (orderedListPrefix(src)?.length ?? 0) +
        (taskListPrefix(src)?.length ?? 0) +
        tableStartPrefix(src).length +
        tableTokenizePrefix(src).length;
      const newline = md.indexOf("\n", start);
      start = newline === -1 ? md.length : newline + 1;
    }
    expect(handed).toBeLessThan(md.length * 16);
  });
});
