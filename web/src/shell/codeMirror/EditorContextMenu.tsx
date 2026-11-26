// The file editor's right-click menu, on the app's context menu. It lists the
// editor's shared commands with their ⌘K labels and shortcuts; Shift+F10 opens
// it at the cursor, and closing it returns to the editor.

import { EditorSelection } from "@codemirror/state";
import type { EditorView } from "@codemirror/view";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { MenuShortcut } from "@/components/ui/menu-item";
import { readClipboardText } from "@/lib/clipboard";
import { isMacPlatform } from "@/lib/hotkeys";
import { isElectronShell } from "@/lib/nativeBridge";
import { requestTextSearch, useTextSearchAvailable } from "@/store/fileFinder";
import { EDITOR_COMMAND_RUNS } from "./codeMirrorEditorCommands";
import { pinnedLineAt, revealStickyLine } from "./codeMirrorStickyScroll";
import {
  type EditorMenuAction,
  type EditorMenuEntry,
  editorMenuEntries,
  selectionSearchQuery,
} from "./editorContextMenuEntries";
import type { CodeMirrorSelectionActions } from "./useCodeMirrorCommentLayer";

/** Choices that focus a field or dialog of their own, so the menu leaves focus there. */
const MOVES_FOCUS: ReadonlySet<EditorMenuAction> = new Set([
  "search-workspace",
  "add-comment",
  "find",
  "find-and-replace",
  "go-to-line",
]);

export const PASTE_FAILED_MESSAGE = "Couldn't read the clipboard.";
export const COPY_FAILED_MESSAGE = "Couldn't copy the selection.";
export const CUT_FAILED_MESSAGE = "Couldn't cut the selection.";

export interface EditorContextMenuProps {
  view: EditorView | null;
  conversationId: string;
  editable: boolean;
  selectionActions: CodeMirrorSelectionActions;
  onFind: (view: EditorView) => void;
  onFindAndReplace: (view: EditorView) => void;
  onGoToLine: (view: EditorView) => void;
}

interface OpenMenu {
  entries: EditorMenuEntry[];
  /** The selection's search query, read as the menu opened. */
  query: string | null;
}

/** Whether `position` falls inside one of the selection's non-empty ranges. */
export function isInsideSelection(selection: EditorSelection, position: number): boolean {
  return selection.ranges.some(
    (range) => !range.empty && range.from <= position && position <= range.to,
  );
}

/** Puts the cursor where a right-click landed, unless it landed on the selection. */
export function placeCursorForMenu(view: EditorView, position: number | null): void {
  if (position === null || isInsideSelection(view.state.selection, position)) return;
  view.dispatch({ selection: EditorSelection.cursor(position), userEvent: "select.pointer" });
}

function canReadClipboard(): boolean {
  // The desktop page may not read the clipboard; its menu points to ⌘V instead.
  return !isElectronShell() && typeof navigator.clipboard?.readText === "function";
}

/** Whether `view` still takes edits: mounted and not read-only, which can change at any time. */
export function acceptsEdits(view: EditorView): boolean {
  return view.dom.isConnected && !view.state.readOnly;
}

async function pasteInto(view: EditorView): Promise<void> {
  const text = await readClipboardText();
  // The file may have turned read-only, or closed, while the clipboard was read.
  if (!acceptsEdits(view)) return;
  if (text === null) {
    toast.error(PASTE_FAILED_MESSAGE);
    return;
  }
  view.dispatch(view.state.replaceSelection(text), {
    scrollIntoView: true,
    userEvent: "input.paste",
  });
}

