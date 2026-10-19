import { afterEach, describe, expect, it, vi } from "vitest";
import { TaskItem, TaskList } from "@tiptap/extension-list";
import { Table, TableCell, TableHeader, TableRow } from "@tiptap/extension-table";
import { MarkdownManager } from "@tiptap/markdown";
import { StarterKit } from "@tiptap/starter-kit";
import fixture from "../../../tests/browser_ui/files/markdown_editor_journey.md?raw";
import { BoundedTable, BoundedTaskList } from "./tiptapBoundedTokenizers";

// Use one manager: the default marked instance shares tokenizer registrations.
const manager = new MarkdownManager({
  extensions: [
    StarterKit,
    BoundedTaskList,
    TaskItem.configure({ nested: true }),
    BoundedTable,
    TableRow,
    TableCell,
    TableHeader,
  ],
});

function lexerHelpers() {
  const lexer = new manager.instance.Lexer(manager.instance.defaults);
  return {
    inlineTokens: (source: string) => lexer.inlineTokens(source),
    blockTokens: (source: string) => lexer.blockTokens(source),
  };
}

afterEach(() => vi.restoreAllMocks());

describe("bounded tokenizer compatibility", () => {
  const shapes = [
    "",
    "\n \n\t\n",
    "- [ ] todo\n- [x] done\n\nAfter",
    "- [X] upper\n+ [ ] plus\n* [x] star\nAfter",
    "\n\n- [ ] leading blanks\n- [ ] sibling\nAfter",
    "- [ ]\n- [ ] second\nAfter",
    "- [ ] \n- [ ] second\nAfter",
    "- [] missing space\n- [z] invalid\nAfter",
    "- [ ] item\nlazy continuation\n\nAfter",
    "- [ ] item\n\n- [x] separated\n\nAfter",
    "- [ ] item\n \n\t\n- [x] separated\n\nAfter",
    "- [ ] parent\n  - [ ] child\n\n  - [x] child two\n\n- [ ] sibling\nAfter",
    "- [ ] parent\n\n  continuation\n\n  - [ ] child\n\n- [x] sibling\nAfter",
    "  - [ ] indented\n\n - [x] shallower\n\n  content\nOutside",
    "- [ ] item\r\n \r\n- [x] sibling\r\nAfter",
    "- [ ] item\n\n\n\nAfter",
    "- [ ] parent\n  - [x] child\n  - [ ] child two\n- [ ] sibling\nAfter",
    "- [ ] parent\n  - [ ] child\n    - [x] grandchild\n  prose\nAfter",
    "- [ ] item\n  continuation\n\n  more content\nOutside",
    "- [ ] item\n- plain bullet\n- [x] sibling\nAfter",
    "- [ ] parent\n  - [x] child\n  - plain bullet\n  1. ordered\nAfter",
    "  - [ ] indented\n - [x] shallower\n  content\nOutside",
    "  - [ ] indented\n\tcontent\nOutside",
    "\t- [ ] tab item\n  - [ ] space child\nOutside",
    "- [ ] item\n\t- [x] tab child\nOutside",
    "- [ ] item\n  ```python\n  print('hello')\n  ```\nAfter",
    "- [ ] item\n  > quote\n\n  | a | b |\n  | - | - |\n  | 1 | 2 |\nAfter",
    "- [ ] item\r\n  nested\r\n\r\nAfter",
    "\u00a0- [ ] item\n\u00a0\u00a0nested\nAfter",
    "- [ ] item\n\u00a0\nAfter",
    "a | b\n- | -\n1 | 2\n\nAfter",
    "| a | b |\n| :- | -: |\n| `x|y` | z |\n\nAfter",
    "| a |\n| - |\n| x |\n \nAfter",
    "header\n---\nAfter",
    "paragraph\n| a | b |\n| - | - |\n| x | y |",
    fixture,
  ];
  const sources = shapes.flatMap((source) => {
    const lines = source.split("\n");
    return lines.map((_, index) => lines.slice(index).join("\n"));
  });

  it("preserves upstream task tokens, consumed source, and nested content", () => {
    const upstream = TaskList.config.markdownTokenizer!;
    const bounded = BoundedTaskList.config.markdownTokenizer!;
    for (const source of sources) {
      expect(bounded.tokenize(source, [], lexerHelpers()), source).toEqual(
        upstream.tokenize(source, [], lexerHelpers()),
      );
    }
  });

  it("preserves table detection without replacing table tokenization", () => {
    const upstream = Table.config.markdownTokenizer!;
    const bounded = BoundedTable.config.markdownTokenizer!;
    expect(bounded.tokenize).toBe(upstream.tokenize);
    expect(typeof upstream.start).toBe("function");
    expect(typeof bounded.start).toBe("function");
    for (const source of sources) {
      expect((bounded.start as (source: string) => number)(source), source).toBe(
        (upstream.start as (source: string) => number)(source),
      );
    }
  });

  it("registers the bounded extensions without dropping ordered lists or tables", () => {
    const doc = manager.parse("1. ordered\n\n- [x] task\n\n| a | b |\n| - | - |\n| `x|y` | z |");
    expect(doc.content?.map((node) => node.type)).toEqual(["orderedList", "taskList", "table"]);
    expect(doc.content?.[1].content?.[0].attrs?.checked).toBe(true);
    expect(doc.content?.[2].content?.[1].content).toHaveLength(2);
  });
});

describe("bounded line splitting", () => {
  const workloads = [
    { name: "CUJ fixture", block: fixture },
    { name: "blank-separated tasks", block: "- [ ] task\n\n" },
    { name: "whitespace-separated tasks", block: "- [ ] task\n \t\n\n" },
  ].flatMap((workload) => [16, 64].map((kib) => ({ ...workload, kib })));

  it.each(workloads)("bounds line splitting in $kib KiB of $name", ({ block, kib, name }) => {
    const repeats = Math.ceil((kib * 1024) / block.length);
    const source = block.repeat(repeats);
    const split = String.prototype.split;
    let splitCharacters = 0;
    vi.spyOn(String.prototype, "split").mockImplementation(function (this: string, ...args) {
      const separator: unknown = args[0];
      if (separator === "\n") splitCharacters += this.length;
      return split.apply(this, args);
    });
    const doc = manager.parse(source);
    expect(doc.content?.length).toBeGreaterThan(0);
    if (name !== "CUJ fixture") {
      const items = doc.content?.flatMap((node) => (node.type === "taskList" ? node.content! : []));
      expect(items).toHaveLength(repeats);
    }
    expect(splitCharacters).toBeLessThan(source.length * 16);
  });
});
