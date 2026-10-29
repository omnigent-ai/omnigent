// React 18 drops the boolean `inert` prop, so a hidden panel passed
// `inert={true}` stays focusable. Set the DOM attribute directly instead.

import { type Ref, type RefCallback, useCallback } from "react";

/**
 * Build a ref callback that toggles the element's `inert` attribute.
 *
 * @param inert Whether the element and its subtree should be inert, e.g.
 *   `!open` for a collapsed panel.
 * @param forwardRef Optional ref the element is also assigned to, for elements
 *   that already need a ref of their own.
 * @returns A ref callback to attach to the element.
 */
export function useInertRef<T extends HTMLElement>(
  inert: boolean,
  forwardRef?: Ref<T>,
): RefCallback<T> {
  return useCallback(
    (element: T | null) => {
      if (typeof forwardRef === "function") forwardRef(element);
      else if (forwardRef) forwardRef.current = element;
      element?.toggleAttribute("inert", inert);
    },
    [inert, forwardRef],
  );
}
