// Floating "Reply ↵" button shown over a text selection inside the chat
// transcript. Extracted from ChatPage so the popup can grow independently;
// behavior is unchanged.

import { useCallback, useEffect, useRef, useState } from "react";
import { CornerUpLeftIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * Floating selection action, scoped to the conversation container.
 *
 * @param containerRef Conversation area; selections outside it (e.g. the
 *   composer) are ignored.
 * @param onReply Append the selected text as a reply quote.
 */
export function SelectionPopup({
  containerRef,
  onReply,
}: {
  containerRef: React.RefObject<HTMLElement | null>;
  onReply: (text: string) => void;
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

  return (
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
        variant="secondary"
        size="sm"
        // Override shared-variant translucent hover — this button floats over text.
        className="gap-1 shadow-md hover:bg-secondary hover:brightness-95 dark:hover:brightness-110"
        onMouseDown={(e) => {
          // Prevent the mousedown from clearing the selection before we read it.
          e.preventDefault();
        }}
        onClick={() => {
          const text = selectedTextRef.current;
          if (text) {
            onReply(text);
            window.getSelection()?.removeAllRanges();
            setPopupPos(null);
            selectedTextRef.current = "";
          }
        }}
      >
        <CornerUpLeftIcon className="size-3.5" />
        Reply ↵
      </Button>
    </div>
  );
}
