import type { WheelEventHandler } from "react";

/**
 * Wheel handler for a layer that opens in a portal while a modal Dialog is up.
 *
 * The Dialog's scroll lock listens on `document` and cancels every wheel event
 * it cannot place inside the dialog, which freezes a menu or popover list that
 * lives outside the dialog's content. Stopping the event once the layer has
 * handled it keeps the browser's own scrolling of the layer intact.
 */
export function keepWheelInsidePortal<Element extends HTMLElement>(
  onWheel?: WheelEventHandler<Element>,
): WheelEventHandler<Element> {
  return (event) => {
    onWheel?.(event);
    event.stopPropagation();
  };
}
