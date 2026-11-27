// Whether focus can go back to where it was before a surface took it.

import { INERT_SELECTOR } from "@/lib/inert";

/** Whether `element` can take focus back: still on the page, shown, enabled and not inert. */
export function canReturnFocusTo(element: HTMLElement | null): element is HTMLElement {
  if (element === null || !element.isConnected) return false;
  if (element.closest(`${INERT_SELECTOR}, [hidden], [aria-hidden='true']`) !== null) return false;
  if ((element as HTMLButtonElement).disabled === true) return false;
  return element.checkVisibility?.() ?? true;
}
