export const CHAT_COLUMN_WIDTH =
  "max-w-3xl min-[1921px]:max-w-4xl min-[2561px]:max-w-[clamp(64rem,40vw,100rem)]";

/**
 * Composer popovers grow upward (`bottom-full`) toward ChatHeader
 * (`absolute z-30`, no paint). Stay in this band so they stack above the
 * transcript but never enter the header box.
 */
export const COMPOSER_POPOVER_Z = "z-20";

/**
 * Cap at the `max-h-64` step of the spacing scale; on short windows shrink to
 * what fits above the composer card: viewport minus the header band
 * (`--omnigent-header-height`) minus the card + gap (~10.5rem).
 */
export const COMPOSER_POPOVER_MAX_H =
  "max-h-[min(--spacing(64),calc(100svh_-_var(--omnigent-header-height)_-_10.5rem))]";
