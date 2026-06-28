// TipTap comment interaction layer: floating "Add Comment" button,
// pending decoration lifecycle, click-outside handling, and scroll-to-active.
//
// Wires:
//   • A floating "Add Comment" button (portal to document.body) that appears
//     on non-collapsed selection and calls onSetActiveSelection.
//   • A "pending" blue highlight while the user types in the comment textarea.
//   • Scroll-to-active when the parent's activeSelection points to a comment.
//   • Click-outside-editor logic to clear the active selection.
//
// Keeps commentStateRef in sync and dispatches a rebuild transaction whenever
// comments, activeSelection, or pendingRange changes.

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MessageSquarePlusIcon } from "lucide-react";
import type { Editor } from "@tiptap/react";
import type { RefObject } from "react";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";
import { commentDecorationKey, type CommentDecorationState } from "./TipTapCommentExtension";
import { computeSelectionData } from "./TipTapEditorHelpers";
import { getEmbedRoot } from "@/lib/host";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Dispatch a decoration-rebuild transaction.
 *
 * Pass `pendingRange` to embed it directly in the meta so the plugin writes
 * it to stateRef inside apply() before the React sync-effect runs.
 */
function rebuildDecorations(editor: Editor, pendingRange?: { from: number; to: number } | null) {
  const meta = pendingRange !== undefined ? { pendingRange } : "rebuild";
  const tr = editor.state.tr.setMeta(commentDecorationKey, meta);
  editor.view.dispatch(tr);
}

// ---------------------------------------------------------------------------
// MarkdownCommentPlugin
// ---------------------------------------------------------------------------

interface MarkdownCommentPluginProps {
  editor: Editor | null;
  /** Shared mutable ref pointing to the raw server file content. */
  contentRef: RefObject<string>;
  /** Shared mutable ref used by CommentDecorationExtension's plugin. */
  commentStateRef: RefObject<CommentDecorationState | null>;
  comments: Comment[];
  isDirty: boolean;
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (
    sel: { start_index: number; end_index: number; anchor_content: string } | null,
  ) => void;
  pendingBodyRef?: RefObject<string>;
  canEdit?: boolean;
}

