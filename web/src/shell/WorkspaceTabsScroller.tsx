import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

const CompactWorkspaceTabsContext = createContext(false);

/** Whether open tabs should render icon-only because their full labels don't fit. */
export function useCompactWorkspaceTabs(): boolean {
  return useContext(CompactWorkspaceTabsContext);
}

/**
 * Decides whether the open tabs show icons only. `fullWidth` is the labelled tabs' width at
 * their narrowest, measured when they last overflowed; compact tabs widen again once it fits.
 */
export function shouldCompactTabs({
  compact,
  contentWidth,
  viewportWidth,
  fullWidth,
}: {
  compact: boolean;
  contentWidth: number;
  viewportWidth: number;
  fullWidth: number;
}): boolean {
  if (viewportWidth <= 0) return compact;
  return compact ? viewportWidth < fullWidth : contentWidth > viewportWidth;
}

/**
 * Scrolls the tab strip just far enough to show `tab`. Unlike `scrollIntoView`, it
 * moves no other scroller: the rail slides in from past the window's right edge,
 * so revealing a tab there would shift the whole page sideways.
 */
export function scrollTabIntoView(tab: HTMLElement | null): void {
  const viewport = tab?.closest<HTMLElement>("[data-workspace-tabs-viewport]");
  if (!tab || !viewport) return;
  const left = viewport.getBoundingClientRect().left + viewport.clientLeft;
  const right = left + viewport.clientWidth;
  const box = tab.getBoundingClientRect();
  if (box.left < left) {
    // A tab wider than the strip whose end already shows keeps its close button in view.
    if (box.right <= right && box.width > viewport.clientWidth) return;
    viewport.scrollLeft -= left - box.left;
  } else if (box.right > right) {
    // A tab wider than the strip keeps its start in view.
    viewport.scrollLeft += Math.min(box.right - right, box.left - left);
  }
}

/**
 * Scroll open tabs horizontally with a hidden scrollbar. When their labels overflow the
 * strip, tabs switch to icons only (see {@link useCompactWorkspaceTabs}).
 */
export function WorkspaceTabsScroller({
  children,
  tabCount = 0,
}: {
  children: ReactNode;
  /** Number of open tabs; a change re-measures them with labels before painting. */
  tabCount?: number;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [compact, setCompact] = useState(false);
  const compactRef = useRef(false);
  compactRef.current = compact;
  // Width of the tabs with labels, as measured when they last overflowed.
  const fullWidthRef = useRef(0);
  const fitRef = useRef(() => {});
  fitRef.current = () => {
    const viewport = viewportRef.current;
    const content = contentRef.current;
    if (!viewport || !content) return;
    // Tabs shrink to fit, so overflow means even their narrowest labelled width is too wide.
    const contentWidth = content.scrollWidth;
    if (!compactRef.current) fullWidthRef.current = contentWidth;
    const next = shouldCompactTabs({
      compact: compactRef.current,
      contentWidth,
      viewportWidth: viewport.clientWidth,
      fullWidth: fullWidthRef.current,
    });
    if (next !== compactRef.current) setCompact(next);
  };
  const measuredTabCountRef = useRef(tabCount);
  useLayoutEffect(() => {
    // Opening or closing a tab changes the labelled width: show labels again and re-measure.
    if (measuredTabCountRef.current !== tabCount) {
      measuredTabCountRef.current = tabCount;
      if (compact) {
        setCompact(false);
        return;
      }
    }
    fitRef.current();
  }, [tabCount, compact]);

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
      fitRef.current();
      scrollTabIntoView(
        content.querySelector<HTMLElement>('[aria-current="true"], [aria-selected="true"]'),
      );
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
      <div
        ref={contentRef}
        data-compact={compact || undefined}
        className="flex w-full min-w-0 items-center gap-0.5"
      >
        <CompactWorkspaceTabsContext.Provider value={compact}>
          {children}
        </CompactWorkspaceTabsContext.Provider>
      </div>
    </div>
  );
}
