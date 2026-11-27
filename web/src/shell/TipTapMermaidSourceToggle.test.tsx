/**
 * A mermaid block in the real rich-text editor opens on its diagram and reveals
 * its source when the caret moves in or find or the active comment lands on it.
 * The mocked node-view test can't reach these: they depend on TipTap's selection
 * tracking and on the node view's update hook re-rendering for decoration-only
 * changes.
 */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Editor, EditorContent } from "@tiptap/react";
import { StarterKit } from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import type { Comment } from "@/hooks/useComments";
import { CodeBlockWithLanguage } from "./TipTapCodeBlockExtension";
import {
  commentDecorationKey,
  createCommentDecorationExtension,
  type CommentDecorationState,
} from "./TipTapCommentExtension";
import {
  createSearchDecorationExtension,
  searchDecorationKey,
  type SearchDecorationState,
} from "./TipTapSearchExtension";

// Streamdown's mermaid renderer needs a real layout engine; the toggle only
// decides whether the source is shown.
vi.mock("./MermaidPreview", () => ({
  MermaidPreview: ({ source }: { source: string }) => (
    <div data-testid="mermaid-preview">{source}</div>
  ),
}));

let editor: Editor | undefined;
afterEach(() => {
  cleanup();
  editor?.destroy();
  editor = undefined;
});

const MARKDOWN = "Intro\n\n```mermaid\ngraph TD\nAlpha-->Beta\n```\n\nOutro";

async function mount(
  searchState: { current: SearchDecorationState | null },
  commentState: { current: CommentDecorationState | null } = { current: null },
  markdown = MARKDOWN,
) {
  editor = new Editor({
    extensions: [
      StarterKit.configure({ codeBlock: false }),
      CodeBlockWithLanguage,
      createSearchDecorationExtension(searchState),
      createCommentDecorationExtension(commentState),
      Markdown,
    ],
    content: markdown,
    contentType: "markdown",
  });
  await act(async () => {
    render(<EditorContent editor={editor!} />);
  });
  return editor;
}

/** Document position of the first character of the mermaid source. */
function mermaidSourceStart(instance: Editor): number {
  let start = -1;
  instance.state.doc.forEach((child, offset) => {
    if (child.type.name === "codeBlock") start = offset + 1;
  });
  return start;
}

// The editable <pre>: find highlights split its text, and the mocked diagram repeats it.
const mermaidSource = () => document.querySelector(".tiptap-code-block > pre")!;

it("opens on the diagram and reveals the source while the caret is inside", async () => {
  const instance = await mount({ current: null });
  expect(mermaidSource()).not.toBeVisible();
  expect(screen.getByTestId("mermaid-preview")).toBeInTheDocument();

  await act(async () => {
    instance.commands.setTextSelection(mermaidSourceStart(instance) + 2);
  });
  expect(mermaidSource()).toBeVisible();
});

it("moves the caret out of the source when switching back to the diagram", async () => {
  const instance = await mount({ current: null });
  await act(async () => {
    instance.commands.setTextSelection(mermaidSourceStart(instance) + 2);
  });
  const document = instance.getJSON();

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Source" }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Diagram" }));
  });
  expect(mermaidSource()).not.toBeVisible();
  expect(instance.state.selection.$from.parent.textContent).toBe("Outro");
  // The toggle is view state only.
  expect(instance.getJSON()).toEqual(document);
});

it("moves the caret out of a file that holds only the diagram", async () => {
  // StarterKit's TrailingNode keeps a paragraph after the block to land on.
  const instance = await mount({ current: null }, undefined, "```mermaid\ngraph TD\nA-->B\n```");
  await act(async () => {
    instance.commands.setTextSelection(mermaidSourceStart(instance) + 2);
  });
  expect(mermaidSource()).toBeVisible();

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Diagram" }));
  });
  expect(mermaidSource()).not.toBeVisible();
  expect(instance.state.selection.$from.parent.type.name).toBe("paragraph");
});

function rebuildSearch(instance: Editor) {
  return act(async () => {
    instance.view.dispatch(instance.state.tr.setMeta(searchDecorationKey, "rebuild"));
  });
}

it("reveals the source while the current find match is inside it", async () => {
  const searchState = { current: null as SearchDecorationState | null };
  const instance = await mount(searchState);

  searchState.current = { query: "beta", currentIndex: 0 };
  await rebuildSearch(instance);
  expect(mermaidSource()).toBeVisible();

  searchState.current = { query: "outro", currentIndex: 0 };
  await rebuildSearch(instance);
  expect(mermaidSource()).not.toBeVisible();
});

it("hides a find match on Diagram until find moves to another match", async () => {
  const searchState = { current: null as SearchDecorationState | null };
  const instance = await mount(searchState);
  searchState.current = { query: "beta", currentIndex: 0 };
  await rebuildSearch(instance);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Diagram" }));
  });
  expect(mermaidSource()).not.toBeVisible();
  // Re-running find on the same match keeps it dismissed.
  await rebuildSearch(instance);
  expect(mermaidSource()).not.toBeVisible();

  searchState.current = { query: "alpha", currentIndex: 0 };
  await rebuildSearch(instance);
  expect(mermaidSource()).toBeVisible();
});

it("reveals the source for the active comment inside it, beside other find highlights", async () => {
  const start = MARKDOWN.indexOf("Alpha");
  const comment: Comment = {
    id: "c1",
    conversation_id: "conv_1",
    path: "diagram.md",
    start_index: start,
    end_index: start + "Alpha".length,
    body: "rename this node",
    status: "draft",
    created_at: 0,
    updated_at: 0,
    anchor_content: "Alpha",
    created_by: null,
  };
  const commentState = {
    current: {
      comments: [comment],
      activeSelection: null,
      rawContent: MARKDOWN,
      pendingRange: null,
      onClickComment: vi.fn(),
    } as CommentDecorationState,
  };
  // A find highlight outside the block must not keep it open on its own.
  const instance = await mount({ current: { query: "outro", currentIndex: 0 } }, commentState);
  const rebuildComments = () =>
    act(async () => {
      instance.view.dispatch(instance.state.tr.setMeta(commentDecorationKey, "rebuild"));
    });
  expect(mermaidSource()).not.toBeVisible();

  commentState.current.activeSelection = {
    start_index: comment.start_index,
    end_index: comment.end_index,
    anchor_content: "Alpha",
  };
  await rebuildComments();
  expect(mermaidSource()).toBeVisible();

  commentState.current.activeSelection = null;
  await rebuildComments();
  expect(mermaidSource()).not.toBeVisible();
});
