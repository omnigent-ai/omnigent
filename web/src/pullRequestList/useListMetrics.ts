// The list's item heights at its current width, so the window's offsets and spacers match the
// rows the CSS container query draws, before and after the list is resized.

import { type RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";

import { type ListMetrics, listMetrics, WIDE_LIST_METRICS } from "./listWindow";

export interface MeasuredListMetrics {
  /** The heights this render lays the list out with. */
  metrics: ListMetrics;
  /** The heights the latest measurement found, which the next render lays out with. */
  measured: RefObject<ListMetrics>;
}

export function useListMetrics(listRef: RefObject<HTMLElement | null>): MeasuredListMetrics {
  const [metrics, setMetrics] = useState(WIDE_LIST_METRICS);
  const measured = useRef(WIDE_LIST_METRICS);
  const observed = useRef<{ element: HTMLElement; observer?: ResizeObserver } | null>(null);
  // The list element mounts and unmounts with the placeholder, so each commit checks it.
  useLayoutEffect(() => {
    const element = listRef.current;
    if (observed.current?.element === element) return;
    observed.current?.observer?.disconnect();
    observed.current = null;
    if (!element) return;
    const update = (width: number) => {
      measured.current = listMetrics(width);
      setMetrics(measured.current);
    };
    // The container query sizes by the content box, which clientWidth gives without a gutter.
    update(element.clientWidth);
    const observer =
      typeof ResizeObserver === "undefined"
        ? undefined
        : new ResizeObserver((entries) => {
            const entry = entries.at(-1);
            if (entry) update(entry.contentRect.width);
          });
    observer?.observe(element);
    observed.current = { element, observer };
  });
  useEffect(
    () => () => {
      observed.current?.observer?.disconnect();
      observed.current = null;
    },
    [],
  );
  return { metrics, measured };
}
