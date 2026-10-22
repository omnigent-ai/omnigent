/**
 * Soft line breaks in the markdown editor: a hard-wrapped paragraph must reflow
 * in Edit mode without rewriting the file. Parsing splits each in-paragraph "\n"
 * into a softBreak node, serialising joins it back, and the text APIs behind
 * comments and search still see the newline. Uses the EXACT extension stack
 * MarkdownRichTextViewer mounts.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { StarterKit } from "@tiptap/starter-kit";
import { Table, TableRow, TableCell, TableHeader } from "@tiptap/extension-table";
import { ListItem, TaskItem, TaskList } from "@tiptap/extension-list";
import { Markdown } from "@tiptap/markdown";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { createWorkspaceImageExtension, ImageAwareLink } from "./TipTapWorkspaceImage";
import { GitHubAlertBlockquote } from "./TipTapGitHubAlert";
import { HtmlPassthrough } from "./TipTapHtmlPassthrough";
import { CodeBlockWithLanguage } from "./TipTapCodeBlockExtension";
import { SOFT_BREAK_NODE, SoftBreak } from "./TipTapSoftBreak";
import { findMatches } from "./TipTapSearchExtension";
import { computeSelectionData, findPmRangeForComment } from "./TipTapEditorHelpers";
import {
  installMarkdownParserPatch,
  installMarkdownSerializerPatch,
} from "./tiptapMarkdownPatches";
import type { Comment } from "@/hooks/useComments";

installMarkdownSerializerPatch();
installMarkdownParserPatch();

const SafeListItem = ListItem.extend({ content: "block+" });

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

function viewerExtensions(withSoftBreak: boolean) {
  return [
    StarterKit.configure({ link: false, blockquote: false, listItem: false, codeBlock: false }),
    SafeListItem,
    CodeBlockWithLanguage,
    TaskList,
    TaskItem.configure({ nested: true }),
    Table.configure({ resizable: true }),
    TableRow,
    TableCell,
    TableHeader,
    ImageAwareLink.configure({ openOnClick: false, autolink: false }),
    GitHubAlertBlockquote,
    HtmlPassthrough,
    ...(withSoftBreak ? [SoftBreak] : []),
    Markdown,
    createWorkspaceImageExtension("conv_test", "README.md"),
  ];
}

function makeEditor(markdown: string, withSoftBreak = true): Editor {
  return new Editor({
    element: document.createElement("div"),
    extensions: viewerExtensions(withSoftBreak),
    content: markdown,
    contentType: "markdown",
  });
}

/** Parse + serialise with a throwaway editor, normalising only the trailing newline. */
function roundTrip(markdown: string, withSoftBreak: boolean): string {
  const ed = makeEditor(markdown, withSoftBreak);
  try {
    return ed.getMarkdown().trim();
  } finally {
    ed.destroy();
  }
}

/** Absolute PM positions of every softBreak node in document order. */
function softBreakPositions(doc: ProseMirrorNode): number[] {
  const positions: number[] = [];
  doc.descendants((node, pos) => {
    if (node.type.name === SOFT_BREAK_NODE) positions.push(pos);
  });
  return positions;
}

// Prose hard-wrapped the way editors and agents commonly write it.
const WRAPPED_LINES = [
  "This design note describes how the workspace file viewer renders Markdown",
  "documents in its three modes. The rendered preview reflows every paragraph",
  "to the full width of the viewer.",
];
const WRAPPED = ["# Wrapped", "", ...WRAPPED_LINES, "", "A second paragraph on one line."].join(
  "\n",
);

