// Shared translucent treatment for mobile header menus.
//
// Menu popovers need an opaque-enough surface over scrolling content. All
// classes are `max-md:` so desktop menus are untouched.

/** Translucent blurred surface for menus opened from mobile header controls. */
export const MOBILE_GLASS_SURFACE =
  "max-md:border max-md:border-black/[0.06] max-md:bg-background/70 max-md:shadow-[0_6px_20px_-4px_rgb(0_0_0/0.18)] max-md:backdrop-blur-xl max-md:backdrop-saturate-150 dark:max-md:border-white/10 dark:max-md:bg-background/60";
