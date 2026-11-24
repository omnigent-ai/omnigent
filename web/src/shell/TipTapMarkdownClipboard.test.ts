/**
 * Copying from the rich Markdown editor: the plain-text flavor is Markdown,
 * the HTML flavor stays ProseMirror's. Runs ProseMirror's own copy serializer
 * so the test also proves this serializer outranks TipTap's plain-text one.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { AllSelection, TextSelection } from "@tiptap/pm/state";
import { createMarkdownDocumentExtensions } from "./markdownDocumentExtensions";
import { rememberSourceBlocks } from "./markdownSourceBlocks";
import { MarkdownClipboard } from "./TipTapMarkdownClipboard";
import {
  installMarkdownParserPatch,
  installMarkdownSerializerPatch,
} from "./tiptapMarkdownPatches";

installMarkdownSerializerPatch();
installMarkdownParserPatch();

// CodeBlockWithLanguage's node view touches blob URLs for the mermaid preview.
const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;
beforeEach(() => {
  URL.createObjectURL = vi.fn(() => "blob:mock");
  URL.revokeObjectURL = vi.fn();
});

let editor: Editor | null = null;
afterEach(() => {
  editor?.destroy();
  editor = null;
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
});

function makeEditor(markdown: string): Editor {
  const ed = new Editor({
    element: document.createElement("div"),
    extensions: [...createMarkdownDocumentExtensions("conv_test1", "notes.md"), MarkdownClipboard],
    content: markdown,
    contentType: "markdown",
  });
  rememberSourceBlocks(ed, markdown);
  editor = ed;
  return ed;
}

/** Document position of *needle*'s first character, or just past it with *after*. */
function textPosition(ed: Editor, needle: string, after = false): number {
  let found = -1;
  ed.state.doc.descendants((node, pos) => {
    if (found === -1 && node.isText && node.text?.includes(needle)) {
      found = pos + node.text.indexOf(needle) + (after ? needle.length : 0);
    }
  });
  if (found === -1) throw new Error(`text not found: ${needle}`);
  return found;
}

/** Select from the start of *from* to the end of *to* and copy, as ⌘C does. */
function copy(ed: Editor, from: string, to: string = from): { text: string; html: string } {
  const selection = TextSelection.create(
    ed.state.doc,
    textPosition(ed, from),
    textPosition(ed, to, true),
  );
  return copySelection(ed, selection);
}

function copySelection(ed: Editor, selection: TextSelection | AllSelection) {
  ed.view.dispatch(ed.state.tr.setSelection(selection));
  const { dom, text } = ed.view.serializeForClipboard(ed.state.selection.content());
  return { text, html: dom.innerHTML };
}

const DOC = [
  "# Release plan",
  "",
  "Ship the **new viewer** after the [design review](https://example.com/review).",
  "",
  "- Draft the notes",
  "- Book the demo",
  "",
  "```python",
  "print('hello')",
  "```",
].join("\n");

describe("copying from the Markdown editor", () => {
  it("copies blocks as Markdown and keeps rich HTML", () => {
    const ed = makeEditor(DOC);
    const { text, html } = copy(ed, "Release plan", "Book the demo");
    expect(text).toBe(
      [
        "# Release plan",
        "",
        "Ship the **new viewer** after the [design review](https://example.com/review).",
        "",
        "- Draft the notes",
        "- Book the demo",
      ].join("\n"),
    );
    expect(html).toContain("<strong>new viewer</strong>");
    expect(html).toContain('href="https://example.com/review"');
  });

  it("copies part of a paragraph as inline Markdown", () => {
    const ed = makeEditor(DOC);
    expect(copy(ed, "new", "design").text).toBe(
      "**new viewer** after the [design](https://example.com/review)",
    );
  });

  it("drops block syntax for part of one block and keeps it for the whole block", () => {
    const ed = makeEditor(DOC);
    expect(copy(ed, "Release").text).toBe("Release");
    expect(copy(ed, "Release plan").text).toBe("# Release plan");
    expect(copy(ed, "Book").text).toBe("Book");
    expect(copy(ed, "Book the demo").text).toBe("- Book the demo");
  });

  it("copies code as written, fenced only when the whole block is selected", () => {
    const ed = makeEditor(DOC);
    expect(copy(ed, "print").text).toBe("print");
    expect(copy(ed, "print('hello')").text).toBe("```python\nprint('hello')\n```");
  });

  it("copies a whole table cell as its text, not a one-cell table", () => {
    const ed = makeEditor("| Option | Latency |\n| --- | --- |\n| **Fast** path | 5 ms |");
    expect(copy(ed, "Fast", " path").text).toBe("**Fast** path");
  });

  it("keeps a hard-wrapped line's newline", () => {
    const ed = makeEditor("First line ends here\nsecond line starts here.");
    expect(copy(ed, "here", "second").text).toBe("here\nsecond");
  });

  it("copies the whole document as its source", () => {
    // The serializer would rewrite `*` bullets and the setext heading.
    const source = "Title\n=====\n\n* one\n* two\n\nA __strong__ word.";
    const ed = makeEditor(source);
    expect(copySelection(ed, new AllSelection(ed.state.doc)).text).toBe(source);
  });
});
