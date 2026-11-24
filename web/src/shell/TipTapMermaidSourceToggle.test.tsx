/**
 * A mermaid block in the real rich-text editor opens on its diagram and reveals
 * its source when the caret moves in or find lands on it. The mocked node-view
 * test can't reach these: they depend on TipTap's selection tracking and on the
 * node view's update hook re-rendering for decoration-only changes.
 */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Editor, EditorContent } from "@tiptap/react";
import { StarterKit } from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import { CodeBlockWithLanguage } from "./TipTapCodeBlockExtension";
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

async function mount(searchState: { current: SearchDecorationState | null }) {
  editor = new Editor({
    extensions: [
      StarterKit.configure({ codeBlock: false }),
      CodeBlockWithLanguage,
      createSearchDecorationExtension(searchState),
      Markdown,
    ],
    content: MARKDOWN,
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

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Diagram" }));
  });
  expect(mermaidSource()).not.toBeVisible();
  expect(instance.state.selection.$from.parent.textContent).toBe("Outro");
});

it("reveals the source while the current find match is inside it", async () => {
  const searchState = { current: null as SearchDecorationState | null };
  const instance = await mount(searchState);
  const rebuild = () =>
    act(async () => {
      instance.view.dispatch(instance.state.tr.setMeta(searchDecorationKey, "rebuild"));
    });

  searchState.current = { query: "beta", currentIndex: 0 };
  await rebuild();
  expect(mermaidSource()).toBeVisible();

  searchState.current = { query: "outro", currentIndex: 0 };
  await rebuild();
  expect(mermaidSource()).not.toBeVisible();
});
