import { afterEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { createMarkdownDocumentExtensions } from "./markdownDocumentExtensions";
import { scanFootnoteDefinitions } from "./TipTapFootnotes";
import {
  installMarkdownParserPatch,
  installMarkdownSerializerPatch,
} from "./tiptapMarkdownPatches";

installMarkdownSerializerPatch();
installMarkdownParserPatch();

let editors: Editor[] = [];
afterEach(() => {
  for (const ed of editors) ed.destroy();
  editors = [];
});

function makeEditor(markdown: string): Editor {
  const ed = new Editor({
    element: document.createElement("div"),
    extensions: createMarkdownDocumentExtensions("conv_test1", "notes.md"),
    content: markdown,
    contentType: "markdown",
  });
  editors.push(ed);
  return ed;
}

/** Parse + serialise; the serialiser drops only the final newline. */
function roundTrip(markdown: string): string {
  return makeEditor(markdown).getMarkdown();
}

/** Position of the nth node of *type* in the editor's document. */
function nodePosition(ed: Editor, type: string, nth = 0): number {
  const positions: number[] = [];
  ed.state.doc.descendants((node, pos) => {
    if (node.type.name === type) positions.push(pos);
  });
  return positions[nth];
}

/** Footnote node types and attributes, in document order. */
function footnoteNodes(ed: Editor): string[] {
  const found: string[] = [];
  ed.state.doc.descendants((node) => {
    if (node.type.name.startsWith("footnote")) {
      found.push(`${node.type.name}:${String(node.attrs.label ?? node.attrs.raw)}`);
    }
  });
  return found;
}

const PR_DESCRIPTION = `Title: Update limits

This change updates the limits so every value passes.[^1] It uses the same rule as PR 42.[^2]

[^1]: https://example.com/issues/1?focusedCommentId=2
[^2]: https://example.com/pulls/42

There is no behavior change.
`;

const TWO_NOTES =
  "Claim one.[^1] Claim two.[^2]\n\n[^1]: https://a.test\n[^2]: https://b.test\n\nAfter.";

describe("footnote round-trips", () => {
  it.each([
    ["definitions between paragraphs", PR_DESCRIPTION],
    ["definitions at the end without a newline", "Claim.[^a]\n\n[^a]: https://example.com/a"],
    ["only definitions", "[^1]: only\n[^2]: definitions"],
    ["definitions before their references", "[^1]: first\n\nThen text[^1]."],
    ["blank lines between definitions", "Claim.[^1][^2]\n\n[^1]: one\n\n\n[^2]: two\n\nAfter."],
    ["lazy continuation line", "Claim.[^1]\n\n[^1]: first line\nsecond line\n\nAfter."],
    [
      "indented continuation paragraphs and code",
      "Claim.[^1]\n\n[^1]: first\n\n    second\n\n    ```\n    code\n    ```\n\nAfter.",
    ],
    ["three-space indented definition", "Claim.[^1]\n\n   [^1]: indented"],
    ["no space after the colon", "Claim.[^1]\n\n[^1]:https://nospace.test"],
    ["trailing spaces and tabs", "Claim.[^1]\n\n[^1]: note   \n[^2]:\ttab"],
    ["definition line inside a paragraph", "text\n[^1]: inside the paragraph"],
    ["adjacent references", "x[^1][^2][^3]\n\n[^1]: a\n[^2]: b\n[^3]: c"],
    ["reference followed by a colon", "see[^1]: here\n\n[^1]: note"],
    ["named labels", "a[^long-label_2] b[^Ü] c[^x.y]\n\n[^long-label_2]: 1\n[^Ü]: 2\n[^x.y]: 3"],
    ["caret text that is not a reference", "Not[^ 1] or[^] references."],
    ["escaped reference", "Escaped \\[^1] text.\n\n[^1]: note"],
    ["regex classes in prose", "Match [^a-z] or [^0-9] characters."],
    ["inline code", "Use `[^1]` literally."],
    ["fenced code", "```\n[^1]: not a definition\nref[^1]\n```"],
    ["heading", "# Title[^1]\n\n[^1]: note"],
    ["bullet list", "- one[^1]\n- two\n\n[^1]: note"],
    ["task list", "- [ ] task[^1]\n\n[^1]: note"],
    ["blockquote", "> quoted[^1]\n\n[^1]: note"],
    ["definition inside a blockquote", "> text[^1]\n>\n> [^1]: inside the quote"],
    ["image before a reference", "![img](a.png)[^1]\n\n[^1]: note"],
    ["raw HTML block", "<details>\n<summary>S</summary>\n\nBody[^1]\n\n</details>\n\n[^1]: n"],
  ])("keeps %s unchanged", (_name, markdown) => {
    expect(roundTrip(markdown)).toBe(markdown.replace(/\n$/, ""));
  });

  it.each([
    ["heading", "Claim.[^1]\n\n[^1]: note\n# Heading", "heading"],
    ["list", "Claim.[^1]\n\n[^1]: note\n- item", "bulletList"],
    ["fence", "Claim.[^1]\n\n[^1]: note\n```\ncode\n```", "codeBlock"],
    ["quote", "Claim.[^1]\n\n[^1]: note\n> quoted", "blockquote"],
  ])("ends the definitions at a %s that follows them", (_name, markdown, type) => {
    const ed = makeEditor(markdown);
    expect(ed.state.doc.lastChild?.type.name).toBe(type);
    expect(footnoteNodes(ed)).toEqual(["footnoteReference:1", "footnoteDefinitions:[^1]: note"]);
    // The serialiser separates blocks with a blank line, as after any paragraph.
    expect(ed.getMarkdown()).toBe(markdown.replace("note\n", "note\n\n"));
  });

  it("keeps a table that follows the definitions a table", () => {
    const markdown = "Claim.[^1]\n\n[^1]: note\n| A | B |\n| --- | --- |\n| 1 | 2 |";
    const ed = makeEditor(markdown);
    expect(ed.state.doc.lastChild?.type.name).toBe("table");
    expect(footnoteNodes(ed)).toEqual(["footnoteReference:1", "footnoteDefinitions:[^1]: note"]);
    // The serialiser always puts an extra blank line before a table.
    const saved = ed.getMarkdown();
    expect(saved).toMatch(/\[\^1\]: note\n{2,}\| A +\| B +\|\n\| -+ \| -+ \|\n\| 1 +\| 2 +\|/);
    expect(roundTrip(saved)).toBe(saved);
  });

  it("keeps a lazy line with a pipe in the definition when no table follows", () => {
    expect(scanFootnoteDefinitions("[^1]: note\na | b\n\nAfter.")).toBe("[^1]: note\na | b\n\n");
  });

  it("keeps a fence with a blank line after a definition a code block", () => {
    const saved = roundTrip("[^1]: note\n```js\na\n\nb\n```\nRest[^1].");
    expect(saved).toBe("[^1]: note\n\n```js\na\n\nb\n```\n\nRest[^1].");
    expect(roundTrip(saved)).toBe(saved);
  });

  it.each([
    ["without a definition", "Dangling[^9] reference.", []],
    ["defined only in prose", "Explanation [^x]: inline prose.\n\nA **literal [^x] marker**.", []],
    ["defined only in a code block", "```\n[^1]: x\n```\n\nSee [^1].", []],
    ["in a task item before the definition", "- [ ] task[^1]\n\n[^1]: n", ["footnoteReference:1"]],
    [
      "matching a definition case-insensitively",
      "x[^Note]\n\n[^note]: n",
      ["footnoteReference:Note"],
    ],
  ])("reads [^label] %s", (_name, markdown, references) => {
    const found = footnoteNodes(makeEditor(markdown)).filter((n) =>
      n.startsWith("footnoteReference"),
    );
    expect(found).toEqual(references);
    expect(roundTrip(markdown)).toBe(markdown);
  });

  it.each([
    ["thematic break", "[^1]: n\n***\nText", "horizontalRule"],
    ["HTML block", '[^1]: n\n<div class="x">\n\nText', "htmlPassthrough"],
  ])("leaves the paragraph after a %s editable", (_name, markdown, type) => {
    const blocks: string[] = [];
    makeEditor(markdown).state.doc.forEach((block) => blocks.push(block.type.name));
    expect(blocks).toEqual(["footnoteDefinitions", type, "paragraph"]);
  });

  it("keeps CRLF footnotes, normalising only the line endings", () => {
    expect(roundTrip("Claim.[^1]\r\n\r\n[^1]: note\r\n\r\nAfter.\r\n")).toBe(
      "Claim.[^1]\n\n[^1]: note\n\nAfter.",
    );
  });

  it("keeps a reference in a table cell and the definition after the table", () => {
    const saved = roundTrip("| A | B |\n| --- | --- |\n| x[^1] | y |\n\n[^1]: note");
    expect(saved).toContain("| x[^1] | y   |");
    expect(saved).toMatch(/\n\[\^1\]: note$/);
  });

  // The serialiser closes marks around an inline atom, as it does for images.
  it.each([
    ["at the end of bold text", "Some **claim[^n]** here.", "Some **claim**[^n] here."],
    ["inside bold text", "**bold [^1] text**", "**bold** [^1] **text**"],
    ["inside italic text", "*em [^1] x*", "*em* [^1] *x*"],
  ])("moves a reference %s outside the markup", (_name, markdown, expected) => {
    const definition = "\n\n[^1]: a\n[^n]: b";
    expect(roundTrip(markdown + definition)).toBe(expected + definition);
  });

  // Link text never forms references, so a link keeps its text and URL.
  it.each([
    ["only a reference", "See [[^1]](https://l.test)."],
    ["text and a reference", "[see[^1]](https://l.test)"],
  ])("keeps a link whose text is %s", (_name, markdown) => {
    const source = `${markdown}\n\n[^1]: a`;
    const ed = makeEditor(source);
    expect(ed.view.dom.querySelector("a")?.getAttribute("href")).toBe("https://l.test");
    expect(ed.getMarkdown()).toBe(source);
  });

  it("keeps the URL of a bold link whose text is a reference", () => {
    // TipTap writes bold around a link as `[**text**](url)`, footnote or not.
    const saved = roundTrip("**[[^1]](https://l.test)**\n\n[^1]: a");
    expect(saved).toBe("[**[^1]**](https://l.test)\n\n[^1]: a");
    expect(roundTrip(saved)).toBe(saved);
  });

  it("keeps typed reference syntax in link text unescaped", () => {
    const ed = makeEditor("[see](https://l.test)\n\n[^1]: a");
    ed.commands.insertContentAt(3, "[^1]");
    expect(ed.getMarkdown().replace(/\n+$/, "")).toBe("[se[^1]e](https://l.test)\n\n[^1]: a");
  });

  it.each([
    ["bullet", "- item[^1]\n\n  [^1]: inside the list", "- item[^1]\n  \n  [^1]: inside the list"],
    ["ordered", "1. item[^1]\n\n   [^1]: ordered", "1. item[^1]\n   \n   [^1]: ordered"],
  ])("keeps a definition in a %s list item a separate block", (_name, markdown, expected) => {
    // List items indent every line of a later block, including the blank one.
    const saved = roundTrip(markdown);
    expect(saved).toBe(expected);
    expect(footnoteNodes(makeEditor(saved))).toContain(
      `footnoteDefinitions:${markdown.split("\n").at(-1)!.trim()}`,
    );
    expect(roundTrip(saved)).toBe(saved);
  });
});

describe("editing a document with footnotes", () => {
  it("keeps definitions and references when another paragraph is edited", () => {
    const ed = makeEditor(PR_DESCRIPTION);
    let from = -1;
    ed.state.doc.descendants((node, pos) => {
      if (from === -1 && node.isText && node.text?.startsWith("This change updates")) {
        from = pos + "This ".length;
      }
    });
    ed.commands.deleteRange({ from, to: from + "change ".length });

    expect(ed.getMarkdown()).toBe(
      PR_DESCRIPTION.replace("This change updates", "This updates").replace(/\n$/, ""),
    );
  });

  it.each([
    ["reference", "footnoteReference", TWO_NOTES.replace("one.[^1]", "one.")],
    ["definition block", "footnoteDefinitions", "Claim one.[^1] Claim two.[^2]\n\nAfter."],
  ])("deletes a %s as one unit", (_name, type, expected) => {
    const ed = makeEditor(TWO_NOTES);
    const at = nodePosition(ed, type);
    ed.commands.deleteRange({ from: at, to: at + ed.state.doc.nodeAt(at)!.nodeSize });
    expect(ed.getMarkdown()).toBe(expected);
  });

  it.each([
    ["before", 0, "Claim one.X[^1]"],
    ["after", 1, "Claim one.[^1]X"],
  ])("types text %s a reference", (_name, offset, expected) => {
    const ed = makeEditor(TWO_NOTES);
    ed.commands.insertContentAt(nodePosition(ed, "footnoteReference") + offset, "X");
    expect(ed.getMarkdown()).toBe(TWO_NOTES.replace("Claim one.[^1]", expected));
  });

  it("splits a paragraph after a reference", () => {
    const ed = makeEditor(TWO_NOTES);
    ed.commands.setTextSelection(nodePosition(ed, "footnoteReference") + 1);
    ed.commands.splitBlock();
    expect(ed.getMarkdown()).toBe(TWO_NOTES.replace("[^1] Claim", "[^1]\n\n Claim"));
  });

  it("inserts a paragraph before the definitions", () => {
    const ed = makeEditor(TWO_NOTES);
    ed.commands.setTextSelection(nodePosition(ed, "footnoteDefinitions"));
    ed.commands.insertContent("New paragraph");
    expect(ed.getMarkdown()).toBe(TWO_NOTES.replace("\n\n[^1]:", "\n\nNew paragraph\n\n[^1]:"));
  });

  it.each(["footnoteReference", "footnoteDefinitions"])(
    "ignores typing and pasting over a selected %s",
    (type) => {
      const ed = makeEditor(TWO_NOTES);
      const at = nodePosition(ed, type);
      ed.commands.setNodeSelection(at);
      const typed = ed.view.someProp("handleTextInput", (handle) =>
        handle(ed.view, at, at + 1, "z", () => ed.state.tr),
      );
      const pasted = ed.view.someProp("handlePaste", (handle) =>
        handle(ed.view, new Event("paste") as ClipboardEvent, ed.state.doc.slice(0, 0)),
      );
      expect([typed, pasted]).toEqual([true, true]);
      ed.commands.setTextSelection(1);
      expect(
        ed.view.someProp("handleTextInput", (handle) =>
          handle(ed.view, 1, 1, "z", () => ed.state.tr),
        ),
      ).toBeFalsy();
    },
  );

  it("restores the document on undo", () => {
    const ed = makeEditor(TWO_NOTES);
    ed.commands.insertContentAt(nodePosition(ed, "footnoteReference") + 1, " typed");
    ed.commands.undo();
    expect(ed.getMarkdown()).toBe(TWO_NOTES);
  });

  it.each([
    ["an undefined label as typed", "Plain.", "Plain. typed [^2] literal"],
    [
      "a defined label escaped",
      "Plain.\n\n[^2]: note",
      "Plain. typed \\[^2] literal\n\n[^2]: note",
    ],
  ])("saves typed reference syntax for %s", (_name, markdown, expected) => {
    const ed = makeEditor(markdown);
    ed.commands.insertContentAt(" Plain.".length, " typed [^2] literal");
    // The edit appends an empty paragraph after the definitions; saves trim it.
    const saved = ed.getMarkdown().replace(/\n+$/, "");
    expect(saved).toBe(expected);
    expect(
      footnoteNodes(makeEditor(saved)).filter((n) => n.startsWith("footnoteReference")),
    ).toEqual([]);
  });

  it("keeps reference syntax split across marks literal", () => {
    const ed = makeEditor("[^1]: note");
    ed.commands.insertContentAt(0, {
      type: "paragraph",
      content: [
        { type: "text", text: "see [^" },
        { type: "text", text: "1]", marks: [{ type: "bold" }] },
      ],
    });
    expect(footnoteNodes(makeEditor(ed.getMarkdown()))).toEqual(["footnoteDefinitions:[^1]: note"]);
  });

  it.each([
    ["an opening parenthesis", "a[^1]\n\n[^1]: n", "(x)", "a[^1]\\(x)"],
    ["a colon at the start of a paragraph", "[^1] b\n\n[^1]: n", ": a", "[^1]\\: a b"],
  ])("keeps a reference when %s is typed after it", (_name, markdown, typed, line) => {
    const ed = makeEditor(markdown);
    ed.commands.insertContentAt(nodePosition(ed, "footnoteReference") + 1, typed);
    const saved = ed.getMarkdown().replace(/\n+$/, "");
    expect(saved.split("\n")[0]).toBe(line);
    expect(footnoteNodes(makeEditor(saved))).toEqual([
      "footnoteReference:1",
      "footnoteDefinitions:[^1]: n",
    ]);
  });

  it("keeps a paragraph whose text before a reference becomes whitespace", () => {
    const ed = makeEditor("x[^1]: y\n\n[^1]: n");
    ed.commands.insertContentAt({ from: 1, to: 2 }, " ");
    const saved = ed.getMarkdown().replace(/\n+$/, "");
    expect(saved).toBe(" [^1]\\: y\n\n[^1]: n");
    const blocks: string[] = [];
    makeEditor(saved).state.doc.forEach((block) => blocks.push(block.type.name));
    expect(blocks).toEqual(["paragraph", "footnoteDefinitions"]);
  });

  it("keeps a colon after a reference mid-paragraph unescaped", () => {
    const markdown = "see[^1]: here\n\n[^1]: n";
    const ed = makeEditor(markdown);
    ed.commands.insertContentAt(1, "x");
    expect(ed.getMarkdown().replace(/\n+$/, "")).toBe(`x${markdown}`);
  });

  it.each([
    ["Backspace", "footnoteReference", TWO_NOTES.replace("one.[^1] Claim", "one. Claim")],
    ["Delete", "footnoteDefinitions", "Claim one.[^1] Claim two.[^2]\n\nAfter."],
  ])("removes a selected footnote with %s", (key, type, expected) => {
    const ed = makeEditor(TWO_NOTES);
    ed.commands.setNodeSelection(nodePosition(ed, type));
    ed.view.dom.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    expect(ed.getMarkdown()).toBe(expected);
  });

  it("adds a paragraph instead of replacing a selected definitions block on Enter", () => {
    const ed = makeEditor(TWO_NOTES);
    ed.commands.setNodeSelection(nodePosition(ed, "footnoteDefinitions"));
    ed.view.dom.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(footnoteNodes(ed)).toContain(
      "footnoteDefinitions:[^1]: https://a.test\n[^2]: https://b.test",
    );
  });

  it("splits after a selected reference on Enter instead of deleting it", () => {
    const ed = makeEditor(TWO_NOTES);
    ed.commands.setNodeSelection(nodePosition(ed, "footnoteReference"));
    ed.view.dom.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(ed.getMarkdown()).toBe(TWO_NOTES.replace("[^1] Claim", "[^1]\n\n Claim"));
  });

  it("keeps references and definitions through copy and paste", () => {
    const copied = makeEditor(TWO_NOTES).getHTML();
    const ed = makeEditor("Start.");
    ed.commands.insertContentAt(ed.state.doc.content.size, copied);
    expect(ed.getMarkdown()).toBe(`Start.\n\n${TWO_NOTES}`);
  });

  it.each([
    [
      "a definitions block that is not a definition run",
      '<div data-footnote-definitions=""># heading</div>',
    ],
    [
      "a definitions block with trailing prose",
      '<div data-footnote-definitions="">[^1]: a\n# b</div>',
    ],
    ["a reference label with a space", '<p>x<sup data-footnote-reference="">a b</sup></p>'],
    ["an empty reference label", '<p>x<sup data-footnote-reference=""></sup></p>'],
  ])("does not paste %s as a footnote", (_name, html) => {
    const ed = makeEditor("Start.");
    ed.commands.insertContentAt(ed.state.doc.content.size, html);
    expect(footnoteNodes(ed)).toEqual([]);
  });

  it.each([
    ["toggleBold", "**Claim one.**[^1] **Claim two.**[^2]"],
    ["toggleItalic", "*Claim one.*[^1] *Claim two.*[^2]"],
    ["toggleStrike", "~~Claim one.~~[^1] ~~Claim two.~~[^2]"],
    ["toggleCode", "`Claim one.`[^1] `Claim two.`[^2]"],
  ] as const)("%s over references saves valid, stable markup", (command, expected) => {
    const definitions = "\n\n[^1]: a\n[^2]: b";
    const ed = makeEditor(`Claim one.[^1] Claim two.[^2]${definitions}`);
    ed.commands.selectAll();
    ed.commands[command]();
    const saved = ed.getMarkdown().replace(/\n+$/, "");
    expect(saved).toBe(expected + definitions);
    expect(footnoteNodes(makeEditor(saved))).toEqual([
      "footnoteReference:1",
      "footnoteReference:2",
      "footnoteDefinitions:[^1]: a\n[^2]: b",
    ]);
    expect(roundTrip(saved)).toBe(saved);
  });

  it.each(["toggleBulletList", "toggleTaskList", "toggleBlockquote"] as const)(
    "%s over everything keeps the definitions",
    (command) => {
      const ed = makeEditor(TWO_NOTES);
      ed.commands.selectAll();
      ed.commands[command]();
      const saved = ed.getMarkdown();
      expect(footnoteNodes(makeEditor(saved))).toEqual([
        "footnoteReference:1",
        "footnoteReference:2",
        "footnoteDefinitions:[^1]: https://a.test\n[^2]: https://b.test",
      ]);
      expect(roundTrip(saved)).toBe(saved);
    },
  );
});

describe("footnote rendering", () => {
  it("renders references as superscript labels and definitions as their source", () => {
    const ed = makeEditor("Claim.[^1]\n\n[^1]: https://example.com/a");
    const dom = ed.view.dom;
    expect(dom.querySelector("sup[data-footnote-reference]")?.textContent).toBe("1");
    expect(dom.querySelector("[data-footnote-definitions]")?.textContent).toBe(
      "[^1]: https://example.com/a",
    );
    expect(dom.querySelector("a")).toBeNull();
  });

  it("leaves an inline link whose text starts with a caret as a link", () => {
    const markdown = "See [^1](https://example.com/a) here.";
    const ed = makeEditor(markdown);
    expect(ed.view.dom.querySelector("a")?.getAttribute("href")).toBe("https://example.com/a");
    expect(ed.getMarkdown()).toBe(markdown);
  });
});

describe("scanFootnoteDefinitions", () => {
  it.each<[string, string, string | null]>([
    ["not a definition", "Plain text.\n", null],
    ["link reference definition", "[ref]: https://example.com\n", null],
    ["four-space indent is code", "    [^1]: code\n", null],
    ["label with a space", "[^a b]: no\n", null],
    ["stops before an unindented paragraph", "[^1]: one\n\nAfter.\n", "[^1]: one\n\n"],
    ["keeps a later definition", "[^1]: one\n\n[^2]: two\n\nAfter.", "[^1]: one\n\n[^2]: two\n\n"],
    ["keeps a tab-indented continuation", "[^1]: one\n\n\ttwo\n\nAfter.", "[^1]: one\n\n\ttwo\n\n"],
    ["ends at the end of the source", "[^1]: one\n[^2]: two", "[^1]: one\n[^2]: two"],
    ["stops at trailing blank lines", "[^1]: one\n\n\n", "[^1]: one\n\n\n"],
    ["stops before a *** break", "[^1]: one\n***\nText", "[^1]: one\n"],
    ["stops before a _ _ _ break", "[^1]: one\n_ _ _\nText", "[^1]: one\n"],
    ["stops before a --- break", "[^1]: one\n---\nText", "[^1]: one\n"],
    ["stops before an HTML block", '[^1]: one\n<div class="x">text', "[^1]: one\n"],
    ["stops before an HTML comment", "[^1]: one\n<!-- note -->", "[^1]: one\n"],
    ["keeps an inline HTML line", "[^1]: one\n<span>two</span>", "[^1]: one\n<span>two</span>"],
  ])("%s", (_name, source, expected) => {
    expect(scanFootnoteDefinitions(source)).toBe(expected);
  });
});
