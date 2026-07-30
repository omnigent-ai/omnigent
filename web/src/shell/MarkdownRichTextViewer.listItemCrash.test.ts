/**
 * Regression test: opening a markdown file whose list contains a block-first
 * item — a nested list (`- - x`), a fenced code block, a blockquote, a heading,
 * or a table as the item's first child — used to crash the whole editor panel.
 *
 * `@tiptap/markdown` (beta) parses those into a `listItem` whose first child is
 * a non-paragraph block. The stock TipTap `listItem` content model is
 * `paragraph block*` (it must START with a paragraph), so the parsed document
 * is schema-invalid. ProseMirror builds the initial doc via `nodeFromJSON`,
 * which does NOT validate content, so the bad doc loads silently — then the
 * first transaction that touches the list item (a user edit, or StarterKit's
 * TrailingNode appendTransaction that runs on load) calls `contentMatchAt` on
 * it and throws ("Called contentMatchAt on a node with invalid content"). The
 * viewer's React panel boundary catches the throw and renders a crash instead
 * of the file. (Same failure family as the blockquote crash fixed in #2004,
 * but for list items — which agent-authored markdown hits constantly.)
 *
 * Fix: relax the list item's content model to `block+` (SafeListItem) so a
 * non-paragraph first child is schema-valid. These tests use the EXACT
 * extension stack from MarkdownRichTextViewer so a regression fails here.
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

// The fix under test — must match MarkdownRichTextViewer's SafeListItem.
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

/** Dispatch an edit inside the first node — the transaction that tripped the crash. */
function typeInsideFirstNode(ed: Editor): void {
  ed.view.dispatch(ed.state.tr.insertText("a", 2));
}

describe("list-item block-first crash", () => {
  // Inputs that previously crashed the editor: a list item whose first (or only)
  // child is a non-paragraph block.
  const CRASHERS = [
    "- - nested",
    "- ```\n  code\n  ```",
    "- > quote",
    "- # heading",
    "- | a | b |\n  |---|---|\n  | 1 | 2 |",
    "> - > ![x](y.png)",
  ];

  it.each(CRASHERS)("parses %j into a schema-valid document", (md) => {
    editor = makeEditor(md);
    // Node.check() recurses the whole tree and throws on invalid content;
    // before the fix this threw for the list item.
    expect(() => editor!.state.doc.check()).not.toThrow();
  });

  it.each(CRASHERS)("survives an edit transaction without crashing: %j", (md) => {
    editor = makeEditor(md);
    expect(() => typeInsideFirstNode(editor!)).not.toThrow();
  });

  it("keeps a nested list schema-valid with the list as the item's first child", () => {
    editor = makeEditor("- - nested");
    const outerItem = editor.state.doc.child(0).child(0); // bulletList > listItem
    expect(outerItem.type.name).toBe("listItem");
    expect(outerItem.child(0).type.name).toBe("bulletList");
  });
});
