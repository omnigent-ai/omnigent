/**
 * Rendering + interaction logic of the code-block node view: a non-empty
 * `mermaid` block shows its diagram and toggles to its source, the language
 * <select> dispatches a flagged transaction (so a blurred picker change still
 * persists), an unknown fence language stays selectable, and the picker is
 * disabled when the editor is read-only.
 *
 * @tiptap/react's NodeViewWrapper/NodeViewContent need the editor's node-view
 * context to render, and MermaidPreview pulls in Streamdown; both are mocked so
 * this stays a fast unit test of our own branching.
 */

import { act, render, screen, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNodeViewProps } from "@tiptap/react";
import { DecorationSet } from "@tiptap/pm/view";
import { TipTapCodeBlockView } from "./TipTapCodeBlockView";
import { CODE_BLOCK_LANGUAGE_EDIT_META } from "./codeBlockLanguageEdit";
import { CODE_BLOCK_LANGUAGES } from "./codeBlockLanguages";

vi.mock("@tiptap/react", () => ({
  NodeViewWrapper: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  NodeViewContent: () => <pre data-testid="node-content" />,
}));

// The real preview reports its first draw asynchronously; tests trigger it.
const drawLatestDiagram = vi.hoisted(() => ({ current: undefined as (() => void) | undefined }));
vi.mock("./MermaidPreview", () => ({
  MermaidPreview: ({ source, onDrawn }: { source: string; onDrawn?: () => void }) => {
    drawLatestDiagram.current = onDrawn;
    return <div data-testid="mermaid-preview">{source}</div>;
  },
}));

afterEach(() => vi.clearAllMocks());

function viewProps(textContent: string, selectionInside = false) {
  return {
    node: { attrs: { language: "mermaid" }, textContent },
    getPos: () => 0,
    editor: { isEditable: true, view: { state: {}, dispatch: vi.fn() } },
    innerDecorations: DecorationSet.empty,
    selectionInside,
  } as unknown as ReactNodeViewProps;
}

function renderView(
  overrides: {
    language?: string | null;
    textContent?: string;
    editable?: boolean;
    selectionInside?: boolean;
  } = {},
) {
  // A chainable transaction stub that records the setNodeAttribute/setMeta calls.
  const tr = { setNodeAttribute: vi.fn(() => tr), setMeta: vi.fn(() => tr) };
  const dispatch = vi.fn();
  const props = {
    node: {
      attrs: { language: overrides.language ?? null },
      textContent: overrides.textContent ?? "",
    },
    getPos: () => 0,
    editor: {
      isEditable: overrides.editable ?? true,
      view: { state: { tr }, dispatch },
    },
    innerDecorations: DecorationSet.empty,
    selectionInside: overrides.selectionInside ?? false,
  } as unknown as ReactNodeViewProps;
  render(<TipTapCodeBlockView {...props} />);
  return { tr, dispatch };
}

const viewButton = (name: "Diagram" | "Source") => screen.getByRole("button", { name });

