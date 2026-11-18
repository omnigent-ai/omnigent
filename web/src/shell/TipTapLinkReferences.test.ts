import { afterEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { createMarkdownDocumentExtensions } from "./markdownDocumentExtensions";
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

function roundTrip(markdown: string): string {
  return makeEditor(markdown).getMarkdown();
}

/** Save after an edit, without the empty paragraph an edit appends at the end. */
function saved(ed: Editor): string {
  return ed.getMarkdown().replace(/\n+$/, "");
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

/** Hrefs of the links in the editor, in document order. */
function linkHrefs(ed: Editor): string[] {
  return Array.from(ed.view.dom.querySelectorAll("a"), (link) => link.getAttribute("href") ?? "");
}

const DOCS = "See [the docs][docs] now.\n\nOther text.\n\n[docs]: https://example.com/docs";

describe("reference link round-trips", () => {
  it.each([
    ["a full reference", DOCS],
    ["a collapsed reference", "See [docs][] now.\n\n[docs]: https://example.com/docs"],
    ["a shortcut reference", "See [docs] now.\n\n[docs]: https://example.com/docs"],
    ["a label matched case- and space-insensitively", "A [link][Docs  Page].\n\n[docs page]: /d"],
    ["a definition with a title", 'A [link][d].\n\n[d]: https://example.com "Title"'],
    ["an angle-bracket URL", "A [link][d].\n\n[d]: <https://example.com/a b>"],
    ["definitions before the links", "[a]: /a\n[b]: /b\n\nUse [x][a] and [y][b]."],
    ["blank lines between definitions", "Use [x][a] and [y][b].\n\n[a]: /a\n\n[b]: /b"],
    ["a definition title on the next line", "A [link][d].\n\n[d]: /d\n  'Title'"],
    ["footnotes and links together", "Note[^1] and [link][r].\n\n[^1]: note\n[r]: /r"],
    ["a definition in a list item", "- item [a][r]\n\n[r]: /r"],
    ["a definition in a blockquote", "> quote [a][r]\n>\n> [r]: /r"],
    ["code in link text", "[the `code`][r]\n\n[r]: /r"],
    ["an undefined reference", "Undefined [x][nope] stays text."],
    ["a bracket that is not a label", "Defined [r] and [other].\n\n[r]: /r"],
    ["an inline link", "Inline [x](https://example.com/x) stays."],
  ])("keeps %s unchanged", (_name, markdown) => {
    expect(roundTrip(markdown)).toBe(markdown);
  });

  it("keeps the URL of a reference link", () => {
    expect(linkHrefs(makeEditor(DOCS))).toEqual(["https://example.com/docs"]);
  });

  it("shows the definitions read-only as their source", () => {
    const ed = makeEditor(DOCS);
    expect(ed.view.dom.querySelector("[data-link-definitions]")?.textContent).toBe(
      "[docs]: https://example.com/docs",
    );
  });

  // The serialiser separates blocks with a blank line and moves bold inside
  // links, as it does without references.
  it.each([
    ["a heading right after a definition", "[r]: /r\n# Head [x][r]", "[r]: /r\n\n# Head [x][r]"],
    ["bold around a reference link", "**[r]** text\n\n[r]: /r", "[**r**] text\n\n[r]: /r"],
  ])("normalises %s and stays stable", (_name, markdown, expected) => {
    const output = roundTrip(markdown);
    expect(output).toBe(expected);
    expect(roundTrip(output)).toBe(output);
  });
});

describe("editing a document with reference links", () => {
  it("keeps references and definitions when another paragraph is edited", () => {
    const ed = makeEditor(DOCS);
    ed.commands.insertContentAt(textPosition(ed, "text."), "more ");
    expect(saved(ed)).toBe(DOCS.replace("Other text.", "Other more text."));
  });

  it("keeps a full reference when its link text is edited", () => {
    const ed = makeEditor(DOCS);
    ed.commands.insertContentAt(textPosition(ed, "docs"), "new ");
    expect(saved(ed)).toBe(DOCS.replace("[the docs]", "[the new docs]"));
  });

  it("writes a link inline when its URL is edited", () => {
    const ed = makeEditor(DOCS);
    ed.commands.setTextSelection(textPosition(ed, "docs") + 1);
    ed.commands.extendMarkRange("link");
    ed.commands.setLink({ href: "https://example.com/new" });
    expect(saved(ed)).toBe(DOCS.replace("[the docs][docs]", "[the docs](https://example.com/new)"));
  });

  it("writes a shortcut link inline when its text no longer matches the label", () => {
    const ed = makeEditor("See [docs] now.\n\n[docs]: https://example.com/docs");
    ed.commands.insertContentAt(textPosition(ed, "docs") + 2, "X");
    expect(saved(ed)).toBe(
      "See [doXcs](https://example.com/docs) now.\n\n[docs]: https://example.com/docs",
    );
  });

  it("keeps a collapsed reference when text is typed after it", () => {
    const ed = makeEditor("See [docs][] now.\n\n[docs]: /d");
    ed.commands.insertContentAt(textPosition(ed, " now"), "!");
    expect(saved(ed)).toBe("See [docs][]! now.\n\n[docs]: /d");
  });

  it("writes links inline when their definitions are deleted", () => {
    const ed = makeEditor(DOCS);
    let at = -1;
    ed.state.doc.forEach((block, offset) => {
      if (block.type.name === "linkDefinitions") at = offset;
    });
    ed.commands.deleteRange({ from: at, to: at + ed.state.doc.nodeAt(at)!.nodeSize });
    expect(saved(ed)).toBe("See [the docs](https://example.com/docs) now.\n\nOther text.");
  });

  it("escapes typed text that would read back as a reference link", () => {
    const ed = makeEditor(DOCS);
    ed.commands.insertContentAt(textPosition(ed, "Other"), "[docs] and [plain] ");
    const output = saved(ed);
    expect(output).toContain("\\[docs] and [plain] Other");
    expect(linkHrefs(makeEditor(output))).toEqual(["https://example.com/docs"]);
  });

  it("ignores typing over a selected definitions block", () => {
    const ed = makeEditor(DOCS);
    let at = -1;
    ed.state.doc.forEach((block, offset) => {
      if (block.type.name === "linkDefinitions") at = offset;
    });
    ed.commands.setNodeSelection(at);
    const typed = ed.view.someProp("handleTextInput", (handle) =>
      handle(ed.view, at, at + 1, "z", () => ed.state.tr),
    );
    expect(typed).toBe(true);
  });

  it("keeps the definitions and the URL through copy and paste", () => {
    const copied = makeEditor(DOCS).getHTML();
    const ed = makeEditor("Start.");
    ed.commands.insertContentAt(ed.state.doc.content.size, copied);
    // A pasted link carries only its URL, so it is written inline.
    expect(saved(ed)).toBe(
      `Start.\n\n${DOCS.replace("[the docs][docs]", "[the docs](https://example.com/docs)")}`,
    );
  });

  it("does not paste text that is not a definition run as definitions", () => {
    const ed = makeEditor("Start.");
    ed.commands.insertContentAt(
      ed.state.doc.content.size,
      '<div data-link-definitions=""># not a definition</div>',
    );
    expect(ed.view.dom.querySelector("[data-link-definitions]")).toBeNull();
  });
});
