import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CornerUpLeftIcon, MessageSquarePlusIcon, MessagesSquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { getEmbedRoot } from "@/lib/host";

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
  const selectedTextRef = useRef<string>("");

  const updatePopup = useCallback(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    const text = sel.toString().trim();
    if (!text) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    // Scope to the conversation container — ignore selections in the composer.
    const container = containerRef.current;
    if (!container) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }
    const anchor = sel.anchorNode;
    if (!anchor || !container.contains(anchor)) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    const range = sel.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    // Position the button just above the selection, horizontally centered.
    setPopupPos({
      x: rect.left + rect.width / 2,
      y: rect.top,
    });
    selectedTextRef.current = text;
  }, [containerRef]);

  useEffect(() => {
    document.addEventListener("mouseup", updatePopup);
    document.addEventListener("selectionchange", updatePopup);
    return () => {
      document.removeEventListener("mouseup", updatePopup);
      document.removeEventListener("selectionchange", updatePopup);
    };
  }, [updatePopup]);

  if (!popupPos) return null;

  const act = (handler: (text: string) => void) => {
    const text = selectedTextRef.current;
    if (!text) return;
    handler(text);
    window.getSelection()?.removeAllRanges();
    setPopupPos(null);
    selectedTextRef.current = "";
  };
  return (
    <SelectionPopupActions
      position={popupPos}
      onReply={() => act(onReply)}
      onAskInSideChat={onAskInSideChat ? () => act(onAskInSideChat) : undefined}
    />
  );
}

// Override shared-variant translucent hover — these buttons float over text.
const FLOATING_BUTTON_CLASS =
  "gap-1 shadow-md hover:bg-secondary hover:brightness-95 dark:hover:brightness-110";

// Prevent the mousedown from clearing the selection before it is read.
function keepSelection(event: React.MouseEvent) {
  event.preventDefault();
}

/**
 * The popup's buttons on their own, for a selection tracked elsewhere, such as text selected
 * inside a sandboxed gadget frame. `position` is the selection's top center in viewport
 * coordinates.
 */
export function SelectionPopupActions({
  position,
  onReply,
  onAskInSideChat,
  onAddComment,
}: {
  position: { x: number; y: number };
  onReply: () => void;
  onAskInSideChat?: () => void;
  /** Comments on the selection, like a file's Add comment. */
  onAddComment?: () => void;
}) {
  // Panel containment changes the fixed-position origin; use the viewport.
  return createPortal(
    <div
      style={{
        position: "fixed",
        // Translate left by 50% to center the buttons over the midpoint of the
        // selection, and up by 100% + 6px to sit just above the selection rect.
        left: position.x,
        top: position.y,
        transform: "translate(-50%, calc(-100% - 6px))",
        zIndex: 50,
      }}
    >
      <Button
        type="button"
        variant="secondary"
        size="sm"
        className={FLOATING_BUTTON_CLASS}
        onMouseDown={keepSelection}
        onClick={onReply}
      >
        <CornerUpLeftIcon className="size-3.5" />
        Reply ↵
      </Button>
      {onAskInSideChat ? (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          className={FLOATING_BUTTON_CLASS}
          onMouseDown={keepSelection}
          onClick={onAskInSideChat}
        >
          <MessagesSquareIcon className="size-3.5" />
          Ask in side chat
        </Button>
      ) : null}
      {onAddComment ? (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          className={FLOATING_BUTTON_CLASS}
          onMouseDown={keepSelection}
          onClick={onAddComment}
        >
          <MessageSquarePlusIcon className="size-3.5" />
          Add comment
        </Button>
      ) : null}
    </div>,
    getEmbedRoot() ?? document.body,
  );
}
