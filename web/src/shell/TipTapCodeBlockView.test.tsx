/**
 * Rendering + interaction logic of the code-block node view: the mermaid
 * preview shows only for a non-empty `mermaid` block, the language <select>
 * dispatches a flagged transaction (so a blurred picker change still persists),
 * an unknown fence language stays selectable, and the picker is disabled when
 * the editor is read-only.
 *
 * @tiptap/react's NodeViewWrapper/NodeViewContent need the editor's node-view
 * context to render, and MermaidPreview pulls in Streamdown; both are mocked so
 * this stays a fast unit test of our own branching.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { NodeViewProps } from "@tiptap/react";
import { TipTapCodeBlockView } from "./TipTapCodeBlockView";
import { CODE_BLOCK_LANGUAGE_EDIT_META } from "./codeBlockLanguageEdit";

vi.mock("@tiptap/react", () => ({
  NodeViewWrapper: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  NodeViewContent: () => <pre data-testid="node-content" />,
}));

vi.mock("./MermaidPreview", () => ({
  MermaidPreview: ({ source }: { source: string }) => (
    <div data-testid="mermaid-preview">{source}</div>
  ),
}));

afterEach(() => vi.clearAllMocks());

function renderView(
  overrides: { language?: string | null; textContent?: string; editable?: boolean } = {},
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
  } as unknown as NodeViewProps;
  render(<TipTapCodeBlockView {...props} />);
  return { tr, dispatch };
}

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

  it("dispatches a language change flagged as a user edit", () => {
    // The flag is what lets the change persist even though the picker blurs the
    // editor before the update fires.
    const { tr, dispatch } = renderView({ language: "python" });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "mermaid" } });
    expect(tr.setNodeAttribute).toHaveBeenCalledWith(0, "language", "mermaid");
    expect(tr.setMeta).toHaveBeenCalledWith(CODE_BLOCK_LANGUAGE_EDIT_META, true);
    expect(dispatch).toHaveBeenCalledWith(tr);
  });

  it("maps the empty selection back to a null language", () => {
    const { tr } = renderView({ language: "python" });
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
});
