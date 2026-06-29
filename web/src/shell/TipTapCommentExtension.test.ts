// Unit tests for the comment-decoration ProseMirror plugin / TipTap extension.
//
// Coverage:
//   - commentDecorationKey is a stable PluginKey.
//   - A real headless Editor wired with createCommentDecorationExtension renders
//     inline decorations (md-comment / md-comment-active) for matched comments,
//     a md-comment-pending highlight for the in-progress range, and skips
//     comments whose anchor_content isn't found in the doc.
//   - A "rebuild" meta dispatch re-reads stateRef so newly added comments appear.
//   - A { pendingRange } meta dispatch writes the range into stateRef before the
//     decoration rebuilds.
//   - Clicking a decorated span invokes onClickComment with the matched comment.
//
// Uses a real TipTap Editor (real schema + real @tiptap/markdown parsing) so
// regressions in decoration / remap behaviour fail the test.

import { afterEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { Markdown } from "@tiptap/markdown";
import StarterKit from "@tiptap/starter-kit";
import { PluginKey } from "@tiptap/pm/state";
import type { RefObject } from "react";
import type { Comment } from "@/hooks/useComments";
import {
  commentDecorationKey,
  createCommentDecorationExtension,
  type CommentDecorationState,
} from "./TipTapCommentExtension";

let editor: Editor | null = null;
afterEach(() => {
  editor?.destroy();
  editor = null;
});

const CONTENT = "The quick brown fox jumps over the lazy dog.";

/** A comment anchored to a verbatim substring of CONTENT. */
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

/** A stable RefObject holding the supplied decoration state. */
function makeStateRef(
  state: CommentDecorationState | null,
): RefObject<CommentDecorationState | null> {
  return { current: state };
}

/** Mounts a headless editor wired with the decoration extension. */
function makeEditor(stateRef: RefObject<CommentDecorationState | null>): Editor {
  return new Editor({
    element: document.createElement("div"),
    extensions: [StarterKit, createCommentDecorationExtension(stateRef), Markdown],
    content: CONTENT,
    contentType: "markdown",
  });
}

// ---------------------------------------------------------------------------
// commentDecorationKey
// ---------------------------------------------------------------------------

describe("commentDecorationKey", () => {
  // WHY: downstream meta dispatches target this exact key, so it must be a PluginKey.
  it("is a PluginKey", () => {
    expect(commentDecorationKey).toBeInstanceOf(PluginKey);
  });
});

// ---------------------------------------------------------------------------
// Decoration rendering
// ---------------------------------------------------------------------------

describe("createCommentDecorationExtension decorations", () => {
  // WHY: a matched comment must render an inline span tagged with its id and the base class.
  it("renders an inline decoration for a matched comment", () => {
    const stateRef = makeStateRef({
      comments: [makeComment()],
      activeSelection: null,
      rawContent: CONTENT,
      pendingRange: null,
      onClickComment: vi.fn(),
    });
    editor = makeEditor(stateRef);
    const span = editor.view.dom.querySelector('[data-comment-id="c1"]');
    expect(span).not.toBeNull();
    expect(span!.classList.contains("md-comment")).toBe(true);
    expect(span!.classList.contains("md-comment-active")).toBe(false);
    expect(span!.textContent).toBe("quick");
  });

  // WHY: the active comment (matching activeSelection offsets) gets the -active class.
  it("adds md-comment-active when the comment matches activeSelection", () => {
    const stateRef = makeStateRef({
      comments: [makeComment()],
      activeSelection: { start_index: 4, end_index: 9, anchor_content: "quick" },
      rawContent: CONTENT,
      pendingRange: null,
      onClickComment: vi.fn(),
    });
    editor = makeEditor(stateRef);
    const span = editor.view.dom.querySelector('[data-comment-id="c1"]')!;
    expect(span.classList.contains("md-comment-active")).toBe(true);
  });

  // WHY: comments whose anchor text isn't in the doc must be silently skipped.
  it("skips comments whose anchor_content is not found", () => {
    const stateRef = makeStateRef({
      comments: [makeComment({ anchor_content: "nonexistent text" })],
      activeSelection: null,
      rawContent: CONTENT,
      pendingRange: null,
      onClickComment: vi.fn(),
    });
    editor = makeEditor(stateRef);
    expect(editor.view.dom.querySelector("[data-comment-id]")).toBeNull();
  });

  // WHY: the in-progress (pending) selection gets its own blue-highlight class.
  it("renders a md-comment-pending decoration for the pending range", () => {
    const stateRef = makeStateRef({
      comments: [],
      activeSelection: null,
      rawContent: CONTENT,
      // PM positions: 1 = doc start (paragraph open), so [1,4) covers "The".
      pendingRange: { from: 1, to: 4 },
      onClickComment: vi.fn(),
    });
    editor = makeEditor(stateRef);
    expect(editor.view.dom.querySelector(".md-comment-pending")).not.toBeNull();
  });

  // WHY: with null stateRef the plugin must produce no decorations, not throw.
  it("renders nothing when stateRef is null", () => {
    const stateRef = makeStateRef(null);
    editor = makeEditor(stateRef);
    expect(editor.view.dom.querySelector("[data-comment-id]")).toBeNull();
    expect(editor.view.dom.querySelector(".md-comment-pending")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// rebuild / pendingRange meta dispatches
// ---------------------------------------------------------------------------

describe("createCommentDecorationExtension meta dispatch", () => {
  // WHY: mutating stateRef + dispatching "rebuild" must surface the new comment.
  it("re-reads stateRef on a 'rebuild' meta dispatch", () => {
    const stateRef = makeStateRef({
      comments: [],
      activeSelection: null,
      rawContent: CONTENT,
      pendingRange: null,
      onClickComment: vi.fn(),
    });
    editor = makeEditor(stateRef);
    expect(editor.view.dom.querySelector("[data-comment-id]")).toBeNull();

    stateRef.current!.comments = [makeComment()];
    editor.view.dispatch(editor.state.tr.setMeta(commentDecorationKey, "rebuild"));

    expect(editor.view.dom.querySelector('[data-comment-id="c1"]')).not.toBeNull();
  });

  // WHY: a { pendingRange } meta writes the range into stateRef and renders it.
  it("writes pendingRange from meta into stateRef and renders it", () => {
    const stateRef = makeStateRef({
      comments: [],
      activeSelection: null,
      rawContent: CONTENT,
      pendingRange: null,
      onClickComment: vi.fn(),
    });
    editor = makeEditor(stateRef);

    editor.view.dispatch(
      editor.state.tr.setMeta(commentDecorationKey, { pendingRange: { from: 1, to: 4 } }),
    );

    expect(stateRef.current!.pendingRange).toEqual({ from: 1, to: 4 });
    expect(editor.view.dom.querySelector(".md-comment-pending")).not.toBeNull();
  });

  // WHY: a meta dispatch while stateRef is null must clear to an empty set safely.
  it("clears decorations when a meta dispatch arrives with null stateRef", () => {
    const stateRef = makeStateRef(null);
    editor = makeEditor(stateRef);
    editor.view.dispatch(editor.state.tr.setMeta(commentDecorationKey, "rebuild"));
    expect(editor.view.dom.querySelector("[data-comment-id]")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// click handling
// ---------------------------------------------------------------------------

describe("createCommentDecorationExtension click handling", () => {
  // WHY: clicking a decorated span resolves the comment by id and invokes the callback.
  it("invokes onClickComment with the matched comment when its decoration is clicked", () => {
    const onClickComment = vi.fn();
    const comment = makeComment();
    const stateRef = makeStateRef({
      comments: [comment],
      activeSelection: null,
      rawContent: CONTENT,
      pendingRange: null,
      onClickComment,
    });
    editor = makeEditor(stateRef);
    const span = editor.view.dom.querySelector('[data-comment-id="c1"]') as HTMLElement;
    span.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(onClickComment).toHaveBeenCalledWith(comment);
  });

  // WHY: clicking outside any decoration must not fire the click callback.
  it("does not invoke onClickComment when the click misses every decoration", () => {
    const onClickComment = vi.fn();
    const stateRef = makeStateRef({
      comments: [makeComment()],
      activeSelection: null,
      rawContent: CONTENT,
      pendingRange: null,
      onClickComment,
    });
    editor = makeEditor(stateRef);
    editor.view.dom.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(onClickComment).not.toHaveBeenCalled();
  });
});
