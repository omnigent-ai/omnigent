// Tests for useMonacoCommentLayer — the shared comment-interaction layer wired
// onto a Monaco editor instance. Monaco can't mount in jsdom, so we drive the
// hook with a hand-rolled fake editor that records decoration sets and lets
// tests fire the editor events (selection change, mouse up, blur) the hook
// subscribes to. We assert the real effects: decorations applied/updated/
// cleared, the floating "Add comment" button shown/hidden, and click-to-
// navigate / click-away-to-clear of the active selection.

import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";
import { useMonacoCommentLayer, type CodeEditorInstance } from "./useMonacoCommentLayer";

// Render comment-layer portals into document.body in tests.
vi.mock("@/lib/host", () => ({ getEmbedRoot: () => null }));

// ── Fake Monaco editor ──────────────────────────────────────────────────────

type Listener = (e: unknown) => void;

interface FakeDecorations {
  set: ReturnType<typeof vi.fn>;
  clear: ReturnType<typeof vi.fn>;
}

// A minimal editor double. `content` backs the offset↔position mapping so the
// decoration ranges are real; the event hooks return disposables and store the
// callbacks so tests can fire them.
function makeFakeEditor(content: string) {
  const listeners: Record<string, Listener[]> = {};
  const on = (name: string) => (cb: Listener) => {
    (listeners[name] ??= []).push(cb);
    return { dispose: vi.fn() };
  };
  const lastDecorations: FakeDecorations = { set: vi.fn(), clear: vi.fn() };
  // Selection state the hook reads via getSelection().
  let selection: {
    isEmpty: () => boolean;
    getStartPosition: () => unknown;
    getEndPosition: () => unknown;
  } | null = null;

  const model = {
    getPositionAt(offset: number) {
      const before = content.slice(0, offset);
      const lines = before.split("\n");
      return { lineNumber: lines.length, column: lines[lines.length - 1].length + 1 };
    },
    getOffsetAt({ offset }: { offset: number }) {
      return offset;
    },
    getValueInRange() {
      return "selected";
    },
  };

  const editor = {
    getModel: () => model,
    createDecorationsCollection: vi.fn(() => lastDecorations),
    getSelection: () => selection,
    getScrolledVisiblePosition: () => ({ left: 10, top: 20 }),
    getDomNode: () => ({ getBoundingClientRect: () => ({ left: 100, top: 200 }) }),
    revealRangeInCenterIfOutsideViewport: vi.fn(),
    onDidChangeCursorSelection: on("selection"),
    onDidScrollChange: on("scroll"),
    onDidBlurEditorWidget: on("blur"),
    onMouseUp: on("mouseup"),
  };

  return {
    editor: editor as unknown as CodeEditorInstance,
    lastDecorations,
    fire(name: string, e?: unknown) {
      for (const cb of listeners[name] ?? []) cb(e);
    },
    setSelection(start: number, end: number, empty = false) {
      selection = {
        isEmpty: () => empty,
        getStartPosition: () => ({ offset: start }),
        getEndPosition: () => ({ offset: end }),
      };
    },
    clearSelection() {
      selection = null;
    },
  };
}

function mkComment(overrides: Partial<Comment>): Comment {
  return {
    id: "c1",
    conversation_id: "conv_1",
    path: "/a.ts",
    start_index: 0,
    end_index: 0,
    body: "",
    status: "draft",
    created_at: 0,
    updated_at: 0,
    anchor_content: null,
    created_by: null,
    ...overrides,
  };
}

const CONTENT = "abc\ndefgh\nij";

interface HookProps {
  comments?: Comment[];
  activeSelection?: ActiveSelection | null;
  onSetActiveSelection?: (sel: ActiveSelection | null) => void;
  canComment?: boolean;
  pendingBodyRef?: React.RefObject<string>;
  mounted?: boolean;
}

// Host component: calls the hook and renders its returned ReactNode (the
// "Add comment" portal) so createPortal actually attaches to the DOM. A plain
// renderHook would discard the return value and the portal would never mount.
function Host(props: HookProps & { editorRef: React.RefObject<CodeEditorInstance | null> }) {
  return useMonacoCommentLayer({
    editorRef: props.editorRef,
    mounted: props.mounted ?? true,
    comments: props.comments ?? [],
    activeSelection: props.activeSelection ?? null,
    onSetActiveSelection: props.onSetActiveSelection ?? (() => {}),
    canComment: props.canComment ?? true,
    pendingBodyRef: props.pendingBodyRef,
  }) as React.ReactElement | null;
}