export function MarkdownCommentPlugin({
  editor,
  contentRef,
  commentStateRef,
  comments,
  isDirty,
  activeSelection,
  onSetActiveSelection,
  pendingBodyRef,
  canEdit = true,
}: MarkdownCommentPluginProps): JSX.Element | null {
  const [buttonPos, setButtonPos] = useState<{ top: number; left: number } | null>(null);

  // Stable refs so callbacks always see the latest values.
  const onSetActiveSelectionRef = useRef(onSetActiveSelection);
  useEffect(() => {
    onSetActiveSelectionRef.current = onSetActiveSelection;
  }, [onSetActiveSelection]);

  const activeSelectionRef = useRef(activeSelection);
  useEffect(() => {
    activeSelectionRef.current = activeSelection;
  }, [activeSelection]);

  const isDirtyRef = useRef(isDirty);
  useEffect(() => {
    isDirtyRef.current = isDirty;
  }, [isDirty]);

  const canEditRef = useRef(canEdit);
  useEffect(() => {
    canEditRef.current = canEdit;
  }, [canEdit]);

  // Tracks the PM range of the in-progress (pending) comment highlight.
  const pendingRangeRef = useRef<{ from: number; to: number } | null>(null);

  // --- Sync comment state into the ProseMirror plugin ---
  useEffect(() => {
    if (!editor) return;
    commentStateRef.current = {
      comments,
      activeSelection,
      rawContent: contentRef.current,
      pendingRange: pendingRangeRef.current,
      onClickComment: (comment: Comment) => {
        onSetActiveSelectionRef.current({
          start_index: comment.start_index,
          end_index: comment.end_index,
          anchor_content: comment.anchor_content ?? "",
        });
      },
    };
    rebuildDecorations(editor);
  }, [editor, comments, activeSelection, contentRef, commentStateRef]);

  // --- When parent clears activeSelection → remove pending decoration ---
  const prevActiveSelectionRef = useRef(activeSelection);
  useEffect(() => {
    const prev = prevActiveSelectionRef.current;
    prevActiveSelectionRef.current = activeSelection;
    if (prev !== null && activeSelection === null) {
      pendingRangeRef.current = null;
      if (commentStateRef.current) {
        commentStateRef.current.pendingRange = null;
      }
      if (editor) rebuildDecorations(editor, null);
    }
  }, [activeSelection, editor, commentStateRef]);

  // --- Scroll to the active comment ---
  useEffect(() => {
    if (!editor || !activeSelection) return;
    const comment = comments.find(
      (c) =>
        c.start_index === activeSelection.start_index && c.end_index === activeSelection.end_index,
    );
    if (!comment) return;
    const rafId = requestAnimationFrame(() => {
      editor.view.dom
        .querySelector(`[data-comment-id="${comment.id}"]`)
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    return () => cancelAnimationFrame(rafId);
  }, [editor, activeSelection, comments]);

  // --- Click on empty editor area → clear active selection ---
  useEffect(() => {
    if (!editor) return;
    const handleClick = (e: MouseEvent) => {
      if (!editor.view.dom.contains(e.target as Node)) return;
      const onCommentEl = (e.target as Element).closest("[data-comment-id]");
      const onAddBtn = (e.target as Element).closest("[data-add-comment-btn]");
      if (!onCommentEl && !onAddBtn && activeSelectionRef.current !== null) {
        const hasDraft = pendingRangeRef.current !== null && !!pendingBodyRef?.current?.trim();
        if (!hasDraft) {
          onSetActiveSelectionRef.current(null);
        }
      }
    };
    editor.view.dom.addEventListener("click", handleClick, true);
    return () => editor.view.dom.removeEventListener("click", handleClick, true);
  }, [editor, pendingBodyRef]);

  // --- Floating button: show on non-collapsed selection ---
  useEffect(() => {
    if (!editor) return;

    const updateButton = () => {
      if (isDirtyRef.current || !canEditRef.current) {
        setButtonPos(null);
        return;
      }
      const { selection } = editor.state;
      if (selection.empty) {
        setButtonPos(null);
        return;
      }

      // Hide if the selection contains no actual text (e.g. only a cursor in
      // an empty node). textBetween with "\n" separator gives us the plain text.
      const selectedText = editor.state.doc.textBetween(selection.from, selection.to, "\n");
      if (!selectedText.trim()) {
        setButtonPos(null);
        return;
      }

      const nativeSel = window.getSelection();
      if (!nativeSel || nativeSel.rangeCount === 0) return;
      const range = nativeSel.getRangeAt(0);
      const rect = range.getClientRects()[0] ?? range.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return;
      setButtonPos({ top: rect.top - 36, left: rect.left });
    };

    const hideButton = () => setTimeout(() => setButtonPos(null), 150);

    editor.on("selectionUpdate", updateButton);
    editor.on("blur", hideButton);

    return () => {
      editor.off("selectionUpdate", updateButton);
      editor.off("blur", hideButton);
    };
  }, [editor, contentRef]);

  // --- Button click: create pending decoration and call parent ---
  const handleAddComment = useCallback(() => {
    if (!editor) return;
    const { state } = editor;
    const { selection } = state;
    if (selection.empty) return;

    const data = computeSelectionData(selection.from, selection.to, state.doc, contentRef.current);
    if (!data) return;

    const newRange = { from: selection.from, to: selection.to };
    pendingRangeRef.current = newRange;
    if (commentStateRef.current) {
      commentStateRef.current.pendingRange = newRange;
    }
    rebuildDecorations(editor, newRange);

    onSetActiveSelectionRef.current(data);
    setButtonPos(null);
  }, [editor, contentRef, commentStateRef]);

  if (!buttonPos) return null;

  return createPortal(
    <button
      type="button"
      data-add-comment-btn
      // onMouseDown + e.preventDefault() keeps the editor focused so the
      // PM selection is still live when handleAddComment reads it.
      onMouseDown={(e) => {
        e.preventDefault();
        handleAddComment();
      }}
      className="fixed z-50 flex items-center gap-1.5 rounded-md border border-border bg-popover backdrop-blur-xl backdrop-saturate-150 px-2.5 py-1 text-xs font-medium text-foreground shadow-md hover:bg-secondary transition-colors"
      style={{ left: buttonPos.left, top: buttonPos.top, transform: "translateY(-100%)" }}
    >
      <MessageSquarePlusIcon className="size-3.5" />
      Add comment
    </button>,
    getEmbedRoot() ?? document.body,
  );
}
