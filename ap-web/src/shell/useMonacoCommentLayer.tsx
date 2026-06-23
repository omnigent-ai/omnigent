// Shared comment-interaction layer for any Monaco code editor surface.
//
// Used by both MonacoCodeEditor (the file editor's modified buffer) and
// MonacoDiffViewer (the diff's modified side). Given an editor instance, it:
//   • renders existing comments as inline decorations (+ the active one stronger),
//   • shows a floating "Add comment" button on a non-empty selection,
//   • navigates to a comment when its highlight is clicked,
//   • reveals the active comment/selection when it changes elsewhere.
//
// Comments anchor by absolute character offset; Monaco's getOffsetAt /
// getPositionAt bridge those to editor positions. The hook returns the floating
// button as a portal node for the caller to render.

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AtSignIcon, MessageSquarePlusIcon } from "lucide-react";
import type { OnMount } from "@monaco-editor/react";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";
import { getEmbedRoot } from "@/lib/host";
import { useChatStore } from "@/store/chatStore";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";

// The IStandaloneCodeEditor instance, derived from the onMount signature so we
// don't deep-import Monaco's types here. The diff editor's getModifiedEditor()
// returns the same type, so both surfaces share this layer.
export type CodeEditorInstance = Parameters<OnMount>[0];
type TextModel = NonNullable<ReturnType<CodeEditorInstance["getModel"]>>;
type ModelDeltaDecoration = NonNullable<
  Parameters<CodeEditorInstance["createDecorationsCollection"]>[0]
>[number];
type DecorationsCollection = ReturnType<CodeEditorInstance["createDecorationsCollection"]>;

/**
 * Build Monaco decorations for the saved comments plus the active (possibly
 * not-yet-saved) selection. Offsets are mapped to positions via the model so
 * highlighting is character-precise.
 *
 * @param model The editor's text model.
 * @param comments Saved comments anchored by absolute char offset.
 * @param activeSelection The currently focused selection/comment, or null.
 * @returns Delta decorations to hand to a decorations collection.
 */
export function buildCommentDecorations(
  model: TextModel,
  comments: Comment[],
  activeSelection: ActiveSelection | null,
): ModelDeltaDecoration[] {
  const rangeOf = (start: number, end: number) => {
    const s = model.getPositionAt(start);
    const e = model.getPositionAt(end);
    return {
      startLineNumber: s.lineNumber,
      startColumn: s.column,
      endLineNumber: e.lineNumber,
      endColumn: e.column,
    };
  };
  const decorations: ModelDeltaDecoration[] = comments.map((c) => {
    const isActive =
      activeSelection != null &&
      activeSelection.start_index === c.start_index &&
      activeSelection.end_index === c.end_index;
    return {
      range: rangeOf(c.start_index, c.end_index),
      options: { inlineClassName: isActive ? "oa-comment-active" : "oa-comment" },
    };
  });
  // A selection the user just made (before it's saved as a comment) gets the
  // active highlight too, matching the Shiki viewer.
  if (
    activeSelection != null &&
    activeSelection.end_index > activeSelection.start_index &&
    !comments.some(
      (c) =>
        c.start_index === activeSelection.start_index && c.end_index === activeSelection.end_index,
    )
  ) {
    decorations.push({
      range: rangeOf(activeSelection.start_index, activeSelection.end_index),
      options: { inlineClassName: "oa-comment-active" },
    });
  }
  return decorations;
}

interface UseMonacoCommentLayerOptions {
  /** The live code editor (the modified buffer for diffs); null until mounted. */
  editorRef: React.RefObject<CodeEditorInstance | null>;
  /** True once `editorRef.current` is set — gates listener/decoration wiring. */
  mounted: boolean;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  /**
   * Whether a new comment may be started right now (e.g. `canEdit && !isDirty`
   * for the editor, `canEdit` for the read-only diff). Controls the floating
   * "Add comment" button; existing comments stay highlighted/navigable either way.
   */
  canComment: boolean;
  /** In-progress comment body; clicking away won't clear an active draft. */
  pendingBodyRef?: React.RefObject<string>;
  /**
   * Workspace-relative path of the file being viewed. When set on a native
   * coding-agent session, an "Attach to agent" button appears beside "Add
   * comment" that tags the selected line span into the chat composer.
   */
  path?: string;
}

/**
 * Wire the comment layer onto a Monaco editor instance.
 *
 * @param opts See {@link UseMonacoCommentLayerOptions}.
 * @returns The floating "Add comment" button portal (or null when hidden).
 */