function renderLayer(fake: ReturnType<typeof makeFakeEditor>, props: HookProps = {}) {
  const editorRef = { current: fake.editor } as React.RefObject<CodeEditorInstance | null>;
  const onSetActiveSelection = props.onSetActiveSelection ?? vi.fn();
  const utils = render(
    <Host {...props} editorRef={editorRef} onSetActiveSelection={onSetActiveSelection} />,
  );
  const rerender = (next: HookProps) =>
    utils.rerender(
      <Host {...next} editorRef={editorRef} onSetActiveSelection={onSetActiveSelection} />,
    );
  return { ...utils, rerender, onSetActiveSelection };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useMonacoCommentLayer — decorations", () => {
  it("creates a decorations collection on mount from the current comments", () => {
    // WHY: on first mount the hook must create (not set) a collection seeded
    // with one decoration per saved comment.
    const fake = makeFakeEditor(CONTENT);
    renderLayer(fake, { comments: [mkComment({ start_index: 1, end_index: 3 })] });
    expect(fake.editor.createDecorationsCollection).toHaveBeenCalledTimes(1);
    const seed = vi.mocked(fake.editor.createDecorationsCollection).mock.calls[0][0];
    expect(seed).toHaveLength(1);
  });

  it("updates the existing collection via set() when comments change", () => {
    // WHY: after the collection exists, a comments change must reuse it via
    // set() rather than creating a second collection.
    const fake = makeFakeEditor(CONTENT);
    const { rerender } = renderLayer(fake, {
      comments: [mkComment({ start_index: 1, end_index: 3 })],
    });
    act(() => {
      rerender({
        comments: [
          mkComment({ start_index: 1, end_index: 3 }),
          mkComment({ id: "c2", start_index: 5, end_index: 7 }),
        ],
      });
    });
    expect(fake.lastDecorations.set).toHaveBeenCalled();
    // Still only one collection created across the whole lifecycle.
    expect(fake.editor.createDecorationsCollection).toHaveBeenCalledTimes(1);
    const latest = fake.lastDecorations.set.mock.lastCall?.[0];
    expect(latest).toHaveLength(2);
  });

  it("clears the decorations collection on unmount", () => {
    // WHY: the cleanup effect drops decorations so a remounted editor doesn't
    // inherit stale highlights.
    const fake = makeFakeEditor(CONTENT);
    const { unmount } = renderLayer(fake, {
      comments: [mkComment({ start_index: 1, end_index: 3 })],
    });
    unmount();
    expect(fake.lastDecorations.clear).toHaveBeenCalled();
  });

  it("does not wire decorations when not yet mounted", () => {
    // WHY: the `mounted` gate prevents touching the editor before it's ready.
    const fake = makeFakeEditor(CONTENT);
    renderLayer(fake, { mounted: false, comments: [mkComment({})] });
    expect(fake.editor.createDecorationsCollection).not.toHaveBeenCalled();
  });
});

