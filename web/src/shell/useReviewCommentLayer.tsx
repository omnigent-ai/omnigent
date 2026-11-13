// The comment layer of the whole-file diff review: comment highlights, the
// Add comment and Attach to agent actions on a selection, and click-to-focus,
// with the same rules as the Monaco code surfaces.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReviewRevealTarget } from "@/components/diff-review/reviewNavigation";
import { positionAt } from "@/components/diff-review/textAnchors";
import type { ReviewTextEvent } from "@/components/diff-review/useReviewTextLayer";
import { useComments, type Comment } from "@/hooks/useComments";
import { useCanEdit } from "@/hooks/usePermissions";
import type { ActiveSelection } from "./codeViewerHelpers";
import {
  attachLinesToAgent,
  CommentSelectionActions,
  type SelectionActionsPosition,
  useCanAttachToAgent,
  useSelectionActionCommands,
} from "./CommentSelectionActions";
import {
  openCommentsByPath,
  reviewCommentHighlights,
  reviewCommentSelectionAt,
  selectedLineRange,
} from "./reviewComments";
import { useMarkdownCommentRemap } from "./useMarkdownCommentRemap";

/** What the file viewer shares with the review for commenting on its files. */
export interface ReviewCommentContext {
  /** The viewer's open comments for its current file. */
  currentComments: readonly Comment[];
  activeSelection: ActiveSelection | null;
  /** Make `selection` active in `path`, which becomes the viewer's current file. */
  onSetActiveSelection: (path: string, selection: ActiveSelection | null) => void;
  /** Whether a new comment may start in `path`, beyond the edit permission the layer checks. */
  canCommentOn: (path: string) => boolean;
  /** The in-progress comment body; clicking away keeps a draft's selection. */
  pendingBodyRef?: React.RefObject<string>;
  /**
   * False for a review that only starts comments elsewhere: it neither reads, highlights nor
   * opens the session's comments, and Add comment goes to `onAddComment`.
   */
  storedComments?: boolean;
  /** Where Add comment sends the selection, with the file text its offsets are in. */
  onAddComment?: (path: string, selection: ActiveSelection, text: string) => void;
}

/** A context that offers Add comment and Attach to agent, with no comments of its own shown. */
export function actionsOnlyCommentContext({
  canCommentOn,
  onAddComment,
}: Required<Pick<ReviewCommentContext, "canCommentOn" | "onAddComment">>): ReviewCommentContext {
  return {
    currentComments: [],
    activeSelection: null,
    onSetActiveSelection: () => {},
    canCommentOn,
    storedComments: false,
    onAddComment,
  };
}

interface PendingSelection {
  path: string;
  start: number;
  end: number;
  /** The file's text when it was selected; the offsets only mean something in it. */
  text: string;
  position: SelectionActionsPosition;
}

