import { type ReactNode, useEffect, useRef } from "react";

/** Scroll open tabs horizontally with a hidden scrollbar. */
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
      content
        .querySelector<HTMLElement>('[aria-current="true"], [aria-selected="true"]')
        ?.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
    observer.observe(viewport);
    observer.observe(content);
    viewport.addEventListener("wheel", wheel, { passive: false });
    return () => {
      observer.disconnect();
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
