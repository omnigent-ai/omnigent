import { type RefObject, useLayoutEffect, useRef } from "react";

/**
 * Measure ``ta`` and set its height to fit its content, capped at
 * ``maxRows`` rows (after which it scrolls). When ``scrollHeight`` is 0 the
 * element isn't laid out yet (e.g. mid client-side route swap), so the
 * natural height is left untouched rather than collapsed to 0px, which
 * would clip the content/placeholder.
 *
 * Reading the content height means collapsing to ``auto`` — a one-row box,
 * since these textareas are ``rows={1}`` — and that collapse must not escape
 * the composer. For the one layout it lasts the composer is short, so the
 * transcript's scroll viewport is correspondingly taller, and the browser
 * clamps its ``scrollTop`` against that larger viewport's smaller maximum.
 * The clamp sticks once the composer springs back, so every re-measure shunts
 * the transcript down a line. Pinning and clipping the wrapper keeps the
 * collapse inside the composer.
 */
function measureTextarea(
  ta: HTMLTextAreaElement,
  maxRows: number,
  onGrowth?: (px: number) => void,
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
  try {
    ta.style.height = "auto";
    if (ta.scrollHeight === 0) {
      // Not laid out, so there's no growth to report either — say so rather
      // than leave a stale offset applied across a route swap.
      onGrowth?.(0);
      return;
    }
    const cs = getComputedStyle(ta);
    const lineHeight = parseFloat(cs.lineHeight);
    const paddingTop = parseFloat(cs.paddingTop);
    const paddingBottom = parseFloat(cs.paddingBottom);
    const maxHeight = lineHeight * maxRows + paddingTop + paddingBottom;
    const height = Math.min(ta.scrollHeight, maxHeight);
    ta.style.height = height + "px";
    // Resting height is a single row — or a CSS floor, for a composer that
    // sets one, so its empty height doesn't read as growth.
    const resting = Math.max(
      lineHeight + paddingTop + paddingBottom,
      parseFloat(cs.minHeight) || 0,
    );
    onGrowth?.(Math.max(0, height - resting));
  } finally {
    // Release both guards before the next layout so the composer resizes once.
    if (pinned) {
      wrapper.style.height = restoreHeight;
      wrapper.style.overflow = restoreOverflow;
    }
  }
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

  // Re-measure on content / maxRows change.
  useLayoutEffect(() => {
    if (ref.current) measureTextarea(ref.current, maxRows, onGrowthRef.current);
  }, [ref, value, maxRows]);

  // Install one observer per element (not per keystroke — value isn't a
  // dep) so the box recovers once layout settles after a 0-height mount.
  // Setting height in measureTextarea converges (auto → fixed → same
  // fixed), so the observer settles rather than looping. Guarded for
  // environments (jsdom) without ResizeObserver.
  useLayoutEffect(() => {
    const ta = ref.current;
    if (!ta || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => measureTextarea(ta, maxRows, onGrowthRef.current));
    ro.observe(ta);
    return () => ro.disconnect();
  }, [ref, maxRows]);
}
