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
