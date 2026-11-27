// The narrowest a workspace sidebar can be while its strip still shows every fixed control, such
// as its Collapse button. Only the tabs' room shrinks with the sidebar, so the rest is the need.

import { type RefObject, useEffect, useLayoutEffect, useRef } from "react";

export interface StripControlsLayout {
  /** The strip's own width, which keeps the sidebar's open width while the sidebar slides. */
  stripWidth: number;
  /** The sidebar's borders beside the strip. */
  sidebarBorder: number;
  /** The width the tabs' room has now; it shrinks to 0 before the strip overflows. */
  tabsAreaWidth: number;
  /** How far the strip's content, end padding included, runs past its visible box. */
  stripOverflow: number;
  /** The strip's end padding, which the last control may run into and still show whole. */
  stripEndPadding: number;
}

/** The sidebar width at which the strip's fixed controls just fit, from one layout of it. */
export function stripControlsWidth(layout: StripControlsLayout): number {
  return Math.ceil(
    layout.stripWidth +
      layout.sidebarBorder -
      layout.tabsAreaWidth +
      layout.stripOverflow -
      layout.stripEndPadding,
  );
}

/**
 * Report the strip's controls width whenever it changes. Its ResizeObserver measures; a commit
 * reads no layout, so a session switch's commit doesn't force a layout of the new page. It
 * measures the strip, not the sliding sidebar, so a reopen that resizes only the sidebar can't
 * leave an underestimate behind. A strip laid out at no width, such as a hidden one, is skipped.
 */
export function useStripControlsWidth(
  tabsAreaRef: RefObject<HTMLElement | null>,
  onWidth: ((width: number) => void) | undefined,
): void {
  const latest = useRef(onWidth);
  latest.current = onWidth;
  const measured = useRef<number | null>(null);
  // What was last reported, and to whom: another session's sidebar needs it even when unchanged.
  const reported = useRef<{ width: number; to: (width: number) => void } | null>(null);
  const report = useRef(() => {});
  report.current = () => {
    const to = latest.current;
    const width = measured.current;
    if (!to || width === null) return;
    if (reported.current?.width === width && reported.current.to === to) return;
    reported.current = { width, to };
    to(width);
  };
  const measure = useRef(() => {});
  measure.current = () => {
    const area = tabsAreaRef.current;
    const strip = area?.parentElement;
    if (!area || !strip) return;
    const stripWidth = strip.getBoundingClientRect().width;
    if (stripWidth <= 0) return;
    const sidebar = strip.closest("aside");
    const sidebarStyle = sidebar ? getComputedStyle(sidebar) : null;
    measured.current = stripControlsWidth({
      stripWidth,
      sidebarBorder: sidebarStyle
        ? (Number.parseFloat(sidebarStyle.borderLeftWidth) || 0) +
          (Number.parseFloat(sidebarStyle.borderRightWidth) || 0)
        : 0,
      tabsAreaWidth: area.getBoundingClientRect().width,
      stripOverflow: strip.scrollWidth - strip.clientWidth,
      stripEndPadding: Number.parseFloat(getComputedStyle(strip).paddingRight) || 0,
    });
    report.current();
  };

  // The strip and each of its parts; a newly observed part gets the observer's first callback.
  const observer = useRef<ResizeObserver | null>(null);
  const parts = useRef<readonly Element[]>([]);
  const removedPart = useRef(false);
  useLayoutEffect(() => {
    const strip = tabsAreaRef.current?.parentElement;
    if (!strip || typeof ResizeObserver === "undefined") return;
    const watching = new ResizeObserver(() => measure.current());
    watching.observe(strip);
    parts.current = Array.from(strip.children);
    for (const part of parts.current) watching.observe(part);
    observer.current = watching;
    return () => {
      watching.disconnect();
      observer.current = null;
    };
  }, [tabsAreaRef]);
  // A commit only re-observes the parts. A removed control resizes none of the rest when the
  // strip overflows, so that case measures after paint, when the layout is already done.
  useLayoutEffect(() => {
    const strip = tabsAreaRef.current?.parentElement;
    const watching = observer.current;
    if (!strip || !watching) return;
    const now = Array.from(strip.children);
    const previous = parts.current;
    if (now.length === previous.length && now.every((part, index) => part === previous[index])) {
      return;
    }
    for (const part of now) watching.observe(part);
    for (const part of previous) {
      if (now.includes(part)) continue;
      watching.unobserve(part);
      removedPart.current = true;
    }
    parts.current = now;
  });
  useEffect(() => {
    if (!removedPart.current) return;
    removedPart.current = false;
    measure.current();
  });
  // A new report target, such as another session's sidebar, gets the last width as it is.
  useLayoutEffect(() => report.current(), [onWidth]);
}
