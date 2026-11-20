// Whether the reader last worked in a surface, for ⌘F and Escape to act there: clicking its
// text focuses nothing, so focus alone can't tell.

import { type RefObject, useEffect, useRef, useState } from "react";

// A click on a tab's non-focusable parts focuses the workspace rail that holds the surface.
// That rail is the surface's own, not a sibling, so focus landing there keeps it active.
function holdsSurface(target: Node, root: HTMLElement): boolean {
  return (
    target instanceof HTMLElement &&
    target.hasAttribute("data-workspace-panel-content") &&
    target.contains(root)
  );
}

/**
 * Whether the reader last clicked or moved focus inside `root`. Showing the surface, and each
 * change of `showing`, makes it the active one again.
 */
export function useLastInteractedSurface(
  root: RefObject<HTMLElement | null>,
  open: boolean,
  showing?: unknown,
) {
  const activeRef = useRef(true);
  // Mirrors the ref for rendering.
  const [active, setActive] = useState(true);
  useEffect(() => {
    if (!open) return;
    activeRef.current = true;
    setActive(true);
  }, [open, showing]);
  useEffect(() => {
    if (!open) return;
    const track = (event: Event) => {
      const element = root.current;
      const target = event.target;
      if (element !== null && target instanceof Node) {
        activeRef.current = element.contains(target) || holdsSurface(target, element);
        setActive(activeRef.current);
      }
    };
    window.addEventListener("mousedown", track, true);
    window.addEventListener("focusin", track, true);
    return () => {
      window.removeEventListener("mousedown", track, true);
      window.removeEventListener("focusin", track, true);
    };
  }, [open, root]);
  return { activeRef, active };
}
