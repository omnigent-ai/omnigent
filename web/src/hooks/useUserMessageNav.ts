// Cursor for stepping through past user messages.
//
// Anchor tracked by itemId (not index) so loadMoreHistory's prepend
// doesn't corrupt position. Stale anchor (e.g. tempId promoted to
// real itemId mid-nav) degrades to outside-end — next goPrev lands
// on the latest message.
//
// Call ONCE per parent and share the returned object; two callers
// would each hold their own anchor and diverge.

import { useCallback, useMemo, useState } from "react";
import { releaseConversationScrollLock } from "@/components/ai-elements/conversation";
import { useChatStore } from "@/store/chatStore";

export interface UserMessageNav {
  goPrev: () => void;
  goNext: () => void;
  canPrev: boolean;
  canNext: boolean;
}

// How long the scroll must stay quiet before we treat the smooth-scroll as
// finished and fire the flash.
const SCROLL_SETTLE_MS = 120;
// Absolute cap so a flash always happens even if scroll events never settle.
const SCROLL_SETTLE_MAX_MS = 1200;

let cancelPendingFlash: (() => void) | null = null;

// Nearest scrollable ancestor — the element scrollIntoView actually moves and
// whose `scroll` events tell us when motion stops. Falls back to window.
function getScrollParent(node: Element): Element | null {
  let el: HTMLElement | null = node.parentElement;
  while (el) {
    const { overflowY } = getComputedStyle(el);
    if (overflowY === "auto" || overflowY === "scroll" || overflowY === "overlay") {
      return el;
    }
    el = el.parentElement;
  }
  return null;
}

/**
 * Center `range` in each scroller around it, up to `outer`: inner ones (a
 * tool's output panel) at once, `outer` with `behavior`.
 */
function scrollRangeIntoView(range: Range, outer: Element | null, behavior: ScrollBehavior): void {
  for (let el = range.startContainer.parentElement; el; el = el.parentElement) {
    const { overflowY } = getComputedStyle(el);
    if (overflowY !== "auto" && overflowY !== "scroll" && overflowY !== "overlay") continue;
    const rect = range.getBoundingClientRect();
    const box = el.getBoundingClientRect();
    const top = rect.top - box.top - (box.height - rect.height) / 2;
    if (el === outer) {
      el.scrollBy?.({ top, behavior });
      return;
    }
    el.scrollTop += top;
  }
}

function rangeInBox(range: Range, box: Element): boolean {
  const rect = range.getBoundingClientRect();
  const bounds = box.getBoundingClientRect();
  return rect.top >= bounds.top && rect.bottom <= bounds.bottom;
}

/**
 * Center a user or assistant message and flash it after scrolling settles.
 * `ensureVisible` mounts a virtualized row before the DOM lookup is retried.
 * `focus` picks a range inside the message to center instead, e.g. a found word.
 */
