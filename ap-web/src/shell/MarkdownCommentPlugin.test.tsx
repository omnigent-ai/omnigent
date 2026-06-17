// Tests for MarkdownCommentPlugin — the comment interaction layer.
//
// The floating "Add comment" button is positioned from
// window.getSelection().getRangeAt(0).getClientRects(), which jsdom returns as
// zero-sized rects, so the button never mounts under test (the source bails on
// a 0×0 rect). These tests therefore cover the parts that ARE deterministic in
// jsdom:
//   - the component renders no button (returns null) by default;
//   - the state-sync effect populates commentStateRef and drives the decoration
//     extension to render comment highlights in a real editor;
//   - commentStateRef.onClickComment maps a clicked comment to onSetActiveSelection;
//   - activeSelection going non-null → null clears the pending range;
//   - click-outside on empty editor area clears the active selection, but a click
//     on a comment / add-comment element (or with a live draft) does not.
//
// A real headless TipTap Editor is used so the decoration extension exercises
// real ProseMirror behaviour.

import { cleanup, render } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { Markdown } from "@tiptap/markdown";
import StarterKit from "@tiptap/starter-kit";
import type { RefObject } from "react";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";
import { MarkdownCommentPlugin } from "./MarkdownCommentPlugin";
import {
  createCommentDecorationExtension,
  type CommentDecorationState,
} from "./TipTapCommentExtension";

const CONTENT = "The quick brown fox jumps over the lazy dog.";

let editor: Editor | null = null;
let commentStateRef: RefObject<CommentDecorationState | null>;
let contentRef: RefObject<string>;

beforeEach(() => {
  commentStateRef = { current: null };
  contentRef = { current: CONTENT };
});

afterEach(() => {
  cleanup();
  editor?.destroy();
  editor = null;
});

/** Mounts a real headless editor wired to the shared decoration state ref. */
function makeEditor(): Editor {
  return new Editor({
    element: document.createElement("div"),
    extensions: [StarterKit, createCommentDecorationExtension(commentStateRef), Markdown],
    content: CONTENT,
    contentType: "markdown",
  });
}

function makeComment(overrides: Partial<Comment> = {}): Comment {
  return {
    id: "c1",
    conversation_id: "conv_1",
    path: "file.md",
    start_index: 4,
    end_index: 9,
    body: "comment body",
    status: "draft",
    created_at: 0,
    updated_at: 0,
    anchor_content: "quick",
    created_by: null,
    ...overrides,
  };
}

interface RenderOpts {
  comments?: Comment[];
  isDirty?: boolean;
  activeSelection?: ActiveSelection | null;
  onSetActiveSelection?: (
    sel: { start_index: number; end_index: number; anchor_content: string } | null,
  ) => void;
  pendingBodyRef?: RefObject<string>;
  canEdit?: boolean;
}

function renderPlugin(opts: RenderOpts = {}) {
  const onSetActiveSelection = opts.onSetActiveSelection ?? vi.fn();
  const utils = render(
    <MarkdownCommentPlugin
      editor={editor}
      contentRef={contentRef}
      commentStateRef={commentStateRef}
      comments={opts.comments ?? []}
      isDirty={opts.isDirty ?? false}
      activeSelection={opts.activeSelection ?? null}
      onSetActiveSelection={onSetActiveSelection}
      pendingBodyRef={opts.pendingBodyRef}
      canEdit={opts.canEdit}
    />,
  );
  return { ...utils, onSetActiveSelection };
}

// ---------------------------------------------------------------------------
// Render contract
// ---------------------------------------------------------------------------

