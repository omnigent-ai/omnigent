import { type RefObject, useLayoutEffect, useRef } from "react";

/**
 * Cache scrollTop of all ancestor elements up to document.documentElement.
 * When a textarea's height collapses (style.height = "auto"), Gecko/Firefox
 * may adjust or reset scroll positions on scrollable ancestor containers
 * due to scroll anchoring or sudden viewport enlargement. Restoring them
 * after setting the new height prevents layout jumps.
 */
function cacheAncestorScrollTops(el: HTMLElement): () => void {
  const entries: [Element, number][] = [];
  let current = el.parentElement;
  while (current && current instanceof Element) {
    if (current.scrollTop > 0) {
      entries.push([current, current.scrollTop]);
    }
    current = current.parentElement;
  }
  if (
    typeof document !== "undefined" &&
    document.documentElement &&
    document.documentElement.scrollTop > 0
  ) {
    entries.push([document.documentElement, document.documentElement.scrollTop]);
  }

  return () => {
    for (const [node, scrollTop] of entries) {
      const elNode = node as HTMLElement;
      const prevBehavior = elNode.style?.scrollBehavior;
      if (elNode.style) elNode.style.scrollBehavior = "auto";
      node.scrollTop = scrollTop;
      if (elNode.style) elNode.style.scrollBehavior = prevBehavior ?? "";
    }
  };
}

/**
 * Measure ``ta`` and set its height to fit its content, capped at
 * ``maxRows`` rows (after which it scrolls). When ``scrollHeight`` is 0 the
 * element isn't laid out yet (e.g. mid client-side route swap), so the
 * natural height is left untouched rather than collapsed to 0px, which
 * would clip the content/placeholder.
 *
 * Reading the content height requires collapsing to ``auto`` — a one-row box,
 * since these textareas are ``rows={1}`` — when checking for potential height
 * shrinkage (e.g. deletions or non-appending edits). That collapse must not
 * escape the composer. For the one layout it lasts the composer is short, so the
 * transcript's scroll viewport is correspondingly taller, and the browser
 * clamps its ``scrollTop`` against that larger viewport's smaller maximum.
 * Pinning and clipping the wrapper keeps the collapse inside the composer.
 *
 * The textarea's own overflow is pinned for the same window: the collapsed
 * one-row box momentarily overflows its draft, and Gecko paints that overflow
 * as a caret jump (the bounce Firefox/Zen users see on wrapped lines) before
 * the new height lands. Blink suppresses the intermediate paint, so the
 * wrapper pin alone reads as fixed there. The scroll offset of the textarea
 * itself and of all scrollable ancestor nodes is preserved across the measure.
 *
 * When pure text appending occurs (`testForHeightReduction` is false), collapsing
 * to `height: auto` is skipped completely because the content height can only
 * grow or stay equal. Skipping the collapse eliminates Gecko scroll-anchoring
 * jitter entirely during normal typing.
 */
