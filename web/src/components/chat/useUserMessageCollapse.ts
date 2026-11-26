import { useLayoutEffect, useState } from "react";
import { omitIncompleteLocalReviewComment } from "@/lib/localReviewComments";

export const USER_MESSAGE_PREVIEW_LINES = 15;
// Bound Markdown parsing until the user asks to read the full prompt.
const PREVIEW_CHARACTER_LIMIT = 12_000;
const MAX_REMEMBERED_MESSAGES = 1_000;
const expandedMessages = new Map<string, boolean>();

/** A height-limited prompt preview with disclosure choices remembered until reload. */
export function useUserMessageCollapse(sessionId: string | null, messageId: string, text: string) {
  const key = JSON.stringify([sessionId, messageId]);
  const [expanded, setExpanded] = useState(() => expandedMessages.get(key) ?? false);
  const [content, setContent] = useState<HTMLDivElement | null>(null);
  const [overflows, setOverflows] = useState(false);

  useLayoutEffect(() => {
    setExpanded(expandedMessages.get(key) ?? false);
  }, [key]);

  useLayoutEffect(() => {
    if (!content) return;
    const measure = () => {
      const lineHeight = Number.parseFloat(getComputedStyle(content).lineHeight);
      setOverflows(content.scrollHeight > USER_MESSAGE_PREVIEW_LINES * lineHeight + 1);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(content);
    return () => observer.disconnect();
  }, [content, text, expanded]);

  const truncated = text.length > PREVIEW_CHARACTER_LIMIT;
  const isCollapsed = !expanded;
  // Avoid ending a sliced preview halfway through an emoji.
  const visibleText =
    isCollapsed && truncated
      ? omitIncompleteLocalReviewComment(
          text.slice(0, PREVIEW_CHARACTER_LIMIT).replace(/[\uD800-\uDBFF]$/, ""),
        )
      : text;

  function toggle() {
    const next = !expanded;
    if (sessionId !== null) {
      expandedMessages.delete(key);
      expandedMessages.set(key, next);
      if (expandedMessages.size > MAX_REMEMBERED_MESSAGES) {
        expandedMessages.delete(expandedMessages.keys().next().value!);
      }
    }
    setExpanded(next);
  }

  return {
    contentRef: setContent,
    isCollapsed,
    canExpand: overflows || truncated,
    visibleText,
    toggle,
  };
}