export function scrollToMessage(
  messageId: string,
  flash?: (id: string) => void,
  ensureVisible?: (id: string) => boolean,
  focus?: (el: Element) => Range | null,
): void {
  // The row may be windowed out of the DOM (virtualized transcript). Ask the
  // transcript to scroll it into the mounted range first; its node then mounts
  // on the next frame, so retry the DOM lookup + centering scroll there.
  const scrolledIntoWindow = ensureVisible?.(messageId) ?? false;
  const id = CSS.escape(messageId);
  const bubbles = [
    ...document.querySelectorAll(`[data-message-id="${id}"]`),
    ...document.querySelectorAll(`[data-user-message-id="${id}"]`),
  ];
  // One response can render as several bubbles: land on the one holding the focus range.
  const el = (focus && bubbles.find((bubble) => focus(bubble))) || bubbles[0];
  if (!el) {
    if (scrolledIntoWindow) {
      // Row is being mounted by the virtualizer — center on it next frame.
      requestAnimationFrame(() => scrollToMessage(messageId, flash, undefined, focus));
      return;
    }
    // Fail loud: id exists in the list but DOM anchor is missing.
    console.warn(`scrollToMessage: no element for messageId=${messageId}`);
    return;
  }

  // Supersede the previous jump's pending flash so rapid nav only flashes
  // the message we finally land on.
  cancelPendingFlash?.();

  // Opening a tall transcript starts StickToBottom locked to the bottom.
  // Without releasing that lock, the next content-resize scrollToBottom
  // yanks the view back — deep-link / rail jumps look like a no-op on
  // multi-message sessions (single short transcripts stay in view anyway).
  releaseConversationScrollLock();

  const message: Element = el;
  const outer = getScrollParent(message);
  const range = focus?.(message) ?? null;
  if (range) scrollRangeIntoView(range, outer, "smooth");
  else el.scrollIntoView({ block: "center", behavior: "smooth" });

  // Nothing to defer when there's no flash to fire — the smooth-scroll runs
  // to completion on its own.
  if (!flash) return;

  // Defer the flash until the smooth-scroll settles. On a long jump the
  // highlight would otherwise burn out before the message is on screen.
  const scroller: EventTarget = outer ?? window;
  let settleTimer = 0;
  let maxTimer = 0;
  let done = false;

  function cleanup(): void {
    window.clearTimeout(settleTimer);
    window.clearTimeout(maxTimer);
    scroller.removeEventListener("scroll", onScroll);
    if (cancelPendingFlash === cleanup) cancelPendingFlash = null;
  }

  function finish(): void {
    if (done) return;
    done = true;
    cleanup();
    // Rows the jump resized (a bubble find opened or closed) move once the
    // virtualizer re-measures them, so a found word can end up off screen.
    const settled = focus?.(message);
    if (settled && outer && !rangeInBox(settled, outer)) {
      scrollRangeIntoView(settled, outer, "auto");
    }
    flash?.(messageId);
  }

  function onScroll(): void {
    window.clearTimeout(settleTimer);
    settleTimer = window.setTimeout(finish, SCROLL_SETTLE_MS);
  }

  cancelPendingFlash = cleanup;
  scroller.addEventListener("scroll", onScroll, { passive: true });
  // First timer doubles as the "already in view, nothing scrolled" fast path;
  // each scroll event reschedules it while the smooth-scroll is in motion.
  settleTimer = window.setTimeout(finish, SCROLL_SETTLE_MS);
  maxTimer = window.setTimeout(finish, SCROLL_SETTLE_MAX_MS);
}

export function scrollToUserMessage(
  itemId: string,
  flash?: (id: string) => void,
  ensureVisible?: (id: string) => boolean,
): void {
  scrollToMessage(itemId, flash, ensureVisible);
}

export function useUserMessageNav(
  userMessageIds: readonly string[],
  // From the virtualized transcript: pulls a windowed-out target into the DOM
  // before the centering scroll. Omitted in tests / non-virtualized callers.
  ensureItemVisible?: (id: string) => boolean,
): UserMessageNav {
  const flashUserMessage = useChatStore((s) => s.flashUserMessage);
  const [anchorId, setAnchorId] = useState<string | null>(null);

  const currentIndex = anchorId === null ? -1 : userMessageIds.indexOf(anchorId);
  // outside = never navigated, or anchor was removed from the list.
  const outside = anchorId === null || currentIndex === -1;

  const canPrev = userMessageIds.length > 0 && (outside || currentIndex > 0);
  const canNext = !outside && currentIndex < userMessageIds.length - 1;

  const goPrev = useCallback(() => {
    if (userMessageIds.length === 0) return;
    if (!outside && currentIndex === 0) return;
    const target = outside
      ? userMessageIds[userMessageIds.length - 1]
      : userMessageIds[currentIndex - 1];
    setAnchorId(target);
    scrollToUserMessage(target, flashUserMessage, ensureItemVisible);
  }, [userMessageIds, currentIndex, outside, flashUserMessage, ensureItemVisible]);

  const goNext = useCallback(() => {
    if (outside) return;
    if (currentIndex >= userMessageIds.length - 1) return;
    const target = userMessageIds[currentIndex + 1];
    setAnchorId(target);
    scrollToUserMessage(target, flashUserMessage, ensureItemVisible);
  }, [userMessageIds, currentIndex, outside, flashUserMessage, ensureItemVisible]);

  // Stable identity so consumers can put the return value in an
  // effect dep array without re-registering on every render.
  return useMemo(() => ({ goPrev, goNext, canPrev, canNext }), [goPrev, goNext, canPrev, canNext]);
}
