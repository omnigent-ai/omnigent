// Find in chat opens the collapsed containers that hold the match it steps to,
// so the highlight painter has rendered text to paint. They close again once
// the find bar closes or steps to another bubble.

import { createContext, useMemo, useState } from "react";
import type { MessageJumpTarget } from "@/hooks/useMessageDeepLink";
import type { RenderItem } from "@/lib/renderItems";
import { findTextRanges } from "@/shell/previewSearch";

/** The bubble the find bar stepped to and the matches stepped to inside it. */
export interface ChatFindReveal {
  /** Anchor id of the bubble: a user bubble's item id or an assistant bubble's response id. */
  anchor: string;
  /** Matched item ids stepped to in this bubble; the last one is the current match. */
  itemIds: readonly string[];
  query: string;
}

export const ChatFindRevealContext = createContext<ChatFindReveal | null>(null);

/**
 * What find reveals for `target`: its bubble, and the matches stepped to there
 * since the find bar last entered that bubble. Null without a target.
 */
export function useChatFindReveal(
  target: MessageJumpTarget | null,
  resolveAnchor: (id: string) => string | null,
): ChatFindReveal | null {
  const anchorOf = (jump: MessageJumpTarget) =>
    jump.candidates.map(resolveAnchor).find((id) => id !== null) ?? null;
  const [trail, setTrail] = useState<{ target: MessageJumpTarget | null; itemIds: string[] }>({
    target: null,
    itemIds: [],
  });
  const anchor = target && anchorOf(target);
  if (target !== trail.target) {
    const sameBubble =
      anchor !== null && trail.target !== null && anchorOf(trail.target) === anchor;
    const itemId = target?.candidates[0];
    setTrail({
      target,
      itemIds: itemId === undefined ? [] : [...(sameBubble ? trail.itemIds : []), itemId],
    });
  }
  const query = target?.query ?? "";
  return useMemo(
    () => (anchor && query ? { anchor, itemIds: trail.itemIds, query } : null),
    [anchor, trail.itemIds, query],
  );
}

/** "current" holds the current match; "kept" holds one stepped to earlier in the same bubble. */
export type FindReveal = "current" | "kept";

/** Marks the element that holds the current match, where the jump centers its first occurrence. */
export const FIND_CURRENT_ATTRIBUTE = "data-chat-find-current";

function holdsItem(item: RenderItem, itemId: string): boolean {
  if (item.itemId === itemId) return true;
  // A tool output and a settled reasoning section render under another item's key.
  if (item.kind === "tool") return item.outputItemId === itemId;
  if (item.kind === "reasoning") return item.sourceItemId === itemId;
  return false;
}

/** The render items that show `itemIds`, each marked current or kept. */
export function findRevealsFor(
  items: readonly RenderItem[],
  itemIds: readonly string[],
): Map<RenderItem, FindReveal> {
  const reveals = new Map<RenderItem, FindReveal>();
  itemIds.forEach((itemId, index) => {
    const item = items.find((candidate) => holdsItem(candidate, itemId));
    if (item) reveals.set(item, index === itemIds.length - 1 ? "current" : "kept");
  });
  return reveals;
}

/**
 * The open state a collapsible shows: open while find reveals it, else `open`.
 * A toggle while revealed holds until the reveal ends, and also lands in `open`,
 * so only a container find opened closes again.
 */
export function useRevealedOpen(
  revealed: boolean,
  open: boolean,
  onOpenChange: (open: boolean) => void,
): [boolean, (open: boolean) => void] {
  return useRevealedValue(revealed ? true : undefined, open, onOpenChange);
}

/** {@link useRevealedOpen} for any state: `revealedValue` shows while defined, else `value`. */
export function useRevealedValue<T>(
  revealedValue: T | undefined,
  value: T,
  onChange: (value: T) => void,
): [T, (value: T) => void] {
  const [override, setOverride] = useState<{ value: T } | null>(null);
  const [wasRevealed, setWasRevealed] = useState(revealedValue);
  if (revealedValue !== wasRevealed) {
    setWasRevealed(revealedValue);
    setOverride(null);
  }
  const revealed = revealedValue !== undefined;
  const shown = revealed ? (override ?? { value: revealedValue }).value : value;
  const change = (next: T) => {
    if (revealed) setOverride({ value: next });
    onChange(next);
  };
  return [shown, change];
}

/** Props for a collapsible that starts closed and opens while find reveals it. */
export function useRevealedCollapsible(revealed: boolean, current = false) {
  const [open, setOpen] = useState(false);
  const [shown, change] = useRevealedOpen(revealed, open, setOpen);
  return { open: shown, onOpenChange: change, [FIND_CURRENT_ATTRIBUTE]: current ? "" : undefined };
}

/**
 * The first occurrence of `query` in the current match's element, else anywhere
 * in `bubble`. Null when the current match is in another bubble of the response.
 */
export function firstFindRange(bubble: Element, query: string): Range | null {
  const current = bubble.querySelector(`[${FIND_CURRENT_ATTRIBUTE}]`);
  if (!current && document.querySelector(`[${FIND_CURRENT_ATTRIBUTE}]`)) return null;
  return (current && findTextRanges(current, query)[0]) || findTextRanges(bubble, query)[0] || null;
}
