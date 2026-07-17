/**
 * Regression test: opening a markdown file that contains a blockquote whose
 * content is a lone inline image (`> ![x](img)`) or an empty blockquote (`>`)
 * used to crash the whole editor panel.
 *
 * `@tiptap/markdown` (beta) parses those into a blockquote holding an inline
 * `image` (or nothing), which violates the blockquote's `block+` content
 * model. ProseMirror builds the initial document with `nodeFromJSON`, which
 * does NOT validate content, so the invalid doc loads silently — then the
 * first edit transaction that touches the blockquote calls `contentMatchAt`
 * on it and throws ("Called contentMatchAt on a node with invalid content").
 * The viewer's React panel boundary caught the throw and rendered a crash
 * instead of the file.
 *
 * These tests use the EXACT extension stack from MarkdownRichTextViewer so a
 * regression re-introducing invalid blockquote content fails here. Only the
 * image extension's HTTP boundary (fetchFileContent) is mocked.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { Table, TableRow, TableCell, TableHeader } from "@tiptap/extension-table";
import { TaskItem, TaskList } from "@tiptap/extension-list";
import { Markdown } from "@tiptap/markdown";
import { createWorkspaceImageExtension, ImageAwareLink } from "./TipTapWorkspaceImage";
import { GitHubAlertBlockquote } from "./TipTapGitHubAlert";
import { HtmlPassthrough } from "./TipTapHtmlPassthrough";

vi.mock("@/hooks/useFileContent", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useFileContent")>();
  return { ...actual, fetchFileContent: vi.fn().mockResolvedValue(undefined) };
});

// jsdom leaves these undefined; the image node view needs both.
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
  vi.clearAllMocks();
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
});

/** Editor with the viewer's full extension stack. */
function makeEditor(markdown: string): Editor {
  return new Editor({
    element: document.createElement("div"),
    extensions: [
      StarterKit.configure({ link: false, blockquote: false }),
      TaskList,
      TaskItem.configure({ nested: true }),
      Table.configure({ resizable: true }),
      TableRow,
      TableCell,
      TableHeader,
      ImageAwareLink.configure({ openOnClick: false, autolink: false }),
      GitHubAlertBlockquote,
      HtmlPassthrough,
      Markdown,
      createWorkspaceImageExtension("conv_test", "README.md"),
    ],
    content: markdown,
    contentType: "markdown",
  });
}

/**
 * Simulate the user clicking into the blockquote and typing — the edit
 * transaction that tripped the crash. `insertText` at position 2 lands inside
 * the (previously invalid) blockquote and runs the fit that called
 * contentMatchAt.
 */
function typeInsideFirstNode(ed: Editor): void {
  ed.view.dispatch(ed.state.tr.insertText("a", 2));
}

describe("blockquote crash", () => {
  // Inputs that previously crashed the editor: a quote whose only content is an
  // inline image, an empty quote, and an image-only quote followed by a block.
  const CRASHERS = ["> ![diagram](diagram.png)", ">", "> ![a](1.png)\n\ntext after"];

  it.each(CRASHERS)("parses %j into a schema-valid document", (md) => {
    editor = makeEditor(md);
    // Node.check() recurses the whole tree and throws on invalid content;
    // before the fix this threw for the blockquote.
    expect(() => editor!.state.doc.check()).not.toThrow();
  });

  it.each(CRASHERS)("survives an edit transaction without crashing: %j", (md) => {
    editor = makeEditor(md);
    expect(() => typeInsideFirstNode(editor!)).not.toThrow();
  });

  it("wraps a lone blockquote image in a paragraph (valid block+ content)", () => {
    editor = makeEditor("> ![diagram](diagram.png)");
    const quote = editor.state.doc.child(0);
    expect(quote.type.name).toBe("blockquote");
    expect(quote.child(0).type.name).toBe("paragraph");
    expect(quote.child(0).child(0).type.name).toBe("image");
    expect(quote.child(0).child(0).attrs.src).toBe("diagram.png");
  });

  it("keeps a lone-image blockquote byte-faithful on round-trip", () => {
    const md = "> ![diagram](diagram.png)";
    editor = makeEditor(md);
    expect(editor.getMarkdown().trim()).toBe(md);
  });
});
