// The row or link keyboard focus is in, so ⌘K can act on what it names. Focus
// moving into the palette or a menu keeps it; focus moving anywhere else lets it go.

import { type FocusEvent, useCallback, useEffect, useRef } from "react";
import { create } from "zustand";
import { regionOfFocusTarget } from "./commandContext";

interface FocusedTargetState<Target> {
  target: Target | null;
  element: HTMLElement | null;
}

/** A focused-target channel: one per kind of thing ⌘K acts on, such as files or PR links. */
export function createFocusedTarget<Target>() {
  const useStore = create<FocusedTargetState<Target>>(() => ({ target: null, element: null }));
  let listening = false;

  function onFocusIn(event: globalThis.FocusEvent) {
    const { element } = useStore.getState();
    if (!element) return;
    const focused = event.target;
    if (focused instanceof Node && element.contains(focused)) return;
    // The palette and menus act on the element focus came from.
    if (regionOfFocusTarget(focused) === undefined) return;
    useStore.setState({ target: null, element: null });
  }

  /** Make `target`, named by `element`, ⌘K's target while focus stays in it. */
  function publish(element: HTMLElement, target: Target): void {
    if (!listening) {
      listening = true;
      document.addEventListener("focusin", onFocusIn);
    }
    useStore.setState({ target, element });
  }

  /** Let `element` go, unless another element has taken over since. */
  function release(element: HTMLElement): void {
    useStore.setState((state) =>
      state.element === element ? { target: null, element: null } : state,
    );
  }

  /**
   * Makes the element that owns `target` ⌘K's target while focus is in it: spread
   * the returned handler as its `onFocus`. A target whose facts change is
   * republished, and an unmounted element lets go.
   */
  function useFocusSource(target: Target | null): (event: FocusEvent<HTMLElement>) => void {
    const elementRef = useRef<HTMLElement | null>(null);
    useEffect(() => {
      const element = elementRef.current;
      if (!element || useStore.getState().element !== element) return;
      if (target) useStore.setState({ target });
      else release(element);
    }, [target]);
    useEffect(
      () => () => {
        if (elementRef.current) release(elementRef.current);
      },
      [],
    );
    return useCallback(
      (event: FocusEvent<HTMLElement>) => {
        elementRef.current = event.currentTarget;
        if (target) publish(event.currentTarget, target);
      },
      [target],
    );
  }

  /** The focused element's target, while it's still on the page. */
  function useFocused(): Target | null {
    return useStore((state) => (state.element?.isConnected ? state.target : null));
  }

  return { useStore, publish, release, useFocusSource, useFocused };
}
