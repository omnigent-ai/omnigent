// React 18 drops the boolean `inert` prop, so a hidden panel passed
// `inert={true}` stays focusable. Set the DOM attribute directly instead.

import { type Ref, type RefCallback, useCallback, useInsertionEffect, useRef } from "react";

/**
 * Build a ref callback that toggles the element's `inert` attribute.
 *
 * The callback's identity depends only on `forwardRef`, so toggling `inert`
 * does not detach and re-attach the element's refs.
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
  const elementRef = useRef<T | null>(null);
  const inertRef = useRef(inert);
  // Apply during DOM mutation, as React would for a native prop, so layout
  // effects (e.g. ResponsiveViewerSlot restoring focus) see the final state.
  useInsertionEffect(() => {
    inertRef.current = inert;
    elementRef.current?.toggleAttribute("inert", inert);
  }, [inert]);
  return useCallback(
    (element: T | null) => {
      elementRef.current = element;
      if (typeof forwardRef === "function") forwardRef(element);
      else if (forwardRef) forwardRef.current = element;
      // Covers an element that mounts while `inert` is unchanged.
      element?.toggleAttribute("inert", inertRef.current);
    },
    [forwardRef],
  );
}