describe("SoftBreak parsing", () => {
  it("splits a hard-wrapped paragraph's newlines into softBreak nodes", () => {
    editor = makeEditor(WRAPPED);
    const paragraph = editor.state.doc.child(1);
    expect(paragraph.type.name).toBe("paragraph");
    expect(softBreakPositions(paragraph)).toHaveLength(WRAPPED_LINES.length - 1);
    paragraph.descendants((node) => {
      if (node.isText) expect(node.text).not.toContain("\n");
    });
    // leafText keeps the newline visible to every textBetween() consumer.
    expect(paragraph.textContent).toBe(WRAPPED_LINES.join("\n"));
    expect(editor.state.doc.child(2).textContent).toBe("A second paragraph on one line.");
  });

  it("renders each soft break as a non-editable collapsible space", () => {
    editor = makeEditor(WRAPPED);
    const spans = editor.view.dom.querySelectorAll<HTMLElement>("span.md-soft-break");
    expect(spans).toHaveLength(WRAPPED_LINES.length - 1);
    for (const span of spans) {
      expect(span.textContent).toBe(" ");
      expect(span.contentEditable).toBe("false");
    }
  });

  it("keeps newlines inside fenced code blocks as text", () => {
    const md = "```python\nprint(1)\nprint(2)\n```";
    editor = makeEditor(md);
    expect(softBreakPositions(editor.state.doc)).toHaveLength(0);
    expect(editor.state.doc.child(0).textContent).toBe("print(1)\nprint(2)");
    expect(editor.getMarkdown().trim()).toBe(md);
  });

  it("keeps hard breaks as hardBreak nodes", () => {
    const md = "first line  \nsecond line";
    editor = makeEditor(md);
    expect(softBreakPositions(editor.state.doc)).toHaveLength(0);
    let hardBreaks = 0;
    editor.state.doc.descendants((node) => {
      if (node.type.name === "hardBreak") hardBreaks += 1;
    });
    expect(hardBreaks).toBe(1);
    expect(editor.getMarkdown().trim()).toBe(md);
  });

  it("leaves documents alone when the SoftBreak node is not in the schema", () => {
    editor = makeEditor(WRAPPED, false);
    const paragraph = editor.state.doc.child(1);
    expect(paragraph.childCount).toBe(1);
    expect(paragraph.textContent).toBe(WRAPPED_LINES.join("\n"));
  });
});

describe("SoftBreak serialisation", () => {
  it.each([
    ["plain prose", WRAPPED],
    ["a mark spanning the break", "Some **bold text\nthat wraps** here."],
    ["a link whose text wraps", "See [the design\nnote](https://example.test/doc) now."],
    ["a blockquote", "> quoted line one\n> quoted line two"],
  ])("round-trips %s byte-identical", (_label, md) => {
    editor = makeEditor(md);
    expect(softBreakPositions(editor.state.doc).length).toBeGreaterThan(0);
    expect(editor.getMarkdown().trim()).toBe(md);
  });

  // Upstream already normalises list continuation indentation; the soft break
  // must not add any churn of its own on top of that.
  it.each([
    ["a lazy bullet continuation", "- first item\n  continues here\n- second item"],
    ["an ordered item continuation", "1. first\n   second line"],
  ])("serialises %s exactly like the editor without soft breaks", (_label, md) => {
    editor = makeEditor(md);
    expect(softBreakPositions(editor.state.doc).length).toBeGreaterThan(0);
    expect(editor.getMarkdown().trim()).toBe(roundTrip(md, false));
  });

  it("keeps the surrounding soft breaks when the user edits a line", () => {
    editor = makeEditor(WRAPPED);
    const [firstBreak] = softBreakPositions(editor.state.doc);
    editor.commands.insertContentAt(firstBreak + 1, "NEW ");
    const lines = [...WRAPPED_LINES];
    lines[1] = `NEW ${lines[1]}`;
    expect(editor.getMarkdown().trim()).toBe(
      ["# Wrapped", "", ...lines, "", "A second paragraph on one line."].join("\n"),
    );
  });

  it("collapses soft breaks left adjacent by edits into one newline", () => {
    editor = makeEditor("alpha\nbeta\ngamma");
    const [first, second] = softBreakPositions(editor.state.doc);
    editor.commands.deleteRange({ from: first + 1, to: second });
    expect(softBreakPositions(editor.state.doc)).toHaveLength(2);
    expect(editor.getMarkdown().trim()).toBe("alpha\ngamma");
  });

  it("drops the marks of a soft break whose marked neighbours were deleted", () => {
    editor = makeEditor("x**a\nb**y");
    const [sb] = softBreakPositions(editor.state.doc);
    // Delete "b" (after the break) and then "a" (before it).
    editor.commands.deleteRange({ from: sb + 1, to: sb + 2 });
    editor.commands.deleteRange({ from: sb - 1, to: sb });
    expect(editor.getMarkdown().trim()).toBe("x\ny");
  });

  it("drops soft breaks left at the start or end of a paragraph by edits", () => {
    editor = makeEditor("alpha\nbeta\ngamma");
    const [first, second] = softBreakPositions(editor.state.doc);
    editor.commands.deleteRange({ from: second + 1, to: second + 6 }); // "gamma"
    editor.commands.deleteRange({ from: 1, to: first }); // "alpha"
    expect(editor.getMarkdown().trim()).toBe("beta");
  });

  it("joins the two source lines when the user deletes a soft break", () => {
    editor = makeEditor(WRAPPED);
    const [firstBreak] = softBreakPositions(editor.state.doc);
    editor.commands.deleteRange({ from: firstBreak, to: firstBreak + 1 });
    const lines = [WRAPPED_LINES[0] + WRAPPED_LINES[1], WRAPPED_LINES[2]];
    expect(editor.getMarkdown().trim()).toBe(
      ["# Wrapped", "", ...lines, "", "A second paragraph on one line."].join("\n"),
    );
  });
});

