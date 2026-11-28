// Whether a split divider is being dragged in this window. Frames over panes stop taking the
// pointer meanwhile: a frame under the pointer would otherwise swallow the drag's moves and its
// release, and the divider would never move.

import { useSyncExternalStore } from "react";

export interface PaneDividerDrag {
  /** A divider took the pointer; returns the call that ends that drag, once. */
  begin: () => () => void;
  dragging: () => boolean;
  subscribe: (listener: () => void) => () => void;
}

export function createPaneDividerDrag(): PaneDividerDrag {
  let active = 0;
  const listeners = new Set<() => void>();
  const notify = () => {
    for (const listener of listeners) listener();
  };
  return {
    begin() {
      active += 1;
      if (active === 1) notify();
      let ended = false;
      return () => {
        if (ended) return;
        ended = true;
        active -= 1;
        if (active === 0) notify();
      };
    },
    dragging: () => active > 0,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** The window's one divider drag state. */
export const paneDividerDrag = createPaneDividerDrag();

export function usePaneDividerDragging(drag: PaneDividerDrag = paneDividerDrag): boolean {
  return useSyncExternalStore(drag.subscribe, drag.dragging, drag.dragging);
}
