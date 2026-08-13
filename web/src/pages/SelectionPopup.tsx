// The one contextual selection toolbar for the chat transcript: "Reply ↵" for
// any selection, plus "Ask sub-agent" when the whole selection sits inside one
// assistant response and the viewer may create a sub-agent.

import { useCallback, useEffect, useRef, useState } from "react";
import { CornerUpLeftIcon, SparklesIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useAskSubagent, type AskSubagentSelection } from "@/shell/AskSubagentContext";

// Wrapper BlockRenderer puts around an assistant message's rendered text.
const ASSISTANT_TEXT_SECTION = '[data-testid="assistant-text-section"]';
// Block elements the "surrounding excerpt" is drawn from — the nearest
// containing paragraph / list item / blockquote / heading / code block.
const BLOCK_TAGS = new Set(["P", "LI", "BLOCKQUOTE", "PRE", "H1", "H2", "H3", "H4", "H5", "H6"]);
const MAX_EXCERPT_CHARS = 2000;

/**
 * The assistant-text-section a selection endpoint sits in, or `null`. Selection
 * endpoints are often Text nodes, which have no `closest`, so resolve via the
 * parent element.
 */
function assistantSectionForNode(node: Node | null): Element | null {
  if (node === null) return null;
  const el = node.nodeType === Node.TEXT_NODE ? node.parentElement : (node as Element | null);
  return el?.closest?.(ASSISTANT_TEXT_SECTION) ?? null;
}

/**
 * The nearest containing block around the selection, within `section` — its
 * trimmed textContent capped at 2000 chars. `null` when there is no distinct
 * block, or the block's text equals the selection (so the excerpt would only
 * duplicate it).
 */
function nearestBlockExcerpt(range: Range, section: Element, selectedText: string): string | null {
  const start = range.commonAncestorContainer;
  let el: Element | null =
    start.nodeType === Node.TEXT_NODE ? start.parentElement : (start as Element | null);
  while (el !== null && el !== section && !BLOCK_TAGS.has(el.tagName)) {
    el = el.parentElement;
  }
  if (el === null || el === section || !section.contains(el)) return null;
  const text = (el.textContent ?? "").trim();
  if (text === "" || text === selectedText.trim()) return null;
  return text.length > MAX_EXCERPT_CHARS ? text.slice(0, MAX_EXCERPT_CHARS) : text;
}

/**
 * Floating selection toolbar, scoped to the conversation container.
 *
 * @param containerRef Conversation area; selections outside it (e.g. the
 *   composer) are ignored.
 * @param onReply Append the selection as a reply quote (unchanged behavior).
 */
export function SelectionPopup({
  containerRef,
  onReply,
}: {
  containerRef: React.RefObject<HTMLElement | null>;
  onReply: (text: string) => void;
}) {
  // Cross-surface "Ask sub-agent" opener (AppShell). `null` when there is no
  // provider (isolated tests) → the action is simply hidden.
  const ask = useAskSubagent();
  const [popupPos, setPopupPos] = useState<{ x: number; y: number } | null>(null);
  // Whether the current selection qualifies for "Ask sub-agent" (entirely
  // inside one assistant response). Reply has no such restriction.
  const [askable, setAskable] = useState(false);
  const selectedTextRef = useRef<string>("");
  // Structured selection for "Ask sub-agent" (exact text + surrounding excerpt),
  // captured when the selection sits inside one assistant response.
  const askSelectionRef = useRef<AskSubagentSelection | null>(null);

  const clear = useCallback(() => {
    setPopupPos(null);
    setAskable(false);
    selectedTextRef.current = "";
    askSelectionRef.current = null;
  }, []);

  // Escape dismisses the toolbar (keyboard parity with click-away).
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") clear();
    },
    [clear],
  );

  const updatePopup = useCallback(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      clear();
      return;
    }
    const text = sel.toString().trim();
    if (!text) {
      clear();
      return;
    }
    // Scope to the conversation container — ignore selections in the composer.
    const container = containerRef.current;
    const anchor = sel.anchorNode;
    if (!container || !anchor || !container.contains(anchor)) {
      clear();
      return;
    }
    const range = sel.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    // Position the toolbar just above the selection, horizontally centered.
    setPopupPos({ x: rect.left + rect.width / 2, y: rect.top });
    selectedTextRef.current = text;
    // Ask is offered only when the whole selection is inside one assistant
    // response; capture its exact text + nearest containing block there.
    const section = assistantSectionForNode(sel.anchorNode);
    if (section !== null && section === assistantSectionForNode(sel.focusNode)) {
      askSelectionRef.current = {
        selectedText: text,
        surroundingExcerpt: nearestBlockExcerpt(range, section, text),
      };
      setAskable(true);
    } else {
      askSelectionRef.current = null;
      setAskable(false);
    }
  }, [containerRef, clear]);

  useEffect(() => {
    document.addEventListener("mouseup", updatePopup);
    document.addEventListener("selectionchange", updatePopup);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mouseup", updatePopup);
      document.removeEventListener("selectionchange", updatePopup);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [updatePopup, handleKeyDown]);

  if (!popupPos) return null;

  return (
    <div
      data-testid="selection-popup"
      style={{
        position: "fixed",
        // Center over the selection midpoint and sit just above the rect.
        left: popupPos.x,
        top: popupPos.y,
        transform: "translate(-50%, calc(-100% - 6px))",
        zIndex: 50,
      }}
    >
      {/* One toolbar row — the actions sit side by side, so neither covers the
          other. */}
      <div className="flex items-center gap-1">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          data-testid="selection-reply"
          className="gap-1 shadow-md hover:bg-secondary hover:brightness-95 dark:hover:brightness-110"
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => {
            const text = selectedTextRef.current;
            if (!text) return;
            onReply(text);
            window.getSelection()?.removeAllRanges();
            clear();
          }}
        >
          <CornerUpLeftIcon className="size-3.5" />
          Reply ↵
        </Button>
        {askable && ask !== null && ask.canAsk && (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            data-testid="selection-ask-subagent"
            className="gap-1 shadow-md hover:bg-secondary hover:brightness-95 dark:hover:brightness-110"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              const selection = askSelectionRef.current;
              if (selection === null) return;
              ask.askSubagent(selection);
              window.getSelection()?.removeAllRanges();
              clear();
            }}
          >
            <SparklesIcon className="size-3.5" />
            Ask sub-agent
          </Button>
        )}
      </div>
    </div>
  );
}
