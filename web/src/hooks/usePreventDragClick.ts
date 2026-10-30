import { useEffect } from "react";

/** Cancel the anchor's native click after a drop, before dnd-kit stops propagation. */
export function usePreventDragClick(dragging: boolean): void {
  useEffect(() => {
    if (!dragging) return;
    const preventClick = (event: MouseEvent) => event.preventDefault();
    window.addEventListener("click", preventClick, true);
    // Pointer-up finishes the drag before dispatching its trailing click.
    return () => {
      window.setTimeout(() => window.removeEventListener("click", preventClick, true), 0);
    };
  }, [dragging]);
}
