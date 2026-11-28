// What makes a region inert for focus, keys and hit testing: the `inert` attribute, or an
// off-screen region whose contents are hidden in its place (useInertRef's `offScreen`).

/** Marks an off-screen element whose contents are hidden in place of `inert`. */
export const OFF_SCREEN_INERT_ATTRIBUTE = "data-inert-offscreen";

/** Matches an element that is inert, by the attribute or hidden off screen in its place. */
export const INERT_SELECTOR = `[inert], [${OFF_SCREEN_INERT_ATTRIBUTE}]`;
