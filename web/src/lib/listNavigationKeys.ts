// Ctrl-N/Ctrl-P as one-row steps in an open pick list, as in Chrome's omnibox,
// VS Code's quick open and macOS text fields. Each list maps a step onto its own
// ArrowDown/ArrowUp handling, so wrap, disabled rows and scrolling stay the same.

export type ListStep = "next" | "previous";

/** Marks the query field (or the editor wrapper) of a list while that list is open. */
export const PICK_LIST_INPUT_ATTRIBUTE = "data-pick-list-input";

interface PickListInputAttributes {
  [PICK_LIST_INPUT_ATTRIBUTE]?: "";
}

/** Spread on a list's input: present only while the list is open. */
export function pickListInputProps(open: boolean): PickListInputAttributes {
  return open ? { [PICK_LIST_INPUT_ATTRIBUTE]: "" } : {};
}

const ROW_SELECTOR =
  '[role="option"], [role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"]';
const LIST_SELECTOR = '[role="listbox"], [role="menu"]';
const IME_PROCESSING_KEY_CODE = 229;

/** The fields a key event carries, whether React's synthetic event or the DOM's own. */
export interface ListKeyEvent {
  key: string;
  code?: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  defaultPrevented: boolean;
  target?: EventTarget | null;
  isComposing?: boolean;
  keyCode?: number;
  nativeEvent?: { isComposing?: boolean; keyCode?: number };
}

function isComposing(event: ListKeyEvent): boolean {
  return (
    event.isComposing === true ||
    event.nativeEvent?.isComposing === true ||
    event.keyCode === IME_PROCESSING_KEY_CODE ||
    event.nativeEvent?.keyCode === IME_PROCESSING_KEY_CODE
  );
}

/** Whether `event` is Ctrl alone with `letter`; a non-Latin layout falls back to the physical key. */
function isControlLetter(event: ListKeyEvent, letter: "n" | "p"): boolean {
  if (!event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return false;
  const key = event.key.toLowerCase();
  if (/^[a-z]$/.test(key)) return key === letter;
  return event.code === `Key${letter.toUpperCase()}`;
}

/** Ctrl-N / Ctrl-P as a step, or null; ignores IME composition and keys already handled. */
export function readlineListStep(event: ListKeyEvent): ListStep | null {
  if (event.defaultPrevented || isComposing(event)) return null;
  if (isControlLetter(event, "n")) return "next";
  if (isControlLetter(event, "p")) return "previous";
  return null;
}

function asElement(target: EventTarget | null | undefined): Element | null {
  return typeof Element !== "undefined" && target instanceof Element ? target : null;
}

/** The menu or listbox row `target` is, or null when it's anything else (a nested editor, say). */
function listRow(target: EventTarget | null | undefined): Element | null {
  const element = asElement(target);
  if (!element?.matches(ROW_SELECTOR)) return null;
  return element.closest(LIST_SELECTOR) ? element : null;
}

function isEditable(element: Element): boolean {
  if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) return true;
  // The attribute, not isContentEditable, which jsdom doesn't implement.
  return element.matches('[contenteditable]:not([contenteditable="false"])');
}

/**
 * Whether `target` steps a list on Ctrl-N/Ctrl-P: an open list's marked input
 * (or the text field inside its marked wrapper), or a focused menu or listbox row.
 */
export function isPickListTarget(target: EventTarget | null | undefined): boolean {
  const element = asElement(target);
  if (!element) return false;
  if (listRow(element) !== null) return true;
  return isEditable(element) && element.closest(`[${PICK_LIST_INPUT_ATTRIBUTE}]`) !== null;
}

/** Whether an open pick list takes this Ctrl-N/Ctrl-P, so an app-wide chord must leave it alone. */
export function pickListOwnsKey(event: ListKeyEvent): boolean {
  return readlineListStep(event) !== null && isPickListTarget(event.target);
}

/**
 * For a Radix menu or select content's onKeyDown: turns Ctrl-N/Ctrl-P on one of
 * its own rows into the ArrowDown/ArrowUp Radix already handles, so its focus,
 * looping and disabled-row skipping run unchanged. A submenu's key, bubbling
 * through the portal into its parent menu, is left to the submenu.
 */
export function translateReadlineStepInList(
  event: ListKeyEvent & { currentTarget: EventTarget | null; preventDefault: () => void },
): void {
  const step = readlineListStep(event);
  if (step === null) return;
  const row = listRow(event.target);
  if (!row || row.closest(LIST_SELECTOR) !== event.currentTarget) return;
  event.preventDefault();
  const key = step === "next" ? "ArrowDown" : "ArrowUp";
  row.dispatchEvent(
    new KeyboardEvent("keydown", { key, code: key, bubbles: true, cancelable: true }),
  );
}

type TranslatableKeyEvent = Parameters<typeof translateReadlineStepInList>[0];

/** A content onKeyDown that runs the caller's first; a caller that prevents the key keeps it. */
export function withReadlineListSteps<Event extends TranslatableKeyEvent>(
  onKeyDown?: (event: Event) => void,
): (event: Event) => void {
  return (event) => {
    onKeyDown?.(event);
    translateReadlineStepInList(event);
  };
}

/** The least a key event from any list handler carries; missing modifiers read as up. */
interface LooseKeyEvent {
  key: string;
  code?: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  defaultPrevented?: boolean;
  nativeEvent?: { isComposing?: boolean; keyCode?: number };
  preventDefault: () => void;
}

/**
 * For an open list with no rows yet (empty or loading): takes Ctrl-N/Ctrl-P, which have
 * nowhere to go, so neither the app nor the browser (Print) acts on them. Arrows pass.
 */
export function holdReadlineStep(event: LooseKeyEvent): boolean {
  const step = readlineListStep({
    key: event.key,
    code: event.code,
    ctrlKey: event.ctrlKey === true,
    metaKey: event.metaKey === true,
    altKey: event.altKey === true,
    shiftKey: event.shiftKey === true,
    defaultPrevented: event.defaultPrevented === true,
    nativeEvent: event.nativeEvent,
  });
  if (step === null) return false;
  event.preventDefault();
  return true;
}
