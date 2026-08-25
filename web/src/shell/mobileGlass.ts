// Shared iOS-style "liquid glass" treatment for the mobile chat header.
//
// The header paints no background and chat scrolls underneath it, so its
// controls — and the menu they open — sit on their own translucent, blurred
// surface, the way native iOS floating chrome does. All classes are `max-md:`
// so the desktop header, which sits above the conversation rather than over
// it, is untouched.

/** Translucent blurred surface: controls and the menus they open. */
export const MOBILE_GLASS_SURFACE =
  "max-md:border max-md:border-black/[0.06] max-md:bg-background/70 max-md:shadow-[0_6px_20px_-4px_rgb(0_0_0/0.18)] max-md:backdrop-blur-xl max-md:backdrop-saturate-150 dark:max-md:border-white/10 dark:max-md:bg-background/60";

/**
 * A control cluster's floating pill. No padding of its own so a single
 * ``size="icon"`` child stays exactly 40px — the sidebar toggle and the
 * session kebab must read as the same size.
 */
export const MOBILE_GLASS_PILL = `${MOBILE_GLASS_SURFACE} max-md:rounded-full`;
