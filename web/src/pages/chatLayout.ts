export const CHAT_COLUMN_WIDTH =
  "max-w-[var(--chat-column-width)] [--chat-column-width:48rem] min-[1921px]:[--chat-column-width:56rem] min-[2561px]:[--chat-column-width:clamp(64rem,40vw,100rem)]";

/** Stack composer popovers below the transparent z-30 header. */
export const COMPOSER_POPOVER_Z = "z-20";

/**
 * Cap a popover anchored in the composer card to the space above that card: 100% is the
 * card's height, and 4rem covers the form padding, Plan-mode status line and mb-2 gap.
 */
export const COMPOSER_POPOVER_MAX_H =
  "max-h-[calc(var(--omnigent-viewport-height,100svh)_-_var(--omnigent-header-height)_-_var(--omnigent-safe-top)_-_var(--omnigent-safe-bottom)_-_100%_-_4rem)]";

const POPOVER_VIEWPORT_INSET = 8;

/**
 * Collision padding for the Radix background-task popover so it opens below the
 * z-30 header (h-14 = 3.5rem); other sides keep the default 8px viewport inset.
 */
export const COMPOSER_POPOVER_HEADER_INSET = {
  top: 56,
  right: POPOVER_VIEWPORT_INSET,
  bottom: POPOVER_VIEWPORT_INSET,
  left: POPOVER_VIEWPORT_INSET,
};

/** {@link COMPOSER_POPOVER_HEADER_INSET}, lowered to clear a header that native shells move down. */
export function measureComposerPopoverHeaderInset(): typeof COMPOSER_POPOVER_HEADER_INSET {
  const headerBottom = Math.max(
    0,
    ...Array.from(
      document.querySelectorAll(".chat-header"),
      (header) => header.getBoundingClientRect().bottom,
    ),
  );
  const top = Math.max(
    COMPOSER_POPOVER_HEADER_INSET.top,
    Math.ceil(headerBottom) + POPOVER_VIEWPORT_INSET,
  );
  return { ...COMPOSER_POPOVER_HEADER_INSET, top };
}