export function useMonacoCommentLayer({
  editorRef,
  mounted,
  comments,
  activeSelection,
  onSetActiveSelection,
  canComment,
  pendingBodyRef,
  path,
}: UseMonacoCommentLayerOptions): React.ReactNode {
  const sessionHarness = useChatStore((s) => s.sessionHarness);
  const canAttachToAgent = !!path && nativeCodingAgentForHarness(sessionHarness) !== undefined;
  const decorationsRef = useRef<DecorationsCollection | null>(null);
  // Floating "Add comment" button position in viewport coords, or null.
  const [buttonPos, setButtonPos] = useState<{ left: number; top: number } | null>(null);

  // Stable refs so the once-registered Monaco listeners read live values.
  const commentsRef = useRef(comments);
  commentsRef.current = comments;
  const activeSelectionRef = useRef(activeSelection);
  activeSelectionRef.current = activeSelection;
  const onSetActiveSelectionRef = useRef(onSetActiveSelection);
  onSetActiveSelectionRef.current = onSetActiveSelection;
  const canCommentRef = useRef(canComment);
  canCommentRef.current = canComment;
  const pendingBodyRefRef = useRef(pendingBodyRef);
  pendingBodyRefRef.current = pendingBodyRef;

  // Recompute comment + active-selection decorations from current offsets.
  const applyDecorations = useCallback(() => {
    const ed = editorRef.current;
    const model = ed?.getModel();
    if (!ed || !model) return;
    const decorations = buildCommentDecorations(
      model,
      commentsRef.current,
      activeSelectionRef.current,
    );
    if (decorationsRef.current) decorationsRef.current.set(decorations);
    else decorationsRef.current = ed.createDecorationsCollection(decorations);
  }, [editorRef]);

  // Reposition / show-hide the floating "Add comment" button for the current
  // selection. Hidden unless a comment may be started (canComment).
  const updateCommentButton = useCallback(() => {
    const ed = editorRef.current;
    if (!ed || !canCommentRef.current) {
      setButtonPos(null);
      return;
    }
    const sel = ed.getSelection();
    const model = ed.getModel();
    if (!sel || !model || sel.isEmpty() || !model.getValueInRange(sel).trim()) {
      setButtonPos(null);
      return;
    }
    const visible = ed.getScrolledVisiblePosition(sel.getStartPosition());
    const node = ed.getDomNode();
    if (!visible || !node) {
      setButtonPos(null);
      return;
    }
    const rect = node.getBoundingClientRect();
    setButtonPos({ left: rect.left + visible.left, top: rect.top + visible.top });
  }, [editorRef]);

  // Register comment interactions once the editor exists; dispose on unmount.
  useEffect(() => {
    const ed = editorRef.current;
    if (!mounted || !ed) return;
    const disposables = [
      ed.onDidChangeCursorSelection(updateCommentButton),
      // Position is relative to the viewport; hide on scroll rather than chase it.
      ed.onDidScrollChange(() => setButtonPos(null)),
      // Hide on blur, immediately. The Add-comment button runs its action on
      // mousedown with preventDefault (which keeps the editor focused, so
      // clicking it doesn't blur) — no delay is needed, and skipping the timer
      // avoids a state update firing after unmount.
      ed.onDidBlurEditorWidget(() => setButtonPos(null)),
      ed.onMouseUp((e) => {
        const model = ed.getModel();
        if (!model) return;
        const sel = ed.getSelection();
        // A real selection is the "add comment" path, handled by the button.
        if (sel && !sel.isEmpty()) return;
        const pos = e.target.position;
        if (pos) {
          const offset = model.getOffsetAt(pos);
          const clicked = commentsRef.current.find(
            (c) => c.start_index <= offset && offset < c.end_index,
          );
          if (clicked) {
            onSetActiveSelectionRef.current({
              start_index: clicked.start_index,
              end_index: clicked.end_index,
              anchor_content: clicked.anchor_content ?? "",
            });
            return;
          }
        }
        // Clicked outside any comment → clear, unless a draft is in progress.
        if (activeSelectionRef.current === null) return;
        if (pendingBodyRefRef.current?.current?.trim()) return;
        onSetActiveSelectionRef.current(null);
      }),
    ];
    return () => {
      for (const d of disposables) d.dispose();
    };
  }, [mounted, editorRef, updateCommentButton]);

  // (Re)apply decorations when comments / active selection change.
  useEffect(() => {
    if (!mounted) return;
    applyDecorations();
  }, [mounted, comments, activeSelection, applyDecorations]);

  // Scroll the active comment/selection into view (e.g. clicked in the panel).
  useEffect(() => {
    const ed = editorRef.current;
    const model = ed?.getModel();
    if (!mounted || !ed || !model || !activeSelection) return;
    const s = model.getPositionAt(activeSelection.start_index);
    const e = model.getPositionAt(activeSelection.end_index);
    ed.revealRangeInCenterIfOutsideViewport({
      startLineNumber: s.lineNumber,
      startColumn: s.column,
      endLineNumber: e.lineNumber,
      endColumn: e.column,
    });
  }, [mounted, editorRef, activeSelection]);

  // Hide the button if commenting becomes unavailable while it's showing
  // (permission/truncation flip, or the buffer became dirty) — selection-change
  // events alone may not fire to clear it.
  useEffect(() => {
    if (!canComment) setButtonPos(null);
  }, [canComment]);

  // Drop the decorations collection on unmount.
  useEffect(
    () => () => {
      decorationsRef.current?.clear();
    },
    [],
  );

  // Create a comment from the current selection.
  const handleAddComment = useCallback(() => {
    const ed = editorRef.current;
    const model = ed?.getModel();
    if (!ed || !model) return;
    // Re-check: commenting may have become unavailable (permission / dirty /
    // truncation) between the button appearing and this click — don't anchor a
    // comment to offsets that no longer match the saved server content.
    if (!canCommentRef.current) {
      setButtonPos(null);
      return;
    }
    const sel = ed.getSelection();
    if (!sel || sel.isEmpty()) return;
    onSetActiveSelectionRef.current({
      start_index: model.getOffsetAt(sel.getStartPosition()),
      end_index: model.getOffsetAt(sel.getEndPosition()),
      anchor_content: model.getValueInRange(sel),
    });
    setButtonPos(null);
  }, [editorRef]);

  // Tag the selected line span into the chat composer. Monaco gives line
  // numbers directly; a selection ending at column 1 of a line means the last
  // line's content isn't included, so step back a line in that case.
  const handleAttachToAgent = useCallback(() => {
    const ed = editorRef.current;
    const sel = ed?.getSelection();
    if (!ed || !sel || sel.isEmpty() || !path) return;
    const start = sel.getStartPosition();
    const end = sel.getEndPosition();
    const endLine = end.column === 1 && end.lineNumber > start.lineNumber ? end.lineNumber - 1 : end.lineNumber;
    useChatStore.getState().addComposerAttachment({
      path,
      isDir: false,
      lineRange: { start: start.lineNumber, end: endLine },
    });
    setButtonPos(null);
  }, [editorRef, path]);

  if (!buttonPos) return null;
  // Rendered into document.body so ancestor CSS transforms don't break fixed
  // positioning.
  return createPortal(
    <div
      className="fixed z-50 flex items-center gap-1"
      style={{
        // Clamp so the (one- or two-) button group can't clip off the right
        // edge near the viewport boundary (mirrors CodeViewer's clamp).
        left: Math.min(
          buttonPos.left,
          Math.max(8, window.innerWidth - (canAttachToAgent ? 288 : 138)),
        ),
        top: buttonPos.top,
        transform: "translateY(-100%)",
      }}
    >
      <button
        data-add-comment-btn
        type="button"
        // preventDefault on mousedown keeps the editor selection live so
        // handleAddComment can read it.
        onMouseDown={(e) => {
          e.preventDefault();
          handleAddComment();
        }}
        className="flex items-center gap-1.5 rounded-md border border-border bg-popover backdrop-blur-xl backdrop-saturate-150 px-2.5 py-1 text-xs font-medium text-foreground shadow-md hover:bg-secondary transition-colors"
      >
        <MessageSquarePlusIcon className="size-3.5" />
        Add comment
      </button>
      {canAttachToAgent && (
        <button
          data-attach-agent-btn
          type="button"
          onMouseDown={(e) => {
            e.preventDefault();
            handleAttachToAgent();
          }}
          className="flex items-center gap-1.5 rounded-md border border-border bg-popover backdrop-blur-xl backdrop-saturate-150 px-2.5 py-1 text-xs font-medium text-foreground shadow-md hover:bg-secondary transition-colors"
        >
          <AtSignIcon className="size-3.5" />
          Attach to agent
        </button>
      )}
    </div>,
    getEmbedRoot() ?? document.body,
  );
}
