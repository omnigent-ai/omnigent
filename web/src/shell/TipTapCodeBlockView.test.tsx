/**
 * Rendering logic of the code-block node view: the mermaid preview shows only
 * for a non-empty `mermaid` block, the language <select> drives
 * updateAttributes, an unknown fence language stays selectable, and the picker
 * is disabled when the editor is read-only.
 *
 * @tiptap/react's NodeViewWrapper/NodeViewContent need the editor's node-view
 * context to render, and MermaidPreview pulls in Streamdown; both are mocked so
 * this stays a fast unit test of our own branching.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { NodeViewProps } from "@tiptap/react";
import { TipTapCodeBlockView } from "./TipTapCodeBlockView";

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
  const updateAttributes = vi.fn();
  const focus = vi.fn();
  const props = {
    node: {
      attrs: { language: overrides.language ?? null },
      textContent: overrides.textContent ?? "",
    },
    updateAttributes,
    editor: { isEditable: overrides.editable ?? true, commands: { focus } },
  } as unknown as NodeViewProps;
  render(<TipTapCodeBlockView {...props} />);
  return { updateAttributes, focus };
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

  it("updates the language attribute when the selector changes", () => {
    const { updateAttributes } = renderView({ language: "python" });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "mermaid" } });
    expect(updateAttributes).toHaveBeenCalledWith({ language: "mermaid" });
  });

  it("refocuses the editor before updating so the change autosaves", () => {
    // The native <select> blurs the editor; without a refocus the language
    // update fires while blurred and the autosave wiring drops it.
    const { updateAttributes, focus } = renderView({ language: "python" });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "mermaid" } });
    expect(focus).toHaveBeenCalled();
    expect(focus.mock.invocationCallOrder[0]).toBeLessThan(
      updateAttributes.mock.invocationCallOrder[0],
    );
  });

  it("maps the empty selection back to a null language", () => {
    const { updateAttributes } = renderView({ language: "python" });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "" } });
    expect(updateAttributes).toHaveBeenCalledWith({ language: null });
  });

  it("keeps a language not in the quick-pick list selectable", () => {
    renderView({ language: "haskell", textContent: "main = pure ()" });
    expect(screen.getByRole("option", { name: "haskell" }).getAttribute("value")).toBe("haskell");
  });

  it("disables the language picker when the editor is read-only", () => {
    renderView({ language: "mermaid", textContent: "graph TD", editable: false });
    expect(screen.getByRole("combobox")).toBeDisabled();
  });
});