export function useReviewCommentLayer({
  conversationId,
  currentPath,
  context,
  currentLines,
}: {
  conversationId: string;
  currentPath: string;
  context: ReviewCommentContext | undefined;
  /** Each loaded file's current text, as lines. */
  currentLines: ReadonlyMap<string, readonly string[]>;
}) {
  const showsStored = context !== undefined && context.storedComments !== false;
  const commentsQuery = useComments(showsStored ? conversationId : undefined);
  // The same permission gate as the Monaco diff.
  const canEdit = useCanEdit(conversationId);
  const canEditRef = useRef(canEdit);
  canEditRef.current = canEdit;
  const currentComments = context?.currentComments;
  const activeSelection = context?.activeSelection ?? null;

  const sessionComments = useMemo(() => commentsQuery.data ?? [], [commentsQuery.data]);
  const remapFormatted = useMarkdownCommentRemap(sessionComments);
  const commentsByPath = useMemo(
    () =>
      openCommentsByPath({
        comments: sessionComments,
        currentPath,
        currentComments: currentComments ?? [],
        currentText: (path) => currentLines.get(path)?.join(""),
        remapFormatted,
      }),
    [sessionComments, currentPath, currentComments, currentLines, remapFormatted],
  );
  const highlights = useMemo(
    () =>
      showsStored ? reviewCommentHighlights(commentsByPath, currentPath, activeSelection) : [],
    [showsStored, commentsByPath, currentPath, activeSelection],
  );

  // The commentable selection, kept for ⌘K while its floating actions are hidden.
  const [pending, setPending] = useState<PendingSelection | null>(null);
  const [actionsShown, setActionsShown] = useState(false);
  const latest = useRef({ context, commentsByPath, currentPath, activeSelection, currentLines });
  latest.current = { context, commentsByPath, currentPath, activeSelection, currentLines };
  const clearPending = useCallback(() => {
    setPending(null);
    setActionsShown(false);
  }, []);

  const onTextEvent = useCallback(
    (event: ReviewTextEvent) => {
      const state = latest.current;
      if (!state.context) return;
      if (event.kind === "scroll") {
        setActionsShown(false);
        return;
      }
      if (event.kind === "clear") {
        clearPending();
        return;
      }
      if (event.kind === "select") {
        const text = state.currentLines.get(event.path)?.join("") ?? "";
        if (
          !event.position ||
          !text.slice(event.start, event.end).trim() ||
          !canEditRef.current ||
          !state.context.canCommentOn(event.path)
        ) {
          clearPending();
          return;
        }
        setPending({
          path: event.path,
          start: event.start,
          end: event.end,
          text,
          position: event.position,
        });
        setActionsShown(true);
        return;
      }
      clearPending();
      if (state.context.storedComments === false) return;
      if (event.path !== undefined && event.offset !== undefined) {
        const clicked = reviewCommentSelectionAt(
          state.commentsByPath.get(event.path),
          event.offset,
        );
        if (clicked) {
          state.context.onSetActiveSelection(event.path, clicked);
          return;
        }
      }
      // A click elsewhere clears the active comment unless a draft is in progress.
      if (state.activeSelection === null) return;
      if (state.context.pendingBodyRef?.current?.trim()) return;
      state.context.onSetActiveSelection(state.currentPath, null);
    },
    [clearPending],
  );

  // A refresh that changes the selected file's text makes the selection's offsets stale.
  const pendingCurrent = useMemo(
    () => pending !== null && currentLines.get(pending.path)?.join("") === pending.text,
    [pending, currentLines],
  );
  // Commenting can also stop being allowed while a selection is pending.
  const pendingAllowed =
    pending !== null && pendingCurrent && canEdit && (context?.canCommentOn(pending.path) ?? false);
  useEffect(() => {
    if (pending && !pendingAllowed) clearPending();
  }, [pending, pendingAllowed, clearPending]);

  const addComment = useCallback(() => {
    const state = latest.current;
    if (
      !pending ||
      !pendingAllowed ||
      !canEditRef.current ||
      !state.context?.canCommentOn(pending.path)
    ) {
      clearPending();
      return;
    }
    const selection = {
      start_index: pending.start,
      end_index: pending.end,
      anchor_content: pending.text.slice(pending.start, pending.end),
    };
    if (state.context.onAddComment)
      state.context.onAddComment(pending.path, selection, pending.text);
    else state.context.onSetActiveSelection(pending.path, selection);
    clearPending();
    document.getSelection()?.removeAllRanges();
  }, [pending, pendingAllowed, clearPending]);

  const attachToAgent = useCallback(() => {
    setActionsShown(false);
    if (!pending || !pendingAllowed) return;
    const lines = currentLines.get(pending.path);
    const lineRange = lines && selectedLineRange(lines, pending.start, pending.end);
    if (lineRange) attachLinesToAgent(pending.path, lineRange);
  }, [pending, pendingAllowed, currentLines]);

  const canAttachToAgent = useCanAttachToAgent(pending?.path ?? currentPath);
  useSelectionActionCommands({
    available: pendingAllowed,
    canAttachToAgent,
    onAddComment: addComment,
    onAttachToAgent: attachToAgent,
  });

  // A comment made active elsewhere, such as in the panel or by a link, is brought into view.
  const [revealTarget, setRevealTarget] = useState<ReviewRevealTarget | null>(null);
  const lines = currentLines.get(currentPath);
  const revealedSelection = useRef<ActiveSelection | null>(null);
  useEffect(() => {
    if (!showsStored) return;
    if (!activeSelection || revealedSelection.current === activeSelection || !lines) return;
    revealedSelection.current = activeSelection;
    const position = positionAt(lines, activeSelection.start_index);
    if (position) setRevealTarget({ path: currentPath, line: position.line, align: "nearest" });
  }, [showsStored, activeSelection, currentPath, lines]);

  const actions =
    pending && pendingAllowed && actionsShown ? (
      <CommentSelectionActions
        position={pending.position}
        canAttachToAgent={canAttachToAgent}
        onAddComment={addComment}
        onAttachToAgent={attachToAgent}
        // Leaving the toolbar hides it; the selection stays for ⌘K.
        onFocusLeave={() => setActionsShown(false)}
        onReturnFocus={() => setActionsShown(false)}
      />
    ) : null;

  return { highlights, onTextEvent, actions, revealTarget };
}
