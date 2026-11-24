// When a strip tab counts as focused: the one rule the focus ring and the dividers beside
// a tab both follow, so they never disagree about which tab has focus.

/** Marks a trigger focused by a pointer press, which browsers can still count as `:focus-visible`. */
export function markPointerFocus(trigger: HTMLElement): void {
  trigger.dataset.pointerFocus = "";
}

/** A key press on the trigger makes its focus the keyboard's. */
export function clearPointerFocus(trigger: HTMLElement): void {
  delete trigger.dataset.pointerFocus;
}

/**
 * The trigger has focus from the keyboard: the browser shows focus for it, and no pointer
 * press put it there. Radix focuses a trigger by script on mousedown, which browsers count
 * as `:focus-visible`, so the pointer mark tells the two apart.
 */
export function isKeyboardFocused(trigger: HTMLElement): boolean {
  return trigger.matches(":focus-visible") && !("pointerFocus" in trigger.dataset);
}