describe("TipTapCodeBlockView", () => {
  it("renders the mermaid preview for a non-empty mermaid block", () => {
    renderView({ language: "mermaid", textContent: "graph TD\nA-->B" });
    expect(screen.getByTestId("mermaid-preview").textContent).toBe("graph TD\nA-->B");
  });

  it("renders the preview for a case-insensitive mermaid fence", () => {
    renderView({ language: "Mermaid", textContent: "graph TD" });
    expect(screen.getByTestId("mermaid-preview").textContent).toBe("graph TD");
  });

  it("does not render a preview for a non-mermaid language", () => {
    renderView({ language: "python", textContent: "print('hi')" });
    expect(screen.queryByTestId("mermaid-preview")).toBeNull();
  });

  it("does not render a preview for an empty mermaid block", () => {
    renderView({ language: "mermaid", textContent: "   " });
    expect(screen.queryByTestId("mermaid-preview")).toBeNull();
  });

  it("opens a mermaid block on its diagram with the source hidden", () => {
    renderView({ language: "mermaid", textContent: "graph TD" });
    expect(screen.getByTestId("node-content")).not.toBeVisible();
    expect(viewButton("Diagram")).toHaveAttribute("aria-pressed", "true");
    expect(viewButton("Source")).toHaveAttribute("aria-pressed", "false");
  });

  it("switches a mermaid block between its diagram and its source", () => {
    renderView({ language: "mermaid", textContent: "graph TD" });
    fireEvent.click(viewButton("Source"));
    expect(screen.getByTestId("node-content")).toBeVisible();
    // The diagram stays below as a live preview of the source being edited.
    expect(screen.getByTestId("mermaid-preview")).toBeInTheDocument();
    expect(viewButton("Source")).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(viewButton("Diagram"));
    expect(screen.getByTestId("node-content")).not.toBeVisible();
    expect(viewButton("Diagram")).toHaveAttribute("aria-pressed", "true");
  });

  it("lets a read-only editor switch to the source", () => {
    renderView({ language: "mermaid", textContent: "graph TD", editable: false });
    fireEvent.click(viewButton("Source"));
    expect(screen.getByTestId("node-content")).toBeVisible();
  });

  it("shows an empty mermaid block's source without a view toggle", () => {
    renderView({ language: "mermaid", textContent: "" });
    expect(screen.getByTestId("node-content")).toBeVisible();
    expect(screen.queryByRole("group", { name: "Mermaid view" })).toBeNull();
  });

  it("keeps new source visible until its first diagram draws", () => {
    vi.useFakeTimers();
    try {
      // Text arriving without the caret (undo, a remote edit) must not hide the
      // source while the preview debounces or Mermaid is still drawing.
      const { rerender } = render(<TipTapCodeBlockView {...viewProps("")} />);
      rerender(<TipTapCodeBlockView {...viewProps("graph TD")} />);
      expect(screen.getByTestId("node-content")).toBeVisible();
      act(() => vi.advanceTimersByTime(300));
      expect(screen.getByTestId("mermaid-preview").textContent).toBe("graph TD");
      expect(screen.getByTestId("node-content")).toBeVisible();

      act(() => drawLatestDiagram.current?.());
      expect(screen.getByTestId("node-content")).not.toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });

  it("opens an existing diagram without waiting for it to draw", () => {
    // A file that loads with diagram source hides it at once, so nothing jumps.
    render(<TipTapCodeBlockView {...viewProps("graph TD")} />);
    expect(screen.getByTestId("node-content")).not.toBeVisible();
  });

  it("shows the source while the caret is inside the block", () => {
    renderView({ language: "mermaid", textContent: "graph TD", selectionInside: true });
    expect(screen.getByTestId("node-content")).toBeVisible();
    expect(viewButton("Source")).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps a non-mermaid block's source visible without a view toggle", () => {
    renderView({ language: "python", textContent: "print('hi')" });
    expect(screen.getByTestId("node-content")).toBeVisible();
    expect(screen.queryByRole("group", { name: "Mermaid view" })).toBeNull();
  });

  it("dispatches a language change flagged as a user edit", () => {
    // The flag is what lets the change persist even though the picker blurs the
    // editor before the update fires.
    const { tr, dispatch } = renderView({ language: "python" });
    fireEvent.focus(screen.getByRole("combobox"));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "mermaid" } });
    expect(tr.setNodeAttribute).toHaveBeenCalledWith(0, "language", "mermaid");
    expect(tr.setMeta).toHaveBeenCalledWith(CODE_BLOCK_LANGUAGE_EDIT_META, true);
    expect(dispatch).toHaveBeenCalledWith(tr);
  });

  it("maps the empty selection back to a null language", () => {
    const { tr } = renderView({ language: "python" });
    fireEvent.focus(screen.getByRole("combobox"));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "" } });
    expect(tr.setNodeAttribute).toHaveBeenCalledWith(0, "language", null);
  });

  it("keeps a language not in the quick-pick list selectable", () => {
    renderView({ language: "haskell", textContent: "main = pure ()" });
    expect(screen.getByRole("option", { name: "haskell" }).getAttribute("value")).toBe("haskell");
  });

  it("maps a cased fence to the canonical option without duplicating it", () => {
    renderView({ language: "Mermaid", textContent: "graph TD" });
    const mermaidOptions = screen.getAllByRole("option").filter((o) => o.textContent === "Mermaid");
    expect(mermaidOptions).toHaveLength(1);
    expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("mermaid");
  });

  it("disables the language picker when the editor is read-only", () => {
    renderView({ language: "mermaid", textContent: "graph TD", editable: false });
    expect(screen.getByRole("combobox")).toBeDisabled();
  });

  it.each(["pointerEnter", "pointerDown", "focus"] as const)(
    "populates the native menu on %s without changing the document",
    (event) => {
      const { dispatch } = renderView({ language: "python" });
      const picker = screen.getByRole("combobox");
      expect(screen.getAllByRole("option")).toHaveLength(1);
      expect(picker).toHaveValue("python");
      fireEvent[event](picker);
      expect(screen.getAllByRole("option")).toHaveLength(CODE_BLOCK_LANGUAGES.length);
      expect(picker).toHaveValue("python");
      expect(dispatch).not.toHaveBeenCalled();
    },
  );
});
