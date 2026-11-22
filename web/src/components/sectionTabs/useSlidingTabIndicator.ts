// One underline under a tab list that slides to the selected tab, instead of each tab fading its
// own in. It only draws: the tabs keep their focus, keys and selection, and their own underline
// shows until the slider is first placed.

import { type RefObject, useLayoutEffect } from "react";

/** Where the underline sits under `tab`, in its positioned list's coordinates. */
export interface TabIndicatorPlacement {
  x: number;
  y: number;
  width: number;
}

/** How far below a tab its own underline starts, as the line tabs draw it. */
export const TAB_UNDERLINE_GAP_PX = 3;

/** The placement under `tab`, the same spot a line tab's own underline takes. */
export function tabIndicatorPlacement(
  tab: Pick<HTMLElement, "offsetLeft" | "offsetTop" | "offsetWidth" | "offsetHeight">,
): TabIndicatorPlacement {
  return {
    x: tab.offsetLeft,
    y: tab.offsetTop + tab.offsetHeight + TAB_UNDERLINE_GAP_PX,
    width: tab.offsetWidth,
  };
}

const SELECTED_TAB = '[role="tab"][aria-selected="true"]';

/**
 * Keep the underline of the tab list inside `containerRef` under its selected tab: as the
 * selection changes, and as the tabs resize (a narrower header drops their icons). The list gets
 * the placement as CSS variables, and `data-indicator-placed` once it has one, which its
 * stylesheet animates from.
 */
export function useSlidingTabIndicator(containerRef: RefObject<HTMLElement | null>): void {
  useLayoutEffect(() => {
    const list = containerRef.current?.querySelector<HTMLElement>('[role="tablist"]');
    if (!list) return;
    const place = () => {
      const tab = list.querySelector<HTMLElement>(SELECTED_TAB);
      // A hidden list measures nothing; it's placed again once it shows.
      if (!tab || tab.offsetWidth === 0) return;
      const { x, y, width } = tabIndicatorPlacement(tab);
      list.style.setProperty("--tab-indicator-x", `${x}px`);
      list.style.setProperty("--tab-indicator-y", `${y}px`);
      list.style.setProperty("--tab-indicator-width", `${width}px`);
      // Placed before the first frame, so it never slides in from the list's edge.
      if (!list.hasAttribute("data-indicator-placed")) {
        requestAnimationFrame(() => list.setAttribute("data-indicator-placed", ""));
      }
    };
    const sizes = new ResizeObserver(place);
    // Tabs that come later, such as Commits once it has a count, are watched as they come.
    const watchTabs = () => {
      for (const tab of list.querySelectorAll('[role="tab"]')) sizes.observe(tab);
    };
    sizes.observe(list);
    watchTabs();
    place();
    const selection = new MutationObserver(() => {
      watchTabs();
      place();
    });
    selection.observe(list, { subtree: true, attributeFilter: ["aria-selected"], childList: true });
    return () => {
      selection.disconnect();
      sizes.disconnect();
    };
  }, [containerRef]);
}
