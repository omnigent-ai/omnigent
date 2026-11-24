// The pointer activation every drag-to-reorder list shares, so a click or a scroll never
// starts a drag: a mouse moves 5px first, and a finger holds still for 250ms.

/** A mouse drag starts once the pointer has moved this far; a shorter press stays a click. */
export const DRAG_MOUSE_SENSOR_OPTIONS = { activationConstraint: { distance: 5 } } as const;

/** A touch drag starts after a still hold, so a swipe still scrolls the list. */
export const DRAG_TOUCH_SENSOR_OPTIONS = {
  activationConstraint: { delay: 250, tolerance: 8 },
} as const;
