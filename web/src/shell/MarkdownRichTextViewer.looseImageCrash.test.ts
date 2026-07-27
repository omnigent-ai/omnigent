/**
 * Regression test: opening a markdown file that contains a bare inline image
 * not wrapped in a paragraph used to crash the whole editor panel — the
 * "known residual" documented in #2320 (which fixed block-first list items
 * via SafeListItem = `block+` but explicitly did not cover loose inline
 * images, because an inline node can't sit directly in a `block+` container).
 *
 * The workspace image node is an inline atom (`inline: true` in
 * TipTapWorkspaceImage), and `@tiptap/markdown` (beta) can hand back a bare
 * image with no wrapping paragraph at the document level (an image as a
 * loose child after a thematic break or heading) and at the list-item level
 * (`1. ![x](y)`). Both the top-level `doc` and SafeListItem have a `block+`
 * content model, which cannot hold a bare inline node, so the parsed doc is
 * schema-invalid. ProseMirror builds the initial doc via `nodeFromJSON`
 * (which does NOT validate), so it loads silently — then the first
 * transaction (a user edit, or StarterKit's TrailingNode appendTransaction
 * on load) calls `contentMatchAt` and throws ("Called contentMatchAt on a
 * node with invalid content"). The viewer's panel boundary catches the throw
 * and the whole file view crashes ("Page failed to load"). Same failure
 * family as #2559 / #2004 / #2320.
 *
 * Fix: SafeInlineWrap generalizes #2004's `toBlockContent` (previously
 * blockquote-only) to every `block+` parent, wrapping loose inline runs in a
 * paragraph after parse. These tests use the EXACT extension stack from
 * MarkdownRichTextViewer so a regression fails here.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { Table, TableRow, TableCell, TableHeader } from "@tiptap/extension-table";
import { ListItem, TaskItem, TaskList } from "@tiptap/extension-list";
import { Markdown } from "@tiptap/markdown";
import { createWorkspaceImageExtension, ImageAwareLink } from "./TipTapWorkspaceImage";
import { GitHubAlertBlockquote } from "./TipTapGitHubAlert";
import { HtmlPassthrough } from "./TipTapHtmlPassthrough";
import { installMarkdownParserPatch } from "./tiptapMarkdownPatches";

// The fix under test — installed at module load, as MarkdownRichTextViewer does.
installMarkdownParserPatch();

// Must match MarkdownRichTextViewer's SafeListItem (the #2320 fix).
const SafeListItem = ListItem.extend({ content: "block+" });

vi.mock("@/hooks/useFileContent", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useFileContent")>();
  return { ...actual, fetchFileContent: vi.fn().mockResolvedValue(undefined) };
});

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

function makeEditor(markdown: string): Editor {
  return new Editor({
    element: document.createElement("div"),
    extensions: [
      StarterKit.configure({ link: false, blockquote: false, listItem: false }),
      SafeListItem,
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

/** Dispatch an edit at the document start — the transaction that trips the crash. */
function typeAtStart(ed: Editor): void {
  ed.view.dispatch(ed.state.tr.insertText("a", 1));
}

describe("loose inline image crash (residual of #2320)", () => {
  // Each input makes @tiptap/markdown emit a bare inline image (or other
  // loose inline run) directly under a block+ parent — the document or a
  // list item — with no wrapping paragraph.
  const CRASHERS = [
    "![x](y.png)", // document-level, only content
    "Intro paragraph.\n\n![x](y.png)\n\n## Next", // document-level, normal block flow
    "---\n\n![x](y.png)", // document-level, after thematic break
    "1. ![x](y.png)", // ordered-list item
    "- ![x](y.png)", // bullet-list item
    "- [![badge](b.png)](https://example.com)", // linked image in a list item
  ];

  it.each(CRASHERS)("parses %j into a schema-valid document", (md) => {
    editor = makeEditor(md);
    // Node.check() recurses the whole tree and throws on invalid content;
    // before the fix this threw for the bare inline image.
    expect(() => editor!.state.doc.check()).not.toThrow();
  });

  it.each(CRASHERS)("survives an edit transaction without crashing: %j", (md) => {
    editor = makeEditor(md);
    expect(() => typeAtStart(editor!)).not.toThrow();
  });

  it("wraps a document-level lone image in a paragraph", () => {
    editor = makeEditor("![x](y.png)");
    const first = editor.state.doc.child(0);
    expect(first.type.name).toBe("paragraph");
    expect(first.child(0).type.name).toBe("image");
  });

  it("wraps a list-item lone image in a paragraph", () => {
    editor = makeEditor("- ![x](y.png)");
    const item = editor.state.doc.child(0).child(0); // bulletList > listItem
    expect(item.type.name).toBe("listItem");
    expect(item.child(0).type.name).toBe("paragraph");
    expect(item.child(0).child(0).type.name).toBe("image");
  });

  it("round-trips a document-level lone image back to markdown", () => {
    editor = makeEditor("Intro.\n\n![x](y.png)\n\n## Next");
    const md = editor.getMarkdown();
    expect(md).toContain("![x](y.png)");
    expect(md).toContain("## Next");
  });
});
