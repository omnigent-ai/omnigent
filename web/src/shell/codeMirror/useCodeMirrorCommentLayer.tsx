// Comments in the CodeMirror viewer: highlights, a click on a comment to open
// it, revealing the active one, and the Add comment and Attach to agent buttons.

import type { Extension } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { type RefObject, useCallback, useEffect, useRef, useState } from "react";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "../codeViewerHelpers";
import { commentSelectionAt } from "../commentHighlights";
import {
  attachLinesToAgent,
  CommentSelectionActions,
  isInSelectionActions,
  type SelectionActionsPosition,
  useCanAttachToAgent,
  useSelectionActionCommands,
} from "../CommentSelectionActions";
import {
  commentHighlighting,
  positionOfRawOffset,
  rawOffsetAt,
  selectionAnchor,
  selectionLineRange,
  setCommentLayer,
} from "./commentHighlighting";

export interface CodeMirrorCommentOptions {
  comments: readonly Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (selection: ActiveSelection | null) => void;
  /** Whether a new comment may be started; saved comments stay highlighted either way. */
  canComment: boolean;
  /** An in-progress comment body; clicking away doesn't clear a draft. */
  pendingBodyRef?: RefObject<string>;
  /** Workspace-relative path, for attaching a selection to the session's coding agent. */
  path: string;
  /** Whether the viewer is on screen; a hidden one offers no ⌘K selection actions. */
  onScreen?: boolean;
}

interface CodeMirrorCommentLayer {
  /** Editor extensions; include them when creating the view. */
  extension: Extension;
  /** The floating selection buttons, or null while hidden. */
  actions: React.ReactNode;
}

/**
 * Whether `position` is on screen: inside the scroller and right of the gutter.
 * CodeMirror renders a margin past the screen, so a rendered line may be hidden.
 */
export function isPositionOnScreen(view: EditorView, position: number): boolean {
  const coordinates = view.coordsAtPos(position);
  if (!coordinates) return false;
  const screen = view.scrollDOM.getBoundingClientRect();
  const gutterRight = view.dom.querySelector(".cm-gutters")?.getBoundingClientRect().right;
  const left = Math.max(screen.left, gutterRight ?? screen.left);
  return (
    coordinates.top >= screen.top &&
    coordinates.bottom <= screen.bottom &&
    coordinates.left >= left &&
    coordinates.left <= screen.right
  );
}

/** Centers `position` when it is off screen, leaving the view alone otherwise. */
export function revealPosition(view: EditorView, position: number): void {
  if (isPositionOnScreen(view, position)) return;
  view.dispatch({ effects: EditorView.scrollIntoView(position, { y: "center", x: "nearest" }) });
}

export function useCodeMirrorCommentLayer(
  view: EditorView | null,
  options: CodeMirrorCommentOptions,
): CodeMirrorCommentLayer {
  const { comments, activeSelection, canComment, path, onScreen = true } = options;
  const canAttachToAgent = useCanAttachToAgent(path);
  const [buttonPosition, setButtonPosition] = useState<SelectionActionsPosition | null>(null);
  // Whether the selection has text to comment on, focused or not, for ⌘K.
  const [hasSelectionText, setHasSelectionText] = useState(false);
  // The extension is built once; its handlers read the latest options here.
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const [extension] = useState<Extension>(() => {
    const placeButton = (target: EditorView) => {
      // Focus moving onto the toolbar itself keeps it up.
      const focused = target.hasFocus || isInSelectionActions(document.activeElement);
      if (!optionsRef.current.canComment || !focused || !selectionAnchor(target.state)) {
        setButtonPosition(null);
        return;
      }
      const from = target.state.selection.main.from;
      target.requestMeasure({
        read: (measured) => measured.coordsAtPos(from),
        write: (coordinates) =>
          setButtonPosition(coordinates ? { left: coordinates.left, top: coordinates.top } : null),
      });
    };
    return [
      commentHighlighting(() => ({
        comments: optionsRef.current.comments,
        activeSelection: optionsRef.current.activeSelection,
      })),
      EditorView.updateListener.of((update) => {
        if (update.selectionSet || update.docChanged) {
          setHasSelectionText(selectionAnchor(update.state) !== null);
        }
        if (update.selectionSet || update.focusChanged) placeButton(update.view);
      }),
      EditorView.domEventHandlers({
        // The button is placed in viewport coordinates; hide it rather than chase a scroll.
        scroll: () => setButtonPosition(null),
        mouseup: (event, target) => {
          if (!target.state.selection.main.empty) return false;
          const current = optionsRef.current;
          const position =
            target.posAtCoords({ x: event.clientX, y: event.clientY }) ??
            target.state.selection.main.head;
          const clicked = commentSelectionAt(current.comments, rawOffsetAt(target.state, position));
          if (clicked) current.onSetActiveSelection(clicked);
          else if (current.activeSelection && !current.pendingBodyRef?.current?.trim()) {
            current.onSetActiveSelection(null);
          }
          return false;
        },
      }),
    ];
  });

  useEffect(() => {
    view?.dispatch({ effects: setCommentLayer.of({ comments, activeSelection }) });
  }, [view, comments, activeSelection]);

  // Bring the active comment into view when it is chosen elsewhere, such as the comments panel.
  useEffect(() => {
    if (!view || !activeSelection) return;
    revealPosition(view, positionOfRawOffset(view.state, activeSelection.start_index));
  }, [view, activeSelection]);

  useEffect(() => {
    if (!canComment) setButtonPosition(null);
  }, [canComment]);

  const addComment = useCallback(() => {
    const anchor = view && optionsRef.current.canComment ? selectionAnchor(view.state) : null;
    if (anchor) optionsRef.current.onSetActiveSelection(anchor);
    setButtonPosition(null);
  }, [view]);
  const attachToAgent = useCallback(() => {
    const lineRange = view ? selectionLineRange(view.state) : null;
    if (lineRange) attachLinesToAgent(optionsRef.current.path, lineRange);
    setButtonPosition(null);
    view?.focus();
  }, [view]);
  useSelectionActionCommands({
    available: onScreen && canComment && hasSelectionText && view !== null,
    canAttachToAgent,
    onAddComment: addComment,
    onAttachToAgent: attachToAgent,
  });

  return {
    extension,
    actions: buttonPosition && (
      <CommentSelectionActions
        position={buttonPosition}
        canAttachToAgent={canAttachToAgent}
        onAddComment={addComment}
        onAttachToAgent={attachToAgent}
        // Returning to the editor re-places the toolbar; anywhere else hides it.
        onFocusLeave={(next) => {
          if (!next || !view?.dom.contains(next)) setButtonPosition(null);
        }}
        onReturnFocus={() => view?.focus()}
      />
    ),
  };
}
