import { afterEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { createMarkdownDocumentExtensions } from "./markdownDocumentExtensions";
import { rememberSourceBlocks } from "./markdownSourceBlocks";
import { applyExternalMarkdown } from "./TipTapEditorHelpers";
import {
  installMarkdownParserPatch,
  installMarkdownSerializerPatch,
  markdownKeepingSource,
} from "./tiptapMarkdownPatches";

installMarkdownSerializerPatch();
installMarkdownParserPatch();

let editors: Editor[] = [];
afterEach(() => {
  for (const ed of editors) ed.destroy();
  editors = [];
});

function makeEditor(markdown: string, remember = true): Editor {
  const ed = new Editor({
    element: document.createElement("div"),
    extensions: createMarkdownDocumentExtensions("conv_test1", "notes.md"),
    content: markdown,
    contentType: "markdown",
  });
  editors.push(ed);
  if (remember) rememberSourceBlocks(ed, markdown);
  return ed;
}

/** Save the editor's document, without the blank lines TipTap leaves at the end. */
function saved(ed: Editor): string {
  return markdownKeepingSource(ed).replace(/\n+$/, "");
}

/** Document position of the first occurrence of *needle* in a text node. */
function textPosition(ed: Editor, needle: string): number {
  let found = -1;
  ed.state.doc.descendants((node, pos) => {
    if (found === -1 && node.isText && node.text?.includes(needle)) {
      found = pos + node.text.indexOf(needle);
    }
  });
  return found;
}

/** Document range of the top-level block containing *needle*. */
function blockRange(ed: Editor, needle: string): { from: number; to: number } {
  let range = { from: -1, to: -1 };
  ed.state.doc.forEach((block, offset) => {
    if (range.from === -1 && block.textContent.includes(needle))
      range = { from: offset, to: offset + block.nodeSize };
  });
  return range;
}

/** Types of the top-level blocks, without the empty paragraph TipTap appends. */
function blockTypes(ed: Editor): string[] {
  const types: string[] = [];
  ed.state.doc.forEach((block) => {
    if (block.type.name !== "paragraph" || block.content.size) types.push(block.type.name);
  });
  return types;
}

function blockTypeOf(ed: Editor, needle: string): string {
  return ed.state.doc.nodeAt(blockRange(ed, needle).from)!.type.name;
}

// Each block uses syntax the serialiser writes differently.
const MIXED = [
  "* star list\n* two",
  "Title\n=====",
  "Edit me here.",
  "| a | b |\n|---|---|\n| long cell | x |",
  "Visit https://example.com and __bold__ text.",
  "    indented code",
  "~~~\ntilde fence\n~~~",
  "***",
  "1) paren item",
].join("\n\n");

describe("saving without edits", () => {
  it.each([
    ["a bare URL", "Visit https://example.com today."],
    ["a setext heading", "Head\n====\n\ntext"],
    ["indented code", "Para\n\n    indented code\n\nafter"],
    ["an unpadded table", "| a | b |\n|---|---|\n| long cell | x |"],
    ["`*` and `+` bullets", "* star\n* two\n\n+ plus"],
    ["underscore emphasis", "__bold__ and _it_"],
    ["a `)` ordered list", "1) paren list\n2) two"],
    ["a `***` rule", "***\n\ntext"],
    ["an autolink", "<https://example.com>"],
    ["a mixed task list", "- [ ] task\n- [x] done\n- plain"],
    ["an escape the serialiser drops", "A\\_escaped"],
    ["a tilde fence", "~~~\ntilde\n~~~"],
    ["an entity", "Text with &amp; entity"],
    ["a closed ATX heading", "# Heading #"],
    ["a standalone image", "![img](a.png)\n\ntext"],
    ["extra blank lines", "one\n\n\n\ntwo"],
    ["a lazy quote line", "> quote\nlazy\n\npara"],
    ["an HTML block", "<div>\nhtml\n</div>\n\ntext"],
    ["a heading without a blank line after it", "# H\ntext right after"],
    ["footnotes and reference links", "Note[^1] and [x][r].\n\n[^1]: def\n[r]: /r"],
    ["every block of a mixed document", MIXED],
  ])("keeps %s byte-for-byte", (_name, markdown) => {
    expect(saved(makeEditor(markdown))).toBe(markdown);
  });

  it("serialises a block the editor changed while loading", () => {
    const markdown = "* star\n\nTitle\n=====";
    const ed = makeEditor(markdown, false);
    ed.commands.insertContentAt(textPosition(ed, "star"), "a ");
    rememberSourceBlocks(ed, markdown);
    expect(saved(ed)).toBe("- a star\n\nTitle\n=====");
  });

  it("writes CRLF source with LF line endings", () => {
    expect(saved(makeEditor("a\r\nb\r\n\r\nc"))).toBe("a\nb\n\nc");
  });

  it("serialises everything when the source was not recorded", () => {
    const ed = makeEditor("* star", false);
    expect(saved(ed)).toBe("- star");
  });

  it("serialises everything for a parse of different Markdown", () => {
    const ed = makeEditor("* star", false);
    rememberSourceBlocks(ed, "* other");
    expect(saved(ed)).toBe("- star");
  });

  it("forgets the previous source when a replacement was not recorded", () => {
    const ed = makeEditor("* star");
    rememberSourceBlocks(ed, "* never parsed");
    expect(saved(ed)).toBe("- star");
  });
});

describe("saving after an edit", () => {
  it("serialises only the edited block", () => {
    const ed = makeEditor(MIXED);
    ed.commands.insertContentAt(textPosition(ed, "here"), "right ");
    expect(saved(ed)).toBe(MIXED.replace("Edit me here.", "Edit me right here."));
  });

  it("rewrites the syntax of the edited block only", () => {
    const ed = makeEditor(MIXED);
    ed.commands.insertContentAt(textPosition(ed, "two"), "item ");
    expect(saved(ed)).toBe(MIXED.replace("* star list\n* two", "- star list\n- item two"));
  });

  it("keeps the source after an edit is undone", () => {
    const ed = makeEditor(MIXED);
    ed.commands.insertContentAt(textPosition(ed, "here"), "right ");
    ed.commands.undo();
    expect(saved(ed)).toBe(MIXED);
  });

  it("keeps the source of a block whose edit was typed back", () => {
    const ed = makeEditor(MIXED);
    const at = textPosition(ed, "two");
    ed.commands.deleteRange({ from: at, to: at + 1 });
    ed.commands.insertContentAt(at, "t");
    expect(saved(ed)).toBe(MIXED);
  });

  it("keeps the blocks around a deleted block", () => {
    const ed = makeEditor(MIXED);
    ed.commands.deleteRange(blockRange(ed, "Edit me"));
    expect(saved(ed)).toBe(MIXED.replace("Edit me here.\n\n", ""));
  });

  it("keeps the blocks around an inserted block", () => {
    const ed = makeEditor(MIXED);
    ed.commands.insertContentAt(blockRange(ed, "Edit me").to, {
      type: "paragraph",
      content: [{ type: "text", text: "New block." }],
    });
    expect(saved(ed)).toBe(MIXED.replace("Edit me here.", "Edit me here.\n\nNew block."));
  });

  it("keeps the source of a block moved elsewhere", () => {
    const ed = makeEditor("* star\n\nTitle\n=====\n\nlast");
    const { from, to } = blockRange(ed, "star");
    const list = ed.state.doc.nodeAt(from)!;
    ed.commands.command(({ tr }) => {
      tr.delete(from, to);
      tr.insert(tr.doc.content.size, list);
      return true;
    });
    expect(saved(ed)).toBe("Title\n=====\n\nlast\n\n* star");
  });

  it("serialises a split task list as a whole when one part changes", () => {
    const markdown = "- [ ] task\n- plain\n\n* other";
    const ed = makeEditor(markdown);
    ed.commands.insertContentAt(textPosition(ed, "plain"), "very ");
    expect(saved(ed)).toBe("- [ ] task\n\n- very plain\n\n* other");
  });

  // Adjacent standalone images load as one paragraph spanning both tokens.
  it("keeps standalone images when the text after them changes", () => {
    const ed = makeEditor("![a](a.png)\n\n![b](b.png)\n\n* text");
    ed.commands.insertContentAt(textPosition(ed, "text"), "new ");
    expect(saved(ed)).toBe("![a](a.png)\n\n![b](b.png)\n\n- new text");
  });

  it("serialises references when the definitions change", () => {
    const markdown = "See [x][r] and https://example.com\n\n[r]: /r";
    const ed = makeEditor(markdown);
    let at = -1;
    ed.state.doc.forEach((block, offset) => {
      if (block.type.name === "linkDefinitions") at = offset;
    });
    ed.commands.deleteRange({ from: at, to: at + ed.state.doc.nodeAt(at)!.nodeSize });
    expect(saved(ed)).toBe("See [x](/r) and [https://example.com](https://example.com)");
  });

  // Kept source can change meaning beside a block it was not next to.
  it.each([
    ["indented code that would join a list", "- item\n\nPara\n\n    code()", "Para"],
    ["a list that would take in indented code", "- item\n\n# Head\n\n    code()", "Head"],
  ])("keeps the block structure when deleting a block leaves %s", (_name, markdown, deleted) => {
    const ed = makeEditor(markdown);
    const before = blockTypes(ed).filter((type) => type !== blockTypeOf(ed, deleted));
    ed.commands.deleteRange(blockRange(ed, deleted));
    expect(blockTypes(makeEditor(saved(ed)))).toEqual(before);
  });

  it("keeps text typed after an unclosed fence out of the code block", () => {
    const ed = makeEditor("```js\nconst a = 1");
    ed.commands.insertContentAt(ed.state.doc.content.size, {
      type: "paragraph",
      content: [{ type: "text", text: "more" }],
    });
    const reloaded = makeEditor(saved(ed));
    expect(blockTypes(reloaded)).toEqual(["codeBlock", "paragraph"]);
    expect(reloaded.state.doc.child(1).textContent).toBe("more");
  });

  it("keeps source beside an edit whose neighbours read back the same", () => {
    const markdown = "* one\n\nPara\n\nTitle\n=====";
    const ed = makeEditor(markdown);
    ed.commands.insertContentAt(textPosition(ed, "Para"), "New ");
    expect(saved(ed)).toBe("* one\n\nNew Para\n\nTitle\n=====");
  });

  it("keeps source loaded by an external update", () => {
    const ed = makeEditor("first");
    applyExternalMarkdown(ed, "* star\n\nTitle\n=====");
    rememberSourceBlocks(ed, "* star\n\nTitle\n=====");
    ed.commands.insertContentAt(textPosition(ed, "star"), "a ");
    expect(saved(ed)).toBe("- a star\n\nTitle\n=====");
  });
});
