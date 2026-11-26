import { useEffect, useRef, type RefObject } from "react";

const OVERLAY = '[data-slot="dialog-overlay"]';

/**
 * A press on a modal dialog's overlay, right over `target`, closes the dialog as usual
 * and then runs `action`, so a covered header button still works in one click.
 * A dialog that stays open, for example to keep unsaved edits, keeps the click.
 */
export function useClickThroughDialog(
  target: RefObject<HTMLElement | null>,
  action: () => void,
): void {
  const latestAction = useRef(action);
  useEffect(() => {
    latestAction.current = action;
  });
  useEffect(() => {
    function onPointerDown(event: PointerEvent) {
      const element = target.current;
      if (event.button !== 0 || !element) return;
      if (!(event.target instanceof Element) || !event.target.closest(OVERLAY)) return;
      const rect = element.getBoundingClientRect();
      const inside =
        event.clientX >= rect.left &&
        event.clientX <= rect.right &&
        event.clientY >= rect.top &&
        event.clientY <= rect.bottom;
      if (!inside || rect.width === 0) return;
      // Mouse presses dismiss on pointerdown and touch on the click after pointerup.
      document.addEventListener(
        "pointerup",
        () =>
          window.setTimeout(() => {
            if (document.querySelector(`${OVERLAY}[data-state="open"]`)) return;
            latestAction.current();
          }, 0),
        { capture: true, once: true },
      );
    }
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [target]);
}
