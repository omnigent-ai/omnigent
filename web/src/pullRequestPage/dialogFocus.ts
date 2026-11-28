// Where a confirmation's focus starts, such as on its expected answer so Enter gives it.

import type { RefObject } from "react";

/**
 * An `onOpenAutoFocus` for `DialogContent` that focuses `target` instead of the first control.
 * A target that can't take focus, such as a disabled button, leaves the default.
 */
export function focusOnOpen(target: RefObject<HTMLElement | null>): (event: Event) => void {
  return (event) => {
    const element = target.current;
    if (event.defaultPrevented || !element) return;
    element.focus();
    if (document.activeElement === element) event.preventDefault();
  };
}
