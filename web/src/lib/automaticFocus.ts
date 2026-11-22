// Focus the app moves or restores on its own, such as a picker returning focus as it
// closes, yields to a dialog, popover, or menu the user has open elsewhere.

/** Open layers that own focus while they're shown; tooltips are not among them. */
const FOCUS_OWNING_LAYER =
  '[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"], [role="menu"][data-state="open"]';

/**
 * Whether an open dialog, popover, or menu above `target` owns focus now. Layers portal to
 * the end of the document as they open, so the last open one is on top; a target inside it
 * isn't blocked by the layers below.
 */
export function isBehindOpenLayer(target: Element): boolean {
  const layers = target.ownerDocument.querySelectorAll(FOCUS_OWNING_LAYER);
  const top = layers[layers.length - 1];
  return top !== undefined && !top.contains(target);
}

/** What a closing picker does with focus: leave it where an action moved it, or restore it. */
export interface ClosingDialogFocus {
  /** Focus sits outside every `dialogSelector` dialog, where something the picker ran put it. */
  movedOn: boolean;
  /** The element focus returns to: the opener, if it's still in the page and nothing covers it. */
  restore: HTMLElement | null;
}

/**
 * As a picker matching `dialogSelector` closes, where focus goes. Radix returns focus only to
 * a dialog trigger, so a picker opened from a shortcut returns it to `opener` itself.
 */
export function closingDialogFocus(
  opener: HTMLElement | null,
  dialogSelector: string,
): ClosingDialogFocus {
  const active = document.activeElement;
  const movedOn =
    active instanceof HTMLElement && active !== document.body && !active.closest(dialogSelector);
  const restore = opener?.isConnected && !isBehindOpenLayer(opener) ? opener : null;
  return { movedOn, restore };
}
