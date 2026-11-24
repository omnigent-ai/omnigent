// What a reader can do with a text selection in a code surface: add a comment
// or attach the lines to the session's coding agent. The floating toolbar and
// the ⌘K commands share one definition and handler each.

import { AtSignIcon, MessageSquarePlusIcon } from "lucide-react";
import { useId, useMemo, type FocusEvent, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { getEmbedRoot } from "@/lib/host";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import { useChatStore } from "@/store/chatStore";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";
import { ADD_COMMENT_BUTTON_CLASS_NAME } from "./addCommentButtonStyles";

/** Viewport coordinates of the selection start the toolbar sits above. */
export interface SelectionActionsPosition {
  left: number;
  top: number;
}

export interface SelectionActionHandlers {
  onAddComment: () => void;
  onAttachToAgent: () => void;
}

const ADD_COMMENT = {
  label: "Add comment",
  icon: MessageSquarePlusIcon,
  keywords: ["comment", "selection", "note", "review", "annotate"],
};
const ATTACH_TO_AGENT = {
  label: "Attach to agent",
  icon: AtSignIcon,
  keywords: ["attach", "selection", "lines", "agent", "composer", "mention"],
};

const SELECTION_ACTIONS_ATTRIBUTE = "data-comment-selection-actions";

/** Whether `element` is inside a selection toolbar, as when focus moves from the editor to it. */
export function isInSelectionActions(element: Element | null): boolean {
  return !!element?.closest(`[${SELECTION_ACTIONS_ATTRIBUTE}]`);
}

/** Whether a selection in `path` can be attached to the session's coding agent. */
export function useCanAttachToAgent(path: string | undefined): boolean {
  const sessionHarness = useChatStore((state) => state.sessionHarness);
  return !!path && nativeCodingAgentForHarness(sessionHarness) !== undefined;
}

/** Tags a line span of `path` into the chat composer. */
export function attachLinesToAgent(path: string, lineRange: { start: number; end: number }): void {
  useChatStore.getState().addComposerAttachment({ path, isDir: false, lineRange });
}

const NO_COMMANDS: PaletteCommand[] = [];

/** Offers the selection actions in ⌘K while `available`, such as when the surface has a selection. */
export function useSelectionActionCommands({
  available,
  canAttachToAgent,
  onAddComment,
  onAttachToAgent,
}: SelectionActionHandlers & { available: boolean; canAttachToAgent: boolean }): void {
  const source = `comment-selection-${useId()}`;
  const commands = useMemo<PaletteCommand[]>(() => {
    if (!available) return NO_COMMANDS;
    const addComment: PaletteCommand = {
      id: "selection.add-comment",
      ...ADD_COMMENT,
      // The comments panel focuses its new comment's text box.
      movesFocus: true,
      run: onAddComment,
    };
    if (!canAttachToAgent) return [addComment];
    return [
      addComment,
      { id: "selection.attach-to-agent", ...ATTACH_TO_AGENT, run: onAttachToAgent },
    ];
  }, [available, canAttachToAgent, onAddComment, onAttachToAgent]);
  usePaletteCommands(source, "Selection", commands);
}

interface CommentSelectionActionsProps extends SelectionActionHandlers {
  position: SelectionActionsPosition;
  canAttachToAgent: boolean;
  /** Focus left the toolbar for `next`, which may be the editor. */
  onFocusLeave: (next: Element | null) => void;
  /** Escape in the toolbar returns focus to the editor with its selection. */
  onReturnFocus: () => void;
}

export function CommentSelectionActions({
  position,
  canAttachToAgent,
  onAddComment,
  onAttachToAgent,
  onFocusLeave,
  onReturnFocus,
}: CommentSelectionActionsProps) {
  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    const next = event.relatedTarget instanceof Element ? event.relatedTarget : null;
    if (!next || !event.currentTarget.contains(next)) onFocusLeave(next);
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    onReturnFocus();
  };
  const actions = [
    { ...ADD_COMMENT, testAttribute: { "data-add-comment-btn": "" }, run: onAddComment },
    ...(canAttachToAgent
      ? [
          {
            ...ATTACH_TO_AGENT,
            testAttribute: { "data-attach-agent-btn": "" },
            run: onAttachToAgent,
          },
        ]
      : []),
  ];
  // Rendered into document.body so ancestor CSS transforms don't break fixed positioning.
  return createPortal(
    <div
      role="toolbar"
      aria-label="Selection actions"
      {...{ [SELECTION_ACTIONS_ATTRIBUTE]: "" }}
      onBlur={handleBlur}
      onKeyDown={handleKeyDown}
      className="fixed z-50 flex items-center gap-1"
      style={{
        // Keep the one- or two-button group from clipping off the right edge.
        left: Math.min(
          position.left,
          Math.max(8, window.innerWidth - (canAttachToAgent ? 288 : 138)),
        ),
        top: position.top,
        transform: "translateY(-100%)",
      }}
    >
      {actions.map(({ label, icon: Icon, testAttribute, run }) => (
        <button
          key={label}
          {...testAttribute}
          type="button"
          // A mouse press keeps the editor focused, so its selection stays live.
          onMouseDown={(event) => event.preventDefault()}
          onClick={run}
          className={ADD_COMMENT_BUTTON_CLASS_NAME}
        >
          <Icon className="size-3.5" />
          {label}
        </button>
      ))}
    </div>,
    getEmbedRoot() ?? document.body,
  );
}
