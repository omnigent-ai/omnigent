/**
 * Rendering + interaction logic of the code-block node view: the mermaid
 * preview shows only for a non-empty `mermaid` block, the language <select>
 * dispatches a flagged transaction (so a blurred picker change still persists),
 * an unknown fence language stays selectable, and the picker is disabled when
 * the editor is read-only.
 *
 * @tiptap/react's NodeViewWrapper and node-view context need a live editor, and
 * MermaidPreview pulls in Streamdown; both are mocked so this stays a fast unit
 * test of our own branching, including how the fence body adopts the editor's
 * content element.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { createPortal } from "react-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { NodeViewProps } from "@tiptap/react";
import { TipTapCodeBlockView } from "./TipTapCodeBlockView";
import { CODE_BLOCK_LANGUAGE_EDIT_META } from "./codeBlockLanguageEdit";

// The context ref @tiptap/react uses to place the content element itself.
const reactNodeView = vi.hoisted(() => ({ nodeViewContentRef: vi.fn() }));

vi.mock("@tiptap/react", () => ({
  NodeViewWrapper: ({ children }: { children: React.ReactNode }) => (
    <div data-node-view-wrapper="">{children}</div>
  ),
  useReactNodeView: () => reactNodeView,
}));

vi.mock("./MermaidPreview", () => ({
  MermaidPreview: ({ source }: { source: string }) => (
    <div data-testid="mermaid-preview">{source}</div>
  ),
}));

afterEach(() => vi.clearAllMocks());

function renderView(
  overrides: {
    language?: string | null;
    textContent?: string;
    editable?: boolean;
    focused?: boolean;
  } = {},
  host?: HTMLElement,
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
      isFocused: overrides.focused ?? false,
      view: { state: { tr }, dispatch },
    },
  } as unknown as NodeViewProps;
  // @tiptap/react renders a node view through a portal into its host element.
  const view = <TipTapCodeBlockView {...props} />;
  const { unmount } = render(host ? createPortal(view, host) : view);
  return { tr, dispatch, unmount };
}

/**
 * The host @tiptap/react renders a node view into: the editor's content element
 * sits beside the React tree until the fence body adopts it.
 */
function renderInHost(focused: boolean) {
  const host = document.createElement("div");
  host.className = "react-renderer";
  const content = document.createElement("div");
  content.setAttribute("data-node-view-content-react", "");
  content.setAttribute("data-node-view-wrapper", "");
  host.appendChild(content);
  document.body.appendChild(host);
  const { unmount } = renderView({ language: "python", focused }, host);
  const body = host.querySelector("code[data-node-view-content]");
  return {
    content,
    body,
    host,
    // Unmount the portal before its host leaves the document.
    cleanup: () => {
      unmount();
      host.remove();
    },
  };
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

describe("fence body", () => {
  // Unmounting the previous test's view forwards a null ref after mocks were cleared.
  beforeEach(() => reactNodeView.nodeViewContentRef.mockClear());

  it("adopts the content element itself while the editor is unfocused", () => {
    // @tiptap/react's ref would first read the DOM selection, which lays out
    // the whole document; unfocused there is no caret to preserve.
    const { content, body, cleanup } = renderInHost(false);
    try {
      expect(body?.firstChild).toBe(content);
      expect(content.hasAttribute("data-node-view-wrapper")).toBe(false);
      expect(reactNodeView.nodeViewContentRef).not.toHaveBeenCalled();
    } finally {
      cleanup();
    }
  });

  it("lets @tiptap/react place the content while the editor has focus", () => {
    const { host, content, body, cleanup } = renderInHost(true);
    try {
      expect(reactNodeView.nodeViewContentRef).toHaveBeenCalledWith(body);
      expect(content.parentElement).toBe(host);
    } finally {
      cleanup();
    }
  });

  it("falls back to @tiptap/react when the host markup is not recognised", () => {
    renderView({ language: "python" });
    const body = document.querySelector("code[data-node-view-content]");
    expect(reactNodeView.nodeViewContentRef).toHaveBeenCalledWith(body);
  });
});
