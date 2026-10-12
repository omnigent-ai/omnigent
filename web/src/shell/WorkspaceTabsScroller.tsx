import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";

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
      const arrows = content.clientWidth > container.clientWidth + 1 && container.clientWidth >= 80;
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
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientWidth : 1;
      viewport.scrollLeft += delta * unit;
      measure();
    };
    const observer = new ResizeObserver(() => {
      content
        .querySelector<HTMLElement>('[aria-current="true"], [aria-selected="true"]')
        ?.scrollIntoView({ block: "nearest", inline: "nearest" });
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
    viewport.scrollBy({ left: direction * Math.max(80, viewport.clientWidth * 0.8) });
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
