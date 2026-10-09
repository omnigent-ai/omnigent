import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";

// Narrowest slot that gets arrow buttons, and the smallest arrow scroll step.
// tests/browser_ui/files/test_workspace_tab_overflow.py asserts the same width.
const MIN_SCROLLER_WIDTH_PX = 80;

/** Scroll open tabs while keeping navigation controls outside the viewport. */
export function WorkspaceTabsScroller({ children }: { children: ReactNode }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ arrows: false, left: false, right: false });

  useEffect(() => {
    const container = containerRef.current;
    const viewport = viewportRef.current;
    const content = contentRef.current;
    if (!container || !viewport || !content) return;
    const measure = () => {
      // Compare with the full slot so the arrows cannot sustain overflow.
      const arrows =
        content.clientWidth > container.clientWidth + 1 &&
        container.clientWidth >= MIN_SCROLLER_WIDTH_PX;
      const left = viewport.scrollLeft > 1;
      const right = viewport.scrollWidth - viewport.clientWidth - viewport.scrollLeft > 1;
      setEdges((previous) =>
        previous.arrows === arrows && previous.left === left && previous.right === right
          ? previous
          : { arrows, left, right },
      );
    };
    const wheel = (event: WheelEvent) => {
      if (viewport.scrollWidth <= viewport.clientWidth) return;
      // Leave native horizontal gestures alone. Ctrl-wheel must be non-passive
      // to scroll tabs instead of zooming the page.
      if (event.deltaX && !event.ctrlKey) return;
      const delta = event.deltaX || event.deltaY;
      if (!delta) return;
      event.preventDefault();
      let unit = 1;
      if (event.deltaMode === WheelEvent.DOM_DELTA_LINE) unit = 16;
      else if (event.deltaMode === WheelEvent.DOM_DELTA_PAGE) unit = viewport.clientWidth;
      viewport.scrollLeft += delta * unit;
      measure();
    };
    const observer = new ResizeObserver((entries) => {
      // Only a resized slot re-reveals the selected tab; tab content changing
      // (a tab closing, a label resolving) keeps the user's scroll position.
      if (entries.some((entry) => entry.target !== content)) {
        content
          .querySelector<HTMLElement>('[aria-current="true"], [aria-selected="true"]')
          ?.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
      measure();
    });
    observer.observe(container);
    observer.observe(viewport);
    observer.observe(content);
    viewport.addEventListener("scroll", measure);
    viewport.addEventListener("wheel", wheel, { passive: false });
    measure();
    return () => {
      observer.disconnect();
      viewport.removeEventListener("scroll", measure);
      viewport.removeEventListener("wheel", wheel);
    };
  }, []);

  const scroll = (direction: number) => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    viewport.scrollBy({
      left: direction * Math.max(MIN_SCROLLER_WIDTH_PX, viewport.clientWidth * 0.8),
    });
  };
  return (
    <div ref={containerRef} className="no-drag flex min-w-0 flex-1 items-center">
      {edges.arrows && (
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          className="size-6 shrink-0"
          aria-label="Scroll tabs left"
          disabled={!edges.left}
          onClick={() => scroll(-1)}
        >
          <ChevronLeftIcon className="size-4" />
        </Button>
      )}
      <div
        ref={viewportRef}
        data-workspace-tabs-viewport
        className="no-drag min-w-0 flex-1 overflow-x-auto overflow-y-hidden [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        <div ref={contentRef} className="flex w-max items-center gap-0.5">
          {children}
        </div>
      </div>
      {edges.arrows && (
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          className="size-6 shrink-0"
          aria-label="Scroll tabs right"
          disabled={!edges.right}
          onClick={() => scroll(1)}
        >
          <ChevronRightIcon className="size-4" />
        </Button>
      )}
    </div>
  );
}
