// One tab stop for a list of buttons: ↑/↓ move between them, Home and End jump
// to the ends, and Enter or Space activate the focused one as buttons do.

import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";

/** Where `key` moves focus from `index` in a list of `count`, or null for a key it ignores. */
export function rovingFocusTarget(key: string, index: number, count: number): number | null {
  if (count === 0) return null;
  switch (key) {
    case "ArrowDown":
      return Math.min(index + 1, count - 1);
    case "ArrowUp":
      return Math.max(index - 1, 0);
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}

export interface RovingFocusOptions {
  /** The item that holds the tab stop at first, such as the one last opened. */
  initialIndex?: number;
  /** A pending request to focus the tab stop's item, as after returning to the list. */
  focusRequest?: object | null;
  /** The request was carried out; it isn't repeated if the list mounts again. */
  onFocusRequestSettled?: () => void;
}

export function useRovingFocus(
  count: number,
  { initialIndex = 0, focusRequest, onFocusRequestSettled }: RovingFocusOptions = {},
) {
  const [activeIndex, setActiveIndex] = useState(initialIndex);
  const items = useRef<(HTMLElement | null)[]>([]);
  const current = count === 0 ? 0 : Math.min(Math.max(activeIndex, 0), count - 1);

  const settled = useRef(onFocusRequestSettled);
  settled.current = onFocusRequestSettled;

  useEffect(() => {
    const item = items.current[current];
    if (!focusRequest || !item) return;
    item.focus();
    settled.current?.();
    // Only a new request moves focus; the tab stop changing doesn't.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusRequest]);

  const itemProps = useCallback(
    (index: number) => ({
      ref: (element: HTMLElement | null) => {
        items.current[index] = element;
      },
      tabIndex: index === current ? 0 : -1,
      onFocus: () => setActiveIndex(index),
      onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
        if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
        const target = rovingFocusTarget(event.key, index, count);
        if (target === null) return;
        event.preventDefault();
        setActiveIndex(target);
        items.current[target]?.focus();
      },
    }),
    [count, current],
  );
  return { itemProps, activeIndex: current };
}
