// Whether focus is in a find bar: the app's own (FindBar marks its root) or the code editor's.
// A second ⌘F from inside one goes on to the app's find; from anywhere else it refocuses it.

/** The attribute FindBar puts on its root. */
export const FIND_BAR_ATTRIBUTE = "data-find-bar";
const FIND_BAR_SELECTOR = `[${FIND_BAR_ATTRIBUTE}], .monaco-editor .find-widget`;

/** Whether `document`'s focus is in a find bar, its field or any of its buttons. */
export function isFocusInFindBar(target: Document = document): boolean {
  const focused = target.activeElement;
  return focused instanceof Element && focused.closest(FIND_BAR_SELECTOR) !== null;
}
