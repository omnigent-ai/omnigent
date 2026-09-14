/**
 * The rich-text editor swaps StarterKit's built-in codeBlock for
 * CodeBlockWithLanguage (adds a React node view: language picker + mermaid
 * preview). The node view must not change the document model, so the fence
 * language still has to round-trip through markdown unchanged — a ```mermaid
 * block loaded from disk must serialise back to ```mermaid, not ``` .
 *
 * These tests use the EXACT extension stack from MarkdownRichTextViewer so a
 * regression (e.g. the swap dropping the base CodeBlock's markdown handlers)
 * fails here.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { StarterKit } from "@tiptap/starter-kit";
import { Table, TableRow, TableCell, TableHeader } from "@tiptap/extension-table";
import { ListItem, TaskItem, TaskList } from "@tiptap/extension-list";
import { Markdown } from "@tiptap/markdown";
import { createWorkspaceImageExtension, ImageAwareLink } from "./TipTapWorkspaceImage";
import { GitHubAlertBlockquote } from "./TipTapGitHubAlert";
import { HtmlPassthrough } from "./TipTapHtmlPassthrough";
import { CodeBlockWithLanguage } from "./TipTapCodeBlockExtension";
import { CODE_BLOCK_LANGUAGE_EDIT_META, isUserEditUpdate } from "./codeBlockLanguageEdit";
import { computeSelectionData, findPmRangeForComment } from "./TipTapEditorHelpers";
import type { Comment } from "@/hooks/useComments";

const SafeListItem = ListItem.extend({ content: "block+" });

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
      StarterKit.configure({
        link: false,
        blockquote: false,
        listItem: false,
        codeBlock: false,
      }),
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
      Markdown,
      createWorkspaceImageExtension("conv_test", "README.md"),
    ],
    content: markdown,
    contentType: "markdown",
  });
}

describe("CodeBlockWithLanguage", () => {
  it("parses a fenced block's language onto the codeBlock node", () => {
    editor = makeEditor("```mermaid\ngraph TD\nA-->B\n```");
    const block = editor.state.doc.child(0);
    expect(block.type.name).toBe("codeBlock");
    expect(block.attrs.language).toBe("mermaid");
  });

  it.each(["mermaid", "python", "rust", "typescript"])(
    "round-trips the ```%s fence language through markdown",
    (lang) => {
      editor = makeEditor(`\`\`\`${lang}\nsome code\n\`\`\``);
      expect(editor.getMarkdown()).toContain(`\`\`\`${lang}`);
    },
  );

  it("preserves a language not in the quick-pick list", () => {
    editor = makeEditor('```haskell\nmain = putStrLn "hi"\n```');
    expect(editor.state.doc.child(0).attrs.language).toBe("haskell");
    expect(editor.getMarkdown()).toContain("```haskell");
  });

  it("registers a node view for the code block (edit-mode rendering)", () => {
    editor = makeEditor("```mermaid\ngraph TD\nA-->B\n```");
    // The node view is what mounts the language picker + mermaid preview; if the
    // swap failed to attach it the editor would fall back to a plain <pre>.
    expect(editor.extensionManager.extensions.some((e) => e.name === "codeBlock")).toBe(true);
    const nodeViews = editor.view.props.nodeViews ?? {};
    expect(Object.keys(nodeViews)).toContain("codeBlock");
  });
});

// Commenting on a mermaid block targets the SELECTABLE SOURCE, not the SVG
// preview (which is contentEditable=false chrome outside the PM document). The
// source lives in the codeBlock node as ordinary PM text, so the existing
// text-offset comment anchoring must work over it — no geometric anchor scheme.
describe("commenting on mermaid source text", () => {
  const MD = "# Title\n\n```mermaid\ngraph TD\nA-->B\n```\n";

  it("produces valid anchor data for a selection inside the mermaid source", () => {
    editor = makeEditor(MD);
    const doc = editor.state.doc;
    const text = doc.textBetween(0, doc.content.size, "\n");
    const from = text.indexOf("graph TD");
    expect(from).toBeGreaterThanOrEqual(0);

    // Simulate selecting "graph TD" inside the code block.
    const data = computeSelectionData(
      // textOffset → PM pos: the plugin passes PM selection positions; emulate by
      // locating the text and letting computeSelectionData resolve raw offsets.
      resolvePmPos(editor, from),
      resolvePmPos(editor, from + "graph TD".length),
      doc,
      MD,
    );
    expect(data).not.toBeNull();
    expect(data!.anchor_content).toBe("graph TD");
    // The anchor text exists verbatim in the raw markdown, so offsets point at it.
    expect(MD.slice(data!.start_index, data!.end_index)).toBe("graph TD");
  });

  it("re-locates the saved comment's highlight range inside the code block", () => {
    editor = makeEditor(MD);
    const start_index = MD.indexOf("graph TD");
    const comment = {
      anchor_content: "graph TD",
      start_index,
      end_index: start_index + "graph TD".length,
    } as Comment;

    const range = findPmRangeForComment(editor.state.doc, comment, MD);
    expect(range).not.toBeNull();
    expect(editor.state.doc.textBetween(range!.from, range!.to, "\n")).toBe("graph TD");
  });
});

// A language change from the picker dispatches while the editor is blurred (the
// native <select> holds focus), so it must be recognized as a user edit — not a
// load-time re-baseline — or the change is silently dropped on reload.
describe("code-block language edit persistence", () => {
  it("treats a focused update as a user edit and a blurred plain update as not", () => {
    editor = makeEditor("plain text");
    expect(isUserEditUpdate(true, editor.state.tr)).toBe(true);
    expect(isUserEditUpdate(false, editor.state.tr)).toBe(false);
  });

  it("recognizes a blurred language-edit transaction as a user edit and persists it", () => {
    editor = makeEditor("```python\nprint(1)\n```");
    const tr = editor.state.tr
      .setNodeAttribute(0, "language", "mermaid")
      .setMeta(CODE_BLOCK_LANGUAGE_EDIT_META, true);

    // The editor is not focused (never mounted into the document), exactly as
    // when the picker dispatches — the flag is what saves the change.
    expect(editor.isFocused).toBe(false);
    expect(isUserEditUpdate(editor.isFocused, tr)).toBe(true);

    editor.view.dispatch(tr);
    expect(editor.state.doc.child(0).attrs.language).toBe("mermaid");
    expect(editor.getMarkdown()).toContain("```mermaid");
  });
});

/** Map a text-content offset (textBetween with "\n") to a PM position. */
function resolvePmPos(ed: Editor, textOffset: number): number {
  const doc = ed.state.doc;
  const max = doc.content.size;
  let lo = 0;
  let hi = max;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (doc.textBetween(0, mid, "\n").length < textOffset) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}
