// Comments on the rendered Markdown preview: highlights saved comments, selects
// one on click, and offers "Add comment" for a text selection. Offsets come from
// markdownPreviewSource, so the same comment shows in Preview, Edit and Source.
import { type RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MessageSquarePlusIcon } from "lucide-react";
import { toast } from "sonner";
import type { Comment } from "@/hooks/useComments";
import { getEmbedRoot } from "@/lib/host";
import { ADD_COMMENT_BUTTON_CLASS_NAME } from "./addCommentButtonStyles";
import type { ActiveSelection } from "./codeViewerHelpers";
import {
  previewCaretToSource,
  previewSelectionToSource,
  type SourceSelection,
  sourceRangeToPreviewRanges,
} from "./markdownPreviewSource";
import { highlightsSupported } from "./previewSearch";

const HIGHLIGHT_COMMENTS = "md-preview-comment";
const HIGHLIGHT_ACTIVE = "md-preview-comment-active";
export const UNMAPPABLE_SELECTION_HINT =
  "That part of the preview can't take a comment. Select nearby text, or switch to Source.";

function paintHighlights(
  root: HTMLElement,
  content: string,
  comments: readonly Comment[],
  active: ActiveSelection | null,
): Range | null {
  if (!highlightsSupported()) return null;
  const all = comments.flatMap((comment) =>
    sourceRangeToPreviewRanges(root, content, comment.start_index, comment.end_index),
  );
  const activeRanges = active
    ? sourceRangeToPreviewRanges(root, content, active.start_index, active.end_index)
    : [];
  CSS.highlights.set(HIGHLIGHT_COMMENTS, new Highlight(...all));
  CSS.highlights.set(HIGHLIGHT_ACTIVE, new Highlight(...activeRanges));
  return activeRanges[0] ?? null;
}

function clearHighlights() {
  if (!highlightsSupported()) return;
  CSS.highlights.delete(HIGHLIGHT_COMMENTS);
  CSS.highlights.delete(HIGHLIGHT_ACTIVE);
}

export function useMarkdownPreviewComments({
  rootRef,
  content,
  enabled,
  canEdit,
  comments,
  activeSelection,
  onSetActiveSelection,
}: {
  rootRef: RefObject<HTMLElement | null>;
  content: string;
  /** False when the preview can't be mapped to source (comments stay in Edit and Source). */
  enabled: boolean;
  canEdit: boolean;
  comments: readonly Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection?: (selection: ActiveSelection | null) => void;
}) {
  const [floating, setFloating] = useState<{
    x: number;
    y: number;
    selection: SourceSelection;
  } | null>(null);
  const latest = useRef({ comments, canEdit, content, onSetActiveSelection });
  useEffect(() => {
    latest.current = { comments, canEdit, content, onSetActiveSelection };
  });

  // Paint after every render that can move text: content, comments or selection.
  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!enabled || !root) return;
    const first = paintHighlights(root, content, comments, activeSelection);
    // Bring a comment picked from the panel into view, without jumping when it's visible.
    if (first && activeSelection?.comment_id) {
      const rect = first.getBoundingClientRect();
      const box = root.getBoundingClientRect();
      if (rect.top < box.top || rect.bottom > box.bottom) {
        first.startContainer.parentElement?.scrollIntoView({ block: "center" });
      }
    }
  }, [rootRef, enabled, content, comments, activeSelection]);
  useEffect(() => clearHighlights, []);

  useEffect(() => {
    const root = rootRef.current;
    if (!enabled || !root) return;
    const handleMouseUp = () => {
      const current = latest.current;
      const select = current.onSetActiveSelection;
      if (!select) return;
      const selection = window.getSelection();
      if (!selection || selection.rangeCount === 0) return;
      const range = selection.getRangeAt(0);
      if (!root.contains(range.commonAncestorContainer)) return;

      if (selection.isCollapsed) {
        const offset = previewCaretToSource(
          range.startContainer,
          range.startOffset,
          root,
          current.content,
        );
        const clicked =
          offset === null
            ? undefined
            : current.comments.find((c) => c.start_index <= offset && offset < c.end_index);
        select(
          clicked
            ? {
                start_index: clicked.start_index,
                end_index: clicked.end_index,
                anchor_content: clicked.anchor_content ?? "",
                comment_id: clicked.id,
              }
            : null,
        );
        setFloating(null);
        return;
      }

      if (!current.canEdit || !selection.toString().trim()) return;
      const mapped = previewSelectionToSource(range, root, current.content);
      if (!mapped) {
        toast.info(UNMAPPABLE_SELECTION_HINT);
        return;
      }
      const existing = current.comments.find(
        (c) =>
          c.status === "draft" &&
          c.start_index === mapped.start_index &&
          c.end_index === mapped.end_index,
      );
      if (existing) {
        select({ ...mapped, comment_id: existing.id });
        setFloating(null);
        return;
      }
      // jsdom has no Range geometry; fall back to the selected text's element.
      const rect =
        typeof range.getClientRects === "function"
          ? (range.getClientRects()[0] ?? range.getBoundingClientRect())
          : (range.startContainer.parentElement ?? root).getBoundingClientRect();
      setFloating({ x: rect.left, y: rect.top - 6, selection: mapped });
    };
    root.addEventListener("mouseup", handleMouseUp);
    return () => root.removeEventListener("mouseup", handleMouseUp);
  }, [rootRef, enabled]);

  // The button belongs to one selection: drop it on any other press or scroll.
  useEffect(() => {
    if (!floating) return;
    const dismiss = (event: Event) => {
      if (!(event.target as Element | null)?.closest?.("[data-add-comment-btn]")) setFloating(null);
    };
    const onScroll = () => setFloating(null);
    document.addEventListener("mousedown", dismiss);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", dismiss);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [floating]);

  if (!floating || !onSetActiveSelection) return null;
  return createPortal(
    <button
      data-add-comment-btn
      type="button"
      className={`fixed z-50 ${ADD_COMMENT_BUTTON_CLASS_NAME}`}
      style={{ left: floating.x, top: floating.y, transform: "translateY(-100%)" }}
      onMouseDown={(event) => event.preventDefault()}
      onClick={() => {
        onSetActiveSelection(floating.selection);
        window.getSelection()?.removeAllRanges();
        setFloating(null);
      }}
    >
      <MessageSquarePlusIcon className="size-3.5" />
      Add comment
    </button>,
    getEmbedRoot() ?? document.body,
  );
}