function measureTextarea(
  ta: HTMLTextAreaElement,
  maxRows: number,
  onGrowth?: (px: number) => void,
  testForHeightReduction = true,
): void {
  const wrapper = ta.parentElement;
  const wrapperHeight = wrapper?.getBoundingClientRect().height ?? 0;
  const restoreHeight = wrapper?.style.height ?? "";
  const restoreOverflow = wrapper?.style.overflow ?? "";
  const pinned = wrapper !== null && wrapperHeight > 0;
  if (pinned) {
    wrapper.style.height = `${wrapperHeight}px`;
    wrapper.style.overflow = "hidden";
  }
  const restoreOverflowY = ta.style.overflowY;
  ta.style.overflowY = "hidden";
  // Captured before the collapse: setting ``height:auto`` clamps the box's
  // scroll offset to 0, and Gecko doesn't restore it when the height lands.
  const restoreScrollTop = ta.scrollTop;
  let restoreAncestorScrollTops: (() => void) | undefined;
  let growth = 0;
  try {
    if (testForHeightReduction) {
      restoreAncestorScrollTops = cacheAncestorScrollTops(ta);
      ta.style.height = "auto";
    }
    if (ta.scrollHeight > 0) {
      const cs = getComputedStyle(ta);
      const lineHeight = parseFloat(cs.lineHeight);
      const paddingTop = parseFloat(cs.paddingTop);
      const paddingBottom = parseFloat(cs.paddingBottom);
      const maxHeight = lineHeight * maxRows + paddingTop + paddingBottom;
      const height = Math.min(ta.scrollHeight, maxHeight);
      ta.style.height = height + "px";
      // Restore the offset the collapse clamped away, bounded by the new box
      // the same way the browser would — the height can land below the old
      // offset (a large deletion, or re-measuring a draft that just capped).
      ta.scrollTop = Math.min(restoreScrollTop, Math.max(0, ta.scrollHeight - ta.clientHeight));
      if (restoreAncestorScrollTops) {
        restoreAncestorScrollTops();
      }
      // Resting height is a single row — or a CSS floor, for a composer that
      // sets one, so its empty height doesn't read as growth.
      const resting = Math.max(
        lineHeight + paddingTop + paddingBottom,
        parseFloat(cs.minHeight) || 0,
      );
      growth = Math.max(0, height - resting);
    }
  } finally {
    // Release both guards before the next layout so the composer resizes once.
    ta.style.overflowY = restoreOverflowY;
    if (pinned) {
      wrapper.style.height = restoreHeight;
      wrapper.style.overflow = restoreOverflow;
    }
  }
  // Reported only after the guards release: a caller re-pinning a sibling
  // viewport (the transcript under a growing composer) must read the
  // post-release geometry — while the wrapper is still pinned, the sibling
  // hasn't shrunk yet and a pin written then is a silent no-op.
  onGrowth?.(growth);
}

/**
 * Auto-grow a textarea from a single row up to ``maxRows`` rows, then
 * let it scroll. Re-measures on every ``value`` change so the height
 * tracks the content.
 *
 * Shared by the in-session composer (ChatPage's ``Composer``) and the
 * home-page composer (``NewChatLandingScreen``) so their grow behavior
 * stays in lockstep instead of drifting between two copies.
 *
 * A ``ResizeObserver`` re-measures when the element's box changes,
 * covering the case where ``scrollHeight`` reads 0 on mount (e.g. mid
 * client-side route swap, before layout settles).
 *
 * ``onGrowth`` reports how much taller than its resting height the box now is
 * for callers that need to coordinate nearby controls with the resized input.
 */
export function useAutoGrowTextarea(
  ref: RefObject<HTMLTextAreaElement | null>,
  value: string,
  maxRows = 10,
  onGrowth?: (px: number) => void,
) {
  // Read through a ref so an inline callback doesn't re-subscribe the
  // observer below on every render. Declared first: effects run in order, so
  // this is current before either measuring effect uses it.
  const onGrowthRef = useRef(onGrowth);
  useLayoutEffect(() => {
    onGrowthRef.current = onGrowth;
  });

  const prevValueRef = useRef(value);

  // Re-measure on content / maxRows change.
  useLayoutEffect(() => {
    const ta = ref.current;
    if (!ta) return;
    const prev = prevValueRef.current;
    prevValueRef.current = value;

    // Only test for height reduction if value shrunk or changed in a non-append way.
    // If empty, test reduction because placeholder or reset might need re-measurement.
    const isAppend = prev !== "" && value.startsWith(prev);
    const testForHeightReduction = !isAppend;

    measureTextarea(ta, maxRows, onGrowthRef.current, testForHeightReduction);
  }, [ref, value, maxRows]);

  // Install one observer per element (not per keystroke — value isn't a dep)
  // so the box recovers once layout settles after a 0-height mount.
  // Setting height in measureTextarea converges (auto → fixed → same fixed),
  // so the observer settles rather than looping. Guarded for environments (jsdom)
  // without ResizeObserver.
  useLayoutEffect(() => {
    const ta = ref.current;
    if (!ta || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => measureTextarea(ta, maxRows, onGrowthRef.current, true));
    ro.observe(ta);
    return () => ro.disconnect();
  }, [ref, maxRows]);
}
