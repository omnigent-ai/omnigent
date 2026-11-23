// React 18 drops the boolean `inert` prop, so a hidden panel passed
// `inert={true}` stays focusable. Set the DOM attribute directly instead.

import {
  type Ref,
  type RefCallback,
  useCallback,
  useInsertionEffect,
  useLayoutEffect,
  useRef,
} from "react";

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
 * @param options.pending The element is hidden and about to go inert, which a caller
 *   defers because it restyles a large subtree. Until then focus is kept out of it as
 *   inert would: a focused element inside is blurred, and focus that enters goes back.
 * @returns A ref callback to attach to the element.
 */
export function useInertRef<T extends HTMLElement>(
  inert: boolean,
  forwardRef?: Ref<T>,
  { pending = false }: { pending?: boolean } = {},
): RefCallback<T> {
  const elementRef = useRef<T | null>(null);
  const inertRef = useRef(inert);
  // Apply during DOM mutation, as React would for a native prop, so layout
  // effects (e.g. ResponsiveViewerSlot restoring focus) see the final state.
  const keepFocusOut = pending && !inert;
  const keepFocusOutRef = useRef(keepFocusOut);
  useInsertionEffect(() => {
    inertRef.current = inert;
    // Before refs attach, so an element mounting as the guard lifts keeps what it focuses.
    keepFocusOutRef.current = keepFocusOut;
    elementRef.current?.toggleAttribute("inert", inert);
  }, [inert, keepFocusOut]);
  useLayoutEffect(() => {
    if (!keepFocusOut) return;
    blurInside(elementRef.current);
    // Reads the element on each event, so one that mounts while pending is guarded too.
    const onFocusIn = (event: FocusEvent) => {
      const element = elementRef.current;
      const target = event.target;
      if (!element || !(target instanceof HTMLElement) || !element.contains(target)) return;
      const from = event.relatedTarget;
      if (from instanceof HTMLElement && from.isConnected && !element.contains(from)) {
        from.focus({ preventScroll: true });
      } else target.blur();
    };
    document.addEventListener("focusin", onFocusIn, true);
    return () => document.removeEventListener("focusin", onFocusIn, true);
  }, [keepFocusOut]);
  return useCallback(
    (element: T | null) => {
      elementRef.current = element;
      if (typeof forwardRef === "function") forwardRef(element);
      else if (forwardRef) forwardRef.current = element;
      // Covers an element that mounts while `inert` is unchanged.
      element?.toggleAttribute("inert", inertRef.current);
      if (keepFocusOutRef.current) blurInside(element);
    },
    [forwardRef],
  );
}

function blurInside(element: HTMLElement | null) {
  const focused = document.activeElement;
  if (element && focused instanceof HTMLElement && element.contains(focused)) focused.blur();
}