describe("SoftBreak code-block conversion", () => {
  it("toggleCodeBlock keeps the source newlines", () => {
    editor = makeEditor("alpha\nbeta");
    editor.commands.setTextSelection(2);
    expect(editor.commands.toggleCodeBlock()).toBe(true);
    const block = editor.state.doc.child(0);
    expect(block.type.name).toBe("codeBlock");
    expect(block.textContent).toBe("alpha\nbeta");
    expect(editor.getMarkdown().trim()).toBe("```\nalpha\nbeta\n```");
    // Toggling back keeps both lines: ProseMirror turns the code block's
    // newlines into hard breaks (its linebreakReplacement).
    expect(editor.commands.toggleCodeBlock()).toBe(true);
    const paragraph = editor.state.doc.child(0);
    expect(paragraph.type.name).toBe("paragraph");
    expect(paragraph.childCount).toBe(3);
    expect(paragraph.child(1).type.name).toBe("hardBreak");
    expect(editor.getMarkdown().trim()).toBe("alpha  \nbeta");
  });

  it("setCodeBlock keeps the source newlines", () => {
    editor = makeEditor("alpha\nbeta");
    editor.commands.setTextSelection(2);
    expect(editor.commands.setCodeBlock()).toBe(true);
    const block = editor.state.doc.child(0);
    expect(block.type.name).toBe("codeBlock");
    expect(block.textContent).toBe("alpha\nbeta");
    expect(editor.getMarkdown().trim()).toBe("```\nalpha\nbeta\n```");
  });

  it("the ``` input rule keeps the source newlines", () => {
    const ed = makeEditor("alpha\nbeta");
    editor = ed;
    ed.view.dispatch(ed.state.tr.insertText("```", 1));
    // Simulate typing the space that completes the ``` prefix.
    const handled = ed.view.someProp("handleTextInput", (handler) =>
      handler(ed.view, 4, 4, " ", () => ed.state.tr),
    );
    expect(handled).toBe(true);
    const block = ed.state.doc.child(0);
    expect(block.type.name).toBe("codeBlock");
    expect(block.textContent).toBe("alpha\nbeta");
  });

  it("still converts hard breaks to newlines", () => {
    editor = makeEditor("first line  \nsecond line");
    editor.commands.setTextSelection(2);
    expect(editor.commands.toggleCodeBlock()).toBe(true);
    expect(editor.state.doc.child(0).textContent).toBe("first line\nsecond line");
  });
});

describe("SoftBreak text consumers", () => {
  const ACROSS = "Markdown\ndocuments";

  it("exposes the newline through getText()", () => {
    editor = makeEditor(WRAPPED);
    expect(editor.getText()).toContain(ACROSS);
  });

  it("find-in-file matches a phrase typed with a space across the break", () => {
    editor = makeEditor(WRAPPED);
    const matches = findMatches(editor.state.doc, "markdown documents");
    expect(matches).toHaveLength(1);
    expect(editor.state.doc.textBetween(matches[0].from, matches[0].to, "\n")).toBe(ACROSS);
  });

  it("anchors a comment spanning the break to the raw file verbatim", () => {
    editor = makeEditor(WRAPPED);
    const { doc } = editor.state;
    const start_index = WRAPPED.indexOf(ACROSS);
    const comment = {
      id: "c1",
      start_index,
      end_index: start_index + ACROSS.length,
      anchor_content: ACROSS,
    } as unknown as Comment;
    const range = findPmRangeForComment(doc, comment, WRAPPED);
    expect(range).not.toBeNull();
    expect(doc.textBetween(range!.from, range!.to, "\n")).toBe(ACROSS);
    expect(computeSelectionData(range!.from, range!.to, doc, WRAPPED)).toEqual({
      start_index,
      end_index: start_index + ACROSS.length,
      anchor_content: ACROSS,
    });
  });
});
