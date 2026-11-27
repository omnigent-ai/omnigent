import { type RefObject, useLayoutEffect, useRef, useState } from "react";

/**
 * The element's inner width (its `clientWidth`), kept current as it resizes. Measured
 * before the first paint, so a layout chosen from it doesn't flash; 0 until mounted.
 */
export function useElementWidth(ref: RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(0);
  const observed = useRef<{ element: HTMLElement; observer: ResizeObserver | null } | null>(null);
  // The render a measurement caused, which must not measure again.
  const measuredRender = useRef(false);
  // After each commit, before paint: a render that resized the element, such as a session
  // switch to another rail width, paints its new width. The render that measurement causes
  // doesn't measure again, so a layout whose width follows its own measured width can't loop;
  // the observer reports any change after it.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- runs after every commit by design
  useLayoutEffect(() => {
    const element = ref.current;
    if (observed.current?.element !== element) {
      observed.current?.observer?.disconnect();
      observed.current = element ? { element, observer: observeWidth(element, setWidth) } : null;
    }
    if (measuredRender.current) {
      measuredRender.current = false;
      return;
    }
    if (!element || element.clientWidth === width) return;
    measuredRender.current = true;
    setWidth(element.clientWidth);
  });
  useLayoutEffect(
    () => () => {
      observed.current?.observer?.disconnect();
      observed.current = null;
    },
    [],
  );
  return width;
}

function observeWidth(element: HTMLElement, setWidth: (width: number) => void) {
  if (typeof ResizeObserver === "undefined") return null;
  const observer = new ResizeObserver(() => setWidth(element.clientWidth));
  observer.observe(element);
  return observer;
}