describe("MarkdownCommentPlugin render", () => {
  // WHY: with no editor and no selection the plugin renders nothing (returns null).
  it("renders no button when editor is null", () => {
    editor = null;
    const { container } = renderPlugin();
    expect(container.querySelector("[data-add-comment-btn]")).toBeNull();
  });

  // WHY: in jsdom getClientRects is empty so the button never positions itself.
  it("renders no floating button by default even with an editor", () => {
    editor = makeEditor();
    renderPlugin();
    expect(document.querySelector("[data-add-comment-btn]")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// State sync into the decoration extension
// ---------------------------------------------------------------------------

describe("MarkdownCommentPlugin state sync", () => {
  // WHY: the sync effect must populate commentStateRef from the props.
  it("populates commentStateRef.current from props", () => {
    editor = makeEditor();
    const comments = [makeComment()];
    renderPlugin({ comments });
    expect(commentStateRef.current).not.toBeNull();
    expect(commentStateRef.current!.comments).toBe(comments);
    expect(commentStateRef.current!.rawContent).toBe(CONTENT);
  });

  // WHY: syncing + rebuilding must surface the comment highlight in the editor DOM.
  it("drives the decoration extension to render comment highlights", () => {
    editor = makeEditor();
    renderPlugin({ comments: [makeComment()] });
    const span = editor.view.dom.querySelector('[data-comment-id="c1"]');
    expect(span).not.toBeNull();
    expect(span!.textContent).toBe("quick");
  });

  // WHY: the wired onClickComment must translate a comment into an active selection.
  it("maps onClickComment to onSetActiveSelection with the comment's anchor", () => {
    editor = makeEditor();
    const comment = makeComment();
    const { onSetActiveSelection } = renderPlugin({ comments: [comment] });
    act(() => {
      commentStateRef.current!.onClickComment(comment);
    });
    expect(onSetActiveSelection).toHaveBeenCalledWith({
      start_index: 4,
      end_index: 9,
      anchor_content: "quick",
    });
  });

  // WHY: a comment with null anchor_content must yield "" (never null) downstream.
  it("falls back to empty anchor_content when the comment has none", () => {
    editor = makeEditor();
    const comment = makeComment({ anchor_content: null });
    const { onSetActiveSelection } = renderPlugin({ comments: [comment] });
    act(() => {
      commentStateRef.current!.onClickComment(comment);
    });
    expect(onSetActiveSelection).toHaveBeenCalledWith(
      expect.objectContaining({ anchor_content: "" }),
    );
  });
});

// ---------------------------------------------------------------------------
// Pending-range cleanup on activeSelection clear
// ---------------------------------------------------------------------------

describe("MarkdownCommentPlugin pending-range cleanup", () => {
  // WHY: when the parent clears a previously-set activeSelection, any pending
  // highlight must be torn down.
  it("clears pendingRange when activeSelection transitions non-null → null", () => {
    editor = makeEditor();
    const active: ActiveSelection = { start_index: 4, end_index: 9, anchor_content: "quick" };
    const { rerender } = renderPlugin({ activeSelection: active });
    // Seed a pending range as the button-click path would.
    act(() => {
      commentStateRef.current!.pendingRange = { from: 1, to: 4 };
    });

    rerender(
      <MarkdownCommentPlugin
        editor={editor}
        contentRef={contentRef}
        commentStateRef={commentStateRef}
        comments={[]}
        isDirty={false}
        activeSelection={null}
        onSetActiveSelection={vi.fn()}
      />,
    );

    expect(commentStateRef.current!.pendingRange).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Click-outside clears the active selection
// ---------------------------------------------------------------------------

describe("MarkdownCommentPlugin click-outside", () => {
  // WHY: clicking empty editor area while a selection is active should clear it.
  it("clears active selection on a click that misses comments and the add button", () => {
    editor = makeEditor();
    const active: ActiveSelection = { start_index: 4, end_index: 9, anchor_content: "quick" };
    const { onSetActiveSelection } = renderPlugin({ activeSelection: active });

    const p = editor.view.dom.querySelector("p")!;
    act(() => {
      p.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onSetActiveSelection).toHaveBeenCalledWith(null);
  });

  // WHY: clicking inside a comment decoration must NOT clear the active selection
  // (that click is the comment-extension's own select-this-comment gesture).
  it("does not clear when the click lands on a comment element", () => {
    editor = makeEditor();
    const active: ActiveSelection = { start_index: 4, end_index: 9, anchor_content: "quick" };
    const { onSetActiveSelection } = renderPlugin({
      comments: [makeComment()],
      activeSelection: active,
    });

    const span = editor.view.dom.querySelector('[data-comment-id="c1"]') as HTMLElement;
    act(() => {
      span.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onSetActiveSelection).not.toHaveBeenCalledWith(null);
  });

  // WHY: clicks outside the editor DOM entirely must be ignored.
  it("ignores clicks outside the editor DOM", () => {
    editor = makeEditor();
    const active: ActiveSelection = { start_index: 4, end_index: 9, anchor_content: "quick" };
    const { onSetActiveSelection } = renderPlugin({ activeSelection: active });
    act(() => {
      document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onSetActiveSelection).not.toHaveBeenCalled();
  });
});