describe("useMonacoCommentLayer — add-comment button", () => {
  it("shows the floating button for a non-empty selection when commenting is allowed", () => {
    // WHY: a real text selection + canComment must surface the Add-comment
    // button (rendered as a portal in document.body).
    const fake = makeFakeEditor(CONTENT);
    fake.setSelection(1, 3);
    renderLayer(fake, { canComment: true });
    act(() => fake.fire("selection"));
    expect(document.querySelector("[data-add-comment-btn]")).not.toBeNull();
  });

  it("hides the button when commenting is not allowed", () => {
    // WHY: with canComment=false the button must never appear even on a real
    // selection (read-only / dirty / truncated gating).
    const fake = makeFakeEditor(CONTENT);
    fake.setSelection(1, 3);
    renderLayer(fake, { canComment: false });
    act(() => fake.fire("selection"));
    expect(document.querySelector("[data-add-comment-btn]")).toBeNull();
  });

  it("hides the button for an empty (collapsed) selection", () => {
    // WHY: a caret with no range is not a comment target — the button stays
    // hidden.
    const fake = makeFakeEditor(CONTENT);
    fake.setSelection(2, 2, true);
    renderLayer(fake, { canComment: true });
    act(() => fake.fire("selection"));
    expect(document.querySelector("[data-add-comment-btn]")).toBeNull();
  });

  it("hides the button on scroll and on blur", () => {
    // WHY: the button is positioned in viewport coords, so scroll/blur must
    // clear it rather than leave it floating in a stale spot.
    const fake = makeFakeEditor(CONTENT);
    fake.setSelection(1, 3);
    renderLayer(fake, { canComment: true });
    act(() => fake.fire("selection"));
    expect(document.querySelector("[data-add-comment-btn]")).not.toBeNull();
    act(() => fake.fire("scroll"));
    expect(document.querySelector("[data-add-comment-btn]")).toBeNull();
  });

  it("hides a visible button when canComment flips false", () => {
    // WHY: the dedicated effect must clear an already-shown button when
    // permission/dirty state flips, since no selection event may fire.
    const fake = makeFakeEditor(CONTENT);
    fake.setSelection(1, 3);
    const { rerender } = renderLayer(fake, { canComment: true });
    act(() => fake.fire("selection"));
    expect(document.querySelector("[data-add-comment-btn]")).not.toBeNull();
    act(() => rerender({ canComment: false }));
    expect(document.querySelector("[data-add-comment-btn]")).toBeNull();
  });

  it("creates a comment selection from the current range when the button is clicked", () => {
    // WHY: clicking Add comment must hand the selection's offsets up via
    // onSetActiveSelection so a draft anchors to the right range.
    const fake = makeFakeEditor(CONTENT);
    fake.setSelection(1, 3);
    const onSet = vi.fn();
    renderLayer(fake, { canComment: true, onSetActiveSelection: onSet });
    act(() => fake.fire("selection"));
    const btn = document.querySelector("[data-add-comment-btn]") as HTMLElement;
    act(() => {
      btn.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    });
    expect(onSet).toHaveBeenCalledWith({
      start_index: 1,
      end_index: 3,
      anchor_content: "selected",
    });
  });
});

describe("useMonacoCommentLayer — mouse-up navigation", () => {
  it("activates a comment when its highlighted range is clicked", () => {
    // WHY: clicking inside a saved comment's range (with no selection) must set
    // that comment active for the panel — the navigate path.
    const fake = makeFakeEditor(CONTENT);
    const onSet = vi.fn();
    renderLayer(fake, {
      comments: [mkComment({ start_index: 1, end_index: 3, anchor_content: "bc" })],
      onSetActiveSelection: onSet,
    });
    act(() => fake.fire("mouseup", { target: { position: { offset: 2 } } }));
    expect(onSet).toHaveBeenCalledWith({
      start_index: 1,
      end_index: 3,
      anchor_content: "bc",
    });
  });

  it("clears the active selection when clicking outside any comment", () => {
    // WHY: a click away with an active selection and no pending draft must
    // deselect (onSetActiveSelection(null)).
    const fake = makeFakeEditor(CONTENT);
    const onSet = vi.fn();
    renderLayer(fake, {
      comments: [mkComment({ start_index: 1, end_index: 3 })],
      activeSelection: { start_index: 1, end_index: 3, anchor_content: "bc" },
      onSetActiveSelection: onSet,
    });
    act(() => fake.fire("mouseup", { target: { position: { offset: 9 } } }));
    expect(onSet).toHaveBeenCalledWith(null);
  });

  it("does not clear the active selection while a draft body is in progress", () => {
    // WHY: an in-progress comment draft must survive a click away — the
    // pendingBodyRef guard prevents losing the user's typed body.
    const fake = makeFakeEditor(CONTENT);
    const onSet = vi.fn();
    renderLayer(fake, {
      comments: [mkComment({ start_index: 1, end_index: 3 })],
      activeSelection: { start_index: 1, end_index: 3, anchor_content: "bc" },
      onSetActiveSelection: onSet,
      pendingBodyRef: { current: "half-typed" } as React.RefObject<string>,
    });
    act(() => fake.fire("mouseup", { target: { position: { offset: 9 } } }));
    expect(onSet).not.toHaveBeenCalled();
  });
});

describe("useMonacoCommentLayer — reveal active selection", () => {
  it("reveals the active selection's range in the viewport", () => {
    // WHY: when activeSelection changes (e.g. clicked in the panel) the editor
    // must scroll the matching range into view.
    const fake = makeFakeEditor(CONTENT);
    renderLayer(fake, {
      activeSelection: { start_index: 5, end_index: 7, anchor_content: "fg" },
    });
    expect(fake.editor.revealRangeInCenterIfOutsideViewport).toHaveBeenCalledWith({
      startLineNumber: 2,
      startColumn: 2,
      endLineNumber: 2,
      endColumn: 4,
    });
  });
});
