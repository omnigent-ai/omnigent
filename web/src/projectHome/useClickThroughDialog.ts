import { useEffect, useRef, type RefObject } from "react";

const OVERLAY = '[data-slot="dialog-overlay"]';
// How long a dismissed dialog may take to go away before the press counts as kept.
const CLOSE_WAIT_MS = 300;

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
      // A dialog closed through the URL (a file preview) unmounts a few frames later.
      document.addEventListener(
        "pointerup",
        () => {
          const deadline = performance.now() + CLOSE_WAIT_MS;
          const actWhenClosed = () => {
            if (!document.querySelector(`${OVERLAY}[data-state="open"]`)) {
              latestAction.current();
            } else if (performance.now() < deadline) {
              window.setTimeout(actWhenClosed, 16);
            }
          };
          window.setTimeout(actWhenClosed, 0);
        },
        { capture: true, once: true },
      );
    }
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [target]);
}