export function EditorContextMenu({
  view,
  conversationId,
  editable,
  selectionActions,
  onFind,
  onFindAndReplace,
  onGoToLine,
}: EditorContextMenuProps) {
  const [menu, setMenu] = useState<OpenMenu | null>(null);
  const triggerRef = useRef<HTMLSpanElement>(null);
  // Set when the menu closes by a click elsewhere or a choice that focuses something else.
  const leavesFocus = useRef(false);
  const canSearch = useTextSearchAvailable(conversationId);
  // The listeners are added once per view; they read the latest props here.
  const latest = useRef({ editable, selectionActions, canSearch });
  latest.current = { editable, selectionActions, canSearch };

  useEffect(() => {
    if (!view) return;
    const open = (x: number, y: number) => {
      const { state } = view;
      const main = state.selection.main;
      const query = selectionSearchQuery(state);
      const current = latest.current;
      leavesFocus.current = false;
      setMenu({
        query,
        entries: editorMenuEntries({
          editable: current.editable,
          hasSelection: !main.empty,
          // Copy and Cut take every range, so a column with an empty main range still has text.
          hasSelectedText: state.selection.ranges.some((range) => !range.empty),
          canSearchWorkspace: current.canSearch && query !== null,
          canComment: current.selectionActions.canComment,
          canAttachToAgent: current.selectionActions.canAttachToAgent,
          canReadClipboard: canReadClipboard(),
          mac: isMacPlatform(),
        }),
      });
      // The menu opens at a point of the editor's choosing, through a trigger outside the text.
      triggerRef.current?.dispatchEvent(
        new MouseEvent("contextmenu", {
          bubbles: true,
          cancelable: true,
          button: 2,
          clientX: x,
          clientY: y,
        }),
      );
    };
    // Only the text and its gutters: the find bar's field keeps its own menu.
    const onContextMenu = (event: MouseEvent) => {
      if (event.defaultPrevented) return;
      event.preventDefault();
      view.focus();
      // A pinned sticky-scroll row stands for its own line, as a left click on it does; the
      // text under it is hidden.
      const pinned = pinnedLineAt(view, event.target);
      if (pinned) revealStickyLine(view, pinned.line, pinned.depth);
      else placeCursorForMenu(view, view.posAtCoords({ x: event.clientX, y: event.clientY }));
      open(event.clientX, event.clientY);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      const keyboardMenu =
        event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey && !event.metaKey);
      if (!keyboardMenu) return;
      event.preventDefault();
      const caret = view.coordsAtPos(view.state.selection.main.head);
      const box = view.contentDOM.getBoundingClientRect();
      open(caret?.left ?? box.left, caret?.bottom ?? box.top);
    };
    view.scrollDOM.addEventListener("contextmenu", onContextMenu);
    view.contentDOM.addEventListener("keydown", onKeyDown);
    return () => {
      view.scrollDOM.removeEventListener("contextmenu", onContextMenu);
      view.contentDOM.removeEventListener("keydown", onKeyDown);
    };
  }, [view]);

  const run = (action: EditorMenuAction, query: string | null) => {
    if (!view) return;
    leavesFocus.current = MOVES_FOCUS.has(action);
    switch (action) {
      case "search-workspace":
        if (query !== null) requestTextSearch(conversationId, { query });
        return;
      case "add-comment":
        selectionActions.onAddComment();
        return;
      case "attach-to-agent":
        selectionActions.onAttachToAgent();
        return;
      case "cut":
      case "copy":
        if (action === "cut" && !acceptsEdits(view)) return;
        // The editor's own copy handler writes the file's raw text.
        view.focus();
        if (!document.execCommand(action)) {
          toast.error(action === "cut" ? CUT_FAILED_MESSAGE : COPY_FAILED_MESSAGE);
        }
        return;
      case "paste":
        if (!acceptsEdits(view)) return;
        view.focus();
        void pasteInto(view);
        return;
      case "find":
        onFind(view);
        return;
      case "find-and-replace":
        onFindAndReplace(view);
        return;
      case "go-to-line":
        onGoToLine(view);
        return;
      default:
        view.focus();
        EDITOR_COMMAND_RUNS[action](view);
    }
  };

  return (
    <ContextMenu
      modal={false}
      onOpenChange={(open) => {
        if (!open) setMenu(null);
      }}
    >
      <ContextMenuTrigger asChild>
        <span ref={triggerRef} aria-hidden className="pointer-events-none fixed size-0" />
      </ContextMenuTrigger>
      {menu && (
        <ContextMenuContent
          data-testid="file-editor-context-menu"
          aria-label="Editor actions"
          onPointerDownOutside={() => {
            leavesFocus.current = true;
          }}
          onCloseAutoFocus={(event) => {
            // Back to the editor, unless the reader clicked elsewhere or the choice focused something.
            event.preventDefault();
            if (!leavesFocus.current) view?.focus();
          }}
        >
          {menu.entries.map((entry) =>
            entry.kind === "separator" ? (
              <ContextMenuSeparator key={entry.key} />
            ) : (
              <ContextMenuItem
                key={entry.action}
                disabled={!entry.enabled}
                shortcut={entry.shortcut}
                description={entry.description}
                onSelect={() => run(entry.action, menu.query)}
              >
                {entry.label}
                {entry.keys && <MenuShortcut keys={entry.keys} />}
              </ContextMenuItem>
            ),
          )}
        </ContextMenuContent>
      )}
    </ContextMenu>
  );
}
