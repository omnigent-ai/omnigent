// Whether a reference has scrolled near the viewport, so only the ones a person can reach soon are
// looked up. One observer serves every reference on the page.

import { type RefObject, useEffect, useState } from "react";

/** How far outside the viewport a reference counts as near. */
const NEAR_MARGIN = "200px 0px";

type Listener = (near: boolean) => void;

let observer: IntersectionObserver | null = null;
const listeners = new Map<Element, Listener>();

function sharedObserver(): IntersectionObserver {
  observer ??= new IntersectionObserver(
    (entries) => {
      for (const entry of entries) listeners.get(entry.target)?.(entry.isIntersecting);
    },
    { rootMargin: NEAR_MARGIN },
  );
  return observer;
}

/** Hears whether `element` is near the viewport until the returned stop is called. */
export function observeNearViewport(element: Element, listener: Listener): () => void {
  listeners.set(element, listener);
  sharedObserver().observe(element);
  return () => {
    listeners.delete(element);
    observer?.unobserve(element);
  };
}

/** Whether `element` has come near the viewport; it stays true once it has. */
export function useHasBeenNearViewport(element: RefObject<Element | null>): boolean {
  const [near, setNear] = useState(false);
  useEffect(() => {
    const target = element.current;
    if (near || !target) return;
    return observeNearViewport(target, (isNear) => {
      if (isNear) setNear(true);
    });
  }, [element, near]);
  return near;
}
