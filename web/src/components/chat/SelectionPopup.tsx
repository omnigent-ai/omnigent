import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CornerUpLeftIcon, MessagesSquareIcon } from "lucide-react";
import { ALT_KEY, CompactShortcutKeys, ENTER_KEY, SHIFT_KEY } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import { getEmbedRoot } from "@/lib/host";
import { isFocusInDialogOrMenu, isKeyOwningOverlayOpen, isTextEntry } from "@/lib/hotkeys";
import { chordMatches } from "@/lib/shortcuts/chord";

function ownsSelectionHotkey(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (isTextEntry(target) ||
      target.closest("button, a[href], select, [role='button'], .xterm, .monaco-editor") !== null)
  );
}

export function SelectionPopup({
  containerRef,
  onReply,
  onAskInSideChat,
}: {
  containerRef: React.RefObject<HTMLElement | null>;
  onReply: (text: string) => void;
  // Present only when the session's harness supports side chat; renders the
  // "Ask in side chat" action beside Reply.
  onAskInSideChat?: (text: string) => void;
}) {
  const [popupPos, setPopupPos] = useState<{ x: number; y: number } | null>(null);
  const selectedTextRef = useRef("");

  const getScopedSelection = useCallback(() => {
    const selection = window.getSelection();
    const container = containerRef.current;
    if (
      !selection ||
      selection.isCollapsed ||
      selection.rangeCount === 0 ||
      !selection.toString().trim() ||
      !container ||
      !container.contains(selection.anchorNode) ||
      !container.contains(selection.focusNode)
    ) {
      return null;
    }
    return selection;
  }, [containerRef]);

  const applySelection = useCallback((action: (text: string) => void, text: string) => {
    window.getSelection()?.removeAllRanges();
    setPopupPos(null);
    selectedTextRef.current = "";
    action(text);
  }, []);

  const updatePopup = useCallback(() => {
    const selection = getScopedSelection();
    if (!selection) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    const range = selection.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    // Position the button just above the selection, horizontally centered.
    setPopupPos({
      x: rect.left + rect.width / 2,
      y: rect.top,
    });
    selectedTextRef.current = selection.toString().trim();
  }, [getScopedSelection]);

  useEffect(() => {
    document.addEventListener("mouseup", updatePopup);
    document.addEventListener("selectionchange", updatePopup);
    return () => {
      document.removeEventListener("mouseup", updatePopup);
      document.removeEventListener("selectionchange", updatePopup);
    };
  }, [updatePopup]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.isComposing) return;
      const action = chordMatches({ key: "Enter", alt: true }, event)
        ? onReply
        : chordMatches({ key: "Enter", alt: true, shift: true }, event)
          ? onAskInSideChat
          : undefined;
      if (
        !action ||
        ownsSelectionHotkey(event.target) ||
        ownsSelectionHotkey(document.activeElement) ||
        isFocusInDialogOrMenu() ||
        isKeyOwningOverlayOpen()
      ) {
        return;
      }
      const selection = getScopedSelection();
      if (!selection) return;
      event.preventDefault();
      event.stopPropagation();
      applySelection(action, selection.toString().trim());
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [applySelection, getScopedSelection, onReply, onAskInSideChat]);

  if (!popupPos) return null;

  // Panel containment changes the fixed-position origin; use the viewport.
  return createPortal(
    <div
      style={{
        position: "fixed",
        // Translate left by 50% to center the button over the midpoint of the
        // selection, and up by 100% + 6px to sit just above the selection rect.
        left: popupPos.x,
        top: popupPos.y,
        transform: "translate(-50%, calc(-100% - 6px))",
        zIndex: 50,
      }}
    >
      <Button
        type="button"
        aria-keyshortcuts="Alt+Enter"
        variant="secondary"
        size="sm"
        // Override shared-variant translucent hover — this button floats over text.
        className="gap-1 shadow-md hover:bg-secondary hover:brightness-95 dark:hover:brightness-110"
        onMouseDown={(e) => {
          // Prevent the mousedown from clearing the selection before we read it.
          e.preventDefault();
        }}
        onClick={() => {
          if (selectedTextRef.current) applySelection(onReply, selectedTextRef.current);
        }}
      >
        <CornerUpLeftIcon className="size-3.5" />
        Reply
        <CompactShortcutKeys keys={[ALT_KEY, ENTER_KEY]} />
      </Button>
      {onAskInSideChat ? (
        <Button
          type="button"
          aria-keyshortcuts="Alt+Shift+Enter"
          variant="secondary"
          size="sm"
          className="gap-1 shadow-md hover:bg-secondary hover:brightness-95 dark:hover:brightness-110"
          onMouseDown={(e) => {
            e.preventDefault();
          }}
          onClick={() => {
            if (selectedTextRef.current) applySelection(onAskInSideChat, selectedTextRef.current);
          }}
        >
          <MessagesSquareIcon className="size-3.5" />
          Ask in side chat
          <CompactShortcutKeys keys={[ALT_KEY, SHIFT_KEY, ENTER_KEY]} />
        </Button>
      ) : null}
    </div>,
    getEmbedRoot() ?? document.body,
  );
}
