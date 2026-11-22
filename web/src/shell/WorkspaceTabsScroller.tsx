import { type ReactNode, useEffect, useRef } from "react";

/**
 * Scrolls the tab strip just far enough to show `tab`. Unlike `scrollIntoView`, it
 * moves no other scroller: the rail slides in from past the window's right edge,
 * so revealing a tab there would shift the whole page sideways.
 */
export function scrollTabIntoView(tab: HTMLElement | null): void {
  const viewport = tab?.closest<HTMLElement>("[data-workspace-tabs-viewport]");
  if (!tab || !viewport) return;
  scrollViewportToShow(viewport, tab.getBoundingClientRect());
}

/** Scroll `viewport` just far enough to show `box`, of which `width` pixels are visible. */
export function scrollViewportToShow(
  viewport: HTMLElement,
  box: DOMRect,
  width = viewport.clientWidth,
): void {
  const left = viewport.getBoundingClientRect().left + viewport.clientLeft;
  const right = left + width;
  if (box.left < left) {
    // A tab wider than the strip whose end already shows keeps its close button in view.
    if (box.right <= right && box.width > width) return;
    viewport.scrollLeft -= left - box.left;
  } else if (box.right > right) {
    // A tab wider than the strip keeps its start in view.
    viewport.scrollLeft += Math.min(box.right - right, box.left - left);
  }
}

// How far the strip's edges fade where more tabs are scrolled out of view.
const EDGE_FADE_PX = 16;

/** The mask that fades the edges with tabs beyond them, or none. */
function edgeFadeMask(start: boolean, end: boolean): string {
  if (!start && !end) return "";
  const from = start ? `transparent, black ${EDGE_FADE_PX}px` : "black";
  const to = end ? `black calc(100% - ${EDGE_FADE_PX}px), transparent` : "black";
  return `linear-gradient(to right, ${from}, ${to})`;
}

/** Scroll open tabs horizontally with a hidden scrollbar; an edge fades while tabs lie past it. */
export function WorkspaceTabsScroller({ children }: { children: ReactNode }) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const viewport = viewportRef.current;
    const content = contentRef.current;
    if (!viewport || !content) return;
    const wheel = (event: WheelEvent) => {
      if (viewport.scrollWidth <= viewport.clientWidth) return;
      // Leave native horizontal gestures alone. Ctrl-wheel must be non-passive
      // to scroll tabs instead of zooming the page.
      if (event.deltaX && !event.ctrlKey) return;
      const delta = event.deltaX || event.deltaY;
      if (!delta) return;
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientWidth : 1;
      viewport.scrollLeft += delta * unit;
    };
    // Keep the active tab in view as the rail or tab list resizes.
    const observer = new ResizeObserver(() => {
      scrollTabIntoView(
        content.querySelector<HTMLElement>('[aria-current="true"], [aria-selected="true"]'),
      );
    });
    observer.observe(viewport);
    observer.observe(content);
    // Set on the element, not through state, so scrolling doesn't re-render the tabs.
    const updateFade = () => {
      const start = viewport.scrollLeft > 1;
      const end = viewport.scrollLeft + viewport.clientWidth < viewport.scrollWidth - 1;
      const mask = edgeFadeMask(start, end);
      viewport.style.maskImage = mask;
      viewport.style.setProperty("-webkit-mask-image", mask);
    };
    const fadeObserver = new ResizeObserver(updateFade);
    fadeObserver.observe(viewport);
    fadeObserver.observe(content);
    viewport.addEventListener("scroll", updateFade, { passive: true });
    viewport.addEventListener("wheel", wheel, { passive: false });
    return () => {
      observer.disconnect();
      fadeObserver.disconnect();
      viewport.removeEventListener("scroll", updateFade);
      viewport.removeEventListener("wheel", wheel);
    };
  }, []);

  return (
    <div
      ref={viewportRef}
      data-workspace-tabs-viewport
      className="no-drag min-w-0 flex-1 overflow-x-auto overflow-y-hidden [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
    >
      <div ref={contentRef} className="flex w-max items-center gap-0.5">
        {children}
      </div>
    </div>
  );
}
