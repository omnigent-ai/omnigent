// Comment layer for the rendered Markdown preview: Add comment on a selection,
// highlights, and click-to-activate. Highlights use the CSS Custom Highlight
// API because react-markdown owns the preview DOM.

import { useEffect, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { MessageSquarePlusIcon } from "lucide-react";
import type { Comment } from "@/hooks/useComments";
import { useCanEdit } from "@/hooks/usePermissions";
import { getEmbedRoot } from "@/lib/host";
import { ADD_COMMENT_BUTTON_CLASS_NAME } from "./addCommentButtonStyles";
import type { ActiveSelection } from "./codeViewerHelpers";
import {
  previewPointToSource,
  previewRangeLookup,
  previewRangesForSource,
  previewSelectionToSource,
  type PreviewSourceSelection,
} from "./markdownPreviewSource";
import { highlightsSupported } from "./previewSearch";

const HIGHLIGHT_COMMENT = "md-preview-comment";
const HIGHLIGHT_ACTIVE = "md-preview-comment-active";
const HIGHLIGHT_PENDING = "md-preview-comment-pending";
// Touch selection ends without a mouseup, so a settled selection change also
// offers the button.
const SELECTION_SETTLE_MS = 300;

interface PaintedRanges {
  comment: Range[];
  active: Range[];
  pending: Range[];
}

// Highlights are document-wide, so each mounted preview registers its ranges
// and every registry change repaints the union.
const paintedByPreview = new Map<symbol, PaintedRanges>();

function repaint() {
  if (!highlightsSupported()) return;
  const groups: [string, keyof PaintedRanges][] = [
    [HIGHLIGHT_COMMENT, "comment"],
    [HIGHLIGHT_ACTIVE, "active"],
    [HIGHLIGHT_PENDING, "pending"],
  ];
  for (const [name, key] of groups) {
    const highlight = new Highlight();
    for (const painted of paintedByPreview.values()) {
      for (const range of painted[key]) highlight.add(range);
    }
    CSS.highlights.set(name, highlight);
  }
}

interface FloatingButton extends PreviewSourceSelection {
  x: number;
  y: number;
}

export interface MarkdownPreviewCommentsProps {
  conversationId: string;
  /** The preview's scroll container, which holds the rendered Markdown. */
  rootRef: RefObject<HTMLDivElement | null>;
  /** Raw Markdown the preview renders; comment offsets index into it. */
  content: string;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (selection: ActiveSelection | null) => void;
  /** Body of the draft being typed; a click elsewhere keeps a non-empty draft. */
  pendingBodyRef?: RefObject<string>;
}

export function MarkdownPreviewComments({
  conversationId,
  rootRef,
  content,
  comments,
  activeSelection,
  onSetActiveSelection,
  pendingBodyRef,
}: MarkdownPreviewCommentsProps) {
  const canEdit = useCanEdit(conversationId);
  const [floating, setFloating] = useState<FloatingButton | null>(null);

  const latest = useRef({ content, comments, activeSelection, onSetActiveSelection, canEdit });
  latest.current = { content, comments, activeSelection, onSetActiveSelection, canEdit };

  // Offer the button for a finished selection; a plain click activates the
  // comment under the caret or clears the active one.
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    let pointerDown = false;
    let settleTimer: ReturnType<typeof setTimeout> | null = null;

    const offerButton = () => {
      const { content: source, canEdit: editable } = latest.current;
      const selection = window.getSelection();
      // A collapsed or moved selection abandons the button.
      if (!editable || !selection || selection.rangeCount === 0 || selection.isCollapsed) {
        setFloating(null);
        return;
      }
      const range = selection.getRangeAt(0);
      if (!root.contains(range.commonAncestorContainer)) {
        setFloating(null);
        return;
      }
      const anchor = previewSelectionToSource(range, root, source);
      if (!anchor) {
        setFloating(null);
        return;
      }
      const rect = range.getClientRects()[0] ?? range.getBoundingClientRect();
      setFloating({ ...anchor, x: rect.left, y: rect.top - 6 });
    };

    const activateAtCaret = (target: EventTarget | null) => {
      const { content: source, comments: current, activeSelection: active } = latest.current;
      const selection = window.getSelection();
      const range = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
      if (target instanceof Element && target.closest("a, button, summary")) return;
      const offset =
        range && root.contains(range.startContainer)
          ? previewPointToSource(range.startContainer, range.startOffset, source)
          : null;
      const clicked =
        offset === null
          ? undefined
          : current.find((comment) => comment.start_index <= offset && offset < comment.end_index);
      if (clicked) {
        latest.current.onSetActiveSelection({
          start_index: clicked.start_index,
          end_index: clicked.end_index,
          anchor_content: clicked.anchor_content ?? "",
          comment_id: clicked.id,
        });
        return;
      }
      const hasDraft = active?.comment_id == null && !!pendingBodyRef?.current?.trim();
      if (active && !hasDraft) latest.current.onSetActiveSelection(null);
    };

    const handlePointerDown = () => {
      pointerDown = true;
    };
    const handleMouseUp = (event: MouseEvent) => {
      pointerDown = false;
      if (window.getSelection()?.isCollapsed) {
        activateAtCaret(event.target);
        return;
      }
      offerButton();
    };
    const handleSelectionChange = () => {
      if (settleTimer !== null) clearTimeout(settleTimer);
      settleTimer = setTimeout(() => {
        settleTimer = null;
        if (!pointerDown) offerButton();
      }, SELECTION_SETTLE_MS);
    };
    // A drag released outside the preview never reaches the root's mouseup.
    const handleDocumentPointerUp = () => {
      if (!pointerDown) return;
      pointerDown = false;
      offerButton();
    };

    root.addEventListener("mousedown", handlePointerDown);
    root.addEventListener("mouseup", handleMouseUp);
    document.addEventListener("mouseup", handleDocumentPointerUp);
    document.addEventListener("selectionchange", handleSelectionChange);
    return () => {
      if (settleTimer !== null) clearTimeout(settleTimer);
      root.removeEventListener("mousedown", handlePointerDown);
      root.removeEventListener("mouseup", handleMouseUp);
      document.removeEventListener("mouseup", handleDocumentPointerUp);
      document.removeEventListener("selectionchange", handleSelectionChange);
    };
  }, [rootRef, pendingBodyRef]);

  // The button sits at fixed viewport coordinates, so any outside press or
  // scroll would leave it pointing at the wrong text.
  useEffect(() => {
    if (!floating) return;
    const dismiss = (event: Event) => {
      if (event.target instanceof Element && event.target.closest("[data-add-comment-btn]")) return;
      setFloating(null);
    };
    document.addEventListener("mousedown", dismiss);
    window.addEventListener("scroll", dismiss, true);
    return () => {
      document.removeEventListener("mousedown", dismiss);
      window.removeEventListener("scroll", dismiss, true);
    };
  }, [floating]);

  useEffect(() => setFloating(null), [content]);

  // Paint saved comments, the active one, and an unsaved draft's range.
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const key = Symbol("markdown-preview");
    const activeId =
      activeSelection?.comment_id ??
      comments.find(
        (comment) =>
          comment.start_index === activeSelection?.start_index &&
          comment.end_index === activeSelection?.end_index,
      )?.id;
    const painted: PaintedRanges = { comment: [], active: [], pending: [] };
    const rangesFor = previewRangeLookup(root, content);
    for (const comment of comments) {
      const ranges = rangesFor(comment.start_index, comment.end_index);
      painted[comment.id === activeId ? "active" : "comment"].push(...ranges);
    }
    if (activeSelection && !activeId) {
      painted.pending.push(...rangesFor(activeSelection.start_index, activeSelection.end_index));
    }
    paintedByPreview.set(key, painted);
    repaint();
    return () => {
      paintedByPreview.delete(key);
      repaint();
    };
  }, [rootRef, content, comments, activeSelection]);

  // Bring a comment chosen in the panel into view.
  const activeCommentId = activeSelection?.comment_id;
  useEffect(() => {
    const root = rootRef.current;
    const { activeSelection: active } = latest.current;
    if (!root || !activeCommentId || !active) return;
    const [first] = previewRangesForSource(root, content, active.start_index, active.end_index);
    if (!first) return;
    // The comment's own rect: in a tall paragraph, the paragraph's could be in view without it.
    const box = root.getBoundingClientRect();
    const rect = first.getBoundingClientRect();
    if (rect.top >= box.top && rect.bottom <= box.bottom) return;
    root.scrollBy({
      top: rect.top - box.top - box.height / 2 + rect.height / 2,
      behavior: "smooth",
    });
  }, [rootRef, content, activeCommentId]);

  if (!floating || !canEdit) return null;
  return createPortal(
    <button
      data-add-comment-btn
      type="button"
      className={`fixed z-50 ${ADD_COMMENT_BUTTON_CLASS_NAME}`}
      style={{ left: floating.x, top: floating.y, transform: "translateY(-100%)" }}
      // Keep the selection while pressed so it doesn't abandon the button.
      onMouseDown={(event) => event.preventDefault()}
      onClick={() => {
        onSetActiveSelection({
          start_index: floating.start_index,
          end_index: floating.end_index,
          anchor_content: floating.anchor_content,
        });
        setFloating(null);
      }}
    >
      <MessageSquarePlusIcon className="size-3.5" />
      Add comment
    </button>,
    getEmbedRoot() ?? document.body,
  );
}
