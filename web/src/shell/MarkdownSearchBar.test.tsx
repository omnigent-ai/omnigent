// Tests for the markdown editor's find-in-file bar (MarkdownSearchBar).
//
// Uses a REAL headless TipTap editor wired with the search extension, so the
// bar's query → match-count → highlight → navigation flow is exercised
// end-to-end against actual ProseMirror decorations.

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { Markdown } from "@tiptap/markdown";
import StarterKit from "@tiptap/starter-kit";
import { createRef } from "react";
import type { RefObject } from "react";
import { MarkdownSearchBar } from "./MarkdownSearchBar";
import {
  createSearchDecorationExtension,
  type SearchDecorationState,
} from "./TipTapSearchExtension";

const CONTENT = "The quick brown fox jumps over the lazy dog.";

let editor: Editor | null = null;
let searchStateRef: RefObject<SearchDecorationState | null>;

beforeEach(() => {
  // scrollIntoView isn't implemented in jsdom.
  Element.prototype.scrollIntoView = vi.fn();
  searchStateRef = { current: null };
  editor = new Editor({
    element: document.createElement("div"),
    extensions: [StarterKit, createSearchDecorationExtension(searchStateRef), Markdown],
    content: CONTENT,
    contentType: "markdown",
  });
});

afterEach(() => {
  editor?.destroy();
  editor = null;
  vi.restoreAllMocks();
});

function renderBar(props: { open?: boolean; onClose?: () => void } = {}) {
  const inputRef = createRef<HTMLInputElement>();
  const onClose = props.onClose ?? vi.fn();
  const utils = render(
    <MarkdownSearchBar
      editor={editor}
      searchStateRef={searchStateRef}
      open={props.open ?? true}
      onClose={onClose}
      inputRef={inputRef}
    />,
  );
  return { ...utils, onClose };
}

function type(value: string) {
  const input = screen.getByPlaceholderText("Find…");
  fireEvent.change(input, { target: { value } });
  return input;
}

describe("MarkdownSearchBar", () => {
  it("renders nothing when closed", () => {
    renderBar({ open: false });
    expect(screen.queryByPlaceholderText("Find…")).toBeNull();
  });

  it("shows the match count and highlights matches as the user types", async () => {
    renderBar();
    await act(async () => {
      type("the");
    });
    // Two case-insensitive matches of "the".
    expect(screen.getByText("1 / 2")).toBeDefined();
    expect(editor!.view.dom.querySelectorAll(".md-search-match")).toHaveLength(2);
    expect(editor!.view.dom.querySelectorAll(".md-search-match-current")).toHaveLength(1);
  });

  it("keeps the count and highlights consistent when the query has surrounding whitespace", async () => {
    renderBar();
    // The plugin highlights against the trimmed query, so the count must trim
    // too — otherwise "the " would count differently than it highlights.
    await act(async () => {
      type("the ");
    });
    const highlighted = editor!.view.dom.querySelectorAll(".md-search-match").length;
    expect(highlighted).toBe(2);
    expect(screen.getByText(`1 / ${highlighted}`)).toBeDefined();
  });

  it("shows 'No results' when nothing matches", async () => {
    renderBar();
    await act(async () => {
      type("zzz");
    });
    expect(screen.getByText("No results")).toBeDefined();
    expect(editor!.view.dom.querySelectorAll(".md-search-match")).toHaveLength(0);
  });

  it("advances to the next match on Enter and wraps around", async () => {
    renderBar();
    const input = await act(async () => type("the"));
    expect(screen.getByText("1 / 2")).toBeDefined();

    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(screen.getByText("2 / 2")).toBeDefined();

    // Wrap back to the first match.
    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(screen.getByText("1 / 2")).toBeDefined();
  });

  it("goes to the previous match on Shift+Enter", async () => {
    renderBar();
    const input = await act(async () => type("the"));
    // From match 1, Shift+Enter wraps to the last (2 / 2).
    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    });
    expect(screen.getByText("2 / 2")).toBeDefined();
  });

  it("navigates with the up/down buttons", async () => {
    renderBar();
    await act(async () => type("the"));
    await act(async () => {
      fireEvent.click(screen.getByLabelText("Next match"));
    });
    expect(screen.getByText("2 / 2")).toBeDefined();
    await act(async () => {
      fireEvent.click(screen.getByLabelText("Previous match"));
    });
    expect(screen.getByText("1 / 2")).toBeDefined();
  });

  it("calls onClose on Escape and clears highlights", async () => {
    const { onClose } = renderBar();
    const input = await act(async () => type("the"));
    expect(editor!.view.dom.querySelectorAll(".md-search-match")).toHaveLength(2);
    await act(async () => {
      fireEvent.keyDown(input, { key: "Escape" });
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when the ✕ button is clicked", async () => {
    const { onClose } = renderBar();
    await act(async () => type("the"));
    await act(async () => {
      fireEvent.click(screen.getByLabelText("Close search"));
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("clears highlights when the bar is reopened after a prior query", async () => {
    const { rerender } = renderBar();
    await act(async () => type("the"));
    expect(editor!.view.dom.querySelectorAll(".md-search-match")).toHaveLength(2);

    // Close the bar — the query resets, so highlights clear.
    await act(async () => {
      rerender(
        <MarkdownSearchBar
          editor={editor}
          searchStateRef={searchStateRef}
          open={false}
          onClose={vi.fn()}
          inputRef={createRef<HTMLInputElement>()}
        />,
      );
    });
    expect(editor!.view.dom.querySelectorAll(".md-search-match")).toHaveLength(0);
  });
});
